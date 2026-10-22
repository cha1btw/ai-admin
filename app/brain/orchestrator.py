"""Reply pipeline for one conversation (docs/PRODUCT.md, section 1).

One run: claim the conversation (advisory lock), read new client messages, apply switches
(pause, human, handoff), limits and media rules, ask the model with tools, check the reply,
add the disclosure, then either send (autopilot) or create a draft for the master. Database
work commits first; sending and notifications happen after the commit.
"""

import dataclasses
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.brain.booking import active_bookings
from app.brain.events import BookingCreated, DraftCreated, Event, HandoffStarted, MediaReceived
from app.brain.guardrails import (
    build_facts,
    disclosure_needed,
    find_violations,
    mentions_health,
    promises_handoff,
    strip_intro,
)
from app.brain.handoff import eta_text, in_owner_hours, start_handoff
from app.brain.i18n import day_label, hhmm
from app.brain.ingest import supersede_drafts
from app.brain.language import choose_language, detect_language
from app.brain.prompts import PROMPT_VERSION, build_system_prompt
from app.brain.templates import render
from app.brain.tools import TurnContext, execute_tool, tool_specs
from app.context import AppContext
from app.core.logging import get_logger
from app.core.text import clean_reply
from app.db.models import (
    SLOT_HOLDING_STATUSES,
    AuditLog,
    Booking,
    ChannelAccount,
    Client,
    Conversation,
    Draft,
    Message,
    OfferedSlot,
    Service,
    Staff,
    Tenant,
)
from app.llm.base import ChatMessage, LLMError
from app.owner.notifications import deliver, dispatch_events
from app.scheduling.base import CalendarProvider
from app.scheduling.registry import provider_for
from app.scheduling.slots import Interval
from app.tenants.loader import studio_config
from app.tenants.public import public_studio_data
from app.tenants.schema import StudioConfig

log = get_logger(__name__)

CONVERSATION_LOCK_SPACE = 1
MAX_TOOL_ROUNDS = 4
HISTORY_DAYS = 14
HISTORY_LIMIT = 30
VISUAL_MEDIA = {"photo", "video", "file"}
MEDIA_LABELS = {
    "photo": "[клієнтка надіслала фото]",
    "video": "[клієнтка надіслала відео]",
    "voice": "[голосове повідомлення: бот не прослуховує]",
    "sticker": "[стікер]",
    "file": "[файл]",
}
_PHONE_WORDS = re.compile(r"телефон|номер|phone|telefon", re.IGNORECASE)
_WORDLESS = re.compile(r"^[\W\d_]*$")
CORRECTION = (
    "Відповідь містила дані, яких немає в ДАНІ чи в результатах інструментів: {problems}. "
    "Напиши відповідь ще раз, використовуючи тільки ДАНІ та результати інструментів."
)


@dataclass
class Outgoing:
    message_id: int


@dataclass
class ReplyPlan:
    status: str  # replied|drafted|skipped:<why>|nothing
    outgoing: list[Outgoing] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    draft_id: int | None = None


@dataclass
class Turn:
    text: str
    from_template: bool
    staged_action: dict[str, Any] | None = None
    handoff: tuple[str, str] | None = None
    events: list[Event] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None
    latency_ms: int = 0


@dataclass
class _State:
    app: AppContext
    session: Any
    tenant: Tenant
    cfg: StudioConfig
    conversation: Conversation
    client: Client
    account: ChannelAccount
    provider: CalendarProvider
    now: datetime
    lang: str
    staged: bool


# --- public entry points -------------------------------------------------------------------


async def process_conversation(app: AppContext, conversation_id: int) -> ReplyPlan | None:
    """Run the pipeline once. Returns None if another worker holds the conversation."""
    plan: ReplyPlan | None = None
    try:
        async with app.session_factory() as session, session.begin():
            locked = await session.scalar(
                select(func.pg_try_advisory_xact_lock(CONVERSATION_LOCK_SPACE, conversation_id))
            )
            if not locked:
                return None
            plan = await _plan(app, session, conversation_id)
    except Exception:
        log.exception("reply.crashed", conversation_id=conversation_id)
        await app.alerts.send(
            f"reply_crashed:{conversation_id}",
            f"Збій обробки діалогу #{conversation_id}; клієнтці відповідає шаблон.",
        )
        plan = await _plan_after_crash(app, conversation_id)
    if plan is None:
        return None
    plan.events += await deliver(app, plan.outgoing)
    await dispatch_events(app, plan.events)
    return plan


# --- planning ------------------------------------------------------------------------------


async def _plan(app: AppContext, session: Any, conversation_id: int) -> ReplyPlan:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return ReplyPlan("nothing")
    now = app.clock.now()
    conversation.reply_due_at = None
    new = list(
        await session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "client",
                Message.id > conversation.last_processed_message_id,
            )
            .order_by(Message.id)
        )
    )
    if not new:
        return ReplyPlan("nothing")
    conversation.last_processed_message_id = new[-1].id

    tenant = await session.get(Tenant, conversation.tenant_id)
    cfg = studio_config(tenant)
    client = await session.get(Client, conversation.client_id)
    account = await session.get(ChannelAccount, conversation.channel_account_id)
    provider = provider_for(app.calendars, cfg)

    texts = [m for m in new if m.text and not m.is_deleted and not _WORDLESS.match(m.text)]
    detected = next((d for m in reversed(texts) if (d := detect_language(m.text))), None)
    if detected:
        client.language = detected
    lang = choose_language(detected, client.language, cfg.allowed_languages, cfg.default_language)

    if tenant.status == "paused":
        return ReplyPlan("skipped:paused")
    if conversation.state == "HUMAN_ACTIVE":
        return ReplyPlan("skipped:human")

    busy = await _master_busy_now(session, tenant, cfg, provider, now)
    st = _State(
        app,
        session,
        tenant,
        cfg,
        conversation,
        client,
        account,
        provider,
        now,
        lang,
        _is_staged(tenant, conversation, cfg, now, busy),
    )
    plan = ReplyPlan("replied")

    if conversation.state == "HANDOFF_PENDING":
        interval = timedelta(hours=cfg.handoff.holding_interval_hours)
        recent = conversation.holding_sent_at and now - conversation.holding_sent_at < interval
        if st.staged or recent:
            return ReplyPlan("skipped:handoff_pending")
        conversation.holding_sent_at = now
        eta = eta_text(cfg, now, lang)
        return await _finish(st, plan, Turn(render("holding", lang, eta=eta), from_template=True))

    # R13: limits before any model call.
    if await _replies_last_hour(session, conversation.id, now) >= cfg.limits.replies_per_hour:
        await app.alerts.send(
            f"replies_limit:{conversation.id}",
            f"{tenant.slug}: діалог #{conversation.id} перевищив ліміт відповідей за годину.",
        )
        return await _finish(st, plan, _fallback(st, "abuse", "Перевищено ліміт відповідей бота"))
    if await _tokens_today(session, tenant.id, cfg, now) >= cfg.limits.daily_token_budget:
        local_day = now.astimezone(ZoneInfo(cfg.timezone)).date()
        await app.alerts.send(
            f"budget:{tenant.id}:{local_day}", f"{tenant.slug}: вичерпано денний бюджет токенів."
        )
        return await _finish(st, plan, _fallback(st, "other", "Вичерпано денний бюджет бота"))

    visual = [m for m in new if m.media_type in VISUAL_MEDIA]
    voices = [m for m in new if m.media_type == "voice"]
    plan.events += [MediaReceived(tenant.id, conversation.id, m.id) for m in visual]
    if voices and not texts and not visual:
        conversation.voice_strikes += 1
        if conversation.voice_strikes >= 2:
            turn = Turn(
                render("handoff_voice", lang, eta=eta_text(cfg, now, lang)),
                from_template=True,
                handoff=("media", "Клієнтка надсилає голосові повідомлення."),
            )
            return await _finish(st, plan, turn)
        return await _finish(st, plan, Turn(render("voice_request", lang), from_template=True))
    if texts:
        conversation.voice_strikes = 0
    if not texts and not visual and conversation.disclosed_at is not None:
        return ReplyPlan("skipped:noise", events=plan.events)
    # Only emoji as the very first message: still answer, so the client is not met with silence.

    turn = await _model_turn(st, new)
    return await _finish(st, plan, turn)


def _is_staged(
    tenant: Tenant, conversation: Conversation, cfg: StudioConfig, now: datetime, busy: bool
) -> bool:
    """Draft logic applies in draft mode and in offhours while the master is free (R12)."""
    if conversation.is_sandbox or tenant.is_demo or tenant.mode == "autopilot":
        return False
    if tenant.mode == "draft":
        return True
    return in_owner_hours(cfg, now) and not busy


async def _master_busy_now(
    session: Any, tenant: Tenant, cfg: StudioConfig, provider: CalendarProvider, now: datetime
) -> bool:
    if tenant.mode != "offhours" or cfg.calendar_provider == "lead_capture":
        return False
    count = await session.scalar(
        select(func.count())
        .select_from(Booking)
        .where(
            Booking.tenant_id == tenant.id,
            Booking.status.in_(SLOT_HOLDING_STATUSES),
            Booking.is_sandbox.is_(False),
            Booking.start_at <= now,
            Booking.end_at > now,
        )
    )
    if count:
        return True
    if cfg.calendar_provider == "google":
        window = Interval(now, now + timedelta(minutes=1))
        for staff in await session.scalars(select(Staff).where(Staff.tenant_id == tenant.id)):
            if await provider.busy(cfg, staff, window):
                return True
    return False


async def _replies_last_hour(session: Any, conversation_id: int, now: datetime) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == "bot",
            Message.created_at > now - timedelta(hours=1),
        )
    )


async def _tokens_today(session: Any, tenant_id: int, cfg: StudioConfig, now: datetime) -> int:
    tz = ZoneInfo(cfg.timezone)
    midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(func.coalesce(func.sum(Message.tokens_in + Message.tokens_out), 0)).where(
            Message.tenant_id == tenant_id, Message.created_at >= midnight
        )
    )
    return int(total or 0)


def _fallback(st: _State, reason: str, summary: str) -> Turn:
    """Safe reply when the model cannot be used: thank, give an honest time, hand off."""
    text = render("fallback", st.lang, eta=eta_text(st.cfg, st.now, st.lang))
    return Turn(text, from_template=True, handoff=(reason, summary))


# --- model turn ----------------------------------------------------------------------------


def _client_content(message: Message) -> str:
    if message.is_deleted:
        return "[повідомлення видалено]"
    parts = []
    if message.is_story_reply:
        parts.append("[відповідь на сторіс, бот не бачить сторіс]")
    if message.media_type:
        parts.append(MEDIA_LABELS.get(message.media_type, "[вкладення]"))
    if message.text:
        parts.append(message.text)
    return " ".join(parts) or "[порожнє повідомлення]"


async def _history(st: _State) -> list[ChatMessage]:
    rows = list(
        await st.session.scalars(
            select(Message)
            .where(
                Message.conversation_id == st.conversation.id,
                Message.role.in_(("client", "bot", "human")),
                Message.created_at >= st.now - timedelta(days=HISTORY_DAYS),
            )
            .order_by(Message.id.desc())
            .limit(HISTORY_LIMIT)
        )
    )
    history = []
    for message in reversed(rows):
        if message.role == "client":
            history.append(ChatMessage("user", _client_content(message)))
        elif message.role == "bot":
            history.append(ChatMessage("assistant", message.text or ""))
        else:
            history.append(ChatMessage("assistant", f"(Відповідь майстра) {message.text or ''}"))
    return history


async def _context_lines(st: _State) -> tuple[list[str], list[str], list[str]]:
    """Active bookings, still valid offered slots, and the times of those slots."""
    tz = ZoneInfo(st.cfg.timezone)
    booking_lines = []
    for booking in await active_bookings(
        st.session, st.client.id, st.now, sandbox=st.conversation.is_sandbox
    ):
        service = await st.session.get(Service, booking.service_id)
        if booking.start_at is None:
            booking_lines.append(
                f"#{booking.id}: заявка на {service.name}, бажаний час: "
                f"{booking.requested_time_text}, статус {booking.status}"
            )
        else:
            local = booking.start_at.astimezone(tz)
            booking_lines.append(
                f"#{booking.id}: {service.name}, {day_label(local.date(), 'uk')} о {hhmm(local)}, "
                f"статус {booking.status}"
            )
    slots = list(
        await st.session.scalars(
            select(OfferedSlot)
            .where(
                OfferedSlot.conversation_id == st.conversation.id,
                OfferedSlot.expires_at > st.now,
            )
            .order_by(OfferedSlot.created_at.desc())
            .limit(6)
        )
    )
    slot_lines, times = [], []
    for slot in slots:
        service = await st.session.get(Service, slot.service_id)
        staff = await st.session.get(Staff, slot.staff_id)
        local = slot.start_at.astimezone(tz)
        times.append(hhmm(local))
        slot_lines.append(
            f"{slot.token}: {service.name}, {day_label(local.date(), 'uk')} о {hhmm(local)}, "
            f"майстер {staff.name}"
        )
    return booking_lines, slot_lines, times


async def _model_turn(st: _State, new: list[Message]) -> Turn:
    today = st.now.astimezone(ZoneInfo(st.cfg.timezone)).date()
    public = public_studio_data(st.cfg, today)
    booking_lines, slot_lines, offered_times = await _context_lines(st)
    system = build_system_prompt(
        st.cfg,
        public,
        now=st.now,
        client_bookings=booking_lines,
        offered_slots=slot_lines,
        reply_language=st.lang,
    )
    messages = [ChatMessage("system", system), *await _history(st)]
    tc = TurnContext(
        session=st.session,
        tenant=st.tenant,
        cfg=st.cfg,
        conversation=st.conversation,
        client=st.client,
        provider=st.provider,
        now=st.now,
        lang=st.lang,
        staged=st.staged,
    )
    turn = Turn("", from_template=False)
    tools = tool_specs(st.cfg)
    tool_texts: list[str] = []
    try:
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            response = await st.app.llm.complete(
                messages, tools if round_no < MAX_TOOL_ROUNDS else []
            )
            _count(turn, response, st)
            if response.tool_calls:
                call = response.tool_calls[0]
                messages.append(ChatMessage("assistant", response.content, tool_calls=[call]))
                outcome = await execute_tool(tc, call)
                payload = json.dumps(outcome.result, ensure_ascii=False, default=str)
                tool_texts.append(payload)
                turn.tool_log.append(
                    {"name": call.name, "arguments": call.arguments, "result": outcome.result}
                )
                messages.append(ChatMessage("tool", payload, tool_call_id=call.id))
                offered_times += outcome.offered_times
                turn.events += outcome.events
                if outcome.final_text is not None:
                    turn.text = outcome.final_text
                    turn.from_template = True
                    turn.staged_action = outcome.staged_action
                    turn.handoff = outcome.handoff
                    return turn
                continue
            turn.text = clean_reply(response.content or "")
            break
    except LLMError as exc:
        await st.app.alerts.send(
            "llm_failed", f"Модель недоступна ({type(exc).__name__}); бот відповідає шаблоном."
        )
        return _merge_meta(
            _fallback(st, "other", "Модель недоступна, клієнтці надіслано шаблон."), turn
        )

    if not turn.text:
        return _merge_meta(_fallback(st, "other", "Модель не дала відповіді."), turn)

    client_texts = [m.text for m in new if m.text]
    # R5, R6, R8: the model said a person will answer but did not hand off; make it real.
    if promises_handoff(turn.text):
        reason = "medical" if mentions_health(client_texts) else "other"
        fallback = _fallback(st, reason, "Бот пообіцяв передати діалог людині.")
        fallback.text = render("handoff", st.lang, eta=eta_text(st.cfg, st.now, st.lang))
        return _merge_meta(fallback, turn)

    # R2: amounts, times, phones and links must be known; regenerate once, then hand off.
    facts = build_facts(
        st.cfg,
        json.dumps(public, ensure_ascii=False),
        offered_times=offered_times,
        extra_texts=client_texts + tool_texts,
    )
    problems = find_violations(turn.text, facts)
    if problems:
        log.warning("guard.violation", conversation_id=st.conversation.id, problems=problems)
        messages.append(ChatMessage("assistant", turn.text))
        messages.append(ChatMessage("system", CORRECTION.format(problems=", ".join(problems))))
        try:
            response = await st.app.llm.complete(messages, [])
            _count(turn, response, st)
            retry = clean_reply(response.content or "")
        except LLMError:
            retry = ""
        if not retry or find_violations(retry, facts):
            fallback = _fallback(st, "unknown_fact", "Бот не зміг відповісти перевіреними даними.")
            fallback.text = render("handoff", st.lang, eta=eta_text(st.cfg, st.now, st.lang))
            return _merge_meta(fallback, turn)
        turn.text = retry
    return turn


def _count(turn: Turn, response: Any, st: _State) -> None:
    turn.tokens_in += response.tokens_in
    turn.tokens_out += response.tokens_out
    turn.latency_ms += response.latency_ms
    turn.model = response.model or st.app.llm.model_name


def _merge_meta(target: Turn, source: Turn) -> Turn:
    target.tool_log = source.tool_log
    target.tokens_in, target.tokens_out = source.tokens_in, source.tokens_out
    target.model, target.latency_ms = source.model, source.latency_ms
    target.events = source.events + target.events
    return target


# --- finishing: draft or send ---------------------------------------------------------------


async def _finish(st: _State, plan: ReplyPlan, turn: Turn) -> ReplyPlan:
    body = turn.text if turn.from_template else strip_intro(turn.text)
    cfg, client, conversation = st.cfg, st.client, st.conversation
    privacy = (
        cfg.profile.privacy_url
        and client.privacy_notified_at is None
        and client.phone is None
        and _PHONE_WORDS.search(body)
    )
    if privacy:
        body = f"{body}\n{render('privacy_notice', st.lang, url=str(cfg.profile.privacy_url))}"

    if st.staged:
        action = turn.staged_action
        if action is None and turn.handoff is not None:
            action = {
                "tool": "handoff_to_human",
                "args": {"reason": turn.handoff[0], "summary": turn.handoff[1]},
            }
        await supersede_drafts(st.session, conversation.id)
        draft = Draft(
            tenant_id=st.tenant.id,
            conversation_id=conversation.id,
            text=body,
            proposed_action=action,
            disclose=disclosure_needed(conversation, st.now, cfg.disclosure_silence_days)
            is not None,
            status="pending",
            owner_messages=[],
            created_at=st.now,
        )
        st.session.add(draft)
        await st.session.flush()
        st.session.add(_bot_message(st, None, turn, delivery="draft", draft_id=draft.id))
        plan.status = "drafted"
        plan.draft_id = draft.id
        plan.events.append(DraftCreated(st.tenant.id, draft.id))
        return plan

    disclosure = disclosure_needed(conversation, st.now, cfg.disclosure_silence_days)
    text = f"{render(disclosure, st.lang, studio=cfg.name)} {body}" if disclosure else body
    message = _bot_message(st, text, turn, delivery="pending")
    st.session.add(message)
    await st.session.flush()
    if disclosure:
        conversation.disclosed_at = st.now
    if privacy:
        client.privacy_notified_at = st.now
    conversation.last_bot_message_at = st.now
    if turn.handoff is not None:
        reason, summary = turn.handoff
        start_handoff(conversation, reason, st.now)
        st.session.add(
            AuditLog(
                tenant_id=st.tenant.id,
                actor="bot",
                action="handoff",
                details={"reason": reason, "conversation_id": conversation.id},
                created_at=st.now,
            )
        )
        plan.events.append(HandoffStarted(st.tenant.id, conversation.id, reason, summary))
    plan.events += [
        dataclasses.replace(e, message_id=message.id) if isinstance(e, BookingCreated) else e
        for e in turn.events
    ]
    plan.outgoing.append(Outgoing(message.id))
    return plan


def _bot_message(
    st: _State, text: str | None, turn: Turn, *, delivery: str, draft_id: int | None = None
) -> Message:
    """The bot message row. For drafts it only records tokens and tools (text stays in draft)."""
    log_entries = list(turn.tool_log)
    if draft_id is not None:
        log_entries.append({"draft_id": draft_id})
    return Message(
        tenant_id=st.tenant.id,
        conversation_id=st.conversation.id,
        role="bot" if draft_id is None else "system",
        text=text,
        tool_calls=log_entries or None,
        prompt_version=PROMPT_VERSION,
        config_version=st.tenant.config_version,
        model=turn.model,
        tokens_in=turn.tokens_in,
        tokens_out=turn.tokens_out,
        latency_ms=turn.latency_ms or None,
        delivery_status=delivery,
        created_at=st.now,
    )


async def _plan_after_crash(app: AppContext, conversation_id: int) -> ReplyPlan | None:
    """Something unexpected failed: never leave the client in silence (PRODUCT.md 1.16)."""
    try:
        async with app.session_factory() as session, session.begin():
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                return None
            conversation.reply_due_at = None
            last = await session.scalar(
                select(func.max(Message.id)).where(
                    Message.conversation_id == conversation_id, Message.role == "client"
                )
            )
            conversation.last_processed_message_id = last or conversation.last_processed_message_id
            tenant = await session.get(Tenant, conversation.tenant_id)
            if tenant.status != "active" or conversation.state != "BOT_ACTIVE":
                return ReplyPlan("skipped:crash")
            cfg = studio_config(tenant)
            client = await session.get(Client, conversation.client_id)
            account = await session.get(ChannelAccount, conversation.channel_account_id)
            now = app.clock.now()
            lang = choose_language(
                None, client.language, cfg.allowed_languages, cfg.default_language
            )
            st = _State(
                app,
                session,
                tenant,
                cfg,
                conversation,
                client,
                account,
                provider_for(app.calendars, cfg),
                now,
                lang,
                _is_staged(tenant, conversation, cfg, now, False),
            )
            return await _finish(
                st, ReplyPlan("replied"), _fallback(st, "other", "Технічний збій бота.")
            )
    except Exception:
        log.exception("reply.crash_fallback_failed", conversation_id=conversation_id)
        return None
