"""Shared SQLite request budgets and streaming body bounds for public APIs."""
import hashlib
import time

from starlette.responses import JSONResponse

import config
import database as db


def initialize() -> None:
    with db.get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS request_budgets (key TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL)")


def allowed(client: str) -> bool:
    key = hashlib.sha256(client.encode()).hexdigest()
    window = int(time.time()) // 60
    with db.get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS request_budgets (key TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM request_budgets WHERE window < ?", (window - 2,))
        conn.execute(
            "INSERT INTO request_budgets VALUES (?, ?, 1) ON CONFLICT(key) DO UPDATE SET "
            "count = CASE WHEN window = excluded.window THEN count + 1 ELSE 1 END, window = excluded.window",
            (key, window),
        )
        count = conn.execute("SELECT count FROM request_budgets WHERE key = ?", (key,)).fetchone()[0]
        return count <= config.PUBLIC_REQUESTS_PER_MINUTE


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        headers = dict(scope.get("headers", []))
        is_admin = path.startswith("/v1/admin/") or b"x-admin-password" in headers
        # Authenticated Twilio messages are budgeted per sender in the webhook.
        # Provider IPs are shared across many unrelated WhatsApp users.
        is_twilio = path == "/v1/integrations/twilio/whatsapp"
        needs_budget = scope.get("method") in {"POST", "PUT", "PATCH"} or (
            is_admin and scope.get("method") in {"GET", "HEAD"}
        )
        if needs_budget and not is_twilio:
            from starlette.concurrency import run_in_threadpool
            client = str((scope.get("client") or ("unknown",))[0])
            budget_key = ("admin:" if is_admin else "public:") + client
            if not await run_in_threadpool(allowed, budget_key):
                return await JSONResponse({"detail": "Too many requests. Please wait a minute."}, status_code=429, headers={"Retry-After": "60"})(scope, receive, send)
        maximum = config.MAX_IMAGE_SIZE_MB * 1024 * 1024 + 65536 if path in {"/v1/triage/image", "/v1/image/preview"} else 128 * 1024
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = maximum + 1
        if declared > maximum:
            return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
        seen = 0
        too_large = False

        async def bounded_receive():
            nonlocal seen, too_large
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > maximum:
                too_large = True
                raise ValueError("Request body too large")
            return message

        async def bounded_send(message):
            if not too_large:
                await send(message)

        try:
            await self.app(scope, bounded_receive, bounded_send)
        except Exception:
            if not too_large:
                raise
        if too_large:
            await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
