"""Bounded, server-side execution traces. Never records model reasoning.

Every model call has a span; text capture requires an explicit conversation
allowlist. Public chat metadata only receives trace IDs and operational events.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from functools import wraps
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import database as db

logger = logging.getLogger(__name__)
_trace = ContextVar("agent_trace", default=None)
_parent = ContextVar("agent_trace_parent", default=None)
_span_row = ContextVar("agent_trace_span_row", default=None)
MAX_SPANS = 200
MAX_VALUE_CHARS = 80000
MAX_TRACE_CHARS = 1200000
_SECRET = re.compile(r"api[_-]?key|authorization|password|secret|cookie|access[_-]?token|session[_-]?token|credential", re.I)


def initialize():
    with db.get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS agent_traces (
            trace_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            session_id TEXT NOT NULL DEFAULT '', source_revision TEXT NOT NULL,
            prompt_version TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
            content_capture INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
            dropped_spans INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS agent_trace_spans (
            trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
            sequence INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY (trace_id, span_id))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_trace_session ON agent_traces(session_id, started_at)")


def _safe(value, depth=0):
    if depth > 14:
        return "[depth limit]"
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        if isinstance(value.get("type"), str) and value.get("type") in {"reasoning", "input_image", "image", "image_url"}:
            return {"type": value["type"], "content": "[omitted]"}
        return {str(k): "[redacted]" if _SECRET.search(str(k)) or str(k) in {"encrypted_content", "reasoning_content"}
                else _safe(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v, depth + 1) for v in list(value)[:100]]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return _safe(json.loads(value), depth + 1)
            except (ValueError, TypeError):
                pass
        value = re.sub(r"data:[^\s,;]+(?:;base64)?,[^\s\"']+", "[image omitted]", value)
        value = re.sub(r"\b(?:sk-|sk_)[A-Za-z0-9_-]{8,}", "[redacted]", value)
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", value)
        value = re.sub(r"(?i)(\b(?:api[_-]?key|password|secret|access[_-]?token|authorization)\s*[=:]\s*)[^\s,;]+", r"\1[redacted]", value)
        def clean_url(match):
            try:
                url = urlsplit(match.group())
                # Query/fragment may carry credentials or tracking identifiers.
                return urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path, "", ""))
            except ValueError:
                return "[invalid URL]"
        value = re.sub(r"https?://[^\s<>\"']+", clean_url, value)
        return value[:MAX_VALUE_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return {"object_type": type(value).__name__}


def _capture(value):
    trace = _trace.get()
    safe = _safe(value)
    encoded = json.dumps(safe, ensure_ascii=False, default=str)
    result = {"sha256": hashlib.sha256(encoded.encode()).hexdigest(), "chars": len(encoded)}
    if trace and trace["capture"]:
        with trace["lock"]:
            available = max(0, min(MAX_VALUE_CHARS, MAX_TRACE_CHARS - trace["chars"]))
            trace["chars"] += min(len(encoded), available)
        if len(encoded) <= available:
            result["value"] = safe
        elif available:
            result.update(preview=encoded[:available], truncated=True)
        else:
            result["omitted"] = "trace_content_budget"
    return result


def _write_span(trace_id, row):
    try:
        with db.get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO agent_trace_spans VALUES (?, ?, ?, ?, ?)",
                         (trace_id, row["id"], row["parent_id"], row["sequence"], json.dumps(row, ensure_ascii=False)))
    except Exception as exc:
        logger.warning("Agent span persistence unavailable (%s)", type(exc).__name__)


@contextmanager
def turn(trace_id=None):
    if _trace.get() is not None:
        yield _trace.get()["id"]
        return
    from services import prompt_store, single_agent_runtime
    try:
        bundle = single_agent_runtime.pinned() or prompt_store.current()
        version, digest = bundle.version, bundle.digest
    except Exception:
        version, digest = "unavailable", ""
    trace = {"id": trace_id or uuid.uuid4().hex, "capture": False, "count": 0,
             "chars": 0, "dropped": 0, "lock": threading.Lock(), "closed": False}
    token = _trace.set(trace)
    parent_token = _parent.set(None)
    status = "ok"
    try:
        try:
            with db.get_db() as conn:
                conn.execute("INSERT INTO agent_traces(trace_id,started_at,source_revision,prompt_version,prompt_sha256,status) VALUES(?,?,?,?,?,?)",
                             (trace["id"], datetime.now(timezone.utc).isoformat(), os.getenv("APP_SOURCE_REVISION", "unknown"), version, digest, "running"))
        except Exception as exc:
            logger.warning("Agent trace persistence unavailable (%s)", type(exc).__name__)
        yield trace["id"]
    except BaseException:
        status = "error"
        raise
    finally:
        trace["closed"] = True
        try:
            with db.get_db() as conn:
                conn.execute("UPDATE agent_traces SET finished_at=?,status=?,dropped_spans=? WHERE trace_id=?",
                             (datetime.now(timezone.utc).isoformat(), status, trace["dropped"], trace["id"]))
        except Exception as exc:
            logger.warning("Agent trace completion unavailable (%s)", type(exc).__name__)
        _parent.reset(parent_token)
        _trace.reset(token)


def bind_session(session_id):
    trace = _trace.get()
    if not trace:
        return
    allowed = {x.strip() for x in os.getenv("AGENT_TRACE_CONTENT_SESSION_IDS", "").split(",") if x.strip()}
    trace["capture"] = session_id in allowed  # No wildcard capture of real users.
    try:
        with db.get_db() as conn:
            conn.execute("UPDATE agent_traces SET session_id=?,content_capture=? WHERE trace_id=?",
                         (session_id, int(trace["capture"]), trace["id"]))
    except Exception as exc:
        logger.warning("Agent trace session binding unavailable (%s)", type(exc).__name__)


@contextmanager
def span(name, inputs=None, *, attributes=None):
    trace = _trace.get()
    row = {}
    if trace is None:
        yield row
        return
    with trace["lock"]:
        if trace["closed"] or trace["count"] >= MAX_SPANS:
            trace["dropped"] += 1
            yield_row = False
        else:
            trace["count"] += 1
            sequence = trace["count"]
            yield_row = True
    if not yield_row:
        yield row
        return
    started = time.monotonic()
    row.update(id=uuid.uuid4().hex, parent_id=_parent.get(), sequence=sequence,
               name=name, started_at=datetime.now(timezone.utc).isoformat(), status="running",
               attributes=_safe(attributes or {}), input=_capture(inputs))
    token = _parent.set(row["id"])
    row_token = _span_row.set(row)
    _write_span(trace["id"], row)
    try:
        yield row
        row["status"] = row.get("status") if row.get("status") != "running" else "ok"
    except BaseException as exc:
        row.update(status="timeout" if "timeout" in type(exc).__name__.lower() else "error", error_type=type(exc).__name__)
        raise
    finally:
        row.update(finished_at=datetime.now(timezone.utc).isoformat(), latency_ms=round((time.monotonic() - started) * 1000))
        _write_span(trace["id"], row)
        _parent.reset(token)
        _span_row.reset(row_token)


def event(component, status, latency_ms=0, error_type=""):
    row = _span_row.get()
    if row and len(row.setdefault("events", [])) < 100:
        row["events"].append({"component": component, "status": status,
                              "latency_ms": latency_ms, "error_type": error_type})


def decision(name, **details):
    with span(name, details):
        pass


def stage(name):
    """Trace deterministic decisions as well as model calls, without changing returns."""
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def run(*args, **kwargs):
            if _trace.get() is None:
                return function(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            inputs = {k: v for k, v in bound.arguments.items() if k not in {"deadline", "page_cache"}}
            if "pages" in inputs:
                inputs["pages"] = {url: {"chars": len(page), "sha256": hashlib.sha256(str(page).encode()).hexdigest()}
                                   for url, page in inputs["pages"].items()}
            with span(name, inputs) as row:
                result = function(*args, **kwargs)
                if row:
                    if name == "source.fetch" and result is not None:
                        row["output"] = _capture({"chars": len(result), "sha256": hashlib.sha256(str(result).encode()).hexdigest(),
                            "identity": getattr(result, "identity_text", ""), "excerpt": str(result)[:6000],
                            "tail": str(result)[-2000:] if len(result) > 6000 else ""})
                    elif name == "sources.fetch_batch":
                        row["output"] = _capture({url: {"chars": len(page), "identity": getattr(page, "identity_text", "")}
                                                  for url, page in result.items()})
                    else:
                        row["output"] = _capture(result)
                return result
        return run
    return decorate


def model_call(role, create, **kwargs):
    if _trace.get() is None:
        with turn():
            return model_call(role, create, **kwargs)
    with span("model." + role, {k: v for k, v in kwargs.items() if k not in {"timeout"}},
              attributes={"model": kwargs.get("model"), "timeout_seconds": kwargs.get("timeout"), "store": kwargs.get("store")}) as row:
        response = create(**kwargs)
        if row:
            try:
                payload = response.model_dump() if hasattr(response, "model_dump") else {}
                if not isinstance(payload, dict):
                    payload = {}
                output = {"text": getattr(response, "output_text", None),
                          "tools": [x for x in payload.get("output", []) if isinstance(x, dict) and x.get("type") in {"web_search_call", "function_call"}]}
                if not isinstance(output["text"], str):
                    output["text"] = None
                if "choices" in payload:
                    output["messages"] = [{"content": c.get("message", {}).get("content"), "finish_reason": c.get("finish_reason")} for c in payload["choices"]]
                if "content" in payload:  # Anthropic text only; thinking is excluded.
                    output["messages"] = [c for c in payload["content"] if isinstance(c, dict) and c.get("type") == "text"]
                row.update(response_id=payload.get("id"), provider_status=payload.get("status"),
                           usage=_safe(payload.get("usage")), output=_capture(output))
            except Exception as exc:
                row["capture_error_type"] = type(exc).__name__
        return response


def read(trace_id):
    with db.get_db() as conn:
        trace = conn.execute("SELECT * FROM agent_traces WHERE trace_id=?", (trace_id,)).fetchone()
        if trace is None:
            return None
        result = dict(trace)
        result["spans"] = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM agent_trace_spans WHERE trace_id=? ORDER BY sequence", (trace_id,))]
        return result


def expire(cutoff):
    with db.get_db() as conn:
        conn.execute("DELETE FROM agent_trace_spans WHERE trace_id IN (SELECT trace_id FROM agent_traces WHERE started_at < ?)", (cutoff,))
        conn.execute("DELETE FROM agent_traces WHERE started_at < ?", (cutoff,))
