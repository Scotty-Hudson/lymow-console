"""WebSocket telemetry endpoint (session-cookie or ?token= auth)."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import security

_LOGGER = logging.getLogger(__name__)

ws_router = APIRouter()


@ws_router.websocket("/ws/telemetry")
async def ws_telemetry(websocket: WebSocket) -> None:
    token = security.get_websocket_session(websocket)
    if token is None:
        await websocket.close(code=4401)
        return

    hub = websocket.app.state.telemetry_hub
    state_service = websocket.app.state.state_service
    queue = hub.subscribe()
    await websocket.accept()
    try:
        # Immediately deliver current state so the UI fills on connect.
        await websocket.send_json({"type": "state", "payload": state_service.snapshot()})
        while True:
            snapshot = await queue.get()
            await websocket.send_json({"type": "state", "payload": snapshot})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(queue)
