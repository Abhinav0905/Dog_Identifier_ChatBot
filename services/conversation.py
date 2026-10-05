"""Anonymous conversation ownership and reload-safe short-term context."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid

from fastapi import HTTPException, Request, Response

import config
import database as db


_GUEST_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{40,128}")


def _valid_conversation_id(value: str | None) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = uuid.UUID(candidate)
    except (TypeError, ValueError, AttributeError):
        return ""
    return str(parsed) if str(parsed) == candidate.lower() else ""


def _guest_token(request: Request) -> str:
    token = str(request.cookies.get(config.CONVERSATION_COOKIE_NAME) or "").strip()
    return token if _GUEST_TOKEN_RE.fullmatch(token) else ""


def _set_guest_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=config.CONVERSATION_COOKIE_NAME,
        value=token,
        max_age=config.CONVERSATION_GUEST_TTL_HOURS * 3600,
        httponly=True,
        secure=config.CONVERSATION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def ensure_guest_token(request: Request, response: Response) -> str:
    token = _guest_token(request)
    if token:
        return token
    token = secrets.token_urlsafe(32)
    _set_guest_cookie(response, token)
    return token


def owner_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_guest_conversation(request: Request, response: Response) -> dict:
    token = ensure_guest_token(request, response)
    db.cleanup_expired_guest_conversations()
    return db.create_guest_conversation(
        owner_hash(token),
        ttl_hours=config.CONVERSATION_GUEST_TTL_HOURS,
    )


def require_owned_conversation(
    request: Request,
    response: Response,
    conversation_id: str,
    *,
    include_archived: bool = False,
    touch: bool = False,
) -> dict:
    valid_id = _valid_conversation_id(conversation_id)
    token = _guest_token(request)
    if not valid_id or not token:
        raise HTTPException(status_code=404, detail="Conversation not found")
    hashed_owner = owner_hash(token)
    conversation = db.get_owned_conversation(
        valid_id,
        hashed_owner,
        include_archived=include_archived,
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if touch and not db.touch_owned_conversation(
        valid_id,
        hashed_owner,
        ttl_hours=config.CONVERSATION_GUEST_TTL_HOURS,
    ):
        raise HTTPException(status_code=404, detail="Conversation not found")
    _set_guest_cookie(response, token)
    return conversation


def resolve_message_session(
    request: Request,
    response: Response,
    requested_session_id: str | None,
) -> str:
    """Resolve a browser conversation using only server-issued ownership."""
    requested = str(requested_session_id or "").strip()
    valid_id = _valid_conversation_id(requested)

    token = ensure_guest_token(request, response)
    hashed_owner = owner_hash(token)

    if valid_id:
        if not db.touch_owned_conversation(
            valid_id,
            hashed_owner,
            ttl_hours=config.CONVERSATION_GUEST_TTL_HOURS,
        ):
            raise HTTPException(status_code=404, detail="Conversation not found")
        _set_guest_cookie(response, token)
        return valid_id

    if requested:
        raise HTTPException(status_code=404, detail="Conversation not found")

    created = db.create_guest_conversation(
        hashed_owner,
        ttl_hours=config.CONVERSATION_GUEST_TTL_HOURS,
    )
    _set_guest_cookie(response, token)
    return str(created["conversation_id"])


def archive_guest_conversation(
    request: Request,
    response: Response,
    conversation_id: str,
) -> None:
    require_owned_conversation(request, response, conversation_id)
    token = _guest_token(request)
    if not db.archive_owned_conversation(conversation_id, owner_hash(token)):
        raise HTTPException(status_code=404, detail="Conversation not found")


__all__ = [
    "archive_guest_conversation",
    "create_guest_conversation",
    "ensure_guest_token",
    "owner_hash",
    "require_owned_conversation",
    "resolve_message_session",
]
