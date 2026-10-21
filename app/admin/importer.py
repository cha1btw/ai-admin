"""AI import: raw studio notes (questionnaire answers, price list, Instagram bio) to a config.

The result is a draft for a founder to review in the editor, never saved automatically. Every
amount in the config is checked against the source text, so a made-up price is flagged (R2).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.admin.service import validate_config
from app.llm.base import ChatMessage, LLMError, LLMProvider
from app.tenants.schema import StudioConfig

MAX_SOURCE_CHARS = 30_000

SYSTEM = """You turn raw notes about a service business (usually a beauty studio in Ukraine) into a JSON config for an AI receptionist. The JSON must match this JSON Schema exactly (no extra keys):

{schema}

Rules:
- Copy facts exactly: prices, durations, hours, address, phone, links, payment details. Never invent or estimate a fact that is not in the notes.
- If a required fact is missing, use the safest placeholder and add a question for the founder to "questions" (in Ukrainian). Examples: no staff listed -> one staff member with id "master" and name "Майстер"; unknown duration -> 60 and a question; no address -> "уточнити" and a question.
- Prices: one amount -> "fixed"; "від 500" -> "from"; "500-700" -> "range"; "за консультацією" or no price -> "on_request" with no amounts.
- ids: lowercase latin transliteration with underscores, e.g. "Манікюр з покриттям" -> "manicure_coating".
- Texts for clients (names, FAQ answers, address, how_to_find) stay in the language of the notes; prefer Ukrainian.
- Put private remarks (who is rude, internal prices, anything not for clients) into internal_notes.
- Hours are "HH:MM". A day off is an empty list.
- initial_mode is "draft" unless the notes say otherwise. calendar_provider is "internal" unless the notes mention Google Calendar (then "google") or that the master confirms every booking by hand (then "lead_capture").
{base_rule}
Answer with one JSON object and nothing else: {{"config": {{...}}, "questions": ["..."]}}"""

BASE_RULE = """- You get the CURRENT config and notes with CHANGES. Return the full updated config: keep everything the notes do not change, keep ids of existing services and staff."""

FIX = """The config failed validation:
{errors}
Return the corrected full JSON object in the same format."""

_NUMBER = re.compile(r"\d[\d\s]{0,8}\d|\d")


@dataclass
class ImportResult:
    config: dict[str, Any] | None
    errors: list[dict[str, str]] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tokens: int = 0
    stored: dict[str, Any] | None = None  # what saving would store, for an exact diff


def _schema() -> str:
    return json.dumps(StudioConfig.model_json_schema(), ensure_ascii=False, separators=(",", ":"))


def _parse(content: str | None) -> dict[str, Any] | None:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def numbers_in(text: str) -> set[int]:
    return {int(m.group(0).replace(" ", "").replace(" ", "")) for m in _NUMBER.finditer(text)}


def unsupported_amounts(config: dict[str, Any], source: str, base: dict | None) -> list[str]:
    """Prices and prepayments that appear neither in the notes nor in the current config."""
    known = numbers_in(source)
    if base:
        known |= numbers_in(json.dumps(base, ensure_ascii=False))
    warnings = []
    for service in config.get("services") or []:
        for key in ("price_min", "price_max", "prepayment_amount"):
            value = service.get(key)
            if isinstance(value, int) and value not in known:
                name = service.get("name") or service.get("id")
                warnings.append(f"{name}: {key}={value} немає в тексті, перевірте")
    default = (config.get("prepayment") or {}).get("default_amount")
    if isinstance(default, int) and default not in known:
        warnings.append(f"передоплата {default} немає в тексті, перевірте")
    return warnings


async def import_studio(
    llm: LLMProvider, source: str, *, base: dict[str, Any] | None = None
) -> ImportResult:
    source = source.strip()[:MAX_SOURCE_CHARS]
    system = SYSTEM.format(schema=_schema(), base_rule=BASE_RULE if base else "")
    user = (
        source
        if base is None
        else (
            "CURRENT config:\n" + json.dumps(base, ensure_ascii=False) + "\n\nCHANGES:\n" + source
        )
    )
    messages = [ChatMessage("system", system), ChatMessage("user", user)]
    result = ImportResult(config=None)
    for attempt in range(2):
        try:
            response = await llm.complete(messages, [])
        except LLMError as exc:
            result.errors = [{"loc": "model", "msg": f"модель недоступна: {exc}"}]
            return result
        result.tokens += response.tokens_in + response.tokens_out
        data = _parse(response.content)
        if data is None:
            result.errors = [{"loc": "model", "msg": "модель повернула не JSON"}]
            messages += [
                ChatMessage("assistant", response.content or ""),
                ChatMessage("user", "Answer with the JSON object only."),
            ]
            continue
        config = data.get("config") if isinstance(data.get("config"), dict) else data
        result.config = config
        result.questions = [str(q) for q in data.get("questions") or [] if q][:20]
        cfg, errors = validate_config(config)
        result.errors = errors
        if cfg is not None:
            result.config = cfg.model_dump(mode="json", exclude_none=True)
            result.stored = cfg.model_dump(mode="json")
            break
        if attempt == 0:
            listed = "\n".join(f"- {e['loc']}: {e['msg']}" for e in errors)
            messages += [
                ChatMessage("assistant", response.content or ""),
                ChatMessage("user", FIX.format(errors=listed)),
            ]
    if result.config is not None:
        result.warnings = unsupported_amounts(result.config, source, base)
    return result
