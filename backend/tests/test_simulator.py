"""Behavior tests for SimulatedMower — the protocol-faithful fake.

Commands are sent as REAL encoded PbInput bytes (same encoders the
command service uses), so these tests also prove the encoder ⇄ firmware
semantics round-trip. The sim's background loop is left running but
tests drive state with explicit _tick() calls, so nothing depends on
wall-clock timing.
"""
from __future__ import annotations

from app.lymow import lymow_extracted_pb2 as pb
from app.lymow.const import (
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_PAUSE,
    USER_CTRL_PAUSE_DOCK,
    USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_RESUME,
    USER_CTRL_RESUME_DOCK,
    WORK_STATUS_CHARGING,
    WORK_STATUS_CHARGING_FULL,
    WORK_STATUS_DOCKING,
    WORK_STATUS_EMERGENCY_STOP,
    WORK_STATUS_ERROR,
    WORK_STATUS_MOWING,
    WORK_STATUS_PAUSE,
    WORK_STATUS_PAUSE_DOCKING,
    WORK_STATUS_WAITING,
)
from app.lymow.protocol import (
    encode_query_map,
    encode_start_zones,
    encode_upload_robot_config,
    encode_userctrl,
    parse_zone_catalog,
)

from helpers import catalog_messages, decode_outputs, state_messages

DOCK = (4.5, -1.0)


async def cmd(sim, payload: bytes) -> None:
    """Publish a real encoded PbInput, the way the command service does."""
    await sim.publish_pbinput(payload)


class TestStartup:
    async def test_start_broadcasts_state_and_catalog(self, sim):
        mower, outputs = sim
        await mower.start()
        assert outputs, "start() must emit at least one PbOutput"
        states = state_messages(outputs)
        assert states, "no robotInfo state broadcast"
        ri = states[-1].robotInfo
        assert ri.workStatus == WORK_STATUS_CHARGING
        assert ri.battery == 72
        cats = catalog_messages(outputs)
        assert len(cats) == 1
        catalog = parse_zone_catalog(cats[0].btMap)
        assert [z.name for z in catalog.zones] == ["Front Lawn", "Back Lawn"]
        assert [z.hash_id for z in catalog.zones] == ["front1", "back01"]
        assert len(catalog.channels) == 2

    async def test_connection_flag_flips(self, sim):
        mower, _ = sim
        assert not mower.is_connected()
        await mower.start()
        assert mower.is_connected()
        await mower.stop()
        assert not mower.is_connected()


class TestTaskLifecycle:
    async def test_clean_with_explicit_zones_starts_mowing(self, sim):
        mower, _ = sim
        await cmd(mower, encode_start_zones(["back01"]))
        assert mower.work_status == WORK_STATUS_MOWING
        assert mower.task_zones == ["back01"]

    async def test_clean_without_zones_uses_all(self, sim):
        mower, _ = sim
        await cmd(mower, encode_userctrl(USER_CTRL_CLEAN))
        assert mower.work_status == WORK_STATUS_MOWING
        assert mower.task_zones == ["front1", "back01"]

    async def test_mowing_tick_moves_pose_and_grows_area(self, sim):
        mower, _ = sim
        await cmd(mower, encode_start_zones(["front1"]))
        before = mower.get_pose()
        mower._tick()
        after = mower.get_pose()
        assert after != before
        assert mower._clean_area_m2 > 0

    async def test_pause_resume_cycle(self, sim):
        mower, _ = sim
        await cmd(mower, encode_start_zones(["front1"]))
        await cmd(mower, encode_userctrl(USER_CTRL_PAUSE))
        assert mower.work_status == WORK_STATUS_PAUSE
        await cmd(mower, encode_userctrl(USER_CTRL_RESUME))
        assert mower.work_status == WORK_STATUS_MOWING

    async def test_dock_abandons_task_and_emits_cancel_report(self, sim):
        mower, _ = sim
        reports: list[pb.PbCleanReport] = []
        mower.on_clean_report = reports.append
        await cmd(mower, encode_start_zones(["front1"]))
        await cmd(mower, encode_userctrl(USER_CTRL_DOCK))
        assert mower.work_status == WORK_STATUS_DOCKING
        assert mower.task_zones == []
        assert len(reports) == 1 and reports[0].mowEndType == 2

    async def test_dock_approach_reaches_charging(self, sim):
        mower, _ = sim
        mower.pose = (0.0, 0.0, 0.0)
        mower._set_status(WORK_STATUS_DOCKING)
        for _ in range(100):
            mower._tick()
            if mower.work_status == WORK_STATUS_CHARGING:
                break
        assert mower.work_status == WORK_STATUS_CHARGING
        assert mower.is_charging
        assert mower.get_pose() == DOCK

    async def test_recharge_dock_keeps_task(self, sim):
        mower, _ = sim
        await cmd(mower, encode_start_zones(["front1", "back01"]))
        await cmd(mower, encode_userctrl(USER_CTRL_RECHARGE_DOCK))
        assert mower.work_status == WORK_STATUS_DOCKING
        assert mower.is_recharging is True
        assert mower.task_zones == ["front1", "back01"], "progress preserved"

    async def test_pause_and_resume_dock(self, sim):
        mower, _ = sim
        mower._set_status(WORK_STATUS_DOCKING)
        await cmd(mower, encode_userctrl(USER_CTRL_PAUSE_DOCK))
        assert mower.work_status == WORK_STATUS_PAUSE_DOCKING
        await cmd(mower, encode_userctrl(USER_CTRL_RESUME_DOCK))
        assert mower.work_status == WORK_STATUS_DOCKING

    async def test_task_completion_reports_and_auto_docks(self, sim):
        mower, _ = sim
        reports: list[pb.PbCleanReport] = []
        mower.on_clean_report = reports.append
        await cmd(mower, encode_start_zones(["front1"]))
        mower._zone_progress["front1"] = 1.0
        mower._tick()  # completion is detected on the next tick
        assert reports and reports[-1].mowEndType == 1
        assert mower.work_status == WORK_STATUS_DOCKING
        assert mower.is_recharging is True

    async def test_resume_from_recharging_dock(self, sim):
        mower, _ = sim
        await cmd(mower, encode_start_zones(["front1"]))
        await cmd(mower, encode_userctrl(USER_CTRL_RECHARGE_DOCK))
        mower._set_status(WORK_STATUS_CHARGING)
        mower.is_charging = True
        await cmd(mower, encode_userctrl(USER_CTRL_RESUME))
        assert mower.work_status == WORK_STATUS_MOWING
        assert mower.is_recharging is False


class TestErrorSemantics:
    async def test_inject_error_broadcasts_error_codes(self, sim):
        mower, outputs = sim
        mower.inject_error(21)
        assert mower.work_status == WORK_STATUS_ERROR
        assert mower.error_codes == [21]
        states = state_messages(outputs)
        assert list(states[-1].errorCodes) == [21]
        assert states[-1].robotInfo.robotStatus == WORK_STATUS_ERROR

    async def test_pause_clears_error(self, sim):
        """arch.md section 6b: PAUSE doubles as Clear-Error in ERROR state."""
        mower, _ = sim
        mower.inject_error(21)
        await cmd(mower, encode_userctrl(USER_CTRL_PAUSE))
        assert mower.error_codes == []
        assert mower.work_status == WORK_STATUS_WAITING

    async def test_emergency_stop_ignores_commands(self, sim):
        mower, _ = sim
        mower._set_status(WORK_STATUS_EMERGENCY_STOP)
        await cmd(mower, encode_start_zones(["front1"]))
        await cmd(mower, encode_userctrl(USER_CTRL_DOCK))
        assert mower.work_status == WORK_STATUS_EMERGENCY_STOP


class TestQueriesAndConfig:
    async def test_query_map_replies_catalog(self, sim):
        mower, outputs = sim
        await cmd(mower, encode_query_map())
        assert len(catalog_messages(outputs)) == 1

    async def test_upload_robot_config_request_replies(self, sim):
        mower, outputs = sim
        await cmd(mower, encode_upload_robot_config())
        replies = [
            m for m in decode_outputs(outputs) if m.robotConfig.ByteSize()
        ]
        assert replies
        rr = replies[-1].robotConfig.rrConfig
        assert rr.rechargeBat == mower.rr_config["rechargeBat"]

    async def test_rr_config_update_applied(self, sim):
        mower, _ = sim
        inp = pb.PbInput()
        inp.version = 40
        inp.robotConfig.rrConfig.enableRr = True
        inp.robotConfig.rrConfig.rechargeBat = 25
        inp.robotConfig.rrConfig.resumeBat = 90
        await cmd(mower, inp.SerializeToString())
        assert mower.rr_config == {
            "enableRr": True, "rechargeBat": 25, "resumeBat": 90,
        }


class TestBattery:
    async def test_charging_raises_battery(self, sim):
        mower, _ = sim
        mower.battery = 50
        mower.is_charging = True
        mower._set_status(WORK_STATUS_CHARGING)
        mower._tick()
        assert mower.battery > 50

    async def test_mowing_drains_battery(self, sim):
        mower, _ = sim
        mower.battery = 50
        await cmd(mower, encode_start_zones(["front1"]))
        mower._tick()
        assert mower.battery < 50

    async def test_full_charge_sets_charging_full(self, sim):
        mower, _ = sim
        mower.battery = 99.99
        mower.is_charging = True
        mower._set_status(WORK_STATUS_CHARGING)
        mower._tick()
        assert mower.work_status == WORK_STATUS_CHARGING_FULL


class TestStateBroadcastFields:
    async def test_broadcast_carries_dashboard_fields(self, sim):
        mower, outputs = sim
        await mower.start()
        await cmd(mower, encode_start_zones(["front1"]))
        mower._tick()
        mower._broadcast()
        msg = state_messages(outputs)[-1]
        assert msg.pose.x != 0 or msg.pose.y != 0
        assert msg.localizationInfo.numSatellites > 0
        assert msg.rtkDiagnosticL1.rtkStatus == 2
        assert msg.deviceInfo.sn == mower.serial
        assert msg.netDetailInfo.wifiIp == mower.ip
        assert 0.0 <= msg.cleanInfo.cleanPercent <= 1.0
