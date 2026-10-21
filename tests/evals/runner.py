"""Evals with the real model (`make eval`). Needs OPENAI_API_KEY and LLM_MODEL; uses the test
database (TEST_DATABASE_URL) and wipes it per case.

    uv run python -m tests.evals.runner                 # all cases, hard ones 3 times
    uv run python -m tests.evals.runner --case allergy  # one case
    uv run python -m tests.evals.runner --runs 1        # quick pass

Rule-level behaviour that does not depend on the model (limits, windows, drafts, duplicates,
DST) is covered by pytest; evals check what the model decides.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.brain import booking as bookings
from app.brain.ingest import Inbound, get_or_create_conversation, record_client_message
from app.brain.orchestrator import process_conversation
from app.channels.accounts import ensure_account
from app.channels.cli import CLIChannel
from app.config import get_settings
from app.context import AppContext
from app.core.alerts import Alerts
from app.core.clock import FrozenClock
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models import Booking, ChannelAccount, Conversation, Message, OfferedSlot, Service
from app.db.session import make_engine, make_session_factory
from app.llm.base import ChatMessage, LLMProvider
from app.llm.factory import LLMConfigError, build_llm
from app.owner.notifier import RecordingNotifier
from app.scheduling.availability import free_slots, offer_slots, staff_for_service
from app.scheduling.registry import default_calendars, provider_for
from app.tenants.loader import load_tenant
from app.tenants.schema import StudioConfig
from tests.evals.checks import TurnResult, check_turn
from tests.helpers import KYIV, ROOT, demo_config_dict

CASES_DIR = ROOT / "tests" / "evals" / "cases"
REPORTS_DIR = ROOT / "tests" / "evals" / "reports"
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://ai_admin:ai_admin@localhost:5435/ai_admin_test"
)
CLIENT = "eval-client"
JUDGE_PROMPT = (
    "Ти суворий оцінювач відповідей AI-асистента студії. Оціни останню відповідь бота за "
    'критерієм. Відповідай лише JSON: {"pass": true або false, "why": "коротко"}.'
)


def build_config(case: dict[str, Any]) -> StudioConfig:
    data = demo_config_dict()
    for key, value in (case.get("config") or {}).items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    data["services"] = data["services"] + list(case.get("add_services", []))
    for staff in data["staff"]:
        staff["services"] = staff["services"] + case.get("staff_add_services", {}).get(
            staff["id"], []
        )
    return StudioConfig.model_validate(data)


def local(text_value: str) -> datetime:
    """'2026-10-20 23:47' is Kyiv time; an explicit offset is kept as given."""
    moment = datetime.fromisoformat(text_value)
    return moment if moment.tzinfo else moment.replace(tzinfo=KYIV)


async def reset_db(engine) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


async def setup_case(app: AppContext, case: dict[str, Any], cfg: StudioConfig) -> int:
    async with app.session_factory() as session, session.begin():
        tenant = await load_tenant(session, cfg)
        tenant.mode = case.get("mode", "autopilot")
        account = await ensure_account(session, tenant.id, "cli", f"cli-{tenant.id}")
        client, conversation = await get_or_create_conversation(
            session, account, CLIENT, app.clock.now()
        )
        for step in case.get("setup", []):
            book = step["book"]
            start = local(book["start"])
            options = await free_slots(
                session,
                tenant.id,
                cfg,
                provider_for(app.calendars, cfg),
                book["service"],
                start.date(),
                start.date(),
                app.clock.now(),
                staff_key=book.get("staff"),
                time_from=start.time(),
                time_to=start.time(),
            )
            service = await session.scalar(select(Service).where(Service.key == book["service"]))
            staff = {
                s.key: s for s in await staff_for_service(session, tenant.id, cfg, book["service"])
            }
            offered = await offer_slots(
                session, conversation.id, service, staff, options[:1], app.clock.now()
            )
            ctx = bookings.BookingContext(
                session,
                tenant,
                cfg,
                provider_for(app.calendars, cfg),
                app.clock.now(),
                conversation,
                client,
            )
            await bookings.create_booking(
                ctx, offered[0].token, book.get("name", "Анна"), book.get("phone")
            )
            await session.execute(
                OfferedSlot.__table__.delete().where(OfferedSlot.conversation_id == conversation.id)
            )
            conversation.disclosed_at = app.clock.now() - timedelta(days=1)
            conversation.last_bot_message_at = app.clock.now() - timedelta(days=1)
        return account.id


async def run_turn(
    app: AppContext, account_id: int, turn: dict[str, Any], channel: CLIChannel, tz: ZoneInfo
) -> TurnResult:
    async with app.session_factory() as session:
        latest = await session.scalar(
            select(OfferedSlot)
            .order_by(OfferedSlot.created_at.desc(), OfferedSlot.start_at)
            .limit(1)
        )
    first_time = latest.start_at.astimezone(tz).strftime("%H:%M") if latest else ""
    user_text = (turn.get("user") or "").replace("{first_offered_time}", first_time)
    before_sent = len(channel.sent)
    started = app.clock.now()
    media = turn.get("media")
    async with app.session_factory() as session, session.begin():
        account = await session.get(ChannelAccount, account_id)
        message = await record_client_message(
            session,
            account,
            Inbound(
                CLIENT,
                None,
                user_text or None,
                media_type=media,
                media={"url": "https://cdn.example/photo.jpg"} if media else None,
            ),
            app.clock.now(),
            debounce=timedelta(0),
        )
        conversation_id = message.conversation_id
    plan = await process_conversation(app, conversation_id)
    async with app.session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        tool_rows = list(
            await session.scalars(
                select(Message.tool_calls).where(
                    Message.conversation_id == conversation_id,
                    Message.created_at >= started,
                    Message.tool_calls.is_not(None),
                )
            )
        )
        offered_now = list(
            await session.scalars(
                select(OfferedSlot).where(OfferedSlot.conversation_id == conversation_id)
            )
        )
        created = list(await session.scalars(select(Booking).where(Booking.created_at >= started)))
        services = {s.id: s.key for s in await session.scalars(select(Service))}
        first_reply = await session.scalar(
            select(Message.text)
            .where(Message.conversation_id == conversation_id, Message.role == "bot")
            .order_by(Message.id)
            .limit(1)
        )
    return TurnResult(
        replies=[t for _, t in channel.sent[before_sent:]],
        tools=[entry["name"] for row in tool_rows if row for entry in row if "name" in entry],
        state=conversation.state,
        handoff_reason=conversation.handoff_reason,
        bookings=[
            {"service": services[b.service_id], "price_min": b.price_min, "status": b.status}
            for b in created
        ],
        offered_times=[o.start_at.astimezone(tz).strftime("%H:%M") for o in offered_now],
        first_reply_of_conversation=first_reply,
        drafted=bool(plan and plan.status == "drafted"),
    )


async def judge(llm: LLMProvider, criterion: str, transcript: list[str]) -> tuple[bool, str]:
    messages = [
        ChatMessage("system", JUDGE_PROMPT),
        ChatMessage("user", f"Критерій: {criterion}\n\nДіалог:\n" + "\n".join(transcript)),
    ]
    response = await llm.complete(messages, [])
    try:
        verdict = json.loads((response.content or "").strip().strip("`").removeprefix("json"))
        return bool(verdict.get("pass")), str(verdict.get("why", ""))
    except (json.JSONDecodeError, AttributeError):
        return False, f"judge answer not JSON: {response.content!r}"


async def run_case(engine, factory, llm: LLMProvider, case: dict[str, Any]) -> list[dict[str, Any]]:
    await reset_db(engine)
    cfg = build_config(case)
    clock = FrozenClock(local(case.get("now", "2026-10-21 12:00")))
    channel = CLIChannel()
    notifier = RecordingNotifier()
    settings = get_settings()
    app = AppContext(
        settings=settings,
        session_factory=factory,
        llm=llm,
        clock=clock,
        channels={"cli": channel},
        calendars=default_calendars(),
        notifier=notifier,
        alerts=Alerts(None, None, clock),
    )
    account_id = await setup_case(app, case, cfg)
    transcript: list[str] = []
    reports = []
    for index, turn in enumerate(case["turns"]):
        clock.advance(minutes=1)
        result = await run_turn(app, account_id, turn, channel, ZoneInfo(cfg.timezone))
        transcript.append(f"Клієнтка: {turn.get('user', '')}")
        transcript += [f"Бот: {r}" for r in result.replies]
        expect = turn.get("expect", {})
        report = check_turn(expect, result, cfg, turn.get("user", ""))
        entry = {
            "turn": index,
            "replies": result.replies,
            "tools": result.tools,
            "checks": [o.__dict__ for o in report.outcomes],
        }
        if "judge" in expect:
            passed, why = await judge(llm, expect["judge"], transcript)
            entry["checks"].append({"key": "judge", "passed": passed, "detail": why, "soft": True})
        reports.append(entry)
    return reports


def load_cases(only: str | None) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        for case in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            if only is None or case["id"] == only:
                cases.append(case)
    return cases


async def amain(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        llm = build_llm(settings)
    except LLMConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    cfg.attributes["configure_logger"] = False
    await asyncio.to_thread(command.upgrade, cfg, "head")
    engine = make_engine(TEST_DATABASE_URL, null_pool=True)
    factory = make_session_factory(engine)
    cases = load_cases(args.case)
    if not cases:
        print("no cases found", file=sys.stderr)
        return 2
    summary = []
    hard_failures = 0
    for case in cases:
        runs = args.runs or (3 if case.get("hard") else 1)
        for run_no in range(runs):
            reports = await run_case(engine, factory, llm, case)
            failed = [c for r in reports for c in r["checks"] if not c["passed"] and not c["soft"]]
            soft_failed = [c for r in reports for c in r["checks"] if not c["passed"] and c["soft"]]
            status = "FAIL" if failed else ("soft" if soft_failed else "ok")
            hard_failures += bool(failed)
            print(f"{status:4} {case['id']} (run {run_no + 1}/{runs})")
            for check in failed + soft_failed:
                print(f"      {check['key']}: {check['detail']}")
            summary.append(
                {"case": case["id"], "run": run_no + 1, "status": status, "turns": reports}
            )
    await engine.dispose()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    report_path = REPORTS_DIR / f"{stamp}-{settings.llm_model or 'model'}.json"
    report_path.write_text(
        json.dumps({"model": settings.llm_model, "results": summary}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    total = len(summary)
    print(f"\n{total - hard_failures}/{total} runs without hard failures. Report: {report_path}")
    return 1 if hard_failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tests.evals.runner")
    parser.add_argument("--case", help="run one case by id")
    parser.add_argument("--runs", type=int, default=0, help="runs per case (default: 3 for hard)")
    sys.exit(asyncio.run(amain(parser.parse_args())))


if __name__ == "__main__":
    main()
