"""SimulatedMower — a protocol-faithful fake mower for development and tests.

Design rules:
  - Uses the real protobuf schema (lymow_extracted_pb2) to serialize
    PbOutput payloads, then feeds them through the SAME JSON envelope +
    merge/derivation pipeline the live mower goes through. The web layer
    cannot tell simulator state from live state except via the
    `simulator` flag.
  - Never opens a socket: no aiohttp, no paho, no DNS. Enforced by tests
    (tests/test_simulator_isolation.py) which blow up the network stack
    inside the provider and assert every command still works.
  - Commands drive an internal state machine whose transitions mirror
    EXPECTED_POST_STATES from Lymow-One-MQTT's userctrl watchdog table.
"""
from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from typing import Any

from app.lymow import lymow_extracted_pb2 as pb
from app.lymow.const import (
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_FORCE_REINIT,
    USER_CTRL_PAUSE,
    USER_CTRL_PAUSE_DOCK,
    USER_CTRL_QUERY_MAP,
    USER_CTRL_QUERY_SCHEDULES,
    USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_RESUME,
    USER_CTRL_RESUME_DOCK,
    WORK_STATUS_CHARGING,
    WORK_STATUS_CHARGING_FULL,
    WORK_STATUS_DOCKING,
    WORK_STATUS_EMERGENCY_STOP,
    WORK_STATUS_ERROR,
    WORK_STATUS_ESCAPING,
    WORK_STATUS_MOWING,
    WORK_STATUS_PAUSE,
    WORK_STATUS_PAUSE_DOCKING,
    WORK_STATUS_RESUME,
    WORK_STATUS_WAITING,
    WORK_STATUS_ZONE_PARTITION,
)
from app.lymow.protocol import wrap_envelope
from app.services.provider import MowerProvider

_LOGGER = logging.getLogger(__name__)

TICK_SECONDS = 1.0
BROADCAST_FAST = 2.0      # during an active task (mirrors live cadence)
BROADCAST_SLOW = 30.0     # idle / charging cadence

MOW_SPEED = 0.35          # m/s
DOCK_SPEED = 0.5          # m/s
BATTERY_DRAIN_PER_MIN = 0.8
CHARGE_RATE_PER_MIN = 2.0


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _len_field(field_no: int, payload: bytes) -> bytes:
    return bytes([field_no << 3 | 2]) + _varint(len(payload)) + payload


class SimulatedMower(MowerProvider):
    """Stateful fake mower: subscribes to PbInput semantics, emits PbOutput."""

    simulator = True

    def __init__(
        self,
        device_id: str = "sim-oneplus",
        *,
        battery: int = 72,
        on_dock: bool = True,
        fault: str | None = None,  # "error" | "float" | None for dev scenarios
    ) -> None:
        super().__init__(device_id)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._running = False

        # ---- world model ----
        self.work_status = WORK_STATUS_CHARGING if on_dock else WORK_STATUS_WAITING
        self.robot_status = self.work_status
        self.battery = battery
        self.is_charging = on_dock
        self.is_recharging = False
        self.error_codes: list[int] = []
        self.warning_codes: list[int] = []
        self.pose = (4.5, -1.0, 0.0)  # dock pose
        self.fault = fault
        self.rr_config = {"enableRr": True, "rechargeBat": 30, "resumeBat": 80}
        self.last_clean_report: dict[str, Any] | None = None

        # task model
        self.task_zones: list[str] = []      # hash ids in mow order
        self._zone_progress: dict[str, float] = {}  # hash -> fraction 0..1
        self._clean_start_mono: float | None = None
        self._clean_time_s = 0
        self._clean_area_m2 = 0.0
        self._sweep = None  # current sweep cursor

        # zone catalog (local ENU meters; regex-safe hash ids)
        self.zones = [
            {"hashId": "front1", "name": "Front Lawn", "poly": [(0, 0), (9, 0), (9, 6), (0, 6)]},
            {"hashId": "back01", "name": "Back Lawn", "poly": [(0, 8), (11, 8), (11, 14), (0, 14)]},
        ]
        self.channels = [
            {"hashId": "dockch01", "zone1": "charging_area", "zone2": "front1",
             "isDockingChannel": True, "poly": [(4.2, -0.8), (4.8, -0.8), (4.8, 0.2), (4.2, 0.2)]},
            {"hashId": "channe1", "zone1": "front1", "zone2": "back01",
             "isDockingChannel": False, "poly": [(4.2, 5.8), (4.8, 5.8), (4.8, 8.2), (4.2, 8.2)]},
        ]
        self.dock = (4.5, -1.0)
        self.enu_base = (52.1534, 4.4832)  # demo lat/lon of RTK base
        self.serial = "LMO1P-SIM-0042"
        self.ip = "192.168.50.42"
        self.fw = "2.1.50.1-sim"
        self.publish_count = 0  # telemetry for tests/metrics

    # ── provider lifecycle ──────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._last_broadcast = 0.0
        self._task = asyncio.create_task(self._run_loop())
        # Initial state broadcast + catalog so the UI fills immediately.
        self._broadcast()
        self._send_catalog()
        self._send_schedules()
        self.on_connection_changed(True)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.on_connection_changed(False)

    def is_connected(self) -> bool:
        return self._running

    async def publish_pbinput(self, raw_pbinput: bytes) -> None:
        """Parse a real PbInput the way firmware would and apply semantics."""
        msg = pb.PbInput()
        msg.ParseFromString(raw_pbinput)
        async with self._lock:
            self.publish_count += 1
            if msg.HasField("btMap") and msg.btMap.queryMap:
                self._send_catalog()
                return
            dbg_upload = msg.HasField("debugSetting") and msg.debugSetting.uploadRobotConfig
            if msg.HasField("robotConfig"):
                rc = msg.robotConfig
                if rc.HasField("rrConfig"):
                    rr = rc.rrConfig
                    self.rr_config = {
                        "enableRr": rr.enableRr,
                        "rechargeBat": rr.rechargeBat,
                        "resumeBat": rr.resumeBat,
                    }
            if dbg_upload:
                self._send_robot_config()
                return
            ctrl = msg.userCtrl if msg.HasField("userCtrl") else 0
            if ctrl:
                self._apply_user_ctrl(ctrl, msg)

    # ── command semantics (mirror EXPECTED_POST_STATES) ─────────

    def _apply_user_ctrl(self, ctrl: int, msg: pb.PbInput) -> None:
        ws, rs = self.work_status, self.robot_status
        if rs == WORK_STATUS_ERROR and ctrl == USER_CTRL_PAUSE:
            # Pause doubles as Clear-Error (arch §6b).
            self.error_codes = []
            self._set_status(WORK_STATUS_WAITING)
            return
        if rs == WORK_STATUS_EMERGENCY_STOP:
            return  # physical e-stop: firmware ignores cloud commands

        if ctrl == USER_CTRL_CLEAN:
            zones = [z.basicInfo.hashId for z in msg.map.goZones] if msg.HasField("map") else []
            self._start_task(zones or [z["hashId"] for z in self.zones])
        elif ctrl == USER_CTRL_PAUSE:
            if ws in (WORK_STATUS_MOWING, WORK_STATUS_RESUME, 9, 14):
                self._set_status(WORK_STATUS_PAUSE)
        elif ctrl == USER_CTRL_RESUME:
            if ws == WORK_STATUS_PAUSE:
                self._set_status(WORK_STATUS_RESUME)
                self._set_status(WORK_STATUS_MOWING)
            elif ws in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL) and self.is_recharging:
                self.is_recharging = False
                self.is_charging = False
                self._set_status(WORK_STATUS_RESUME)
                self._set_status(WORK_STATUS_MOWING)
        elif ctrl == USER_CTRL_DOCK:
            self._abandon_task()
            self._set_status(WORK_STATUS_DOCKING)
        elif ctrl == USER_CTRL_PAUSE_DOCK:
            if ws == WORK_STATUS_DOCKING:
                self._set_status(WORK_STATUS_PAUSE_DOCKING)
        elif ctrl == USER_CTRL_RESUME_DOCK:
            if ws == WORK_STATUS_PAUSE_DOCKING:
                self._set_status(WORK_STATUS_DOCKING)
        elif ctrl == USER_CTRL_RECHARGE_DOCK:
            if ws in (WORK_STATUS_MOWING, WORK_STATUS_RESUME):
                self.is_recharging = True
                self._set_status(WORK_STATUS_DOCKING)
        elif ctrl == USER_CTRL_FORCE_REINIT:
            self._abandon_task()
            self._set_status(WORK_STATUS_WAITING)
        elif ctrl == USER_CTRL_QUERY_MAP:
            self._send_catalog()
        elif ctrl == USER_CTRL_QUERY_SCHEDULES:
            self._send_schedules()
        # The matrix expects these post-states; the sim applies them
        # synchronously and the next broadcast confirms them.

    def _set_status(self, s: int) -> None:
        self.work_status = s
        self.robot_status = s

    def _start_task(self, zone_hashes: list[str]) -> None:
        self.task_zones = [h for h in zone_hashes if any(z["hashId"] == h for z in self.zones)]
        self._zone_progress = {h: 0.0 for h in self.task_zones}
        self._clean_start_mono = time.monotonic()
        self._clean_time_s = 0
        self._clean_area_m2 = 0.0
        self._sweep = None
        self.is_recharging = False
        self.is_charging = False
        self._set_status(WORK_STATUS_MOWING)
        self._send_catalog()  # mowOrder becomes visible in the catalog

    def _abandon_task(self) -> None:
        if self.task_zones:
            self._emit_clean_report(end_type=2)  # 2 = user-cancelled (observed value space)
        self.task_zones = []
        self._zone_progress = {}
        self._sweep = None
        self._send_catalog()  # clear mowOrder

    def _emit_clean_report(self, end_type: int) -> None:
        rep = pb.PbCleanReport()
        rep.cleanStartTime = int(time.time() - self._clean_time_s)
        rep.mowEndType = end_type
        rep.usedBattery = int(BATTERY_DRAIN_PER_MIN * self._clean_time_s / 60)
        rep.cleanInfo.cleanTime = int(self._clean_time_s)
        rep.cleanInfo.cleanArea = self._clean_area_m2
        self.last_clean_report = rep
        self.on_clean_report(rep)

    # hook installed by MowerStateService
    def on_clean_report(self, rep: pb.PbCleanReport) -> None:  # pragma: no cover
        pass

    # ── simulation loop ─────────────────────────────────────────

    async def _run_loop(self) -> None:
        self._last_broadcast = time.monotonic()
        while self._running:
            try:
                await asyncio.sleep(TICK_SECONDS)
                async with self._lock:
                    self._tick()
                    now = time.monotonic()
                    interval = BROADCAST_FAST if self._status_active() else BROADCAST_SLOW
                    if now - self._last_broadcast >= interval:
                        self._broadcast()
                        self._last_broadcast = now
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("simulator tick failed")

    def _status_active(self) -> bool:
        return self.work_status in (
            WORK_STATUS_MOWING, WORK_STATUS_RESUME, WORK_STATUS_DOCKING,
            WORK_STATUS_PAUSE, WORK_STATUS_PAUSE_DOCKING, WORK_STATUS_ERROR,
        )

    def _tick(self) -> None:
        self.publish_count += 0
        ws = self.work_status
        # battery
        if self.is_charging:
            self.battery = min(100, self.battery + CHARGE_RATE_PER_MIN / 60)
            if self.battery >= 100:
                self._set_status(WORK_STATUS_CHARGING_FULL)
                self.is_charging = self.work_status in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL)
        elif ws in (WORK_STATUS_MOWING, WORK_STATUS_RESUME, WORK_STATUS_DOCKING):
            self.battery = max(0, self.battery - BATTERY_DRAIN_PER_MIN / 60)
        # pose/motion
        if ws in (WORK_STATUS_MOWING, WORK_STATUS_RESUME, WORK_STATUS_ZONE_PARTITION,
                  WORK_STATUS_ESCAPING):
            self._advance_mowing()
            self._clean_time_s += TICK_SECONDS
        elif ws == WORK_STATUS_DOCKING:
            prev = self.work_status
            self._advance_docking()
            if self.work_status != prev and self.task_zones:
                self._send_catalog()
        # scripted fault for dev UI testing: "error" after a while
        # (only if fault requested at construction; never spontaneously)

    def _advance_mowing(self) -> None:
        if not self.task_zones:
            return
        zone = next((z for z in self.zones if z["hashId"] == self.task_zones[0]), None)
        if zone is None:
            return
        # zone transition
        if self._zone_progress.get(self.task_zones[0], 0) >= 1.0:
            done = self.task_zones.pop(0)
            self._zone_progress.pop(done, None)
            if not self.task_zones:
                self._emit_clean_report(end_type=1)  # 1 = completed
                self.is_recharging = True
                self._set_status(WORK_STATUS_DOCKING)
                self._send_catalog()  # mowOrder cleared
                return
            self._sweep = None
            zone = next((z for z in self.zones if z["hashId"] == self.task_zones[0]), None)
            if zone is None:
                return
        xs = [p[0] for p in zone["poly"]]
        ys = [p[1] for p in zone["poly"]]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if self._sweep is None:
            self._sweep = {"row": y0 + 0.3, "dir": 1, "x": x0}
        sw = self._sweep
        step = MOW_SPEED * TICK_SECONDS
        area_per_m = 0.6  # mower swath width (m)
        if sw["dir"] == 1:
            sw["x"] += step
            if sw["x"] >= x1:
                sw["x"] = x1
                sw["dir"] = -1
                sw["row"] += 0.6
            self.pose = (sw["x"], sw["row"], 0.0)
        else:
            sw["x"] -= step
            if sw["x"] <= x0:
                sw["x"] = x0
                sw["dir"] = 1
                sw["row"] += 0.6
            self.pose = (sw["x"], sw["row"], math.pi)
        self._clean_area_m2 += step * area_per_m
        total = (x1 - x0) * (y1 - y0)
        self._zone_progress[self.task_zones[0]] = min(1.0, self._clean_area_m2 / max(1e-6, total))
        # progress is per-zone; keep simple global estimate below in broadcast

    def _advance_docking(self) -> None:
        px, py, th = self.pose
        dx, dy = self.dock[0] - px, self.dock[1] - py
        dist = math.hypot(dx, dy)
        step = DOCK_SPEED * TICK_SECONDS
        if dist <= step:
            self.pose = (self.dock[0], self.dock[1], 0.0)
            self.is_charging = True
            self._set_status(WORK_STATUS_CHARGING)
        else:
            self.pose = (px + dx / dist * step, py + dy / dist * step, math.atan2(dy, dx))

    # ── PbOutput serialization (real schema) ────────────────────

    def _build_state_output(self) -> pb.PbOutput:
        out = pb.PbOutput()
        ri = out.robotInfo
        ri.robotStatus = self.robot_status
        ri.workStatus = self.work_status
        ri.battery = int(self.battery)
        ri.isCharging = self.is_charging
        ri.isRecharging = self.is_recharging
        ri.wifiSignalQuality = 4
        ri.lteSignalQuality = 2
        ri.wifiWorking = True
        ri.lteWorking = True
        out.pose.x = self.pose[0]
        out.pose.y = self.pose[1]
        out.pose.theta = self.pose[2]
        ci = out.cleanInfo
        if self.task_zones:
            total_task_area = sum(
                _poly_area(z["poly"]) for z in self.zones if z["hashId"] in self.task_zones
            )
            ci.cleanTime = int(self._clean_time_s)
            ci.cleanArea = self._clean_area_m2
            ci.cleanPercent = min(1.0, self._clean_area_m2 / max(1e-6, total_task_area))
            remaining_area = max(0.0, total_task_area - self._clean_area_m2)
            ci.remainCleanTime = int(remaining_area / (0.6 * MOW_SPEED))
        ci.mapArea = sum(_poly_area(z["poly"]) for z in self.zones)
        li = out.localizationInfo
        degraded = self.fault == "float"
        li.horizontalAccuracy = 1.8 if degraded else 0.03
        li.locNodeStatus = 3
        li.numSatellites = 9 if degraded else 14
        l1 = out.rtkDiagnosticL1
        l1.rtkStatus = 1 if degraded else 2
        l1.precision = 1.8 if degraded else 0.02
        l1.satelliteCount = 9 if degraded else 14
        l1.baseStationStatus = 1
        nd = out.netDetailInfo
        nd.currentNet = 1
        nd.wifiName = "HomeLAN"
        nd.wifiIp = self.ip
        nd.wifiSignal = -58
        di = out.deviceInfo
        di.fwVersion = self.fw
        di.mcuVersion = "1.8.4-sim"
        di.ipAddress = self.ip
        di.sn = self.serial
        out.chargingStationLoc.x = self.dock[0]
        out.chargingStationLoc.y = self.dock[1]
        out.chargingStationLoc.theta = 0.0
        if self.error_codes:
            out.errorCodes.extend(self.error_codes)
        if self.warning_codes:
            out.warningCodes.extend(self.warning_codes)
        return out

    def _broadcast(self) -> None:
        out = self._build_state_output()
        self.on_pboutput(wrap_envelope(out.SerializeToString()).encode("utf-8"))

    def _send_robot_config(self) -> None:
        out = pb.PbOutput()
        rr = out.robotConfig.rrConfig
        rr.enableRr = self.rr_config.get("enableRr", False)
        rr.rechargeBat = self.rr_config.get("rechargeBat", 30)
        rr.resumeBat = self.rr_config.get("resumeBat", 80)
        self.on_pboutput(wrap_envelope(out.SerializeToString()).encode("utf-8"))

    def _send_catalog(self) -> None:
        """btMap.queryAck[3] = PbMap, matching parse_zone_catalog's walk."""
        m = pb.PbMap()
        for i, z in enumerate(self.zones):
            zone = m.goZones.add()
            bi = zone.basicInfo
            bi.type = 1
            bi.name = z["name"]
            bi.hashId = z["hashId"]
            bi.isEnabled = True
            bi.mowOrder = (
                self.task_zones.index(z["hashId"]) + 1 if z["hashId"] in self.task_zones else 0
            )
            for x, y in z["poly"]:
                p = bi.polygon.points.add()
                p.x = x
                p.y = y
            zc = zone.zoneConfig
            zc.cutHeight = 45
            zc.moveSpeed = 0.35
            zc.cutSpeed = 4
        for ch in self.channels:
            c = m.channels.add()
            c.hashId = ch["hashId"]
            c.zone1 = ch["zone1"]
            c.zone2 = ch["zone2"]
            c.isValid = True
            c.isDockingChannel = ch["isDockingChannel"]
            for x, y in ch["poly"]:
                p = c.polygon.points.add()
                p.x = x
                p.y = y
        m.chargingStationLoc.x = self.dock[0]
        m.chargingStationLoc.y = self.dock[1]
        m.chargingStationLoc.theta = 0.0
        m.enuBasePoint.latitude = self.enu_base[0]
        m.enuBasePoint.longitude = self.enu_base[1]
        m.enuBasePoint.altitude = 0.0
        m.runTimeConfig.cutHeight = 45
        m.runTimeConfig.moveSpeed = 0.35
        m.runTimeConfig.cutSpeed = 4
        map_bytes = m.SerializeToString()
        # queryAck is an empty placeholder message upstream; carry the PbMap
        # at its field 3 via unknown-field round-trip, exactly like the
        # firmware's raw wire shape.
        qa_bytes = _len_field(3, map_bytes)
        bmap_raw = _len_field(1, _varint(1)) + _len_field(2, qa_bytes)
        bmap = pb.PbBtMap()
        bmap.MergeFromString(bmap_raw)
        out = pb.PbOutput()
        out.btMap.CopyFrom(bmap)
        self.on_pboutput(wrap_envelope(out.SerializeToString()).encode("utf-8"))

    def _send_schedules(self) -> None:
        # PbSchedule is a placeholder upstream; schedules are display-only
        # and not exercised by the console v1. Send nothing.
        return

    # ── test/dev hooks ──────────────────────────────────────────

    def inject_error(self, code: int = 21) -> None:
        """Dev hook: force an error state the way firmware would broadcast it."""
        self.error_codes = [code]
        self._set_status(WORK_STATUS_ERROR)
        self._broadcast()

    def get_pose(self) -> tuple[float, float]:
        return (self.pose[0], self.pose[1])


def _poly_area(poly: list[tuple[float, float]]) -> float:
    n = len(poly)
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5
