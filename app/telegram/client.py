import asyncio
import base64
import binascii
import io
import re
import warnings

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import Settings
from app.core.errors import InvalidInput, TelegramUnavailable


class TelegramClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings, self.http = settings, http
        self.root = "https://api.telegram.org/bot" + settings.telegram_bot_token.get_secret_value()

    async def call(self, method: str, payload: dict) -> dict:
        if method not in {
            "sendMessage",
            "answerCallbackQuery",
            "getFile",
            "setWebhook",
            "getWebhookInfo",
        }:
            raise ValueError("Unsupported Telegram method")
        try:
            async with asyncio.timeout(20):
                response = await self.http.post(self.root + "/" + method, json=payload, timeout=15)
            data = response.json()
            if response.status_code != 200 or not data.get("ok"):
                raise TelegramUnavailable("Telegram request failed")
            return data["result"]
        except (httpx.HTTPError, ValueError, KeyError, TimeoutError) as exc:
            raise TelegramUnavailable("Telegram request failed") from exc

    async def send(self, chat_id: int, result: dict) -> None:
        if result.get("photo_png"):
            await self.send_report(chat_id, result)
            return
        text = result["text"]
        # Telegram limits UTF-16 units. 1800 Unicode codepoints safely fits, including emoji.
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)] or ["No data."]
        for index, chunk in enumerate(chunks):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "link_preview_options": {"is_disabled": True},
                "protect_content": True,
            }
            if index == len(chunks) - 1 and result.get("reply_markup"):
                payload["reply_markup"] = result["reply_markup"]
            await self.call("sendMessage", payload)

    async def send_report(self, chat_id: int, result: dict) -> None:
        """One delivery request; the worker persists/retries the same artifact."""
        try:
            encoded = result["photo_png"]
            if not isinstance(encoded, str) or len(encoded) > 2_666_668:
                raise ValueError("Invalid report size")
            png = base64.b64decode(encoded, validate=True)
            if len(png) > 2_000_000 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Invalid report image")
            async with asyncio.timeout(20):
                response = await self.http.post(
                    self.root + "/sendPhoto",
                    data={
                        "chat_id": str(chat_id),
                        "caption": result["text"][:500],
                        "protect_content": "true",
                    },
                    files={"photo": ("financial-report.png", png, "image/png")},
                    timeout=15,
                )
            value = response.json()
            if response.status_code != 200 or not isinstance(value, dict) or not value.get("ok"):
                raise TelegramUnavailable("Report delivery failed")
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            binascii.Error,
            TimeoutError,
        ) as exc:
            raise TelegramUnavailable("Report delivery failed") from exc

    async def receipt(
        self, file_id: str, declared_size: int | None, mime: str | None = None
    ) -> bytes:
        limit = self.settings.max_receipt_file_size_mb * 1024 * 1024
        if declared_size is not None and declared_size > limit:
            raise InvalidInput("Receipt image is too large.")
        if mime and mime not in {"image/jpeg", "image/png"}:
            raise InvalidInput("Only JPEG and PNG receipt images are supported.")
        metadata = await self.call("getFile", {"file_id": file_id})
        if metadata.get("file_size", 0) > limit:
            raise InvalidInput("Receipt image is too large.")
        path = metadata.get("file_path", "")
        if (
            not re.fullmatch(r"[A-Za-z0-9_/-]+\.(jpg|jpeg|png)", path, re.IGNORECASE)
            or ".." in path
            or path.startswith("/")
        ):
            raise InvalidInput("Unsupported receipt file path.")
        url = (
            "https://api.telegram.org/file/bot"
            + self.settings.telegram_bot_token.get_secret_value()
            + "/"
            + path
        )
        content = bytearray()
        try:
            async with asyncio.timeout(25):
                async with self.http.stream(
                    "GET", url, timeout=15, follow_redirects=False
                ) as response:
                    if response.status_code != 200:
                        raise TelegramUnavailable("Receipt download failed")
                    media_type = response.headers.get("content-type", "").split(";")[0].lower()
                    if media_type not in {"image/jpeg", "image/png", "application/octet-stream"}:
                        raise InvalidInput("Unsupported receipt content type.")
                    async for chunk in response.aiter_bytes(65536):
                        content.extend(chunk)
                        if len(content) > limit:
                            raise InvalidInput("Receipt image is too large.")
        except (httpx.HTTPError, TimeoutError) as exc:
            raise TelegramUnavailable("Receipt download failed") from exc
        return await asyncio.to_thread(normalize_image, bytes(content))


def normalize_image(raw: bytes) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if original.format not in {"JPEG", "PNG"} or getattr(original, "n_frames", 1) != 1:
                    raise InvalidInput("Only single-frame JPEG and PNG receipts are supported.")
                if original.width * original.height > 20_000_000:
                    raise InvalidInput("Receipt image resolution is too large.")
                original.verify()
            with Image.open(io.BytesIO(raw)) as original:
                image = ImageOps.exif_transpose(original).convert("RGB")
                image.thumbnail((1024, 1024))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=90)
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise InvalidInput("The receipt image is invalid or unsafe.") from exc
