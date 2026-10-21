"""Command line for founders: studio configs and a terminal chat with a studio bot.

uv run python -m app.cli tenant validate studios/private/x.json
uv run python -m app.cli tenant load studios/private/x.json [--demo]
uv run python -m app.cli tenant preview demo_studio
uv run python -m app.cli chat demo_studio
"""

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from app.brain.ingest import Inbound, record_client_message
from app.brain.orchestrator import process_conversation
from app.channels.accounts import ensure_account
from app.channels.cli import CLIChannel
from app.config import get_settings
from app.context import AppContext
from app.core.alerts import Alerts
from app.core.clock import SystemClock
from app.core.logging import configure_logging
from app.db.models import ChannelAccount, Client, Tenant
from app.db.session import make_engine, make_session_factory
from app.llm.factory import LLMConfigError, build_llm
from app.owner.notifier import RecordingNotifier
from app.scheduling.registry import default_calendars
from app.tenants.loader import ConfigError, load_tenant, read_config, studio_config
from app.tenants.public import public_studio_data

CLI_USER = "terminal-user"


def _factory():
    settings = get_settings()
    return settings, make_session_factory(make_engine(settings.database_url))


async def cmd_validate(path: str) -> int:
    try:
        cfg = read_config(Path(path))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"OK: {cfg.name} ({cfg.slug}), послуг: {len(cfg.services)}, майстрів: {len(cfg.staff)}, "
        f"календар: {cfg.calendar_provider}, мови: {', '.join(cfg.allowed_languages)}"
    )
    return 0


async def cmd_load(path: str, demo: bool) -> int:
    try:
        cfg = read_config(Path(path))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    _, factory = _factory()
    async with factory() as session, session.begin():
        tenant = await load_tenant(session, cfg, source=str(path), is_demo=demo)
        version, mode = tenant.config_version, tenant.mode
    print(f"Завантажено {cfg.slug}: версія конфігурації {version}, режим {mode}.")
    return 0


async def cmd_preview(slug: str) -> int:
    _, factory = _factory()
    async with factory() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is None:
            print(f"Студії {slug} немає в базі.", file=sys.stderr)
            return 1
        cfg = studio_config(tenant)
    today = SystemClock().now().astimezone(ZoneInfo(cfg.timezone)).date()
    print(json.dumps(public_studio_data(cfg, today), ensure_ascii=False, indent=2))
    return 0


async def cmd_chat(slug: str) -> int:
    settings, factory = _factory()
    try:
        llm = build_llm(settings)
    except LLMConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    clock = SystemClock()
    # Show master notifications in the terminal even for the demo studio.
    settings = settings.model_copy(update={"demo_owner_chat_id": settings.demo_owner_chat_id or -1})
    notifier = RecordingNotifier(echo=True)
    app = AppContext(
        settings=settings,
        session_factory=factory,
        llm=llm,
        clock=clock,
        channels={"cli": CLIChannel(echo=True)},
        calendars=default_calendars(),
        notifier=notifier,
        alerts=Alerts(None, None, clock),
    )
    async with factory() as session, session.begin():
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is None:
            print(f"Студії {slug} немає в базі. Спочатку: make demo", file=sys.stderr)
            return 1
        account = await ensure_account(session, tenant.id, "cli", f"cli-{tenant.id}")
        account_id, name, mode = account.id, tenant.name, tenant.mode
    print(f"Чат зі студією «{name}» (режим {mode}). /reset — нова розмова, /exit — вихід.\n")
    while True:
        try:
            text = (await asyncio.to_thread(input, "Ви: ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in {"/exit", "/quit"}:
            break
        if text == "/reset":
            async with factory() as session, session.begin():
                await session.execute(
                    delete(Client).where(
                        Client.channel_account_id == account_id, Client.external_user_id == CLI_USER
                    )
                )
            print("Нова розмова.\n")
            continue
        async with factory() as session, session.begin():
            account = await session.get(ChannelAccount, account_id)
            message = await record_client_message(
                session, account, Inbound(CLI_USER, None, text), clock.now(), debounce=timedelta(0)
            )
            conversation_id = message.conversation_id
        plan = await process_conversation(app, conversation_id)
        if plan and plan.status.startswith("skipped"):
            print(f"(бот мовчить: {plan.status.split(':', 1)[1]})\n")
        elif plan and plan.status == "drafted":
            print("(режим чернетки: відповідь пішла майстру на погодження)\n")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    tenant = sub.add_parser("tenant", help="studio configs")
    tenant_sub = tenant.add_subparsers(dest="action", required=True)
    v = tenant_sub.add_parser("validate")
    v.add_argument("path")
    ld = tenant_sub.add_parser("load")
    ld.add_argument("path")
    ld.add_argument("--demo", action="store_true", help="public demo studio (daily cleanup)")
    pv = tenant_sub.add_parser("preview")
    pv.add_argument("slug")
    chat = sub.add_parser("chat", help="talk to a studio bot in the terminal")
    chat.add_argument("slug")
    args = parser.parse_args()

    configure_logging("WARNING")
    if args.command == "chat":
        code = asyncio.run(cmd_chat(args.slug))
    elif args.action == "validate":
        code = asyncio.run(cmd_validate(args.path))
    elif args.action == "load":
        code = asyncio.run(cmd_load(args.path, args.demo))
    else:
        code = asyncio.run(cmd_preview(args.slug))
    sys.exit(code)


if __name__ == "__main__":
    main()
