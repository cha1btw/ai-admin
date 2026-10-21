"""Instagram channel against fake Meta endpoints (httpx.MockTransport)."""

import base64
import dataclasses
import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.main import create_app
from app.brain.orchestrator import process_conversation
from app.channels.base import ChannelError
from app.channels.instagram import (
    InstagramChannel,
    authorize_url,
    handle_instagram_event,
    refresh_tokens,
    save_account,
    signature_valid,
    split_webhook,
)
from app.config import Settings
from app.core.crypto import decrypt, sign_state
from app.core.text import utf8_len
from app.db.models import ChannelAccount, Conversation, Message
from app.llm.fake import say
from tests.integration.conftest import make_studio

IG_ID = "17841400000000001"
CLIENT = "5550001"
SECRET = "app-secret"
KEY = Fernet.generate_key().decode()


def ig_settings(**extra) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://unused",
        meta_app_id="123",
        meta_app_secret=SECRET,
        meta_verify_token="verify-me",
        encryption_key=KEY,
        public_base_url="https://api.example.com",
        **extra,
    )


class FakeMeta:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail_send: int | None = None
        self.refresh_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "api.instagram.com":
            return httpx.Response(
                200, json={"data": [{"access_token": "short", "user_id": "1", "permissions": "x"}]}
            )
        if path == "/access_token":
            return httpx.Response(
                200, json={"access_token": "long", "token_type": "bearer", "expires_in": 5183944}
            )
        if path.endswith("/me"):
            return httpx.Response(200, json={"user_id": IG_ID, "username": "demo.nails"})
        if path.endswith("/subscribed_apps"):
            assert request.url.params["subscribed_fields"] == "messages,message_echoes"
            return httpx.Response(200, json={"success": True})
        if path == "/refresh_access_token":
            if not self.refresh_ok:
                return httpx.Response(400, json={})
            return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 5184000})
        if path.endswith("/messages"):
            if self.fail_send:
                return httpx.Response(self.fail_send, json={})
            assert request.headers["Authorization"] == "Bearer long"
            body = json.loads(request.content)
            self.sent.append(body)
            return httpx.Response(
                200,
                json={
                    "recipient_id": body["recipient"]["id"],
                    "message_id": f"mid.out.{len(self.sent)}",
                },
            )
        return httpx.Response(404, json={})


@pytest.fixture
def meta() -> FakeMeta:
    return FakeMeta()


@pytest.fixture
def ig_app(app, meta):
    settings = ig_settings(alert_chat_id=999)
    http = httpx.AsyncClient(transport=httpx.MockTransport(meta))
    channels = {**app.channels, "instagram": InstagramChannel(settings, http)}
    return dataclasses.replace(app, settings=settings, channels=channels)


async def connected_studio(ig_app):
    studio = await make_studio(ig_app)
    async with ig_app.session_factory() as session, session.begin():
        await save_account(
            session,
            ig_app.settings,
            ig_app.clock.now(),
            studio.tenant_id,
            IG_ID,
            "demo.nails",
            "long",
            5184000,
        )
    return studio


def item(text=None, *, mid="mid.in.1", echo=False, sender=CLIENT, **message_extra) -> dict:
    message = {"mid": mid, **({"text": text} if text else {}), **message_extra}
    if echo:
        message["is_echo"] = True
        return {
            "sender": {"id": IG_ID},
            "recipient": {"id": sender},
            "timestamp": 1,
            "message": message,
        }
    return {
        "sender": {"id": sender},
        "recipient": {"id": IG_ID},
        "timestamp": 1,
        "message": message,
    }


async def deliver_item(ig_app, payload_item: dict) -> None:
    await handle_instagram_event(ig_app, {"account_id": IG_ID, "item": payload_item})


async def conversation(ig_app) -> Conversation:
    async with ig_app.session_factory() as session:
        return (await session.scalars(select(Conversation))).one()


def test_signature() -> None:
    body = b'{"object":"instagram"}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert signature_valid(SECRET, body, good)
    assert not signature_valid(SECRET, body + b" ", good)
    assert not signature_valid(SECRET, body, good.removeprefix("sha256="))
    assert not signature_valid(SECRET, body, None)


def test_split_webhook_dedupes_by_content() -> None:
    payload = {
        "object": "instagram",
        "entry": [{"id": IG_ID, "time": 1, "messaging": [item("a"), item("b", mid="m2")]}],
    }
    events = split_webhook(payload)
    assert len(events) == 2 and events[0][1]["account_id"] == IG_ID
    assert split_webhook(payload)[0][0] == events[0][0]  # same item -> same key on retry


def test_webhook_endpoints(database_url: str) -> None:
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        meta_app_secret=SECRET,
        meta_verify_token="verify-me",
    )
    with TestClient(create_app(settings)) as client:
        ok = client.get(
            "/webhooks/instagram",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "42",
            },
        )
        assert ok.status_code == 200 and ok.text == "42"
        bad = client.get(
            "/webhooks/instagram",
            params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"},
        )
        assert bad.status_code == 403
        body = json.dumps(
            {"object": "instagram", "entry": [{"id": IG_ID, "messaging": [item("Привіт")]}]}
        ).encode()
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers = {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}
        assert client.post("/webhooks/instagram", content=body).status_code == 403
        for _ in range(2):  # Meta retries: stored once
            assert (
                client.post("/webhooks/instagram", content=body, headers=headers).status_code == 200
            )
        assert client.get("/privacy/data-deletion").status_code == 200


async def test_client_message_and_reply_split_by_bytes(ig_app, meta, llm) -> None:
    await connected_studio(ig_app)
    await deliver_item(ig_app, item("Скільки коштує манікюр?"))
    long_reply = "Манікюр з гель-лаком коштує 900 грн. " * 30
    llm.add(say(long_reply))
    plan = await process_conversation(ig_app, (await conversation(ig_app)).id)
    assert plan.status == "replied"
    assert len(meta.sent) >= 2
    assert all(utf8_len(m["message"]["text"]) <= 1000 for m in meta.sent)
    assert all(m["recipient"]["id"] == CLIENT for m in meta.sent)
    async with ig_app.session_factory() as session:
        bot = await session.scalar(select(Message).where(Message.role == "bot"))
    assert bot.external_id == "mid.out.1" and bot.delivery_status == "sent"


async def test_own_echo_vs_human_echo(ig_app, meta, llm) -> None:
    await connected_studio(ig_app)
    await deliver_item(ig_app, item("Працюєте завтра?"))
    llm.add(say("Так, працюємо з 10:00 до 20:00."))
    await process_conversation(ig_app, (await conversation(ig_app)).id)
    sent_text = meta.sent[0]["message"]["text"]
    await deliver_item(ig_app, item(sent_text, mid="mid.out.1", echo=True))
    assert (await conversation(ig_app)).state == "BOT_ACTIVE"
    await deliver_item(ig_app, item("Це Олена, підкажу вам сама", mid="mid.human.1", echo=True))
    assert (await conversation(ig_app)).state == "HUMAN_ACTIVE"


async def test_edits_deletes_media_story_and_reactions(ig_app) -> None:
    await connected_studio(ig_app)
    await deliver_item(ig_app, item("На завтра", mid="m1"))
    await deliver_item(
        ig_app,
        {
            "sender": {"id": CLIENT},
            "recipient": {"id": IG_ID},
            "message_edit": {"mid": "m1", "text": "На післязавтра", "num_edit": 1},
        },
    )
    await deliver_item(
        ig_app,
        item(
            None,
            mid="m2",
            attachments=[{"type": "image", "payload": {"url": "https://cdn.example/x.jpg"}}],
        ),
    )
    await deliver_item(
        ig_app,
        item(
            "Скільки коштує?",
            mid="m3",
            reply_to={"story": {"id": "s1", "url": "https://cdn.example/s"}},
        ),
    )
    await deliver_item(
        ig_app,
        {
            "sender": {"id": CLIENT},
            "recipient": {"id": IG_ID},
            "reaction": {"mid": "m3", "action": "react", "emoji": "❤️"},
        },
    )
    await deliver_item(ig_app, item(None, mid="m4", is_deleted=True))
    async with ig_app.session_factory() as session:
        rows = {m.external_id: m for m in await session.scalars(select(Message))}
    assert rows["m1"].text == "На післязавтра"
    assert rows["m2"].media_type == "photo" and rows["m2"].media == {
        "url": "https://cdn.example/x.jpg"
    }
    assert rows["m3"].is_story_reply
    assert len(rows) == 3  # reaction stored nothing; m4 was never received, nothing to delete


async def test_send_errors(ig_app, meta) -> None:
    await connected_studio(ig_app)
    async with ig_app.session_factory() as session:
        account = (
            await session.scalars(select(ChannelAccount).where(ChannelAccount.kind == "instagram"))
        ).one()
    channel = ig_app.channels["instagram"]
    meta.fail_send = 400
    with pytest.raises(ChannelError) as permanent:
        await channel.send_text(account, CLIENT, "Привіт")
    assert permanent.value.permanent
    meta.fail_send = 503
    with pytest.raises(ChannelError) as temporary:
        await channel.send_text(account, CLIENT, "Привіт")
    assert not temporary.value.permanent


def test_authorize_url_and_oauth_callback(database_url: str, meta) -> None:
    settings = ig_settings().model_copy(update={"database_url": database_url})
    link = urlparse(authorize_url(settings, "demo_studio"))
    query = parse_qs(link.query)
    assert link.netloc == "www.instagram.com" and query["response_type"] == ["code"]
    assert "instagram_business_manage_messages" in query["scope"][0]
    assert query["redirect_uri"] == ["https://api.example.com/oauth/instagram/callback"]

    with TestClient(create_app(settings)) as client:
        client.app.state.instagram_http = httpx.AsyncClient(transport=httpx.MockTransport(meta))
        import asyncio

        async def seed() -> None:
            from app.db.session import make_engine, make_session_factory
            from app.tenants.loader import load_tenant
            from tests.helpers import demo_config

            engine = make_engine(database_url, null_pool=True)
            async with make_session_factory(engine)() as session, session.begin():
                await load_tenant(session, demo_config())
            await engine.dispose()

        asyncio.run(seed())
        tampered = client.get("/oauth/instagram/callback", params={"code": "c", "state": "x.y"})
        assert tampered.status_code == 400
        response = client.get(
            "/oauth/instagram/callback", params={"code": "c", "state": query["state"][0]}
        )
        assert response.status_code == 200 and "@demo.nails" in response.text

        async def check() -> None:
            from app.db.session import make_engine, make_session_factory

            engine = make_engine(database_url, null_pool=True)
            async with make_session_factory(engine)() as session:
                account = await session.scalar(
                    select(ChannelAccount).where(ChannelAccount.kind == "instagram")
                )
                assert account.external_id == IG_ID
                assert (
                    account.credentials != "long"
                    and decrypt(settings, account.credentials) == "long"
                )
            await engine.dispose()

        asyncio.run(check())


async def test_token_refresh_and_alert(ig_app, meta, clock) -> None:
    await connected_studio(ig_app)
    clock.advance(days=55)  # 5 days before expiry
    await refresh_tokens(ig_app, ig_app.channels["instagram"].http)
    async with ig_app.session_factory() as session:
        account = (
            await session.scalars(select(ChannelAccount).where(ChannelAccount.kind == "instagram"))
        ).one()
    assert decrypt(ig_app.settings, account.credentials) == "refreshed"
    assert account.token_expires_at > clock.now() + timedelta(days=50)

    clock.advance(days=55)
    meta.refresh_ok = False
    await refresh_tokens(ig_app, ig_app.channels["instagram"].http)
    assert any("токен Instagram" in text for text in ig_app.alerts.sent)


def test_state_is_bound_and_expires() -> None:
    settings = ig_settings()
    from app.core.crypto import CryptoError, verify_state

    state = sign_state(settings, {"tenant": "demo_studio"}, 60, now=1000)
    assert verify_state(settings, state, now=1010)["tenant"] == "demo_studio"
    with pytest.raises(CryptoError, match="expired"):
        verify_state(settings, state, now=2000)
    _, mac = state.split(".")
    forged = (
        base64.urlsafe_b64encode(
            json.dumps({"tenant": "evil", "exp": 1060}, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(CryptoError, match="signature"):
        verify_state(settings, f"{forged}.{mac}", now=1010)
