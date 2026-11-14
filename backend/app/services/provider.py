"""Mower provider interface: the seam between mower communication and the API.

Two implementations:
  - RealMowerProvider (app/services/real_provider.py): Lymow cloud + AWS IoT MQTT
  - SimulatedMower (app/services/simulator.py): fully local, no network ever
"""
from __future__ import annotations

import abc
from collections.abc import Callable
from typing import Any


class ProviderError(Exception):
    """Base error surfaced to the API layer."""


class PublishError(ProviderError):
    """Failed to hand a PbInput payload to the transport."""


class MowerProvider(abc.ABC):
    """Transport + discovery contract consumed by the service layer."""

    simulator: bool = False

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        # Callbacks wired by MowerStateService before start().
        self.on_pboutput: Callable[[bytes], None] = lambda raw: None
        self.on_notify_app: Callable[[dict], None] = lambda payload: None
        self.on_connection_changed: Callable[[bool], None] = lambda online: None

    @abc.abstractmethod
    async def start(self) -> None:
        """Authenticate (real only), connect, subscribe, fire startup queries."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Disconnect and cancel background tasks."""

    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Transport-level connection liveness (MQTT connected)."""

    @abc.abstractmethod
    async def publish_pbinput(self, raw_pbinput: bytes) -> None:
        """Publish raw PbInput bytes; raise PublishError on transport failure."""

    async def list_devices(self) -> list[dict[str, Any]]:
        """Discover devices bound to the account. Simulator returns itself."""
        return [{"deviceThingName": self.device_id, "deviceName": "simulated-mower"}]
