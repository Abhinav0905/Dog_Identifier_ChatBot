"""Web adapter for the evaluated agent; uses private, bounded production traces."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import re
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import HTTPException
from openai import OpenAI

import config
import database as db
from models import ChatResponse
from simple_agent.agent import AgentConfig, SingleAgent
from simple_agent.release import load_bundle
from services import agent_trace, web_operations

_pinned = ContextVar("single_agent_release", default=None)


def enabled():
    return config.CHAT_ENGINE == "single_agent"


def pinned():
    return _pinned.get()


def current():
    return pinned() or load_bundle(config.SIMPLE_AGENT_PROMPT_DIR)


@contextmanager
def pin(bundle):
    token = _pinned.set(bundle)
    try:
        yield bundle
    finally:
        _pinned.reset(token)


def status():
    try:
        bundle = current()
        return {"version": bundle.version, "sha256": bundle.digest, "status": "valid", "engine": "single_agent"}
    except (OSError, ValueError):
        return {"version": "unavailable", "sha256": "", "status": "invalid", "engine": "single_agent"}


@lru_cache(maxsize=1)
def client():
    return OpenAI(api_key=config.OPENAI_API_KEY, max_retries=0)


def _create(**kwargs):
    role = "single_agent_photo" if kwargs.get("text") else "single_agent"
    started = time.monotonic()
    try:
        result = agent_trace.model_call(role, client().responses.create, **kwargs)
        state = "ok" if result.status == "completed" else "error"
        web_operations.record_event("model:" + role, state, int((time.monotonic() - started) * 1000))
        return result
    except Exception as exc:
        web_operations.record_event("model:" + role, "error", int((time.monotonic() - started) * 1000), type(exc).__name__)
        raise


def answer(session_id, question, *, image_bytes=None, media_type="image/jpeg", deadline=None):
    remaining = min(80, deadline - time.monotonic()) if deadline else 80
    if remaining <= 0:
        raise HTTPException(503, "The request timed out. Please try again.")
    history = []
    for row in db.get_chat_history(session_id, limit=20):
        if row["role"] not in {"user", "assistant"}:
            continue
        content = row["content"]
        links = (row.get("metadata") or {}).get("resource_links", [])
        if links:
            content += "\n" + "\n".join(x["url"] for x in links if x.get("url"))
        history.append({"role": row["role"], "content": content})
    agent = SingleAgent(SimpleNamespace(responses=SimpleNamespace(create=_create)),
                        config=AgentConfig(model=config.SIMPLE_AGENT_MODEL,
                                           vision_model=config.SIMPLE_AGENT_VISION_MODEL,
                                           turn_budget=remaining), prompts=current().catalog)
    if image_bytes is None:
        result = agent.turn(question, history)
    else:
        # Normalize HEIC and other accepted uploads before the tool reads them.
        from PIL import Image, ImageOps
        import io
        with tempfile.TemporaryDirectory(prefix="dorjee-photo-") as directory:
            path = Path(directory) / "photo.jpg"
            with Image.open(io.BytesIO(image_bytes)) as image:
                ImageOps.exif_transpose(image).convert("RGB").save(path, format="JPEG", quality=90)
            result = agent.turn(question or "Please assess this animal photo.", history, image_path=path)
    for event in result["trace"]:
        if event["stage"] in {"function_result", "agent_error", "turn_end"}:
            agent_trace.decision("single_agent." + event["stage"], event=event)
    if result["error"] or result["status"] != "completed" or not result["answer"]:
        raise HTTPException(503, "I could not complete this answer. Please try again.")
    links = []
    seen = set()
    for citation in result["citations"]:
        url = citation.get("url", "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and url not in seen:
            links.append({"label": citation.get("title") or parsed.hostname, "url": url})
            seen.add(url)
    return ChatResponse(response=result["answer"], resource_links=links,
                        answer_format="phone_only" if re.fullmatch(r"[+\d ()-]{5,25}", result["answer"]) else "text")


def save(session_id, question, result):
    db.save_chat_message(session_id, "user", question)
    db.save_chat_message(session_id, "assistant", result.response, metadata={
        "resource_links": [link.model_dump() for link in result.resource_links],
        "answer_format": result.answer_format, "chat_engine": "single_agent",
        "prompt_version": current().version, "stage_trace": web_operations.current_turn_trace(),
    })
