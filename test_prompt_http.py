"""HTTP-level verification of prompt-only activation and request pinning."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import app
from scripts.manage_prompts import seal
from services import prompt_store
from services.prompts import PromptCatalog
import test_text_turns as fixtures


class TestPromptHTTP(unittest.TestCase):
    setUp = fixtures.TestTextConversations.setUp

    def test_activation_during_http_request_is_seen_only_by_next_request(self):
        with tempfile.TemporaryDirectory(prefix="gaia-http-prompts-") as directory:
            package = Path(directory) / "release"
            shutil.copytree(Path(__file__).parent / "prompts/runtime-v2", package)
            store = prompt_store.PromptStore(package)
            original = store.current().version
            seen = []

            def generate(*args, **kwargs):
                before = PromptCatalog.VERSION
                seal(package, "http-next-release")
                after = PromptCatalog.VERSION
                seen.append((before, after))
                return "Give the dog space and ask a trusted adult for help."

            with patch.object(prompt_store, "_store", store), patch.object(
                app.query_router, "plan_text_turn", return_value=fixtures.planned("answer", "Explain dog safety.")
            ), patch.object(app.triage, "generate_chat_response", side_effect=generate):
                first = self.client.post("/v1/chat/query", json={
                    "session_id": self.session, "message": "Explain dog safety.",
                })
                second = self.client.post("/v1/chat/query", json={
                    "session_id": self.session, "message": "Explain dog safety again.",
                })
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(first.headers["X-Prompt-Package-Version"], original)
            self.assertEqual(second.headers["X-Prompt-Package-Version"], "http-next-release")
            self.assertEqual(seen, [(original, original), ("http-next-release", "http-next-release")])

    def test_no_valid_package_returns_service_unavailable_without_model_call(self):
        store = prompt_store.PromptStore(Path(self.tmp.name) / "missing")
        with patch.object(prompt_store, "_store", store), patch.object(app.query_router, "plan_text_turn") as model:
            response = self.client.post("/v1/chat/query", json={
                "session_id": self.session, "message": "Explain dog safety.",
            })
        self.assertEqual(response.status_code, 503)
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
