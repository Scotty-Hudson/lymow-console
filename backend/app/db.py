"""Local SQLite persistence: settings, task history, command audit.

aiosqlite, WAL mode. The DB file (and the session key file) are chmod
0600 — the daemon runs as the user, but we still keep credentials and
audit data private even in a shared /home.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

_LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_tasks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at   REAL NOT NULL,
  ended_at     REAL,
  end_type     INTEGER,               -- 1 completed, 2 cancelled, ...
  duration_s   INTEGER,
  area_m2      REAL,
  used_battery INTEGER,
  zones_json   TEXT,
  simulator    INTEGER NOT NULL       -- 1 = produced by SimulatedMower
);
CREATE TABLE IF NOT EXISTS command_audit (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL NOT NULL,
  action     TEXT NOT NULL,           -- e.g. start_mowing, dock, zone_start
  userctrl   INTEGER NOT NULL,
  detail     TEXT,                    -- zones/config json, no secrets
  outcome    TEXT NOT NULL,           -- sent | rejected:<reason> | error:<msg>
  simulator  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_started ON history_tasks(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON command_audit(ts DESC);
"""

# Settings keys we treat as secret when serializing for logs/API.
SECRET_SETTING_KEYS = {"lymow_password", "session_secret_override"}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_chmod = not self.path.exists()
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.commit()
        if needs_chmod:
            self.path.chmod(0o600)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database.open() not called"
        return self._db

    # ── settings ────────────────────────────────────────────────

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self.db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def delete_setting(self, key: str) -> None:
        await self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await self.db.commit()

    async def all_settings_public(self) -> dict[str, Any]:
        """All settings with secret values masked, for the settings UI."""
        async with self.db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            if row["key"] in SECRET_SETTING_KEYS:
                out[row["key"]] = "***" if row["value"] else ""
            else:
                try:
                    out[row["key"]] = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    out[row["key"]] = row["value"]
        return out

    # ── history ─────────────────────────────────────────────────

    async def record_task(
        self,
        *,
        started_at: float,
        ended_at: float | None,
        end_type: int | None,
        duration_s: int | None,
        area_m2: float | None,
        used_battery: int | None,
        zones: list[str] | None,
        simulator: bool,
    ) -> None:
        await self.db.execute(
            "INSERT INTO history_tasks(started_at, ended_at, end_type, duration_s,"
            " area_m2, used_battery, zones_json, simulator)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                started_at, ended_at, end_type, duration_s, area_m2,
                used_battery,
                json.dumps(zones) if zones is not None else None,
                int(simulator),
            ),
        )
        await self.db.commit()

    async def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM history_tasks ORDER BY started_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for row in rows:
            d = dict(row)
            if d.get("zones_json"):
                d["zones"] = json.loads(d.pop("zones_json"))
            d["simulator"] = bool(d["simulator"])
            out.append(d)
        return out

    # ── command audit ───────────────────────────────────────────

    async def audit_command(
        self,
        *,
        action: str,
        userctrl: int,
        detail: dict[str, Any] | None,
        outcome: str,
        simulator: bool,
    ) -> None:
        # Never log credentials or tokens; detail is command parameters only.
        _LOGGER.info("command audit: %s userCtrl=%s outcome=%s sim=%s",
                     action, userctrl, outcome, simulator)
        await self.db.execute(
            "INSERT INTO command_audit(ts, action, userctrl, detail, outcome,"
            " simulator) VALUES(?,?,?,?,?,?)",
            (
                time.time(), action, userctrl,
                json.dumps(detail) if detail else None,
                outcome, int(simulator),
            ),
        )
        await self.db.commit()

    async def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM command_audit ORDER BY ts DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for row in rows:
            d = dict(row)
            if d.get("detail"):
                d["detail"] = json.loads(d["detail"])
            d["simulator"] = bool(d["simulator"])
            out.append(d)
        return out
