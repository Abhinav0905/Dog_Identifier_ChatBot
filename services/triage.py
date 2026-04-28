"""
Vision triage service: assesses distress indicators from uploaded stray dog images.
Supports Anthropic (Claude) and OpenAI via the MODEL_PROVIDER config flag.
"""

import base64
import json
import time
from config import ESCALATION_SEVERITY_THRESHOLD
from services import ai_client
import database as db

VISION_SYSTEM_PROMPT = """You are an animal rescue helper for Dharamsala Animal Rescue.
You look at photos of stray dogs and describe what you can see about how the dog is doing.

IMPORTANT RULES:
- You are NOT a vet and you are NOT giving a medical diagnosis. You are only describing what you can see.
- Write simply and kindly — your answers will be read by children and young people.
- Do not say you are certain about anything — only describe what you can see.

Analyze the image and respond with ONLY a valid JSON object (no markdown, no extra text):
{
    "severity": "low|moderate|high|critical",
    "severity_score": <1-10 integer>,
    "confidence": <0.0-1.0 float>,
    "indicators": ["list of things you can see that suggest the dog needs help"],
    "recommended_actions": ["simple safety steps if enrichment is unavailable"],
    "triage_summary": "A short, simple description of how the dog looks — written so a child can understand"
}

Severity guide:
- low (1-3): Dog looks mostly okay, maybe a small concern
- moderate (4-6): Dog looks like it has not been cared for, or has a minor injury or illness
- high (7-8): Dog is clearly in pain or distress, or is very thin or injured
- critical (9-10): Dog is in serious danger — badly hurt, cannot move, or looks very ill"""

ENRICHMENT_SYSTEM_PROMPT = """You are a friendly helper for Dharamsala Animal Rescue.
You will be given a description of how a stray dog looks and some helpful information from our rescue team.

Your job is to give the person 3–5 simple things they can do right now to help the dog safely.

RULES:
- Use simple, clear words and short sentences — easy for anyone to understand and act on.
- Only suggest things the person can do right now where they are.
- Do NOT tell them to call a helpline or share their location — our rescue team will handle that.
- Do NOT suggest any medicine or treatment.
- Be kind, calm, and encouraging — the person is trying to help and may be worried.
- Respond with ONLY a valid JSON array of strings, no markdown, no extra text.
  Example: ["Stay calm and keep a safe distance from the dog.", "Do not try to pick up or move the dog."]"""

CHAT_SYSTEM_PROMPT = """You are a friendly helper for Dharamsala Animal Rescue. You help people
with questions about stray dogs and animal rescue in the Dharamsala area.

RULES:
- Use simple, clear words and short sentences. Write at a level anyone can easily understand.
- Be warm, calm, and encouraging — the person may be worried about an animal.
- Never give a medical diagnosis or suggest any medicine or treatment.
- Do NOT tell users to call a helpline or share their location — our rescue team handles that.
- Only answer questions about animal rescue, stray dogs, dog bites, and animal safety.
- If someone asks about something else, kindly explain what you can help with instead.
- If you are not sure how serious a situation is, describe what signs to look for and suggest the person stay nearby and keep watching the dog from a safe distance.

If someone has been bitten by a dog, always tell them to:
1. Wash the bite right away with soap and water for at least 10 minutes
2. See a doctor as soon as possible
3. Report it so the dog can be checked
4. Watch the wound for any signs of infection"""


def _language_instruction(lang: str) -> str:
    """Return a system prompt suffix that instructs the model to respond in the user's language."""
    if lang == "hi":
        return "\nRespond in Hindi (Devanagari script)."
    return "\nRespond in English."


def analyze_image(image_bytes: bytes, media_type: str, user_context: str = "", language: str = "en") -> dict:
    """Send image to the configured AI provider for distress assessment."""
    if not ai_client.is_available():
        return _fallback_triage()

    start_time = time.time()
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    user_message = "Please analyze this image of a stray dog and assess its condition."
    if user_context:
        user_message += f"\n\nAdditional context from the reporter: {user_context}"

    system_prompt = VISION_SYSTEM_PROMPT + _language_instruction(language)

    try:
        raw_text = ai_client.create_vision_completion(
            system_prompt=system_prompt,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_message,
        )

        latency_ms = int((time.time() - start_time) * 1000)

        # Parse the JSON response
        result = _parse_triage_response(raw_text)
        result["latency_ms"] = latency_ms
        result["model_version"] = ai_client.get_model_name()
        result["raw_output"] = raw_text
        return result

    except Exception as e:
        return _fallback_triage(str(e))


def _parse_triage_response(text: str) -> dict:
    """Parse and validate the structured triage JSON from Claude."""
    try:
        # Try to extract JSON from the response
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)

        severity = data.get("severity", "moderate")
        score = max(1, min(10, int(data.get("severity_score", 5))))
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))

        return {
            "severity": severity,
            "severity_score": score,
            "confidence": confidence,
            "indicators": data.get("indicators", []),
            "recommended_actions": data.get("recommended_actions", []),
            "triage_summary": data.get("triage_summary", "Unable to determine condition."),
            "escalation_needed": score >= ESCALATION_SEVERITY_THRESHOLD,
        }
    except (json.JSONDecodeError, KeyError, ValueError):
        return _fallback_triage("Failed to parse vision model response")


def _fallback_triage(error: str = "") -> dict:
    """Return a safe fallback triage when the model is unavailable."""
    return {
        "severity": "unknown",
        "severity_score": None,
        "confidence": None,
        "indicators": [],
        "recommended_actions": [
            "Please describe how the dog looks in a text message",
            "Stay a safe distance away from the dog",
        ],
        "triage_summary": "We could not check the photo right now. Can you describe how the dog looks? We will do our best to help.",
        "escalation_needed": False,
        "is_fallback": True,
        "model_version": "fallback",
        "raw_output": error,
        "latency_ms": 0,
    }


def enrich_recommended_actions(triage_result: dict, language: str = "en") -> list[str]:
    """Use a second LLM call with RAG context to generate grounded recommended actions.

    The vision model's raw recommended_actions are replaced with advice that is
    specifically grounded in DAR's published knowledge base. Falls back to the
    original actions if the model is unavailable or the triage is a fallback.
    """
    if not ai_client.is_available() or triage_result.get("is_fallback"):
        return triage_result.get("recommended_actions", [])

    from services import rag

    indicators = triage_result.get("indicators", [])
    summary = triage_result.get("triage_summary", "")
    rag_query = f"{summary} {' '.join(indicators[:3])}"

    chunks = rag.retrieve(rag_query, k=2)
    rag_context = rag.format_context(chunks)

    user_message = (
        f"Triage assessment:\n"
        f"- Severity: {triage_result.get('severity')} ({triage_result.get('severity_score')}/10)\n"
        f"- Summary: {summary}\n"
        f"- Observed indicators: {', '.join(indicators)}\n"
        f"- Original recommended actions: {', '.join(triage_result.get('recommended_actions', []))}"
    )

    system_prompt = ENRICHMENT_SYSTEM_PROMPT + _language_instruction(language)
    if rag_context:
        system_prompt = rag_context + "\n\n" + system_prompt

    try:
        raw = ai_client.create_chat_completion(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=512,
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        actions = json.loads(raw)
        if isinstance(actions, list) and all(isinstance(a, str) for a in actions):
            return actions
    except Exception:
        pass

    return triage_result.get("recommended_actions", [])


def generate_chat_response(message: str, history: list[dict], session_id: str, language: str = "en") -> str:
    """Generate a chat response for text queries, augmented with RAG context."""
    if not ai_client.is_available():
        return _fallback_chat_response(message)

    # Retrieve relevant knowledge chunks
    from services import rag
    chunks = rag.retrieve(message)
    rag_context = rag.format_context(chunks)

    system_prompt = CHAT_SYSTEM_PROMPT + _language_instruction(language)
    if rag_context:
        system_prompt = rag_context + "\n\n" + system_prompt

    messages = []
    for entry in history[-10:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": message})

    try:
        return ai_client.create_chat_completion(
            system_prompt=system_prompt,
            messages=messages,
        )
    except Exception:
        return _fallback_chat_response(message)


def _fallback_chat_response(message: str) -> str:
    """Provide a helpful response when the AI model is unavailable."""
    lower = message.lower()
    if any(w in lower for w in ["bite", "bitten", "bit me", "bit my"]):
        return (
            "**What to do after a dog bite:**\n\n"
            "1. **Wash the wound** with soap and running water for at least 10 minutes — do this right away.\n"
            "2. **Put a clean bandage** over the wound if you have one.\n"
            "3. **See a doctor as soon as you can** — they will check if you need any treatment.\n"
            "4. **Report it** so the dog can be found and checked.\n\n"
            "This is general guidance. Please see a doctor promptly."
        )
    if any(w in lower for w in ["injured", "hurt", "bleeding", "broken"]):
        return (
            "If you have found a dog that is hurt:\n\n"
            "1. **Keep your distance** — a hurt dog may bite even if it is usually friendly.\n"
            "2. **Do not try to move it** unless it is in immediate danger.\n"
            "3. **Stay nearby if it is safe** so you can show rescuers where the dog is.\n\n"
            "You can upload a photo of the dog and we will check how serious it looks."
        )
    return (
        "Hi! I am the Dharamsala Animal Rescue helper. Here is what I can help with:\n\n"
        "- **Reporting a dog** that looks hurt or sick — upload a photo and we will check it\n"
        "- **Dog bite advice** — what to do if a dog bites you\n"
        "- **Rescue questions** about stray dogs in the Dharamsala area\n\n"
        "What do you need help with?"
    )
