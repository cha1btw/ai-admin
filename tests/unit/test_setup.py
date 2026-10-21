from app.setup import clean_secret, describe, env_value, set_env_values

ENV = """# comment
OWNER_BOT_TOKEN=
LLM_MODEL=gemini-2.5-flash
OPENAI_API_KEY=
"""


def test_replaces_existing_lines_and_keeps_the_rest() -> None:
    text = set_env_values(ENV, {"OWNER_BOT_TOKEN": "123:abc", "OPENAI_API_KEY": "sk-1"})
    assert text.splitlines() == [
        "# comment",
        "OWNER_BOT_TOKEN=123:abc",
        "LLM_MODEL=gemini-2.5-flash",
        "OPENAI_API_KEY=sk-1",
    ]


def test_appends_missing_keys() -> None:
    text = set_env_values(ENV, {"ADMIN_TELEGRAM_IDS": "42"})
    assert text.endswith("OPENAI_API_KEY=\nADMIN_TELEGRAM_IDS=42\n")
    assert env_value(text, "ADMIN_TELEGRAM_IDS") == "42"


def test_values_with_special_characters_are_written_verbatim() -> None:
    value = r"a\1b$c=d"  # regex backreferences and shell symbols must not be interpreted
    text = set_env_values(ENV, {"LLM_API_KEY": value, "OWNER_BOT_TOKEN": value})
    assert env_value(text, "LLM_API_KEY") == value
    assert env_value(text, "OWNER_BOT_TOKEN") == value
    assert env_value(text, "LLM_MODEL") == "gemini-2.5-flash"


def test_pasted_keys_lose_terminal_markers_quotes_and_spaces() -> None:
    assert clean_secret("\x1b[200~123456:AAHtoken\x1b[201~\n") == "123456:AAHtoken"
    assert clean_secret('  "AIzaKey123"  ') == "AIzaKey123"
    assert clean_secret("«AIzaKey123»") == "AIzaKey123"


def test_description_shows_only_length_and_prefix() -> None:
    assert describe("123456:secretpart") == "отримано 17 символів, починається з «1234»"
    assert "не той текст" in describe("Привіт, ось мій ключ")
