"""TelemetryHub — fan-out of state snapshots to WebSocket clients."""
from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

# Bounded per-client queue: if a client falls this far behind the UI,
# drop its oldest frame instead of leaking memory or blocking the bus.
MAX_QUEUE = 32


class TelemetryHub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[dict]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Snapshot callbacks can fire from the MQTT client thread; the
        hub needs the app loop to enqueue thread-safely."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=MAX_QUEUE)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        self._clients.discard(q)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def publish(self, snapshot: dict) -> None:
        """Thread-safe-ish entrypoint used as a state-service listener."""
        if not self._clients:
            return
        loop = self._loop
        if loop is None:  # same-loop call sites (simulator, tests)
            self._publish_now(snapshot)
        else:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                self._publish_now(snapshot)
            else:
                loop.call_soon_threadsafe(self._publish_now, snapshot)

    def _publish_now(self, snapshot: dict) -> None:
        for q in list(self._clients):
            if q.full():
                try:
                    q.get_nowait()  # drop oldest frame for slow clients
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(snapshot)
