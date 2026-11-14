"""Tests for app/db.py — settings, history, command audit, file perms."""
from __future__ import annotations

import stat
import time

import pytest

from app.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "data" / "console.db")
    await database.open()
    yield database
    await database.close()


class TestSettings:
    async def test_set_get_delete(self, db):
        assert await db.get_setting("missing") is None
        assert await db.get_setting("missing", "fallback") == "fallback"
        await db.set_setting("device_thing_name", "thing-1")
        assert await db.get_setting("device_thing_name") == "thing-1"
        await db.set_setting("device_thing_name", "thing-2")  # upsert
        assert await db.get_setting("device_thing_name") == "thing-2"
        await db.delete_setting("device_thing_name")
        assert await db.get_setting("device_thing_name") is None

    async def test_secret_values_are_masked_for_public_view(self, db):
        await db.set_setting("lymow_username", "user@example.com")
        await db.set_setting("lymow_password", "hunter2")
        public = await db.all_settings_public()
        assert public["lymow_username"] == "user@example.com"
        assert public["lymow_password"] == "***"
        # the raw value must still work server-side
        assert await db.get_setting("lymow_password") == "hunter2"

    async def test_json_values_roundtrip_in_public_view(self, db):
        await db.set_setting("cmd_config", '{"cutHeight": 45}')
        public = await db.all_settings_public()
        assert public["cmd_config"] == {"cutHeight": 45}


class TestHistory:
    async def test_record_and_list(self, db):
        now = time.time()
        await db.record_task(
            started_at=now - 3600, ended_at=now, end_type=1, duration_s=3600,
            area_m2=512.5, used_battery=25, zones=["front1", "back01"],
            simulator=True,
        )
        rows = await db.list_tasks()
        assert len(rows) == 1
        r = rows[0]
        assert r["end_type"] == 1 and r["area_m2"] == 512.5
        assert r["zones"] == ["front1", "back01"]
        assert r["simulator"] is True

    async def test_list_is_newest_first_and_limited(self, db):
        base = time.time()
        for i in range(5):
            await db.record_task(
                started_at=base + i, ended_at=None, end_type=None,
                duration_s=None, area_m2=None, used_battery=None,
                zones=None, simulator=False,
            )
        rows = await db.list_tasks(limit=3)
        assert len(rows) == 3
        assert rows[0]["started_at"] == pytest.approx(base + 4)


class TestAudit:
    async def test_audit_records_without_detail_leaks(self, db):
        await db.audit_command(
            action="zone_start", userctrl=1,
            detail={"zones": ["front1"]}, outcome="sent", simulator=True,
        )
        await db.audit_command(
            action="dock", userctrl=33, detail=None,
            outcome="rejected:state", simulator=False,
        )
        rows = await db.list_audit()
        assert rows[0]["action"] == "dock"  # newest first
        assert rows[0]["outcome"] == "rejected:state"
        assert rows[1]["detail"] == {"zones": ["front1"]}
        assert rows[1]["simulator"] is True


class TestFileSecurity:
    async def test_db_file_is_owner_only(self, tmp_path):
        database = Database(tmp_path / "p" / "console.db")
        await database.open()
        mode = stat.S_IMODE((tmp_path / "p" / "console.db").stat().st_mode)
        await database.close()
        assert mode == 0o600
