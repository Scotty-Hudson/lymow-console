"""Shared test helpers (not fixtures) for the backend suite."""
from __future__ import annotations

import pathlib

from app.lymow import lymow_extracted_pb2 as pb
from app.lymow.protocol import decode_pboutput_envelope

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"


def load_fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def decode_outputs(raws: list[bytes]) -> list[pb.PbOutput]:
    """Decode every captured on_pboutput payload (JSON envelope → PbOutput)."""
    return [decode_pboutput_envelope(raw) for raw in raws]


def state_messages(raws: list[bytes]) -> list[pb.PbOutput]:
    """Captured payloads that carry a robotInfo snapshot."""
    return [m for m in decode_outputs(raws) if m.robotInfo.ByteSize() or m.ListFields()
            and any(f.name == "robotInfo" for f, _ in m.ListFields())]


def catalog_messages(raws: list[bytes]) -> list[pb.PbOutput]:
    """Captured payloads carrying the btMap zone-catalog blob."""
    return [m for m in decode_outputs(raws) if m.btMap.ByteSize()]
