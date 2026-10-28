"""Two capabilities: native web search and an allowlisted image-analysis function."""
import base64
import hashlib
import json
import mimetypes
import time

from .tracing import public_response


class SearchTool:
    @staticmethod
    def definition():
        return {"type": "web_search", "search_context_size": "medium",
                "user_location": {"type": "approximate", "country": "IN"}}


class ImageAnalysisTool:
    schema = {
        "type": "object", "properties": {
            "observations": {"type": "array", "items": {"type": "string"}},
            "limitations": {"type": "array", "items": {"type": "string"}},
            "urgency": {"type": "string", "enum": ["uncertain", "routine", "prompt_vet", "emergency"]},
            "actions": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"}, "usable_animal_photo": {"type": "boolean"}},
        "required": ["observations", "limitations", "urgency", "actions", "summary", "usable_animal_photo"],
        "additionalProperties": False,
    }

    def __init__(self, client, model, prompts):
        self.client, self.model, self.prompts = client, model, prompts

    @staticmethod
    def definition():
        return {"type": "function", "name": "analyze_photo", "strict": True,
                "description": "Inspect a supplied photo for visible animal-welfare concerns. Use only the current attachment ID; do not invent an image.",
                "parameters": {"type": "object", "properties": {
                    "image_id": {"type": "string", "description": "ID from current available attachments"},
                    "question": {"type": "string", "description": "Reporter question, relevant symptoms and desired language"}},
                    "required": ["image_id", "question"], "additionalProperties": False}}

    def execute(self, args, images, deadline, trace):
        if not isinstance(args, dict) or args.get("image_id") not in images:
            return {"error": "No supplied image with this ID."}
        if not isinstance(args.get("question"), str):
            return {"error": "A photo question is required."}
        path = images[args["image_id"]]
        mime = mimetypes.guess_type(path.name)[0]
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            return {"error": "Unsupported image format."}
        if path.stat().st_size > 20 * 1024 * 1024:
            return {"error": "Photo exceeds the size limit."}
        data = path.read_bytes()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"error": "Photo analysis deadline reached."}
        trace.record("photo_request", image_id=args["image_id"], question=args["question"],
                     image_sha256=hashlib.sha256(data).hexdigest(), model=self.model)
        started = time.monotonic()
        response = self.client.responses.create(
            model=self.model, store=False, max_output_tokens=1400,
            reasoning={"effort": "low"}, timeout=min(45, remaining),
            input=[{"role": "system", "content": self.prompts.photo},
                   {"role": "developer", "content": self.prompts.guardrails},
                   {"role": "user", "content": [
                       {"type": "input_text", "text": args["question"]},
                       {"type": "input_image", "image_url": f"data:{mime};base64," + base64.b64encode(data).decode(), "detail": "auto"}]}],
            text={"format": {"type": "json_schema", "name": "photo_observations", "schema": self.schema, "strict": True}},
        )
        payload = response.model_dump(mode="json")
        trace.record("photo_analysis", image_id=args["image_id"], seconds=round(time.monotonic() - started, 3), response=public_response(payload))
        if response.status != "completed":
            return {"error": "Photo analysis did not complete."}
        return json.loads(response.output_text)
