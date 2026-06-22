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
from services import image_processing

logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Number of times to retry a vision call on transient errors before giving up
VISION_MAX_ATTEMPTS = 2
VISION_SYSTEM_PROMPT = """You are an animal welfare photo triage assistant for Dharamsala Animal Rescue.
You analyze images of stray dogs to assess their condition and urgency level.

IMPORTANT RULES:
- You are NOT providing a veterinary diagnosis. You are identifying visible distress indicators.
- Use youth-friendly, clear language.
- Be compassionate but factual.
- Never claim certainty about medical conditions.
- For animal welfare concerns beyond minor issues, recommend a local animal rescue organisation, animal welfare NGO, or trained rescuer.
- Do not mention animal control, local authorities, municipal animal services, SPCA, police, Google Maps, or external websites.
- Recommended actions must follow a community-first workflow:
  1. First ask people nearby whether the dog has a feeder or owner.
  2. Ask whether local NGOs or community groups are already vaccinating and sterilizing dogs in the area.
  3. Do NOT recommend NGO pickup only because the dog looks a little thin.
  4. Recommend animal rescue/NGO escalation when the dog appears injured, sick, immobile, or in immediate danger.
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
- Provide immediate safety guidance for dog bites: wash the wound and seek medical attention promptly.
- Never provide veterinary diagnoses or prescribe treatment.
- Do not mention animal control, local authorities, municipal animal services, SPCA, police, Google Maps, or external websites.
- For stray dog welfare questions, start with community questions before escalation:
  1. Ask whether people nearby know the dog, feed the dog, or say it has an owner.
  2. Ask whether local NGOs or community groups are already vaccinating and sterilizing dogs in that area.
  3. If the dog only looks thin but is otherwise alert, suggest feeding, water, and monitoring rather than assuming an NGO pickup will happen.
  4. If the dog appears injured, sick, immobile, or in immediate danger, recommend a local animal rescue organisation, animal welfare NGO, or local nonprofit.
  5. In rural areas, the animal husbandry department may be the only public option, though support may be limited.
  6. Do not automatically say "keep your distance." Say that a calm, slow approach can be okay if locals say the dog is friendly. Recommend extra space only if the dog seems fearful, aggressive, or badly injured.
- Stay on topic: animal rescue, stray animal welfare, dog bite safety, and animal safety.
- If asked about non-rescue topics, politely redirect to your purpose.
- If unsure about a situation's severity, err on the side of caution and recommend professional help.
- If a user asks for help outside Dharamsala, suggest a local animal rescue organisation, animal welfare NGO, or local nonprofit."""

CHAT_SYSTEM_PROMPT = """You are a friendly helper for Dharamsala Animal Rescue. You help people
with questions about stray dogs and animal rescue in the Dharamsala area.

RULES:
- Use simple, clear words and short sentences. Write at a level anyone can easily understand.
- Be warm, calm, and encouraging - the person may be worried about an animal.
- Never give a medical diagnosis or suggest any medicine or treatment.
- Do NOT tell users to call a helpline, share their location, or open external links.
- Do NOT mention animal control, local authorities, municipal animal services, SPCA, police, Google Maps, or external websites.
- For India-specific animal help, prefer "local animal rescue organisation", "animal welfare NGO", or "local nonprofit".
- Only answer questions about animal rescue, stray dogs, dog bites, and animal safety.
- If someone asks about something else, kindly explain what you can help with instead.
- If you are not sure how serious a situation is, describe what signs to look for and suggest the person stay nearby and keep watching the dog from a safe distance.

If someone has been bitten by a dog, always tell them to:
1. Wash the bite right away with soap and water for at least 10 minutes
2. See a doctor as soon as possible
3. Watch the wound for any signs of infection"""


def _dar_contact_phrase() -> str:
    if DAR_PHONE_NUMBER:
        return f"contact Dharamsala Animal Rescue on {DAR_PHONE_NUMBER}"
    return "contact Dharamsala Animal Rescue"

PROFESSIONAL_HELP_KEYWORDS = (
    "injur", "bleed", "wound", "fracture", "broken", "limp", "pain",
    "sick", "ill", "infection", "mange", "vomit", "immobile", "collapsed",
    "unable", "distress",
)


def needs_rescue_help(triage_result: dict) -> bool:
    """Return True when the photo looks unhealthy enough to route for help."""
    if triage_result.get("is_fallback"):
        return False

    severity = (triage_result.get("severity") or "").lower()
    score = triage_result.get("severity_score")
    if severity in {"moderate", "high", "critical"}:
        return True
    try:
        if score is not None and int(score) >= 4:
            return True
    except (TypeError, ValueError):
        pass
    return _needs_professional_help(triage_result)

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

    try:
        vision_bytes, normalized_media_type = image_processing.prepare_for_vision(image_bytes, media_type)
    except image_processing.ImageProcessingError as exc:
        logger.warning(
            "Vision triage skipped: image preparation failed for media type %r: %s",
            media_type,
            exc,
        )
        return _fallback_triage(str(exc))

    image_b64 = base64.b64encode(vision_bytes).decode("utf-8")

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
                normalized_media_type, len(vision_bytes), last_error,
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
            f"If the dog appears injured, sick, immobile, or in immediate danger, {_dar_contact_phrase()}.",
            "If the dog is only thin but alert, ask locals or feeders to provide food, water, and monitor it.",
            "If this is outside Dharamsala, contact a local animal rescue organisation, animal welfare NGO, or local nonprofit.",
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
    faq_response = _faq_guidance_response(message)
    if faq_response:
        return faq_response

    if not client:
        return _fallback_chat_response(message)

    # Retrieve relevant knowledge chunks
    rag_context = ""
    try:
        from services import rag
        chunks = rag.retrieve(message)
        rag_context = rag.format_context(chunks)
    except Exception as exc:  # noqa: BLE001 - chat should still fall back cleanly
        logger.warning("RAG retrieval failed for chat query: %s", exc)

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
            input=[{"role": "system", "content": system_prompt}] + messages,
        )
        return (response.output_text or "").strip() or _fallback_chat_response(message)
    except Exception as exc:  # noqa: BLE001 - model outages should degrade gracefully
        logger.warning("Chat model failed, using fallback response: %s", exc)
        return _fallback_chat_response(message)


def _fallback_chat_response(message: str) -> str:
    """Provide a helpful response when the AI model is unavailable."""
    faq_response = _faq_guidance_response(message)
    if faq_response:
        return faq_response

    lower = message.lower()
    if any(w in lower for w in ["bite", "bitten", "bit me", "bit my"]):
        return (
            "**What to do after a dog bite:**\n\n"
            "1. **Wash the wound** with soap and running water for at least 10 minutes — do this right away.\n"
            "2. **Put a clean bandage** over the wound if you have one.\n"
            "3. **Seek medical attention as soon as you can** — a doctor will check if you need any treatment.\n"
            "4. **Do not wait for symptoms** before getting medical advice.\n\n"
            "This is general guidance. Please see a doctor promptly."
        )
    if any(w in lower for w in ["vet near me", "nearby vet", "near me", "google maps", "maps", "clinic nearby"]):
        return (
            "If you are in Dharamsala and the animal is sick, injured, unable to move, or in pain, contact "
            f"{_dar_contact_phrase()}. If you are outside Dharamsala, contact a local animal rescue organisation, "
            "animal welfare NGO, or local nonprofit."
        )
    if any(w in lower for w in ["injured", "hurt", "bleeding", "broken"]):
        return (
            "If you've found an injured animal:\n\n"
            "1. **Ask people nearby first** whether the dog has a feeder or owner.\n"
            "2. **Ask whether local NGOs are already vaccinating or sterilizing dogs nearby** so you do not duplicate an existing effort.\n"
            f"3. **If the dog appears injured, sick, immobile, or unsafe, {_dar_contact_phrase()}.**\n"
            "4. **If locals say the dog is friendly, you can approach slowly and calmly.** Keep more distance only if the dog seems fearful, aggressive, or in severe pain.\n"
            "5. **If this is outside Dharamsala, contact a local animal rescue organisation, animal welfare NGO, or local nonprofit.**\n\n"
            "You can upload a photo of the animal for a condition assessment."
        )
    return (
        "Hello! I'm the Dharamsala Animal Rescue assistant. Examples of questions I can help you with:\n\n"
        "- **Checking a stray dog's condition** from a photo\n"
        "- **Dog bite first aid** guidance\n"
        "- **What to do next** if an animal looks sick or injured\n\n"
        "How can I help you today?"
    )


def _faq_guidance_response(message: str) -> str | None:
    """Deterministic guidance for common rescue and public-safety questions."""
    lower = message.lower()

    def has_any(*terms: str) -> bool:
        return any(term in lower for term in terms)

    if has_any("cow", "milk") and has_any("bitten by a dog", "bit by a dog", "dog bit"):
        return (
            "**About the cow, dog bite, and milk:**\n\n"
            "1. Drinking milk from a cow that was bitten by a dog does not mean you will die.\n"
            "2. The cow still needs an animal husbandry officer or local animal welfare professional, especially if there is a wound.\n"
            "3. If dog saliva touched your broken skin, eyes, mouth, or an open cut, ask a doctor about rabies PEP.\n"
            "4. Do not ignore the cow's wound. A bite can become infected."
        )

    if has_any("do i have rabies", "will i get rabies"):
        return (
            "**Rabies concern:**\n\n"
            "I cannot tell if you have rabies from a chat. If a dog bit or scratched you, wash the wound for at least 10 minutes and see a doctor the same day. Rabies is preventable with timely PEP, but it is dangerous once symptoms start."
        )

    if has_any("bit me", "bitten me", "dog bite", "dog just bit", "dog has bitten", "bitten someone", "after a dog bite", "bite cause rabies"):
        return (
            "**Dog bite safety:**\n\n"
            "1. Wash the bite with soap and running water for at least 10 minutes right now.\n"
            "2. Seek medical attention the same day. A doctor can decide if you need rabies PEP or other care.\n"
            "3. Cover the wound with a clean cloth or bandage.\n"
            "4. A dog can look normal and a bite can still be risky, so do not wait for symptoms."
        )

    if has_any("signs of rabies", "rabies in dogs"):
        return (
            "**Possible rabies signs in dogs:**\n\n"
            "- Sudden behaviour change, such as friendly to aggressive or unusually quiet.\n"
            "- Excessive drooling or foaming at the mouth.\n"
            "- Trouble swallowing, staggering, disorientation, or weakness.\n"
            "- Unprovoked aggression or later-stage paralysis.\n\n"
            "Do not approach a dog showing these signs. Keep people away and contact a local animal rescue organisation, animal welfare NGO, or local nonprofit."
        )

    if has_any("chasing me", "growling", "aggressive", "following me"):
        return (
            "**Stay calm and create space:**\n\n"
            "1. Do not run, scream, kick, or stare directly at the dog.\n"
            "2. Stop or slow down, turn slightly sideways, and keep your hands close to your body.\n"
            "3. Back away slowly toward people, a doorway, or a safe place.\n"
            "4. Put a bag, bicycle, or other object between you and the dog if needed.\n"
            "5. If you are bitten, wash the wound with soap and water for at least 10 minutes and see a doctor the same day."
        )

    if has_any("give up my dog", "moving cities", "leave my dog", "don't have time", "don’t have time", "pet anymore", "adopt my dog", "new home", "cannot afford treatment", "can you take it"):
        return (
            "**If you cannot keep your pet:**\n\n"
            "Please do not abandon the dog. First ask family, friends, neighbours, and trusted adopters; share clear photos, vaccination/sterilisation details, and temperament. DAR may guide or share adoption options, but it may not be able to take healthy owned pets. If your dog is sick and cost is the issue, ask a local animal rescue organisation or animal welfare NGO about low-cost treatment options before surrendering."
        )

    if has_any("can i touch", "touch it"):
        return (
            "**Do not touch with bare hands:**\n\n"
            "If a dog is bleeding or injured, it may bite from pain or fear. Keep people away. If you are trained and the dog is calm, use gloves or a clean cloth; otherwise wait for a trained rescuer."
        )

    if has_any("sick dog", "very sick", "bleeding", "maggots", "wound", "not moving", "still breathing", "vomiting continuously", "ticks all over", "pregnant", "about to give birth"):
        return (
            "**This dog may need urgent help:**\n\n"
            "1. Keep a safe distance, especially if the dog is in pain, scared, bleeding, or not moving.\n"
            "2. Note the exact location, landmark, colour/size of the dog, and what you can see.\n"
            "3. Ask nearby people if the dog has a feeder or owner and whether an NGO is already helping there.\n"
            "4. Upload a photo if you can. A bleeding wound, maggots, collapse, repeated vomiting, or heavy tick infestation needs urgent rescue assessment.\n"
            "5. Do not give human medicine or pour chemicals on wounds/ticks. Move the dog only if it is in immediate danger and you can do it safely."
        )

    if has_any("human medicines", "human medicine", "give medicine", "paracetamol", "ibuprofen"):
        return (
            "**Do not give human medicines to a dog.**\n\n"
            "Many human medicines can poison dogs or hide serious symptoms. Contact a local animal rescue organisation, animal welfare NGO, or trained rescuer with the dog's weight, symptoms, and location."
        )

    if has_any("stuck", "drain", "building"):
        return (
            "**Dog stuck somewhere:**\n\n"
            "1. Do not climb into unsafe drains, construction sites, or buildings yourself.\n"
            "2. Share the exact location, photos, and what the dog is stuck in.\n"
            "3. Contact a local animal rescue organisation, animal welfare NGO, or local nonprofit for help.\n"
            "4. Keep people from crowding the dog while help is arranged."
        )

    if has_any("mother dog", "puppies", "abandoned puppies", "take puppies", "feed them"):
        return (
            "**Mother dog and puppies:**\n\n"
            "1. Do not move puppies if the mother is nearby and they are safe. Separating them can harm them.\n"
            "2. Give the mother food and clean water from a little distance if she is calm.\n"
            "3. If puppies are truly abandoned, cold, injured, or crying for a long time, they need urgent warmth and rescue guidance.\n"
            "4. Do not feed very young puppies biscuits or regular cow's milk. Ask a trained rescuer or animal welfare NGO about puppy milk replacer and feeding frequency."
        )

    if has_any("healthy dogs", "remove street dogs", "relocate", "shift them", "take them away", "move all street dogs", "pick up all street dogs", "removed permanently", "to shelters"):
        return (
            "**About removing healthy street dogs:**\n\n"
            "DAR generally cannot pick up or permanently relocate healthy street dogs. The safer long-term approach is vaccination, sterilisation, and returning dogs to their own area. Removing dogs often creates a vacant territory where unvaccinated dogs can move in.\n\n"
            "For dogs that are injured, sick, suspected rabid, or repeatedly aggressive, contact a local animal rescue organisation, animal welfare NGO, or local nonprofit for guidance."
        )

    if has_any("bark", "nuisance", "scared of dogs", "kids are scared", "pooping", "feeding dogs", "neighbors feed", "rules for feeding", "stop them"):
        return (
            "**Community dog conflict:**\n\n"
            "1. Do not harm or chase the dogs. That usually makes conflict worse.\n"
            "2. Work with feeders/residents on fixed feeding spots away from gates, schools, and busy paths.\n"
            "3. Keep feeding areas clean and remove leftover food.\n"
            "4. Prioritise sterilisation and rabies vaccination for the local dogs.\n"
            "5. If a specific dog bites someone, the person should wash the wound and see a doctor the same day."
        )

    if has_any("how long", "how soon", "come immediately", "urgent", "delay", "hasn't anyone arrived", "already called"):
        return (
            "**About rescue timing:**\n\n"
            "Rescue time depends on exact location, traffic, current rescue load, and how severe the case is. Critical cases are prioritised, but a team may still be delayed.\n\n"
            "Please stay nearby only if it is safe, keep your phone reachable, and send a clear landmark/photo. If the animal moves or gets worse, update the rescue group helping you."
        )

    if has_any("i am in", "near me", "vets near", "who can help", "location"):
        return (
            "**Finding nearby help:**\n\n"
            "If you are in Dharamsala and the animal is sick, injured, unable to move, or in pain, contact Dharamsala Animal Rescue. If you are outside Dharamsala, contact a local animal rescue organisation, animal welfare NGO, or local nonprofit."
        )

    if has_any("animal cruelty", "someone is harming", "harming dogs", "abuse"):
        return (
            "**Animal cruelty safety:**\n\n"
            "Do not put yourself in danger or confront a violent person alone. If it is safe, note the location, time, photos/videos, vehicle numbers if relevant, and witness details. Contact a local animal welfare NGO or animal rescue organisation for guidance."
        )

    if has_any("what is dar doing", "dogs are still on streets", "why don't you pick"):
        return (
            "**What DAR focuses on:**\n\n"
            "DAR helps through rescue of sick and injured animals, rabies vaccination, sterilisation, adoption for dogs that cannot safely return, and community education. Healthy street dogs may still remain in their own areas because vaccination and sterilisation are more effective than mass removal."
        )

    return None


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
                f"Because the dog appears injured, sick, immobile, or at immediate risk, {_dar_contact_phrase()} as soon as possible.",
                "If locals say the dog is friendly, one calm person can stay nearby or help arrange transport while avoiding sudden handling.",
            ]
        )
    else:
        actions.extend(
            [
                "If the dog is only thin but alert and mobile, ask locals or feeders to provide food, water, and monitor it rather than assuming an NGO pickup will happen.",
                "If this is outside Dharamsala, contact a local animal rescue organisation, animal welfare NGO, or local nonprofit if the dog gets worse.",
            ]
        )

    if severity in {"high", "critical"} and not needs_professional_help:
        actions.append("If the dog worsens, becomes immobile, or looks obviously sick or injured, escalate to a local animal rescue organisation or animal welfare NGO quickly.")
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
    return text
