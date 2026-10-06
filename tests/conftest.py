import os

import pytest

from app.core.config import Settings
from app.services.validation import today


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        app_env="test",
        app_secret_key="a" * 40,
        telegram_webhook_secret="b" * 40,
        telegram_bot_token="123:test-token",
        telegram_allowed_user_ids="1001,1002",
        openrouter_api_key="test-key",
        database_url=os.environ.get(
            "TEST_DATABASE_URL", "postgresql+asyncpg://unused@127.0.0.1/unused"
        ),
    )


@pytest.fixture
def draft():
    return {
        "intent": "expense",
        "transaction_date": today().isoformat(),
        "merchant": "Point Coffee",
        "description": "Coffee",
        "amount": 25000,
        "currency": "IDR",
        "category": "food",
        "confidence": 0.98,
        "receipt": None,
    }
