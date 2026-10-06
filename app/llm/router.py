from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings

Role = Literal["vision", "extraction", "advisor"]


@dataclass(frozen=True)
class ModelProfile:
    model: str
    max_output: int
    image_allowance: int = 0


# Reviewed byte-token profiles: no auto-router or arbitrary unreviewed tokenizer.
# Adding another gateway model requires verifying its input envelope and output cap.
TEXT_PROFILES = frozenset({"openai/gpt-5.4-nano", "openai/gpt-5.4", "openai/gpt-5.4-mini"})


def profile(settings: Settings, role: Role) -> ModelProfile:
    model = getattr(settings, f"openrouter_{role}_model")
    if model not in TEXT_PROFILES:
        raise ValueError(
            "Model lacks a reviewed token-bound profile; add and test a profile before enabling it"
        )
    if role == "vision" and model != "openai/gpt-5.4-nano":
        raise ValueError("Only GPT-5.4 Nano has a reviewed vision profile")
    return ModelProfile(
        model, getattr(settings, f"{role}_max_output_tokens"), 4096 if role == "vision" else 0
    )
