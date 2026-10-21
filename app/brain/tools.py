"""Tools the model may call (CLAUDE.md, "Инструменты для LLM").

Results are plain data for the model (R7). After a side effect the client sees a code template
(R3), not model text. In draft logic side effects are validated with dry_run and staged for the
master's approval instead of being executed (R12).
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain import booking as bookings
from app.brain.booking import BookingContext, BookingError
from app.brain.events import (
    BookingCancelled,
    BookingCreated,
    BookingRescheduled,
    Event,
    RequestCreated,
)
from app.brain.handoff import HANDOFF_REASONS, eta_text
from app.brain.i18n import day_label, hhmm, price_text
from app.brain.replies import booking_text, cancelled_text, request_text, rescheduled_text
from app.brain.templates import render
from app.db.models import Client, Conversation, Service, Tenant
from app.llm.base import ToolCall, ToolSpec
from app.scheduling.availability import free_slots, offer_slots, staff_for_service
from app.scheduling.base import CalendarProvider
from app.scheduling.slots import horizon_date
from app.tenants.schema import StudioConfig

MAX_RANGE_DAYS = 14


@dataclass
class TurnContext:
    session: AsyncSession
    tenant: Tenant
    cfg: StudioConfig
    conversation: Conversation
    client: Client
    provider: CalendarProvider
    now: datetime
    lang: str
    staged: bool  # draft logic: wait for the master before any side effect

    def booking_ctx(self) -> BookingContext:
        return BookingContext(
            session=self.session,
            tenant=self.tenant,
            cfg=self.cfg,
            provider=self.provider,
            now=self.now,
            conversation=self.conversation,
            client=self.client,
        )


@dataclass
class ToolOutcome:
    result: dict[str, Any]
    final_text: str | None = None  # ends the turn with this code-made reply
    staged_action: dict[str, Any] | None = None
    handoff: tuple[str, str] | None = None  # (reason, summary), applied by the orchestrator
    events: list[Event] = field(default_factory=list)
    offered_times: list[str] = field(default_factory=list)


class ArgumentError(Exception):
    pass


def tool_specs(cfg: StudioConfig) -> list[ToolSpec]:
    service_ids = [s.id for s in cfg.services]
    staff_ids = [m.id for m in cfg.staff]
    specs: list[ToolSpec] = []
    if cfg.calendar_provider != "lead_capture":
        specs += [
            ToolSpec(
                "get_free_slots",
                "Find free time for one service. Returns up to 6 slots, each with slot_id, local "
                "date, weekday and time. Dates are the studio's local dates.",
                {
                    "type": "object",
                    "properties": {
                        "service_id": {"type": "string", "enum": service_ids},
                        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                        "date_to": {
                            "type": "string",
                            "description": "YYYY-MM-DD, at most 14 days after date_from",
                        },
                        "staff_id": {
                            "type": "string",
                            "enum": staff_ids,
                            "description": "Only if the client asked for a specific master.",
                        },
                        "time_from": {"type": "string", "description": "HH:MM, earliest start"},
                        "time_to": {"type": "string", "description": "HH:MM, latest start"},
                    },
                    "required": ["service_id", "date_from", "date_to"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "create_booking",
                "Book a slot_id returned by get_free_slots. The system sets price and duration "
                "and sends the confirmation to the client.",
                {
                    "type": "object",
                    "properties": {
                        "slot_id": {"type": "string"},
                        "client_name": {"type": "string"},
                        "client_phone": {"type": "string"},
                    },
                    "required": ["slot_id", "client_name"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "reschedule_booking",
                "Move one of the client's active bookings to a new slot_id from get_free_slots.",
                {
                    "type": "object",
                    "properties": {
                        "booking_id": {"type": "integer"},
                        "slot_id": {"type": "string"},
                    },
                    "required": ["booking_id", "slot_id"],
                    "additionalProperties": False,
                },
            ),
        ]
    else:
        specs.append(
            ToolSpec(
                "request_booking",
                "Send a booking request to the master, who confirms the exact time. The system "
                "sends the reply to the client.",
                {
                    "type": "object",
                    "properties": {
                        "service_id": {"type": "string", "enum": service_ids},
                        "preferred_time_text": {
                            "type": "string",
                            "description": "Wished day and time in the client's words, "
                            "with the explicit date",
                        },
                        "client_name": {"type": "string"},
                        "client_phone": {"type": "string"},
                    },
                    "required": [
                        "service_id",
                        "preferred_time_text",
                        "client_name",
                        "client_phone",
                    ],
                    "additionalProperties": False,
                },
            )
        )
    specs += [
        ToolSpec(
            "cancel_booking",
            "Cancel one of the client's active bookings (ids from 'Активні записи клієнта').",
            {
                "type": "object",
                "properties": {"booking_id": {"type": "integer"}},
                "required": ["booking_id"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            "handoff_to_human",
            "Pass the conversation to the master and stop answering. Use for anything not in "
            "ДАНІ and for medical questions, complaints, discounts, refunds, special requests.",
            {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "enum": list(HANDOFF_REASONS)},
                    "summary": {
                        "type": "string",
                        "description": "1-2 sentences in Ukrainian for the master: what the "
                        "client wants",
                    },
                },
                "required": ["reason", "summary"],
                "additionalProperties": False,
            },
        ),
    ]
    return specs


# --- argument helpers -------------------------------------------------------------------


def _str(args: dict[str, Any], key: str, *, required: bool = True) -> str | None:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ArgumentError(f"{key} is required")
        return None
    return str(value).strip()


def _int(args: dict[str, Any], key: str) -> int:
    value = args.get(key)
    try:
        return int(str(value).lstrip("#"))
    except (TypeError, ValueError) as exc:
        raise ArgumentError(f"{key} must be an integer") from exc


def _date(value: str, key: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ArgumentError(f"{key} must be YYYY-MM-DD") from exc


def _time(value: str | None, key: str) -> time | None:
    if not value:
        return None
    match = re.fullmatch(r"(\d{1,2})[:.](\d{2})", value.strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ArgumentError(f"{key} must be HH:MM")
    return time(int(match.group(1)), int(match.group(2)))


# --- handlers -----------------------------------------------------------------------------


async def _get_free_slots(tc: TurnContext, args: dict[str, Any]) -> ToolOutcome:
    cfg = tc.cfg
    service_key = _str(args, "service_id")
    service_cfg = cfg.service(service_key)
    if service_cfg is None:
        raise ArgumentError(f"unknown service_id {service_key}")
    today = tc.now.astimezone(ZoneInfo(cfg.timezone)).date()
    date_from = _date(_str(args, "date_from"), "date_from")
    date_to = _date(_str(args, "date_to", required=False) or date_from.isoformat(), "date_to")
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    if date_to < today:
        return ToolOutcome({"error": "date_in_past", "today": day_label(today, "uk")})
    date_from = max(date_from, today)
    date_to = min(date_to, date_from + timedelta(days=MAX_RANGE_DAYS - 1))
    staff_key = _str(args, "staff_id", required=False)
    if staff_key is not None and staff_key not in [
        m.id for m in cfg.staff_for_service(service_key)
    ]:
        masters = [m.name for m in cfg.staff_for_service(service_key)]
        return ToolOutcome({"error": "master_does_not_provide_service", "masters": masters})
    time_from = _time(args.get("time_from"), "time_from")
    time_to = _time(args.get("time_to"), "time_to")

    options = await free_slots(
        tc.session,
        tc.tenant.id,
        cfg,
        tc.provider,
        service_key,
        date_from,
        date_to,
        tc.now,
        staff_key=staff_key,
        time_from=time_from,
        time_to=time_to,
    )
    note = None
    horizon = horizon_date(cfg, tc.now)
    if not options and date_to < horizon:
        options = await free_slots(
            tc.session,
            tc.tenant.id,
            cfg,
            tc.provider,
            service_key,
            date_to + timedelta(days=1),
            min(date_to + timedelta(days=MAX_RANGE_DAYS), horizon),
            tc.now,
            staff_key=staff_key,
            limit=3,
        )
        if options:
            note = "У запитаний час вільних вікон немає. Це найближчі інші варіанти."
    if not options:
        return ToolOutcome(
            {
                "slots": [],
                "no_availability": True,
                "hint": f"Вільних вікон до {day_label(horizon, 'uk')} немає: "
                'виклич handoff_to_human з reason="special_request".',
            }
        )

    service_row = await tc.session.scalar(
        select(Service).where(Service.tenant_id == tc.tenant.id, Service.key == service_key)
    )
    staff_rows = {
        s.key: s for s in await staff_for_service(tc.session, tc.tenant.id, cfg, service_key)
    }
    offered = await offer_slots(
        tc.session, tc.conversation.id, service_row, staff_rows, options, tc.now
    )
    tz = ZoneInfo(cfg.timezone)
    slots = []
    for slot, option in zip(offered, options, strict=True):
        local = option.start.astimezone(tz)
        slots.append(
            {
                "slot_id": slot.token,
                "date": local.date().isoformat(),
                "day": day_label(local.date(), "uk"),
                "time": hhmm(local),
                "master": staff_rows[option.staff_key].name,
            }
        )
    result: dict[str, Any] = {
        "service": service_cfg.name,
        "price": price_text(
            service_cfg.price_type, service_cfg.price_min, service_cfg.price_max, cfg.currency, "uk"
        ),
        "duration_min": service_cfg.duration_min,
        "slots": slots,
    }
    if note:
        result["note"] = note
    return ToolOutcome(result, offered_times=[s["time"] for s in slots])


async def _create_booking(tc: TurnContext, args: dict[str, Any]) -> ToolOutcome:
    token = _str(args, "slot_id")
    view = await bookings.create_booking(
        tc.booking_ctx(),
        token,
        _str(args, "client_name", required=False),
        _str(args, "client_phone", required=False),
        dry_run=tc.staged,
    )
    text = booking_text(tc.cfg, view, tc.lang)
    if tc.staged:
        action = {
            "tool": "create_booking",
            "args": {
                "slot_id": token,
                "client_name": view.client_name,
                "client_phone": view.client_phone,
            },
        }
        return ToolOutcome(
            {"status": "pending_owner_approval"}, final_text=text, staged_action=action
        )
    return ToolOutcome(
        {"status": view.status, "booking_id": view.id},
        final_text=text,
        events=[BookingCreated(tc.tenant.id, view.id)],
    )


async def _request_booking(tc: TurnContext, args: dict[str, Any]) -> ToolOutcome:
    view = await bookings.request_booking(
        tc.booking_ctx(),
        _str(args, "service_id"),
        _str(args, "preferred_time_text"),
        _str(args, "client_name", required=False),
        _str(args, "client_phone", required=False),
        dry_run=tc.staged,
    )
    text = request_text(view, tc.lang, eta_text(tc.cfg, tc.now, tc.lang))
    if tc.staged:
        action = {
            "tool": "request_booking",
            "args": {
                "service_id": view.service_key,
                "preferred_time_text": view.requested_time_text,
                "client_name": view.client_name,
                "client_phone": view.client_phone,
            },
        }
        return ToolOutcome(
            {"status": "pending_owner_approval"}, final_text=text, staged_action=action
        )
    return ToolOutcome(
        {"status": "requested", "booking_id": view.id},
        final_text=text,
        events=[RequestCreated(tc.tenant.id, view.id)],
    )


async def _cancel_booking(tc: TurnContext, args: dict[str, Any]) -> ToolOutcome:
    booking_id = _int(args, "booking_id")
    view = await bookings.cancel_booking(
        tc.booking_ctx(), booking_id, by="client", dry_run=tc.staged
    )
    text = cancelled_text(tc.cfg, view, tc.lang)
    if tc.staged:
        action = {"tool": "cancel_booking", "args": {"booking_id": booking_id}}
        return ToolOutcome(
            {"status": "pending_owner_approval"}, final_text=text, staged_action=action
        )
    return ToolOutcome(
        {"status": "cancelled"},
        final_text=text,
        events=[BookingCancelled(tc.tenant.id, booking_id)],
    )


async def _reschedule_booking(tc: TurnContext, args: dict[str, Any]) -> ToolOutcome:
    booking_id = _int(args, "booking_id")
    token = _str(args, "slot_id")
    old, new = await bookings.reschedule_booking(
        tc.booking_ctx(), booking_id, token, dry_run=tc.staged
    )
    text = rescheduled_text(tc.cfg, new, tc.lang)
    if tc.staged:
        action = {
            "tool": "reschedule_booking",
            "args": {"booking_id": booking_id, "slot_id": token},
        }
        return ToolOutcome(
            {"status": "pending_owner_approval"}, final_text=text, staged_action=action
        )
    return ToolOutcome(
        {"status": "rescheduled", "booking_id": new.id},
        final_text=text,
        events=[BookingRescheduled(tc.tenant.id, old.id, new.id)],
    )


async def _handoff(tc: TurnContext, args: dict[str, Any]) -> ToolOutcome:
    reason = _str(args, "reason", required=False) or "other"
    if reason not in HANDOFF_REASONS:
        reason = "other"
    summary = (_str(args, "summary", required=False) or "")[:500]
    eta = eta_text(tc.cfg, tc.now, tc.lang)
    text = render("handoff_media" if reason == "media" else "handoff", tc.lang, eta=eta)
    if tc.staged:
        action = {"tool": "handoff_to_human", "args": {"reason": reason, "summary": summary}}
        return ToolOutcome(
            {"status": "pending_owner_approval"}, final_text=text, staged_action=action
        )
    return ToolOutcome({"status": "handed_off"}, final_text=text, handoff=(reason, summary))


_HANDLERS = {
    "get_free_slots": _get_free_slots,
    "create_booking": _create_booking,
    "request_booking": _request_booking,
    "cancel_booking": _cancel_booking,
    "reschedule_booking": _reschedule_booking,
    "handoff_to_human": _handoff,
}

_ERROR_HINTS = {
    "slot_unknown": "Бронюй тільки slot_id з get_free_slots або зі списку «Запропоновані слоти».",
    "slot_expired": "Виклич get_free_slots ще раз.",
    "slot_taken": "Цей час уже зайняли. Виклич get_free_slots і запропонуй інші варіанти.",
    "name_required": "Запитай ім'я клієнта.",
    "phone_required": "Запитай номер телефону.",
    "phone_invalid": "Номер виглядає неправильним, попроси перевірити.",
    "too_many_active": "У клієнта вже максимум активних записів; запропонуй перенести чи "
    "скасувати один або виклич handoff_to_human.",
    "booking_not_found": "Такого активного запису в клієнта немає.",
    "not_active": "Цей запис уже не активний.",
    "in_past": "Цей запис уже минув.",
    "calendar_unavailable": 'Календар недоступний: виклич handoff_to_human з reason="other".',
    "time_required": "Запитай бажаний день і час.",
    "service_unknown": "Такої послуги немає в ДАНІ.",
}


async def execute_tool(tc: TurnContext, call: ToolCall) -> ToolOutcome:
    handler = _HANDLERS.get(call.name)
    if handler is None or (
        call.name in ("get_free_slots", "create_booking", "reschedule_booking")
        and tc.cfg.calendar_provider == "lead_capture"
    ):
        return ToolOutcome({"error": "unknown_tool"})
    try:
        return await handler(tc, call.arguments)
    except ArgumentError as exc:
        return ToolOutcome({"error": "invalid_arguments", "detail": str(exc)})
    except BookingError as exc:
        result: dict[str, Any] = {"error": exc.code}
        if exc.code == "late_cancellation":
            result["rule"] = tc.cfg.cancellation.late_policy_text or (
                f"Безкоштовно скасувати можна не пізніше ніж за "
                f"{tc.cfg.cancellation.free_cancel_hours} год."
            )
            result["hint"] = 'Поясни правило й виклич handoff_to_human з reason="refund".'
        elif exc.code in _ERROR_HINTS:
            result["hint"] = _ERROR_HINTS[exc.code]
        return ToolOutcome(result)
