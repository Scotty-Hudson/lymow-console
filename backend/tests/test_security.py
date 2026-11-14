"""Tests for app/security.py — password hashing, sessions, auth deps."""
from __future__ import annotations

import pytest

from app import security
from app.security import SessionSigner, hash_password, verify_password


class TestPasswordHashing:
    def test_roundtrip(self):
        stored = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", stored)

    def test_wrong_password(self):
        stored = hash_password("right")
        assert not verify_password("wrong", stored)

    def test_plaintext_never_stored(self):
        stored = hash_password("s3cr3t-passphrase")
        assert "s3cr3t" not in stored
        assert stored.startswith("pbkdf2_sha256$")

    def test_salted_per_call(self):
        a, b = hash_password("same"), hash_password("same")
        assert a != b
        assert verify_password("same", a) and verify_password("same", b)

    @pytest.mark.parametrize("junk", ["", "garbage", "pbkdf2_md5$1$ab$cd", "a$b"])
    def test_malformed_hash_is_rejected_not_crash(self, junk):
        assert verify_password("x", junk) is False


class TestSessionSigner:
    def test_create_verify_roundtrip(self):
        signer = SessionSigner("secret-A", max_age_s=3600)
        token = signer.create()
        assert signer.verify(token) == "console-user"

    def test_wrong_secret_rejects(self):
        token = SessionSigner("secret-A", 3600).create()
        assert SessionSigner("secret-B", 3600).verify(token) is None

    def test_tampered_token_rejects(self):
        signer = SessionSigner("secret-A", 3600)
        token = signer.create()
        assert signer.verify(token[:-2] + "xx") is None

    def test_expired_token_rejects(self):
        # max_age_s=-1 forces any token to be "expired" deterministically.
        signer = SessionSigner("secret-A", max_age_s=-1)
        assert signer.verify(signer.create()) is None

    def test_empty_token(self):
        assert SessionSigner("s", 60).verify("") is None


def _fake_app() -> object:
    from starlette.applications import Starlette

    app = Starlette()
    app.state.session_signer = SessionSigner("kitten", max_age_s=3600)
    return app


def _http_request(headers: list[tuple[bytes, bytes]]):
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": "/", "headers": headers,
        "query_string": b"", "app": _fake_app(),
    }
    return Request(scope)


def _ws(headers: list[tuple[bytes, bytes]], query: bytes = b""):
    from starlette.websockets import WebSocket

    scope = {
        "type": "websocket", "headers": headers, "query_string": query,
        "subprotocols": [], "cookies": None, "app": _fake_app(),
    }
    async def _noop():  # pragma: no cover - never called by these tests
        pass

    return WebSocket(scope, receive=_noop, send=_noop)


class TestAuthDependencies:
    def test_http_requires_session(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            security.get_current_session(_http_request([]))
        assert exc.value.status_code == 401

    def test_http_accepts_valid_cookie(self):
        token = _fake_app().state.session_signer.create()  # type: ignore[attr-defined]
        req = _http_request([(b"cookie", f"{security.SESSION_COOKIE_NAME}={token}".encode())])
        assert security.get_current_session(req) == token

    def test_http_accepts_bearer(self):
        token = _fake_app().state.session_signer.create()  # type: ignore[attr-defined]
        req = _http_request([(b"authorization", f"Bearer {token}".encode())])
        assert security.get_current_session(req) == token

    def test_http_rejects_bogus_cookie(self):
        from fastapi import HTTPException

        req = _http_request([(b"cookie", b"lymow_session=not-a-token")])
        with pytest.raises(HTTPException):
            security.get_current_session(req)

    def test_ws_accepts_cookie(self):
        token = _fake_app().state.session_signer.create()  # type: ignore[attr-defined]
        ws = _ws([(b"cookie", f"{security.SESSION_COOKIE_NAME}={token}".encode())])
        assert security.get_websocket_session(ws) == token

    def test_ws_accepts_query_token(self):
        token = _fake_app().state.session_signer.create()  # type: ignore[attr-defined]
        ws = _ws([], query=f"token={token}".encode())
        assert security.get_websocket_session(ws) == token

    def test_ws_rejects_anonymous(self):
        assert security.get_websocket_session(_ws([])) is None
