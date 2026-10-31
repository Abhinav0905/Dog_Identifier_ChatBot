"""HTTP integration and atomic release regressions, with no external calls."""
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from PIL import Image
import app
import config
import database as db
from services import single_agent_runtime as runtime, web_operations, agent_trace
from simple_agent.release import load_bundle
from scripts.manage_prompts import activate
from test_simple_agent_modular import response, message
from test_text_turns import TestTextConversations


class SingleAgentHTTPTests(unittest.TestCase):
    def setUp(self):
        TestTextConversations.setUp(self)
        web_operations.initialize()
        for target, name, value in [(config, "CHAT_ENGINE", "single_agent")]:
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.model = MagicMock()
        patcher = patch.object(runtime, "client", return_value=self.model)
        patcher.start()
        self.addCleanup(patcher.stop)
        network = patch("socket.socket.connect", side_effect=AssertionError("Network disabled"))
        network.start()
        self.addCleanup(network.stop)

    def post(self, text):
        return self.client.post("/v1/chat/query", json={"session_id": self.session, "message": text})

    def test_real_agent_http_history_number_contract_and_private_traces(self):
        self.model.responses.create.side_effect = [response([message("01894230000")], "01894230000"),
                                                   response([message("Yes.")], "Yes.")]
        with patch.object(app.query_router, "plan_text_turn", side_effect=AssertionError("Legacy router")), \
             patch.object(app.guardrails, "check_input", side_effect=AssertionError("Legacy filter")):
            first = self.post("Only the college number")
            second = self.post("Is that the same college?")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["response"], "01894230000")
        self.assertEqual(first.json()["answer_format"], "phone_only")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.headers["x-prompt-package-version"], "single-agent-v1.0.0-2026-10-28")
        transcript = self.model.responses.create.call_args.kwargs["input"]
        self.assertIn({"role": "assistant", "content": "01894230000"}, transcript)
        history = db.get_chat_history(self.session)
        trace = agent_trace.read(history[-1]["metadata"]["stage_trace"]["id"])
        self.assertEqual(trace["prompt_version"], first.headers["x-prompt-package-version"])
        self.assertTrue(any(s["name"] == "model.single_agent" for s in trace["spans"]))
        self.assertNotIn("Is that the same college?", json.dumps(trace))

    def test_failure_does_not_save_success_or_advance_history(self):
        self.model.responses.create.side_effect = TimeoutError()
        result = self.post("Find a vet")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(db.get_chat_history(self.session), [])

    def test_invalid_release_blocks_request_before_model(self):
        with patch.object(config, "SIMPLE_AGENT_PROMPT_DIR", Path(self.tmp.name) / "missing"):
            result = self.post("Help")
        self.assertEqual(result.status_code, 503)
        self.model.responses.create.assert_not_called()

    def test_two_conversations_do_not_share_history(self):
        self.model.responses.create.return_value = response([message("Give the dog space.")], "Give the dog space.")
        self.post("Dog in Hamirpur, HP")
        self.session = self.client.post("/v1/conversations").json()["conversation_id"]
        self.post("New case: help in Una")
        self.assertNotIn("Hamirpur", json.dumps(self.model.responses.create.call_args.kwargs["input"]))

    def test_photo_tool_continuation_and_no_incident_without_precise_local_pin(self):
        photo = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(photo, "PNG")
        observation = {"observations": [], "limitations": ["Blank image"], "urgency": "uncertain", "actions": [], "summary": "No animal visible", "usable_animal_photo": False}
        call = {"type": "function_call", "name": "analyze_photo", "arguments": '{"image_id":"photo_1","question":"Look"}', "call_id": "c1"}
        self.model.responses.create.side_effect = [response([call]), response([message(json.dumps(observation))], json.dumps(observation)), response([message("I cannot see an animal.")], "I cannot see an animal.")]
        with patch.object(app, "_resolve_upload_location", return_value=(None, None, None, "")), \
             patch.object(app.triage, "analyze_image", side_effect=AssertionError("Legacy photo answer")), \
             patch.object(db, "create_incident", side_effect=AssertionError("No pin")), \
             patch.object(app.alerts, "send_alert", side_effect=AssertionError("No alert")):
            result = self.client.post("/v1/triage/image", data={"session_id": self.session}, files={"image": ("test.png", photo.getvalue(), "image/png")})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("I cannot see an animal", result.json()["response"])
        self.assertFalse(result.json()["escalation_triggered"])
        trace_id = db.get_chat_history(self.session)[-1]["metadata"]["stage_trace"]["id"]
        trace = agent_trace.read(trace_id)
        self.assertTrue(any(s["name"] == "model.single_agent_photo" for s in trace["spans"]))
        self.assertNotIn("data:image", json.dumps(trace))

    def test_local_photo_preserves_report_and_failed_delivery_notice(self):
        from models import ChatResponse
        photo = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(photo, "PNG")
        assessment = {"severity": "severe", "severity_score": 8, "confidence": 0.9,
                      "indicators": ["injury"], "recommended_actions": [],
                      "escalation_needed": True, "triage_summary": "Injury"}
        with patch.object(runtime, "answer", return_value=ChatResponse(response="Please seek urgent veterinary care.")), \
             patch.object(app, "_resolve_upload_location", return_value=({}, 32.22, 76.32, "browser")), \
             patch.object(app.location, "is_in_dharamsala_region", return_value=True), \
             patch.object(app.region_scope, "upload_is_in_india", return_value=True), \
             patch.object(config, "STORAGE_DIR", Path(self.tmp.name)), \
             patch.object(app.triage, "analyze_image", return_value=assessment), \
             patch.object(app.similarity, "run_similarity_checks", return_value={"similar_incidents": []}), \
             patch.object(app.alerts, "send_alert", return_value=SimpleNamespace(delivered=False, status="failed")) as alert:
            result = self.client.post("/v1/triage/image", data={"session_id": self.session, "lat": "32.22", "lng": "76.32", "location_source": "browser"}, files={"image": ("test.png", photo.getvalue(), "image/png")})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("Please seek urgent veterinary care", result.json()["response"])
        self.assertIn("no rescue notification was delivered", result.json()["response"])
        alert.assert_called_once()
        with db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)


class ReleaseTests(unittest.TestCase):
    def test_verified_activation_rollback_and_request_pinning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = config.BASE_DIR / "prompts/V1"
            current = root / "current"
            first = activate(source, current, loader=load_bundle)
            next_source = root / "next"
            shutil.copytree(source, next_source)
            manifest = json.loads((next_source / "manifest.json").read_text())
            manifest["version"] = "test-v2"
            (next_source / "manifest.json").write_text(json.dumps(manifest))
            with patch.object(config, "SIMPLE_AGENT_PROMPT_DIR", current):
                with runtime.pin(runtime.current()) as old:
                    activate(next_source, current, loader=load_bundle)
                    self.assertEqual(runtime.current().version, old.version)
                self.assertEqual(runtime.current().version, "test-v2")
                activate(source, current, loader=load_bundle)
                self.assertEqual(runtime.current().digest, first["sha256"])
                (next_source / "care.txt").write_text("tampered")
                with self.assertRaises(ValueError):
                    activate(next_source, current, loader=load_bundle)
                self.assertEqual(runtime.current().digest, first["sha256"])

    def test_legacy_bundle_is_not_accepted_as_v1(self):
        with self.assertRaises(ValueError):
            load_bundle(config.BASE_DIR / "prompts/runtime-v2")


if __name__ == "__main__":
    unittest.main()
