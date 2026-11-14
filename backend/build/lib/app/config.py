"""Application configuration via pydantic-settings + environment.

Everything is local-first: the server binds to 127.0.0.1 by default and
must be deliberately re-bound for LAN use (README documents the
localhost-ssh-tunnel pattern instead).
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PORT = 8384


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LYMOW_", env_file=".env", env_file_encoding="utf-8"
    )

    # sim | real — real mode additionally requires credentials configured
    # through the settings UI (stored server-side in the SQLite DB).
    provider: str = "sim"

    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT

    # SQLite + generated secrets live here (chmod 0600 files).
    data_dir: Path = Path.home() / ".local" / "share" / "lymow-console"

    # Explicit override; otherwise a secret is generated once into
    # <data_dir>/session.key and reused across restarts.
    session_secret: str | None = None
    session_max_age_s: int = 12 * 3600

    device_thing_name: str | None = None  # real mode: pick a specific device

    # Lymow cloud region pool (verified: eu-west-1, us-east-2,
    # ap-southeast-2, ap-east-1 — see app/lymow/const.py).
    region: str = "eu-west-1"

    @field_validator("provider")
    @classmethod
    def _provider_choice(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("sim", "real"):
            raise ValueError("LYMOW_PROVIDER must be 'sim' or 'real'")
        return v

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_tilde(cls, v):
        # ".env.example" ships LYMOW_DATA_DIR=~/.local/... — Path("~")
        # would otherwise create a literal "~" folder inside the cwd.
        return Path(v).expanduser() if isinstance(v, str) else v

    def resolved_session_secret(self) -> str:
        """Session signing key: env override or generated-once file."""
        if self.session_secret:
            return self.session_secret
        key_file = self.data_dir / "session.key"
        if key_file.exists():
            secret = key_file.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        self.data_dir.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(32)
        key_file.write_text(secret, encoding="utf-8")
        key_file.chmod(0o600)
        return secret

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lymow-console.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
