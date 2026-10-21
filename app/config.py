"""Application settings loaded from environment variables (.env in development)."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+asyncpg://ai_admin:ai_admin@localhost:5435/ai_admin"

    # LLM
    llm_provider: Literal["openai", "fake"] = "openai"
    llm_model: str = ""
    llm_temperature: float | None = None  # some models accept only the default temperature
    llm_max_completion_tokens: int = 600
    llm_timeout_s: float = 25.0
    openai_api_key: SecretStr | None = None

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
