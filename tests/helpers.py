"""Shared test helpers (no fixtures here, so unit tests can import them freely)."""

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.tenants.schema import StudioConfig

ROOT = Path(__file__).resolve().parent.parent
DEMO_CONFIG_PATH = ROOT / "studios" / "demo_studio.json"
KYIV = ZoneInfo("Europe/Kyiv")


def demo_config_dict() -> dict[str, Any]:
    return json.loads(DEMO_CONFIG_PATH.read_text(encoding="utf-8"))


def demo_config(**overrides: Any) -> StudioConfig:
    """Demo studio config with top-level keys replaced (nested dicts are replaced as a whole)."""
    data = copy.deepcopy(demo_config_dict())
    data.update(overrides)
    return StudioConfig.model_validate(data)


def kyiv(text: str) -> datetime:
    """'2026-10-20 23:47' in Kyiv local time -> aware datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=KYIV)
