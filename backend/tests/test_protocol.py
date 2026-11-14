"""Protocol-layer tests against the REAL captured fixtures.

The .bin fixtures on disk are raw PbOutput protobuf (the MQTT JSON
envelope was stripped when they were captured); decode_pboutput_envelope
handles both shapes.

Fixture sources and semantics: derived from Lymow-One-MQTT's tests
(MIT, (c) 2026 Aaron Gibbs); fixtures re-verified in this project.
"""
from __future__ import annotations

import base64

import pytest

from app.lymow import lymow_extracted_pb2 as pb
from app.lymow.const import USER_CTRL_CLEAN, USER_CTRL_QUERY_MAP
from app.lymow.protocol import (
    decode_pboutput,
    decode_pboutput_envelope,
    decode_schedules,
    encode_query_map,
    encode_start_zones,
    encode_userctrl,
    extract_error_codes,
    parse_zone_catalog,
    populated_fields,
    unwrap_envelope,
    wrap_envelope,
)

from helpers import FIXTURE_DIR, load_fixture_bytes

ALL_FIXTURES = sorted(p.name for p in FIXTURE_DIR.glob("*.bin"))


class TestEnvelope:
    def test_wrap_unwrap_roundtrip(self):
        raw = pb.PbInput(version=40, userCtrl=19).SerializeToString()
        env = wrap_envelope(raw)
        assert env.startswith("{") and "message" in env
        assert unwrap_envelope(env.encode("utf-8")) == raw

    def test_decode_handles_raw_and_envelope(self):
        raw = load_fixture_bytes("state_broadcast.bin")
        as_raw = decode_pboutput(raw)
        env = wrap_envelope(raw)
        as_env = decode_pboutput_envelope(env.encode("utf-8"))
        assert as_raw.robotInfo.battery == as_env.robotInfo.battery

    def test_garbage_raises(self):
        with pytest.raises(Exception):
            decode_pboutput_envelope(b"\xff\xff\xff\xff\xff\xff garbage")


class TestRealFixtures:
    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_fixture_decodes_as_pboutput(self, name):
        msg = decode_pboutput_envelope(load_fixture_bytes(name))
        assert populated_fields(msg), f"{name} decoded to an empty message"

    def test_query_map_response_has_catalog(self):
        out = decode_pboutput_envelope(load_fixture_bytes("query_map_response.bin"))
        assert out.btMap.ByteSize() >= 200, "catalog blob suspiciously small"

    def test_query_map_catalog_has_real_zones(self):
        out = decode_pboutput_envelope(load_fixture_bytes("query_map_response.bin"))
        catalog = parse_zone_catalog(out.btMap)
        names = {z.name for z in catalog.zones}
        hashes = {z.hash_id for z in catalog.zones}
        assert len(catalog.zones) == 13
        assert "Pool" in names and "Front yard" in names
        assert "8FyN5qA4" in hashes

    def test_schedule_response_decodes_one_schedule(self):
        out = decode_pboutput_envelope(load_fixture_bytes("schedule_response.bin"))
        assert out.schedule.ByteSize() > 0
        scheds = decode_schedules(out.schedule)
        assert len(scheds) == 1

    def test_unknown_field_roundtrip_is_stable(self):
        """The btMap blob contains fields missing from the .proto schema;
        MergeFromString/SerializeToString must preserve them byte-stably.
        The simulator's catalog trick (and any future re-broadcast) relies
        on this property."""
        out = decode_pboutput_envelope(load_fixture_bytes("query_map_response.bin"))
        src = out.btMap.SerializeToString()
        bt = pb.PbBtMap()
        bt.MergeFromString(src)
        once = bt.SerializeToString()
        bt2 = pb.PbBtMap()
        bt2.ParseFromString(once)
        assert bt2.SerializeToString() == once == src


class TestEncoders:
    def test_encode_userctrl(self):
        raw = encode_userctrl(USER_CTRL_QUERY_MAP)
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        assert msg.userCtrl == USER_CTRL_QUERY_MAP
        assert msg.version > 0, "PB_VERSION must be stamped on every command"

    def test_encode_query_map_matches_userctrl_encoder(self):
        a = pb.PbInput()
        a.ParseFromString(encode_query_map())
        b = pb.PbInput()
        b.ParseFromString(encode_userctrl(USER_CTRL_QUERY_MAP))
        assert a.userCtrl == b.userCtrl == USER_CTRL_QUERY_MAP

    def test_encode_start_zones_carries_hash_ids(self):
        raw = encode_start_zones(["8FyN5qA4", "front1"])
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        assert msg.userCtrl == USER_CTRL_CLEAN
        got = [z.basicInfo.hashId for z in msg.map.goZones]
        assert got == ["8FyN5qA4", "front1"]

    def test_start_zones_survive_wire_envelope(self):
        env = wrap_envelope(encode_start_zones(["back01"]))
        msg = pb.PbInput()
        msg.ParseFromString(unwrap_envelope(env.encode("utf-8")))
        assert [z.basicInfo.hashId for z in msg.map.goZones] == ["back01"]


class TestErrorCodes:
    def test_extract_error_codes(self):
        out = pb.PbOutput()
        out.errorCodes.extend([21, 47])
        assert extract_error_codes(out) == [21, 47]

    def test_extract_empty_when_absent(self):
        out = pb.PbOutput()
        assert extract_error_codes(out) == []


class TestCatalogReembedding:
    def test_real_catalog_embedded_in_pbinput_survives(self):
        """Simulator/dev flow: take a real catalog, embed it in a PbInput
        the way a queryAck reply is shaped, re-decode, zones must match."""
        real = decode_pboutput_envelope(load_fixture_bytes("query_map_response.bin"))
        ack = pb.PbInput()
        ack.version = 40
        ack.userCtrl = USER_CTRL_QUERY_MAP
        ack.btMap.CopyFrom(real.btMap)
        env = base64.b64encode(ack.SerializeToString()).decode()
        probe = pb.PbOutput()
        probe.ParseFromString(base64.b64decode(env))
        catalog = parse_zone_catalog(probe.btMap)
        assert [z.name for z in catalog.zones] == [
            z.name for z in parse_zone_catalog(real.btMap).zones
        ]
