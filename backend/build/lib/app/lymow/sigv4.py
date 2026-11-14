# Derived from Lymow-One-MQTT (https://github.com/Mortimer452/Lymow-One-MQTT),
# Copyright (c) 2026 Aaron Gibbs, MIT License — see LICENSE-lymow-one-mqtt.
"""AWS SigV4 query-string signing for the MQTT WSS connect (arch.md §4b).

Lymow REST uses Cognito access tokens directly (no SigV4); the IoT WSS
connection is authorized with a presigned /mqtt path — presigned_ws_path.
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from datetime import UTC, datetime

_IOT_SERVICE = "iotdevicegateway"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key for (datestamp, region, service)."""
    k = _sign(("AWS4" + secret).encode(), datestamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def presigned_ws_path(
    host: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str | None,
    now: datetime | None = None,
    expires_seconds: int = 86400,
) -> str:
    """Query-string SigV4 for AWS IoT MQTT-over-WSS connect.

    Returns the path component (`/mqtt?...`) for paho's ws_set_options().
    The session_token is appended UNSIGNED as X-Amz-Security-Token —
    this is the AWS IoT idiom.

    Ported from harness.py:presigned_ws_path. arch.md §4b.
    """
    if now is None:
        now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/{_IOT_SERVICE}/aws4_request"

    qs_pairs = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires_seconds),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(qs_pairs.items())
    )

    canonical_req = "\n".join([
        "GET", "/mqtt", canonical_qs,
        f"host:{host}\n", "host",
        hashlib.sha256(b"").hexdigest(),
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_req.encode()).hexdigest(),
    ])
    sig = hmac.new(
        signing_key(secret_key, datestamp, region, _IOT_SERVICE),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    final_qs = canonical_qs + f"&X-Amz-Signature={sig}"
    if session_token:
        final_qs += "&X-Amz-Security-Token=" + urllib.parse.quote(session_token, safe="")
    return f"/mqtt?{final_qs}"
