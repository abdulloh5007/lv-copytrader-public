"""Image preparation for the model's vision input: decode, downscale, re-encode, size-cap."""

from __future__ import annotations

import asyncio
import base64
import io
import math
import os

from PIL import Image, UnidentifiedImageError

from app.errors import IngestError

_JPEG_MEDIA_TYPE = "image/jpeg"
MAX_SOURCE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_PIXELS = 25_000_000

# Pillow >= 9.1 exposes the filters on Image.Resampling; the flat aliases stay for older builds.
_RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

# Quality is dropped in steps of this size while the encoded payload is over the byte cap.
_QUALITY_STEP = 10

# Anthropic and OpenAI-compatible vision endpoints both bill an image as ceil(w/28) * ceil(h/28).
_TOKEN_BLOCK_PX = 28

# White is the right flattening ground: a transparent chart composited onto black is unreadable.
_FLATTEN_BACKGROUND = (255, 255, 255)


def _open_rgb(path: str) -> Image.Image:
    """Open an image file and return it as a loaded, fully opaque RGB copy."""
    if not os.path.isfile(path):
        raise IngestError(f"image file not found: {path}", detail={"path": path})
    if os.path.getsize(path) > MAX_SOURCE_IMAGE_BYTES:
        raise IngestError("source image exceeds the 20 MiB safety limit")
    source: Image.Image | None = None
    try:
        source = Image.open(path)
        if source.width * source.height > MAX_SOURCE_PIXELS:
            raise IngestError("source image exceeds the 25 megapixel safety limit")
        source.load()
        mode = source.mode
        if mode in ("RGBA", "LA") or (mode == "P" and "transparency" in source.info):
            rgba = source.convert("RGBA")
            flat = Image.new("RGB", rgba.size, _FLATTEN_BACKGROUND)
            flat.paste(rgba, mask=rgba.split()[-1])
            rgba.close()
            return flat
        if mode == "RGB":
            return source.copy()
        return source.convert("RGB")
    except UnidentifiedImageError as exc:
        raise IngestError(
            f"cannot decode image {path}: unrecognised image format",
            detail={"path": path},
        ) from exc
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise IngestError(f"cannot decode image {path}: {exc}", detail={"path": path}) from exc
    finally:
        if source is not None:
            source.close()


def _encode_jpeg(image: Image.Image, quality: int) -> bytes:
    """Encode an RGB image to JPEG bytes at the given quality."""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _prepare_image_sync(
    path: str,
    max_edge: int,
    quality: int,
    min_quality: int,
    max_bytes: int,
) -> tuple[str, str, int, int]:
    """Blocking half of prepare_image; always runs off the event loop."""
    image = _open_rgb(path)
    try:
        width, height = image.size
        if width <= 0 or height <= 0:
            raise IngestError(
                f"cannot use image {path}: it has no pixels ({width}x{height})",
                detail={"path": path, "width": width, "height": height},
            )
        longest = max(width, height)
        if max_edge > 0 and longest > max_edge:
            scale = max_edge / float(longest)
            new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
            resized = image.resize(new_size, _RESAMPLE)
            image.close()
            image = resized
            width, height = image.size

        current = int(quality)
        floor_quality = min(int(min_quality), current)
        try:
            payload = _encode_jpeg(image, current)
            while len(payload) > max_bytes and current > floor_quality:
                current = max(floor_quality, current - _QUALITY_STEP)
                payload = _encode_jpeg(image, current)
        except (OSError, ValueError) as exc:
            raise IngestError(
                f"cannot encode image {path} as JPEG: {exc}", detail={"path": path}
            ) from exc

        return base64.b64encode(payload).decode("ascii"), _JPEG_MEDIA_TYPE, width, height
    finally:
        image.close()


async def prepare_image(
    path: str,
    *,
    max_edge: int,
    quality: int,
    min_quality: int,
    max_bytes: int,
) -> tuple[str, str, int, int]:
    """Downscale and JPEG-encode an image under a byte cap; returns (base64, media_type, w, h)."""
    return await asyncio.to_thread(
        _prepare_image_sync, path, max_edge, quality, min_quality, max_bytes
    )


def _image_dimensions_sync(path: str) -> tuple[int, int]:
    """Blocking half of image_dimensions; reads the header only."""
    if not os.path.isfile(path):
        raise IngestError(f"image file not found: {path}", detail={"path": path})
    try:
        with Image.open(path) as image:
            width, height = image.size
    except UnidentifiedImageError as exc:
        raise IngestError(
            f"cannot decode image {path}: unrecognised image format",
            detail={"path": path},
        ) from exc
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise IngestError(f"cannot decode image {path}: {exc}", detail={"path": path}) from exc
    return int(width), int(height)


async def image_dimensions(path: str) -> tuple[int, int]:
    """Read an image's pixel size without decoding the whole frame."""
    return await asyncio.to_thread(_image_dimensions_sync, path)


def data_url(b64: str, media_type: str) -> str:
    """Build the inline data URL an OpenAI-compatible endpoint needs for an image."""
    return f"data:{media_type};base64,{b64}"


def estimate_tokens(w: int, h: int) -> int:
    """Estimate what an image of this size costs a vision model, in tokens."""
    if w <= 0 or h <= 0:
        return 0
    return math.ceil(w / _TOKEN_BLOCK_PX) * math.ceil(h / _TOKEN_BLOCK_PX)
