from pathlib import Path

import yaml

from tests.evals.checks import TurnResult, check_turn
from tests.evals.runner import build_config
from tests.helpers import ROOT, demo_config


def _result(**kwargs) -> TurnResult:
    base = dict(
        replies=["Вітаю! Я AI-асистент студії «Demo Nail Studio». Коштує 900 грн, є 15:30."],
        tools=["get_free_slots"],
        state="BOT_ACTIVE",
        handoff_reason=None,
        bookings=[],
        offered_times=["15:30"],
        first_reply_of_conversation="Вітаю! Я AI-асистент студії «Demo Nail Studio».",
    )
    base.update(kwargs)
    return TurnResult(**base)


def test_deterministic_checks_pass_and_fail() -> None:
    cfg = demo_config()
    expect = {
        "tools_called": ["get_free_slots"],
        "tools_called_not": ["create_booking"],
        "first_bot_message_discloses_ai": True,
        "prices_in_reply_match_db": True,
        "slot_times_in_reply_offered": True,
        "reply_language": "uk",
    }
    assert check_turn(expect, _result(), cfg, "").hard_failed == []
    bad = _result(replies=["Для вас 750 грн о 17:45."], tools=["create_booking"])
    failed = {o.key for o in check_turn(expect, bad, cfg, "").hard_failed}
    assert failed == {
        "tools_called",
        "tools_called_not",
        "prices_in_reply_match_db",
        "slot_times_in_reply_offered",
        "reply_language",  # too short to tell Ukrainian from Russian
    }


def test_booking_price_check_uses_config() -> None:
    cfg = demo_config()
    ok = _result(bookings=[{"service": "gel_manicure", "price_min": 900, "status": "confirmed"}])
    wrong = _result(bookings=[{"service": "gel_manicure", "price_min": 90, "status": "confirmed"}])
    assert not check_turn({"booking_price_equals_db": True}, ok, cfg, "").hard_failed
    assert check_turn({"booking_price_equals_db": True}, wrong, cfg, "").hard_failed


def test_unknown_expectation_is_reported() -> None:
    report = check_turn({"no_such_key": True}, _result(), demo_config(), "")
    assert report.hard_failed[0].detail == "unknown expectation key"


def test_all_cases_load_and_their_configs_validate() -> None:
    cases_dir = ROOT / "tests" / "evals" / "cases"
    cases = [
        c for p in sorted(Path(cases_dir).glob("*.yaml")) for c in yaml.safe_load(p.read_text())
    ]
    assert len(cases) >= 20
    assert len({c["id"] for c in cases}) == len(cases)
    for case in cases:
        build_config(case)  # raises on an invalid config override
        assert case["turns"] and all("expect" in t for t in case["turns"])
