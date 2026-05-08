"""
Vision triage service: uses the OpenAI Responses API for vision and chat
from uploaded stray dog images.
"""

import base64
import json
import logging
import time
from openai import OpenAI
from config import (
    DAR_PHONE_NUMBER,
    OPENAI_API_KEY,
    OPENAI_CHAT_MODEL,
    OPENAI_VISION_MODEL,
    ESCALATION_SEVERITY_THRESHOLD,
)

logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Number of times to retry a vision call on transient errors before giving up
VISION_MAX_ATTEMPTS = 2
# Media types we will forward to the vision API. iPhone HEIC and other exotic
# types are rejected here so we surface a clear error instead of a silent
# fallback.
VISION_SUPPORTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

VISION_SYSTEM_PROMPT = """You are a veterinary triage assistant for Dharamsala Animal Rescue.
You analyze images of stray dogs to assess their condition and urgency level.

IMPORTANT RULES:
- You are NOT providing a veterinary diagnosis. You are identifying visible distress indicators.
- Use youth-friendly, clear language.
- Be compassionate but factual.
- Never claim certainty about medical conditions.
- Always recommend professional veterinary assessment for anything beyond minor concerns.
- Recommended actions must follow a community-first workflow:
  1. First ask people nearby whether the dog has a feeder or owner.
  2. Ask whether local NGOs or community groups are already vaccinating and sterilizing dogs in the area.
  3. Do NOT recommend NGO pickup only because the dog looks a little thin.
  4. Recommend NGO or veterinary escalation when the dog appears injured, sick, immobile, or in immediate danger.
  5. Do not automatically tell people to keep their distance. Only recommend extra distance if the dog appears fearful, aggressive, or in severe pain. If the dog seems calm and locals say it is friendly, it is acceptable to suggest a slow, careful approach.

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
- Be youth-friendly, compassionate, and concise.
- Provide immediate safety guidance for dog bites (wash wound, seek medical attention, report).
- Never provide veterinary diagnoses or prescribe treatment.
- For stray dog welfare questions, start with community questions before escalation:
  1. Ask whether people nearby know the dog, feed the dog, or say it has an owner.
  2. Ask whether local NGOs or community groups are already vaccinating and sterilizing dogs in that area.
  3. If the dog only looks thin but is otherwise alert, suggest feeding, water, and monitoring rather than assuming an NGO pickup will happen.
  4. If the dog appears injured, sick, immobile, or in immediate danger, recommend a local NGO or veterinarian.
  5. In urban areas, feeders may be able to take the dog to a local veterinarian. In rural areas, the animal husbandry department may be the only public option, though support may be limited.
  6. Do not automatically say "keep your distance." Say that a calm, slow approach can be okay if locals say the dog is friendly. Recommend extra space only if the dog seems fearful, aggressive, or badly injured.
- Stay on topic: animal rescue, stray animal welfare, dog bite safety, reporting incidents.
- If asked about non-rescue topics, politely redirect to your purpose.
- If unsure about a situation's severity, err on the side of caution and recommend professional help.
- If a user asks for nearby vets or animal help, tell them to share location so the app can open Google Maps searches for nearby services."""

CHAT_SYSTEM_PROMPT = """You are a friendly helper for Dharamsala Animal Rescue. You help people
with questions about stray dogs and animal rescue in the Dharamsala area.

RULES:
- Use simple, clear words and short sentences. Write at a level anyone can easily understand.
- Be warm, calm, and encouraging - the person may be worried about an animal.
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

PROFESSIONAL_HELP_KEYWORDS = (
    "injur", "bleed", "wound", "fracture", "broken", "limp", "pain",
    "sick", "ill", "infection", "mange", "vomit", "immobile", "collapsed",
    "unable", "distress",
)

TRIAGE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {
            "type": "string",
            "enum": ["low", "moderate", "high", "critical"],
        },
        "severity_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "indicators": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recommended_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "triage_summary": {"type": "string"},
    },
    "required": [
        "severity",
        "severity_score",
        "confidence",
        "indicators",
        "recommended_actions",
        "triage_summary",
    ],
    "additionalProperties": False,
}


def analyze_image(image_bytes: bytes, media_type: str, user_context: str = "", language: str = "en") -> dict:
    """Send image to OpenAI for distress assessment.

    Adds logging + a single retry on transient errors so we can diagnose why
    "the bot sometimes fails to read the picture". Previously every error
    (network blip, empty response, unsupported media type, model name typo)
    was swallowed silently into a fallback with no breadcrumbs.
    """
    if not client:
        logger.warning("Vision triage skipped: OPENAI_API_KEY not configured")
        return _fallback_triage("OPENAI_API_KEY not configured")

    if not image_bytes:
        logger.warning("Vision triage skipped: empty image payload")
        return _fallback_triage("Empty image payload")

    normalized_media_type = (media_type or "").lower().strip()
    if normalized_media_type not in VISION_SUPPORTED_MEDIA_TYPES:
        logger.warning(
            "Vision triage skipped: unsupported media type %r (size=%d bytes)",
            media_type, len(image_bytes),
        )
        return _fallback_triage(
            f"Unsupported image type for vision model: {media_type or 'unknown'}"
        )

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    user_message = "Please analyze this image of a stray dog and assess its condition."
    if user_context:
        user_message += f"\n\nAdditional context from the reporter: {user_context}"

    last_error: str = ""
    start_time = time.time()

    for attempt in range(1, VISION_MAX_ATTEMPTS + 1):
        try:
            response = client.responses.create(
                model=OPENAI_VISION_MODEL,
                input=[
                    {"role": "system", "content": VISION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": user_message,
                            },
                            {
                                "type": "input_image",
                                "image_url": f"data:{normalized_media_type};base64,{image_b64}",
                            },
                        ],
                    },
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "animal_triage",
                        "schema": TRIAGE_RESPONSE_SCHEMA,
                        "strict": True,
                    }
                },
            )

            latency_ms = int((time.time() - start_time) * 1000)
            raw_text = (response.output_text or "").strip()
            if not raw_text:
                raise ValueError("OpenAI returned an empty structured response")

            result = _parse_triage_response(raw_text)
            result = apply_local_workflow_guidance(result, user_context=user_context)
            result["latency_ms"] = latency_ms
            result["model_version"] = OPENAI_VISION_MODEL
            result["raw_output"] = raw_text
            if attempt > 1:
                logger.info("Vision triage succeeded on attempt %d", attempt)
            return result

        except Exception as e:  # noqa: BLE001 - we log and retry/fallback
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "Vision triage attempt %d/%d failed (model=%s, media=%s, bytes=%d): %s",
                attempt, VISION_MAX_ATTEMPTS, OPENAI_VISION_MODEL,
                normalized_media_type, len(image_bytes), last_error,
            )

    logger.error("Vision triage giving up after %d attempts: %s",
                 VISION_MAX_ATTEMPTS, last_error)
    return _fallback_triage(last_error)


def _parse_triage_response(text: str) -> dict:
    """Parse and validate the structured triage JSON from OpenAI."""
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
            "Ask people nearby whether the dog has a regular feeder or owner.",
            "Ask whether local NGOs or community groups are already vaccinating or sterilizing dogs in this area.",
            f"If the dog appears injured, sick, immobile, or in immediate danger, contact Dharamsala Animal Rescue on {DAR_PHONE_NUMBER} or a local veterinarian.",
            "If the dog is only thin but alert, ask locals or feeders to provide food, water, and monitor it.",
            "Only keep extra distance if the dog seems fearful, aggressive, or in severe pain.",
        ],
        "triage_summary": "Automated image assessment is currently unavailable. Please describe what you see, and start by asking nearby people whether the dog already has a feeder or owner.",
        "escalation_needed": False,
        "is_fallback": True,
        "model_version": "fallback",
        "raw_output": error,
        "latency_ms": 0,
    }


def _language_instruction(language: str) -> str:
    if language == "hi":
        return "\n\nRespond in Hindi."
    return ""


def enrich_recommended_actions(triage_result: dict, language: str = "en") -> list[str]:
    """Use a second LLM call with RAG context to generate grounded recommended actions.

    The vision model's raw recommended_actions are replaced with advice that is
    specifically grounded in DAR's published knowledge base. Falls back to the
    original actions if the model is unavailable or the triage is a fallback.
    """
    if not client or triage_result.get("is_fallback"):
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
        response = client.responses.create(
            model=OPENAI_CHAT_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            max_output_tokens=512,
        )
        raw = (response.output_text or "").strip()
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
    if not client:
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
        response = client.responses.create(
            model=OPENAI_CHAT_MODEL,
            input=[{"role": "system", "content": CHAT_SYSTEM_PROMPT}] + messages,
        )
        return (response.output_text or "").strip() or _fallback_chat_response(message)
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
    if any(w in lower for w in ["vet near me", "nearby vet", "near me", "google maps", "maps", "clinic nearby"]):
        return (
            "If you share your location in the app, I can give you one-tap Google Maps links for nearby vets "
            "and animal help. You can also ask people nearby whether they already use a local feeder, clinic, "
            "or NGO for dogs in that area."
        )
    if any(w in lower for w in ["injured", "hurt", "bleeding", "broken"]):
        return (
            "If you've found an injured animal:\n\n"
            "1. **Ask people nearby first** whether the dog has a feeder or owner.\n"
            "2. **Ask whether local NGOs are already vaccinating or sterilizing dogs nearby** so you do not duplicate an existing effort.\n"
            f"3. **If the dog appears injured, sick, immobile, or unsafe, contact Dharamsala Animal Rescue on {DAR_PHONE_NUMBER} or a local veterinarian.**\n"
            "4. **If locals say the dog is friendly, you can approach slowly and calmly.** Keep more distance only if the dog seems fearful, aggressive, or in severe pain.\n"
            "5. **In towns, feeders may take the dog to a local veterinarian. In rural areas, the department of animal husbandry may be the only public option.**\n\n"
            "You can upload a photo of the animal for a condition assessment."
        )
    return (
        "Hello! I'm the Dharamsala Animal Rescue assistant. Examples of questions I can help you with:\n\n"
        "- **Reporting a stray dog** and figuring out whether locals already feed or know the dog\n"
        "- **Dog bite first aid** guidance\n"
        "- **Nearby help** like opening Google Maps searches for vets once you share location\n\n"
        "How can I help you today?"
    )


def apply_local_workflow_guidance(triage_result: dict, user_context: str = "") -> dict:
    """Normalize recommended actions around the rescue team's community-first workflow."""
    result = dict(triage_result)
    severity = (result.get("severity") or "unknown").lower()
    needs_professional_help = _needs_professional_help(result)

    actions = [
        "Ask people nearby whether this dog has a regular feeder or owner.",
        "Ask whether local NGOs or community groups are already vaccinating or sterilizing dogs in this area.",
    ]

    if needs_professional_help:
        actions.extend(
            [
                f"Because the dog appears injured, sick, immobile, or at immediate risk, contact Dharamsala Animal Rescue on {DAR_PHONE_NUMBER} or a local veterinarian as soon as possible.",
                "If locals say the dog is friendly, one calm person can stay nearby or help arrange transport while avoiding sudden handling.",
            ]
        )
    else:
        actions.extend(
            [
                "If the dog is only thin but alert and mobile, ask locals or feeders to provide food, water, and monitor it rather than assuming an NGO pickup will happen.",
                "In urban areas, feeders may be able to take the dog to a local veterinarian. In rural areas, the department of animal husbandry may be the only public option.",
            ]
        )

    if severity in {"high", "critical"} and not needs_professional_help:
        actions.append("If the dog worsens, becomes immobile, or looks obviously sick or injured, escalate to a local NGO or veterinarian quickly.")
    else:
        actions.append("Only keep extra distance if the dog seems fearful, aggressive, or in severe pain. A calm, slow approach can be okay if locals say the dog is friendly.")

    result["recommended_actions"] = actions[:5]
    result["triage_summary"] = _normalize_triage_summary(
        result.get("triage_summary", ""),
        needs_professional_help=needs_professional_help,
        user_context=user_context,
    )
    return result


def _needs_professional_help(triage_result: dict) -> bool:
    severity = (triage_result.get("severity") or "").lower()
    if severity in {"high", "critical"}:
        return True

    haystack = " ".join(
        [triage_result.get("triage_summary", "")]
        + list(triage_result.get("indicators") or [])
    ).lower()
    return any(keyword in haystack for keyword in PROFESSIONAL_HELP_KEYWORDS)


def _normalize_triage_summary(summary: str, needs_professional_help: bool, user_context: str = "") -> str:
    text = (summary or "We could not confidently assess the dog's condition from the image.").strip()
    lower = text.lower()
    if "feeder" in lower or "owner" in lower:
        return text

    if needs_professional_help:
        return text + " Ask nearby people for feeder or owner context while you arrange help."

    return text + " Start with community questions before escalating, especially if the dog looks thin but still alert."
