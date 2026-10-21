"""Application settings loaded from environment variables (.env in development)."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: "ALERT_CHAT_ID=" copied from .env.example means "not set", not "".
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+asyncpg://ai_admin:ai_admin@localhost:5435/ai_admin"

    # LLM
    llm_provider: Literal["openai", "fake"] = "openai"
    llm_model: str = ""
    llm_temperature: float | None = None  # some models accept only the default temperature
    llm_max_completion_tokens: int = 600
    llm_timeout_s: float = 25.0
    openai_api_key: SecretStr | None = None
    # Any OpenAI-compatible Chat Completions API; empty means OpenAI itself. Examples:
    # Gemini https://generativelanguage.googleapis.com/v1beta/openai/, Ollama http://localhost:11434/v1/
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None  # key of that API; falls back to OPENAI_API_KEY

    # Telegram
    owner_bot_token: SecretStr | None = None
    demo_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    admin_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    alert_chat_id: int | None = None
    demo_owner_chat_id: int | None = None
    demo_tenant_slug: str = "demo_studio"

    # Meta / Instagram
    meta_app_id: str | None = None
    meta_app_secret: SecretStr | None = None
    meta_verify_token: SecretStr | None = None
    instagram_api_version: str = "v26.0"

    # Google Calendar
    google_service_account_file: str | None = None

    # Security and public URLs
    encryption_key: SecretStr | None = None
    public_base_url: str | None = None

    # Admin panel and landing
    admin_session_hours: int = 12
    landing_origin: str | None = None  # e.g. https://ai-admin.vercel.app (CORS for /leads)

    # Cost estimate in /stats and the admin panel (USD per 1M tokens of the chosen model)
    llm_price_input_per_m: float | None = None
    llm_price_output_per_m: float | None = None

    # Pipeline tuning
    debounce_seconds: float = 4.0
    worker_poll_interval_s: float = 0.5

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(" ", "").split(",") if part]
        return value

    @property
    def instagram_redirect_uri(self) -> str | None:
        if not self.public_base_url:
            return None
        return self.public_base_url.rstrip("/") + "/oauth/instagram/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
