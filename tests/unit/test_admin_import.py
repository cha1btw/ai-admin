import copy
import json

from app.admin.importer import import_studio, unsupported_amounts
from app.admin.service import config_diff
from app.llm.base import LLMError
from app.llm.fake import FakeLLM, say
from tests.helpers import demo_config_dict

NOTES = (
    "Манікюр з гель-лаком 900 грн, без покриття 450, педикюр 1100, нарощування від 1300, зняття 200"
)


def answer(config: dict, questions: list[str] | None = None) -> str:
    return "```json\n" + json.dumps({"config": config, "questions": questions or []}) + "\n```"


async def test_invalid_answer_gets_one_fix_round() -> None:
    broken = copy.deepcopy(demo_config_dict())
    broken["services"][0].pop("price_min")
    llm = FakeLLM([say(answer(broken)), say(answer(demo_config_dict(), ["Адреса?"]))])
    result = await import_studio(llm, NOTES)
    assert result.errors == []
    assert result.questions == ["Адреса?"]
    assert "price_min" in llm.calls[1][0][-1].content
    assert result.tokens > 0


async def test_still_invalid_config_is_returned_with_errors() -> None:
    broken = copy.deepcopy(demo_config_dict())
    broken["staff"] = []
    llm = FakeLLM([say(answer(broken)), say(answer(broken))])
    result = await import_studio(llm, NOTES)
    assert result.config is not None  # the founder fixes it in the editor
    assert result.errors and result.errors[0]["loc"] == "staff"


async def test_model_failure_is_reported() -> None:
    result = await import_studio(FakeLLM([LLMError("timeout")]), NOTES)
    assert result.config is None
    assert "timeout" in result.errors[0]["msg"]


def test_amounts_missing_from_the_notes_are_flagged() -> None:
    config = copy.deepcopy(demo_config_dict())
    config["services"][0]["price_min"] = 990
    warnings = unsupported_amounts(config, NOTES, None)
    assert warnings == ["Манікюр з гель-лаком: price_min=990 немає в тексті, перевірте"]
    # When updating, amounts already in the current config are fine.
    assert unsupported_amounts(config, "манікюр 990", demo_config_dict()) == []


def test_diff_matches_services_by_id_and_ignores_order() -> None:
    old = demo_config_dict()
    new = copy.deepcopy(old)
    new["services"].reverse()
    assert config_diff(old, new) == []
    new["services"][0]["duration_min"] = 45
    new["faq"] = []
    paths = [change["path"] for change in config_diff(old, new)]
    assert "services[art_design].duration_min" in paths
    assert "faq" in paths
