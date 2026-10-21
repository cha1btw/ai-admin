"""Expectation checks for eval turns. Deterministic where possible; `judge` is the only soft one."""

from dataclasses import dataclass, field
from typing import Any

from app.brain.guardrails import amounts_in, times_in
from app.brain.language import detect_language
from app.tenants.schema import StudioConfig

DISCLOSURE_MARKERS = ("AI-асистент", "AI-ассистент", "AI assistant", "asystentem AI")
SOFT_KEYS = {"judge"}


@dataclass
class TurnResult:
    replies: list[str]
    tools: list[str]
    state: str
    handoff_reason: str | None
    bookings: list[dict[str, Any]]  # created during this turn
    offered_times: list[str]
    first_reply_of_conversation: str | None
    drafted: bool = False


@dataclass
class CheckOutcome:
    key: str
    passed: bool
    detail: str = ""
    soft: bool = False


@dataclass
class TurnReport:
    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def hard_failed(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if not o.passed and not o.soft]


def _allowed_amounts(cfg: StudioConfig) -> set[int]:
    amounts = set()
    for service in cfg.services:
        for value in (service.price_min, service.price_max, cfg.prepayment_for(service)):
            if value is not None:
                amounts.add(value)
    for faq in cfg.faq:
        amounts.update(amounts_in(faq.a))
    fixed = [s.price_min for s in cfg.services if s.price_min is not None]
    amounts.update(a + b for i, a in enumerate(fixed) for b in fixed[i + 1 :])
    return amounts


def _hours_times(cfg: StudioConfig) -> set[str]:
    times = set()
    for weekly in (cfg.hours, cfg.effective_owner_hours):
        for day in range(7):
            for rng in weekly.for_weekday(day):
                times.update({f"{rng.start:%H:%M}", f"{rng.end:%H:%M}"})
    return times


def check_turn(
    expect: dict[str, Any], result: TurnResult, cfg: StudioConfig, client_text: str
) -> TurnReport:
    report = TurnReport()
    reply = " ".join(result.replies)

    def add(key: str, passed: bool, detail: str = "") -> None:
        report.outcomes.append(CheckOutcome(key, passed, detail, soft=key in SOFT_KEYS))

    for key, value in expect.items():
        if key == "tools_called":
            missing = [t for t in value if t not in result.tools]
            add(key, not missing, f"called {result.tools}, missing {missing}")
        elif key == "tools_called_not":
            present = [t for t in value if t in result.tools]
            add(key, not present, f"called {result.tools}")
        elif key == "first_bot_message_discloses_ai":
            first = result.first_reply_of_conversation or ""
            add(key, any(m in first for m in DISCLOSURE_MARKERS) == bool(value), first[:80])
        elif key == "prices_in_reply_match_db":
            wrong = [a for a in amounts_in(reply) if a not in _allowed_amounts(cfg)]
            add(key, not wrong, f"unknown amounts {wrong}")
        elif key == "slot_times_in_reply_offered":
            allowed = set(result.offered_times) | _hours_times(cfg) | set(times_in(client_text))
            wrong = [t for t in times_in(reply) if t not in allowed]
            add(key, not wrong, f"unknown times {wrong}, offered {result.offered_times}")
        elif key == "booking_created":
            add(key, bool(result.bookings) == bool(value), f"bookings {result.bookings}")
        elif key == "booking_price_equals_db":
            bad = []
            for booking in result.bookings:
                service = cfg.service(booking["service"])
                if service is None or booking["price_min"] != service.price_min:
                    bad.append(booking)
            add(key, bool(result.bookings) and not bad, f"bookings {result.bookings}")
        elif key == "state_after":
            add(key, result.state == value, f"state {result.state}")
        elif key == "handoff_reason":
            reasons = value if isinstance(value, list) else [value]
            add(key, result.handoff_reason in reasons, f"reason {result.handoff_reason}")
        elif key == "reply_contains":
            missing = [s for s in value if s.lower() not in reply.lower()]
            add(key, not missing, f"missing {missing}")
        elif key == "reply_contains_any":
            add(key, any(s.lower() in reply.lower() for s in value), reply[:120])
        elif key == "reply_not_contains":
            present = [s for s in value if s.lower() in reply.lower()]
            add(key, not present, f"present {present}")
        elif key == "reply_language":
            detected = detect_language(reply)
            add(key, detected == value, f"detected {detected}")
        elif key == "reply_max_chars":
            add(key, len(reply) <= value, f"{len(reply)} chars")
        elif key == "no_reply":
            add(key, (not result.replies) == bool(value), f"replies {len(result.replies)}")
        elif key == "judge":
            continue  # evaluated by the runner with a model call
        else:
            add(key, False, "unknown expectation key")
    return report
