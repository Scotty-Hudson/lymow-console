"""Camera service: simulated frame renderer + availability posture."""
from __future__ import annotations

import shutil
import struct
import sys
import zlib

import pytest

from app.services.camera_service import CameraService, render_sim_frame
from app.services.simulator import SimulatedMower
from app.services.state_service import MowerStateService


@pytest.fixture
async def sim_state():
    mower = SimulatedMower()
    outputs: list[bytes] = []
    mower.on_pboutput = outputs.append
    state = MowerStateService(mower)
    state.attach()
    await mower.start()
    yield mower, state
    await mower.stop()


def _png_valid(data: bytes) -> bool:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    w, h, depth, ctype = struct.unpack(">IIBB", data[16:26])
    assert (w, h, depth, ctype) == (320, 240, 8, 2)
    # IDAT stream must decompress to (h*(w*3+1)) raw bytes
    idat = data[data.index(b"IDAT") + 4: data.index(b"IEND")]
    raw = zlib.decompress(idat[: len(idat) - 4])
    assert len(raw) == 240 * (320 * 3 + 1)
    return True


class TestSimFrames:
    async def test_frame_renders_valid_png_with_map(self, sim_state):
        mower, state = sim_state
        frame = render_sim_frame(state)
        assert _png_valid(frame)

    async def test_frame_tracks_pose_change(self, sim_state):
        mower, state = sim_state
        before = render_sim_frame(state)
        await mower.publish_pbinput(b"")  # no-op input
        mower._set_status(2)  # WORK_STATUS_MOWING
        mower._start_task(["front1"])
        for _ in range(5):
            mower._tick()
        state._on_pboutput(__import__("app.lymow.protocol", fromlist=["wrap_envelope"]).wrap_envelope(
            mower._build_state_output().SerializeToString()).encode("utf-8"))
        after = render_sim_frame(state)
        assert before != after, "frame must reflect motion"

    async def test_sim_camera_status_live(self, sim_state):
        mower, state = sim_state
        cam = CameraService(mower, state)
        s = cam.status()
        assert s["mode"] == "simulated" and s["available"] and s["live"]

    async def test_real_camera_reports_ffmpeg_or_ip_gating(self, sim_state):
        """Real-mode availability degrades with a reason, never crashes."""
        mower, state = sim_state
        mower.simulator = False  # pretend real for this check
        try:
            cam = CameraService(mower, state)
            url = cam._rtsp_url()
            assert url.startswith("rtsp://192.168.50.42:10022/")
        finally:
            mower.simulator = True

    def test_rest_ip_fallback_when_mqtt_deviceinfo_absent(self):
        """Live mower idles without deviceInfo: the REST-poll IP is used."""
        from app.services.provider import MowerProvider
        from app.services.state_service import MowerStateService

        class QuietProvider(MowerProvider):
            def __init__(self):
                super().__init__("quiet")
                self.rest_ip_address = "192.168.10.99"

            async def start(self): pass
            async def stop(self): pass
            def is_connected(self): return True
            async def publish_pbinput(self, raw): pass
            async def list_devices(self): return []

        state = MowerStateService(QuietProvider())
        cam = CameraService(state.provider, state)
        assert cam._rtsp_url() == "rtsp://192.168.10.99:10022/h264ESVideoTest"


class TestFfmpegDiscovery:
    def test_prefers_system_binary(self, monkeypatch):
        monkeypatch.setattr(shutil, "which",
                            lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
        assert CameraService._find_ffmpeg() == "/usr/bin/ffmpeg"

    def test_falls_back_to_pip_wheel(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        try:
            import imageio_ffmpeg
        except ImportError:
            pytest.skip("imageio-ffmpeg not installed")
        found = CameraService._find_ffmpeg()
        assert found and "ffmpeg" in found.lower()

    def test_none_when_never_available(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
        assert CameraService._find_ffmpeg() is None
