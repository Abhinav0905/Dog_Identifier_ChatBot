"""
Guardrails service: domain relevance filtering, harmful input detection,
medical-certainty limitation, and prompt-injection protection.
"""

import re

# Off-topic keywords that clearly indicate non-rescue queries
OFF_TOPIC_PATTERNS = [
    r"\b(stock|crypto|bitcoin|invest|trading)\b",
    r"\b(recipe|cook|baking|ingredient)\b",
    r"\b(homework|essay|exam|assignment)\b",
    r"\b(dating|relationship|romance)\b",
    r"\b(hack|exploit|crack|bypass security)\b",
    r"\b(write code|debug|programming|javascript|python script)\b",
]

# Patterns suggesting prompt injection attempts
INJECTION_PATTERNS = [
    r"ignore (all |your |previous )?instructions",
    r"you are now",
    r"new system prompt",
    r"disregard (all |your )?previous",
    r"override (your |the )?rules",
    r"pretend you are",
    r"act as (a |an )?(?!volunteer|rescue)",
    r"reveal (your |the )?system prompt",
]

# Harmful/abusive content patterns
HARMFUL_PATTERNS = [
    r"\b(kill|torture|abuse|hurt|poison)\s+(a |an |the )?(dog|cat|animal|puppy|kitten)\b",
    r"\b(how to|ways to)\s+(harm|injure|kill|abuse)\b",
]

# Rescue-relevant keywords
RESCUE_KEYWORDS = [
    "dog", "puppy", "stray", "animal", "rescue", "bite", "bitten", "injured",
    "hurt", "sick", "bleeding", "limping", "lost", "found", "abandoned",
    "help", "save", "vet", "veterinary", "shelter", "adopt", "vaccination",
    "rabies", "wound", "dharamsala", "dharmasala", "incident", "report",
    "location", "volunteer", "emergency", "distress",
]


class GuardrailResult:
    def __init__(self, allowed: bool, reason: str = "", category: str = "ok"):
        self.allowed = allowed
        self.reason = reason
        self.category = category


def check_input(text: str) -> GuardrailResult:
    """Run all guardrail checks on user input. Returns GuardrailResult."""
    lower = text.lower().strip()

    if not lower or len(lower) < 2:
        return GuardrailResult(False, "Input is too short to process.", "empty")

    # Prompt injection check
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lower):
            return GuardrailResult(
                False,
                "I'm designed to help with animal rescue queries only. I can't change my operating guidelines.",
                "injection",
            )

    # Harmful content check
    for pattern in HARMFUL_PATTERNS:
        if re.search(pattern, lower):
            return GuardrailResult(
                False,
                "I can't assist with that request. If you're witnessing animal abuse, contact a local animal welfare NGO or animal rescue organisation for guidance.",
                "harmful",
            )

    # Off-topic check (only if no rescue keywords present)
    has_rescue_keyword = any(kw in lower for kw in RESCUE_KEYWORDS)
    if not has_rescue_keyword:
        for pattern in OFF_TOPIC_PATTERNS:
            if re.search(pattern, lower):
                return GuardrailResult(
                    False,
                    "I'm the Dharamsala Animal Rescue assistant. I can help with stray animal distress, dog bite guidance, and rescue-related questions. How can I help with an animal rescue concern?",
                    "off_topic",
                )

    return GuardrailResult(True)


def sanitize_response(response: str) -> str:
    """Post-process AI response to enforce safety constraints."""
    replacements = [
        (r"\banimal control\b", "local animal rescue organisation"),
        (r"\blocal authorities\b", "local animal welfare NGO"),
        (r"\bauthorities\b", "local animal welfare NGO"),
        (r"\bmunicipal animal services?\b", "local animal rescue organisation or animal welfare NGO"),
        (r"\bmunicipal animal control services?\b", "local animal rescue organisation or animal welfare NGO"),
        (r"\bSPCA\b", "local animal welfare NGO"),
        (r"\bGoogle Maps links?\b", "nearby help"),
        (r"\bGoogle Maps\b", "nearby help"),
    ]
    for pattern, replacement in replacements:
        response = re.sub(pattern, replacement, response, flags=re.IGNORECASE)

    # Ensure no diagnostic language slips through
    diagnostic_phrases = [
        "I diagnose", "the diagnosis is", "this dog has", "it is definitely",
        "you should administer", "give the dog medication", "prescribe",
    ]
    for phrase in diagnostic_phrases:
        if phrase.lower() in response.lower():
            response += "\n\n**Note:** This is not a veterinary diagnosis. Please contact a local animal rescue organisation or animal welfare NGO for guidance if you are worried."
            break
    return response
