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
    r"new system prompt",
    r"disregard (all |your )?previous",
    r"override (your |the )?rules",
    r"reveal (your |the )?system prompt",
]

# Harmful/abusive content patterns
HARMFUL_PATTERNS = [
    r"\b(?:how\s+(?:to|(?:can|do|should)\s+i)|ways?\s+to|help\s+me|"
    r"teach\s+me\s+to|i\s+(?:want|plan)\s+to)\s+"
    r"(?:deliberately\s+)?(?:kill|torture|abuse|hurt|poison|harm|injure)\b",
]
_PROTECTIVE_INTENT_RE = re.compile(
    r"\b(?:prevent|stop|report|witness(?:ed|ing)?|accidentally|without|"
    r"do\s+not|don['’]t|never|avoid)\b.{0,100}\b(?:kill|torture|abuse|"
    r"hurt|poison|harm|injur)\w*\b", re.IGNORECASE,
)

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
        if re.search(pattern, lower) and not _PROTECTIVE_INTENT_RE.search(lower):
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
    """Use one output policy across photos and text; preserve institution names."""
    return sanitize_text_response(response)


# Check affirmative actions, not the mere presence of words in a warning. These
# checks are a backstop for generated text; protected encounter guidance lives
# in triage. A warning about a tactic raised by the user must remain visible;
# unsolicited tactic prohibitions are checked separately with user context.
_UNSAFE_DETERRENT_RE = re.compile(
    r"\b(?:throw|throwing|toss|tossing|hurl|hurling)\b.{0,100}"
    r"\b(?:stones?|rocks?|sticks?|objects?)\b"
    r"|\b(?:swing|swinging|wave|waving)\b.{0,60}\b(?:sticks?|rocks?)\b"
    r"|\b(?:use|sound|blow)\b.{0,35}\b(?:horn|whistle)\b"
    r"|\b(?:honk(?:ing)?|beep(?:ing)?|whistl(?:e|ing)|rev(?:ving)?)\b"
    r"|\b(?:kick|hit|strike)\s+(?:at\s+)?(?:a\s+|the\s+|that\s+)?(?:dog|it|them)\b",
    re.IGNORECASE,
)
_NEGATED_ACTION_RE = re.compile(
    r"\b(?:do\s+not|don['’]t|never|avoid|refrain\s+from|without|"
    r"should\s+not|shouldn['’]t|must\s+not|mustn['’]t|no)\b",
    re.IGNORECASE,
)
_DETERRENT_CORRECTION = (
    "Give the dog space, stay calm and avoid startling or confronting it. If you are "
    "riding, road safety comes first: do not brake suddenly or swerve into traffic."
)


def _unsafe_behavior_spans(text: str) -> list[tuple[int, int]]:
    """Return unsafe clauses without deleting safe warnings or contact lines."""
    spans = []
    clauses = re.finditer(
        r"[^.!?;\n]+(?:[.!?;]|$)", str(text or "")
    )
    for clause in clauses:
        # A contrasting or new imperative clause ends a preceding prohibition.
        parts = re.split(
            r"\b(?:but|however)\b|,\s*(?=(?:use|try|you\s+can|you\s+should|"
            r"throw|toss|a\s+short|a\s+quick)\b)",
            clause.group(),
            flags=re.IGNORECASE,
        )
        for part in parts:
            for action in _UNSAFE_DETERRENT_RE.finditer(part):
                prefix = part[:action.start()]
                # Also accept "throwing stones is unsafe/not recommended".
                suffix = part[action.end():]
                prohibited = _NEGATED_ACTION_RE.search(prefix) or re.match(
                    r"\s+(?:is|are|can\s+be)\s+(?:unsafe|dangerous|harmful|"
                    r"not\s+(?:safe|recommended|helpful))\b",
                    suffix,
                    re.IGNORECASE,
                )
                if not prohibited:
                    spans.append(clause.span())
                    break
            else:
                continue
            break
    return spans


def has_unsafe_behavior_advice(response: str) -> bool:
    """Identify affirmative aversive deterrence advice in generated care text."""
    return bool(_unsafe_behavior_spans(response))


_AVERSIVE_MENTION_RE = re.compile(
    r"\bkick(?:ing)?\b|\b(?:hit|strike|beat)\b.{0,20}\b(?:dogs?|it|them)\b|"
    r"\b(?:throw\w*|toss\w*|hurl\w*|swing\w*|wav(?:e|ing))\b.{0,65}\b(?:stones?|rocks?|sticks?|objects?)\b|"
    r"\b(?:horns?|whistles?|honk\w*|rev(?:ving)?|beep\w*)\b|"
    r"\b(?:shout|yell|scream)\w*\s+at\s+(?:the\s+)?(?:dogs?|it|them)\b|"
    r"लात|पत्थर|डंडा|डंडे|हॉर्न|सीटी", re.I,
)


def has_unsolicited_aversive_mentions(response: str, user_context: str) -> bool:
    """User-raised unsafe tactics may be discouraged; do not invent them first.

    Callers must supply user-established context, not prior assistant prose or
    model rewrites, so an old unwanted tactic cannot authorize its repetition.
    """
    return bool(_AVERSIVE_MENTION_RE.search(response) and not _AVERSIVE_MENTION_RE.search(user_context))


def sanitize_text_response(response: str) -> str:
    """Preserve sourced names while removing clearly unsafe generated clauses.

    This narrow backstop supplements the shared model policy; it is not a
    medical classifier and must not rename public services or delete a whole
    paragraph merely because it contains a generic referral.
    """
    text = str(response or "")
    spans = _unsafe_behavior_spans(text)
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if spans:
        text = f"{text}\n\n{_DETERRENT_CORRECTION}".strip()
    medical_spans = _unsafe_medical_spans(text)
    for start, end in reversed(medical_spans):
        text = text[:start] + text[end:]
    if medical_spans:
        hindi = bool(re.search(r"[\u0900-\u097f]", str(response or "")))
        correction = (
            "चैट से बीमारी का निदान या दवा की खुराक तय नहीं की जा सकती। पशु चिकित्सक से जल्द सलाह लें; अपने आप मानव दवा न दें।"
            if hindi else
            "A chat cannot establish a diagnosis or prescribe a dose. Ask a veterinarian for an assessment; do not give human medicine on your own."
        )
        text = f"{text.strip()}\n\n{correction}".strip()
    return clean_response_layout(text)


def clean_response_layout(text: str) -> str:
    """Remove empty list markers left by clause filtering and renumber items."""
    lines = []
    active = False
    number = 0
    for line in str(text or "").splitlines():
        if re.fullmatch(r"\s*(?:\d+[.)]|[-*])\s*(?:\*\*)?\s*", line):
            continue
        item = re.match(r"^(\d+)([.)])(\s+\S.*)$", line)
        if item:
            number = number + 1 if active else 1
            active = True
            lines.append(f"{number}{item.group(2)}{item.group(3)}")
        else:
            if line.strip() and not line.startswith((" ", "\t", "-", "*")):
                active = False
                number = 0
            lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_MEDICINE_ACTION_RE = re.compile(
    r"\b(?:give|administer|dose|prescribe|treat\s+\w+\s+with)\b.{0,100}"
    r"\b(?:ibuprofen|paracetamol|acetaminophen|aspirin|antibiotics?|steroids?|"
    r"medication|medicine|\d+(?:\.\d+)?\s*(?:mg|ml))\b", re.IGNORECASE,
)
_CERTAIN_DIAGNOSIS_RE = re.compile(
    r"\b(?:i\s+diagnose|the\s+diagnosis\s+is|(?:dog|puppy|cat|animal)\s+"
    r"(?:definitely\s+|certainly\s+)?has\s+(?:rabies|parvo(?:virus)?|distemper|"
    r"cancer|kidney\s+failure))\b", re.IGNORECASE,
)


def _unsafe_medical_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for clause in re.finditer(r"[^.!?;\n]+(?:[.!?;]|$)", text):
        value = clause.group()
        action = _MEDICINE_ACTION_RE.search(value) or _CERTAIN_DIAGNOSIS_RE.search(value)
        professional_instruction = action and re.search(
            r"\b(?:veterinarian|veterinary\s+professional|vet|doctor)\b.{0,35}$",
            value[:action.start()], re.I,
        )
        if action and not professional_instruction and not _NEGATED_ACTION_RE.search(value[:action.start()]):
            spans.append(clause.span())
    return spans
