"""Instagram webhooks, studio connection (Business Login callback) and the data deletion page."""

import hmac
import json
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import select

from app.channels.instagram import (
    InstagramError,
    connect_account,
    save_account,
    signature_valid,
    split_webhook,
)
from app.core.crypto import CryptoError, verify_state
from app.core.inbox import store_event
from app.core.logging import get_logger
from app.db.models import AuditLog, Tenant

log = get_logger(__name__)
router = APIRouter()

PAGE = """<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;
color:#142030;background:#f8f9fb}}h1{{font-size:1.4rem}}</style></head>
<body><h1>{title}</h1>{body}</body></html>"""


@router.get("/webhooks/instagram")
async def verify_webhook(request: Request) -> PlainTextResponse:
    params = request.query_params
    expected = request.app.state.settings.meta_verify_token
    token = params.get("hub.verify_token", "")
    if (
        params.get("hub.mode") != "subscribe"
        or expected is None
        or not hmac.compare_digest(token, expected.get_secret_value())
    ):
        raise HTTPException(status_code=403)
    return PlainTextResponse(params.get("hub.challenge", ""))


@router.post("/webhooks/instagram")
async def receive_webhook(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    body = await request.body()
    secret = settings.meta_app_secret
    if secret is None or not signature_valid(
        secret.get_secret_value(), body, request.headers.get("X-Hub-Signature-256")
    ):
        raise HTTPException(status_code=403)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400) from exc
    if payload.get("object") != "instagram":
        return {"ok": True}
    async with request.app.state.session_factory() as session, session.begin():
        for key, event in split_webhook(payload):
            await store_event(session, "instagram", key, event)
    return {"ok": True}


@router.get("/oauth/instagram/callback", response_class=HTMLResponse)
async def oauth_callback(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    params = request.query_params
    if "error" in params or "code" not in params:
        return HTMLResponse(
            PAGE.format(title="Підключення скасовано", body="<p>Instagram не підключено.</p>"),
            status_code=400,
        )
    try:
        state = verify_state(settings, params.get("state", ""))
    except CryptoError:
        return HTMLResponse(
            PAGE.format(title="Посилання застаріло", body="<p>Попросіть у нас нове посилання.</p>"),
            status_code=400,
        )
    http = getattr(request.app.state, "instagram_http", None) or httpx.AsyncClient(timeout=15.0)
    try:
        ig_id, username, token, ttl = await connect_account(settings, http, params["code"])
    except (InstagramError, httpx.HTTPError, KeyError) as exc:
        log.warning("instagram.connect_failed", error=type(exc).__name__)
        return HTMLResponse(
            PAGE.format(
                title="Не вдалося підключити", body="<p>Спробуйте ще раз або напишіть нам.</p>"
            ),
            status_code=502,
        )
    async with request.app.state.session_factory() as session, session.begin():
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == state["tenant"]))
        if tenant is None:
            raise HTTPException(status_code=404)
        await save_account(
            session, settings, datetime.now(UTC), tenant.id, ig_id, username, token, ttl
        )
        session.add(
            AuditLog(
                tenant_id=tenant.id,
                actor="oauth",
                action="instagram.connected",
                details={"username": username},
            )
        )
        name = tenant.name
    body = (
        f"<p>Акаунт @{username} підключено до студії «{name}». Бот відповідатиме за режимом "
        "студії (/mode в Telegram).</p><p>Вимкніть інші автовідповіді в Instagram, щоб клієнтки "
        "не отримували дві відповіді.</p>"
    )
    return HTMLResponse(PAGE.format(title="✅ Instagram підключено", body=body))


@router.get("/privacy/data-deletion", response_class=HTMLResponse)
async def data_deletion() -> HTMLResponse:
    body = (
        "<p>Бот AI-адміністратора зберігає ім'я, телефон і листування, щоб відповідати й "
        "записувати вас до студії. Листування видаляється автоматично через 90 днів, якщо студія "
        "не встановила інший строк.</p>"
        "<p>Щоб видалити ваші дані раніше, напишіть у тому самому чаті: «Видаліть мої дані». "
        "Ми видалимо листування, ім'я й телефон; запис у студії залишиться без ваших даних.</p>"
    )
    return HTMLResponse(PAGE.format(title="Видалення даних", body=body))
