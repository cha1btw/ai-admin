"""Encryption of stored channel tokens (Fernet) and signed short-lived values (OAuth state)."""

import base64
import hashlib
import hmac
import json
import time

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings


class CryptoError(Exception):
    pass


def _fernet(settings: Settings) -> Fernet:
    if settings.encryption_key is None:
        raise CryptoError("ENCRYPTION_KEY is not set (see .env.example)")
    return Fernet(settings.encryption_key.get_secret_value().encode())


def encrypt(settings: Settings, value: str) -> str:
    return _fernet(settings).encrypt(value.encode()).decode()


def decrypt(settings: Settings, token: str) -> str:
    try:
        return _fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CryptoError("cannot decrypt a stored token (wrong ENCRYPTION_KEY?)") from exc


def _signing_key(settings: Settings) -> bytes:
    if settings.encryption_key is None:
        raise CryptoError("ENCRYPTION_KEY is not set")
    return hashlib.sha256(b"state:" + settings.encryption_key.get_secret_value().encode()).digest()


def sign_state(
    settings: Settings, payload: dict, ttl_seconds: int, *, now: float | None = None
) -> str:
    body = dict(payload, exp=int((now or time.time()) + ttl_seconds))
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=")
    mac = hmac.new(_signing_key(settings), raw, hashlib.sha256).digest()
    return f"{raw.decode()}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


def verify_state(settings: Settings, state: str, *, now: float | None = None) -> dict:
    try:
        raw, mac_text = state.split(".", 1)
        expected = hmac.new(_signing_key(settings), raw.encode(), hashlib.sha256).digest()
        given = base64.urlsafe_b64decode(mac_text + "=" * (-len(mac_text) % 4))
        if not hmac.compare_digest(expected, given):
            raise CryptoError("bad state signature")
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise CryptoError("malformed state") from exc
    if body.get("exp", 0) < (now or time.time()):
        raise CryptoError("state expired")
    return body
