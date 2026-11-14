"""SIMULATOR ISOLATION — the safety guarantee for the whole project.

While developing against the simulator, no code path may ever touch a
real transport: no sockets, no DNS, no MQTT client, no HTTP session,
no websocket. This test blows up every network entry point, then runs
the COMPLETE command surface through SimulatedMower and asserts that
(a) nothing network-shaped was even constructed and (b) the commands
still worked. If anyone ever wires the simulator to a real provider
path, this test goes red before a real mower can receive a stray
command.
"""
from __future__ import annotations

import socket

import pytest

from app.lymow.const import (
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_FORCE_REINIT,
    USER_CTRL_PAUSE,
    USER_CTRL_PAUSE_DOCK,
    USER_CTRL_QUERY_MAP,
    USER_CTRL_QUERY_SCHEDULES,
    USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_RESUME,
    USER_CTRL_RESUME_DOCK,
    WORK_STATUS_CHARGING,
    WORK_STATUS_MOWING,
)
from app.lymow.protocol import (
    encode_query_map,
    encode_start_zones,
    encode_upload_robot_config,
    encode_userctrl,
)
from app.services.simulator import SimulatedMower

ALL_CTRLS = [
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_PAUSE,
    USER_CTRL_RESUME,
    USER_CTRL_PAUSE_DOCK,
    USER_CTRL_RESUME_DOCK,
    USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_FORCE_REINIT,
    USER_CTRL_QUERY_MAP,
    USER_CTRL_QUERY_SCHEDULES,
]


@pytest.fixture
def network_tripwires(monkeypatch):
    """Explode every network entry point; record any attempted use."""
    attempts: list[str] = []

    def trip(name):
        def boom(*args, **kwargs):
            attempts.append(name)
            raise AssertionError(f"SIMULATOR TOUCHED NETWORK: {name}")
        return boom

    class ExplodingClient:  # paho.mqtt.client.Client
        def __init__(self, *a, **k):
            boom = trip("paho.Client")
            boom()

    # AF_UNIX must pass through: asyncio's event-loop self-pipe is created
    # lazily and uses socketpair(); only outbound INET/INET6 sockets trip.
    real_socket_cls = socket.socket

    def guarded_socket(*args, **kwargs):
        family = args[0] if args else kwargs.get("family", socket.AF_INET)
        if family == socket.AF_UNIX:
            return real_socket_cls(*args, **kwargs)
        trip(f"socket.socket(family={family!r})")

    monkeypatch.setattr(socket, "socket", guarded_socket)
    monkeypatch.setattr(socket, "create_connection", trip("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", trip("socket.getaddrinfo"))
    monkeypatch.setattr("paho.mqtt.client.Client", ExplodingClient)
    monkeypatch.setattr("websockets.connect", trip("websockets.connect"))
    monkeypatch.setattr("aiohttp.ClientSession", trip("aiohttp.ClientSession"))
    return attempts


async def test_full_command_surface_never_touches_network(network_tripwires):
    attempts = network_tripwires
    mower = SimulatedMower()
    outputs: list[bytes] = []
    mower.on_pboutput = outputs.append

    await mower.start()
    assert mower.is_connected()
    assert outputs, "simulator produced no state while network was disabled"

    # Every userCtrl the console can send, in a realistic sequence.
    await mower.publish_pbinput(encode_start_zones(["front1", "back01"]))
    assert mower.work_status == WORK_STATUS_MOWING
    await mower.publish_pbinput(encode_userctrl(USER_CTRL_PAUSE))
    await mower.publish_pbinput(encode_userctrl(USER_CTRL_RESUME))
    await mower.publish_pbinput(encode_userctrl(USER_CTRL_RECHARGE_DOCK))
    await mower.publish_pbinput(encode_userctrl(USER_CTRL_PAUSE_DOCK))
    await mower.publish_pbinput(encode_userctrl(USER_CTRL_RESUME_DOCK))
    await mower.publish_pbinput(encode_userctrl(USER_CTRL_DOCK))
    await mower.publish_pbinput(encode_userctrl(USER_CTRL_FORCE_REINIT))
    await mower.publish_pbinput(encode_query_map())
    await mower.publish_pbinput(encode_userctrl(USER_CTRL_QUERY_SCHEDULES))
    await mower.publish_pbinput(encode_upload_robot_config())

    # Simulator keeps responding locally: run the state machine a bit.
    for _ in range(10):
        mower._tick()
    devices = await mower.list_devices()
    assert devices[0]["deviceThingName"] == mower.device_id

    await mower.stop()

    assert attempts == [], f"simulator touched network: {attempts}"
    assert outputs, "commands through the simulated mower produced no telemetry"


async def test_simulator_provider_flag_is_bulwark():
    """The service layer gates real-mower wiring on `simulator is False`;
    if this flag ever lies, the isolation tests above are the backstop."""
    mower = SimulatedMower()
    assert mower.simulator is True
    assert mower.simulator is not False  # must be a real bool, not truthy object
