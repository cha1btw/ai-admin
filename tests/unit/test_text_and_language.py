import pytest

from app.brain.language import choose_language, detect_language
from app.brain.phones import normalize_phone
from app.core.text import clean_reply, split_message, utf8_len


def test_short_text_is_not_split() -> None:
    assert split_message("Добрий вечір!", max_bytes=1000) == ["Добрий вечір!"]


def test_cyrillic_is_split_by_bytes_not_characters() -> None:
    sentence = "Манікюр з гель-лаком коштує 900 грн і триває приблизно дві години. "
    text = sentence * 20  # ~1300 characters, ~2400 bytes
    assert len(text) < 2000 < utf8_len(text)
    parts = split_message(text, max_bytes=1000)
    assert len(parts) >= 3
    assert all(utf8_len(p) <= 1000 for p in parts)
    assert " ".join(parts).split() == text.split()  # nothing lost or cut mid-word


def test_one_huge_word_is_cut_without_breaking_characters() -> None:
    parts = split_message("ї" * 700, max_bytes=1000)
    assert all(utf8_len(p) <= 1000 for p in parts)
    assert "".join(parts) == "ї" * 700


def test_clean_reply_removes_markdown() -> None:
    assert clean_reply("**Ціна:** `900 грн`\n\n\n\nДякуємо") == "Ціна: 900 грн\n\nДякуємо"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("067 123 45 67", "+380671234567"),
        ("+38 (067) 123-45-67", "+380671234567"),
        ("380671234567", "+380671234567"),
        ("80671234567", "+380671234567"),
        ("671234567", "+380671234567"),
        ("+48 512 345 678", "+48512345678"),
        ("0048512345678", "+48512345678"),
        ("+3806712345", None),  # Ukrainian number with missing digits
        ("12345", None),
        ("телефон", None),
        ("", None),
    ],
)
def test_normalize_phone(raw: str, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Скільки коштує манікюр?", "uk"),
        ("Добрий вечір! Є вікно на завтра?", "uk"),
        ("Сколько стоит маникюр с покрытием?", "ru"),
        ("Здравствуйте, можно записаться?", "ru"),
        ("Dzień dobry, ile kosztuje manicure?", "pl"),
        ("Hi, how much is a gel manicure?", "en"),
        ("Хочу записатися", "uk"),
        ("Можна на завтра?", "uk"),
        ("Сколько стоит педикюр?", "ru"),
        ("Хочу на педикюр", None),  # same words in both languages: keep the previous one
        ("Так", None),
        ("👍", None),
    ],
)
def test_detect_language(text: str, expected: str | None) -> None:
    assert detect_language(text) == expected


def test_choose_language_respects_allowed_list() -> None:
    assert choose_language("ru", None, ["uk"], "uk") == "uk"
    assert choose_language("en", None, ["uk", "en"], "uk") == "en"
    assert choose_language(None, "pl", ["uk", "pl"], "uk") == "pl"


def test_empty_env_values_mean_unset(tmp_path, monkeypatch) -> None:
    from app.config import Settings

    env = tmp_path / ".env"
    env.write_text("LLM_TEMPERATURE=\nALERT_CHAT_ID=\nADMIN_TELEGRAM_IDS=1, 2\n")
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.llm_temperature is None and settings.alert_chat_id is None
    assert settings.admin_telegram_ids == [1, 2]


async def test_runtime_without_model_key_still_builds() -> None:
    from app.config import Settings
    from app.llm.base import LLMError
    from app.runtime import Bots, build_context

    settings = Settings(_env_file=None, llm_provider="openai", llm_model="", openai_api_key=None)
    app = build_context(settings, session_factory=None, bots=Bots())
    assert app.llm.model_name == "unavailable"
    import pytest

    with pytest.raises(LLMError):
        await app.llm.complete([], [])
