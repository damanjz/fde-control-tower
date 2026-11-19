"""HMAC webhook signatures and admin-key checks.

Signed string: f"{timestamp}." + raw body. Headers: X-Tower-Timestamp (unix seconds),
X-Tower-Signature: sha256=<hex>. The window is symmetric: a sender clock ahead is rejected like a replay.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Tower-Signature"
TIMESTAMP_HEADER = "X-Tower-Timestamp"
SCHEME = "sha256="


class SignatureError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return SCHEME + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify(
    secret: str, body: bytes, signature: str | None, timestamp: str | None, now_ts: float, window_s: int
) -> int:
    """Return the verified timestamp or raise SignatureError(code). Signature is checked before the window."""
    if not secret:
        raise SignatureError("no_secret_configured")
    if not signature or not timestamp:
        raise SignatureError("missing_headers")
    try:
        ts = int(timestamp)
    except ValueError:
        raise SignatureError("bad_timestamp") from None
    if not hmac.compare_digest(sign(secret, ts, body), signature):
        raise SignatureError("bad_signature")
    if abs(now_ts - ts) > window_s:
        raise SignatureError("timestamp_outside_window")
    return ts


def admin_ok(expected: str, provided: str | None) -> bool:
    return bool(expected) and bool(provided) and hmac.compare_digest(expected, provided)
