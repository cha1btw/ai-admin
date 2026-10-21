"""Founders' login to the admin panel without passwords.

`/admin` in Owner Bot (or `python -m app.cli admin link`) gives a one-time link valid for
10 minutes; opening it sets a signed session cookie. Only ADMIN_TELEGRAM_IDS get in.
Changing requests must carry the X-Admin-Request header: a browser cannot add it to a
cross-site request without CORS, so forged form posts fail (CSRF).
"""

import secrets

from fastapi import HTTPException, Request
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.core.crypto import CryptoError, sign_state, verify_state
from app.db.models import JobRun

COOKIE = "ai_admin_session"
LOGIN_TTL = 600
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def login_link(settings: Settings, telegram_id: int, base_url: str | None = None) -> str:
    token = sign_state(
        settings,
        {"admin": telegram_id, "purpose": "login", "nonce": secrets.token_urlsafe(12)},
        LOGIN_TTL,
    )
    base = (base_url or settings.public_base_url or "http://localhost:8000").rstrip("/")
    return f"{base}/admin/login?token={token}"


async def consume_login(request: Request, token: str) -> int:
    """Check a login token once; returns the founder's Telegram id."""
    settings: Settings = request.app.state.settings
    try:
        body = verify_state(settings, token)
    except CryptoError as exc:
        raise HTTPException(status_code=401, detail="login link expired or invalid") from exc
    telegram_id = body.get("admin")
    if body.get("purpose") != "login" or telegram_id not in settings.admin_telegram_ids:
        raise HTTPException(status_code=403)
    async with request.app.state.session_factory() as session, session.begin():
        claimed = await session.execute(
            insert(JobRun)
            .values(name="admin_login", scope="", run_key=str(body.get("nonce", ""))[:64])
            .on_conflict_do_nothing(index_elements=["name", "scope", "run_key"])
        )
        if not claimed.rowcount:
            raise HTTPException(status_code=401, detail="login link already used")
    return telegram_id


def session_cookie(settings: Settings, telegram_id: int) -> str:
    return sign_state(
        settings, {"admin": telegram_id, "purpose": "session"}, settings.admin_session_hours * 3600
    )


def require_admin(request: Request) -> int:
    """FastAPI dependency: the founder's Telegram id or 401."""
    settings: Settings = request.app.state.settings
    cookie = request.cookies.get(COOKIE)
    if not cookie:
        raise HTTPException(status_code=401, detail="not logged in")
    try:
        body = verify_state(settings, cookie)
    except CryptoError as exc:
        raise HTTPException(status_code=401, detail="session expired") from exc
    telegram_id = body.get("admin")
    if body.get("purpose") != "session" or telegram_id not in settings.admin_telegram_ids:
        raise HTTPException(status_code=401)
    if request.method not in SAFE_METHODS and request.headers.get("X-Admin-Request") != "1":
        raise HTTPException(status_code=403, detail="missing X-Admin-Request header")
    return telegram_id
