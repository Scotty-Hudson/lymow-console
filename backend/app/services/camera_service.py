"""CameraService — secure media relay for the mower camera.

Real mower: the community-verified RTSP endpoint (device IP, port 10022,
path h264ESVideoTest — see const.RTSP_*) is pulled by a LOCAL ffmpeg
subprocess and re-exposed as an MJPEG stream. The browser never talks
to the mower and never sees the RTSP URL.

Simulator: synthesizes PNG frames locally (pure zlib/struct — no extra
dependencies) so the camera panel is exercised end-to-end offline.

Availability is probed, never assumed: missing ffmpeg or a dead RTSP
stream degrades to an "unavailable" API response with a reason, and
clients are disconnected gracefully.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import struct
import subprocess
import time
import zlib
from typing import AsyncIterator

from app.lymow.const import RTSP_PATH, RTSP_PORT
from app.services.provider import MowerProvider
from app.services.state_service import MowerStateService

_LOGGER = logging.getLogger(__name__)

FRAME_INTERVAL_S = 0.4          # simulator frame pacing (real feed: RELAY_FPS)
RELAY_FPS = 15                  # ~mower camera native rate; 2.5 fps looked like a slideshow
FFMPEG_BINARIES = ("ffmpeg", "avconv")


class CameraUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CameraService:
    def __init__(self, provider: MowerProvider, state: MowerStateService) -> None:
        self.provider = provider
        self.state = state
        self._proc: subprocess.Popen | None = None
        self._latest_frame: bytes | None = None
        self._last_frame_ts: float = 0.0
        self._reader_task: asyncio.Task | None = None
        self._unavailable_reason: str | None = None
        self._relay_active = False  # only one pipe-reader allowed at a time
        self._stopping = False

    @staticmethod
    def _find_ffmpeg() -> str | None:
        """Locate an ffmpeg binary: system install first, then the pip
        wheel fallback (imageio-ffmpeg ships a static build — works
        without sudo; `pip install imageio-ffmpeg`)."""
        binary = shutil.which(FFMPEG_BINARIES[0]) or shutil.which(FFMPEG_BINARIES[1])
        if binary:
            return binary
        try:
            import imageio_ffmpeg

            exe = imageio_ffmpeg.get_ffmpeg_exe()
            return exe if exe and os.path.exists(exe) else None
        except Exception:
            return None

    # ── public API ──────────────────────────────────────────────

    def status(self) -> dict:
        live = self._latest_frame is not None and (
            time.time() - self._last_frame_ts < 10.0
        )
        return {
            "mode": "simulated" if self.provider.simulator else "rtsp-relay",
            "available": self._unavailable_reason is None,
            "live": live or self.provider.simulator,
            "reason": self._unavailable_reason,
        }

    async def start(self) -> None:
        if self.provider.simulator:
            return  # frames are synthesized on demand
        if self._find_ffmpeg():
            return  # lazily start the subprocess on first stream request
        self._unavailable_reason = (
            "ffmpeg is not installed (sudo apt install ffmpeg, or "
            "pip install imageio-ffmpeg) — RTSP relay disabled"
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        self._kill_proc()

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield JPEG/PNG frame payloads for the MJPEG response."""
        if self._unavailable_reason and self._find_ffmpeg():
            # A previously dropped RTSP source is retryable once ffmpeg
            # exists — clear the latch so the browser's "retry" works.
            _LOGGER.info("camera: retrying after earlier failure: %s",
                         self._unavailable_reason)
            self._unavailable_reason = None
        if self._unavailable_reason:
            raise CameraUnavailable(self._unavailable_reason)
        if self.provider.simulator:
            async for frame in self._sim_stream():
                yield frame
            return
        if self._relay_active:
            # Two concurrent relay loops would split one stdout pipe and
            # corrupt every frame — refuse the second viewer.
            raise CameraUnavailable("camera feed already open — close the other tab first")
        self._ensure_ffmpeg()
        async for frame in self._relay_stream():
            yield frame

    # ── simulated frames ────────────────────────────────────────

    async def _sim_stream(self) -> AsyncIterator[bytes]:
        while not self._stopping:
            yield render_sim_frame(self.state)
            await asyncio.sleep(FRAME_INTERVAL_S)

    # ── real relay ──────────────────────────────────────────────

    def _rtsp_url(self) -> str:
        t = self.state.snapshot()["telemetry"]
        # The live mower publishes deviceInfo only sporadically on the
        # MQTT feed; the REST device-info poll is the reliable IP source.
        ip = t.get("ip_address") or getattr(self.provider, "rest_ip_address", None)
        if not ip:
            raise CameraUnavailable(
                "mower IP unknown yet (wait for a telemetry broadcast)"
            )
        return f"rtsp://{ip}:{RTSP_PORT}/{RTSP_PATH}"

    def _ensure_ffmpeg(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        binary = self._find_ffmpeg()
        if binary is None:
            raise CameraUnavailable("ffmpeg not installed")
        url = self._rtsp_url()
        _LOGGER.info("starting camera relay (RTSP source hidden from browser)")
        try:
            self._proc = subprocess.Popen(
                [
                    binary, "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp", "-i", url,
                    "-vf", f"fps={RELAY_FPS}",
                    "-f", "image2pipe", "-vcodec", "mjpeg",
                    "-q:v", "6", "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise CameraUnavailable(f"could not start ffmpeg: {exc}") from exc

    def _kill_proc(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=3)  # SIGKILL is immediate; reap the zombie
                except Exception:
                    pass
            self._proc = None

    async def _relay_stream(self) -> AsyncIterator[bytes]:
        assert self._proc is not None and self._proc.stdout is not None
        loop = asyncio.get_running_loop()
        buf = b""
        self._relay_active = True
        try:
            async for frame in self._read_frames(loop, buf):
                yield frame
        finally:
            self._relay_active = False
            self._kill_proc()  # last viewer gone: free the mower's RTSP slot

    async def _read_frames(self, loop, buf: bytes) -> AsyncIterator[bytes]:
        while not self._stopping:
            chunk = await loop.run_in_executor(
                None, lambda: self._proc.stdout.read(65536)
                if self._proc and self._proc.stdout else b""
            )
            if not chunk:
                self._unavailable_reason = "RTSP stream ended (mower camera offline?)"
                self._kill_proc()
                break
            buf += chunk
            # split on JPEG EOI (0xFFD9): each MJPEG frame ends there
            while True:
                eoi = buf.find(b"\xff\xd9")
                if eoi < 0:
                    break
                frame, buf = buf[: eoi + 2], buf[eoi + 2:]
                if frame.startswith(b"\xff\xd8"):
                    self._latest_frame = frame
                    self._last_frame_ts = time.time()
                    yield frame
            if len(buf) > 4_000_000:  # runaway guard: no JPEG magic found
                self._unavailable_reason = "RTSP payload is not MJPEG-framed"
                self._kill_proc()
                break


# ── synthetic PNG renderer (simulator mode) ─────────────────────
#
# Draws the loaded zone polygons + dock + live pose into a flat RGB
# image and encodes it as a valid PNG with zlib — deliberately no
# Pillow/ffmpeg dependency so `sim` mode works on a bare system.

SIM_W, SIM_H = 320, 240


def _png_bytes(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3]
                   for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def render_sim_frame(state_service: MowerStateService) -> bytes:
    w, h = SIM_W, SIM_H
    px = bytearray(b"\x10\x18\x14" * (w * h))  # dark grey-green background

    def put(x: int, y: int, rgb: tuple[int, int, int]) -> None:
        if 0 <= x < w and 0 <= y < h:
            i = (y * w + x) * 3
            px[i:i + 3] = bytes(rgb)

    def line(x0: int, y0: int, x1: int, y1: int, rgb) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            put(x0, y0, rgb)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy; x0 += sx
            if e2 <= dx:
                err += dx; y0 += sy

    catalog = state_service.state.get("zone_catalog")
    pose = state_service.state.get("pose")
    cs = state_service.state.get("chargingStationLoc")

    # world (ENU meters) -> pixels: fit all zones with padding
    pts = [p for z in (catalog.zones if catalog else []) for p in z.polygon_points]
    if cs is not None:
        pts.append((cs.x, cs.y))
    if pts:
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        scale = min((w - 24) / max(1e-6, max_x - min_x),
                    (h - 24) / max(1e-6, max_y - min_y))
        off_x = (w - (max_x - min_x) * scale) / 2
        off_y = (h + (max_y - min_y) * scale) / 2  # +y up -> image down

        def world(pt: tuple[float, float]) -> tuple[int, int]:
            return (int(off_x + (pt[0] - min_x) * scale),
                    int(off_y - (pt[1] - min_y) * scale))

        for z in (catalog.zones if catalog else []):
            poly = z.polygon_points
            for a, b in zip(poly, poly[1:] + poly[:1]):
                (ax, ay), (bx, by) = world(a), world(b)
                line(ax, ay, bx, by, (60, 140, 70))
        if catalog and catalog.channels:
            for c in catalog.channels:
                poly = c.polygon_points
                for a, b in zip(poly, poly[1:] + poly[:1]):
                    (ax, ay), (bx, by) = world(a), world(b)
                    line(ax, ay, bx, by, (90, 90, 140))
        if cs is not None:
            dx, dy = world((cs.x, cs.y))
            for xx in range(dx - 5, dx + 6):
                for yy in range(dy - 5, dy + 6):
                    put(xx, yy, (220, 190, 60))
        if pose is not None:
            mx, my = world((pose.x, pose.y))
            for xx in range(mx - 4, mx + 5):
                for yy in range(my - 4, my + 5):
                    put(xx, yy, (240, 80, 60))
    # scanline darkening for a "camera feed" vibe + timestamp-free grain
    for y in range(0, h, 3):
        for x in range(w):
            i = (y * w + x) * 3
            px[i] = max(0, px[i] - 12)
            px[i + 1] = max(0, px[i + 1] - 12)
            px[i + 2] = max(0, px[i + 2] - 12)
    return _png_bytes(w, h, bytes(px))
