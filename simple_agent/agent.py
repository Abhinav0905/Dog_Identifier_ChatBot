"""One agent selects tools; there are no routing or answer-review model calls."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time

from .catalog import PromptCatalog
from .tools import SearchTool, ImageAnalysisTool
from .tracing import TraceRecorder, public_response, utc_now


@dataclass(frozen=True)
class AgentConfig:
    model: str = "gpt-5.6-terra"
    vision_model: str = "gpt-5.6-terra"
    request_timeout: float = 35
    turn_budget: float = 80
    tool_budget: int = 8
    max_steps: int = 5


def citations(payload):
    rows = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            for annotation in content.get("annotations", []):
                if annotation.get("type") == "url_citation":
                    row = {k: annotation[k] for k in ("url", "title", "start_index", "end_index") if k in annotation}
                    if row not in rows:
                        rows.append(row)
    return rows


class SingleAgent:
    def __init__(self, client, config=None, prompts=None):
        self.client = client
        self.config = config or AgentConfig()
        self.prompts = prompts or PromptCatalog.load()
        self.photo_tool = ImageAnalysisTool(client, self.config.vision_model, self.prompts)

    def turn(self, question, history=None, *, image_path=None, trace_path=None):
        history = history if history is not None else []
        trace = TraceRecorder(trace_path)
        started = time.monotonic()
        deadline = started + self.config.turn_budget
        history_count = min(20, len(history))
        images = {}
        if image_path:
            path = Path(image_path).resolve()
            if not path.is_file():
                raise FileNotFoundError("Missing supplied photo")
            images["photo_1"] = path
        message = question + ("\n\nAvailable attachment: photo_1. Use analyze_photo to inspect it." if images else "")
        transcript = self.prompts.messages() + history[-20:] + [{"role": "user", "content": message}]
        trace.record("turn_start", question=question, history=history[-20:], image_ids=list(images),
                     prompt_hashes=self.prompts.hashes(), model=self.config.model)
        definitions = [SearchTool.definition(), self.photo_tool.definition()]
        calls_used = 0
        answer, status, error = "", "error", None
        sources, refusals = [], []
        for step in range(self.config.max_steps):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = {"type": "TurnDeadline"}
                break
            options = {"tools": definitions, "tool_choice": "auto", "max_tool_calls": max(1, self.config.tool_budget - calls_used)} if definitions else {}
            trace.record("agent_request", step=step, available_tools=[d.get("name", d["type"]) for d in definitions], remaining_seconds=round(remaining, 3))
            call_started = time.monotonic()
            try:
                response = self.client.responses.create(
                    model=self.config.model, input=transcript, store=False,
                    include=["web_search_call.action.sources", "reasoning.encrypted_content"],
                    reasoning={"effort": "low"}, max_output_tokens=3200,
                    timeout=min(self.config.request_timeout, remaining), **options,
                )
                payload = response.model_dump(mode="json")
                trace.record("agent", step=step, seconds=round(time.monotonic() - call_started, 3), response=public_response(payload))
                sources.extend(c for c in citations(payload) if c not in sources)
                outputs = payload.get("output", [])
                refusals.extend(c.get("refusal", "") for item in outputs for c in item.get("content", []) if c.get("type") == "refusal")
                calls_used += sum(item.get("type") in {"web_search_call", "function_call"} for item in outputs)
                functions = [item for item in outputs if item.get("type") == "function_call"]
                if not functions:
                    answer, status = response.output_text or "", response.status
                    if status != "completed":
                        error = {"type": "IncompleteResponse", "status": status}
                    elif not answer.strip():
                        status, error = "error", {"type": "EmptyAnswer"}
                    break
                # Opaque API reasoning state stays only in this continuation.
                transcript.extend(outputs)
                for call in functions:
                    try:
                        args = json.loads(call["arguments"])
                        result = self.photo_tool.execute(args, images, deadline, trace) if call["name"] == "analyze_photo" else {"error": "Unknown tool."}
                    except Exception as exc:
                        result = {"error": type(exc).__name__}
                    trace.record("function_result", name=call["name"], arguments=call["arguments"], output=result)
                    transcript.append({"type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(result, ensure_ascii=False)})
                if calls_used >= self.config.tool_budget:
                    definitions = []
            except Exception as exc:
                error = {"type": type(exc).__name__}
                trace.record("agent_error", step=step, error=error)
                break
        else:
            error = {"type": "AgentStepLimit"}
        rendered = re.sub(r"cite[^]*", "", answer).strip() if sources else answer.strip()
        trace.record("turn_end", status=status, error=error, answer=rendered, citations=sources)
        if rendered:
            history.extend([{"role": "user", "content": question},
                            {"role": "assistant", "content": rendered + "\n" + "\n".join(c["url"] for c in sources)}])
        return {"question": question, "status": status, "answer": rendered, "raw_answer": answer,
                "citations": sources, "trace": trace.events, "error": error, "api_refusal_items": refusals,
                "elapsed_seconds": round(time.monotonic() - started, 3), "history_messages": history_count,
                "completed_at": utc_now(), "verdict": "UNREVIEWED",
                "verdict_note": "A completed answer is not automatically a correctness pass."}
