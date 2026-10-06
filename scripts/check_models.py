"""Check selected OpenRouter model availability without making a generation call."""

import asyncio

import httpx

from app.core.config import get_settings
from app.llm.router import profile


async def main() -> None:
    settings = get_settings()
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20) as http:
        response = await http.get(settings.openrouter_base_url + "/models")
        response.raise_for_status()
    models = {row["id"]: row for row in response.json()["data"]}
    for role in ("vision", "extraction", "advisor"):
        selected = profile(settings, role)
        model = models.get(selected.model)
        if not model:
            raise SystemExit(f"{role}: configured model is not in OpenRouter's current catalog")
        parameters = set(model.get("supported_parameters", []))
        if not {"response_format", "max_tokens"} <= parameters:
            raise SystemExit(f"{role}: required structured output/output cap unavailable")
        if role == "vision" and "image" not in model.get("architecture", {}).get(
            "input_modalities", []
        ):
            raise SystemExit("vision: image input unavailable")
        print(f"{role}: {selected.model} catalog checks passed")
    print(
        "Catalog availability does not prove account/provider access. Run staging acceptance before release."
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except httpx.HTTPError as exc:
        raise SystemExit(f"Model catalog unavailable: {type(exc).__name__}") from None
