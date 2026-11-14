"""HistoryService — persists completed/cancelled mow tasks to SQLite."""
from __future__ import annotations

import logging
import time
from typing import Any

from app.db import Database
from app.services.provider import spawn
from app.services.state_service import MowerStateService

_LOGGER = logging.getLogger(__name__)


class HistoryService:
    def __init__(self, provider_simulator: bool, db: Database,
                 state: MowerStateService) -> None:
        self.simulator = provider_simulator
        self.db = db
        self.state = state

    def attach(self) -> None:
        self.state.on_clean_report = self._record

    def _record(self, report: dict[str, Any]) -> None:
        try:
            started = report.get("start_time") or time.time()
            duration = report.get("duration_s")
            ended = (started + duration) if duration is not None else None
            zones = [z["name"] for z in self.state.snapshot()["task_zones"]]
            spawn(self.db.record_task(
                started_at=float(started),
                ended_at=float(ended) if ended is not None else None,
                end_type=report.get("end_type"),
                duration_s=int(duration) if duration is not None else None,
                area_m2=report.get("area_m2"),
                used_battery=report.get("used_battery"),
                zones=zones or None,
                simulator=self.simulator,
            ))
        except Exception:
            _LOGGER.exception("failed to schedule history record")

    async def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.db.list_tasks(limit)
