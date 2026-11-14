"""MowerStateService — merges the live PbOutput feed into typed dashboard state.

Responsibilities (mirrors the Lymow-One-MQTT coordinator, without HA):
  - decode + merge every pboutput payload (app.lymow.state.merge rules)
  - zone-catalog lifecycle: parse btMap replies, sticky-guard against
    firmware sending an empty/partial catalog after a good one
  - error-code lifecycle: clear cached errorCodes when robotStatus
    leaves ERROR (arch.md §7c) — firmware repeats codes while erroring
    and does not send an explicit empty list when cleared
  - current-zone spatial cache
  - staleness tracking so the UI can mark old telemetry
  - build the JSON snapshot consumed by REST + WebSocket clients
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from app.lymow import state as lstate
from app.lymow import state_matrix, telemetry as ltelemetry
from app.lymow.const import WORK_STATUS_ERROR
from app.lymow.protocol import decode_pboutput_envelope, parse_zone_catalog
from app.services.provider import MowerProvider

_LOGGER = logging.getLogger(__name__)

# Telemetry older than this (while transport is up) is rendered stale.
# The live mower idles at ~30s broadcasts; 3 missed broadcasts.
STALE_AFTER_S = 95.0


class MowerStateService:
    def __init__(self, provider: MowerProvider) -> None:
        self.provider = provider
        self.state: dict[str, Any] = {}
        self.last_pboutput_mono: float | None = None
        self.last_pboutput_wall: float | None = None
        self.transport_online = False
        self._listeners: list[Callable[[dict], None]] = []
        self._last_clean_report_key: tuple | None = None
        self.on_clean_report: Callable[[dict], None] | None = None

    # ── wiring ──────────────────────────────────────────────────

    def attach(self) -> None:
        """Install ourselves as the provider's message sink."""
        self.provider.on_pboutput = self._on_pboutput
        self.provider.on_connection_changed = self._on_connection_changed
        # Real provider's QUERY_MAP refire rule reads robotInfo/zone_catalog
        # straight from our live state dict.
        if hasattr(self.provider, "_state_ref"):
            self.provider._state_ref = self.state  # noqa: SLF001

    def add_listener(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    # ── ingestion ───────────────────────────────────────────────

    def _on_pboutput(self, raw: bytes) -> None:
        try:
            msg = decode_pboutput_envelope(raw)
        except Exception:
            _LOGGER.exception("undecodable pboutput payload dropped")
            return
        try:
            self._merge(msg)
        except Exception:
            _LOGGER.exception("pboutput merge failed; state left untouched")
            return
        self.last_pboutput_mono = time.monotonic()
        self.last_pboutput_wall = time.time()
        self._notify()

    def _merge(self, msg) -> None:
        # error_cleared recipe: robotStatus 7 -> non-7 clears cached codes
        ri = msg.robotInfo if msg.ListFields() and any(
            f.name == "robotInfo" for f, _ in msg.ListFields()
        ) else None
        if ri is not None and getattr(ri, "robotStatus", None) != WORK_STATUS_ERROR:
            prev_ri = self.state.get("robotInfo")
            if prev_ri is not None and getattr(prev_ri, "robotStatus", None) == WORK_STATUS_ERROR:
                self.state["errorCodes"] = []

        had_btmap = any(f.name == "btMap" for f, _ in msg.ListFields())
        if had_btmap:
            new_catalog = parse_zone_catalog(msg.btMap)
            old = self.state.get("zone_catalog")
            if lstate.is_real_zone_catalog(new_catalog) or old is None or not old.zones:
                self.state["zone_catalog"] = new_catalog
                # Replace semantics only for the parsed catalog; drop the
                # raw blob so it doesn't alias into the next merge.
            # Raw blob merge happens below regardless for completeness.

        lstate.merge_pboutput(self.state, msg)
        lstate.compute_current_zone_cache(self.state)

        cr = self.state.get("cleanReport")
        new_report = False
        if cr is not None:
            key = (
                getattr(cr, "cleanStartTime", None),
                getattr(cr, "mowEndType", None),
                getattr(cr.cleanInfo, "cleanTime", 0) if cr.ByteSize() else 0,
            )
            if key != self._last_clean_report_key:
                new_report = True
                self._last_clean_report_key = key
                if self.on_clean_report:
                    self.on_clean_report({
                        "start_time": key[0],
                        "end_type": key[1],
                        "duration_s": cr.cleanInfo.cleanTime,
                        "area_m2": cr.cleanInfo.cleanArea,
                        "used_battery": getattr(cr, "usedBattery", None),
                    })

        # Upstream refire rule: re-ask for the zone catalog when a task
        # starts or completes, so task-zone highlights don't go stale on
        # schedules we didn't start ourselves. No-op on the simulator.
        refire = getattr(self.provider, "maybe_refire_query_map", None)
        if refire is not None:
            refire(cleanreport_arrived=new_report)

    def _on_connection_changed(self, online: bool) -> None:
        self.transport_online = online
        _LOGGER.info("mower transport %s", "online" if online else "offline")
        self._notify()

    def _notify(self) -> None:
        snap = self.snapshot()
        for cb in self._listeners:
            try:
                cb(snap)
            except Exception:
                _LOGGER.exception("state listener failed")

    # ── views ───────────────────────────────────────────────────

    def is_stale(self) -> bool:
        if self.last_pboutput_mono is None:
            return True
        return (time.monotonic() - self.last_pboutput_mono) > STALE_AFTER_S

    def current_row(self) -> state_matrix.StateRow:
        """Matrix row with the single no-telemetry gate: no robotInfo seen
        yet -> DEFAULT_ROW (unknown state offers no buttons). Single home —
        dashboard and command gate must always agree on what is allowed."""
        if self.state.get("robotInfo") is None:
            return state_matrix.DEFAULT_ROW
        t = ltelemetry.telemetry_from_wire(self.state)
        return state_matrix.lookup(
            work_status=t.get("work_status") or 0,
            robot_status=t.get("robot_status") or 0,
            is_recharging=bool(t.get("is_recharging")),
        )

    def snapshot(self) -> dict[str, Any]:
        """Full dashboard state document (also sent on every WS push)."""
        t = ltelemetry.telemetry_from_wire(self.state)
        if t.get("ip_address") is None:
            rest_ip = getattr(self.provider, "rest_ip_address", None)
            if rest_ip:
                t["ip_address"] = rest_ip
        if t.get("fw_version") is None:
            rest_fw = getattr(self.provider, "rest_firmware_version", None)
            if rest_fw:
                t["fw_version"] = rest_fw
        status = ltelemetry.describe_status(t)
        row = self.current_row()
        catalog = self.state.get("zone_catalog")
        task_zones = [
            {"hash_id": z.hash_id, "name": z.name, "mow_order": z.mow_order}
            for z in lstate.current_task_zones(self.state)
        ]
        pose = self.state.get("pose")
        pose_out = (
            {"x": pose.x, "y": pose.y, "theta": pose.theta} if pose is not None else None
        )
        return {
            "telemetry": t,
            "status": status,
            "activity": row.activity,
            "allowed_actions": state_matrix.allowed_actions(row),
            "online": bool(self.transport_online),
            "stale": self.is_stale(),
            "simulator": bool(self.provider.simulator),
            "device_id": self.provider.device_id,
            "task_zones": task_zones,
            "map_ready": bool(catalog and catalog.zones),
            "pose": pose_out,
            "last_update_epoch": self.last_pboutput_wall,
        }
