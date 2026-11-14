"""RealMowerProvider internal rules, tested without any network:
the lazy QUERY_MAP refire rule and the reconnect backoff loop."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from app.lymow.const import (
    USER_CTRL_QUERY_MAP,
    WORK_STATUS_MOWING,
    WORK_STATUS_WAITING,
)
from app.services.real_provider import RealMowerProvider


def make_provider() -> RealMowerProvider:
    p = RealMowerProvider(region="us-east-2", device_id="device_test")
    p.fired: list[tuple[int, bool]] = []

    async def fake_pub(ctrl: int, with_query_map_flag: bool = False) -> None:
        p.fired.append((ctrl, with_query_map_flag))

    p._publish_userctrl = fake_pub  # noqa: SLF001
    return p


class TestRefireRule:
    async def _drain(self) -> None:
        await asyncio.sleep(0)  # let spawn()'ed publish tasks run

    async def test_transition_and_report_fire_steady_state_does_not(self):
        p = make_provider()
        complete = SimpleNamespace(zones=[object()], runtime_config=object())
        p._state_ref = {  # noqa: SLF001
            "robotInfo": SimpleNamespace(workStatus=WORK_STATUS_MOWING),
            "zone_catalog": complete,
        }
        p._prev_work_status = WORK_STATUS_WAITING  # noqa: SLF001
        p.maybe_refire_query_map()
        await self._drain()
        assert p.fired == [(USER_CTRL_QUERY_MAP, True)], "WAITING->MOWING must refire"

        p.maybe_refire_query_map()  # steady MOWING, complete catalog
        await self._drain()
        assert len(p.fired) == 1, "steady state must NOT refire"

        p.maybe_refire_query_map(cleanreport_arrived=True)
        await self._drain()
        assert len(p.fired) == 2, "a new clean report must refire"

    async def test_incomplete_catalog_retries_are_throttled(self):
        p = make_provider()
        p._state_ref = {  # noqa: SLF001
            "robotInfo": SimpleNamespace(workStatus=WORK_STATUS_MOWING),
            "zone_catalog": None,
        }
        p._prev_work_status = WORK_STATUS_MOWING  # noqa: SLF001
        p.maybe_refire_query_map()  # _last_catalog_query_at = datetime.min: fire
        await self._drain()
        assert len(p.fired) == 1
        p.maybe_refire_query_map()  # just queried: 60 s throttle blocks it
        await self._drain()
        assert len(p.fired) == 1, "retry must be throttled to 60 s apart"


class TestReconnectBackoff:
    async def test_backoff_doubles_then_recovers_with_fresh_catalog(self, monkeypatch):
        import app.services.real_provider as rp

        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)

        monkeypatch.setattr(rp.asyncio, "sleep", fake_sleep)

        p = make_provider()
        attempts = {"n": 0}

        class FlakyMqtt:
            async def disconnect(self) -> None: pass

            async def connect(self) -> None:
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise OSError("broker down")

        p.mqtt = FlakyMqtt()  # type: ignore[assignment]
        await p._reconnect_with_fresh_creds()  # noqa: SLF001
        assert sleeps == [5, 10], "backoff must double per failed attempt"
        assert p.fired == [(USER_CTRL_QUERY_MAP, True)], "requery catalog after reconnect"
        assert p._reconnecting is False  # noqa: SLF001
