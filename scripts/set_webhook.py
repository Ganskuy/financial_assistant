"""Run as python -m scripts.set_webhook https://your-domain.example/telegram/webhook."""

import asyncio
import sys
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.telegram.client import TelegramClient


async def main(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/telegram/webhook"
    ):
        raise SystemExit(
            "Use an HTTPS URL ending in /telegram/webhook, without credentials/query/fragment."
        )
    settings = get_settings()
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
        client = TelegramClient(settings, http)
        await client.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": settings.telegram_webhook_secret.get_secret_value(),
                "allowed_updates": ["message", "callback_query"],
                "max_connections": 4,
                "drop_pending_updates": False,
            },
        )
    print("Webhook configured. Send /help in your allowed private Telegram chat.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m scripts.set_webhook https://host/telegram/webhook")
    try:
        asyncio.run(main(sys.argv[1]))
    except Exception as exc:
        raise SystemExit(
            f"Webhook configuration failed: {type(exc).__name__}. Check credentials and network."
        ) from None
