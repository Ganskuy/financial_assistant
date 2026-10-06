import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.llm.token_budget import TokenBudget


@pytest.mark.parametrize("limit", [1, 20000, 100000])
def test_settings_and_budget_accept_supported_limits(settings, limit):
    configured = Settings(**(settings.model_dump() | {"daily_llm_token_limit": limit}))
    assert TokenBudget(None, configured.daily_llm_token_limit).limit == limit


@pytest.mark.parametrize("limit", [0, -1, 100001])
def test_settings_and_budget_reject_out_of_range_limits(settings, limit):
    with pytest.raises(ValidationError):
        Settings(**(settings.model_dump() | {"daily_llm_token_limit": limit}))
    with pytest.raises(ValueError):
        TokenBudget(None, limit)


def test_default_limit_is_100000(settings, monkeypatch):
    monkeypatch.delenv("DAILY_LLM_TOKEN_LIMIT", raising=False)
    values = settings.model_dump(exclude={"daily_llm_token_limit"})
    assert Settings(**values).daily_llm_token_limit == 100000
    assert TokenBudget(None).limit == 100000
