"""Where the bot's /admin link points: PUBLIC_BASE_URL, else the running tunnel."""

from app.admin import auth
from app.config import Settings


def settings(**values) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://unused",
        encryption_key="unit-test-key-not-a-secret",
        **values,
    )


def test_public_base_prefers_the_setting(tmp_path, monkeypatch) -> None:
    file = tmp_path / ".public_url"
    file.write_text("https://tunnel.trycloudflare.com")
    monkeypatch.setattr(auth, "PUBLIC_URL_FILE", file)
    assert auth.public_base(settings(public_base_url="https://admin.example.com/")) == (
        "https://admin.example.com"
    )


def test_public_base_reads_the_tunnel_file(tmp_path, monkeypatch) -> None:
    file = tmp_path / ".public_url"
    monkeypatch.setattr(auth, "PUBLIC_URL_FILE", file)
    assert auth.public_base(settings()) is None  # no tunnel running
    file.write_text("https://tunnel.trycloudflare.com\n")
    assert auth.public_base(settings()) == "https://tunnel.trycloudflare.com"
    link = auth.login_link(settings(), 42)
    assert link.startswith("https://tunnel.trycloudflare.com/admin/login?token=")
    file.write_text("http://insecure.example.com")  # only https addresses are handed out
    assert auth.public_base(settings()) is None
