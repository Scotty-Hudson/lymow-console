"""Console authentication: local password + signed session cookie.

Design:
  - Dashboard password stored ONLY as a PBKDF2-HMAC-SHA256 hash in the
    SQLite settings table (never the plaintext, never logged).
  - Sessions are stateless signed cookies (itsdangerous
    URLSafeTimedSerializer) — HttpOnly, SameSite=Lax. Browsers send
    the same cookie on the WebSocket handshake, so one mechanism covers
    HTTP and WS auth.
  - Lymow CLOUD credentials are a separate concern: they live in the
    DB (secret-masked) and are used server-side only; they never reach
    the browser and are never written to logs.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from fastapi import HTTPException, Request, WebSocket, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_LOGGER = logging.getLogger(__name__)

PASSWORD_ITERATIONS = 600_000
SESSION_COOKIE_NAME = "lymow_session"
SESSION_SALT = "lymow-console-session-v1"

# ── password hashing ────────────────────────────────────────────


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex),
            int(iterations),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# ── session tokens ──────────────────────────────────────────────


class SessionSigner:
    def __init__(self, secret: str, max_age_s: int) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt=SESSION_SALT)
        self.max_age_s = max_age_s

    def create(self, subject: str = "console-user") -> str:
        return self._serializer.dumps({"sub": subject})

    def verify(self, token: str) -> str | None:
        """Return the session subject, or None when invalid/expired."""
        try:
            data = self._serializer.loads(token, max_age=self.max_age_s)
        except SignatureExpired:
            return None
        except BadSignature:
            _LOGGER.warning("rejected bad session signature")
            return None
        if isinstance(data, dict):
            return data.get("sub")
        return None


# ── FastAPI auth dependencies ───────────────────────────────────


def _signer(request_or_ws: Request | WebSocket) -> SessionSigner:
    return request_or_ws.app.state.session_signer


def get_current_session(request: Request) -> str:
    """HTTP dependency: valid session cookie required, else 401."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth.removeprefix("Bearer ").strip()
    if not token or _signer(request).verify(token) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return token


def get_websocket_session(websocket: WebSocket) -> str | None:
    """WS handshake auth: cookie or ?token= query param (EventSource-style
    clients can't set headers). Returns token or None (caller closes)."""
    token = websocket.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        return None
    return token if _signer(websocket).verify(token) is not None else None
