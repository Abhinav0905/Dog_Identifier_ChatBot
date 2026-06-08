"""Twilio WhatsApp webhook helpers."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

import config

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 1500
MEDIA_DOWNLOAD_ATTEMPTS = 5
MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS = 10
MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS = 60
_IMAGE_EXTENSIONS = {
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _is_allowed_twilio_media_host(hostname: str | None) -> bool:
    return bool(hostname and (hostname == "twilio.com" or hostname.endswith(".twilio.com")))


def session_id_for_sender(sender: str) -> str:
    """Return a stable pseudonymous session ID without storing the phone number."""
    digest = hashlib.sha256(sender.encode("utf-8")).hexdigest()[:32]
    return f"whatsapp:{digest}"


def validate_webhook(request_url: str, params: dict[str, str], signature: str) -> bool:
    if not config.TWILIO_VALIDATE_SIGNATURES:
        return True
    if not config.TWILIO_AUTH_TOKEN or not signature:
        logger.warning("Twilio signature validation is enabled but credentials/signature are missing")
        return False
    return RequestValidator(config.TWILIO_AUTH_TOKEN).validate(request_url, params, signature)


def public_request_url(request_url: str, path: str, query: str = "") -> str:
    """Use an explicit public base URL when the app is behind a proxy."""
    if not config.TWILIO_WEBHOOK_BASE_URL:
        return request_url
    suffix = f"?{query}" if query else ""
    return f"{config.TWILIO_WEBHOOK_BASE_URL}{path}{suffix}"


def download_image_media(media_url: str, content_type: str, message_sid: str) -> tuple[bytes, str, str]:
    """Download one Twilio-hosted image with SSRF and upload-size guards."""
    parsed = urlparse(media_url)
    if parsed.scheme != "https" or not _is_allowed_twilio_media_host(parsed.hostname):
        raise ValueError("Twilio media URL was not hosted on twilio.com")
    if content_type not in _IMAGE_EXTENSIONS:
        raise ValueError("Only image attachments are supported")

    auth = None
    if config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN:
        auth = (config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)

    max_bytes = config.MAX_IMAGE_SIZE_MB * 1024 * 1024
    timeout = (MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS, MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS)
    headers = {"User-Agent": "gaia-chatbot/1.0"}
    last_error: Exception | None = None

    for attempt in range(1, MEDIA_DOWNLOAD_ATTEMPTS + 1):
        response = None
        try:
            response = requests.get(
                media_url,
                auth=auth,
                headers=headers,
                timeout=timeout,
                stream=True,
            )
            final_url = urlparse(response.url)
            if final_url.scheme != "https":
                raise ValueError("Twilio media URL redirected to a non-HTTPS URL")
            response.raise_for_status()

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"WhatsApp image exceeds the {config.MAX_IMAGE_SIZE_MB}MB limit")
                chunks.append(chunk)

            logger.info(
                "twilio_media_downloaded message_sid=%s attempt=%d host=%s bytes=%d content_type=%s",
                message_sid,
                attempt,
                final_url.hostname or parsed.hostname,
                total,
                content_type,
            )
            filename = f"whatsapp-{message_sid or 'image'}{_IMAGE_EXTENSIONS[content_type]}"
            return b"".join(chunks), content_type, filename
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if response is not None:
                response.close()
            if attempt >= MEDIA_DOWNLOAD_ATTEMPTS:
                break
            logger.warning(
                "twilio_media_download_retry message_sid=%s attempt=%d/%d host=%s error=%s",
                message_sid,
                attempt,
                MEDIA_DOWNLOAD_ATTEMPTS,
                parsed.hostname,
                exc,
            )
            time.sleep(min(8, attempt * 1.5))

    raise RuntimeError(f"Could not download WhatsApp image from Twilio after retries: {last_error}")


def build_twiml(text: str) -> str:
    response = MessagingResponse()
    response.message(_whatsapp_text(text))
    return str(response)


def send_whatsapp_message(*, to: str, from_: str, text: str) -> str:
    if not config.TWILIO_ACCOUNT_SID or not config.TWILIO_AUTH_TOKEN:
        raise RuntimeError("Twilio credentials are not configured")
    if not to or not from_:
        raise ValueError("Both WhatsApp sender and recipient numbers are required")

    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    message = client.messages.create(
        to=to,
        from_=from_,
        body=_whatsapp_text(text),
    )
    return message.sid


def _whatsapp_text(text: str) -> str:
    """Make the app's markdown-like output compact and WhatsApp-friendly."""
    text = re.sub(r"^#{1,4}\s+", "", text, flags=re.MULTILINE)
    text = text.replace("**", "*")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_REPLY_CHARS:
        links_marker = "\n\nHelpful links:\n"
        suffix = "\n\nReply with a follow-up question for more detail."
        if links_marker in text:
            main_text, links_text = text.rsplit(links_marker, 1)
            link_section = f"{links_marker.strip()}\n{links_text.strip()}"
            main_limit = MAX_REPLY_CHARS - len(link_section) - len(suffix) - 2
            if main_limit > 200:
                text = main_text[:main_limit].rstrip() + suffix + "\n\n" + link_section
            else:
                text = text[: MAX_REPLY_CHARS - 60].rstrip() + suffix
        else:
            text = text[: MAX_REPLY_CHARS - 60].rstrip() + suffix
    return text or "Message received."
