import pytest
from pydantic import ValidationError

from app.tenants.public import public_studio_data
from app.tenants.schema import StudioConfig
from tests.helpers import demo_config, demo_config_dict, kyiv


def test_demo_config_is_valid() -> None:
    cfg = demo_config()
    assert cfg.slug == "demo_studio"
    assert cfg.service("gel_manicure").price_max == 900  # fixed price fills price_max
    assert [m.id for m in cfg.staff_for_service("art_design")] == ["olena"]


@pytest.mark.parametrize(
    ("service_patch", "message"),
    [
        ({"price_type": "fixed", "price_min": None}, "fixed price needs price_min"),
        ({"price_type": "from", "price_min": 100, "price_max": 200}, "only price_min"),
        ({"price_type": "range", "price_min": 300, "price_max": 200}, "greater than"),
        ({"price_type": "on_request", "price_min": 100}, "must not have amounts"),
    ],
)
def test_price_rules(service_patch: dict, message: str) -> None:
    data = demo_config_dict()
    data["services"][0].update(service_patch)
    with pytest.raises(ValidationError, match=message):
        StudioConfig.model_validate(data)


def test_staff_must_reference_existing_services() -> None:
    data = demo_config_dict()
    data["staff"][0]["services"].append("unknown_service")
    with pytest.raises(ValidationError, match="unknown services"):
        StudioConfig.model_validate(data)


def test_prepayment_requires_requisites_and_amounts() -> None:
    data = demo_config_dict()
    data["prepayment"] = {"required": True, "default_amount": 200}
    with pytest.raises(ValidationError, match="requisites_text"):
        StudioConfig.model_validate(data)
    data["prepayment"]["requisites_text"] = "Картка 0000 0000 0000 0000, Олена"
    cfg = StudioConfig.model_validate(data)
    assert cfg.prepayment_for(cfg.service("gel_manicure")) == 200


def test_unknown_language_and_timezone_rejected() -> None:
    data = demo_config_dict()
    data["allowed_languages"] = ["uk", "de"]
    with pytest.raises(ValidationError, match="unsupported languages"):
        StudioConfig.model_validate(data)
    data = demo_config_dict()
    data["timezone"] = "Europe/Atlantis"
    with pytest.raises(ValidationError, match="unknown timezone"):
        StudioConfig.model_validate(data)


def test_overlapping_hours_rejected() -> None:
    data = demo_config_dict()
    data["hours"]["mon"] = [
        {"start": "10:00", "end": "14:00"},
        {"start": "13:00", "end": "18:00"},
    ]
    with pytest.raises(ValidationError, match="overlap"):
        StudioConfig.model_validate(data)


def test_unknown_fields_rejected() -> None:
    data = demo_config_dict()
    data["profile"]["adress"] = "typo"
    with pytest.raises(ValidationError, match="adress"):
        StudioConfig.model_validate(data)


def test_public_data_hides_internal_fields() -> None:
    data = demo_config_dict()
    data["prepayment"] = {
        "required": True,
        "default_amount": 200,
        "requisites_text": "SECRET-CARD-0000",
    }
    cfg = StudioConfig.model_validate(data)
    public = public_studio_data(cfg, kyiv("2026-10-21 10:00").date())
    dumped = repr(public)
    assert "SECRET-CARD-0000" not in dumped
    assert "Публічна демо-студія" not in dumped  # internal_notes
    assert "daily_token_budget" not in dumped
    services = {s["id"]: s for s in public["services"]}
    assert services["gel_manicure"]["price"] == "900 грн"
    assert services["nail_extension"]["price"] == "від 1300 грн"
    assert services["art_design"]["price"] == "за консультацією"
    assert public["working_hours"]["неділя"] == "вихідний"
    # Upcoming special days are listed with their weekday.
    # Holidays beyond the booking horizon are listed too: clients ask about New Year early.
    assert public["special_days"] == [
        {
            "date": "четвер, 31.12",
            "hours": "10:00–15:00",
            "note": "Передноворічний скорочений день",
        },
        {"date": "п'ятниця, 01.01", "hours": "вихідний", "note": "Новий рік"},
    ]
