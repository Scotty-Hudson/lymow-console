"""Federated (Google/Apple) auth helpers — URL building, code extraction,
token restoration, and the provider's oauth login path. No network."""
from __future__ import annotations

import pytest

from app.lymow import const
from app.lymow.auth import (
    LymowAuthError,
    build_oauth_url,
    extract_oauth_code,
)
from app.services.real_provider import RealMowerProvider


class TestBuildOauthUrl:
    @pytest.mark.parametrize("region", list(const.COGNITO_CONFIG))
    def test_url_shape_per_region(self, region):
        url = build_oauth_url(region)
        cfg = const.COGNITO_CONFIG[region]
        assert url.startswith(f"https://{cfg['hosted_ui_domain']}/login?")
        assert f"client_id={cfg['client_id']}" in url
        assert "myapp%3A%2F%2Fcallback%2F" in url  # encoded redirect_uri
        assert "response_type=code" in url


class TestExtractOauthCode:
    def test_full_callback_url(self):
        code = extract_oauth_code(
            "myapp://callback/?code=abc123XYZ&state=whatever"
        )
        assert code == "abc123XYZ"

    def test_http_url_with_code_param(self):
        assert extract_oauth_code("https://x/?code=q7") == "q7"

    def test_bare_code_passes_through(self):
        assert extract_oauth_code("plaincode") == "plaincode"

    def test_empty_and_junk(self):
        assert extract_oauth_code("   ") is None
        assert extract_oauth_code("myapp://callback/?error=denied") is None


class TestRestoreRefreshToken:
    async def test_provider_oauth_login_without_token(self):
        """Google mode with no stored token: clear error, no connection."""
        p = RealMowerProvider(region="eu-west-1", device_id="t",
                              auth_method="oauth")
        await p._ensure_session()  # noqa: SLF001
        try:
            with pytest.raises(LymowAuthError, match="Google sign-in"):
                await p._login()  # noqa: SLF001
        finally:
            await p.stop()

    async def test_provider_oauth_login_uses_refresh_token(self, monkeypatch):
        """With a stored refresh token the login path is refresh_tokens
        (NOT SRP), and AWS creds are then fetched."""
        calls = []

        from app.lymow.auth import CognitoAuth

        async def fake_refresh(self):
            calls.append(("refresh", self.refresh_token))
            self.id_token, self.access_token = "id", "acc"

        async def fake_aws(self):
            calls.append(("aws", self.id_token))

        monkeypatch.setattr(CognitoAuth, "refresh_tokens", fake_refresh)
        monkeypatch.setattr(CognitoAuth, "get_aws_credentials", fake_aws)

        p = RealMowerProvider(region="eu-west-1", device_id="t",
                              auth_method="oauth", refresh_token="rt-1")
        await p._ensure_session()  # noqa: SLF001
        try:
            await p._login()  # noqa: SLF001
        finally:
            await p.stop()
        assert calls == [("refresh", "rt-1"), ("aws", "id")]

    async def test_rotation_callback_updates_provider_and_calls_back(self):
        """Rotated refresh tokens flow back to the console for storage."""
        seen = []
        p = RealMowerProvider(region="eu-west-1", device_id="t",
                              auth_method="oauth", refresh_token="rt-1")
        p.on_refresh_token = seen.append
        await p._ensure_session()  # noqa: SLF001
        try:
            p._handle_token_rotation("rt-2")  # noqa: SLF001
        finally:
            await p.stop()
        assert p.refresh_token == "rt-2"
        assert seen == ["rt-2"]

    async def test_password_method_still_uses_srp(self, monkeypatch):
        calls = []
        from app.lymow.auth import CognitoAuth

        async def fake_srp(self, email, password):
            calls.append(("srp", email))
            self.id_token = "id"

        async def fake_aws(self):
            calls.append(("aws",))

        monkeypatch.setattr(CognitoAuth, "login_srp", fake_srp)
        monkeypatch.setattr(CognitoAuth, "get_aws_credentials", fake_aws)
        p = RealMowerProvider(region="eu-west-1", device_id="t",
                              email="u@e.com", password="pw")
        await p._ensure_session()  # noqa: SLF001
        try:
            await p._login()  # noqa: SLF001
        finally:
            await p.stop()
        assert calls == [("srp", "u@e.com"), ("aws",)]
