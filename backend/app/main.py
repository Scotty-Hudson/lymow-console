"""lymow-console backend application factory + lifecycle.

Composition root: provider (sim|real) → state service → hub/services →
HTTP+WS routes. Run with:

    uvicorn app.main:app --host 127.0.0.1 --port 8384
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api.routes import auth_router, router
from app.api.ws import ws_router
from app.config import Settings, get_settings
from app.db import Database
from app.security import SessionSigner
from app.services.camera_service import CameraService
from app.services.command_service import CommandService
from app.services.history_service import HistoryService
from app.services.map_service import MapService
from app.services.state_service import MowerStateService
from app.services.telemetry_service import TelemetryHub

_LOGGER = logging.getLogger(__name__)

# lymow-console/frontend/dist (repo root = parents[2] of backend/app/main.py)
STATIC_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _make_provider(settings: Settings, db: Database):
    from app.services.simulator import SimulatedMower

    if settings.provider == "sim":
        _LOGGER.warning("provider=sim: NO real mower contact is possible")
        return SimulatedMower()
    from app.services.real_provider import RealMowerProvider

    return RealMowerProvider(
        region=settings.region,
        device_id="",  # resolved from credentials/discovery below
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        await db.open()
        app.state.settings = settings
        app.state.db = db
        app.state.session_signer = SessionSigner(
            settings.resolved_session_secret(), settings.session_max_age_s
        )

        provider = _make_provider(settings, db)
        if getattr(provider, "simulator", False) is False:
            # Real mode: fill in credentials + device from the DB.
            email = await db.get_setting("lymow_username")
            password = await db.get_setting("lymow_password")
            device = (
                settings.device_thing_name
                or await db.get_setting("device_thing_name")
            )
            provider.email = email
            provider.password = password
            provider.device_id = device or provider.device_id
            if not email or not password or not device:
                _LOGGER.error(
                    "provider=real but credentials/device are not configured "
                    "— configure them in Settings; the console stays offline"
                )

        state_service = MowerStateService(provider)
        state_service.attach()
        hub = TelemetryHub()
        hub.bind_loop(asyncio.get_running_loop())
        state_service.add_listener(hub.publish)
        command_service = CommandService(provider, state_service, db)
        map_service = MapService(state_service)
        history_service = HistoryService(provider.simulator, db, state_service)
        history_service.attach()
        camera_service = CameraService(provider, state_service)
        await camera_service.start()

        app.state.provider = provider
        app.state.state_service = state_service
        app.state.telemetry_hub = hub
        app.state.command_service = command_service
        app.state.map_service = map_service
        app.state.history_service = history_service
        app.state.camera_service = camera_service

        try:
            await provider.start()
        except Exception:
            _LOGGER.exception("provider start failed; console stays offline")

        _LOGGER.info(
            "lymow-console ready (provider=%s, bind=%s:%s)",
            settings.provider, settings.host, settings.port,
        )
        yield
        await provider.stop()
        await camera_service.stop()
        await db.close()

    app = FastAPI(title="lymow-console", version="0.1.0", lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(ws_router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "provider": settings.provider}

    # Built frontend (created by dev/build scripts); optional in dev,
    # where Vite serves the UI and proxies /api to this backend.
    if STATIC_DIR.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=False)
