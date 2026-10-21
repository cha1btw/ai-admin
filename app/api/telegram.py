"""Telegram webhooks: check the secret header, store the update, answer 200 right away."""

import hmac

from fastapi import APIRouter, HTTPException, Request

from app.core.inbox import store_event

router = APIRouter()
SOURCES = {"owner": "telegram_owner", "demo": "telegram_demo"}


@router.post("/webhooks/telegram/{bot}")
async def telegram_webhook(bot: str, request: Request) -> dict[str, bool]:
    source = SOURCES.get(bot)
    if source is None:
        raise HTTPException(status_code=404)
    secret = request.app.state.settings.telegram_webhook_secret
    given = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret is None or not hmac.compare_digest(given, secret.get_secret_value()):
        raise HTTPException(status_code=403)
    payload = await request.json()
    update_id = payload.get("update_id")
    if update_id is None:
        raise HTTPException(status_code=400)
    async with request.app.state.session_factory() as session, session.begin():
        await store_event(session, source, str(update_id), payload)
    return {"ok": True}
