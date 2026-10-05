"""Scoped web search for current dog and animal-rescue information in India."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import ipaddress
import json
import logging
import math
import queue
import re
import socket
import threading
import time
from typing import Any, Iterable
import unicodedata
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup
from openai import OpenAI
import urllib3

import config
import database as db
from services import location, query_router, region_scope, web_operations
from services.prompts import PromptCatalog

logger = logging.getLogger(__name__)

client = (
    OpenAI(
        api_key=config.OPENAI_API_KEY,
        timeout=config.DOG_WEB_SEARCH_OPENAI_TIMEOUT_SECONDS,
        max_retries=0,
    )
    if config.OPENAI_API_KEY
    else None
)

ANIMAL_SCOPE_PATTERNS = (
    r"\b(dog|dogs|puppy|puppies|canine)\b",
    r"\b(street|stray|community)\s+(dog|dogs|animal|animals)\b",
    r"\b(animal rescue|animal welfare|animal charity|animal nonprofit)\b",
    r"\b(rabies|dog bite|dog bites|bitten by a dog)\b",
)

ORGANISATION_PATTERNS = (
    r"\bngo(?:s)?\b",
    r"\b(rescue|shelter|charity|nonprofit|organisation|organization)\b",
)

SEARCH_INTENT_PATTERNS = (
    r"\b(find|search|look up|lookup|list|near me|nearby)\b",
    r"\b(contact|phone|number|address|website|link|email|hours)\b",
    r"\b(current|currently|latest|today|recent|updated)\b",
    r"\b(where|which|who)\b.*\b(ngo|rescue|shelter|charity|nonprofit)\b",
    r"\bwho\s+can\s+(?:help|i\s+(?:contact|call))\b",
    r"\bwhere\s+can\s+i\s+(?:get|find)\s+help\b",
)

LOCAL_HELP_INTENT_RE = re.compile(
    r"\b(?:ngo|ngos|rescue|rescues|shelter|shelters|charity|nonprofit|organisation|"
    r"organization|contact|phone|number|who can help|who can i call|where can i get help)\b",
    re.IGNORECASE,
)

DIRECT_ANIMAL_RESCUE_EVIDENCE_RE = re.compile(
    r"(?:\b(?:rescue(?:s|d|ing)?|rehabilitat(?:e|es|ed|ing)|treat(?:s|ed|ing)?|"
    r"respond(?:s|ed|ing)?|accept(?:s|ed|ing)?|admit(?:s|ted|ting)?|"
    r"take(?:s|n)?\s+in)\b[^.;]{0,120}"
    r"\b(?:dog|dogs|canine|puppy|puppies|animals?|strays?)\b)|"
    r"(?:\b(?:dog|dogs|canine|puppy|puppies|animals?|strays?)\b[^.;]{0,120}"
    r"\b(?:rescued|treated|admitted|rehabilitated|taken\s+in)\b)|"
    r"(?:\b(?:animal|dog|canine)\s+rescue\s+"
    r"(?:service|services|helpline|centre|center|shelter|clinic|team|operations?)\b)",
    re.IGNORECASE,
)

INDIRECT_RESCUE_CONTENT_RE = re.compile(
    r"\b(?:publish(?:es|ed|ing)?|articles?|stories|blog|news|information|awareness|"
    r"refer(?:s|red|ring|ral)?|directory|fund(?:s|ed|ing)?|grants?|donations?|"
    r"sponsor(?:s|ed|ing)?|partner(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)

NEGATED_RESCUE_EVIDENCE_RE = re.compile(
    r"(?:\b(?:no|not|never|without|cannot|can't|could\s+not|couldn't|does\s+not|"
    r"doesn't|did\s+not|didn't|unable\s+to|unverified|unconfirmed)\b[^.]{0,100}"
    r"\b(?:dog|animal|stray|rescue|shelter|intake|field\s+work)\b)|"
    r"(?:\b(?:dog|animal|stray|rescue|shelter|intake|field\s+work)\b[^.]{0,100}"
    r"\b(?:not\s+verified|not\s+confirmed|unverified|unconfirmed|no\s+evidence)\b)",
    re.IGNORECASE,
)

INACTIVE_OR_INDIRECT_RESCUE_EVIDENCE_RE = re.compile(
    r"\b(?:previously|formerly|used\s+to|permanently\s+closed|no\s+longer|"
    r"hope(?:s|d)?\s+to|plan(?:s|ned)?\s+to|aim(?:s|ed)?\s+to|someday|"
    r"in\s+the\s+future|raise(?:s|d|ing)?\s+(?:money|funds)|fundrais(?:e|es|ed|ing)|"
    r"donat(?:e|es|ed|ing)\s+to|partner\s+that)\b",
    re.IGNORECASE,
)

NEGATED_SERVICE_AREA_RE = re.compile(
    r"(?:\b(?:do(?:es)?\s+not|doesn't|don't|not|never|no\s+longer|without|"
    r"except|excluding|outside)\b[^.;]{0,80}\b(?:serve|serving|cover|covering|"
    r"operate|operating|work|working|rescue|rescuing|treat|treating|in|within)\b)|"
    r"(?:\b(?:serve|serving|cover|covering|operate|operating|work|working)\b"
    r"[^.;]{0,80}\b(?:except|excluding|not\s+including|outside)\b)",
    re.IGNORECASE,
)

AFFIRMATIVE_SERVICE_AREA_RE = re.compile(
    r"(?:\b(?:serve(?:s|d|ing)?|cover(?:s|ed|ing)?|treat(?:s|ed|ing|ment)?|"
    r"respond(?:s|ed|ing)?|conduct(?:s|ed|ing)?|run(?:s|ning)?|"
    r"locat(?:e|ed|ing)|based|"
    r"accept(?:s|ed|ing)?)\b)|"
    r"(?:\b(?:we|they|it|organisation|organization|team|centre|center|clinic|"
    r"shelter)\s+(?:also\s+|directly\s+)?rescue(?:s)?\b)|"
    r"(?:\b(?:animal|dog)\s+rescue\s+(?:work|services?|operations?)\b)",
    re.IGNORECASE,
)

SERVICE_SITE_RE = re.compile(
    r"\b(?:(?:animal|dog)\s+rescue\s+)?(?:centre|center|clinic|shelter|hospital)\b",
    re.IGNORECASE,
)

ANIMAL_SERVICE_CONTEXT_RE = re.compile(
    r"\b(?:dog|dogs|canine|puppy|puppies|animals?|strays?|rescue|shelter|"
    r"animal\s+welfare|injured|sick|rabies|vaccinat(?:e|es|ed|ing|ion)|"
    r"sterilis(?:e|es|ed|ing|ation)|steriliz(?:e|es|ed|ing|ation))\b",
    re.IGNORECASE,
)

INDIRECT_SERVICE_AREA_RE = re.compile(
    r"\b(?:fundrais(?:e|es|ed|ing)|donors?|donations?|registered\s+office|office|"
    r"volunteer\s+recruitment|recruit(?:s|ed|ing|ment)|grants?|sponsor(?:s|ed|ing)?|"
    r"referrals?|directory|elsewhere|partner(?:s|ed|ing)?|awareness|workshops?|"
    r"seminars?|campaigns?)\b",
    re.IGNORECASE,
)

SERVICE_SCOPE_ONLY_RE = re.compile(
    r"\b(?:serve(?:s|d|ing)?|cover(?:s|ed|ing)?|operate(?:s|d|ing)?|"
    r"work(?:s|ed|ing)?)\b[^.;]{0,100}\bonly\b",
    re.IGNORECASE,
)

# Official sites use several current and former spellings for the same Indian
# state/UT. Canonicalising them prevents both false conflicts and accidental
# acceptance of a genuinely different state in a multi-branch sentence.
INDIA_REGION_ALIASES = {
    "national capital territory of delhi": "delhi",
    "nct of delhi": "delhi",
    "delhi ncr": "delhi",
    "orissa": "odisha",
    "pondicherry": "puducherry",
    "uttaranchal": "uttarakhand",
}

DELHI_SERVICE_CITY_ALIASES = (
    "New Delhi",
    "Delhi",
    "Delhi NCR",
    "NCT of Delhi",
    "National Capital Territory of Delhi",
)

# The city's official sources commonly use several English spellings. Treat
# them as the same verified locality when corroborating an organisation's own
# service-area text; this is spelling normalisation, not a broader-area match.
DHARAMSHALA_SERVICE_CITY_ALIASES = (
    "Dharamshala",
    "Dharamsala",
    "Dharmasala",
    "Dharmsala",
    "Dharmshala",
)

NON_GOVERNMENTAL_EVIDENCE_RE = re.compile(
    r"\b(?:non[\s-]?profit|not[\s-]?for[\s-]?profit|non[\s-]?governmental|ngo|"
    r"charit(?:y|able)|registered\s+(?:public\s+)?trust|public\s+trust|"
    r"voluntary\s+organisation|voluntary\s+organization)\b",
    re.IGNORECASE,
)

STRONG_NON_GOVERNMENTAL_EVIDENCE_RE = re.compile(
    r"\b(?:non[\s-]?profit|not[\s-]?for[\s-]?profit|non[\s-]?governmental|"
    r"charit(?:y|able)|registered\s+(?:public\s+)?trust|public\s+trust|"
    r"voluntary\s+organisation|voluntary\s+organization)\b",
    re.IGNORECASE,
)

NEGATED_OR_COMMERCIAL_TYPE_RE = re.compile(
    r"(?:\b(?:not|never|no\s+longer|isn't|is\s+not|not\s+registered\s+as)\b"
    r"[^.;]{0,80}\b(?:non[\s-]?profit|ngo|charit(?:y|able)|trust)\b)|"
    r"(?:\b(?:commercial|for[\s-]?profit|private\s+(?:veterinary\s+)?"
    r"(?:clinic|practice|hospital))\b)",
    re.IGNORECASE,
)

BANNED_ORGANISATION_RE = re.compile(
    r"\b(?:government|govt|municipal|municipality|corporation|police|board|commission|"
    r"authority|department|directorate|ministry|forest|wildlife\s+division|"
    r"wildlife\s+department|range\s+office|warden|animal\s+husbandry|rotary\s+club|"
    r"animal\s+control|spca)\b",
    re.IGNORECASE,
)

NGO_CACHE_VERSION = "v21"
REGIONAL_NGO_CACHE_HOURS = 1
PAGE_FETCH_BUDGET_SENTINEL = "__page_fetch_budget_exhausted__"
PAGE_FETCH_SEMAPHORE = threading.BoundedSemaphore(4)
DNS_RESOLUTION_SEMAPHORE = threading.BoundedSemaphore(4)

# These words may be appended to an organization's public-facing name as a
# legal form. They are ignored only when the remaining name is distinctive;
# generic names such as "Animal Rescue Trust" must not become loose matches.
ORGANIZATION_LEGAL_SUFFIXES = frozenset(
    {
        "association",
        "foundation",
        "organisation",
        "organization",
        "society",
        "trust",
    }
)
GENERIC_ORGANIZATION_NAME_TOKENS = frozenset(
    {
        "animal",
        "animals",
        "care",
        "charity",
        "community",
        "dog",
        "dogs",
        "india",
        "rescue",
        "shelter",
        "stray",
        "strays",
        "welfare",
    }
)

# These are discovery seeds for the two regression cities central to Ask
# Dorjee. They contain no trusted contact data and never bypass verification:
# every request still re-fetches the official site and proves identity, current
# animal work, service geography, and non-governmental status before rendering.
CORE_CITY_OFFICIAL_DISCOVERY_SEEDS = {
    ("manali", "himachal pradesh"): (
        {
            "name": "Manali Strays",
            "possible_official_url": "https://manalistrays.org/veterinary-care/",
        },
    ),
    ("dharamshala", "himachal pradesh"): (
        {
            "name": "Dharamsala Animal Rescue",
            "possible_official_url": "https://dharamsalaanimalrescue.org/",
        },
        {
            "name": "Tibet Charity",
            "possible_official_url": "https://tibetcharity.in/animal-care/",
        },
    ),
}

NGO_DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "possible_official_url": {"type": "string"},
                },
                "required": ["name", "possible_official_url"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

NGO_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "organizations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "service_area": {"type": "string"},
                    "service_city": {"type": "string"},
                    "service_region": {"type": "string"},
                    "animal_rescue_evidence": {"type": "string"},
                    "service_area_evidence": {"type": "string"},
                    "organization_type_evidence": {"type": "string"},
                    "animal_rescue_evidence_url": {"type": "string"},
                    "service_area_evidence_url": {"type": "string"},
                    "organization_type_evidence_url": {"type": "string"},
                    "official_url": {"type": "string"},
                    "phone": {"type": "string"},
                    "phone_source_url": {"type": "string"},
                    "address": {"type": "string"},
                    "address_source_url": {"type": "string"},
                    "opening_hours": {"type": "string"},
                    "opening_hours_source_url": {"type": "string"},
                    "rescue_work_verified": {"type": "boolean"},
                    "service_area_verified": {"type": "boolean"},
                    "non_governmental": {"type": "boolean"},
                },
                "required": [
                    "name",
                    "service_area",
                    "service_city",
                    "service_region",
                    "animal_rescue_evidence",
                    "service_area_evidence",
                    "organization_type_evidence",
                    "animal_rescue_evidence_url",
                    "service_area_evidence_url",
                    "organization_type_evidence_url",
                    "official_url",
                    "phone",
                    "phone_source_url",
                    "address",
                    "address_source_url",
                    "opening_hours",
                    "opening_hours_source_url",
                    "rescue_work_verified",
                    "service_area_verified",
                    "non_governmental",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "organizations",
    ],
    "additionalProperties": False,
}


@dataclass
class SearchResult:
    response: str
    resource_links: list[dict[str, str]] = field(default_factory=list)
    searched: bool = False
    cached: bool = False
    result_kind: str = ""
    organizations: list[dict[str, str]] = field(default_factory=list)
    # None is retained for callers/tests that construct a result directly.
    # Exact-city production searches set this explicitly so an interrupted
    # candidate-discovery pass cannot create a reusable partial snapshot.
    candidate_discovery_complete: bool | None = None
    # Internal research evidence, never automatically rendered as supporting links.
    research_sources: list[str] = field(default_factory=list)
    # The evidence review assesses whether the original request was answered.
    # None means no adequacy review ran; it never implies success.
    request_satisfied: bool | None = None
    validated_facts: list[dict[str, str]] = field(default_factory=list)


class PublicPageText(str):
    """Visible page text carrying separately extracted official identity markers."""

    identity_text: str
    identity_blocks: tuple[str, ...]
    blocks: tuple[str, ...]
    source_hostname: str
    is_html: bool
    links: tuple[str, ...]

    def __new__(
        cls,
        value: str,
        *,
        identity_text: str = "",
        identity_blocks: Iterable[str] = (),
        blocks: Iterable[str] = (),
        source_hostname: str = "",
        is_html: bool = False,
        links: Iterable[str] = (),
    ) -> PublicPageText:
        instance = super().__new__(cls, value)
        instance.identity_text = identity_text
        instance.identity_blocks = tuple(identity_blocks)
        instance.blocks = tuple(blocks)
        instance.source_hostname = source_hostname
        instance.is_html = is_html
        instance.links = tuple(links)
        return instance


def should_search(message: str, history: Iterable[dict] = ()) -> bool:
    """Return True only for dog/animal-rescue queries that need current web data."""
    current = (message or "").strip().lower()
    if not current:
        return False

    history_text = " ".join(
        str(entry.get("content") or "")
        for entry in list(history)[-6:]
        if isinstance(entry, dict)
    ).lower()
    conversation = f"{history_text} {current}".strip()

    animal_scope = any(re.search(pattern, conversation) for pattern in ANIMAL_SCOPE_PATTERNS)
    organisation_scope = any(re.search(pattern, current) for pattern in ORGANISATION_PATTERNS)
    if not animal_scope:
        return False

    return organisation_scope or any(
        re.search(pattern, current) for pattern in SEARCH_INTENT_PATTERNS
    )


def search_text_query(
    message: str, history: Iterable[dict] = (), *, lat: float | None = None,
    lng: float | None = None, resolved_place: str = "", resolved_country_code: str = "",
    language: str = "en",
) -> SearchResult:
    """All text contact requests use the same need-led research policy."""
    return search_animal_question(message, history, lat=lat, lng=lng,
        resolved_place=resolved_place, resolved_country_code=resolved_country_code,
        language=language)


def search_animal_question(
    message: str,
    history: Iterable[dict] = (),
    *,
    contextual_request: str = "",
    resolved_place: str = "",
    resolved_country_code: str = "",
    lat: float | None = None,
    lng: float | None = None,
    language: str = "en",
    tool_choice: str = "required",
    requested_institution: str | None = None,
    requested_location_text: str = "",
    phone_only: bool | None = None,
    deadline: float | None = None,
) -> SearchResult:
    """Let the model research the actual question without the NGO directory pipeline."""
    if tool_choice not in {"required", "auto"}:
        raise ValueError("Animal-question search supports required or auto tool choice")
    history = list(history)
    deadline = min(deadline or float("inf"), time.monotonic() + config.DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS)
    context = {
        "contextual_request": str(contextual_request or "")[:4000],
        "confirmed_case_location": str(resolved_place or "")[:500],
        "confirmed_country_code": str(resolved_country_code or "")[:10],
        "requested_institution": str(requested_institution or "")[:300],
        "requested_location_text": str(requested_location_text or "")[:500],
        "phone_only": phone_only,
    }
    if lat is not None and lng is not None and _coordinates_are_valid(lat, lng):
        context["shared_coordinates"] = {"latitude": float(lat), "longitude": float(lng)}
    instructions = PromptCatalog.animal_web_search_instructions(
        language=language,
        context=context,
    )
    messages = [{"role": "developer", "content": instructions}]
    recent = _animal_question_history(history)
    if recent:
        # Research the current request afresh. Replaying old assistant answers
        # as this model's own output encouraged it to continue stale contacts.
        # Preserve all recent wording and sources, but mark them as reference data.
        messages.append({
            "role": "user",
            "content": PromptCatalog.prior_conversation_reference(recent),
        })
    messages.append({"role": "user", "content": str(message or "")})
    result = _run_search(
        messages,
        ngo_mode=False,
        tool_choice=tool_choice,
        model_directed=True,
        language=language,
        deadline=deadline,
    )
    from services.search_evidence import review_contact_answer, _number_only_request
    effective_phone_only = _number_only_request(message) if phone_only is None else phone_only
    page_cache: dict[str, str | None] = {}

    def review_result(candidate: SearchResult) -> SearchResult:
        if candidate.result_kind == "unavailable":
            candidate.response = _animal_question_unavailable_response(
                language, request_context=f"{message}\n{contextual_request}",
                phone_only=effective_phone_only,
            )
        if candidate.result_kind != "unavailable":
            review = review_contact_answer(
                message, history, contextual_request, candidate.response,
                [link["url"] for link in candidate.resource_links] + candidate.research_sources,
                language=language,
                requested_institution=requested_institution,
                requested_location_text=requested_location_text,
                phone_only=phone_only,
                deadline=deadline,
                requested_place=resolved_place,
                page_cache=page_cache,
            )
            if review.status != "not_needed":
                candidate.response = review.answer
                candidate.resource_links = review.links
                candidate.result_kind = "search_answer" if review.status == "approved" else "contact_unconfirmed"
                candidate.request_satisfied = getattr(review, "request_satisfied", None)
                candidate.validated_facts = getattr(review, "validated_facts", [])
        return candidate

    result = review_result(result)
    if _provider_research_needs_refinement(message, contextual_request, result, phone_only=effective_phone_only) and deadline - time.monotonic() >= 24.0:
        # One adaptive pass shares the same total turn budget and source cache.
        # This is evidence repair, never a fallback to an unrelated city/provider.
        refinement = {
            "original_request": message,
            "requested_institution": requested_institution,
            "case_place": resolved_place,
            "evidence_issue": result.response[:2500],
            "already_consulted_sources": result.research_sources[:12],
        }
        refined_messages = [*messages, {
            "role": "user",
            "content": PromptCatalog.web_search_refinement(refinement),
        }]
        # _run_search already reserves 35% for source review; do not shrink
        # the same remaining budget twice for the refinement pass.
        candidate = _run_search(refined_messages, ngo_mode=False, tool_choice="required",
            model_directed=True, language=language, deadline=deadline)
        candidate = review_result(candidate)
        if candidate.result_kind != "unavailable" and (
            not _provider_research_needs_refinement(message, contextual_request, candidate, phone_only=effective_phone_only)
            or (not result.resource_links and candidate.resource_links)
            or _validated_partial_is_richer(result, candidate)
        ):
            result = candidate
    # Retain an explicitly requested service boundary even when evidence review
    # rewrites an answer or removes unsupported contact details.
    request_text = f"{message}\n{contextual_request}"
    if not effective_phone_only and re.search(
        r"\brescue\s+pick[ -]?up\b|\b(?:distinguish|difference|assume|separate)\b[^.!?]{0,90}\bpick[ -]?up\b|"
        r"बचाव.{0,20}पिकअप", request_text, re.I,
    ) and not re.search(r"\bpick[ -]?up\b|पिकअप|लेने\s+आ", result.response, re.I):
        result.response += (
            "\n\nइलाज की सुविधा सूचीबद्ध होने से पशु को लेने आने की बचाव सेवा की पुष्टि नहीं होती।"
            if language == "hi" else
            "\n\nA treatment facility's listing does not establish rescue pickup."
        )
    return result


def _validated_partial_is_richer(current: SearchResult, candidate: SearchResult) -> bool:
    """Prefer a useful partial only when grounded facts are preserved and added."""
    old, new = current.validated_facts, candidate.validated_facts
    if not new:
        return False

    def covers(facts: list[dict[str, str]], expected: dict[str, str]) -> bool:
        for fact in facts:
            if (fact.get("kind"), fact.get("institution")) != (expected.get("kind"), expected.get("institution")):
                continue
            actual, wanted = fact.get("value", ""), expected.get("value", "")
            if expected.get("kind") == "location":
                # A fuller grounded address preserves its prior shorter form.
                if wanted and f" {wanted} " in f" {actual} ":
                    return True
            elif actual == wanted:
                return True
        return False

    return all(covers(new, fact) for fact in old) and any(not covers(old, fact) for fact in new)


def _provider_research_needs_refinement(message: str, contextual_request: str, result: SearchResult, *, phone_only: bool) -> bool:
    if result.result_kind == "unavailable":
        return False  # Do not retry authentication/provider outages as new research.
    if result.request_satisfied is not None:
        return not result.request_satisfied
    if not re.search(r"\b(?:find|where|options?|treatment|veterinar\w*|hospital|clinic|college|rescue|contact|phone|number)\b", f"{message} {contextual_request}", re.I):
        return False
    if result.result_kind == "contact_unconfirmed":
        return True
    if phone_only:
        from services.search_evidence import _answer_phone_spans
        return not bool(_answer_phone_spans(result.response))
    return not result.resource_links or bool(re.search(
        r"\b(?:could\s+not|couldn't|unable\s+to|cannot|can't)\s+(?:establish|find|confirm|verify)\b",
        result.response[:350], re.I,
    ))


def _animal_question_history(history: Iterable[dict]) -> list[dict[str, str]]:
    """Retain recent role-tagged answers and their sources within a fixed input budget."""
    recent: list[dict[str, str]] = []
    remaining = 16000
    for entry in reversed(list(history)[-20:]):
        if not isinstance(entry, dict) or entry.get("role") not in {"user", "assistant"}:
            continue
        content = str(entry.get("content") or "").strip()
        metadata = entry.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, TypeError):
                metadata = {}
        references = []
        if entry["role"] == "assistant" and isinstance(metadata, dict):
            links = metadata.get("resource_links")
            for link in links[:10] if isinstance(links, list) else []:
                if not isinstance(link, dict):
                    continue
                url = str(link.get("url") or "")
                if _is_safe_http_url(url) and url not in content:
                    references.append(f"{str(link.get('label') or 'Source')[:160]}: {url}")
        source_context = (
            "\nSources linked in this earlier answer (not freshly checked):\n"
            + "\n".join(references)
            if references else ""
        )
        if not content and not source_context:
            continue
        # Reserve room for links that may be separate from the stored answer text.
        source_context = source_context[:1500]
        content = content[: max(0, 4000 - len(source_context))] + source_context
        if len(content) > remaining:
            break
        recent.append({"role": entry["role"], "content": content})
        remaining -= len(content)
    return list(reversed(recent))


def search_local_animal_help(
    lat: float, lng: float, situation: str = "", language: str = "en",
) -> SearchResult:
    """Coordinate-based lookups use the same broad provider policy as text."""
    if not _coordinates_are_in_india(lat, lng):
        return SearchResult(response=region_scope.INDIA_ONLY_RESPONSE)
    return search_animal_question(
        situation or "Find appropriate nearby veterinary or animal-welfare help.",
        contextual_request="Find appropriate animal-care help for this case; do not assume rescue pickup is available.",
        lat=lat, lng=lng, language=language,
    )


def search_verified_india_local_help(
    canonical_place: str, *, lat: float, lng: float, city: str = "", region: str = "",
    country_code: str = "IN", situation: str = "", language: str = "en",
    named_location_verified: bool = False,
) -> SearchResult:
    """Compatibility entry point: validated geography, shared provider research.

    City-level NGO caches are intentionally not reused for a case-specific need.
    A veterinary college, public hospital or clinic can be the appropriate service.
    """
    if str(country_code or "").upper() != "IN" or not _coordinates_are_valid(lat, lng):
        return SearchResult(response=region_scope.INDIA_ONLY_RESPONSE)
    if not named_location_verified and not _coordinates_are_in_india(lat, lng):
        return SearchResult(response=region_scope.INDIA_ONLY_RESPONSE)
    return search_animal_question(
        situation or f"Find appropriate animal-care help in {canonical_place}.",
        contextual_request=f"Find appropriate help for this case in {canonical_place}; distinguish clinical treatment from pickup.",
        resolved_place=canonical_place, resolved_country_code="IN", lat=lat, lng=lng,
        language=language,
    )


def unavailable_response() -> str:
    return (
        "I could not check current local services right now. This is a search failure, not evidence "
        "that no veterinary or animal-welfare service is available. Please try again shortly."
    )


def _run_search(
    prompt: str | list[dict[str, str]],
    *,
    ngo_mode: bool = False,
    tool_choice: str = "required",
    model_directed: bool = False,
    language: str = "en",
    deadline: float | None = None,
) -> SearchResult:
    failure_text = (
        _animal_question_unavailable_response(language)
        if model_directed else unavailable_response()
    )
    if not config.DOG_WEB_SEARCH_ENABLED or not client:
        logger.info(
            "Dog web search skipped: enabled=%s api_key_configured=%s",
            config.DOG_WEB_SEARCH_ENABLED,
            bool(config.OPENAI_API_KEY),
        )
        return SearchResult(response=failure_text, result_kind="unavailable")

    if ngo_mode:
        return _run_structured_ngo_search(prompt)

    if deadline is not None and time.monotonic() >= deadline:
        return SearchResult(response=failure_text, result_kind="unavailable")
    call_started = time.monotonic()
    try:
        # A little reasoning helps distinguish similarly named institutions and
        # evaluate contact evidence. Non-reasoning model configurations still work.
        reasoning = (
            {"reasoning": {"effort": "low"}}
            if model_directed and config.OPENAI_WEB_SEARCH_MODEL.startswith("gpt-5")
            else {}
        )
        response = client.responses.create(
            model=config.OPENAI_WEB_SEARCH_MODEL,
            tools=[_web_search_tool(search_context_size="medium" if model_directed else "low")],
            tool_choice=tool_choice,
            include=["web_search_call.action.sources"],
            input=prompt,
            store=False,
            max_output_tokens=3200 if model_directed else 900,
            max_tool_calls=config.DOG_WEB_SEARCH_MAX_TOOL_CALLS if model_directed else 3,
            timeout=min(
                config.DOG_WEB_SEARCH_OPENAI_TIMEOUT_SECONDS,
                config.DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS,
                max(0.01, (deadline - time.monotonic()) * 0.65) if deadline is not None else float("inf"),
            ),
            **reasoning,
        )
        web_operations.record_event(component="model:search", status="ok", latency_ms=round((time.monotonic() - call_started) * 1000))
    except Exception as exc:  # noqa: BLE001 - search must degrade gracefully
        web_operations.record_event(component="model:search", status="error", latency_ms=round((time.monotonic() - call_started) * 1000), error_type=type(exc).__name__)
        logger.warning("Dog web search failed: %s", type(exc).__name__)
        return SearchResult(response=failure_text, result_kind="unavailable")

    payload = _model_dump(response)
    answer = (getattr(response, "output_text", "") or "").strip()
    if model_directed:
        answer, links = _render_animal_question_answer(payload, answer)
        # Pages the research model explicitly opened/searched within are the
        # strongest candidates for independent verification.  Keep that
        # semantic order: alphabetically sorting a large result set could push
        # the official page beyond the evidence fetcher's bounded source cap.
        research_sources = _ordered_source_urls(payload)
        searched = any(
            isinstance(item, dict)
            and item.get("type") == "web_search_call"
            and item.get("status", "completed") == "completed"
            for item in payload.get("output") or []
        )
        if not answer:
            return SearchResult(
                response=failure_text, searched=searched, result_kind="unavailable",
                research_sources=research_sources,
            )
        logger.info(
            "Animal-question response completed: model=%s searched=%s cited_sources=%d",
            config.OPENAI_WEB_SEARCH_MODEL, searched, len(links),
        )
        return SearchResult(
            response=answer,
            resource_links=links,
            searched=searched,
            result_kind="search_answer" if searched else "model_answer",
            research_sources=research_sources,
        )
    answer = _trim_follow_up_offer(answer)
    citations = _extract_citations(payload)
    answer = _add_clickable_citations(answer, citations)
    links = _resource_links(citations)

    if not answer:
        return SearchResult(response=unavailable_response())

    logger.info(
        "Dog web search completed: model=%s cited_sources=%d",
        config.OPENAI_WEB_SEARCH_MODEL,
        len(links),
    )
    return SearchResult(response=answer, resource_links=links, searched=True)


def _generic_veterinary_referral(language: str = "en") -> str:
    if language == "hi":
        return "इलाज के लिए नज़दीकी पशु चिकित्सालय या क्लिनिक से संपर्क करें; ज़िला पशुपालन कार्यालय स्थानीय पशु चिकित्सा सेवा तक पहुँचने में मार्गदर्शन दे सकता है। मैं उनके फ़ोन, खुलने के समय या पशु को लेने आने की सेवा की पुष्टि नहीं कर सका।"
    return "For treatment, try a nearby veterinary hospital or clinic; the district animal husbandry office may help you find the local veterinary service. I could not confirm their numbers, opening hours, or rescue pickup."


def _animal_question_unavailable_response(language: str = "en", *, request_context: str = "", phone_only: bool = False) -> str:
    if phone_only:
        return "मैं अभी अनुरोधित फ़ोन नंबर की पुष्टि नहीं कर सका।" if language == "hi" else "I couldn't confirm the requested phone number right now."
    local_help = bool(re.search(
        r"\b(?:veterinar\w*|vets?|hospitals?|clinics?|colleges?|rescue|treatment|animal.care|local\s+help)\b|"
        r"पशु\s*चिकित्स|अस्पताल|क्लिनिक|इलाज|बचाव", request_context, re.I,
    ))
    if language == "hi":
        answer = "मैं अभी वर्तमान सेवाओं की जानकारी की जाँच नहीं कर सका। इसका मतलब यह नहीं कि वहाँ सेवाएँ उपलब्ध नहीं हैं।"
        if local_help:
            return answer + " " + _generic_veterinary_referral(language)
        return answer + " कृपया थोड़ी देर में फिर कोशिश करें।"
    answer = "I could not check current information right now. This does not establish that the requested service is unavailable."
    if local_help:
        return answer + " " + _generic_veterinary_referral(language)
    return answer + " Please try again shortly."


def _render_animal_question_answer(
    payload: dict,
    fallback_text: str,
) -> tuple[str, list[dict[str, str]]]:
    """Render cited claims without promoting every consulted page to a citation."""
    parts = []
    cited = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            text = str(content.get("text") or "")
            edits: dict[tuple[int, int], list[str]] = {}
            for annotation in content.get("annotations") or []:
                if not isinstance(annotation, dict) or (
                    annotation.get("type") != "url_citation"
                    and not isinstance(annotation.get("url_citation"), dict)
                ):
                    continue
                citation = _normalise_citation(annotation)
                if not citation:
                    continue
                cited.append(citation)
                start, end = citation.get("start_index"), citation.get("end_index")
                if citation["url"] in text or not (
                    isinstance(end, int) and 0 <= end <= len(text)
                ):
                    continue
                label = _escape_markdown_text(citation["title"][:80] or "Source")
                marker = f" [{label}](<{citation['url']}>)"
                if (
                    isinstance(start, int) and 0 <= start < end
                    and re.fullmatch(r"\s*cite[^]*\s*", text[start:end])
                ):
                    edit_range = (start, end)
                else:
                    edit_range = (end, end)
                if marker not in edits.setdefault(edit_range, []):
                    edits[edit_range].append(marker)
            for (start, end), markers in sorted(edits.items(), reverse=True):
                text = text[:start] + "".join(markers) + text[end:]
            if text.strip():
                parts.append(text)
    answer = "\n\n".join(parts) if parts else fallback_text
    # A native citation without usable offsets still has a clickable resource link.
    answer = re.sub(r"cite[^]*", "", answer)
    # Markdown sources written in the answer can be used when they were consulted;
    # unrelated web_search_call.action.sources are never appended automatically.
    consulted = _extract_source_urls(payload)
    for match in re.finditer(r"\[([^\]\n]+)\]\(<?(https?://[^\s<>]+?)>?\)", answer):
        url = match.group(2)
        if _source_url_key(url) in consulted:
            citation = _normalise_citation({"url": url, "title": match.group(1)})
            if citation:
                cited.append(citation)
    return _trim_follow_up_offer(answer).strip(), _resource_links(_deduplicate_citations(cited))


def _run_structured_ngo_search(
    prompt: str,
    *,
    web_search_tool: dict[str, Any] | None = None,
    require_official_url_in_sources: bool = False,
    no_results_place: str = "",
    required_city: str = "",
    required_region: str = "",
    language: str = "en",
    allow_region_fallback: bool = False,
    discover_exact_city_candidates: bool = False,
    deadline: float | None = None,
) -> SearchResult:
    search_deadline = time.monotonic() + config.DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS
    if deadline is not None:
        search_deadline = min(search_deadline, deadline)
    tool = web_search_tool or _web_search_tool()
    discovered_candidates: list[dict[str, str]] | None = None
    official_seeds: list[dict[str, str]] = []
    directly_verified: list[dict[str, str]] = []
    candidate_discovery_complete: bool | None = None
    if require_official_url_in_sources and (not required_city or not required_region):
        logger.warning("Strict NGO verification requires a canonical city and region")
        return SearchResult(response=unavailable_response())
    should_discover_exact_city = bool(
        require_official_url_in_sources or discover_exact_city_candidates
    )
    if should_discover_exact_city and (not required_city or not required_region):
        logger.info(
            "Exact-city NGO discovery skipped reason=missing_canonical_geography"
        )
        candidate_discovery_complete = False
    elif should_discover_exact_city:
        # Reserve part of the overall request budget for the independent
        # structured search. A slow discovery call must not consume the full
        # deadline and turn every otherwise recoverable city lookup into an
        # unavailable response.
        discovery_deadline = min(
            search_deadline,
            time.monotonic()
            + max(10.0, config.DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS * 0.5),
        )
        discovered_candidates = _discover_ngo_candidates(
            city=required_city,
            region=required_region,
            web_search_tool=tool,
            deadline=discovery_deadline,
        )
        candidate_discovery_complete = discovered_candidates is not None
        official_seeds = _core_city_official_discovery_seeds(
            required_city,
            required_region,
        )
        if official_seeds:
            discovered_candidates = _dedupe_discovered_candidates(
                [*official_seeds, *(discovered_candidates or [])]
            )
        if discovered_candidates is None:
            logger.info(
                "Exact-city NGO discovery incomplete "
                "reason=structured_discovery_unavailable city=%s region=%s",
                required_city,
                required_region,
            )
            if require_official_url_in_sources:
                return SearchResult(response=unavailable_response())
        elif not discovered_candidates and require_official_url_in_sources:
            answer = _no_verified_options_response(no_results_place, language=language)
            logger.info(
                "Structured dog NGO discovery completed "
                "reason=no_exact_city_candidates"
            )
            return SearchResult(
                response=answer,
                searched=True,
                result_kind="no_results",
                candidate_discovery_complete=True,
            )
        elif not discovered_candidates:
            logger.info(
                "Exact-city NGO discovery completed "
                "reason=no_deterministic_candidates city=%s region=%s",
                required_city,
                required_region,
            )

    page_content_cache: dict[str, str | None] = {}
    verification_state = {"complete": True}
    if discovered_candidates:
        # Verify the small core-city seeds independently so a slow or blocked
        # first website cannot consume the shared crawl window and starve the
        # next known official site. The global request deadline still bounds
        # the combined work.
        for seed in official_seeds:
            seed_verified = _deterministically_verify_discovered_candidates(
                [seed],
                raw_options=[],
                required_city=required_city,
                required_region=required_region,
                page_content_cache=page_content_cache,
                deadline=search_deadline,
                completion_state=verification_state,
            )
            directly_verified = _merge_verified_organizations(
                directly_verified,
                seed_verified,
            )
        remaining_candidates = [
            candidate
            for candidate in discovered_candidates
            if not any(
                _same_website(
                    str(candidate.get("possible_official_url") or ""),
                    str(seed.get("possible_official_url") or ""),
                )
                for seed in official_seeds
            )
        ]
        if remaining_candidates and time.monotonic() < search_deadline:
            discovered_verified = _deterministically_verify_discovered_candidates(
                remaining_candidates,
                raw_options=[],
                required_city=required_city,
                required_region=required_region,
                page_content_cache=page_content_cache,
                deadline=search_deadline,
                completion_state=verification_state,
            )
            directly_verified = _merge_verified_organizations(
                directly_verified,
                discovered_verified,
            )
        elif remaining_candidates:
            verification_state["complete"] = False
        if directly_verified:
            logger.info(
                "Official-site NGO verification retained candidates=%d; "
                "continuing reason=merge_additional_exact_city_candidates",
                len(directly_verified),
            )
        # Candidate websites fail independently. A dead, blocked, or malformed
        # site must not abort verification of every other organization in the
        # city or prevent the structured verifier from trying its own sources.

    if require_official_url_in_sources:
        prompt_mode = "strict"
        request_max_tool_calls = config.DOG_WEB_SEARCH_MAX_TOOL_CALLS
        request_max_output_tokens = 2200
    elif allow_region_fallback:
        prompt_mode = "regional"
        request_max_tool_calls = min(4, config.DOG_WEB_SEARCH_MAX_TOOL_CALLS)
        request_max_output_tokens = 1800
    else:
        prompt_mode = "fast"
        request_max_tool_calls = min(4, config.DOG_WEB_SEARCH_MAX_TOOL_CALLS)
        request_max_output_tokens = 1800

    strict_prompt = PromptCatalog.structured_ngo_verification(
        prompt,
        discovered_candidates or (),
        mode=prompt_mode,
        required_city=required_city,
        required_region=required_region,
    )

    researched = _request_structured_web_search(
        strict_prompt,
        web_search_tool=tool,
        schema=NGO_SEARCH_SCHEMA,
        schema_name="verified_local_animal_rescues",
        max_output_tokens=request_max_output_tokens,
        max_tool_calls=request_max_tool_calls,
        require_sources=require_official_url_in_sources,
        retry_on_empty_field="organizations",
        deadline=search_deadline,
    )
    if researched is None:
        if directly_verified and not allow_region_fallback:
            answer, resource_links = _render_verified_organizations(
                directly_verified,
                language=language,
            )
            logger.info(
                "Returning deterministic NGO candidates "
                "reason=model_merge_unavailable verified_options=%d",
                len(directly_verified),
            )
            return SearchResult(
                response=answer,
                resource_links=resource_links,
                searched=True,
                result_kind="verified_options",
                organizations=directly_verified,
                candidate_discovery_complete=candidate_discovery_complete,
            )
        return SearchResult(response=unavailable_response())
    payload, source_urls = researched
    raw_organizations = payload.get("organizations")
    if not isinstance(raw_organizations, list):
        logger.warning("Structured dog NGO response omitted organizations list")
        if directly_verified and not allow_region_fallback:
            answer, resource_links = _render_verified_organizations(
                directly_verified,
                language=language,
            )
            return SearchResult(
                response=answer,
                resource_links=resource_links,
                searched=True,
                result_kind="verified_options",
                organizations=directly_verified,
                candidate_discovery_complete=False,
            )
        return SearchResult(response=unavailable_response())

    organizations: list[dict[str, str]] = []
    if allow_region_fallback:
        # Model output is discovery data only for regional results. Re-fetch
        # the candidate's own HTTPS website and derive every displayed claim,
        # including its phone, from that bounded official-site crawl.
        regional_candidates: list[dict[str, str]] = []
        seen_candidate_websites: set[str] = set()
        for raw_option in raw_organizations:
            if not isinstance(raw_option, dict):
                continue
            name = _clean_rendered_text(raw_option.get("name"), max_length=160)
            official_url = str(raw_option.get("official_url") or "").strip()
            parsed_url = _parse_http_url(official_url)
            hostname = (
                _normalise_hostname((parsed_url.hostname or "").lower())
                if parsed_url is not None
                else ""
            )
            website_key = _website_identity_key(official_url)
            if (
                not name
                or parsed_url is None
                or parsed_url.scheme.lower() != "https"
                or not hostname
                or not website_key
                or website_key in seen_candidate_websites
                or BANNED_ORGANISATION_RE.search(f"{name} {hostname}")
                or _is_government_or_directory_domain(hostname)
            ):
                continue
            seen_candidate_websites.add(website_key)
            regional_candidates.append(
                {"name": name, "possible_official_url": official_url}
            )
        organizations = _deterministically_verify_discovered_candidates(
            regional_candidates,
            raw_options=[],
            required_city=required_city,
            required_region=required_region,
            page_content_cache=page_content_cache,
            deadline=search_deadline,
            allow_region_fallback=True,
            completion_state=verification_state,
        )
        organizations = [
            option for option in organizations if option.get("phone")
        ]
    elif not require_official_url_in_sources:
        # Normal model output is discovery data only. Every displayed claim is
        # re-derived from a bounded crawl of the candidate's own HTTPS site;
        # model-supplied evidence, phone numbers, and verification booleans are
        # never rendered directly.
        model_candidates: list[dict[str, str]] = []
        for raw_option in raw_organizations:
            if not isinstance(raw_option, dict):
                continue
            name = _clean_rendered_text(raw_option.get("name"), max_length=160)
            official_url = str(raw_option.get("official_url") or "").strip()
            parsed_url = _parse_http_url(official_url)
            hostname = (
                _normalise_hostname((parsed_url.hostname or "").lower())
                if parsed_url is not None
                else ""
            )
            if (
                not name
                or parsed_url is None
                or parsed_url.scheme.lower() != "https"
                or not hostname
                or BANNED_ORGANISATION_RE.search(f"{name} {hostname}")
                or _is_government_or_directory_domain(hostname)
            ):
                continue
            model_candidates.append(
                {"name": name, "possible_official_url": official_url}
            )
        model_verified = _deterministically_verify_discovered_candidates(
            model_candidates,
            raw_options=raw_organizations,
            required_city=required_city,
            required_region=required_region,
            page_content_cache=page_content_cache,
            deadline=search_deadline,
            completion_state=verification_state,
        )
        organizations = _merge_verified_organizations(
            directly_verified,
            model_verified,
        )
    else:
        model_organizations: list[dict[str, str]] = []
        for raw_option in raw_organizations:
            option = _validate_ngo_option(
                raw_option,
                source_urls=source_urls,
                require_official_url_in_sources=require_official_url_in_sources,
                required_city=required_city,
                required_region=required_region,
                discovered_candidates=discovered_candidates,
                page_content_cache=page_content_cache,
                deadline=search_deadline,
                allow_region_fallback=allow_region_fallback,
            )
            if option is None:
                continue
            model_organizations.append(option)

        repaired_candidates: list[dict[str, str]] = []
        if discovered_candidates:
            repaired_candidates = _deterministically_verify_discovered_candidates(
                discovered_candidates,
                raw_options=raw_organizations,
                required_city=required_city,
                required_region=required_region,
                page_content_cache=page_content_cache,
                deadline=search_deadline,
                completion_state=verification_state,
            )
        organizations = _merge_verified_organizations(
            directly_verified,
            repaired_candidates,
            model_organizations,
        )

    if not verification_state["complete"]:
        candidate_discovery_complete = False

    if not organizations:
        if _all_attempted_page_fetches_failed(page_content_cache):
            logger.info("Structured dog NGO verification had an indeterminate page fetch")
            return SearchResult(response=unavailable_response())
        answer = _no_verified_options_response(no_results_place, language=language)
        logger.info("Structured dog NGO web search completed with no verified options")
        return SearchResult(
            response=answer,
            searched=True,
            result_kind="no_results",
            candidate_discovery_complete=candidate_discovery_complete,
        )

    answer, resource_links = _render_verified_organizations(
        organizations,
        language=language,
    )
    logger.info(
        "Structured dog NGO web search completed: model=%s verified_options=%d",
        config.OPENAI_WEB_SEARCH_MODEL,
        len(organizations),
    )
    return SearchResult(
        response=answer,
        resource_links=resource_links,
        searched=True,
        result_kind="verified_options",
        organizations=organizations,
        candidate_discovery_complete=candidate_discovery_complete,
    )


def _validate_ngo_option(
    value: Any,
    *,
    source_urls: set[str] | None = None,
    require_official_url_in_sources: bool = False,
    required_city: str = "",
    required_region: str = "",
    discovered_candidates: list[dict[str, str]] | None = None,
    page_content_cache: dict[str, str | None] | None = None,
    deadline: float | None = None,
    allow_region_fallback: bool = False,
) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    # This validator is only for the citation-bound strict path. Normal and
    # regional search results are candidates, never trusted answer objects;
    # they are handled by deterministic official-site verification instead.
    if not require_official_url_in_sources:
        return None
    option = {
        "name": _clean_rendered_text(value.get("name"), max_length=160),
        "service_area": _clean_rendered_text(value.get("service_area"), max_length=240),
        "service_city": _clean_rendered_text(value.get("service_city"), max_length=100),
        "service_region": _clean_rendered_text(value.get("service_region"), max_length=100),
        "animal_rescue_evidence": _clean_rendered_text(
            value.get("animal_rescue_evidence"),
            max_length=400,
        ),
        "service_area_evidence": _clean_rendered_text(
            value.get("service_area_evidence"),
            max_length=400,
        ),
        "organization_type_evidence": _clean_rendered_text(
            value.get("organization_type_evidence"),
            max_length=400,
        ),
        "animal_rescue_evidence_url": str(
            value.get("animal_rescue_evidence_url") or ""
        ).strip(),
        "service_area_evidence_url": str(
            value.get("service_area_evidence_url") or ""
        ).strip(),
        "organization_type_evidence_url": str(
            value.get("organization_type_evidence_url") or ""
        ).strip(),
        "official_url": str(value.get("official_url") or "").strip(),
        "phone": _clean_rendered_text(value.get("phone"), max_length=80),
        "phone_source_url": str(value.get("phone_source_url") or "").strip(),
        "address": _clean_rendered_text(value.get("address"), max_length=300),
        "address_source_url": str(value.get("address_source_url") or "").strip(),
        "opening_hours": _clean_rendered_text(
            value.get("opening_hours"),
            max_length=160,
        ),
        "opening_hours_source_url": str(
            value.get("opening_hours_source_url") or ""
        ).strip(),
        "coverage_scope": _clean_rendered_text(
            value.get("coverage_scope"),
            max_length=20,
        ),
    }
    if (
        not option["name"]
        or not option["service_area"]
        or not option["animal_rescue_evidence"]
        or not option["service_area_evidence"]
    ):
        return None
    if any(
        value.get(field) is not True
        for field in (
            "rescue_work_verified",
            "service_area_verified",
            "non_governmental",
        )
    ):
        return None
    if NEGATED_RESCUE_EVIDENCE_RE.search(option["animal_rescue_evidence"]):
        return None
    if INACTIVE_OR_INDIRECT_RESCUE_EVIDENCE_RE.search(
        option["animal_rescue_evidence"]
    ):
        return None
    if INDIRECT_RESCUE_CONTENT_RE.search(option["animal_rescue_evidence"]):
        return None
    if not DIRECT_ANIMAL_RESCUE_EVIDENCE_RE.search(option["animal_rescue_evidence"]):
        return None
    if not _is_safe_http_url(option["official_url"]):
        return None
    hostname = (urlparse(option["official_url"]).hostname or "").lower()
    if BANNED_ORGANISATION_RE.search(f"{option['name']} {hostname}"):
        return None
    if _is_government_or_directory_domain(hostname):
        return None
    if option["phone"]:
        option["phone"] = _validated_phone_display(option["phone"])
        if not option["phone"]:
            option["phone_source_url"] = ""
    if required_city and allow_region_fallback:
        if (
            _normalise_cache_component(option["service_region"])
            != _normalise_cache_component(required_region)
            or not _source_backed_region_geography_matches(
                option,
                required_region,
            )
        ):
            return None
        option["service_city"] = required_city
        option["service_region"] = required_region
        option["service_area"] = (
            f"{required_region} network (confirm {required_city} coverage)"
        )
        option["coverage_scope"] = "region"
    elif required_city:
        if not _verified_service_geography_matches(
            option["service_city"],
            option["service_region"],
            required_city,
            required_region,
        ):
            return None
        if NEGATED_SERVICE_AREA_RE.search(option["service_area"]):
            return None
        if require_official_url_in_sources and not _service_evidence_matches_geography(
            option["service_area_evidence"],
            required_city,
            required_region,
        ):
            return None
        if not require_official_url_in_sources and not _source_backed_service_geography_matches(
            option,
            required_city,
            required_region,
        ):
            return None
        option["service_area"] = ", ".join(
            part for part in (required_city, required_region) if part
        )
    if not require_official_url_in_sources:
        if page_content_cache is not None and not _first_party_site_identity_is_valid(
            option["name"],
            option["official_url"],
            page_content_cache=page_content_cache,
            deadline=deadline,
        ):
            return None
        if allow_region_fallback:
            # Never trust a phone copied by the model for a broader regional
            # fallback. It must be rediscovered on the candidate's own site.
            option["phone"] = ""
            option["phone_source_url"] = ""
        for source_field in (
            "animal_rescue_evidence_url",
            "service_area_evidence_url",
            "organization_type_evidence_url",
        ):
            if not _is_safe_http_url(option[source_field]):
                option[source_field] = option["official_url"]
        for detail_field, source_field in (
            ("phone", "phone_source_url"),
            ("address", "address_source_url"),
            ("opening_hours", "opening_hours_source_url"),
        ):
            if option[detail_field] and not _is_safe_http_url(option[source_field]):
                option[source_field] = option["official_url"]
        if not option["phone"]:
            _fill_missing_phone_from_source(
                option,
                required_city=required_city,
                required_region=required_region,
                page_content_cache=page_content_cache,
                deadline=deadline,
            )
        if allow_region_fallback and not option["phone"]:
            return None
    if require_official_url_in_sources:
        if (
            not _evidence_excerpt_shape_is_valid(
                option["organization_type_evidence"]
            )
            or not _organization_type_evidence_is_valid(
                option["organization_type_evidence"],
                option["name"],
            )
        ):
            return None
        sources = source_urls or set()
        if not _url_is_in_sources(option["official_url"], sources):
            return None
        if not discovered_candidates or not _matches_discovered_candidate(
            option["name"],
            option["official_url"],
            discovered_candidates,
        ):
            return None
        for source_field in (
            "animal_rescue_evidence_url",
            "service_area_evidence_url",
            "organization_type_evidence_url",
        ):
            if not _official_claim_source(
                option[source_field],
                official_url=option["official_url"],
                source_urls=sources,
            ):
                return None
        for detail_field, source_field in (
            ("phone", "phone_source_url"),
            ("address", "address_source_url"),
            ("opening_hours", "opening_hours_source_url"),
        ):
            if bool(option[detail_field]) != bool(option[source_field]):
                return None
            if option[detail_field] and not _official_claim_source(
                option[source_field],
                official_url=option["official_url"],
                source_urls=sources,
            ):
                return None
        if page_content_cache is None or not _official_page_content_supports_option(
            option,
            required_city=required_city,
            required_region=required_region,
            page_content_cache=page_content_cache,
            deadline=deadline,
        ):
            return None
    return option


def _source_backed_service_geography_matches(
    option: dict[str, str],
    required_city: str,
    required_region: str,
) -> bool:
    """Fast production check: bind NGO results to the verified city without deep crawling."""
    if not required_city:
        return False
    # A registered-office address, URL slug, or unrelated branch must not turn
    # rescue work in another city into exact-city coverage. Require the source
    # excerpt designated as service evidence itself to name the verified city.
    service_text = option.get("service_area_evidence", "")
    return _service_evidence_matches_geography(
        service_text,
        required_city,
        required_region,
    )


def _source_backed_region_geography_matches(
    option: dict[str, str],
    required_region: str,
) -> bool:
    """Require the service excerpt itself to prove work in the region."""
    return _regional_service_evidence_matches(
        option.get("service_area_evidence", ""),
        required_region,
    )


def _regional_service_evidence_matches(
    evidence: str,
    required_region: str,
) -> bool:
    """Require one sentence to bind operational animal work to the region."""
    if not required_region:
        return False
    for clause in re.split(r"(?<=[.!?;])\s+|[\n|]+", str(evidence or "")):
        if (
            not _has_standalone_location_occurrence(clause, required_region)
            or NEGATED_SERVICE_AREA_RE.search(clause)
            or not ANIMAL_SERVICE_CONTEXT_RE.search(clause)
            or _regional_clause_has_unscoped_conflicting_region(
                clause,
                required_region,
            )
        ):
            continue
        if (
            AFFIRMATIVE_SERVICE_AREA_RE.search(clause)
            or DIRECT_ANIMAL_RESCUE_EVIDENCE_RE.search(clause)
            or re.search(
                r"\b(?:rescue\s+teams?|feeding\s+programs?|animal\s+welfare|"
                r"veterinary\s+partnerships?|rescue|feeding|welfare)\b"
                r"[^.;]{0,180}\b(?:across|throughout|within|in)\b",
                clause,
                re.IGNORECASE,
            )
            or re.search(
                r"\b(?:across|throughout|within|in)\b[^.;]{0,180}"
                r"\b(?:rescue|feeding|animal\s+welfare|veterinary)\b",
                clause,
                re.IGNORECASE,
            )
        ):
            return True
    return False


def _regional_clause_has_unscoped_conflicting_region(
    clause: str,
    required_region: str,
) -> bool:
    """Reject another state unless an explicit across/throughout list scopes both."""
    expected = _canonical_india_region_name(required_region)
    mentions: list[tuple[int, int, str]] = []
    for region_name in sorted(
        set(query_router.INDIA_STATES) | set(INDIA_REGION_ALIASES),
        key=lambda value: len(_normalize_page_evidence(value)),
        reverse=True,
    ):
        for start, end in _normalized_phrase_spans(clause, region_name):
            canonical = _canonical_india_region_name(region_name)
            if any(
                start < existing_end and end > existing_start
                for existing_start, existing_end, _ in mentions
            ):
                continue
            mentions.append((start, end, canonical))
    if not any(region == expected for _, _, region in mentions):
        return True
    if not any(region != expected for _, _, region in mentions):
        return False
    scope_marker = re.search(r"\b(?:across|throughout)\b", clause, re.IGNORECASE)
    if scope_marker and all(start > scope_marker.end() for start, _, _ in mentions):
        return False
    return True


def _discover_ngo_candidates(
    *,
    city: str,
    region: str,
    web_search_tool: dict[str, Any],
    deadline: float | None = None,
    regional_scope: bool = False,
) -> list[dict[str, str]] | None:
    """Run candidate discovery separately from official-site verification."""
    discovery_prompt = PromptCatalog.ngo_discovery(
        city=city,
        region=region,
        maximum_candidates=max(5, config.DOG_WEB_SEARCH_MAX_RESULTS * 2),
        regional_scope=regional_scope,
    )
    discovery_tool = dict(web_search_tool)
    if not regional_scope:
        # Candidate recall is the bottleneck: verification remains bounded and
        # fail-closed, so a broader discovery context improves coverage without
        # relaxing which organisations may be shown to the user.
        discovery_tool["search_context_size"] = "high"
    researched = _request_structured_web_search(
        discovery_prompt,
        web_search_tool=discovery_tool,
        schema=NGO_DISCOVERY_SCHEMA,
        schema_name="local_animal_rescue_candidates",
        max_output_tokens=1000,
        max_tool_calls=min(4, config.DOG_WEB_SEARCH_MAX_TOOL_CALLS),
        require_sources=True,
        retry_on_empty_field="candidates",
        deadline=deadline,
    )
    if researched is None:
        return None
    payload, source_urls = researched
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        return None

    candidates: list[dict[str, str]] = []
    for value in raw_candidates:
        if not isinstance(value, dict):
            continue
        name = _clean_discovered_candidate_name(
            _clean_rendered_text(value.get("name"), max_length=160)
        )
        url = str(value.get("possible_official_url") or "").strip()
        hostname = (urlparse(url).hostname or "").lower() if _is_safe_http_url(url) else ""
        if (
            not name
            or not hostname
            or BANNED_ORGANISATION_RE.search(f"{name} {hostname}")
            or _is_government_or_directory_domain(hostname)
            or not _url_is_in_sources(url, source_urls)
        ):
            continue
        candidates.append({"name": name, "possible_official_url": url})
    return _dedupe_discovered_candidates(candidates)[
        : max(5, config.DOG_WEB_SEARCH_MAX_RESULTS * 2)
    ]


def _core_city_official_discovery_seeds(
    city: str,
    region: str,
) -> list[dict[str, str]]:
    normalized_city = _normalise_cache_component(city)
    if normalized_city in {
        _normalise_cache_component(value)
        for value in DHARAMSHALA_SERVICE_CITY_ALIASES
    }:
        normalized_city = "dharamshala"
    key = (
        normalized_city,
        _normalise_cache_component(region),
    )
    return [dict(candidate) for candidate in CORE_CITY_OFFICIAL_DISCOVERY_SEEDS.get(key, ())]


def _clean_discovered_candidate_name(value: str) -> str:
    """Remove search-result descriptors while preserving a distinctive name."""
    name = str(value or "").strip()
    parts = re.split(r"\s+[\-–—]\s+", name, maxsplit=1)
    if len(parts) != 2:
        return name
    base = parts[0].strip()
    tokens = _canonical_organization_name(base).split()
    if (
        len(tokens) >= 2
        and len("".join(tokens)) >= 6
        and any(token not in GENERIC_ORGANIZATION_NAME_TOKENS for token in tokens)
    ):
        return base
    return name


def _dedupe_discovered_candidates(
    candidates: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    """Keep one discovery seed per official domain, preferring a service page."""
    deduplicated: list[dict[str, str]] = []
    domain_indexes: dict[str, int] = {}
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, dict):
            continue
        name = _clean_rendered_text(raw_candidate.get("name"), max_length=160)
        url = str(raw_candidate.get("possible_official_url") or "").strip()
        parsed = _parse_http_url(url)
        website_key = _website_identity_key(url)
        if not name or not website_key:
            continue
        candidate = {"name": name, "possible_official_url": url}
        existing_index = domain_indexes.get(website_key)
        if existing_index is None:
            domain_indexes[website_key] = len(deduplicated)
            deduplicated.append(candidate)
            continue
        existing = deduplicated[existing_index]
        if _candidate_seed_priority(url) < _candidate_seed_priority(
            existing["possible_official_url"]
        ):
            deduplicated[existing_index] = candidate
        logger.info(
            "Deduplicated NGO discovery candidate "
            "reason=duplicate_official_site site=%s",
            website_key,
        )
    return deduplicated


def _candidate_seed_priority(url: str) -> tuple[int, int]:
    """Prefer a supplied animal-service page over a generic site root."""
    parsed = _parse_http_url(url)
    if parsed is None:
        return (1000, len(str(url or "")))
    path = _normalize_page_evidence(parsed.path or "/")
    score = 100 if (parsed.path or "/") == "/" else 0
    for marker, weight in (
        ("animal care", 120),
        ("veterinary", 110),
        ("rescue", 100),
        ("hospital", 90),
        ("clinic", 80),
        ("treatment", 70),
    ):
        if marker in path:
            score -= weight
    return score, len(url)


def _request_structured_web_search(
    prompt: str,
    *,
    web_search_tool: dict[str, Any],
    schema: dict[str, Any],
    schema_name: str,
    max_output_tokens: int,
    max_tool_calls: int,
    require_sources: bool,
    retry_on_empty_field: str = "",
    deadline: float | None = None,
) -> tuple[dict[str, Any], set[str]] | None:
    """Request schema-constrained web research with one bounded transient retry."""
    for attempt in range(2):
        remaining = (
            deadline - time.monotonic()
            if deadline is not None
            else config.DOG_WEB_SEARCH_OPENAI_TIMEOUT_SECONDS
        )
        if remaining <= 1.0:
            logger.info("Structured web research skipped after overall deadline")
            return None
        try:
            response = client.responses.create(
                model=config.OPENAI_WEB_SEARCH_MODEL,
                tools=[web_search_tool],
                tool_choice="required",
                include=["web_search_call.action.sources"],
                input=prompt,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    }
                },
                max_output_tokens=max_output_tokens,
                max_tool_calls=max_tool_calls,
                timeout=min(
                    config.DOG_WEB_SEARCH_OPENAI_TIMEOUT_SECONDS,
                    remaining,
                ),
            )
            payload = json.loads((getattr(response, "output_text", "") or "").strip())
            if not isinstance(payload, dict):
                raise ValueError("Structured web-search response must be a JSON object")
            required_array_fields = [
                key
                for key, definition in (schema.get("properties") or {}).items()
                if isinstance(definition, dict)
                and definition.get("type") == "array"
                and key in (schema.get("required") or [])
            ]
            if any(not isinstance(payload.get(key), list) for key in required_array_fields):
                raise ValueError("Structured web-search response omitted a required array")
            source_urls = _extract_source_urls(_model_dump(response))
            if require_sources and not source_urls:
                raise ValueError("Structured web-search response did not include sources")
            if (
                retry_on_empty_field
                and attempt == 0
                and payload.get(retry_on_empty_field) == []
            ):
                logger.info(
                    "Structured web research returned an empty %s list; retrying once",
                    retry_on_empty_field,
                )
                continue
            return payload, source_urls
        except Exception as exc:  # noqa: BLE001 - bounded retry then safe failure
            logger.warning(
                "Structured web research attempt %d failed: %s",
                attempt + 1,
                type(exc).__name__,
            )
    return None


def _matches_discovered_candidate(
    name: str,
    official_url: str,
    candidates: list[dict[str, str]],
) -> bool:
    for candidate in candidates:
        candidate_url = str(candidate.get("possible_official_url") or "")
        if not _same_website(official_url, candidate_url):
            continue
        if _organization_names_match(name, str(candidate.get("name") or "")):
            return True
    return False


def _organization_names_match(left: str, right: str) -> bool:
    left_name = _canonical_organization_name(left)
    right_name = _canonical_organization_name(right)
    if not left_name or not right_name:
        return False
    if left_name == right_name:
        return True
    left_core = _organization_name_without_legal_suffix(left_name)
    right_core = _organization_name_without_legal_suffix(right_name)
    return bool(
        left_core
        and left_core == right_core
        # Do not equate two differently suffixed legal entities. The safe
        # tolerance is a public short name on one side and that same name plus
        # a legal suffix (for example "Tibet Charity" / "... Trust").
        and (left_name == left_core or right_name == right_core)
    )


def _canonical_organization_name(value: str) -> str:
    return " ".join(
        re.findall(r"[^\W_]+", str(value or "").casefold(), flags=re.UNICODE)
    )


def _organization_name_without_legal_suffix(value: str) -> str:
    canonical = _canonical_organization_name(value)
    tokens = canonical.split()
    if not tokens or tokens[-1] not in ORGANIZATION_LEGAL_SUFFIXES:
        return canonical
    core_tokens = tokens[:-1]
    compact_core = "".join(core_tokens)
    if (
        len(core_tokens) < 2
        or len(compact_core) < 8
        or not any(
            len(token) >= 3
            and token not in GENERIC_ORGANIZATION_NAME_TOKENS
            for token in core_tokens
        )
    ):
        return canonical
    return " ".join(core_tokens)


def _organization_name_variants(value: str) -> tuple[str, ...]:
    raw_value = str(value or "")
    variants = [raw_value]
    variants.extend(re.findall(r"\(([^)]+)\)", raw_value))
    variants.extend(re.split(r"\s+[\-–—]\s+|\s*\(", raw_value, maxsplit=1)[:1])
    expanded: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        canonical = _canonical_organization_name(variant)
        for name_variant in (
            canonical,
            _organization_name_without_legal_suffix(canonical),
        ):
            if name_variant and name_variant not in seen:
                seen.add(name_variant)
                expanded.append(name_variant)
    return tuple(expanded)


def _merge_verified_organizations(
    *organization_groups: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    """Merge independently verified results without duplicating an NGO/domain."""
    merged: list[dict[str, str]] = []
    detail_pairs = (
        ("phone", "phone_source_url"),
        ("address", "address_source_url"),
        ("opening_hours", "opening_hours_source_url"),
    )
    for group in organization_groups:
        for raw_option in group:
            if not isinstance(raw_option, dict):
                continue
            option = dict(raw_option)
            website_key = _website_identity_key(option.get("official_url", ""))
            duplicate: dict[str, str] | None = None
            duplicate_reason = ""
            for existing in merged:
                existing_website_key = _website_identity_key(
                    existing.get("official_url", "")
                )
                if website_key and website_key == existing_website_key:
                    duplicate = existing
                    duplicate_reason = "duplicate_official_site"
                    break
                if _organization_names_match(
                    option.get("name", ""),
                    existing.get("name", ""),
                ):
                    duplicate = existing
                    duplicate_reason = "duplicate_organization_name"
                    break
            if duplicate is None:
                merged.append(option)
                continue
            for detail_field, source_field in detail_pairs:
                if not duplicate.get(detail_field) and option.get(detail_field):
                    duplicate[detail_field] = option[detail_field]
                    duplicate[source_field] = option.get(source_field, "")
            logger.info(
                "Deduplicated verified NGO reason=%s site=%s",
                duplicate_reason,
                website_key,
            )
    return merged[: config.DOG_WEB_SEARCH_MAX_RESULTS]


def _official_claim_source(
    url: str,
    *,
    official_url: str,
    source_urls: set[str],
) -> bool:
    return (
        _is_safe_http_url(url)
        and _url_is_in_sources(url, source_urls)
        and _same_website(url, official_url)
    )


def _official_page_content_supports_option(
    option: dict[str, str],
    *,
    required_city: str,
    required_region: str,
    page_content_cache: dict[str, str | None],
    deadline: float | None = None,
) -> bool:
    """Verify model claims against text fetched from the cited public pages."""
    service_evidence = option.get("service_area_evidence", "")
    if not service_evidence or not _service_evidence_matches_geography(
        service_evidence,
        required_city,
        required_region,
    ):
        return False

    official_text = _cached_public_page_text(
        option["official_url"], page_content_cache, deadline=deadline
    )
    if not official_text or not _organization_name_appears_on_page(
        option["name"],
        official_text,
    ):
        return False
    root_url = _website_root_url(option["official_url"])
    root_text = _cached_public_page_text(
        root_url, page_content_cache, deadline=deadline
    )
    if not root_text or not _organization_name_appears_on_page(
        option["name"],
        root_text,
    ):
        return False

    rescue_text = _cached_public_page_text(
        option["animal_rescue_evidence_url"],
        page_content_cache,
        deadline=deadline,
    )
    if not rescue_text or not _evidence_excerpt_appears_on_page(
        option["animal_rescue_evidence"],
        rescue_text,
    ):
        return False

    service_text = _cached_public_page_text(
        option["service_area_evidence_url"],
        page_content_cache,
        deadline=deadline,
    )
    if (
        not service_text
        or not _evidence_excerpt_appears_on_page(service_evidence, service_text)
    ):
        return False

    organization_type_text = _cached_public_page_text(
        option["organization_type_evidence_url"],
        page_content_cache,
        deadline=deadline,
    )
    if (
        not organization_type_text
        or not _evidence_excerpt_appears_on_page(
            option["organization_type_evidence"],
            organization_type_text,
        )
    ):
        return False

    for detail_field, source_field in (
        ("phone", "phone_source_url"),
        ("address", "address_source_url"),
        ("opening_hours", "opening_hours_source_url"),
    ):
        detail = option.get(detail_field, "")
        if not detail:
            continue
        detail_text = _cached_public_page_text(
            option[source_field], page_content_cache, deadline=deadline
        )
        if (
            not detail_text
            or not _published_detail_matches_city_block(
                detail,
                detail_text,
                detail_field,
                required_city,
            )
        ):
            option[detail_field] = ""
            option[source_field] = ""
    return True


def _deterministically_verify_discovered_candidates(
    candidates: list[dict[str, str]],
    *,
    raw_options: list[Any],
    required_city: str,
    required_region: str,
    page_content_cache: dict[str, str | None],
    deadline: float | None = None,
    allow_region_fallback: bool = False,
    completion_state: dict[str, bool] | None = None,
) -> list[dict[str, str]]:
    """Corroborate discovery candidates from bounded official-site page content."""
    verified: list[dict[str, str]] = []
    seen_websites: set[str] = set()
    crawl_deadline = time.monotonic() + 20.0
    if deadline is not None:
        crawl_deadline = min(crawl_deadline, deadline)
    candidate_queue = _dedupe_discovered_candidates(candidates)
    if allow_region_fallback:
        candidate_queue.sort(key=_regional_candidate_priority)
    stopped_for_result_limit = False
    for candidate in candidate_queue:
        if time.monotonic() >= crawl_deadline:
            break
        name = _clean_rendered_text(candidate.get("name"), max_length=160)
        candidate_url = str(candidate.get("possible_official_url") or "").strip()
        parsed_candidate = _parse_http_url(candidate_url)
        if (
            not name
            or parsed_candidate is None
            or parsed_candidate.scheme.lower() != "https"
        ):
            continue
        website = _website_identity_key(candidate_url)
        if not website or website in seen_websites:
            continue

        seeds = [candidate_url, _website_root_url(candidate_url)]
        for raw_option in raw_options:
            if not isinstance(raw_option, dict):
                continue
            for field in (
                "official_url",
                "animal_rescue_evidence_url",
                "service_area_evidence_url",
                "organization_type_evidence_url",
            ):
                raw_url = str(raw_option.get(field) or "").strip()
                if _same_website(raw_url, candidate_url):
                    seeds.append(raw_url)

        pages = _crawl_candidate_website_pages(
            seeds,
            required_city=required_city,
            page_content_cache=page_content_cache,
            deadline=crawl_deadline,
            prefer_about=allow_region_fallback,
        )
        identity_matcher = (
            _organization_name_appears_anywhere
            if allow_region_fallback
            else _organization_name_appears_on_page
        )
        identity_page = next(
            (
                (url, page)
                for url, page in pages
                if identity_matcher(name, page)
            ),
            None,
        )
        root_identity = next(
            (
                (url, page)
                for url, page in pages
                if _source_url_key(url) == _source_url_key(_website_root_url(url))
            ),
            None,
        )
        if identity_page is None or not (
            (
                root_identity is not None
                and bool(
                    getattr(root_identity[1], "identity_text", "")
                    or getattr(root_identity[1], "identity_blocks", ())
                )
                and _organization_name_appears_on_page(name, root_identity[1])
            )
            or _organization_name_matches_hostname(name, candidate_url)
        ):
            continue

        rescue = _find_evidence_on_pages(
            pages,
            lambda excerpt: bool(
                DIRECT_ANIMAL_RESCUE_EVIDENCE_RE.search(excerpt)
                and not NEGATED_RESCUE_EVIDENCE_RE.search(excerpt)
                and not INACTIVE_OR_INDIRECT_RESCUE_EVIDENCE_RE.search(excerpt)
                and not INDIRECT_RESCUE_CONTENT_RE.search(excerpt)
            ),
        )
        if allow_region_fallback:
            service_predicate = lambda excerpt: _regional_service_evidence_matches(
                excerpt,
                required_region,
            )
        else:
            service_predicate = lambda excerpt: _service_evidence_matches_geography(
                excerpt,
                required_city,
                required_region,
            )
        service = _find_evidence_on_pages(pages, service_predicate)
        organization_type = _find_evidence_on_pages(
            pages,
            lambda excerpt: _organization_type_evidence_is_valid(
                excerpt,
                name,
            ),
        )
        if not rescue or not service or not organization_type:
            continue

        verified_option = {
                "name": name,
                "service_area": (
                    f"{required_region} network (confirm {required_city} coverage)"
                    if allow_region_fallback
                    else ", ".join(
                        part for part in (required_city, required_region) if part
                    )
                ),
                "service_city": required_city,
                "service_region": required_region,
                "animal_rescue_evidence": rescue[1],
                "service_area_evidence": service[1],
                "organization_type_evidence": organization_type[1],
                "animal_rescue_evidence_url": rescue[0],
                "service_area_evidence_url": service[0],
                "organization_type_evidence_url": organization_type[0],
                "official_url": (
                    _website_root_url(identity_page[0])
                    if allow_region_fallback
                    else identity_page[0]
                ),
                "phone": "",
                "phone_source_url": "",
                "address": "",
                "address_source_url": "",
                "opening_hours": "",
                "opening_hours_source_url": "",
            }
        if allow_region_fallback:
            verified_option["coverage_scope"] = "region"
        _fill_missing_phone_from_source(
            verified_option,
            required_city=required_city,
            required_region=required_region,
            page_content_cache=page_content_cache,
            deadline=crawl_deadline,
        )
        verified.append(verified_option)
        seen_websites.add(website)
        if (
            (allow_region_fallback and verified_option.get("phone"))
            or len(verified) >= config.DOG_WEB_SEARCH_MAX_RESULTS
        ):
            stopped_for_result_limit = True
            break
    verification_complete = bool(
        stopped_for_result_limit
        or (
            time.monotonic() < crawl_deadline
            and PAGE_FETCH_BUDGET_SENTINEL not in page_content_cache
            and all(
                value is not None
                for key, value in page_content_cache.items()
                if key != PAGE_FETCH_BUDGET_SENTINEL
            )
        )
    )
    if completion_state is not None:
        completion_state["complete"] = bool(
            completion_state.get("complete", True) and verification_complete
        )
    if allow_region_fallback:
        verified.sort(key=lambda option: (not bool(option.get("phone")), option["name"]))
    return verified


def _regional_candidate_priority(candidate: dict[str, str]) -> tuple[int, int, int, str]:
    """Prefer likely direct-rescue organizations without relying on city lists."""
    text = _normalize_page_evidence(
        f"{candidate.get('name', '')} {candidate.get('possible_official_url', '')}"
    )
    return (
        0 if "rescue" in text else 1,
        0 if "animal welfare" in text or "welfare" in text else 1,
        0 if "animal" in text or "dog" in text else 1,
        text,
    )


def _crawl_candidate_website_pages(
    seed_urls: Iterable[str],
    *,
    required_city: str,
    page_content_cache: dict[str, str | None],
    deadline: float,
    prefer_about: bool = False,
) -> list[tuple[str, str]]:
    queue: list[str] = []
    queued: set[str] = set()
    root_url = ""
    preferred_seed_key = ""
    for seed in seed_urls:
        key = _source_url_key(seed)
        parsed = _parse_http_url(seed)
        if (
            not key
            or parsed is None
            or parsed.scheme.lower() != "https"
            or key in queued
        ):
            continue
        if not root_url:
            root_url = seed
        if root_url and not _same_website(seed, root_url):
            continue
        if not preferred_seed_key:
            # The discovery result may already be the relevant service page
            # (for example /animal-care/). Inspect it before root/contact/about
            # sorting can consume the four-page crawl budget.
            preferred_seed_key = key
        queued.add(key)
        queue.append(seed)

    pages: list[tuple[str, str]] = []
    visited: set[str] = set()
    while queue and len(pages) < 4 and time.monotonic() < deadline:
        preferred_index = next(
            (
                index
                for index, value in enumerate(queue)
                if _source_url_key(value) == preferred_seed_key
            ),
            None,
        )
        if preferred_index is not None:
            url = queue.pop(preferred_index)
            preferred_seed_key = ""
        else:
            queue.sort(
                key=lambda value: _candidate_page_priority(
                    value,
                    required_city,
                    prefer_about=prefer_about,
                )
            )
            url = queue.pop(0)
        key = _source_url_key(url)
        if not key or key in visited:
            continue
        visited.add(key)
        page = _cached_public_page_text(
            url,
            page_content_cache,
            deadline=deadline,
        )
        if not page:
            continue
        pages.append((url, page))
        for linked_url in getattr(page, "links", ()):
            linked_key = _source_url_key(linked_url)
            if (
                linked_key
                and linked_key not in queued
                and linked_key not in visited
                and _same_website(linked_url, root_url)
            ):
                queued.add(linked_key)
                queue.append(linked_url)
    return pages


def _candidate_page_priority(
    url: str,
    required_city: str,
    *,
    prefer_about: bool = False,
) -> tuple[int, int]:
    parsed = _parse_http_url(url)
    path = _normalize_page_evidence(parsed.path if parsed is not None else url)
    city_tokens = _normalize_page_evidence(required_city).split()
    score = 0
    if parsed is not None and (parsed.path or "/") == "/":
        score -= 70
    if city_tokens and all(token in path.split() for token in city_tokens):
        score -= 100
    marker_weights = (
        ("animal care", 130),
        ("veterinary", 120),
        ("rescue", 110),
        ("treatment", 100),
        ("welfare", 90),
        ("centre", 50),
        ("center", 50),
        ("about", 120 if prefer_about else 80),
        ("work", 90 if prefer_about else 30),
        ("contact", 50),
    )
    for marker, weight in marker_weights:
        if marker in path:
            score -= weight
    if re.search(r"(?:^|\s)(?:hi|hindi)(?:\s|$)", path):
        score += 100
    return score, len(url)


def _find_evidence_on_pages(
    pages: Iterable[tuple[str, str]],
    predicate,
) -> tuple[str, str] | None:
    for url, page in pages:
        for block in _page_evidence_blocks(page):
            excerpt = _find_exact_evidence_excerpt(block, predicate)
            if excerpt:
                return url, excerpt
    return None


def _page_evidence_blocks(page_text: str) -> tuple[str, ...]:
    """Return independent DOM/identity blocks without joining unrelated text."""
    identity_blocks = tuple(getattr(page_text, "identity_blocks", ()) or ())
    visible_blocks = tuple(getattr(page_text, "blocks", ()) or ())
    blocks = tuple(part for part in (*identity_blocks, *visible_blocks) if part)
    if blocks:
        return blocks
    if getattr(page_text, "is_html", False):
        return ()
    fallback = str(page_text).strip()
    return (fallback,) if fallback else ()


def _find_exact_evidence_excerpt(text: str, predicate) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    if _evidence_excerpt_shape_is_valid(value) and predicate(value):
        return value
    tokens = list(re.finditer(r"\S+", value))
    max_width = min(40, len(tokens))
    widths = list(range(12, max_width + 1)) + list(range(6, min(12, max_width + 1)))
    for width in widths:
        for start in range(0, len(tokens) - width + 1):
            excerpt = value[tokens[start].start() : tokens[start + width - 1].end()]
            if (
                _evidence_excerpt_shape_is_valid(excerpt)
                and predicate(excerpt)
            ):
                return excerpt
    return ""


def _cached_public_page_text(
    url: str,
    page_content_cache: dict[str, str | None],
    *,
    deadline: float | None = None,
) -> str | None:
    key = _source_url_key(url)
    if not key:
        return None
    if key not in page_content_cache:
        # Bound verification work even when the model returns many distinct pages.
        page_count = sum(
            1
            for cached_key in page_content_cache
            if cached_key != PAGE_FETCH_BUDGET_SENTINEL
        )
        if page_count >= 12:
            page_content_cache[PAGE_FETCH_BUDGET_SENTINEL] = None
            return None
        page_content_cache[key] = _fetch_public_page_text(url, deadline=deadline)
        if page_content_cache[key] is None:
            parsed = _parse_http_url(url)
            logger.info(
                "Official NGO page unavailable host=%s path=%s",
                (parsed.hostname or "") if parsed is not None else "invalid",
                (parsed.path or "/")[:160] if parsed is not None else "invalid",
            )
    return page_content_cache[key]


def _all_attempted_page_fetches_failed(
    page_content_cache: dict[str, str | None],
) -> bool:
    """Return true only when no candidate page could be inspected at all."""
    attempted = [
        value
        for key, value in page_content_cache.items()
        if key != PAGE_FETCH_BUDGET_SENTINEL
    ]
    return bool(attempted) and all(value is None for value in attempted)


def _fetch_public_page_text(
    url: str,
    *,
    deadline: float | None = None,
    allow_public_redirects: bool = False,
) -> str | None:
    if deadline is not None and time.monotonic() >= deadline:
        return None
    if not PAGE_FETCH_SEMAPHORE.acquire(timeout=0.25):
        logger.info("Official NGO page verification concurrency limit reached")
        return None
    try:
        return _fetch_public_page_text_unlocked(url, deadline=deadline, allow_public_redirects=allow_public_redirects)
    finally:
        PAGE_FETCH_SEMAPHORE.release()


def _fetch_public_page_text_unlocked(
    url: str,
    *,
    deadline: float | None = None,
    allow_public_redirects: bool = False,
) -> str | None:
    """Fetch bounded HTTPS text by connecting only to a DNS-validated public IP."""
    original_url = url
    # Upgrade old published HTTP links; never send an unencrypted request.
    current_url = "https://" + url[7:] if url.lower().startswith("http://") else url
    visited: set[str] = set()
    fetch_deadline = time.monotonic() + max(
        8.0,
        config.DOG_WEB_PAGE_VERIFY_TIMEOUT_SECONDS + 4.0,
    )
    if deadline is not None:
        fetch_deadline = min(fetch_deadline, deadline)
    for _ in range(4):
        if time.monotonic() >= fetch_deadline:
            return None
        if not allow_public_redirects and not _same_website(original_url, current_url):
            return None
        addresses = _public_https_addresses(current_url, deadline=fetch_deadline)
        if not addresses:
            return None
        current_key = _source_url_key(current_url)
        if not current_key or current_key in visited:
            return None
        visited.add(current_key)
        parsed = _parse_http_url(current_url)
        if parsed is None:
            return None
        try:
            hostname = (parsed.hostname or "").encode("idna").decode("ascii")
        except UnicodeError:
            return None
        target = parsed.path or "/"
        if parsed.params:
            target += f";{parsed.params}"
        if parsed.query:
            target += f"?{parsed.query}"

        response = None
        pool = None
        for address in addresses:
            remaining = fetch_deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                pool = urllib3.HTTPSConnectionPool(
                    address,
                    port=443,
                    server_hostname=hostname,
                    assert_hostname=hostname,
                    cert_reqs="CERT_REQUIRED",
                    timeout=urllib3.Timeout(
                        connect=min(3.0, max(0.25, remaining)),
                        read=min(
                            config.DOG_WEB_PAGE_VERIFY_TIMEOUT_SECONDS,
                            max(0.25, remaining),
                        ),
                    ),
                    retries=False,
                    maxsize=1,
                    block=True,
                )
                response = pool.request(
                    "GET",
                    target,
                    headers={
                        "Host": hostname,
                        "User-Agent": (
                            "AskDorjee/1.0 "
                            "(+https://askdorjee.dharamsalaanimalrescue.org)"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9",
                        "Accept-Encoding": "identity",
                    },
                    redirect=False,
                    preload_content=False,
                    decode_content=True,
                )
                break
            except (OSError, urllib3.exceptions.HTTPError):
                if pool is not None:
                    pool.close()
                response = None
                pool = None
        if response is None or pool is None:
            logger.info("Official NGO page fetch failed host=%s", hostname)
            return None
        try:
            if response.status in {301, 302, 303, 307, 308}:
                redirect = str(response.headers.get("Location") or "").strip()
                if not redirect:
                    return None
                current_url = urljoin(current_url, redirect)
                continue
            if response.status != 200:
                return None
            content_type = str(response.headers.get("Content-Type") or "").lower()
            media_type = content_type.split(";", 1)[0].strip()
            if media_type not in {"text/html", "application/xhtml+xml", "text/plain", "application/pdf"}:
                return None
            max_bytes = config.DOG_WEB_PDF_VERIFY_MAX_BYTES if media_type == "application/pdf" else config.DOG_WEB_PAGE_VERIFY_MAX_BYTES
            content_encoding = str(response.headers.get("Content-Encoding") or "").lower()
            if content_encoding not in {"", "identity", "gzip", "deflate"}:
                return None
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    parsed_length = int(content_length)
                    if (
                        parsed_length < 0
                        or parsed_length > max_bytes
                    ):
                        return None
                except ValueError:
                    return None
            chunks: list[bytes] = []
            total = 0
            try:
                for chunk in response.stream(amt=16_384, decode_content=True):
                    if time.monotonic() >= fetch_deadline:
                        return None
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
            except urllib3.exceptions.HTTPError:
                return None
            if media_type == "application/pdf":
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(BytesIO(b"".join(chunks)), strict=False)
                    if reader.is_encrypted:
                        return None
                    passages = []
                    for page in reader.pages[:config.DOG_WEB_PDF_VERIFY_MAX_PAGES]:
                        if time.monotonic() >= fetch_deadline:
                            break
                        passages.append((page.extract_text() or "")[:20000])
                        if sum(map(len, passages)) >= config.DOG_WEB_PDF_VERIFY_MAX_CHARS:
                            break
                    text = "\n".join(passages)[:config.DOG_WEB_PDF_VERIFY_MAX_CHARS]
                    return PublicPageText(text, source_hostname=hostname) if text.strip() else None
                except Exception as exc:
                    logger.info("Public PDF text unavailable: %s", type(exc).__name__)
                    return None
            charset_match = re.search(r"charset=([^;\s]+)", content_type)
            encoding = (
                charset_match.group(1).strip('"\'').casefold()
                if charset_match
                else "utf-8"
            )
            if encoding not in {"utf-8", "utf8", "iso-8859-1", "windows-1252"}:
                return None
            markup = b"".join(chunks).decode(encoding, errors="replace")
            soup = BeautifulSoup(markup, "html.parser")
            identity_parts: list[str] = []
            identity_blocks: list[str] = []
            if soup.title:
                title_text = soup.title.get_text(" ", strip=True)
                identity_parts.append(title_text)
                identity_blocks.append(title_text)
            for meta in soup.find_all("meta"):
                marker = str(meta.get("property") or meta.get("name") or "").casefold()
                if marker in {"og:site_name", "og:title", "application-name"}:
                    meta_text = str(meta.get("content") or "").strip()
                    identity_parts.append(meta_text)
                    identity_blocks.append(meta_text)
            for heading in soup.find_all("h1", limit=3):
                heading_text = heading.get_text(" ", strip=True)
                identity_parts.append(heading_text)
                identity_blocks.append(heading_text)
            page_links: list[str] = []
            seen_links: set[str] = set()
            for anchor in soup.find_all("a", href=True):
                linked_url, _ = urldefrag(
                    urljoin(current_url, str(anchor.get("href") or "").strip())
                )
                linked_key = _source_url_key(linked_url)
                if (
                    linked_key
                    and linked_key not in seen_links
                    and _same_website(original_url, linked_url)
                ):
                    seen_links.add(linked_key)
                    page_links.append(linked_url)
                if len(page_links) >= 200:
                    break
            for element in soup(
                ["script", "style", "noscript", "svg", "iframe", "form", "template"]
            ):
                element.decompose()
            block_tags = ("address", "p", "li", "tr", "dd", "dt", "div")
            blocks: list[str] = []
            seen_blocks: set[str] = set()
            for element in soup.find_all(block_tags):
                if element.name == "div" and element.find(block_tags):
                    continue
                block = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
                normalized_block = _normalize_page_evidence(block)
                if len(block) >= 4:
                    for bounded_block in _bounded_page_blocks(block):
                        normalized_block = _normalize_page_evidence(bounded_block)
                        if normalized_block and normalized_block not in seen_blocks:
                            seen_blocks.add(normalized_block)
                            blocks.append(bounded_block)
                if len(blocks) >= 500:
                    break
            return PublicPageText(
                soup.get_text(" ", strip=True)[:100_000],
                identity_text=" ".join(part for part in identity_parts if part)[:2_000],
                identity_blocks=identity_blocks,
                blocks=blocks,
                source_hostname=hostname,
                is_html=media_type != "text/plain",
                links=page_links,
            )
        finally:
            response.close()
            pool.close()
    return None


def _is_public_https_url(url: str) -> bool:
    return bool(_public_https_addresses(url))


def _public_https_addresses(
    url: str,
    *,
    deadline: float | None = None,
) -> list[str]:
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or "\\" in url
        or re.search(r"[\x00-\x20\x7f]", url)
    ):
        return []
    parsed = _parse_http_url(url)
    if parsed is None:
        return []
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        return []
    try:
        hostname = (parsed.hostname or "").encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        return []
    if not hostname or "." not in hostname or hostname == "localhost":
        return []
    try:
        ipaddress.ip_address(hostname)
        return []
    except ValueError:
        pass
    remaining = 3.0 if deadline is None else min(3.0, deadline - time.monotonic())
    if remaining <= 0 or not DNS_RESOLUTION_SEMAPHORE.acquire(timeout=0.25):
        return []
    resolved: queue.Queue[object] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            resolved.put(
                socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM),
                block=False,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            resolved.put(exc, block=False)
        finally:
            DNS_RESOLUTION_SEMAPHORE.release()

    threading.Thread(
        target=resolve,
        name="ngo-dns-resolver",
        daemon=True,
    ).start()
    try:
        records = resolved.get(timeout=max(0.01, remaining))
    except queue.Empty:
        logger.info("Official NGO DNS resolution timed out host=%s", hostname)
        return []
    if isinstance(records, BaseException):
        return []
    addresses = {record[4][0] for record in records}
    if not addresses:
        return []
    try:
        for address in addresses:
            parsed_address = ipaddress.ip_address(address)
            if not parsed_address.is_global:
                return []
            if isinstance(parsed_address, ipaddress.IPv6Address) and (
                parsed_address.ipv4_mapped is not None
                or parsed_address.sixtofour is not None
                or parsed_address.teredo is not None
                or parsed_address in ipaddress.ip_network("64:ff9b::/96")
            ):
                return []
    except ValueError:
        return []
    return sorted(addresses)[:4]


def _organization_name_appears_on_page(name: str, page_text: str) -> bool:
    identity_text = getattr(page_text, "identity_text", "")
    identity_blocks = tuple(getattr(page_text, "identity_blocks", ()) or ())
    if not identity_blocks:
        identity_blocks = (identity_text or str(page_text),)
    pages = tuple(
        _normalize_page_evidence(block)
        for block in identity_blocks
        if str(block).strip()
    )
    for variant in _organization_name_variants(name):
        normalized = _normalize_page_evidence(variant)
        tokens = normalized.split()
        if (
            normalized
            and (len(tokens) >= 2 or len(normalized) >= 6)
            and any(normalized in page for page in pages)
        ):
            return True
        compact = re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)
        if 6 <= len(compact) <= 60 and any(
            compact in re.sub(r"[^\w]+", "", page, flags=re.UNICODE)
            for page in pages
        ):
            return True
    raw_tokens = re.findall(r"[^\W_]+", str(name or ""), flags=re.UNICODE)
    compact_name = "".join(token.casefold() for token in raw_tokens)
    compact_host = re.sub(
        r"[^a-z0-9]",
        "",
        str(getattr(page_text, "source_hostname", "")).casefold(),
    )
    if (
        2 <= len(raw_tokens) <= 3
        and 6 <= len(compact_name) <= 30
        and any(2 <= len(token) <= 6 and token.isupper() for token in raw_tokens)
        and compact_name in compact_host
    ):
        return True
    return False


def _organization_type_evidence_is_valid(evidence: str, name: str) -> bool:
    if NEGATED_OR_COMMERCIAL_TYPE_RE.search(evidence):
        return False
    if STRONG_NON_GOVERNMENTAL_EVIDENCE_RE.search(evidence):
        return True
    normalized_evidence = _normalize_page_evidence(evidence)
    for variant in _organization_name_variants(name):
        normalized_name = _normalize_page_evidence(variant)
        if not normalized_name:
            continue
        name_tokens = normalized_name.split()
        distinctive_name = bool(
            len(name_tokens) >= 2
            and len("".join(name_tokens)) >= 8
            and any(
                len(token) >= 3
                and token not in GENERIC_ORGANIZATION_NAME_TOKENS
                for token in name_tokens
            )
        )
        name_pattern = re.escape(normalized_name).replace(r"\ ", r"\s+")
        # Some Indian charities publish their registered legal name (for
        # example, "<distinctive public name> Trust") in the official footer
        # without separately saying "registered trust". On an already
        # identity-verified official page, that exact legal-name construction
        # is sufficient type evidence. Generic names remain rejected.
        if distinctive_name and (
            (
                name_tokens[-1] in {"trust", "society"}
                and re.search(rf"\b{name_pattern}\b", normalized_evidence)
            )
            or re.search(
                rf"\b{name_pattern}\b\s+(?:trust|society)\b",
                normalized_evidence,
            )
        ):
            return True
        if not NON_GOVERNMENTAL_EVIDENCE_RE.search(evidence):
            continue
        if re.search(
            rf"(?:\b{name_pattern}\b(?:\s+\w+){{0,4}}\s+\bngo\b)|"
            rf"(?:\bngo\b(?:\s+\w+){{0,4}}\s+\b{name_pattern}\b)",
            normalized_evidence,
        ):
            return True
    return False


def _bounded_page_blocks(text: str) -> list[str]:
    """Split long DOM blocks with enough overlap to retain any valid excerpt."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return []
    if len(value) <= 800:
        return [value]
    blocks: list[str] = []
    start = 0
    while start < len(value):
        end = min(len(value), start + 800)
        if end < len(value):
            word_boundary = value.rfind(" ", start + 500, end)
            if word_boundary > start:
                end = word_boundary
        block = value[start:end].strip()
        if block:
            blocks.append(block)
        if end >= len(value):
            break
        next_start = max(start + 1, end - 300)
        next_boundary = value.find(" ", next_start, min(end, next_start + 80))
        start = next_boundary + 1 if next_boundary >= 0 else next_start
    return blocks


def _evidence_excerpt_appears_on_page(excerpt: str, page_text: str) -> bool:
    normalized_excerpt = _normalize_page_evidence(excerpt)
    if not _evidence_excerpt_shape_is_valid(excerpt):
        return False
    return any(
        normalized_excerpt in _normalize_page_evidence(block)
        for block in _page_evidence_blocks(page_text)
    )


def _evidence_excerpt_shape_is_valid(excerpt: str) -> bool:
    token_count = len(_normalize_page_evidence(excerpt).split())
    return bool(
        5 <= token_count <= 40
        and len(excerpt) <= 240
        and "..." not in excerpt
        and "…" not in excerpt
    )


def _contains_normalized_phrase(text: str, phrase: str) -> bool:
    normalized_text = f" {_normalize_page_evidence(text)} "
    normalized_phrase = _normalize_page_evidence(phrase)
    return bool(normalized_phrase and f" {normalized_phrase} " in normalized_text)


def _published_detail_appears_on_page(
    detail: str,
    page_text: str,
    detail_field: str,
) -> bool:
    if detail_field == "phone":
        detail_digits = re.sub(r"\D", "", detail)
        if len(detail_digits) < 7:
            return False
        for candidate in re.findall(
            r"(?<!\d)(?:\+?\d[\d\s().\-–—]{5,}\d)(?!\d)",
            page_text,
        ):
            candidate_digits = re.sub(r"\D", "", candidate)
            if (
                detail_digits == candidate_digits
                or (
                    len(detail_digits) >= 10
                    and len(candidate_digits) >= 10
                    and detail_digits[-10:] == candidate_digits[-10:]
                )
            ):
                return True
        return False
    normalized_detail = _normalize_page_evidence(detail)
    return bool(
        len(normalized_detail.split()) >= 2
        and normalized_detail in _normalize_page_evidence(page_text)
    )


def _published_detail_matches_city_block(
    detail: str,
    page_text: str,
    detail_field: str,
    required_city: str,
) -> bool:
    """Bind optional contact data to the requested branch, not the whole page."""
    blocks = getattr(page_text, "blocks", ())
    if not blocks or not required_city:
        return False
    return any(
        any(
            _has_standalone_location_occurrence(block, city_name)
            for city_name in _service_city_names(required_city)
        )
        and _published_detail_appears_on_page(detail, block, detail_field)
        for block in blocks
    )


def _fill_missing_phone_from_source(
    option: dict[str, str],
    *,
    required_city: str,
    required_region: str,
    page_content_cache: dict[str, str | None] | None,
    deadline: float | None,
) -> None:
    if option.get("phone") or not _is_safe_http_url(option.get("official_url", "")):
        return
    cache = page_content_cache if page_content_cache is not None else {}
    page_text = _cached_public_page_text(
        option["official_url"],
        cache,
        deadline=deadline,
    )
    if not page_text:
        return
    if not _organization_name_appears_anywhere(option["name"], page_text):
        return
    city_supported = bool(required_city) and any(
        _has_standalone_location_occurrence(page_text, city_name)
        for city_name in _service_city_names(required_city)
    )
    region_supported = bool(required_region) and _has_standalone_location_occurrence(
        page_text,
        required_region,
    )
    if required_city and not city_supported and not region_supported:
        return
    if required_city and not city_supported and region_supported:
        option["service_area"] = (
            f"{required_region} network (confirm {required_city} coverage)"
        )

    pages: list[tuple[str, str]] = []
    linked_contacts = sorted(
        {
            linked_url
            for linked_url in getattr(page_text, "links", ())
            if _same_website(option["official_url"], linked_url)
            and re.search(
                r"(?:^|[/_-])(?:contact|phone|helpline|reach)(?:[/_.-]|$)",
                (_parse_http_url(linked_url).path if _parse_http_url(linked_url) else ""),
                re.IGNORECASE,
            )
        },
        key=lambda url: _candidate_page_priority(url, required_city),
    )
    for linked_url in linked_contacts[:1]:
        linked_page = _cached_public_page_text(
            linked_url,
            cache,
            deadline=deadline,
        )
        if linked_page and _organization_name_appears_anywhere(
            option["name"],
            linked_page,
        ):
            pages.append((linked_url, linked_page))
    # Prefer a dedicated official contact page over a generic number that may
    # appear beside fundraising or administrative text on the home page.
    pages.append((option["official_url"], page_text))

    for source_url, source_page in pages:
        phones = _extract_phone_numbers_from_page(source_page)
        if not phones:
            continue
        option["phone"] = ", ".join(phones[:2])
        option["phone_source_url"] = source_url
        return


def _organization_name_appears_anywhere(name: str, page_text: str) -> bool:
    """Allow a generic contact-page title when its visible body names the NGO."""
    if _organization_name_appears_on_page(name, page_text):
        return True
    normalized_page = _normalize_page_evidence(str(page_text))
    return any(
        (normalized := _normalize_page_evidence(variant))
        and (len(normalized.split()) >= 2 or len(normalized) >= 6)
        and normalized in normalized_page
        for variant in _organization_name_variants(name)
    )


def _organization_name_matches_hostname(name: str, url: str) -> bool:
    """Allow an official root whose domain is exactly an NGO name or acronym."""
    parsed = _parse_http_url(url)
    if parsed is None:
        return False
    labels = {
        re.sub(r"[^a-z0-9]", "", label.casefold())
        for label in (parsed.hostname or "").split(".")
        if label and label.casefold() not in {"www", "org", "in", "com", "net"}
    }
    compact_variants = {
        re.sub(r"[^a-z0-9]", "", variant.casefold())
        for variant in _organization_name_variants(name)
    }
    name_tokens = _canonical_organization_name(name).split()
    if name_tokens and all(
        token in GENERIC_ORGANIZATION_NAME_TOKENS for token in name_tokens
    ):
        return False
    return any(
        len(compact) >= 3 and compact in labels
        for compact in compact_variants
    )


def _first_party_site_identity_is_valid(
    name: str,
    official_url: str,
    *,
    page_content_cache: dict[str, str | None],
    deadline: float | None,
) -> bool:
    """Reject directories whose listing title merely repeats an NGO name."""
    parsed = _parse_http_url(official_url)
    if parsed is None:
        return False
    root_url = _website_root_url(official_url)
    if _organization_name_matches_hostname(name, root_url):
        return True
    root_page = _cached_public_page_text(
        root_url,
        page_content_cache,
        deadline=deadline,
    )
    root_identity = " ".join(
        str(part)
        for part in (
            *tuple(getattr(root_page, "identity_blocks", ()) or ()),
            getattr(root_page, "identity_text", "") if root_page else "",
        )
        if part
    )
    if root_page and re.search(
        r"\b(?:business|charity|ngo|organisation|organization|service)?\s*"
        r"(?:directory|listings?|registry|search\s+results?)\b",
        root_identity,
        re.IGNORECASE,
    ):
        return False
    if root_page and (
        _organization_name_appears_on_page(name, root_page)
        or _organization_name_matches_hostname(name, root_url)
    ):
        return True

    hostname = _normalise_hostname((parsed.hostname or "").lower())
    hosted_site = hostname.endswith(".wixsite.com") or (
        hostname == "sites.google.com"
        and re.match(r"^/(?:view|site)/", parsed.path or "", re.IGNORECASE)
    )
    if not hosted_site:
        return False
    official_page = _cached_public_page_text(
        official_url,
        page_content_cache,
        deadline=deadline,
    )
    return bool(
        official_page and _organization_name_appears_on_page(name, official_page)
    )


def _extract_phone_numbers_from_page(page_text: str) -> list[str]:
    """Extract visible Indian phone-like numbers from source text."""
    blocks = tuple(getattr(page_text, "blocks", ()) or ()) or (str(page_text),)
    service_indexes = [
        index
        for index, block in enumerate(blocks)
        if re.search(
            r"\b(?:animal\s+care|animal\s+rescue|dog\s+rescue|rescue\s+helpline|"
            r"emergency|veterinary|vet\s+care|clinic)\b",
            block,
            re.I,
        )
    ]
    contact_indexes = [
        index
        for index, block in enumerate(blocks)
        if re.search(r"\b(?:phone|mobile|call|helpline|whatsapp|contact)\b", block, re.I)
    ]

    def nearby_indexes(anchors: list[int], radius: int) -> list[int]:
        indexes: list[int] = []
        for anchor in anchors:
            for index in range(
                max(0, anchor - radius),
                min(len(blocks), anchor + radius + 1),
            ):
                if index not in indexes:
                    indexes.append(index)
        return indexes

    def extract(candidate_blocks: Iterable[str]) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        for block in candidate_blocks:
            for match in re.finditer(
                r"(?<!\d)(?:\+?\d[\d\s().\-–—]{8,}\d)(?!\d)",
                block,
            ):
                raw = re.sub(r"\s+", " ", match.group(0)).strip(" .,:;()[]")
                digits = re.sub(r"\D", "", raw)
                if not 10 <= len(digits) <= 14:
                    continue
                if len(digits) > 10 and not (
                    digits.startswith("91")
                    or digits.startswith("0")
                    or (len(digits) == 11 and digits.startswith("1800"))
                ):
                    continue
                key = digits[-10:]
                if not _phone_digits_are_plausible(key):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(raw)
                if len(candidates) >= 3:
                    return candidates
        return candidates

    service_phones = extract(blocks[index] for index in service_indexes)
    if service_phones:
        return service_phones
    service_phones = extract(
        blocks[index] for index in nearby_indexes(service_indexes, 1)
    )
    if service_phones:
        return service_phones

    preferred_indexes: list[int] = []
    for contact_index in contact_indexes:
        for index in range(
            max(0, contact_index - 1),
            min(len(blocks), contact_index + 3),
        ):
            if index not in preferred_indexes:
                preferred_indexes.append(index)
    candidate_blocks = (
        [blocks[index] for index in preferred_indexes]
        if preferred_indexes
        else list(blocks)
    )
    return extract(candidate_blocks)


def _phone_digits_are_plausible(value: str) -> bool:
    digits = re.sub(r"\D", "", str(value or ""))[-10:]
    if len(digits) != 10:
        return False
    if digits in {
        "0000000000",
        "0123456789",
        "1234567890",
        "0987654321",
        "9876543210",
        "1231231234",
    }:
        return False
    if len(set(digits)) <= 2 or re.search(r"(\d)\1{6,}", digits):
        return False
    return True


def _validated_phone_display(value: str) -> str:
    """Return only plausible phone tokens, dropping mixed-in placeholders."""
    return ", ".join(_extract_phone_numbers_from_page(str(value or ""))[:2])


def _normalize_page_evidence(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"[^\w]+", " ", normalized.casefold(), flags=re.UNICODE),
    ).strip()


def _same_website(left: str, right: str) -> bool:
    left_key = _website_identity_key(left)
    right_key = _website_identity_key(right)
    return bool(left_key and left_key == right_key)


def _website_identity_key(url: str) -> str:
    """Identify one independent site, including tenants on shared hosts."""
    parsed = _parse_http_url(url)
    if parsed is None:
        return ""
    hostname = _normalise_hostname((parsed.hostname or "").lower())
    path_parts = [part.casefold() for part in (parsed.path or "").split("/") if part]
    if hostname == "sites.google.com":
        if len(path_parts) >= 2 and path_parts[0] in {"view", "site"}:
            return f"{hostname}/{path_parts[0]}/{path_parts[1]}"
        return ""
    if hostname.endswith(".wixsite.com"):
        # A Wix account can host unrelated sites under different first path
        # segments. Evidence and deduplication must not cross those tenants.
        return f"{hostname}/{path_parts[0]}" if path_parts else ""
    return hostname


def _normalise_hostname(hostname: str) -> str:
    value = str(hostname or "").lower().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def _is_government_or_directory_domain(hostname: str) -> bool:
    if hostname == "sites.google.com":
        return False
    blocked_parts = (
        ".gov.in",
        ".nic.in",
        ".gov",
        "justdial.",
        "sulekha.",
        "google.",
        "facebook.",
        "instagram.",
        "linkedin.",
        "medium.",
        "substack.",
        "blogspot.",
        "linktr.ee",
        "youtube.",
        "twitter.",
        "tiktok.",
        "wikipedia.",
    )
    return (
        hostname in {"gov.in", "nic.in"}
        or hostname.endswith((".gov.in", ".nic.in", ".gov"))
        or any(part in hostname for part in blocked_parts)
        or "spca" in hostname
    )


def _verified_service_geography_matches(
    service_city: str,
    service_region: str,
    required_city: str,
    required_region: str,
) -> bool:
    if _normalise_cache_component(service_city) != _normalise_cache_component(required_city):
        return False
    if required_region and (
        _normalise_cache_component(service_region)
        != _normalise_cache_component(required_region)
    ):
        return False
    return True


def _service_evidence_matches_geography(
    evidence: str,
    required_city: str,
    required_region: str,
) -> bool:
    """Require affirmative exact-city proof without trusting a conflicting state."""
    if (
        not _evidence_excerpt_shape_is_valid(evidence)
        or not required_city
        or NEGATED_SERVICE_AREA_RE.search(evidence)
    ):
        return False
    clauses = re.split(r"(?<=[.!?;])\s+|[\r\n]+", evidence)
    for clause in clauses:
        city_spans = _service_city_spans(clause, required_city)
        normalized_clause = _normalize_page_evidence(clause)
        if (
            not city_spans
            or not _city_spans_match_nearest_region(
                clause,
                city_spans,
                required_region,
            )
            or INDIRECT_SERVICE_AREA_RE.search(clause)
            or (
                SERVICE_SCOPE_ONLY_RE.search(normalized_clause)
                and not _only_scopes_required_location(
                    normalized_clause,
                    required_city,
                    required_region,
                )
            )
        ):
            continue
        if SERVICE_SITE_RE.search(clause):
            return True
        if re.match(r"^\s*located\b", clause, re.IGNORECASE):
            return True
        if not ANIMAL_SERVICE_CONTEXT_RE.search(clause):
            continue
        for operation in AFFIRMATIVE_SERVICE_AREA_RE.finditer(clause):
            if any(
                min(
                    abs(operation.start() - city_end),
                    abs(city_start - operation.end()),
                )
                <= 180
                for city_start, city_end in city_spans
            ):
                return True
    return False


def _city_spans_match_nearest_region(
    clause: str,
    city_spans: list[tuple[int, int]],
    required_region: str,
) -> bool:
    """Bind an explicit state/UT to its nearest city instead of the whole clause."""
    if not required_region:
        return True

    region_mentions: list[tuple[int, int, str]] = []
    region_names = set(query_router.INDIA_STATES) | set(INDIA_REGION_ALIASES)
    for region_name in sorted(
        region_names,
        key=lambda value: len(_normalize_page_evidence(value)),
        reverse=True,
    ):
        for start, end in _normalized_phrase_spans(clause, region_name):
            if any(start < existing_end and end > existing_start for existing_start, existing_end, _ in region_mentions):
                continue
            region_mentions.append((start, end, region_name))
    if not region_mentions:
        return True

    expected = _canonical_india_region_name(required_region)
    for city_start, city_end in city_spans:
        distances = [
            min(
                abs(mention[0] - city_end),
                abs(city_start - mention[1]),
            )
            for mention in region_mentions
        ]
        nearest_distance = min(distances)
        nearest_regions = {
            _canonical_india_region_name(mention[2])
            for mention, distance in zip(region_mentions, distances)
            if distance == nearest_distance
        }
        # Equal-distance conflicting states are ambiguous and must fail closed.
        if nearest_regions != {expected}:
            return False
    return True


def _canonical_india_region_name(region_name: str) -> str:
    normalized = _normalize_page_evidence(region_name)
    return INDIA_REGION_ALIASES.get(normalized, normalized)


def _service_city_names(required_city: str) -> tuple[str, ...]:
    normalized = _normalize_page_evidence(required_city)
    if normalized in {
        "new delhi",
        "delhi",
        "delhi ncr",
        "nct of delhi",
        "national capital territory of delhi",
    }:
        return DELHI_SERVICE_CITY_ALIASES
    if normalized in {
        _normalize_page_evidence(value)
        for value in DHARAMSHALA_SERVICE_CITY_ALIASES
    }:
        return DHARAMSHALA_SERVICE_CITY_ALIASES
    return (required_city,)


def _service_city_spans(
    clause: str,
    required_city: str,
) -> list[tuple[int, int]]:
    """Return non-overlapping exact spans, preferring the longest city alias."""
    spans: list[tuple[int, int]] = []
    for city_name in sorted(
        _service_city_names(required_city),
        key=lambda value: len(_normalize_page_evidence(value)),
        reverse=True,
    ):
        for start, end in _standalone_location_spans(clause, city_name):
            if any(
                start < existing_end and end > existing_start
                for existing_start, existing_end in spans
            ):
                continue
            spans.append((start, end))
    return sorted(spans)


def _normalized_phrase_spans(text: str, phrase: str) -> list[tuple[int, int]]:
    normalized_text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    tokens = re.findall(
        r"[^\W_]+",
        unicodedata.normalize("NFKC", str(phrase or "")).casefold(),
        flags=re.UNICODE,
    )
    if not normalized_text or not tokens:
        return []
    pattern = r"(?<!\w)" + r"[^\w]+".join(re.escape(token) for token in tokens) + r"(?!\w)"
    return [match.span() for match in re.finditer(pattern, normalized_text, flags=re.UNICODE)]


def _only_scopes_required_location(
    normalized_clause: str,
    required_city: str,
    required_region: str,
) -> bool:
    city = _normalize_page_evidence(required_city)
    region = _normalize_page_evidence(required_region)
    if not city:
        return False
    city_pattern = re.escape(city).replace(r"\ ", r"\s+")
    region_pattern = re.escape(region).replace(r"\ ", r"\s+") if region else ""
    after_only = rf"\bonly\s+(?:the\s+)?{city_pattern}\b"
    before_only = rf"\b{city_pattern}(?:\s+{region_pattern})?\s+only\b"
    return bool(
        re.search(after_only, normalized_clause)
        or re.search(before_only, normalized_clause)
    )


def _has_standalone_location_occurrence(text: str, location_name: str) -> bool:
    """Avoid treating a suffix of a larger place name as the requested city."""
    return bool(_standalone_location_spans(text, location_name))


def _standalone_location_spans(
    text: str,
    location_name: str,
) -> list[tuple[int, int]]:
    normalized_text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    location_tokens = re.findall(
        r"[^\W_]+",
        unicodedata.normalize("NFKC", str(location_name or "")).casefold(),
        flags=re.UNICODE,
    )
    if not normalized_text or not location_tokens:
        return []

    pattern = r"(?<!\w)" + r"[^\w]+".join(
        re.escape(token) for token in location_tokens
    ) + r"(?!\w)"
    allowed_preceding_tokens = {
        "across",
        "and",
        "at",
        "based",
        "centre",
        "center",
        "cover",
        "covering",
        "covers",
        "for",
        "from",
        "in",
        "located",
        "of",
        "only",
        "operate",
        "operates",
        "operating",
        "our",
        "rescue",
        "rescues",
        "serve",
        "serves",
        "serving",
        "the",
        "throughout",
        "to",
        "treat",
        "treats",
        "within",
    }
    matches: list[tuple[int, int]] = []
    for match in re.finditer(pattern, normalized_text, flags=re.UNICODE):
        prefix = normalized_text[: match.start()]
        previous = re.search(r"([^\W_]+)([\W_]*)$", prefix, flags=re.UNICODE)
        if previous is None:
            matches.append(match.span())
            continue
        separator = previous.group(2)
        if re.search(r"[,;:./()\[\]{}]", separator):
            matches.append(match.span())
            continue
        if previous.group(1) in allowed_preceding_tokens:
            matches.append(match.span())
    return matches


def _render_verified_organizations(
    organizations: list[dict[str, str]],
    *,
    language: str,
    heading: str = "**Verified animal-rescue options**",
) -> tuple[str, list[dict[str, str]]]:
    """Render public output only from the revalidated structured snapshot."""
    if heading == "**Verified animal-rescue options**" and any(
        option.get("coverage_scope") == "region" for option in organizations
    ):
        heading = "**Regional animal-rescue contacts (confirm local coverage)**"
    lines = [heading]
    resource_links: list[dict[str, str]] = []
    for index, option in enumerate(organizations, start=1):
        name = _escape_markdown_text(option["name"])
        service_area = _escape_markdown_text(option["service_area"])
        evidence = (
            "The linked source supports current, direct dog or animal rescue "
            "or treatment work."
        )
        address = (
            f" Address: {_escape_markdown_text(option['address'])}."
            if option.get("address")
            else ""
        )
        phone_value = _validated_phone_display(
            _clean_rendered_text(option.get("phone"), max_length=80)
        )
        phone = (
            f" Phone: {_escape_markdown_text(phone_value)}."
            if phone_value
            else ""
        )
        lines.append(
            f"{index}. **{name}** - {service_area}. "
            f"{evidence} "
            f"[Source/contact]({option['official_url']}).{address}{phone}"
        )
        resource_links.append(_organization_resource_link(option))

    return "\n".join(lines), resource_links


def _organization_resource_link(option: dict[str, str]) -> dict[str, str]:
    link = {
        "label": option["name"],
        "url": option["official_url"],
    }
    for field in ("phone", "address", "opening_hours"):
        value = _clean_rendered_text(option.get(field), max_length=300)
        if field == "phone":
            value = _validated_phone_display(value)
        if value:
            link[field] = value
    return link


def _safe_immediate_guidance(language: str) -> str:
    if language == "hi":
        return (
            "कुत्ते से सुरक्षित दूरी रखें और डरे हुए या घायल कुत्ते को न दौड़ाएं। "
            "मदद मांगते समय सही स्थान और फोटो साझा करें।"
        )
    return (
        "Keep a safe distance and do not chase or handle a frightened or injured dog. "
        "Share the exact location and a photo when requesting help."
    )


def _no_verified_options_response(place: str, *, language: str) -> str:
    place_label = _escape_markdown_text(place or "this city")
    if language == "hi":
        no_results = (
            f"मुझे **{place_label}** के लिए आधिकारिक स्रोत से सत्यापित कोई कुत्ता बचाव NGO "
            "नहीं मिला। इसका अर्थ यह नहीं है कि वहां कोई बचाव समूह नहीं है; केवल इतना है कि "
            "मैं अभी उसकी आधिकारिक जानकारी सत्यापित नहीं कर सका।"
        )
    else:
        no_results = (
            f"I could not verify a dog-rescue NGO for **{place_label}** from an official source. "
            "This does not prove that no rescue group exists there; it means no option passed "
            "the verification checks right now."
        )
    return no_results


def _web_search_tool(
    *,
    city: str = "",
    region: str = "",
    country_code: str = "IN",
    search_context_size: str = "low",
) -> dict[str, Any]:
    user_location = {
        "type": "approximate",
        "country": (country_code or "IN").strip().upper(),
        "timezone": "Asia/Kolkata",
    }
    if cleaned_city := _clean_prompt_value(city, max_length=100):
        user_location["city"] = cleaned_city
    if cleaned_region := _clean_prompt_value(region, max_length=100):
        user_location["region"] = cleaned_region

    return {
        "type": "web_search",
        "search_context_size": (
            search_context_size
            if search_context_size in {"low", "medium", "high"}
            else "low"
        ),
        "user_location": user_location,
    }


def _format_history(history: Iterable[dict]) -> str:
    lines = []
    for entry in list(history)[-6:]:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "user")
        content = str(entry.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content[:500]}")
    return "\n".join(lines)


def _trim_follow_up_offer(text: str) -> str:
    """Remove generic closing offers so search answers end with the useful result."""
    return re.sub(
        r"\n{2,}(?:if you (?:want|would like)|would you like|i can (?:also|next))\b.*$",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()


def _coordinates_are_in_india(lat: Any, lng: Any) -> bool:
    if not _coordinates_are_valid(lat, lng):
        return False
    return location.is_in_india(float(lat), float(lng))


def _coordinates_are_valid(lat: Any, lng: Any) -> bool:
    try:
        parsed_lat = float(lat)
        parsed_lng = float(lng)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(parsed_lat) or not math.isfinite(parsed_lng):
        return False
    return -90 <= parsed_lat <= 90 and -180 <= parsed_lng <= 180


def _ngo_cache_key(city: str, region: str, country_code: str, language: str) -> str:
    normalized_city = _normalise_cache_component(city)
    normalized_region = _normalise_cache_component(region)
    if (
        not config.DOG_WEB_SEARCH_CACHE_ENABLED
        or not normalized_city
        or not normalized_region
    ):
        return ""

    parts = (
        NGO_CACHE_VERSION,
        _normalise_cache_component(country_code),
        normalized_region,
        normalized_city,
        "hi" if language == "hi" else "en",
        str(config.DOG_WEB_SEARCH_MAX_RESULTS),
    )
    return "ngo:" + ":".join(parts)


def _legacy_cached_ngo_candidates(cache_key: str) -> list[dict[str, str]]:
    """Reuse old official URLs as discovery seeds, never as trusted answers."""
    parts = str(cache_key or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "ngo":
        return []
    suffix = parts[2]
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for version in (
        "v20",
        "v19",
        "v18",
        "v17",
        "v16",
        "v15",
        "v14",
        "v13",
        "v12",
        "v11",
    ):
        legacy_key = f"ngo:{version}:{suffix}"
        try:
            cached = db.get_ngo_search_cache(
                legacy_key,
                max_age_hours=config.DOG_WEB_SEARCH_STALE_CACHE_HOURS,
            )
        except Exception as exc:  # noqa: BLE001 - seed lookup must not block search
            logger.warning("Legacy NGO seed cache read failed: %s", type(exc).__name__)
            continue
        if not cached or not isinstance(cached.get("organizations"), list):
            continue
        for raw_option in cached["organizations"]:
            if not isinstance(raw_option, dict):
                continue
            name = _clean_rendered_text(raw_option.get("name"), max_length=160)
            url = str(raw_option.get("official_url") or "").strip()
            hostname = (urlparse(url).hostname or "").lower() if _is_safe_http_url(url) else ""
            identity = (_canonical_organization_name(name), _normalise_hostname(hostname))
            if (
                not name
                or not hostname
                or identity in seen
                or BANNED_ORGANISATION_RE.search(f"{name} {hostname}")
                or _is_government_or_directory_domain(hostname)
            ):
                continue
            seen.add(identity)
            candidates.append(
                {
                    "name": name,
                    "possible_official_url": url,
                }
            )
    return candidates


def _normalise_cache_component(value: Any) -> str:
    return re.sub(
        r"[^\w]+",
        " ",
        str(value or "").casefold(),
        flags=re.UNICODE,
    ).strip()


def _get_cached_ngo_result(
    cache_key: str,
    *,
    city: str,
    region: str,
    language: str,
    max_age_hours: int | None = None,
    allow_regional: bool = True,
) -> SearchResult | None:
    try:
        cached = db.get_ngo_search_cache(
            cache_key,
            max_age_hours=(
                config.DOG_WEB_SEARCH_CACHE_HOURS
                if max_age_hours is None
                else max_age_hours
            ),
        )
    except Exception as exc:  # noqa: BLE001 - cache failure must not block rescue lookup
        logger.warning("NGO cache read failed: %s", type(exc).__name__)
        return None
    if not cached:
        return None

    expected_city = _normalise_cache_component(city)
    expected_region = _normalise_cache_component(region)
    if (
        not expected_city
        or not expected_region
        or _normalise_cache_component(cached.get("city")) != expected_city
        or _normalise_cache_component(cached.get("region")) != expected_region
        or str(cached.get("language") or "") != ("hi" if language == "hi" else "en")
    ):
        return None

    raw_organizations = cached.get("organizations")
    if not isinstance(raw_organizations, list):
        return None

    organizations: list[dict[str, str]] = []
    for raw_organization in raw_organizations:
        organization = _validate_cached_ngo_organization(
            raw_organization,
            required_city=city,
            required_region=region,
        )
        if organization is None:
            return None
        organizations.append(organization)

    if not allow_regional and any(
        organization.get("coverage_scope") == "region"
        for organization in organizations
    ):
        return None

    result_kind = str(cached.get("result_kind") or "")
    if result_kind != "verified_options" or not organizations:
        return None

    response, links = _render_verified_organizations(
        organizations,
        language=language,
    )

    logger.info("Using cached city-level NGO result key=%s", cache_key)
    return SearchResult(
        response=response,
        resource_links=links,
        searched=True,
        cached=True,
        result_kind=result_kind,
        organizations=organizations,
        # Current-version snapshots are written only after the exact-city
        # candidate pass completes. The version bump prevents older partial
        # positives from being treated as exhaustive by this reader.
        candidate_discovery_complete=True,
    )


def _save_cached_ngo_result(
    cache_key: str,
    result: SearchResult,
    *,
    city: str,
    region: str,
    country_code: str,
    language: str,
) -> None:
    if result.result_kind != "verified_options" or not result.organizations:
        return
    if result.candidate_discovery_complete is False and not any(
        option.get("coverage_scope") == "region"
        for option in result.organizations
    ):
        logger.info(
            "Skipping NGO cache save reason=incomplete_candidate_discovery key=%s",
            cache_key,
        )
        return
    organizations: list[dict[str, str]] = []
    for raw_organization in result.organizations:
        organization = _validate_cached_ngo_organization(
            raw_organization,
            required_city=city,
            required_region=region,
        )
        if organization is None:
            logger.info(
                "Skipping NGO cache save because a result failed cache validation key=%s",
                cache_key,
            )
            return
        organizations.append(organization)
    if not organizations:
        return
    response, links = _render_verified_organizations(
        organizations,
        language=language,
    )
    try:
        db.save_ngo_search_cache(
            cache_key,
            city=city,
            region=region,
            country_code=country_code,
            language="hi" if language == "hi" else "en",
            result_kind=result.result_kind,
            response=response,
            resource_links=links,
            organizations=organizations,
        )
    except Exception as exc:  # noqa: BLE001 - cache failure must not alter response
        logger.warning("NGO cache write failed: %s", type(exc).__name__)


def _validate_cached_ngo_organization(
    value: Any,
    *,
    required_city: str = "",
    required_region: str = "",
) -> dict[str, str] | None:
    """Revalidate the public organization snapshot read from SQLite."""
    if not isinstance(value, dict):
        return None
    option = {
        "name": _clean_rendered_text(value.get("name"), max_length=160),
        "service_area": _clean_rendered_text(value.get("service_area"), max_length=240),
        "service_city": _clean_rendered_text(value.get("service_city"), max_length=100),
        "service_region": _clean_rendered_text(value.get("service_region"), max_length=100),
        "animal_rescue_evidence": _clean_rendered_text(
            value.get("animal_rescue_evidence"),
            max_length=400,
        ),
        "service_area_evidence": _clean_rendered_text(
            value.get("service_area_evidence"),
            max_length=400,
        ),
        "organization_type_evidence": _clean_rendered_text(
            value.get("organization_type_evidence"),
            max_length=400,
        ),
        "animal_rescue_evidence_url": str(
            value.get("animal_rescue_evidence_url") or ""
        ).strip(),
        "service_area_evidence_url": str(
            value.get("service_area_evidence_url") or ""
        ).strip(),
        "organization_type_evidence_url": str(
            value.get("organization_type_evidence_url") or ""
        ).strip(),
        "official_url": str(value.get("official_url") or "").strip(),
        "phone": _clean_rendered_text(value.get("phone"), max_length=80),
        "phone_source_url": str(value.get("phone_source_url") or "").strip(),
        "address": _clean_rendered_text(value.get("address"), max_length=300),
        "address_source_url": str(value.get("address_source_url") or "").strip(),
        "opening_hours": _clean_rendered_text(
            value.get("opening_hours"),
            max_length=160,
        ),
        "opening_hours_source_url": str(
            value.get("opening_hours_source_url") or ""
        ).strip(),
        "coverage_scope": _clean_rendered_text(
            value.get("coverage_scope"),
            max_length=20,
        ),
    }
    if (
        not option["name"]
        or not option["service_area"]
        or not option["animal_rescue_evidence"]
        or not option["service_area_evidence"]
        or not option["organization_type_evidence"]
    ):
        return None
    if NEGATED_RESCUE_EVIDENCE_RE.search(option["animal_rescue_evidence"]):
        return None
    if INACTIVE_OR_INDIRECT_RESCUE_EVIDENCE_RE.search(
        option["animal_rescue_evidence"]
    ):
        return None
    if INDIRECT_RESCUE_CONTENT_RE.search(option["animal_rescue_evidence"]):
        return None
    if not DIRECT_ANIMAL_RESCUE_EVIDENCE_RE.search(option["animal_rescue_evidence"]):
        return None
    if not _organization_type_evidence_is_valid(
            option["organization_type_evidence"],
            option["name"],
        ):
        return None
    if not _is_safe_http_url(option["official_url"]):
        return None
    hostname = (urlparse(option["official_url"]).hostname or "").lower()
    if BANNED_ORGANISATION_RE.search(f"{option['name']} {hostname}"):
        return None
    if _is_government_or_directory_domain(hostname):
        return None
    if option["coverage_scope"] not in {"", "city", "region"}:
        return None
    for source_field in (
        "animal_rescue_evidence_url",
        "service_area_evidence_url",
        "organization_type_evidence_url",
    ):
        if (
            not _is_safe_http_url(option[source_field])
            or not _same_website(option[source_field], option["official_url"])
        ):
            return None
    if option["phone"]:
        option["phone"] = _validated_phone_display(option["phone"])
        if not option["phone"]:
            option["phone_source_url"] = ""
    for detail_field, source_field in (
        ("phone", "phone_source_url"),
        ("address", "address_source_url"),
        ("opening_hours", "opening_hours_source_url"),
    ):
        if bool(option[detail_field]) != bool(option[source_field]):
            return None
        if option[detail_field] and (
            not _is_safe_http_url(option[source_field])
            or not _same_website(option[source_field], option["official_url"])
        ):
            return None
    if required_city and option["coverage_scope"] == "region":
        if (
            not option["phone"]
            or not _extract_phone_numbers_from_page(option["phone"])
            or not _is_safe_http_url(option["phone_source_url"])
            or not _same_website(
                option["phone_source_url"],
                option["official_url"],
            )
            or not _regional_service_evidence_matches(
                option["service_area_evidence"],
                required_region,
            )
            or not _verified_service_geography_matches(
                option["service_city"],
                option["service_region"],
                required_city,
                required_region,
            )
            or not _source_backed_region_geography_matches(
                option,
                required_region,
            )
        ):
            return None
        option["service_area"] = (
            f"{required_region} network (confirm {required_city} coverage)"
        )
    elif required_city:
        if not _verified_service_geography_matches(
            option["service_city"],
            option["service_region"],
            required_city,
            required_region,
        ):
            return None
        if NEGATED_SERVICE_AREA_RE.search(option["service_area"]):
            return None
        if not _source_backed_service_geography_matches(
            option,
            required_city,
            required_region,
        ):
            return None
        option["service_area"] = ", ".join(
            part for part in (required_city, required_region) if part
        )
    return option


def _clean_prompt_value(value: Any, *, max_length: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:max_length]


def _clean_rendered_text(value: Any, *, max_length: int) -> str:
    return _clean_prompt_value(value, max_length=max_length)


def _escape_markdown_text(value: str) -> str:
    escaped = str(value or "").replace("\\", "\\\\")
    return re.sub(r"([`*_{}\[\]()<>#+.!|~-])", r"\\\1", escaped)


def _model_dump(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(value, dict):
        return value
    return {}


def _ordered_source_urls(payload: dict) -> list[str]:
    consulted_pages: list[str] = []
    discovered_sources: list[str] = []
    seen: set[str] = set()

    def add(url: Any, destination: list[str]) -> None:
        if citation := _normalise_citation({"url": url}):
            if (key := _source_url_key(citation["url"])) and key not in seen:
                seen.add(key)
                destination.append(key)

    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        if item.get("status", "completed") != "completed":
            continue
        action = item.get("action") or {}
        # Opening a page or finding text on it is also research evidence.
        # These actions expose their URL directly, without search.sources.
        # Keep it available to the independent fetch/review step; merely
        # consulting it still does not make it a supporting answer citation.
        if action.get("type") in {"open_page", "find_in_page"}:
            add(action.get("url"), consulted_pages)
        for source in action.get("sources") or []:
            citation = _normalise_citation(source)
            if citation:
                add(citation["url"], discovered_sources)
    return consulted_pages + discovered_sources


def _extract_source_urls(payload: dict) -> set[str]:
    """Compatibility helper for membership checks and existing callers."""
    return set(_ordered_source_urls(payload))


def _url_is_in_sources(url: str, source_urls: set[str]) -> bool:
    key = _source_url_key(url)
    return bool(key and key in source_urls)


def _source_url_key(url: str) -> str:
    parsed = _parse_http_url(url)
    if parsed is None:
        return ""
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{hostname}{port}{path}{query}"


def _website_root_url(url: str) -> str:
    parsed = _parse_http_url(url)
    if parsed is None:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    hostname = _normalise_hostname((parsed.hostname or "").lower())
    path_parts = [part for part in (parsed.path or "").split("/") if part]
    if (
        hostname == "sites.google.com"
        and len(path_parts) >= 2
        and path_parts[0].casefold() in {"view", "site"}
    ):
        return (
            f"{parsed.scheme.lower()}://{parsed.hostname}{port}/"
            f"{path_parts[0]}/{path_parts[1]}/"
        )
    if hostname.endswith(".wixsite.com") and path_parts:
        return f"{parsed.scheme.lower()}://{parsed.hostname}{port}/{path_parts[0]}/"
    return f"{parsed.scheme.lower()}://{parsed.hostname}{port}/"


def _extract_citations(payload: dict) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    fallback_sources: list[dict[str, Any]] = []

    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                for annotation in content.get("annotations") or []:
                    citation = _normalise_citation(annotation)
                    if citation:
                        citations.append(citation)
        elif item.get("type") == "web_search_call":
            action = item.get("action") or {}
            for source in action.get("sources") or []:
                citation = _normalise_citation(source)
                if citation:
                    fallback_sources.append(citation)

    unique = _deduplicate_citations(citations)
    if unique:
        return unique[: config.DOG_WEB_SEARCH_MAX_RESULTS]
    return _deduplicate_citations(fallback_sources)[: config.DOG_WEB_SEARCH_MAX_RESULTS]


def _normalise_citation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    nested = value.get("url_citation")
    if isinstance(nested, dict):
        value = nested

    url = str(value.get("url") or "").strip()
    if not _is_safe_http_url(url):
        return None
    return {
        "url": url,
        "title": str(value.get("title") or urlparse(url).netloc or "Source").strip(),
        "start_index": value.get("start_index"),
        "end_index": value.get("end_index"),
    }


def _deduplicate_citations(citations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = []
    seen = set()
    for citation in citations:
        url = citation["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(citation)
    return unique


def _add_clickable_citations(text: str, citations: list[dict[str, Any]]) -> str:
    if not text or not citations:
        return text

    insertions = []
    source_fallbacks = []
    for number, citation in enumerate(citations, start=1):
        if citation["url"] in text:
            continue
        end_index = citation.get("end_index")
        if isinstance(end_index, int) and 0 <= end_index <= len(text):
            insertions.append((end_index, f" [{number}]({citation['url']})"))
        else:
            source_fallbacks.append((number, citation))

    if insertions:
        for index, marker in sorted(insertions, reverse=True):
            text = text[:index] + marker + text[index:]

    if source_fallbacks:
        source_lines = [
            f"- [{number}. {citation['title']}]({citation['url']})"
            for number, citation in source_fallbacks
        ]
        text = f"{text}\n\n**Sources:**\n" + "\n".join(source_lines)
    return text


def _resource_links(citations: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "label": f"Source {number}: {citation['title'][:80]}",
            "url": citation["url"],
        }
        for number, citation in enumerate(citations, start=1)
    ]


def _is_safe_http_url(url: str) -> bool:
    return _parse_http_url(url) is not None


def _parse_http_url(url: str):
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or "\\" in url
        or re.search(r"[\x00-\x20\x7f]", url)
    ):
        return None
    try:
        parsed = urlparse(url)
        parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return parsed
