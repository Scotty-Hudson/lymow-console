"""Shared pytest fixtures for the lymow-console backend suite."""
from __future__ import annotations

import pytest

from app.services.simulator import SimulatedMower


@pytest.fixture
async def sim():
    """A SimulatedMower whose PbOutput feed is captured into a list.

    Yields (mower, outputs). The provider loop is always stopped at
    teardown so stray broadcasts can't leak between tests.
    """
    mower = SimulatedMower()
    outputs: list[bytes] = []
    mower.on_pboutput = outputs.append
    yield mower, outputs
    await mower.stop()
