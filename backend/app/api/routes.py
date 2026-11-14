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
from app.lymow.auth import build_oauth_url, extract_oauth_code
from app.lymow.const import API_ENDPOINTS
from app.models.api import (
    CloudSettingsIn,
    CommandIn,
    LoginIn,
    OAuthExchangeIn,
)
from app.services.camera_service import CameraUnavailable
from app.services.command_service import CommandRejected
from app.services.real_provider import RealMowerProvider

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
    method = body.auth_method or "password"
    if method not in ("password", "google"):
        raise HTTPException(status_code=422,
                            detail="auth_method must be 'password' or 'google'")
    if method == "password":
        haveStored = (
            await db.get_setting("lymow_username")
            and await db.get_setting("lymow_password")
        )
        if not haveStored and not (body.lymow_username and body.lymow_password):
            raise HTTPException(
                status_code=422,
                detail="email + password required for password auth "
                       "(use Sign in with Google for a Google account)",
            )
    await db.set_setting("lymow_auth_method", method)
    if body.lymow_username:
        await db.set_setting("lymow_username", body.lymow_username)
    if body.lymow_password:
        await db.set_setting("lymow_password", body.lymow_password)
    if body.device_thing_name:
        await db.set_setting("device_thing_name", body.device_thing_name)
    if body.region:
        if body.region not in API_ENDPOINTS:
            raise HTTPException(status_code=422, detail=f"unknown region: {body.region}")
        await db.set_setting("lymow_region", body.region)
    _LOGGER.info("cloud settings updated (method=%s, values not logged)", method)
    return {"ok": True}


@router.delete("/settings/cloud")
async def delete_cloud_settings(request: Request) -> dict[str, Any]:
    db = _services(request).db
    for key in ("lymow_username", "lymow_password", "device_thing_name",
                "lymow_region", "lymow_auth_method", "lymow_refresh_token"):
        await db.delete_setting(key)
    return {"ok": True}


@router.get("/settings/oauth-url")
async def oauth_start_url(request: Request, region: str | None = None) -> dict[str, Any]:
    """Cognito hosted-UI link for 'Sign in with Google'. The final
    myapp://callback redirect deliberately fails in the browser — the
    user copies that URL and pastes it into oauth-exchange."""
    region = await _resolve_region(request, region)
    return {"url": build_oauth_url(region), "region": region,
            "redirect_hint": "After Google sign-in the browser fails to open "
                             "myapp://callback/... — copy that full URL."}


@router.post("/settings/oauth-exchange")
async def oauth_exchange(body: OAuthExchangeIn, request: Request) -> dict[str, Any]:
    """Complete the federated Google flow: extract the auth code from the
    pasted callback URL, exchange it for Cognito tokens, persist the
    refresh token (masked secret), and return the account's devices."""
    db = _services(request).db
    region = await _resolve_region(request, body.region)
    code = extract_oauth_code(body.pasted)
    if not code:
        raise HTTPException(
            status_code=422,
            detail="no auth code found — paste the FULL myapp://callback/?code=… "
                   "URL from the browser address bar",
        )
    probe = RealMowerProvider(region=region, device_id="", auth_method="oauth")
    try:
        await probe._ensure_session()  # noqa: SLF001
        assert probe.auth is not None
        await probe.auth.exchange_oauth_code(code)
        if not probe.auth.refresh_token:
            raise HTTPException(
                status_code=502,
                detail="sign-in succeeded but returned no refresh token — "
                       "run the Google flow again",
            )
        await probe.auth.get_aws_credentials()
        devices = await probe.list_devices()
        await db.set_setting("lymow_auth_method", "google")
        await db.set_setting("lymow_refresh_token", probe.auth.refresh_token)
        await db.set_setting("lymow_region", region)
        _LOGGER.info("Google sign-in completed (tokens stored, not logged)")
        return {"ok": True, "devices": devices}
    except HTTPException:
        raise
    except Exception as exc:
        _LOGGER.warning("oauth exchange failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="code exchange failed — the code is single-use and short-"
                   "lived; redo the Google sign-in and paste the fresh URL",
        ) from exc
    finally:
        await probe.stop()


async def _resolve_region(request: Request, region: str | None) -> str:
    if not region:
        region = await _services(request).db.get_setting("lymow_region") \
            or request.app.state.settings.region
    if region not in API_ENDPOINTS:
        raise HTTPException(status_code=422, detail=f"unknown region: {region}")
    return region


@router.get("/discovery/devices")
async def discovery_devices(request: Request, region: str | None = None) -> list[dict]:
    """Log in with the STORED credentials (transient session, closed right
    after) and list the account's mower thing names. Read-only: touches
    the REST device list only — the mower itself is never commanded."""
    db = _services(request).db
    method = await db.get_setting("lymow_auth_method") or "password"
    email = await db.get_setting("lymow_username")
    password = await db.get_setting("lymow_password")
    refresh = await db.get_setting("lymow_refresh_token")
    if method == "google":
        if not refresh:
            raise HTTPException(
                status_code=409,
                detail="complete Sign in with Google before discovery",
            )
    elif not email or not password:
        raise HTTPException(status_code=409,
                            detail="no Lymow credentials configured yet")
    region = await _resolve_region(request, region)

    probe = RealMowerProvider(region=region, device_id="", email=email,
                              password=password,
                              auth_method="oauth" if method == "google" else "password",
                              refresh_token=refresh)
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
