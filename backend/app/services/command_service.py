"""MowerCommandService — validated, audited, never auto-retried commands.

Safety contract (user requirement):
  - every command is gated against the state matrix before publish, so
    the UI can't send e.g. RESUME while mowing or DOCK during e-stop;
  - destructive/irreversible actions (task-cancel-dock, force re-init)
    require an explicit `confirm=true` from the caller;
  - a 2.5s post-state watchdog flags firmware silent-rejects (arch.md
    §11) — we report "unconfirmed", we NEVER auto-retry: most commands
    are not idempotent (a double CLEAN could start a second task);
  - every attempt lands in the command_audit table with its outcome.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any

from app.db import Database
from app.lymow import state as lstate
from app.lymow import state_matrix
from app.lymow.const import (
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_FORCE_REINIT,
    USER_CTRL_QUERY_MAP,
    USER_CTRL_QUERY_SCHEDULES,
    USER_CTRL_RESUME,
    USER_CTRL_RESUME_DOCK,
)
from app.lymow.protocol import (
    encode_query_map,
    encode_set_rr_config,
    encode_start_zones,
    encode_userctrl,
)
from app.lymow.userctrl import EXPECTED_POST_STATES
from app.services.provider import MowerProvider, ProviderError
from app.services.state_service import MowerStateService

_LOGGER = logging.getLogger(__name__)

# Firmware confirmation window before we call a command silently rejected
# (value verified against Lymow-One-MQTT's watchdog).
WATCHDOG_S = 2.5
# Preflight QUERY_MAP budget for zone-start with an unloaded catalog.
PREFLIGHT_S = 4.0

DESTRUCTIVE_ACTIONS = {"dock_cancel", "force_reinit"}


class CommandRejected(Exception):
    """Command refused before reaching the transport (validation gate)."""


def _audited(action: str):
    """Audit refusals too: the log should show attempts, not just sends.

    The command lock serializes check+publish so two concurrent requests
    (two tabs) can't both pass the matrix gate and double-send a
    non-idempotent command to the mower.
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            try:
                async with self._cmd_lock:
                    return await fn(self, *args, **kwargs)
            except CommandRejected as exc:
                detail = {"zones": kwargs["zones"]} if kwargs.get("zones") else None
                await self._audit(action, 0, detail, f"rejected:{exc}")
                raise
        return wrapper
    return deco


class CommandService:
    def __init__(
        self, provider: MowerProvider, state: MowerStateService, db: Database
    ) -> None:
        self.provider = provider
        self.state = state
        self.db = db
        self._cmd_lock = asyncio.Lock()

    # ── public control surface ──────────────────────────────────

    @_audited("start")
    async def start(self, zones: list[str] | None = None) -> dict[str, Any]:
        row = self._current_row()
        ctrl = row.start_mowing
        if ctrl is None:
            raise CommandRejected(
                f"start not allowed while activity={row.activity or 'unknown'}"
            )
        if zones:
            try:
                resolved = lstate.resolve_zones(
                    self.state.state.get("zone_catalog"), zones
                )
            except ValueError as exc:
                raise CommandRejected(str(exc)) from exc
            if not resolved:
                raise CommandRejected("no usable zones after name resolution")
            payload = encode_start_zones(resolved)
            ctrl = USER_CTRL_CLEAN
            detail = {"zones": resolved}
        else:
            payload = encode_userctrl(ctrl)
            detail = {"variant": ctrl}
        return await self._send("start", ctrl, payload, detail)

    @_audited("pause")
    async def pause(self) -> dict[str, Any]:
        row = self._current_row()
        ctrl = row.pause
        if ctrl is None:
            raise CommandRejected(f"pause not allowed while activity={row.activity}")
        return await self._send("pause", ctrl, encode_userctrl(ctrl), {"variant": ctrl})

    @_audited("resume")
    async def resume(self) -> dict[str, Any]:
        row = self._current_row()
        ctrl = row.start_mowing
        if ctrl not in (USER_CTRL_RESUME, USER_CTRL_RESUME_DOCK):
            raise CommandRejected("no saved task to resume in this state")
        return await self._send("resume", ctrl, encode_userctrl(ctrl), {"variant": ctrl})

    @_audited("dock")
    async def dock(self) -> dict[str, Any]:
        """Safe dock: matrix picks keep-progress (33) vs plain (2) variant."""
        row = self._current_row()
        ctrl = row.dock
        if ctrl is None:
            raise CommandRejected(f"dock not allowed while activity={row.activity}")
        return await self._send("dock", ctrl, encode_userctrl(ctrl), {"variant": ctrl})

    @_audited("dock_cancel")
    async def dock_cancel(self, confirm: bool) -> dict[str, Any]:
        """Destructive: abandon the current task and dock (DOCK=2)."""
        self._require_confirm("dock_cancel", confirm)
        return await self._send(
            "dock_cancel", USER_CTRL_DOCK, encode_userctrl(USER_CTRL_DOCK),
            {"variant": USER_CTRL_DOCK, "task_abandoned": True},
        )

    @_audited("force_reinit")
    async def force_reinit(self, confirm: bool) -> dict[str, Any]:
        """Destructive: firmware re-init (abandons task, re-localizes)."""
        self._require_confirm("force_reinit", confirm)
        return await self._send(
            "force_reinit", USER_CTRL_FORCE_REINIT,
            encode_userctrl(USER_CTRL_FORCE_REINIT), None,
        )

    @_audited("set_rr_config")
    async def set_rr_config(self, **fields: Any) -> dict[str, Any]:
        """Return-to-dock recharge/resume thresholds (no userCtrl; robotConfig
        write). Carry-forward from the cached config so unset fields are not
        reset to firmware defaults."""
        cached = self.state.state.get("robotConfig")
        rr = getattr(cached, "rrConfig", None) if cached is not None else None
        payload = encode_set_rr_config(
            enable_rr=fields.get("enableRr", bool(getattr(rr, "enableRr", True))),
            recharge_bat=int(fields.get("rechargeBat",
                                        getattr(rr, "rechargeBat", 30))),
            resume_bat=int(fields.get("resumeBat", getattr(rr, "resumeBat", 80))),
            period_start_hour=fields.get("periodStartHour"),
            period_start_minute=fields.get("periodStartMinute"),
            period_end_hour=fields.get("periodEndHour"),
            period_end_minute=fields.get("periodEndMinute"),
        )
        return await self._send("set_rr_config", 0, payload, fields)

    @_audited("query")
    async def query(self, what: str) -> dict[str, Any]:
        if what == "map":
            return await self._send("query_map", USER_CTRL_QUERY_MAP,
                                    encode_query_map(), None)
        if what == "schedules":
            return await self._send("query_schedules", USER_CTRL_QUERY_SCHEDULES,
                                    encode_userctrl(USER_CTRL_QUERY_SCHEDULES), None)
        raise CommandRejected(f"unknown query: {what}")

    # ── internals ───────────────────────────────────────────────

    def _current_row(self) -> state_matrix.StateRow:
        if self.state.state.get("robotInfo") is None:
            return state_matrix.DEFAULT_ROW  # no telemetry yet — no buttons
        t = self.state.snapshot()["telemetry"]
        return state_matrix.lookup(
            work_status=t.get("work_status") or 0,
            robot_status=t.get("robot_status") or 0,
            is_recharging=bool(t.get("is_recharging")),
        )

    def _require_confirm(self, action: str, confirm: bool) -> None:
        if not confirm:
            raise CommandRejected(
                f"{action} is destructive and needs confirm=true"
            )

    async def _send(
        self, action: str, user_ctrl: int, payload: bytes,
        detail: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if action == "start" and detail and detail.get("zones"):
            await self._preflight_map()
        expected = EXPECTED_POST_STATES.get(user_ctrl, set())
        pre_status = self._statuses()
        try:
            await self.provider.publish_pbinput(payload)
        except ProviderError as exc:
            await self._audit(action, user_ctrl, detail, f"error:{exc}")
            raise
        if not expected:
            await self._audit(action, user_ctrl, detail, "sent")
            return {"action": action, "outcome": "sent", "confirmed": True}
        confirmed = await self._watchdog(expected, pre_status)
        outcome = "sent" if confirmed else "sent-unconfirmed"
        await self._audit(action, user_ctrl, detail, outcome)
        if not confirmed:
            _LOGGER.warning(
                "command %s (userCtrl=%s) not confirmed within %.1fs — "
                "NOT retrying (non-idempotent)", action, user_ctrl, WATCHDOG_S,
            )
        return {"action": action, "outcome": outcome, "confirmed": confirmed}

    async def _preflight_map(self) -> None:
        """Zone-start needs a loaded catalog; ask for one and wait briefly."""
        catalog = self.state.state.get("zone_catalog")
        if catalog is not None and catalog.zones:
            return
        await self.provider.publish_pbinput(encode_query_map())
        deadline = time.monotonic() + PREFLIGHT_S
        while time.monotonic() < deadline:
            catalog = self.state.state.get("zone_catalog")
            if catalog is not None and catalog.zones:
                return
            await asyncio.sleep(0.1)
        raise CommandRejected("map catalog unavailable; zone-start refused")

    def _statuses(self) -> tuple[int, int]:
        ri = self.state.state.get("robotInfo")
        if ri is None:
            return (0, 0)
        return (getattr(ri, "workStatus", 0), getattr(ri, "robotStatus", 0))

    async def _watchdog(self, expected: set[int], pre: tuple[int, int]) -> bool:
        """Poll state for a transition into the expected post-state set."""
        deadline = time.monotonic() + WATCHDOG_S
        while time.monotonic() < deadline:
            ws, rs = self._statuses()
            if ws in expected or rs in expected:
                return True
            await asyncio.sleep(0.05)
        return self._statuses() != pre

    async def _audit(self, action: str, user_ctrl: int,
                     detail: dict[str, Any] | None, outcome: str) -> None:
        await self.db.audit_command(
            action=action, userctrl=user_ctrl, detail=detail,
            outcome=outcome, simulator=bool(self.provider.simulator),
        )
