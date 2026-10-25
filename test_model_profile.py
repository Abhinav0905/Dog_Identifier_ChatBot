"""GPT-5.6 compatibility of the retained Chat Completions adapter."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from services import ai_client


class TestModelProfile(unittest.TestCase):
    def make_client(self):
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="test answer"))],
        )
        return client

    def test_text_adapter_uses_completion_token_budget_for_gpt56(self):
        client = self.make_client()
        with patch("openai.OpenAI", return_value=client), patch.object(ai_client, "_OPENAI_MODEL", "gpt-5.6-sol"):
            answer = ai_client._openai_chat("system", [{"role": "user", "content": "test"}], 256)
        self.assertEqual(answer, "test answer")
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertNotIn("max_tokens", kwargs)
        self.assertEqual(kwargs["max_completion_tokens"], 256)
        self.assertEqual(kwargs["reasoning_effort"], "none")

    def test_vision_adapter_respects_vision_role_instead_of_text_model(self):
        client = self.make_client()
        with patch("openai.OpenAI", return_value=client), patch.object(ai_client, "OPENAI_VISION_MODEL", "gpt-5.6-terra"), patch.object(ai_client, "_OPENAI_MODEL", "gpt-5.6-sol"):
            ai_client._openai_vision("system", "AA==", "image/png", "test", 128)
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-5.6-terra")
        self.assertEqual(kwargs["max_completion_tokens"], 128)
        self.assertNotIn("max_tokens", kwargs)


if __name__ == "__main__":
    unittest.main()
