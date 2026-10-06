from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_DAILY_LLM_TOKENS = 100_000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )
    app_env: Literal["development", "test", "production"] = "development"
    app_secret_key: SecretStr
    app_timezone: Literal["Asia/Jakarta"] = "Asia/Jakarta"
    database_url: SecretStr
    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr
    telegram_allowed_user_ids: str
    openrouter_api_key: SecretStr
    openrouter_base_url: Literal["https://openrouter.ai/api/v1"] = "https://openrouter.ai/api/v1"
    openrouter_vision_model: str = "openai/gpt-5.4-nano"
    openrouter_extraction_model: str = "openai/gpt-5.4-nano"
    openrouter_advisor_model: str = "openai/gpt-5.4"
    openrouter_timeout_seconds: float = Field(default=45, ge=1, le=90)
    openrouter_max_attempts: int = Field(default=2, ge=1, le=2)
    daily_llm_token_limit: int = Field(
        default=MAX_DAILY_LLM_TOKENS, ge=1, le=MAX_DAILY_LLM_TOKENS
    )
    vision_max_output_tokens: int = Field(default=1800, ge=128, le=3000)
    extraction_max_output_tokens: int = Field(default=1600, ge=128, le=3000)
    advisor_max_output_tokens: int = Field(default=1200, ge=128, le=3000)
    max_receipt_file_size_mb: int = Field(default=5, ge=1, le=10)
    pending_transaction_ttl_minutes: int = Field(default=30, ge=1, le=1440)
    rate_limit_per_minute: int = Field(default=20, ge=1, le=100)
    worker_count: int = Field(default=2, ge=1, le=4)
    max_update_age_hours: int = Field(default=24, ge=1, le=48)

    @field_validator("app_secret_key", "telegram_webhook_secret")
    @classmethod
    def strong_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < 32 or not all(c.isalnum() or c in "_-" for c in raw):
            raise ValueError("Use at least 32 ASCII letters, digits, hyphens or underscores")
        if not raw.isascii():
            raise ValueError("Secret must be ASCII")
        return value

    @field_validator("telegram_bot_token", "openrouter_api_key")
    @classmethod
    def not_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Credential is required")
        return value

    @model_validator(mode="after")
    def validate_configuration(self) -> "Settings":
        if not self.database_url.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use postgresql+asyncpg://")
        if not self.allowed_users:
            raise ValueError("Explicit TELEGRAM_ALLOWED_USER_IDS is required")
        return self

    @property
    def allowed_users(self) -> frozenset[int]:
        values = frozenset(
            int(x.strip()) for x in self.telegram_allowed_user_ids.split(",") if x.strip()
        )
        if any(x <= 0 for x in values):
            raise ValueError("Telegram user IDs must be positive")
        return values


@lru_cache
def get_settings() -> Settings:
    load_dotenv(override=False)
    return Settings()
