"""Access token for a Google service account (OAuth 2.0 JWT bearer flow), without extra SDKs.

https://developers.google.com/identity/protocols/oauth2/service-account
The JWT is signed with RS256; the assertion lives at most one hour; the token endpoint returns
access_token and expires_in.
"""

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.scheduling.base import CalendarError

TOKEN_URI = "https://oauth2.googleapis.com/token"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass(frozen=True)
class ServiceAccount:
    client_email: str
    private_key_pem: str
    token_uri: str = TOKEN_URI

    @classmethod
    def from_file(cls, path: str | Path) -> "ServiceAccount":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["client_email"], data["private_key"], data.get("token_uri", TOKEN_URI))


def make_assertion(account: ServiceAccount, scope: str, now: int) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": account.client_email,
        "scope": scope,
        "aud": account.token_uri,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    key = serialization.load_pem_private_key(account.private_key_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CalendarError("service account key is not an RSA key")
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


class TokenSource:
    """Caches the access token and renews it a minute before it expires."""

    def __init__(
        self,
        account: ServiceAccount,
        http: httpx.AsyncClient,
        *,
        scope: str = CALENDAR_SCOPE,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.account = account
        self._http = http
        self._scope = scope
        self._now = now
        self._token: str | None = None
        self._expires_at = 0

    async def token(self) -> str:
        now = int(self._now())
        if self._token is not None and now < self._expires_at - 60:
            return self._token
        try:
            response = await self._http.post(
                self.account.token_uri,
                data={
                    "grant_type": JWT_GRANT,
                    "assertion": make_assertion(self.account, self._scope, now),
                },
            )
        except httpx.HTTPError as exc:
            raise CalendarError(f"token request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise CalendarError(f"token request returned {response.status_code}")
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = now + int(body.get("expires_in", 3600))
        return self._token
