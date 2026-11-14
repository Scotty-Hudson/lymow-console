"""HTTP API routes.

Auth posture: EVERY /api endpoint except /api/auth/* requires a valid
session cookie. Credentials for the Lymow CLOUD are accepted write-only
and are never echoed back (masked in GET, never logged).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app import security
from app.lymow.const import API_ENDPOINTS
from app.models.api import (
    CloudSettingsIn,
    CommandIn,
    LoginIn,
)
from app.services.camera_service import CameraUnavailable
from app.services.command_service import CommandRejected

_LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(security.get_current_session)])


def _services(request: Request):
    return request.app.state  # db, state_service, command_service, ...


# ── auth ────────────────────────────────────────────────────────

auth_router = APIRouter(prefix="/api/auth")


@auth_router.get("/status")
async def auth_status(request: Request) -> dict[str, Any]:
    needs_setup = await _services(request).db.get_setting("console_password_hash") is None
    token = request.cookies.get(security.SESSION_COOKIE_NAME)
    authenticated = bool(token and request.app.state.session_signer.verify(token))
    return {"needs_setup": needs_setup, "authenticated": authenticated}


@auth_router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    db = _services(request).db
    stored = await db.get_setting("console_password_hash")
    if stored is None:
        # First run: this login initializes the console password. Safe
        # because the server binds to localhost by default (README
        # documents the LAN exposure pattern).
        await db.set_setting("console_password_hash", security.hash_password(body.password))
        _LOGGER.info("console password initialized (first login)")
    elif not security.verify_password(body.password, stored):
        raise HTTPException(status_code=401, detail="Invalid password")
    token = request.app.state.session_signer.create()
    response.set_cookie(
        key=security.SESSION_COOKIE_NAME,
        value=token,
        max_age=request.app.state.settings.session_max_age_s,
        httponly=True,
        samesite="lax",
        secure=False,  # LAN/localhost http; document TLS-terminating proxy for internet
    )
    return {"ok": True}


@auth_router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    response.delete_cookie(security.SESSION_COOKIE_NAME)
    return {"ok": True}


# ── read-only views ─────────────────────────────────────────────

@router.get("/state")
async def get_state(request: Request) -> dict[str, Any]:
    return _services(request).state_service.snapshot()


@router.get("/map")
async def get_map(request: Request) -> dict[str, Any]:
    return _services(request).map_service.get_map()


@router.get("/history")
async def get_history(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    return await _services(request).history_service.list_tasks(min(limit, 500))


@router.get("/audit")
async def get_audit(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    return await _services(request).db.list_audit(min(limit, 500))


@router.get("/camera/status")
async def camera_status(request: Request) -> dict[str, Any]:
    return _services(request).camera_service.status()


# ── control (validated + audited inside CommandService) ─────────

@router.post("/commands/start")
async def cmd_start(body: CommandIn, request: Request) -> dict[str, Any]:
    return await _run(request, lambda cs: cs.start(zones=body.zones))


@router.post("/commands/pause")
async def cmd_pause(request: Request) -> dict[str, Any]:
    return await _run(request, lambda cs: cs.pause())


@router.post("/commands/resume")
async def cmd_resume(request: Request) -> dict[str, Any]:
    return await _run(request, lambda cs: cs.resume())


@router.post("/commands/dock")
async def cmd_dock(request: Request) -> dict[str, Any]:
    return await _run(request, lambda cs: cs.dock())


@router.post("/commands/dock-cancel")
async def cmd_dock_cancel(body: CommandIn, request: Request) -> dict[str, Any]:
    return await _run(request, lambda cs: cs.dock_cancel(confirm=body.confirm))


@router.post("/commands/force-reinit")
async def cmd_force_reinit(body: CommandIn, request: Request) -> dict[str, Any]:
    return await _run(request, lambda cs: cs.force_reinit(confirm=body.confirm))


@router.post("/commands/query-map")
async def cmd_query_map(request: Request) -> dict[str, Any]:
    return await _run(request, lambda cs: cs.query("map"))


@router.post("/commands/query-schedules")
async def cmd_query_schedules(request: Request) -> dict[str, Any]:
    return await _run(request, lambda cs: cs.query("schedules"))


async def _run(request: Request, fn) -> dict[str, Any]:
    cs = _services(request).command_service
    try:
        return await fn(cs)
    except CommandRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        _LOGGER.exception("command transport failure")
        raise HTTPException(status_code=502, detail=f"mower transport error: {exc}") from exc


# ── configuration ───────────────────────────────────────────────

@router.put("/config/return-to-dock")
async def set_rr(body: dict[str, Any], request: Request) -> dict[str, Any]:
    cs = _services(request).command_service
    payload = {
        "enableRr": bool(body.get("enableRr", True)),
        "rechargeBat": body.get("rechargeBat"),
        "resumeBat": body.get("resumeBat"),
    }
    if payload["rechargeBat"] is not None and not (1 <= int(payload["rechargeBat"]) <= 100):
        raise HTTPException(status_code=422, detail="rechargeBat must be 1..100")
    if payload["resumeBat"] is not None and not (1 <= int(payload["resumeBat"]) <= 100):
        raise HTTPException(status_code=422, detail="resumeBat must be 1..100")
    try:
        return await cs.set_rr_config(**payload)
    except CommandRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ── settings (cloud credentials: write-only) ────────────────────

@router.get("/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    data = await _services(request).db.all_settings_public()
    data.pop("console_password_hash", None)
    return data


@router.put("/settings/cloud")
async def put_cloud_settings(body: CloudSettingsIn, request: Request) -> dict[str, Any]:
    db = _services(request).db
    await db.set_setting("lymow_username", body.lymow_username)
    await db.set_setting("lymow_password", body.lymow_password)
    if body.device_thing_name:
        await db.set_setting("device_thing_name", body.device_thing_name)
    if body.region:
        if body.region not in API_ENDPOINTS:
            raise HTTPException(status_code=422, detail=f"unknown region: {body.region}")
        await db.set_setting("lymow_region", body.region)
    _LOGGER.info("cloud credentials updated (value not logged)")
    return {"ok": True}


@router.delete("/settings/cloud")
async def delete_cloud_settings(request: Request) -> dict[str, Any]:
    db = _services(request).db
    for key in ("lymow_username", "lymow_password", "device_thing_name",
                "lymow_region"):
        await db.delete_setting(key)
    return {"ok": True}


@router.get("/discovery/devices")
async def discovery_devices(request: Request, region: str | None = None) -> list[dict]:
    """Log in with the STORED credentials (transient session, closed right
    after) and list the account's mower thing names. Read-only: touches
    the REST device list only — the mower itself is never commanded."""
    db = _services(request).db
    email = await db.get_setting("lymow_username")
    password = await db.get_setting("lymow_password")
    if not email or not password:
        raise HTTPException(status_code=409,
                            detail="no Lymow credentials configured yet")
    region = region or await db.get_setting("lymow_region") \
        or request.app.state.settings.region
    if region not in API_ENDPOINTS:
        raise HTTPException(status_code=422, detail=f"unknown region: {region}")

    from app.services.real_provider import RealMowerProvider

    probe = RealMowerProvider(region=region, device_id="", email=email,
                              password=password)
    try:
        await probe._ensure_session()  # noqa: SLF001 — deliberate internal reuse
        await probe._login()
        return await probe.list_devices()
    except HTTPException:
        raise
    except Exception as exc:
        _LOGGER.warning("device discovery failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="login/discovery failed (check credentials and region)",
        ) from exc
    finally:
        await probe.stop()


# ── camera stream (multipart MJPEG/PNG) ─────────────────────────

@router.get("/camera/stream")
async def camera_stream(request: Request):
    from fastapi.responses import StreamingResponse

    camera = _services(request).camera_service

    async def parts():
        boundary = "--lymowframe"
        try:
            async for frame in camera.stream():
                yield (
                    f"{boundary}\r\nContent-Type: "
                    f"{'image/png' if request.app.state.provider.simulator else 'image/jpeg'}"
                    f"\r\nContent-Length: {len(frame)}\r\n\r\n"
                ).encode("ascii") + frame + b"\r\n"
        except CameraUnavailable as exc:
            _LOGGER.info("camera stream closed: %s", exc.reason)
            return

    try:
        return StreamingResponse(
            parts(), media_type="multipart/x-mixed-replace; boundary=lymowframe"
        )
    except Exception:  # pragma: no cover — generation errors surface in-stream
        raise HTTPException(status_code=503, detail="camera unavailable")
