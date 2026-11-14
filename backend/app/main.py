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
from app.lymow.const import API_ENDPOINTS
from app.security import SessionSigner
from app.services.camera_service import CameraService
from app.services.command_service import CommandService
from app.services.history_service import HistoryService
from app.services.provider import spawn
from app.services.map_service import MapService
from app.services.state_service import MowerStateService
from app.services.telemetry_service import TelemetryHub

_LOGGER = logging.getLogger(__name__)

# The built frontend lives inside the package so the console is one
# installable unit (uvx / pip). Rebuild with `npm run build` in
# frontend/ and commit the output.
STATIC_DIR = Path(__file__).resolve().parent / "static"


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
            auth_method = await db.get_setting("lymow_auth_method") or "password"
            provider.auth_method = "oauth" if auth_method == "google" else "password"
            provider.refresh_token = await db.get_setting("lymow_refresh_token")

            def _persist_refresh_token(token: str, _db=db) -> None:
                # CognitoAuth may rotate the refresh token inside async
                # tasks; schedule the DB write on the running loop.
                spawn(
                    _db.set_setting("lymow_refresh_token", token)
                )

            provider.on_refresh_token = _persist_refresh_token
            have_creds = (
                provider.refresh_token if provider.auth_method == "oauth"
                else (email and password)
            )
            region = await db.get_setting("lymow_region") or provider.region
            if region not in API_ENDPOINTS:
                _LOGGER.error("stored region %r unknown — falling back to %s",
                              region, provider.region)
                region = provider.region
            provider.region = region
            provider.host = API_ENDPOINTS[provider.region]["iotDomain"]
            if not have_creds:
                _LOGGER.error(
                    "provider=real but no %s configured — use Settings "
                    "(email+password or Sign in with Google); staying offline",
                    "Google sign-in token" if provider.auth_method == "oauth"
                    else "Lymow credentials",
                )
            elif not device:
                # No pinned device: auto-pick when the account has exactly
                # one mower; ambiguous accounts must pin one in Settings.
                try:
                    await provider._ensure_session()  # noqa: SLF001
                    await provider._login()  # noqa: SLF001
                    devices = await provider.list_devices()
                    if len(devices) == 1:
                        device = devices[0]["deviceThingName"]
                        await db.set_setting("device_thing_name", device)
                        _LOGGER.info("auto-selected sole account device %s", device)
                    else:
                        _LOGGER.error(
                            "provider=real but no device pinned and account has "
                            "%d devices — pick one in Settings; staying offline",
                            len(devices),
                        )
                except Exception:
                    _LOGGER.exception(
                        "device discovery failed — configure credentials/"
                        "region in Settings; the console stays offline"
                    )
            provider.device_id = device or provider.device_id

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

        if provider.simulator is False and not provider.device_id:
            _LOGGER.error(
                "real provider has no device thing name — NOT connecting "
                "(configure credentials + device in Settings, then restart)"
            )
        else:
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


def _cloud_configured(s: Settings) -> bool:
    """True once the Settings page has stored Lymow cloud credentials
    (Google refresh token or native password). Read-only sync probe used
    by the installed entry point only, before the async DB is opened."""
    if not s.db_path.exists():
        return False
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{s.db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT 1 FROM settings WHERE key IN "
                "('lymow_refresh_token','lymow_password') "
                "AND value != '' LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        return row is not None
    except sqlite3.Error:
        return False


def run() -> None:
    """Console entry point installed by pip/uvx (command: lymow-console).

    Boots straight onto the real mower when the Settings page has already
    stored cloud credentials; until then the safe simulator. An explicit
    LYMOW_PROVIDER env var always wins. systemd deploys use uvicorn directly.
    """
    import os
    import threading
    import webbrowser

    import uvicorn

    s = get_settings()
    if s.provider == "sim" and os.environ.get("LYMOW_PROVIDER") is None \
            and _cloud_configured(s):
        _LOGGER.info("Lymow credentials on file -> starting against the "
                     "real mower (set LYMOW_PROVIDER=sim to override)")
        s.provider = "real"
    url = f"http://{s.host}:{s.port}"
    mode = "real mower" if s.provider == "real" else "simulator"
    print(f"lymow-console -> {url}  ({mode})")
    threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run(create_app(s), host=s.host, port=s.port)


if __name__ == "__main__":
    run()
