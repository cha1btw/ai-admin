"""Admin panel and landing form over HTTP: login, studio editing, test chat, leads."""

import copy
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.admin.auth import login_link
from app.api.main import create_app
from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import Client, Conversation, InboundEvent, Lead, Tenant
from app.db.session import SessionFactory
from app.llm.fake import FakeLLM, say
from app.owner.notifications import notify_lead
from app.owner.notifier import RecordingNotifier
from tests.helpers import demo_config_dict, kyiv

ADMIN = 111
STRANGER = 222
KEY = Fernet.generate_key().decode()
HEADERS = {"X-Admin-Request": "1"}


def admin_settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        admin_telegram_ids=str(ADMIN),
        encryption_key=KEY,
        llm_provider="fake",
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def http(
    database_url: str, session_factory: SessionFactory, fake_llm: FakeLLM
) -> AsyncIterator[httpx.AsyncClient]:
    # session_factory truncates the tables; the app opens its own engine.
    settings = admin_settings(database_url)
    application = create_app(settings, llm=fake_llm, clock=FrozenClock(kyiv("2026-10-20 23:47")))
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.app = application  # type: ignore[attr-defined]
            yield client


async def login(client: httpx.AsyncClient, telegram_id: int = ADMIN) -> httpx.Response:
    link = login_link(client.app.state.settings, telegram_id, base_url="http://test")
    token = link.split("token=", 1)[1]
    page = await client.get(f"/admin/login?token={token}")
    assert page.status_code == 200 and "Увійти" in page.text  # GET does not use the link up
    return await client.post("/admin/login", data={"token": token})


def demo(slug: str = "studio_one") -> dict:
    data = copy.deepcopy(demo_config_dict())
    data["slug"] = slug
    data["name"] = "Studio One"
    return data


async def test_login_link_is_single_use_and_admin_only(http: httpx.AsyncClient) -> None:
    assert (await http.get("/admin/api/studios")).status_code == 401
    response = await login(http)
    assert response.status_code == 303
    assert (await http.get("/admin/api/studios")).json() == []

    link = login_link(http.app.state.settings, ADMIN, base_url="http://test")
    token = link.split("token=", 1)[1]
    assert (await http.post("/admin/login", data={"token": token})).status_code == 303
    assert (await http.post("/admin/login", data={"token": token})).status_code == 401
    assert (await http.post("/admin/login", data={"token": token + "x"})).status_code == 401

    http.cookies.clear()
    assert (await login(http, STRANGER)).status_code == 403


async def test_changing_requests_need_the_csrf_header(http: httpx.AsyncClient) -> None:
    await login(http)
    response = await http.post("/admin/api/studios", json={"config": demo()})
    assert response.status_code == 403
    response = await http.post("/admin/api/studios", json={"config": demo()}, headers=HEADERS)
    assert response.json()["ok"] is True


async def test_studio_create_validate_update_rollback(http: httpx.AsyncClient) -> None:
    await login(http)
    created = (
        await http.post("/admin/api/studios", json={"config": demo()}, headers=HEADERS)
    ).json()
    assert created == {"ok": True, "slug": "studio_one", "version": 1}
    again = (await http.post("/admin/api/studios", json={"config": demo()}, headers=HEADERS)).json()
    assert again["ok"] is False and again["errors"][0]["loc"] == "slug"

    broken = demo()
    broken["services"][0].pop("price_min")
    result = (
        await http.post("/admin/api/validate", json={"config": broken}, headers=HEADERS)
    ).json()
    assert result["ok"] is False
    assert "price_min" in result["errors"][0]["msg"]

    # The editor sends a fixed price without price_max; only the real change shows in the diff.
    changed = demo()
    changed["services"][0]["price_min"] = 950
    changed["services"][0].pop("price_max", None)
    check = (
        await http.post(
            "/admin/api/validate", json={"config": changed, "slug": "studio_one"}, headers=HEADERS
        )
    ).json()
    assert check["ok"] is True
    assert check["diff"] == [{"path": "services[gel_manicure].price_min", "old": 900, "new": 950}]
    assert check["public_data"]["services"][0]["name"] == "Манікюр з гель-лаком"

    saved = (
        await http.put("/admin/api/studios/studio_one", json={"config": changed}, headers=HEADERS)
    ).json()
    assert saved["version"] == 2
    renamed = demo("other_slug")
    response = await http.put(
        "/admin/api/studios/studio_one", json={"config": renamed}, headers=HEADERS
    )
    assert response.json()["ok"] is False

    rolled = (await http.post("/admin/api/studios/studio_one/rollback/1", headers=HEADERS)).json()
    assert rolled["version"] == 3
    detail = (await http.get("/admin/api/studios/studio_one")).json()
    assert detail["config"]["services"][0]["price_min"] == 900
    assert [v["source"] for v in detail["versions"]] == ["rollback:v1", "admin", "admin"]

    await http.post(
        "/admin/api/studios/studio_one/runtime", json={"status": "paused"}, headers=HEADERS
    )
    row = (await http.get("/admin/api/studios")).json()[0]
    assert row["status"] == "paused" and row["mode"] == "autopilot"


async def test_test_chat_answers_inline_and_stays_sandboxed(
    http: httpx.AsyncClient, fake_llm: FakeLLM, session_factory: SessionFactory
) -> None:
    await login(http)
    await http.post("/admin/api/studios", json={"config": demo()}, headers=HEADERS)
    fake_llm.add(say("Манікюр з гель-лаком коштує 900 грн."))
    reply = (
        await http.post(
            "/admin/api/studios/studio_one/chat",
            json={"text": "Скільки коштує манікюр?"},
            headers=HEADERS,
        )
    ).json()
    assert "900 грн" in " ".join(reply["replies"])
    assert reply["state"] == "BOT_ACTIVE"
    async with session_factory() as session:
        conversation = (await session.scalars(select(Conversation))).one()
        assert conversation.is_sandbox and conversation.reply_due_at is None

    await http.post("/admin/api/studios/studio_one/chat/reset", headers=HEADERS)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Client)) == 0


async def test_import_returns_a_validated_draft(
    http: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    await login(http)
    imported = demo("imported")
    http.app.state.import_llm = FakeLLM(
        [say(json.dumps({"config": imported, "questions": ["Чи є парковка?"]}))]
    )
    source = (
        "Манікюр з гель-лаком 900 грн, без покриття 450, педикюр 1100, нарощування від 1300, "
        "зняття 200"
    )
    result = (await http.post("/admin/api/import", json={"text": source}, headers=HEADERS)).json()
    assert result["errors"] == []
    assert result["questions"] == ["Чи є парковка?"]
    assert result["config"]["slug"] == "imported"
    async with session_factory() as session:  # nothing is saved by an import
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 0


async def test_landing_lead_is_stored_rate_limited_and_sent_to_founders(
    http: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    form = {"name": "Олена", "contact": "@olena", "business": "Nails", "service": "crm"}
    assert (await http.post("/leads", json=form)).status_code == 201
    assert (await http.post("/leads", json={**form, "website": "spam"})).status_code == 201
    async with session_factory() as session:
        lead = (await session.scalars(select(Lead))).one()
        event = (await session.scalars(select(InboundEvent))).one()
    assert lead.service == "crm" and event.source == "lead"

    for _ in range(4):
        assert (await http.post("/leads", json=form)).status_code == 201
    assert (await http.post("/leads", json=form)).status_code == 429

    app = http.app.state.admin
    app.notifier = RecordingNotifier()
    await notify_lead(app, lead.id)
    assert app.notifier.sent[0].chat_id == ADMIN
    assert "@olena" in app.notifier.sent[0].text

    await login(http)
    await http.post(f"/admin/api/leads/{lead.id}", json={"status": "won"}, headers=HEADERS)
    leads = (await http.get("/admin/api/leads")).json()
    assert {item["status"] for item in leads if item["id"] == lead.id} == {"won"}
