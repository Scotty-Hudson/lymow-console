# Derived from Lymow-One-MQTT (https://github.com/Mortimer452/Lymow-One-MQTT),
# Copyright (c) 2026 Aaron Gibbs, MIT License — see LICENSE-lymow-one-mqtt.
"""Cognito authentication — SRP, OAuth (federated), Identity Pool credential exchange.

Two flows:
- Native (USER_SRP_AUTH): pycognito handles SRP transparently.
- Federated (Google/Apple): user pastes the auth code from a failed
  myapp://callback redirect; we exchange via /oauth2/token.

Both produce a Cognito id_token; that's exchanged at cognito-identity for
temporary AWS credentials used in SigV4 signing of MQTT WSS and S3 calls.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import aiohttp

try:
    from pycognito import Cognito as _PyCognito
    _HAS_PYCOGNITO = True
except ImportError:
    _HAS_PYCOGNITO = False

from .const import COGNITO_CONFIG

_LOGGER = logging.getLogger(__name__)


# ── Federated (Google/Apple) sign-in helpers ─────────────────────
# Ported from Lymow-One-MQTT's config_flow.py (same MIT license).

OAUTH_REDIRECT_URI = "myapp://callback/"  # only redirect Cognito accepts


def build_oauth_url(region: str) -> str:
    """Cognito hosted-UI link for federated login.

    The federated client only accepts myapp://callback/ as redirect_uri
    (verified empirically upstream); signing in there fails the final
    redirect, and the user copies the code from that failed URL.
    """
    cfg = COGNITO_CONFIG[region]
    qs = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "response_type": "code",
        "scope": "openid aws.cognito.signin.user.admin",
        "redirect_uri": OAUTH_REDIRECT_URI,
    })
    return f"https://{cfg['hosted_ui_domain']}/login?{qs}"


def extract_oauth_code(pasted: str) -> str | None:
    """Extract the auth code from a full myapp://callback URL or a bare code."""
    pasted = pasted.strip()
    if pasted.startswith("myapp://"):
        try:
            parsed = urllib.parse.urlparse(pasted)
            qs = urllib.parse.parse_qs(parsed.query)
            return qs.get("code", [None])[0]
        except Exception:
            return None
    if "code=" in pasted:
        try:
            qs = urllib.parse.parse_qs(pasted.split("?", 1)[1] if "?" in pasted else pasted)
            return qs.get("code", [None])[0]
        except Exception:
            return None
    return pasted if pasted else None


class LymowAuthError(Exception):
    """Authentication error."""


def _decode_jwt_exp(token: str) -> datetime | None:
    """Extract the `exp` claim from a JWT id_token. Returns UTC datetime or None.

    No signature verification — we trust the token shape since it came from
    a successful Cognito flow. The exp is used only for proactive refresh
    timing.
    """
    try:
        _, payload_b64, _ = token.split(".")
        # Pad base64 if needed
        padding = "=" * ((4 - len(payload_b64) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        exp = payload.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(exp, UTC)
    except Exception:
        return None


class CognitoAuth:
    """Holds Cognito tokens + AWS Identity Pool credentials. Refreshes proactively."""

    def __init__(self, region: str, session: aiohttp.ClientSession) -> None:
        if not _HAS_PYCOGNITO:
            raise LymowAuthError("pycognito is required: pip install pycognito")
        self._region = region
        self._session = session
        self._cfg = COGNITO_CONFIG[region]

        self.id_token: str | None = None
        self.access_token: str | None = None
        self.refresh_token: str | None = None

        self.identity_id: str | None = None
        self.access_key_id: str | None = None
        self.secret_access_key: str | None = None
        self.session_token: str | None = None
        self._creds_expiry: datetime | None = None

        # For native re-login after refresh failure
        self._email: str | None = None
        self._password: str | None = None

        # For federated reauth detection: did we use OAuth?
        self._is_federated: bool = False

        # Called whenever the refresh token changes (initial exchange or
        # rotation on refresh) so the console can persist it. The refresh
        # token is the durable credential for federated (Google) accounts.
        self.on_token_rotated: Callable[[str], None] = lambda token: None

    # ── Native SRP login ───────────────────────────────────────

    async def login_srp(self, email: str, password: str) -> None:
        """Native USER_SRP_AUTH. Blocking pycognito calls run in executor."""
        _LOGGER.debug("SRP login: %s @ %s", email, self._region)

        def _do_srp() -> tuple[str, str, str]:
            u = _PyCognito(
                user_pool_id=self._cfg["user_pool_id"],
                client_id=self._cfg["client_id"],
                user_pool_region=self._region,
                username=email,
            )
            u.authenticate(password=password)
            return u.id_token, u.access_token, u.refresh_token

        try:
            loop = asyncio.get_running_loop()
            id_t, acc_t, ref_t = await loop.run_in_executor(None, _do_srp)
        except Exception as e:
            raise LymowAuthError(f"SRP login failed: {e}") from e

        self.id_token = id_t
        self.access_token = acc_t
        self.refresh_token = ref_t
        self._email = email
        self._password = password
        self._is_federated = False

    # ── Federated OAuth code exchange ───────────────────────────

    async def exchange_oauth_code(self, code: str) -> None:
        """Exchange a federated OAuth code for tokens.

        The user obtained the code by completing the OAuth flow in a browser
        and copying it from the failed myapp://callback redirect URL.
        """
        token_url = f"https://{self._cfg['hosted_ui_domain']}/oauth2/token"
        data = {
            "grant_type": "authorization_code",
            "client_id": self._cfg["client_id"],
            "code": code,
            "redirect_uri": "myapp://callback/",
        }
        async with self._session.post(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise LymowAuthError(f"OAuth exchange failed: {body}")
        # Cognito sometimes returns 200 with an error payload — guard against it
        if "id_token" not in body or "access_token" not in body:
            raise LymowAuthError(f"OAuth exchange returned no tokens: {body}")

        self.id_token = body["id_token"]
        self.access_token = body["access_token"]
        self.refresh_token = body.get("refresh_token") or self.refresh_token
        self._is_federated = True
        if self.refresh_token:
            self.on_token_rotated(self.refresh_token)

    def restore_refresh_token(self, token: str) -> None:
        """Re-arm a previously persisted federated session (Google/Apple).

        The refresh token is the durable credential for federated accounts;
        set it and refresh_tokens() re-establishes the token pair.
        """
        if not token:
            raise LymowAuthError("empty refresh token")
        self.refresh_token = token
        self._is_federated = True

    # ── Token refresh ───────────────────────────────────────────

    async def refresh_tokens(self) -> None:
        """Refresh tokens using the stored refresh_token.

        Uses /oauth2/token for both native and federated paths; pycognito's
        renew_access_token is SRP-flavored which works for native but not
        federated. Going through /oauth2/token works for both.
        """
        if not self.refresh_token:
            raise LymowAuthError("No refresh_token — re-login required")

        token_url = f"https://{self._cfg['hosted_ui_domain']}/oauth2/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": self._cfg["client_id"],
            "refresh_token": self.refresh_token,
        }
        async with self._session.post(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise LymowAuthError(f"Refresh failed: {body}")
        # Cognito sometimes returns 200 with an error payload — guard against it
        if "id_token" not in body or "access_token" not in body:
            raise LymowAuthError(f"Refresh returned no tokens: {body}")

        self.id_token = body["id_token"]
        self.access_token = body["access_token"]
        # Cognito generally doesn't rotate refresh_token on refresh
        if "refresh_token" in body:
            self.refresh_token = body["refresh_token"]
            self.on_token_rotated(self.refresh_token)

    # ── Identity Pool → AWS credentials ─────────────────────────

    async def get_aws_credentials(self) -> None:
        """Exchange id_token for temporary AWS credentials."""
        if not self.id_token:
            raise LymowAuthError("No id_token — call login first")

        logins = {
            f"cognito-idp.{self._region}.amazonaws.com/{self._cfg['user_pool_id']}": self.id_token
        }
        id_url = f"https://cognito-identity.{self._region}.amazonaws.com/"
        common_hdrs = {
            "Content-Type": "application/x-amz-json-1.1",
        }

        async with self._session.post(
            id_url,
            json={"IdentityPoolId": self._cfg["identity_pool_id"], "Logins": logins},
            headers={**common_hdrs, "X-Amz-Target": "AWSCognitoIdentityService.GetId"},
        ) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise LymowAuthError(f"GetId failed: {data}")
            identity_id = data["IdentityId"]
            self.identity_id = identity_id

        async with self._session.post(
            id_url,
            json={"IdentityId": identity_id, "Logins": logins},
            headers={**common_hdrs, "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity"},
        ) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise LymowAuthError(f"GetCredentialsForIdentity failed: {data}")
            c = data["Credentials"]

        self.access_key_id = c["AccessKeyId"]
        self.secret_access_key = c["SecretKey"]
        self.session_token = c["SessionToken"]
        exp = c["Expiration"]
        self._creds_expiry = (
            datetime.fromtimestamp(exp, UTC) if isinstance(exp, (int, float)) else None
        )
        _LOGGER.debug("AWS credentials OK, expire: %s", self._creds_expiry)

    # ── Lifecycle ───────────────────────────────────────────────

    def _id_token_expiring(self) -> bool:
        if not self.id_token:
            return True
        exp = _decode_jwt_exp(self.id_token)
        if exp is None:
            return True
        return datetime.now(UTC) >= (exp - timedelta(minutes=5))

    def _creds_expiring(self) -> bool:
        if not self._creds_expiry:
            return True
        return datetime.now(UTC) >= (self._creds_expiry - timedelta(minutes=10))

    async def ensure_valid(self) -> None:
        """Refresh tokens/credentials proactively before they expire."""
        if self._id_token_expiring():
            try:
                await self.refresh_tokens()
            except LymowAuthError:
                # Refresh failed — fall back to native re-login if possible
                if not self._is_federated and self._email and self._password:
                    await self.login_srp(self._email, self._password)
                else:
                    # Federated: caller must trigger reauth flow
                    raise
        if self._creds_expiring():
            await self.get_aws_credentials()

    # ── Persistence ─────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id_token": self.id_token,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "_email": self._email,
            "_is_federated": self._is_federated,
            "identity_id": self.identity_id,
        }

    def from_dict(self, d: dict) -> None:
        self.id_token = d.get("id_token")
        self.access_token = d.get("access_token")
        self.refresh_token = d.get("refresh_token")
        self._email = d.get("_email")
        self._is_federated = d.get("_is_federated", False)
        self.identity_id = d.get("identity_id")
