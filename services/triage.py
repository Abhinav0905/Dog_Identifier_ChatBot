"""
Vision triage service: uses Claude vision API to assess distress indicators
from uploaded stray dog images.
"""

import base64
import json
import time
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, ESCALATION_SEVERITY_THRESHOLD
import database as db

client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

VISION_SYSTEM_PROMPT = """You are a veterinary triage assistant for Dharmasala Animal Rescue.
You analyze images of stray dogs to assess their condition and urgency level.

IMPORTANT RULES:
- You are NOT providing a veterinary diagnosis. You are identifying visible distress indicators.
- Use youth-friendly, clear language.
- Be compassionate but factual.
- Never claim certainty about medical conditions.
- Always recommend professional veterinary assessment for anything beyond minor concerns.

Analyze the image and respond with ONLY a valid JSON object (no markdown, no extra text):
{
    "severity": "low|moderate|high|critical",
    "severity_score": <1-10 integer>,
    "confidence": <0.0-1.0 float>,
    "indicators": ["list of visible distress indicators"],
    "recommended_actions": ["list of recommended next steps"],
    "triage_summary": "Brief youth-friendly summary of the animal's apparent condition"
}

Severity guide:
- low (1-3): Animal appears generally healthy, minor concerns
- moderate (4-6): Visible signs of neglect, minor injury, or illness
- high (7-8): Clear distress, significant injury, emaciation, or illness
- critical (9-10): Life-threatening condition, severe injury, immobility, extreme distress"""

CHAT_SYSTEM_PROMPT = """You are the Dharmasala Animal Rescue chatbot assistant. You help the public
and volunteers with animal rescue questions, particularly about stray dogs in the Dharamsala area.

RULES:
- Be youth-friendly, compassionate, and concise.
- Provide immediate safety guidance for dog bites (wash wound, seek medical attention, report).
- Never provide veterinary diagnoses or prescribe treatment.
- For emergencies, always recommend contacting local rescue/veterinary services.
- Stay on topic: animal rescue, stray animal welfare, dog bite safety, reporting incidents.
- If asked about non-rescue topics, politely redirect to your purpose.
- If unsure about a situation's severity, err on the side of caution and recommend professional help.

For dog bite queries, always include:
1. Immediate first aid (wash with soap and water for 10+ minutes)
2. Seek medical attention promptly
3. Report the incident to local animal control
4. Monitor the wound for signs of infection
5. Ask about rabies vaccination status if the dog is known"""


def analyze_image(image_bytes: bytes, media_type: str, user_context: str = "") -> dict:
    """Send image to Claude for distress assessment."""
    if not client:
        return _fallback_triage()

    start_time = time.time()
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    user_message = "Please analyze this image of a stray dog and assess its condition."
    if user_context:
        user_message += f"\n\nAdditional context from the reporter: {user_context}"

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024,
            system=VISION_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": user_message},
                    ],
                }
            ],
        )

        latency_ms = int((time.time() - start_time) * 1000)
        raw_text = response.content[0].text

        # Parse the JSON response
        result = _parse_triage_response(raw_text)
        result["latency_ms"] = latency_ms
        result["model_version"] = "claude-sonnet-4-5-20250929"
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
        "severity": "moderate",
        "severity_score": 5,
        "confidence": 0.3,
        "indicators": ["Unable to perform automated assessment"],
        "recommended_actions": [
            "Please describe the animal's condition in text",
            "If the animal appears injured or in distress, contact local rescue services",
            "Keep a safe distance from the animal",
        ],
        "triage_summary": "Automated image assessment is currently unavailable. Please provide a text description of the animal's condition so we can assist you.",
        "escalation_needed": False,
        "model_version": "fallback",
        "raw_output": error,
        "latency_ms": 0,
    }


def generate_chat_response(message: str, history: list[dict], session_id: str) -> str:
    """Generate a chat response for text queries."""
    if not client:
        return _fallback_chat_response(message)

    messages = []
    for entry in history[-10:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": message})

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024,
            system=CHAT_SYSTEM_PROMPT,
            messages=messages,
        )
        return response.content[0].text
    except Exception:
        return _fallback_chat_response(message)


def _fallback_chat_response(message: str) -> str:
    """Provide a helpful response when the AI model is unavailable."""
    lower = message.lower()
    if any(w in lower for w in ["bite", "bitten", "bit me", "bit my"]):
        return (
            "**Dog Bite First Aid:**\n\n"
            "1. **Wash the wound** immediately with soap and running water for at least 10 minutes.\n"
            "2. **Apply antiseptic** if available, then cover with a clean bandage.\n"
            "3. **Seek medical attention** as soon as possible - a doctor will assess if rabies post-exposure treatment is needed.\n"
            "4. **Report the incident** to local animal control so the dog can be monitored.\n"
            "5. **Note the dog's appearance and location** for the report.\n\n"
            "This is general first-aid guidance, not medical advice. Please see a healthcare professional promptly."
        )
    if any(w in lower for w in ["injured", "hurt", "bleeding", "broken"]):
        return (
            "If you've found an injured animal:\n\n"
            "1. **Keep a safe distance** - injured animals may bite out of fear.\n"
            "2. **Do not attempt to move** the animal unless it's in immediate danger.\n"
            "3. **Contact local rescue services** or a veterinarian.\n"
            "4. **Note the exact location** and share it with responders.\n"
            "5. **Stay nearby if safe** to guide rescuers to the spot.\n\n"
            "You can upload a photo of the animal for a condition assessment."
        )
    return (
        "Hello! I'm the Dharmasala Animal Rescue assistant. I can help with:\n\n"
        "- **Reporting a stray dog** in distress (upload a photo for assessment)\n"
        "- **Dog bite first aid** guidance\n"
        "- **Rescue questions** about stray animals in the Dharamsala area\n\n"
        "How can I help you today?"
    )
