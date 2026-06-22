"""Image upload validation and preparation for vision-model requests."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

import config


class ImageProcessingError(ValueError):
    """Raised when an uploaded image cannot be decoded or prepared."""


_EXTENSION_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_FORMAT_MEDIA_TYPES = {
    "GIF": "image/gif",
    "HEIF": "image/heic",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}

_MEDIA_TYPE_ALIASES = {
    "image/heic-sequence": "image/heic",
    "image/heif-sequence": "image/heif",
    "image/x-heic": "image/heic",
    "image/x-heif": "image/heif",
    "image/jpg": "image/jpeg",
}

_HEIF_REGISTERED = False


def register_heif_support() -> bool:
    """Register Pillow's HEIC/HEIF decoder when pillow-heif is installed."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return True
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _HEIF_REGISTERED = True
    except ImportError:
        return False
    return True


def heif_support_available() -> bool:
    return register_heif_support()


def normalize_media_type(media_type: str | None, filename: str | None = None) -> str:
    """Normalize browser MIME values and infer HEIC/HEIF from the filename."""
    normalized = (media_type or "").split(";", 1)[0].strip().lower()
    normalized = _MEDIA_TYPE_ALIASES.get(normalized, normalized)
    if normalized in config.ALLOWED_IMAGE_TYPES:
        return normalized

    inferred = _EXTENSION_MEDIA_TYPES.get(Path(filename or "").suffix.lower(), "")
    if inferred and normalized in {"", "application/octet-stream"}:
        return inferred
    return normalized


def validate_upload(image_bytes: bytes, media_type: str | None, filename: str | None = None) -> str:
    """Decode an upload and return its canonical supported MIME type."""
    if not image_bytes:
        raise ImageProcessingError("The uploaded image is empty.")

    normalized = normalize_media_type(media_type, filename)
    if normalized not in config.ALLOWED_IMAGE_TYPES:
        raise ImageProcessingError(
            "Unsupported image type. Please upload a JPEG, PNG, WebP, GIF, HEIC, or HEIF photo."
        )

    if normalized in {"image/heic", "image/heif"} and not register_heif_support():
        raise ImageProcessingError("HEIC/HEIF support is not installed on the server.")

    register_heif_support()
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            actual_media_type = _FORMAT_MEDIA_TYPES.get((image.format or "").upper())
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageProcessingError("The uploaded file could not be decoded as an image.") from exc

    if actual_media_type not in config.ALLOWED_IMAGE_TYPES:
        raise ImageProcessingError(
            "Unsupported image type. Please upload a JPEG, PNG, WebP, GIF, HEIC, or HEIF photo."
        )
    return actual_media_type


def prepare_for_vision(image_bytes: bytes, media_type: str) -> tuple[bytes, str]:
    """Convert any accepted upload to a resized, orientation-correct JPEG."""
    return _convert_to_jpeg(
        image_bytes,
        media_type,
        max_dimension=config.VISION_IMAGE_MAX_DIMENSION,
        quality=config.VISION_IMAGE_JPEG_QUALITY,
        error_message="The uploaded image could not be prepared for assessment.",
    )


def prepare_preview(image_bytes: bytes, media_type: str) -> tuple[bytes, str]:
    """Convert any accepted upload to a browser-displayable JPEG preview."""
    return _convert_to_jpeg(
        image_bytes,
        media_type,
        max_dimension=min(config.VISION_IMAGE_MAX_DIMENSION, 1200),
        quality=min(config.VISION_IMAGE_JPEG_QUALITY, 85),
        error_message="The uploaded image could not be prepared for preview.",
    )


def _convert_to_jpeg(
    image_bytes: bytes,
    media_type: str,
    *,
    max_dimension: int,
    quality: int,
    error_message: str,
) -> tuple[bytes, str]:
    if media_type in {"image/heic", "image/heif"} and not register_heif_support():
        raise ImageProcessingError("HEIC/HEIF support is not installed on the server.")

    register_heif_support()
    try:
        with Image.open(io.BytesIO(image_bytes)) as uploaded:
            uploaded.seek(0)
            image = ImageOps.exif_transpose(uploaded)
            image.thumbnail(
                (max_dimension, max_dimension),
                Image.Resampling.LANCZOS,
            )
            image = _flatten_to_rgb(image)

            output = io.BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=True,
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageProcessingError(error_message) from exc

    return output.getvalue(), "image/jpeg"


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")
