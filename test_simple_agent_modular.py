"""Verify tool continuation, trace durability and failure handling, not answer quality."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from simple_agent.agent import AgentConfig, SingleAgent
from simple_agent.catalog import PromptCatalog
from simple_agent.tools import ImageAnalysisTool
from simple_agent.tracing import TraceRecorder


def response(output, text="", status="completed"):
    result = MagicMock(status=status, output_text=text)
    result.model_dump.return_value = {"id": "test", "status": status, "model": "test", "output": output}
    return result


def message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}


class ModularAgentTests(unittest.TestCase):
    def test_photo_result_returns_to_agent_and_trace_excludes_image_and_reasoning(self):
        with TemporaryDirectory() as directory:
            image = Path(directory) / "dog.png"
            image.write_bytes(b"test photo bytes")
            trace = Path(directory) / "trace.jsonl"
            call = {"type": "function_call", "name": "analyze_photo", "arguments": json.dumps({"image_id": "photo_1", "question": "Look"}), "call_id": "call_1"}
            observation = {"observations": ["A dog"], "limitations": ["No diagnosis"], "urgency": "uncertain", "actions": [], "summary": "A dog", "usable_animal_photo": True}
            client = MagicMock()
            client.responses.create.side_effect = [
                response([{"type": "reasoning", "encrypted_content": "hidden-state"}, call]),
                response([message(json.dumps(observation))], json.dumps(observation)),
                response([message("Visible dog; health uncertain.")], "Visible dog; health uncertain."),
            ]
            history = []
            row = SingleAgent(client).turn("Look", history, image_path=image, trace_path=trace)
            self.assertEqual(row["status"], "completed")
            self.assertEqual(len(history), 2)
            continuation = client.responses.create.call_args_list[2].kwargs["input"]
            self.assertTrue(any(x.get("type") == "function_call_output" for x in continuation))
            self.assertTrue(any(x.get("encrypted_content") == "hidden-state" for x in continuation))
            saved = trace.read_text()
            self.assertNotIn("hidden-state", saved)
            self.assertNotIn("data:image", saved)
            self.assertEqual(len(saved.splitlines()), len(row["trace"]))

    def test_unknown_photo_id_never_reads_arbitrary_file_or_calls_api(self):
        client = MagicMock()
        result = ImageAnalysisTool(client, "test", PromptCatalog.load()).execute(
            {"image_id": "../../secret", "question": "Look"}, {}, 1, TraceRecorder())
        self.assertIn("error", result)
        client.responses.create.assert_not_called()

    def test_failed_and_empty_responses_are_not_counted_as_completed(self):
        for value in [TimeoutError(), response([], "")]:
            with self.subTest(value=value):
                client = MagicMock()
                client.responses.create.side_effect = [value]
                history = []
                row = SingleAgent(client).turn("Find care", history)
                self.assertEqual(row["status"], "error")
                self.assertTrue(row["error"])
                self.assertEqual(history, [])

    def test_tool_error_is_returned_to_model_and_recorded(self):
        client = MagicMock()
        call = {"type": "function_call", "name": "analyze_photo", "arguments": "not-json", "call_id": "c1"}
        client.responses.create.side_effect = [response([call]), response([message("Unable to inspect a photo.")], "Unable to inspect a photo.")]
        row = SingleAgent(client).turn("Look")
        result = next(e for e in row["trace"] if e["stage"] == "function_result")
        self.assertIn("error", result["output"])
        self.assertEqual(client.responses.create.call_count, 2)

    def test_history_window_and_number_only_output_are_preserved(self):
        client = MagicMock()
        client.responses.create.return_value = response([message("123")], "123")
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(28)]
        row = SingleAgent(client).turn("Only the number", history)
        self.assertEqual(row["answer"], "123")
        self.assertEqual(row["history_messages"], 20)
        sent = client.responses.create.call_args.kwargs["input"]
        self.assertEqual(sent[2]["content"], "8")
        self.assertEqual(len(sent), 23)

    def test_tool_budget_disables_further_tools(self):
        client = MagicMock()
        call = {"type": "function_call", "name": "analyze_photo", "arguments": "{}", "call_id": "c1"}
        client.responses.create.side_effect = [response([call]), response([message("No image supplied.")], "No image supplied.")]
        SingleAgent(client, AgentConfig(tool_budget=1)).turn("Look")
        self.assertNotIn("tools", client.responses.create.call_args_list[1].kwargs)


if __name__ == "__main__":
    unittest.main()
