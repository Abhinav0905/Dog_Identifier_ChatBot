"""Observable calls/results, written as they happen; no hidden reasoning/images."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def public_response(payload):
    metadata = {key: value for key, value in payload.items()
                if key in {"id", "model", "status", "usage", "incomplete_details"}}
    metadata["output"] = [item for item in payload.get("output", [])
                          if item.get("type") != "reasoning"]
    return metadata


def safe_error(exc):
    result = {"type": type(exc).__name__}
    for field in ("status_code", "code", "param", "request_id"):
        value = getattr(exc, field, None)
        if isinstance(value, (str, int)):
            result[field] = value
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        message = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted-key]", body["message"])
        message = re.sub(r"[A-Za-z0-9_+/=-]{80,}", "[redacted-long-value]", message)
        result["message"] = message[:600]
    return result


class TraceRecorder:
    def __init__(self, path=None):
        self.events = []
        self.path = Path(path) if path else None
        self.started = time.monotonic()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=False)

    def record(self, stage, **fields):
        event = {"stage": stage, "at": utc_now(),
                 "elapsed_seconds": round(time.monotonic() - self.started, 3), **fields}
        self.events.append(event)
        if self.path:
            with self.path.open("a") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event
