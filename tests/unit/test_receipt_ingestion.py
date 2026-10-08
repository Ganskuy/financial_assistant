"""Image/Telegram regressions; synthetic pixels do not measure model OCR accuracy."""

import io
import logging

import httpx
import pytest
from PIL import Image, ImageDraw, PngImagePlugin

from app.core.errors import InvalidInput, TelegramUnavailable
from app.telegram.client import TelegramClient, normalize_image


def encode(image: Image.Image, image_format: str = "PNG", **options) -> bytes:
    output = io.BytesIO()
    image.save(output, format=image_format, **options)
    return output.getvalue()


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "RGB"])
def test_transparent_receipt_keeps_dark_text_on_white_paper(mode):
    # A transparent black background must not become black paper. Include PNG
    # palette and color-key transparency, not only the common RGBA representation.
    image = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((40, 40, 160, 60), fill=(0, 0, 0, 255))
    options = {}
    if mode == "LA":
        image = image.convert("LA")
    elif mode == "P":
        image = Image.new("P", image.size, 0)
        image.putpalette([0, 0, 0] * 256)
        ImageDraw.Draw(image).rectangle((40, 40, 160, 60), fill=1)
        options["transparency"] = 0
    elif mode == "RGB":
        image = Image.new("RGB", image.size, (255, 0, 255))
        ImageDraw.Draw(image).rectangle((40, 40, 160, 60), fill="black")
        options["transparency"] = (255, 0, 255)
    normalized = normalize_image(encode(image, **options))
    with Image.open(io.BytesIO(normalized)) as result:
        assert result.format == "JPEG"
        assert result.mode == "RGB"
        assert min(result.getpixel((0, 0))) >= 250
        assert max(result.getpixel((100, 50))) <= 5
        assert result.size == image.size


def test_semitransparent_ink_composites_without_erasing_it():
    image = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((20, 20, 100, 60), fill=(0, 0, 0, 128))
    with Image.open(io.BytesIO(normalize_image(encode(image)))) as result:
        assert 120 <= result.getpixel((60, 40))[0] <= 135
        assert min(result.getpixel((0, 0))) >= 250


def test_exif_orientation_is_applied_and_metadata_is_removed():
    image = Image.new("RGB", (240, 120), "white")
    ImageDraw.Draw(image).rectangle((0, 0, 119, 119), fill="black")
    exif = Image.Exif()
    exif[274] = 6  # 90 degrees clockwise: left dark half becomes top dark half.
    exif[315] = "private author"
    normalized = normalize_image(encode(image, "JPEG", exif=exif))
    with Image.open(io.BytesIO(normalized)) as result:
        assert result.size == (120, 240)
        assert max(result.getpixel((60, 60))) < 10
        assert min(result.getpixel((60, 180))) > 245
        assert not result.getexif()
    assert b"private author" not in normalized


@pytest.mark.parametrize(
    "size,expected", [((96, 64), (96, 64)), ((400, 2400), (171, 1024)), ((2048, 1024), (1024, 512))]
)
def test_receipt_dimensions_are_bounded_without_upscaling(size, expected):
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((5, 5), "TOTAL Rp15.000", fill="black")
    with Image.open(io.BytesIO(normalize_image(encode(image)))) as result:
        assert result.size == expected
        assert result.format == "JPEG"


def test_png_metadata_is_removed():
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "private receipt metadata")
    raw = encode(Image.new("RGB", (32, 32), "white"), pnginfo=metadata)
    normalized = normalize_image(raw)
    assert b"private receipt metadata" not in normalized


@pytest.mark.parametrize("raw", [b"", b"not an image", b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"])
def test_corrupted_receipts_are_rejected(raw):
    with pytest.raises(InvalidInput, match="invalid or unsafe"):
        normalize_image(raw)


@pytest.mark.parametrize("image_format", ["GIF", "WEBP", "BMP"])
def test_unsupported_formats_are_rejected(image_format):
    with pytest.raises(InvalidInput, match="single-frame JPEG and PNG"):
        normalize_image(encode(Image.new("RGB", (32, 32)), image_format))


def test_animated_png_is_rejected():
    image = Image.new("RGB", (32, 32), "white")
    raw = encode(image, save_all=True, append_images=[Image.new("RGB", image.size, "black")])
    with pytest.raises(InvalidInput, match="single-frame JPEG and PNG"):
        normalize_image(raw)


def test_resolution_limit_is_checked_before_decoding(monkeypatch):
    # Alter only the opened header size to test the 20MP gate without allocating
    # an oversized decompressed image on every CI run.
    original_open = Image.open
    raw = encode(Image.new("RGB", (32, 32)))

    def open_large_header(*args, **kwargs):
        image = original_open(*args, **kwargs)
        image._size = (5000, 4001)
        return image

    monkeypatch.setattr("app.telegram.client.Image.open", open_large_header)
    with pytest.raises(InvalidInput, match="resolution is too large"):
        normalize_image(raw)


def test_pillow_bomb_warning_is_rejected(monkeypatch):
    raw = encode(Image.new("RGB", (32, 32)))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(InvalidInput, match="invalid or unsafe"):
        normalize_image(raw)


def test_image_diagnostics_contain_only_safe_dimensions_and_operations(caplog):
    raw = encode(Image.new("RGBA", (2000, 100), (0, 0, 0, 0)))
    with caplog.at_level(logging.INFO, logger="finance"):
        normalize_image(raw)
    record = next(record for record in caplog.records if record.msg == "receipt_image_processed")
    assert (record.image_width, record.image_height) == (1024, 51)
    assert record.preprocessed is True
    assert record.reason == "resize,alpha_composite"
    assert record.latency_ms >= 0
    assert not hasattr(record, "raw")


@pytest.mark.parametrize(
    "declared_mime",
    [None, "image/jpeg", "image/png", "application/octet-stream", "Image/PNG; charset=binary"],
)
async def test_telegram_photo_or_document_download_uses_verified_image_bytes(
    settings, declared_mime
):
    raw = encode(Image.new("RGB", (120, 80), "white"))
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"file_path": "photos/file_123.png", "file_size": len(raw)},
                },
            )
        return httpx.Response(
            200, content=raw, headers={"content-type": "Application/Octet-Stream; charset=binary"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        normalized = await TelegramClient(settings, http).receipt(
            "file-id", len(raw), declared_mime
        )
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.format == "JPEG"
    assert len(requests) == 2


@pytest.mark.parametrize("mime", ["application/pdf", "image/gif", "text/html"])
async def test_unsupported_document_mime_stops_before_download(settings, mime):
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as http:
        with pytest.raises(InvalidInput, match="Only JPEG and PNG"):
            await TelegramClient(settings, http).receipt("file-id", 10, mime)
    assert not requests


@pytest.mark.parametrize("stage", ["declared", "metadata", "stream"])
async def test_receipt_size_limit_applies_at_every_ingestion_boundary(settings, stage):
    settings.max_receipt_file_size_mb = 1
    limit = 1024 * 1024
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "file_path": "photos/file.png",
                        "file_size": limit + 1 if stage == "metadata" else 0,
                    },
                },
            )
        return httpx.Response(
            200, content=b"x" * (limit + 1), headers={"content-type": "image/png"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(InvalidInput, match="too large"):
            await TelegramClient(settings, http).receipt(
                "file-id", limit + 1 if stage == "declared" else None
            )
    assert len(requests) == {"declared": 0, "metadata": 1, "stream": 2}[stage]


@pytest.mark.parametrize(
    "path", ["../file.png", "/photo/file.jpg", "https://example.com/file.png", "photos/file.pdf"]
)
async def test_unsafe_file_paths_stop_before_download(settings, path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"file_path": path}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(InvalidInput, match="file path"):
            await TelegramClient(settings, http).receipt("file-id", None)
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["timeout", "redirect", "mime", "fake_image"])
async def test_failed_downloads_are_safe_and_never_follow_redirects(settings, failure):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "photos/file.png"}}
            )
        if failure == "timeout":
            raise httpx.ReadTimeout("private token must not be in user response")
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://example.com/file.png"})
        return httpx.Response(
            200,
            content=b"not an image",
            headers={
                "content-type": "text/html" if failure == "mime" else "application/octet-stream"
            },
        )

    expected = TelegramUnavailable if failure in {"timeout", "redirect"} else InvalidInput
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(expected) as error:
            await TelegramClient(settings, http).receipt("file-id", None)
    assert len(requests) == 2
    assert "private token" not in str(error.value)
