"""Instagram API with Instagram Login (docs/research/instagram.md).

Outbound: Send API, text at most 1000 UTF-8 bytes per message, 24-hour window (R11).
Inbound: webhook items are stored one per event; echoes of our replies are matched by message
id (and by text as a fallback); any other echo means the studio wrote by hand (R8).
Connection: Business Login (OAuth) with a signed state that names the studio.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from app.brain.ingest import (
    Inbound,
    record_client_message,
    record_deleted,
    record_edit,
    record_outgoing,
)
from app.channels.base import ChannelError
from app.config import Settings
from app.context import AppContext
from app.core.crypto import CryptoError, decrypt, encrypt, sign_state
from app.core.logging import get_logger
from app.core.text import split_message
from app.db.models import ChannelAccount, Client, Conversation, Message

log = get_logger(__name__)

GRAPH = "https://graph.instagram.com"
AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
MAX_BYTES = 1000
# message_echoes needs instagram_business_manage_comments (webhooks fields matrix).
SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_messages",
    "instagram_business_manage_comments",
)
WEBHOOK_FIELDS = "messages,message_echoes"
ATTACHMENT_TYPES = {"image": "photo", "video": "video", "audio": "voice", "file": "file"}
STATE_TTL = 3600


class InstagramError(Exception):
    pass


class InstagramChannel:
    kind = "instagram"
    max_bytes = MAX_BYTES
    max_chars: int | None = None
    window = timedelta(hours=24)

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=15.0)

    def _url(self, path: str) -> str:
        return f"{GRAPH}/{self.settings.instagram_api_version}/{path}"

    async def send_text(self, account: ChannelAccount, recipient_id: str, text: str) -> list[str]:
        try:
            token = decrypt(self.settings, account.credentials or "")
        except CryptoError as exc:
            raise ChannelError(str(exc), permanent=True) from exc
        ids = []
        for part in split_message(text, max_bytes=MAX_BYTES):
            try:
                response = await self.http.post(
                    self._url(f"{account.external_id}/messages"),
                    headers={"Authorization": f"Bearer {token}"},
                    json={"recipient": {"id": recipient_id}, "message": {"text": part}},
                )
            except httpx.HTTPError as exc:
                raise ChannelError(f"instagram send failed: {type(exc).__name__}") from exc
            if response.status_code == 429 or response.status_code >= 500:
                raise ChannelError(f"instagram send returned {response.status_code}")
            if response.status_code != 200:
                # 4xx: closed window, revoked token, blocked user: retrying will not help.
                raise ChannelError(
                    f"instagram send returned {response.status_code}", permanent=True
                )
            ids.append(str(response.json().get("message_id", "")))
        return ids


# --- webhooks ----------------------------------------------------------------------------------


def signature_valid(app_secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: 'sha256=' + HMAC-SHA256 of the raw body with the app secret."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def split_webhook(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """One stored event per messaging item: (dedupe key, {"account_id", "item"})."""
    events = []
    for entry in payload.get("entry", []):
        account_id = str(entry.get("id", ""))
        for item in entry.get("messaging", []):
            canonical = json.dumps(item, sort_keys=True, ensure_ascii=False)
            key = hashlib.sha256(f"{account_id}:{canonical}".encode()).hexdigest()[:40]
            events.append((key, {"account_id": account_id, "item": item}))
    return events


def _inbound_from(item: dict[str, Any], client_id: str) -> Inbound:
    message = item.get("message", {})
    media_type, media = None, None
    for attachment in message.get("attachments", []):
        kind = ATTACHMENT_TYPES.get(attachment.get("type"))
        if kind:
            media_type = kind
            url = attachment.get("payload", {}).get("url")
            media = {"url": url} if url else None
            break
    return Inbound(
        external_user_id=client_id,
        external_message_id=message.get("mid"),
        text=message.get("text"),
        media_type=media_type,
        media=media,
        is_story_reply=bool(message.get("reply_to", {}).get("story")),
    )


async def handle_instagram_event(app: AppContext, payload: dict[str, Any]) -> None:
    item = payload["item"]
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        account = await session.scalar(
            select(ChannelAccount).where(
                ChannelAccount.kind == "instagram",
                ChannelAccount.external_id == payload["account_id"],
            )
        )
        if account is None or account.status != "active":
            log.warning("instagram.unknown_account")
            return
        sender = str(item.get("sender", {}).get("id", ""))
        recipient = str(item.get("recipient", {}).get("id", ""))
        debounce = timedelta(seconds=app.settings.debounce_seconds)
        if "message_edit" in item:
            edit = item["message_edit"]
            await record_edit(session, account, sender, edit.get("mid", ""), edit.get("text"), now)
            return
        message = item.get("message")
        if message is None:
            return  # reactions, reads, postbacks: nothing to answer
        if message.get("is_deleted"):
            await record_deleted(session, account, sender, [message.get("mid", "")])
            return
        if message.get("is_self"):
            # Self messaging (testing with one account): the tester's own messages are the
            # "client"; our replies come back as echoes and are only marked delivered.
            mid = message.get("mid")
            if await _is_own(session, account, recipient, mid):
                await record_outgoing(
                    session, account, recipient, mid, message.get("text"), now, from_bot=True
                )
            else:
                inbound = _inbound_from(item, recipient)
                await record_client_message(session, account, inbound, now, debounce=debounce)
            return
        if message.get("is_echo"):
            # Sent by the studio account: ours (matched) or typed by a human (R8).
            await record_outgoing(
                session, account, recipient, message.get("mid"), message.get("text"), now
            )
            return
        await record_client_message(
            session, account, _inbound_from(item, sender), now, debounce=debounce
        )


async def _is_own(session, account: ChannelAccount, client_id: str, mid: str | None) -> bool:
    if not mid:
        return False
    row = await session.scalar(
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(Client, Client.id == Conversation.client_id)
        .where(
            Client.channel_account_id == account.id,
            Client.external_user_id == client_id,
            Message.external_id == mid,
        )
    )
    return row is not None


# --- Business Login (connecting a studio) ------------------------------------------------------


def authorize_url(settings: Settings, tenant_slug: str) -> str:
    if not settings.meta_app_id or not settings.instagram_redirect_uri:
        raise InstagramError("set META_APP_ID and PUBLIC_BASE_URL")
    state = sign_state(settings, {"tenant": tenant_slug}, STATE_TTL * 24 * 3)
    query = urlencode(
        {
            "client_id": settings.meta_app_id,
            "redirect_uri": settings.instagram_redirect_uri,
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "state": state,
            "enable_fb_login": "false",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


async def connect_account(
    settings: Settings, http: httpx.AsyncClient, code: str
) -> tuple[str, str, str, int]:
    """Code -> short token -> long-lived token -> profile. Returns (ig_id, username, token, ttl)."""
    response = await http.post(
        TOKEN_URL,
        data={
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret.get_secret_value(),
            "grant_type": "authorization_code",
            "redirect_uri": settings.instagram_redirect_uri,
            "code": code,
        },
    )
    if response.status_code != 200:
        raise InstagramError(f"code exchange returned {response.status_code}")
    body = response.json()
    short = (body.get("data") or [body])[0]["access_token"]
    response = await http.get(
        f"{GRAPH}/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.meta_app_secret.get_secret_value(),
            "access_token": short,
        },
    )
    if response.status_code != 200:
        raise InstagramError(f"long-lived exchange returned {response.status_code}")
    long_lived = response.json()
    token, ttl = long_lived["access_token"], int(long_lived.get("expires_in", 60 * 24 * 3600))
    response = await http.get(
        f"{GRAPH}/{settings.instagram_api_version}/me",
        params={"fields": "user_id,username", "access_token": token},
    )
    if response.status_code != 200:
        raise InstagramError(f"profile request returned {response.status_code}")
    profile = response.json()
    ig_id = str(profile["user_id"])
    response = await http.post(
        f"{GRAPH}/{settings.instagram_api_version}/{ig_id}/subscribed_apps",
        params={"subscribed_fields": WEBHOOK_FIELDS, "access_token": token},
    )
    if response.status_code != 200 or not response.json().get("success"):
        raise InstagramError(f"webhook subscription returned {response.status_code}")
    return ig_id, profile.get("username", ""), token, ttl


async def save_account(
    session,
    settings: Settings,
    now: datetime,
    tenant_id: int,
    ig_id: str,
    username: str,
    token: str,
    ttl: int,
) -> ChannelAccount:
    """Store or re-bind the Instagram account with an encrypted long-lived token."""
    account = await session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.kind == "instagram", ChannelAccount.external_id == ig_id
        )
    )
    if account is None:
        account = ChannelAccount(tenant_id=tenant_id, kind="instagram", external_id=ig_id, meta={})
        session.add(account)
    account.tenant_id = tenant_id
    account.credentials = encrypt(settings, token)
    account.token_expires_at = now + timedelta(seconds=ttl)
    account.status = "active"
    account.meta = {**(account.meta or {}), "username": username}
    await session.flush()
    return account


async def refresh_tokens(app: AppContext, http: httpx.AsyncClient | None = None) -> None:
    """Long-lived tokens live 60 days; refresh 10 days before expiry (token must be >24 h old)."""
    http = (
        http
        or getattr(app.channels.get("instagram"), "http", None)
        or httpx.AsyncClient(timeout=15.0)
    )
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        accounts = list(
            await session.scalars(
                select(ChannelAccount).where(
                    ChannelAccount.kind == "instagram",
                    ChannelAccount.status == "active",
                    ChannelAccount.token_expires_at < now + timedelta(days=10),
                )
            )
        )
        for account in accounts:
            try:
                token = decrypt(app.settings, account.credentials or "")
                response = await http.get(
                    f"{GRAPH}/refresh_access_token",
                    params={"grant_type": "ig_refresh_token", "access_token": token},
                )
                if response.status_code != 200:
                    raise InstagramError(f"refresh returned {response.status_code}")
                body = response.json()
                account.credentials = encrypt(app.settings, body["access_token"])
                account.token_expires_at = now + timedelta(
                    seconds=int(body.get("expires_in", 5_184_000))
                )
            except (InstagramError, CryptoError, httpx.HTTPError, KeyError) as exc:
                log.warning("instagram.refresh_failed", account_id=account.id)
                await app.alerts.send(
                    f"instagram_token:{account.id}",
                    f"Не вдалося оновити токен Instagram (акаунт #{account.id}): "
                    f"{type(exc).__name__}. Студії треба перепідключити Instagram, інакше бот "
                    "замовкне після закінчення токена.",
                )
