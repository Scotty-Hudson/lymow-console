"""Typed API models for the console HTTP/WS contract.

These mirror the JSON shapes the frontend consumes; the state document
is produced by MowerStateService.snapshot() and pushed verbatim over
the WebSocket, so this module is the single place both sides trust.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ── responses ───────────────────────────────────────────────────


class TelemetryOut(BaseModel):
    """Flat telemetry field table (see app/lymow/telemetry.py)."""
    model_config = {"extra": "allow"}


class StateSnapshotOut(BaseModel):
    """Dashboard snapshot pushed on every state change."""
    device_id: str
    online: bool = False
    simulator: bool = False
    stale: bool = True              # no fresh telemetry inside window
    map_ready: bool = False
    activity: str | None = None
    status: dict = Field(default_factory=dict)
    telemetry: TelemetryOut = Field(default_factory=TelemetryOut)
    allowed_actions: dict[str, int | None] = Field(default_factory=dict)
    task_zones: list[dict] = Field(default_factory=list)
    pose: dict | None = None
    last_update_epoch: float | None = None


class MapDocumentOut(BaseModel):
    available: bool = False
    zones: list[dict] = Field(default_factory=list)
    channels: list[dict] = Field(default_factory=list)
    dock: dict | None = None
    pose: dict | None = None
    mower_latlon: dict | None = None
    enu_base: dict | None = None
    current_zone: str | None = None
    frame: str | None = None
    # Documented gaps in the community protocol (read-only console):
    unsupported: list[dict] = Field(default_factory=list)


class CommandResultOut(BaseModel):
    action: str
    outcome: str                    # sent | sent-unconfirmed
    confirmed: bool


# ── requests ────────────────────────────────────────────────────


class LoginIn(BaseModel):
    password: str = Field(min_length=1)


class CommandIn(BaseModel):
    """Body for every control action (all fields optional)."""
    zones: list[str] | None = None  # zone start: names or hashIds
    confirm: bool = False           # required for destructive actions


class ZoneConfigIn(BaseModel):
    """Auto-recharge/resume thresholds — verified setRR fields only."""
    enable_rr: bool
    recharge_bat: int | None = Field(default=None, ge=1, le=100)
    resume_bat: int | None = Field(default=None, ge=1, le=100)


class CloudSettingsIn(BaseModel):
    """Lymow cloud credentials — accepted write-only, stored server-side,
    NEVER returned by any endpoint (GET shows masked values)."""
    lymow_username: str | None = None
    lymow_password: str | None = None
    device_thing_name: str | None = None
    region: str | None = None  # eu-west-1 | us-east-2 | ap-southeast-2 | ap-east-1
    auth_method: str | None = None  # "password" | "google"


class OAuthExchangeIn(BaseModel):
    """Federated (Google) sign-in: the full failed myapp://callback URL
    (or the bare auth code) copied from the browser."""
    pasted: str = Field(min_length=8)
    region: str | None = None
