"""Typed API request models for the console HTTP contract.

Response shapes are produced by MowerStateService.snapshot() /
MapService.get_map() and mirrored (by hand) in frontend/src/types.ts;
requests are validated here.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# ── requests ────────────────────────────────────────────────────


class LoginIn(BaseModel):
    password: str = Field(min_length=1)


class CommandIn(BaseModel):
    """Body for every control action (all fields optional)."""
    zones: list[str] | None = None  # zone start: names or hashIds
    confirm: bool = False           # required for destructive actions


class ZoneConfigIn(BaseModel):
    """Auto-recharge/resume thresholds — verified setRR fields only.

    Field names match the JSON the frontend sends (camelCase)."""
    model_config = ConfigDict(populate_by_name=True)

    enable_rr: bool = Field(default=True, alias="enableRr")
    recharge_bat: int | None = Field(default=None, ge=1, le=100, alias="rechargeBat")
    resume_bat: int | None = Field(default=None, ge=1, le=100, alias="resumeBat")


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
