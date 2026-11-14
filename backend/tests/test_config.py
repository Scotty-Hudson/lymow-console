"""Tests for app/config.py — defaults, validation, session-key handling."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.config import Settings


def make_settings(tmp_path, **overrides):
    return Settings(
        data_dir=tmp_path,
        _env_file=None,
        **overrides,
    )


class TestDefaults:
    def test_data_dir_expands_tilde(self):
        """LYMOW_DATA_DIR=~/.local/... must not create a literal '~' dir."""
        s = Settings(_env_file=None, data_dir="~/.lymow-console-test")
        assert s.data_dir == Path.home() / ".lymow-console-test"

    def test_localhost_only_default_bind(self, tmp_path):
        s = make_settings(tmp_path)
        assert s.host == "127.0.0.1"
        assert s.port == 8384

    def test_simulator_is_the_default_provider(self, tmp_path):
        assert make_settings(tmp_path).provider == "sim"

    def test_invalid_provider_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            make_settings(tmp_path, provider="production")


class TestSessionSecret:
    def test_generated_once_and_owner_only(self, tmp_path):
        s = make_settings(tmp_path)
        secret = s.resolved_session_secret()
        assert len(secret) >= 32
        again = make_settings(tmp_path).resolved_session_secret()
        assert again == secret, "secret must be stable across restarts"
        mode = stat.S_IMODE((tmp_path / "session.key").stat().st_mode)
        assert mode == 0o600

    def test_env_override_wins(self, tmp_path):
        s = make_settings(tmp_path, session_secret="explicit-secret")
        assert s.resolved_session_secret() == "explicit-secret"
        assert not (tmp_path / "session.key").exists()


class TestEnvLoading:
    def test_env_prefix_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LYMOW_PROVIDER", "REAL")  # case-insensitive
        monkeypatch.setenv("LYMOW_PORT", "9000")
        monkeypatch.setenv("LYMOW_DATA_DIR", str(tmp_path))
        s = Settings(_env_file=None)
        assert s.provider == "real"
        assert s.port == 9000

    def test_provider_env_cannot_break_default_bind(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LYMOW_HOST", raising=False)
        assert Settings(_env_file=None, data_dir=tmp_path).host == "127.0.0.1"
