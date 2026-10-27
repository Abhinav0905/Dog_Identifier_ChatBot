"""Isolated trace integrity/privacy checks; no live model calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import database as db
from services import agent_trace as trace, web_operations


class AgentTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(self.temp.name) / "test.db"))
        self.enterContext(patch.dict(os.environ, {"AGENT_TRACE_CONTENT_SESSION_IDS": "synthetic-test", "APP_SOURCE_REVISION": "test-build"}))
        trace.initialize()

    def response(self):
        return SimpleNamespace(output_text='{"action":"search"}', model_dump=lambda: {
            "id": "response-1", "usage": {"input_tokens": 20, "output_tokens": 7},
            "output": [{"type": "reasoning", "summary": "private thinking"},
                       {"type": "web_search_call", "action": {"query": "Pune vet", "sources": [{"url": "https://example.org/contact?token=secret"}]}}]})

    def test_calls_keep_actual_request_response_usage_and_parent(self):
        create = MagicMock(return_value=self.response())
        with trace.turn() as trace_id:
            trace.bind_session("synthetic-test")
            with trace.span("router.plan"):
                response = trace.model_call("router", create, model="fixture", input=[{"role": "user", "content": "Pune vet"}], store=False)
        result = trace.read(trace_id)
        parent, child = result["spans"]
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["output"]["value"]["text"], {"action": "search"})
        self.assertEqual(child["usage"]["output_tokens"], 7)
        self.assertEqual(result["source_revision"], "test-build")
        self.assertIs(response, create.return_value)
        create.assert_called_once_with(model="fixture", input=[{"role": "user", "content": "Pune vet"}], store=False)
        self.assertNotIn("private thinking", json.dumps(result))
        self.assertNotIn("token=secret", json.dumps(result))

    def test_content_is_off_outside_exact_allowlist_and_no_wildcard(self):
        for session in ("real-session", "", "synthetic-test-extra"):
            with self.subTest(session=session), trace.turn() as trace_id:
                trace.bind_session(session)
                trace.decision("request", message="private question")
            result = trace.read(trace_id)
            self.assertEqual(result["content_capture"], 0)
            self.assertNotIn("private question", json.dumps(result))
        with patch.dict(os.environ, {"AGENT_TRACE_CONTENT_SESSION_IDS": "*"}), trace.turn() as trace_id:
            trace.bind_session("real-session")
            trace.decision("request", message="private question")
        self.assertNotIn("private question", json.dumps(trace.read(trace_id)))

    def test_credentials_nested_json_images_and_error_messages_are_excluded(self):
        with trace.turn() as trace_id:
            trace.bind_session("synthetic-test")
            trace.decision("input", data={"api_key": "hidden-key", "content": json.dumps({"password": "hidden-pass"}),
                           "text": "Bearer private-token sk-veryprivatekey123 https://user:pass@host.test/path?key=hidden-url",
                           "image": {"type": "input_image", "image_url": "data:image/png;base64,secretimage"}})
            with self.assertRaises(TimeoutError):
                trace.model_call("search", MagicMock(side_effect=TimeoutError("hidden-exception-secret")), model="fixture")
        result = trace.read(trace_id)
        text = json.dumps(result)
        for secret in ("hidden-key", "hidden-pass", "private-token", "sk-veryprivatekey123", "user:pass", "hidden-url", "secretimage", "hidden-exception-secret"):
            self.assertNotIn(secret, text)
        self.assertEqual(result["spans"][-1]["status"], "timeout")

    def test_parallel_fetches_preserve_parent_and_turn_isolation(self):
        def request(session):
            with trace.turn() as trace_id:
                trace.bind_session(session)
                with trace.span("sources"):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        list(f.result() for f in [pool.submit(copy_context().run, trace.decision, "fetch", url=f"https://example.org/{i}") for i in range(2)])
            return trace_id
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(request, ["synthetic-test", "other-session"]))
        self.assertNotEqual(*ids)
        for trace_id in ids:
            spans = trace.read(trace_id)["spans"]
            self.assertEqual(len(spans), 3)
            self.assertTrue(all(s["parent_id"] == spans[0]["id"] for s in spans[1:]))
        self.assertIsNone(trace._trace.get())

    def test_db_and_response_inspection_failures_preserve_actual_call(self):
        response = SimpleNamespace(output_text="answer", model_dump=MagicMock(side_effect=ValueError("bad serializer")))
        with trace.turn() as trace_id:
            self.assertIs(trace.model_call("care", lambda **kw: response, model="fixture"), response)
        self.assertEqual(trace.read(trace_id)["spans"][0]["capture_error_type"], "ValueError")
        with patch.object(db, "get_db", side_effect=OSError("unavailable")), trace.turn():
            self.assertEqual(trace.model_call("care", lambda **kw: "answer", model="fixture"), "answer")

    def test_bounds_and_expiration_are_explicit(self):
        with patch.object(trace, "MAX_SPANS", 2), trace.turn() as trace_id:
            trace.bind_session("synthetic-test")
            for i in range(4): trace.decision("step", index=i)
        self.assertEqual(trace.read(trace_id)["dropped_spans"], 2)
        trace.expire("2999-01-01")
        self.assertIsNone(trace.read(trace_id))

    def test_public_trace_contains_no_debug_content(self):
        @web_operations.traced_turn
        def request():
            trace.bind_session("synthetic-test")
            trace.decision("private", prompt="internal prompt")
            return web_operations.current_turn_trace()
        public = request()
        self.assertEqual(set(public), {"id", "events"})
        self.assertNotIn("internal prompt", json.dumps(public))
        self.assertIn("internal prompt", json.dumps(trace.read(public["id"])))

    def test_admin_endpoint_requires_existing_admin_credential(self):
        import app
        from fastapi.testclient import TestClient
        with trace.turn() as trace_id:
            trace.bind_session("synthetic-test")
            trace.decision("private", prompt="internal prompt")
        with patch.object(app.config, "ADMIN_PASSWORD", "test-only-admin"), TestClient(app.app) as client:
            self.assertEqual(client.get("/v1/admin/traces/" + trace_id).status_code, 403)
            response = client.get("/v1/admin/traces/" + trace_id, headers={"X-Admin-Password": "test-only-admin"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("internal prompt", response.text)


if __name__ == "__main__":
    unittest.main()
