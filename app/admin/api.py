"""Admin panel HTTP layer: a static single-page app at /admin and a JSON API at /admin/api.

Founders only (see app/admin/auth.py). The panel replaces hand-editing studio JSON: forms with
validation, AI import, a test chat with the studio's real data, versions with rollback.
"""

import html
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.admin import service
from app.admin.auth import COOKIE, consume_login, require_admin, session_cookie
from app.admin.importer import import_studio
from app.config import Settings
from app.context import AppContext
from app.core.logging import get_logger
from app.db.models import Tenant
from app.llm.factory import LLMConfigError, build_llm
from app.owner.actions import health_text, stats_text
from app.tenants.public import public_studio_data

log = get_logger(__name__)

STATIC = Path(__file__).parent / "static"
IMPORT_MAX_TOKENS = 16_000
IMPORT_TIMEOUT_S = 180


class ConfigBody(BaseModel):
    config: dict[str, Any]
    slug: str | None = None  # validate: compare with this studio's current config


class RuntimeBody(BaseModel):
    mode: str | None = None
    status: str | None = None


class RoleBody(BaseModel):
    role: str = "owner"


class ChatBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ImportBody(BaseModel):
    text: str = Field(min_length=20, max_length=30_000)
    slug: str | None = None  # update this studio instead of creating a new one


class LeadStatusBody(BaseModel):
    status: str


router = APIRouter()
api = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])


def _ctx(request: Request) -> AppContext:
    return request.app.state.admin


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="studio not found")


async def _bot_username(request: Request) -> str | None:
    state = request.app.state
    if getattr(state, "owner_bot_username", None) is None and state.settings.owner_bot_token:
        from aiogram import Bot

        bot = Bot(token=state.settings.owner_bot_token.get_secret_value())
        try:
            state.owner_bot_username = (await bot.get_me()).username
        except Exception:
            log.warning("admin.bot_username_failed")
        finally:
            await bot.session.close()
    return getattr(state, "owner_bot_username", None)


# --- pages and login ---------------------------------------------------------------------------


def _asset_version() -> str:
    """Changes whenever a static file changes, so browsers never run a stale admin.js."""
    return str(int(max(f.stat().st_mtime for f in STATIC.iterdir())))


@router.get("/admin", include_in_schema=False)
async def index() -> HTMLResponse:
    page = (STATIC / "index.html").read_text(encoding="utf-8").replace("__V__", _asset_version())
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


LOGIN_PAGE = """<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Вхід в адмінку</title>
<link rel="stylesheet" href="/admin/static/admin.css"></head><body class="login">
<form method="post" action="/admin/login" class="card login-card">
<h1>AI-адміністратор</h1><p>Вхід для засновників.</p>
<input type="hidden" name="token" value="{token}">
<button class="btn primary" type="submit">Увійти</button></form></body></html>"""


@router.get("/admin/login", include_in_schema=False)
async def login_page(token: str) -> HTMLResponse:
    # The link is consumed by POST: link previews in Telegram only GET it, so they cannot burn it.
    return HTMLResponse(LOGIN_PAGE.format(token=html.escape(token, quote=True)))


@router.post("/admin/login", include_in_schema=False)
async def login(request: Request) -> RedirectResponse:
    # A plain urlencoded form; parsed by hand to avoid the python-multipart dependency.
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    telegram_id = await consume_login(request, (form.get("token") or [""])[0])
    settings: Settings = request.app.state.settings
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        COOKIE,
        session_cookie(settings, telegram_id),
        max_age=settings.admin_session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https"
        or (settings.public_base_url or "").startswith("https"),
        path="/admin",
    )
    return response


@router.post("/admin/logout", include_in_schema=False)
async def logout() -> RedirectResponse:
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(COOKIE, path="/admin")
    return response


# --- read -------------------------------------------------------------------------------------


@api.get("/me")
async def me(request: Request, admin_id: int = Depends(require_admin)) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return {
        "telegram_id": admin_id,
        "bot_username": await _bot_username(request),
        "demo_slug": settings.demo_tenant_slug,
        "llm": {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "ready": _ctx(request).llm.model_name != "unavailable",
        },
        "instagram_ready": bool(settings.meta_app_id and settings.public_base_url),
    }


@api.get("/studios")
async def studios(request: Request) -> list[dict[str, Any]]:
    return await service.dashboard(_ctx(request))


@api.get("/studios/{slug}")
async def studio(request: Request, slug: str) -> dict[str, Any]:
    detail = await service.studio_detail(_ctx(request), slug)
    if detail is None:
        raise _not_found()
    return detail


@api.get("/studios/{slug}/versions/{version}")
async def version(request: Request, slug: str, version: int) -> dict[str, Any]:
    data = await service.version_config(_ctx(request), slug, version)
    if data is None:
        raise HTTPException(status_code=404)
    current = await service.studio_detail(_ctx(request), slug)
    return {"config": data, "diff": service.config_diff(data, current["config"])}


@api.get("/studios/{slug}/stats")
async def stats(request: Request, slug: str, days: int = 30) -> dict[str, str]:
    ctx = _ctx(request)
    async with ctx.session_factory() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise _not_found()
    return {"text": await stats_text(ctx, tenant, max(1, min(days, 365)))}


@api.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {"text": await health_text(_ctx(request))}


@api.get("/leads")
async def leads(request: Request) -> list[dict[str, Any]]:
    return await service.list_leads(_ctx(request))


# --- edit --------------------------------------------------------------------------------------


@api.post("/validate")
async def validate(request: Request, body: ConfigBody) -> dict[str, Any]:
    cfg, errors = service.validate_config(body.config)
    result: dict[str, Any] = {"ok": cfg is not None, "errors": errors}
    if body.slug:
        current = await service.studio_detail(_ctx(request), body.slug)
        # Compare what would be stored (defaults filled in) with what is stored.
        new = cfg.model_dump(mode="json") if cfg is not None else body.config
        result["diff"] = service.config_diff(current["config"] if current else None, new)
    if cfg is not None:
        today = _ctx(request).clock.now().astimezone(ZoneInfo(cfg.timezone)).date()
        result["public_data"] = public_studio_data(cfg, today)
    return result


@api.post("/studios")
async def create_studio(
    request: Request, body: ConfigBody, admin_id: int = Depends(require_admin)
) -> dict[str, Any]:
    return await service.save_studio(
        _ctx(request), body.config, admin_id=admin_id, expected_slug=None
    )


@api.put("/studios/{slug}")
async def update_studio(
    request: Request,
    slug: str,
    body: ConfigBody,
    admin_id: int = Depends(require_admin),
) -> dict[str, Any]:
    if await service.studio_detail(_ctx(request), slug) is None:
        raise _not_found()
    return await service.save_studio(
        _ctx(request), body.config, admin_id=admin_id, expected_slug=slug
    )


@api.post("/studios/{slug}/rollback/{version}")
async def rollback(
    request: Request, slug: str, version: int, admin_id: int = Depends(require_admin)
) -> dict[str, Any]:
    data = await service.version_config(_ctx(request), slug, version)
    if data is None:
        raise HTTPException(status_code=404)
    return await service.save_studio(
        _ctx(request), data, admin_id=admin_id, expected_slug=slug, source=f"rollback:v{version}"
    )


@api.post("/studios/{slug}/runtime")
async def runtime(
    request: Request,
    slug: str,
    body: RuntimeBody,
    admin_id: int = Depends(require_admin),
) -> dict[str, bool]:
    ok = await service.set_runtime(
        _ctx(request), slug, admin_id=admin_id, mode=body.mode, status=body.status
    )
    if not ok:
        raise _not_found()
    return {"ok": True}


@api.post("/studios/{slug}/owner-link")
async def owner_link(request: Request, slug: str, body: RoleBody) -> dict[str, Any]:
    if body.role not in ("owner", "admin"):
        raise HTTPException(status_code=422, detail="role must be owner or admin")
    result = await service.owner_link(_ctx(request), slug, body.role, await _bot_username(request))
    if not result["ok"]:
        raise _not_found()
    return result


@api.post("/studios/{slug}/instagram-link")
async def instagram_link(request: Request, slug: str) -> dict[str, str]:
    from app.channels.instagram import InstagramError, authorize_url

    try:
        return {"url": authorize_url(request.app.state.settings, slug)}
    except InstagramError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --- test chat, check, import ----------------------------------------------------------------


@api.post("/studios/{slug}/chat")
async def chat(
    request: Request,
    slug: str,
    body: ChatBody,
    admin_id: int = Depends(require_admin),
) -> dict[str, Any]:
    result = await service.test_chat(_ctx(request), slug, admin_id, body.text)
    if result is None:
        raise _not_found()
    return result


@api.post("/studios/{slug}/chat/reset")
async def chat_reset(
    request: Request, slug: str, admin_id: int = Depends(require_admin)
) -> dict[str, bool]:
    await service.reset_test_chat(_ctx(request), slug, admin_id)
    return {"ok": True}


@api.post("/studios/{slug}/check")
async def check(
    request: Request, slug: str, admin_id: int = Depends(require_admin)
) -> list[dict[str, Any]]:
    results = await service.automatic_check(_ctx(request), slug, admin_id)
    if results is None:
        raise _not_found()
    return results


@api.post("/import")
async def ai_import(request: Request, body: ImportBody) -> dict[str, Any]:
    ctx = _ctx(request)
    base = None
    if body.slug:
        detail = await service.studio_detail(ctx, body.slug)
        if detail is None:
            raise _not_found()
        base = detail["config"]
    llm = getattr(request.app.state, "import_llm", None)
    if llm is None:
        try:
            llm = build_llm(
                request.app.state.settings,
                max_completion_tokens=IMPORT_MAX_TOKENS,
                timeout_s=IMPORT_TIMEOUT_S,
            )
        except LLMConfigError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = await import_studio(llm, body.text, base=base)
    target = result.stored or result.config
    diff = service.config_diff(base, target) if target is not None else []
    return {
        "config": result.config,
        "errors": result.errors,
        "questions": result.questions,
        "warnings": result.warnings,
        "tokens": result.tokens,
        "diff": diff,
    }


@api.post("/leads/{lead_id}")
async def lead_status(request: Request, lead_id: int, body: LeadStatusBody) -> dict[str, bool]:
    if not await service.set_lead_status(_ctx(request), lead_id, body.status):
        raise HTTPException(status_code=404)
    return {"ok": True}


router.include_router(api)
