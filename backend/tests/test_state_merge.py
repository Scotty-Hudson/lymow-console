"""Tests for state.py merge/derivation rules used by the dashboard."""
from __future__ import annotations

from app.lymow import lymow_extracted_pb2 as pb
from app.lymow.state import merge_pboutput


def _msg() -> pb.PbOutput:
    return pb.PbOutput()


class TestMergeRules:
    def test_robot_info_is_snapshot_replaced(self):
        state: dict = {}
        m1 = _msg()
        m1.robotInfo.battery = 80
        merge_pboutput(state, m1)
        m2 = _msg()
        m2.robotInfo.battery = 79
        merge_pboutput(state, m2)
        assert state["robotInfo"].battery == 79

    def test_clean_info_is_sticky_merged(self):
        """A broadcast carrying only cleanArea must not wipe mapArea."""
        state: dict = {}
        m1 = _msg()
        m1.cleanInfo.mapArea = 512.0
        merge_pboutput(state, m1)
        m2 = _msg()
        m2.cleanInfo.cleanArea = 40.0
        merge_pboutput(state, m2)
        assert state["cleanInfo"].mapArea == 512.0
        assert state["cleanInfo"].cleanArea == 40.0

    def test_first_sticky_merge_does_not_alias_inbound(self):
        state: dict = {}
        m1 = _msg()
        m1.deviceInfo.sn = "SN-1"
        merge_pboutput(state, m1)
        m1.deviceInfo.sn = "MUTATED"
        assert state["deviceInfo"].sn == "SN-1"

    def test_error_codes_replace_when_present(self):
        state: dict = {"errorCodes": [21]}
        m = _msg()
        m.errorCodes.extend([47, 48])
        merge_pboutput(state, m)
        assert state["errorCodes"] == [47, 48]

    def test_error_codes_untouched_when_absent(self):
        state: dict = {"errorCodes": [21]}
        merge_pboutput(state, _msg())
        assert state["errorCodes"] == [21]

    def test_bt_map_replaces_not_merges(self):
        """Catalog replies must not accumulate repeated fields."""
        state: dict = {}
        m1 = _msg()
        m1.btMap.MergeFromString(b"\x08\x01")
        merge_pboutput(state, m1)
        m2 = _msg()
        m2.btMap.MergeFromString(b"\x08\x02")
        merge_pboutput(state, m2)
        assert state["btMap"].SerializeToString() == b"\x08\x02"

    def test_rr_config_replaces_on_new_robot_config(self):
        """enableRr=false on the wire is indistinguishable from absent in
        proto3; rrConfig must be wholesale-replaced so the cached value
        can never stay stale-true (arch.md section 6e)."""
        state: dict = {}
        m1 = _msg()
        m1.robotConfig.rrConfig.enableRr = True
        m1.robotConfig.rrConfig.rechargeBat = 30
        merge_pboutput(state, m1)
        m2 = _msg()
        m2.robotConfig.rrConfig.enableRr = False
        m2.robotConfig.rrConfig.rechargeBat = 40
        merge_pboutput(state, m2)
        assert state["robotConfig"].rrConfig.enableRr is False
        assert state["robotConfig"].rrConfig.rechargeBat == 40
