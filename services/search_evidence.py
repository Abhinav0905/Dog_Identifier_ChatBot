"""Check researched contact claims against fetched pages, without filtering providers."""

from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from services import agent_trace

import json
import logging
import re
import time
import threading
from contextvars import copy_context
import unicodedata
from typing import Iterable
from urllib.parse import urldefrag, urlparse, parse_qsl, urlencode, urlunparse

import config
from services import query_router, web_search, web_operations, text_contract
from services.prompts import PromptCatalog


logger = logging.getLogger(__name__)
FETCH_BUDGET_SECONDS = 12.0
MAX_SOURCES = 12
FETCH_WORKERS = 4
_FETCH_EXECUTOR = ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="contact-evidence")
_FETCH_CAPACITY = threading.BoundedSemaphore(MAX_SOURCES)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d \t().\-–—]{4,}\d(?!\w)")
_CONTACT_REQUEST = re.compile(
    r"\b(?:phone|telephone|helpline)\b|\b(?:mobile|contact)\s+(?:number|details)\b|"
    r"\b(?:who|whom)\s+(?:can|should)\s+I\s+(?:call|contact)\b|"
    r"\b(?:its?|their|only|just|give|provide)\b[^.!?]{0,40}\bnumber\b",
    re.IGNORECASE,
)


@dataclass
class ContactAnswerReview:
    answer: str
    links: list[dict[str, str]] = field(default_factory=list)
    status: str = "unavailable"
    request_satisfied: bool | None = None
    validated_facts: list[dict[str, str]] = field(default_factory=list)
    evidence_issue: str = ""


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "request_satisfied": {"type": "boolean"},
        "provider_claims": {
            "type": "array", "items": {
                "type": "object", "properties": {
                    "institution": {"type": "string"},
                    "location": {"type": "string"},
                    "service_type": {"type": "string", "enum": ["clinical_treatment", "rescue_pickup", "no_rescue_pickup", "identity_only"]},
                    "source_url": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["institution", "location", "service_type", "source_url", "evidence_quote"],
                "additionalProperties": False,
            },
        },
        "contact_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "institution": {"type": "string"},
                    "phone": {"type": "string"},
                    "source_url": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "matches_requested_entity": {"type": "boolean"},
                },
                "required": ["institution", "phone", "source_url", "evidence_quote", "matches_requested_entity"],
                "additionalProperties": False,
            },
        },
        "cited_source_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "request_satisfied", "provider_claims", "contact_claims", "cited_source_urls"],
    "additionalProperties": False,
}


@agent_trace.stage('evidence.review')
def review_contact_answer(
    message: str,
    history: Iterable[dict],
    contextual_request: str,
    draft: str,
    source_urls: Iterable[str],
    language: str = "en",
    *,
    requested_institution: str | None = None,
    output_format: str = "text",
    phone_only: bool | None = None,
    deadline: float | None = None,
    requested_place: str = "",
    requested_city: str = "",
    requested_location_text: str = "",
    page_cache: dict[str, str | None] | None = None,
) -> ContactAnswerReview:
    """Independently review phone answers; discovery remains unrestricted."""
    history = list(history)
    address_only = output_format == "address_only" or _address_only_request(message)
    full_address_required = bool(re.search(r"\b(?:full|complete|postal|street)\s+(?:postal\s+)?address\b|पूरा\s*पता", message, re.I))
    contact_or_address = bool(re.search(r"\baddress\s+or\s+(?:(?:a|an|the|published)\s+)*(?:phone|contact)|\b(?:phone|contact)(?:\s+number)?\s+or\s+(?:(?:a|an|the|published)\s+)*address\b", message, re.I))
    phone_only = _number_only_request(message) if phone_only is None else phone_only
    target = _requested_target(message, history, requested_institution)
    needs_contact = bool(phone_only or _CONTACT_REQUEST.search(f"{message}\n{contextual_request}"))
    optional_contact = not phone_only and bool(re.search(
        r"\b(?:phone|contact)(?:\s+number)?\s+(?:if\s+(?:available|published|verified)|when\s+available)\b|"
        r"\bif\s+available[^.!?]{0,30}\b(?:phone|contact)\b", message, re.I))
    needs_provider = bool(re.search(
        r"\b(?:veterinar\w*|hospitals?|clinics?|colleges?|shelters?|rescue|treatment|local\s+help)\b",
        f"{message}\n{contextual_request}", re.I,
    ))
    request_text = f"{message}\n{contextual_request}"
    clinical_discovery = not phone_only and not address_only and bool(
        re.search(r"\b(?:treat(?:ment|s|ing)?|clinical|animal.care|outpatient|opd)\b|इलाज|पशु\s*चिकित्सा", request_text, re.I)
        or (not needs_contact and needs_provider and re.search(r"\b(?:find|where|options?|help)\b", request_text, re.I))
        or (re.search(r"\b(?:dogs?|pupp(?:y|ies)|cats?|animals?)\b", request_text, re.I)
            and re.search(r"\b(?:injured|wound(?:ed)?|bleeding|sick|hurt)\b", request_text, re.I)
            and re.search(r"\b(?:where|who|take|find|help)\b", request_text, re.I))
    )
    require_small_animal = bool(re.search(r"\b(?:dogs?|pupp(?:y|ies)|canine|pets?|cats?|kittens?)\b|कुत्त|पिल्ल|बिल्ली", request_text, re.I))
    if not clinical_discovery and not needs_contact and not needs_provider and not _answer_phone_spans(draft):
        return ContactAnswerReview(answer=draft, status="not_needed")
    direct_address = None
    def fallback(reason="review_unavailable"):
        web_operations.record_event("evidence:result", "fallback", error_type=re.sub(r"\W+", "_", reason)[:79])
        if direct_address is not None and direct_address.validated_facts:
            direct_address.evidence_issue = "full_address_not_grounded"
            return direct_address
        if address_only:
            return ContactAnswerReview(
                answer="मैं अनुरोधित संस्था का पता सुनिश्चित नहीं कर सका।" if language == "hi" else "I couldn't confirm the requested institution's address from accessible sources.",
                request_satisfied=False, evidence_issue=reason,
            )
        result = _unavailable(language, draft="" if clinical_discovery else draft, phone_only=phone_only, phone_requested=needs_contact)
        result.evidence_issue = reason
        return result
    if deadline is not None and time.monotonic() >= deadline:
        return fallback("review_deadline_exhausted")
    if query_router.client is None:
        logger.info("Contact evidence review status=unavailable reason=model_unavailable fetched_sources=0")
        return fallback("review_model_unavailable")
    fetch_deadline = deadline
    if deadline is not None:
        # Keep time for the evidence model rather than spending the entire
        # remaining turn on unreadable source pages.
        fetch_deadline = min(deadline, time.monotonic() + max(0.01, (deadline - time.monotonic()) * 0.45))
    pages = _fetch_sources(source_urls, deadline=fetch_deadline, page_cache=page_cache)
    if not pages:
        logger.info("Contact evidence review status=unavailable reason=no_readable_sources fetched_sources=0")
        return fallback("no_readable_sources")
    if address_only:
        direct_address = _grounded_address_answer([], pages, list(pages), target, language, full_address_required=full_address_required)
        if direct_address.request_satisfied:
            agent_trace.decision("evidence.direct_address", answer=direct_address.answer,
                                 request_satisfied=direct_address.request_satisfied, method="literal_source_row")
            return direct_address

    instructions = PromptCatalog.CONTACT_EVIDENCE_REVIEW_SYSTEM
    data = {
        "current_message": str(message or ""),
        "requested_institution": target,
        "phone_only": phone_only,
        "address_only": address_only,
        "clinical_discovery": clinical_discovery,
        "require_small_animal_service": require_small_animal,
        "contextual_request": str(contextual_request or "")[:4000],
        "recent_conversation": web_search._animal_question_history(history),
        "untrusted_draft": str(draft or "")[:8000],
        "fetched_sources": [
            {"url": url, "identity_text": str(getattr(text, "identity_text", "")),
             "text": _source_context(text)} for url, text in pages.items()
        ],
        "reply_language": "Hindi" if language == "hi" else "English",
    }
    try:
        # Reviewing multiple fetched pages is a different workload from the
        # short routing decision. Still share the whole turn's hard deadline.
        remaining = config.OPENAI_EVIDENCE_TIMEOUT_SECONDS
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        if remaining <= 0:
            return fallback()
        response = _call_evidence_model(
            model=config.OPENAI_QUERY_ROUTER_MODEL,
            input=[{"role": "system", "content": instructions},
                   {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
            text={"format": {"type": "json_schema", "name": "animal_contact_evidence_review",
                             "schema": REVIEW_SCHEMA, "strict": True}},
            store=False,
            max_output_tokens=3600,
            timeout=remaining,
            **({"reasoning": {"effort": "none" if config.OPENAI_QUERY_ROUTER_MODEL.startswith("gpt-5.6") else "low"}}
               if config.OPENAI_QUERY_ROUTER_MODEL.startswith("gpt-5") else {}),
        )
        reviewed = json.loads(response.output_text or "")
        clinical_only = bool(re.search(
            r"\b(?:clinical|emergency|treatment|outpatient|opd)\b[^.!?]{0,45}\b(?:phone|number|line|contact)\b|"
            r"\b(?:phone|number|line|contact)\b[^.!?]{0,35}\b(?:clinical|emergency|treatment|outpatient|opd)\b",
            message, re.I,
        ))
        result = _validate_review(reviewed, pages, requested_institution=target, phone_only=phone_only, requested_place=requested_place, requested_location_text=requested_location_text, clinical_only=clinical_only, address_only=address_only,
                                  clinical_discovery=clinical_discovery, require_small_animal=require_small_animal, language=language, requested_city=requested_city,
                                  phone_required=needs_contact and not contact_or_address and not optional_contact,
                                  address_required=not contact_or_address and bool(re.search(r"\baddress\b", message, re.I) and not re.search(r"\b(?:name|details)\s+or\s+(?:an?\s+)?address\b", message, re.I)),
                                  contact_or_address=contact_or_address, full_address_required=full_address_required)
        logger.info(
            "Contact evidence review status=%s fetched_sources=%d reason=%s",
            result.status if result is not None else "unavailable", len(pages),
            (result.evidence_issue or "reviewed") if result is not None else "evidence_validation_failed",
        )
        if result is not None:
            if address_only and not result.validated_facts and direct_address is not None and direct_address.validated_facts:
                return fallback("full_address_not_grounded")
            web_operations.record_event("evidence:result", "fallback" if result.request_satisfied is False else "ok",
                                        error_type="NoGroundedProviderClaims" if result.evidence_issue == "no_grounded_provider_claims" else "")
            return result
        if clinical_discovery or address_only:
            # A malformed evidence review cannot reintroduce an ungrounded
            # treatment assertion from either the review or the original draft.
            return fallback("evidence_validation_failed")
        # The reviewer may have corrected a mistaken name or service claim even
        # when its phone quote fails a stricter application check. Do not revive
        # the original draft's factual errors by discarding those corrections.
        safe_review = _reviewed_nonphone_fallback(reviewed, pages, language=language, phone_only=phone_only, phone_requested=needs_contact)
        if safe_review and needs_contact:
            # Removing failed phone claims cannot satisfy a requested contact,
            # even when it leaves useful facility information for discovery.
            safe_review.request_satisfied = False
        return safe_review or fallback()
    except Exception as exc:
        logger.warning("Contact evidence review status=unavailable fetched_sources=%d error=%s", len(pages), type(exc).__name__)
        return fallback("review_" + type(exc).__name__)


def _call_evidence_model(**kwargs):
    started = time.monotonic()
    try:
        response = agent_trace.model_call('evidence', query_router.client.responses.create, **kwargs)
    except Exception as exc:
        web_operations.record_event(component="model:evidence", status="error", latency_ms=round((time.monotonic() - started) * 1000), error_type=type(exc).__name__)
        raise
    web_operations.record_event(component="model:evidence", status="ok", latency_ms=round((time.monotonic() - started) * 1000))
    return response


def _url_key(value: object) -> str:
    url = str(value or "").strip()
    if not web_search._is_safe_http_url(url):
        return ""
    parsed = urlparse(urldefrag(url)[0])
    # Search tracking does not identify different evidence. Preserve functional
    # query parameters (document IDs, languages, etc.).
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.casefold().startswith("utm_") and key.casefold() not in {"gclid", "fbclid"}]
    return urlunparse(parsed._replace(query=urlencode(query)))


@agent_trace.stage('sources.fetch_batch')
def _fetch_sources(source_urls: Iterable[str], *, deadline: float | None = None, page_cache: dict[str, str | None] | None = None) -> dict[str, str]:
    urls = []
    for value in source_urls:
        if isinstance(value, dict):
            value = value.get("url")
        url = _url_key(value)
        if url and url not in urls:
            urls.append(url)
        if len(urls) == 200:
            break
    if not urls:
        return {}
    # The leading citations retain priority. Remaining slots prefer a relevant
    # contact/service path over alphabetically early recruitment/tender results.
    primary_hosts = {urlparse(url).hostname for url in urls[:4]}
    def priority(url: str) -> int:
        path = urlparse(url).path.casefold()
        return (4 * bool(re.search(r"contact|telephone|clinical|hospital|clinic|services|veterinary-college", path))
                + int(urlparse(url).hostname in primary_hosts)
                - 4 * bool(re.search(r"recruit|tender|admission|notification|counselling", path)))
    urls = urls[:4] + sorted(urls[4:], key=priority, reverse=True)
    # Inspect different publishers before spending every slot on one blocked
    # host. Deferred same-host pages remain eligible when there are free slots.
    diverse, deferred, host_counts = [], [], {}
    for url in urls:
        host = str(urlparse(url).hostname or "").removeprefix("www.")
        if host_counts.get(host, 0) >= 3:
            deferred.append(url)
        else:
            diverse.append(url)
            host_counts[host] = host_counts.get(host, 0) + 1
    urls = diverse + deferred
    cached = page_cache if page_cache is not None else {}
    found: dict[str, str] = {}
    attempted: list[str] = []
    deadline = min(deadline or float("inf"), time.monotonic() + FETCH_BUDGET_SECONDS)

    def fetch_batch(batch: list[str]) -> None:
        batch = [url for url in batch if url not in attempted][:MAX_SOURCES - len(attempted)]
        attempted.extend(batch)
        for url in batch:
            if isinstance(cached.get(url), str) and cached[url]:
                found[url] = cached[url]
        pending_urls = [url for url in batch if url not in cached]
        if not pending_urls or time.monotonic() >= deadline:
            return
        # A shared bounded executor prevents expired batches from spawning a
        # new group of workers while the preceding HTTP/parser calls finish.
        pending = {}
        for url in pending_urls:
            if not _FETCH_CAPACITY.acquire(blocking=False):
                web_operations.record_event("fetch:queue", "unavailable", error_type="FetchQueueFull")
                break
            try:
                future = _FETCH_EXECUTOR.submit(copy_context().run, web_search._fetch_public_page_text, url, deadline=deadline, allow_public_redirects=True)
            except Exception:
                _FETCH_CAPACITY.release()
                raise
            future.add_done_callback(lambda _: _FETCH_CAPACITY.release())
            pending[future] = url
        try:
            for future in as_completed(pending, timeout=max(0.001, deadline - time.monotonic())):
                try:
                    page = future.result()
                except Exception:
                    continue
                if isinstance(page, str) and page.strip():
                    found[pending[future]] = page
                    cached[pending[future]] = page
                else:
                    cached[pending[future]] = None
        except FuturesTimeout:
            logger.info("Contact source fetch reached its deadline")
        finally:
            for future in pending:
                future.cancel()

    # Reserve two source slots for a fetched publisher's own contact/service
    # pages. This is one hop, never an unbounded website crawl.
    fetch_batch(urls[:MAX_SOURCES - 2])
    linked = []
    # Follow links only from the leading cited/consulted pages.  A contact link
    # on a low-ranked directory or recruitment page must not consume a slot
    # that could otherwise verify a primary provider source.
    for source in attempted[:4]:
        page = found.get(source)
        if not page or not getattr(page, "is_html", False):
            continue
        source_host = str(urlparse(source).hostname or "").removeprefix("www.")
        for value in getattr(page, "links", ()):
            url = _url_key(value)
            if not url or url in attempted or url in linked:
                continue
            parts = urlparse(url)
            if str(parts.hostname or "").removeprefix("www.") != source_host:
                continue
            if re.search(r"contact|clinical|hospital|clinic|emergency|services", parts.path, re.I) and not re.search(r"recruit|tender|admission|notification|login|checkout", parts.path, re.I):
                linked.append(url)
    linked = sorted(linked, key=priority, reverse=True)[:2]
    fetch_batch(linked + [url for url in urls if url not in attempted])
    logger.info("Contact source fetch attempted_sources=%d readable_sources=%d", len(attempted), len(found))
    return {url: found[url] for url in attempted if url in found}


def _source_context(text: str) -> str:
    """Keep page identity plus phone passages when navigation exceeds the budget."""
    if len(text) <= 18000:
        return text
    passages = [text[:10000]]
    remaining = 8000
    for start, end, _ in _phone_spans(text):
        if end <= 10000:
            continue
        excerpt = text[max(0, start - 500): min(len(text), end + 500)]
        if len(excerpt) > remaining:
            break
        passages.append(excerpt)
        remaining -= len(excerpt)
    return "\n[Additional passage from this same source]\n".join(passages)


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def _normalized_location(value: str) -> str:
    """Normalize written separators, without adding or changing place words."""
    value = re.sub(r"\bpoly\s+clinic\b", "polyclinic", _normalized(value))
    value = re.sub(r"(?<=\d)\s+(?=\d)", "", value)
    return re.sub(r"[^\w]+", " ", value).strip()


def _literal_name_pattern(institution: str) -> str:
    """Allow written separators, without dropping or substituting name words."""
    words = re.findall(r"\w+", _normalized(institution))
    return r"(?<!\w)" + r"[\W_]+".join(map(re.escape, words)) + r"(?!\w)" if words else r"(?!)"


def _entity_key(value: str) -> str:
    value = _normalized_location(value)
    value = re.sub(r"\b(?:vet|vety)\b", "veterinary", value)
    value = re.sub(r"\bhospitals\b", "hospital", value)
    value = re.sub(r"^(?:o o|office of)\s+", "", value)
    return re.sub(r"\band\b", "", value).strip()


def _literal_source_entity(page: str, institution: str) -> str:
    """Return the source's own spelling; never infer a proper-name alias."""
    candidates = [institution]
    publisher_base = re.sub(
        r"\s*\([A-Z][A-Z0-9&.]{1,10}\)\s*", " ", institution
    ).strip()
    if (
        publisher_base != institution
        and _publisher_page_context(page, institution)
    ):
        # A first-party title may spell out the full name while omitting its
        # acronym.  Keep the literal spelling returned from that same page.
        candidates.append(publisher_base)
    # Preserve parent/campus words while tolerating a source's punctuation in
    # place of the user's linking preposition. No name or locality is dropped.
    if re.search(r"\s+(?:in|at)\s+", institution, re.I):
        candidates.append(re.sub(r"\s+(?:in|at)\s+", ", ", institution, flags=re.I))
    # A department qualifier is usable only when this source identifies it.
    identity = str(getattr(page, "identity_text", ""))
    if re.search(r"\banimal\s+husbandry\b", identity, re.I):
        candidates.append(re.sub(r",?\s+(?:of\s+)?Animal Husbandry(?:,.*)?$", "", institution, flags=re.I))
    generic = re.fullmatch(r"Veterinary\s+Hospitals?[, :()-]+(.+)", institution, re.I)
    if generic and re.search(r"\bveterinary\s+hospitals?\b", str(page), re.I):
        candidates.append("Hospital " + generic.group(1))
    for candidate in candidates:
        tokens = re.findall(r"\w+", candidate)
        if not tokens:
            continue
        parts = []
        for token in tokens:
            token = token.casefold()
            parts.append({"vet": r"(?:vet|vety|veterinary)", "vety": r"(?:vet|vety|veterinary)",
                          "veterinary": r"(?:vet|vety|veterinary)", "hospital": r"hospitals?",
                          "hospitals": r"hospitals?", "polyclinic": r"poly[\W_]*clinic"}.get(token, re.escape(token)))
        pattern = r"(?<!\w)" + r"[\W_]+".join(parts) + r"(?!\w)"
        match = re.search(pattern, str(page), re.I)
        if match:
            end = match.end()
            if "(" in match.group() and str(page)[end:end + 1] == ")":
                end += 1
            return str(page)[match.start():end]
    return ""



def _without_address_landmarks(text: str) -> str:
    # A named landmark in a postal address is not the treating institution.
    # This does not erase a second provider in an ordinary service/contact row.
    return re.sub(r"\b(?:in\s+front\s+of|opposite|near|beside|behind|adjacent\s+to)\s+(?:[\w&'.-]+\s+){0,6}(?:hospital|clinic|college|institute)\b", "address landmark", text, flags=re.I)


def _publisher_page_context(page: str, institution: str) -> bool:
    """Establish one publisher before using separated same-page fact blocks.

    This is deliberately unavailable for plain PDF text, directories, colleges
    whose university title does not identify the requested unit, and pages with
    another named provider. Existing local excerpt validation handles those.
    """
    if not getattr(page, "is_html", False) or not getattr(page, "blocks", ()):
        return False
    identity = str(getattr(page, "identity_text", ""))
    host = str(getattr(page, "source_hostname", "")).casefold().removeprefix("www.").split(".", 1)[0]
    if not identity or not host or re.search(r"\b(?:directory|directories|listing|list\s+of|find\s+(?:a\s+)?vets?|our\s+partners|provider\s+network)\b", identity, re.I):
        return False
    name = re.sub(r"\s*\([A-Z][A-Z0-9&.]{1,10}\)\s*", " ", institution).strip()
    identity_key = _entity_key(identity)
    full_base = _entity_key(name)
    # A clinic suffix can be absent from its publisher's logo/title. Proper
    # names, campuses and places are never removed by this comparison.
    base = re.sub(r"\s+(?:veterinary\s+)?(?:clinic|hospital|centre|center)$", "", full_base)
    if f" {base} " not in f" {identity_key} ":
        qualified = re.fullmatch(r"(.+?)\s*\(([^()]{3,100})\)", name)
        parent = _entity_key(qualified.group(1)) if qualified else ""
        parent = re.sub(r"\s+(?:veterinary\s+)?(?:clinic|hospital|centre|center)$", "", parent)
        # A first-party organisation contact page may title itself with the
        # parent name while a real facility appears as a parenthetical unit in
        # a source block, e.g. "Organisation (ABC and Rescue Centre)".
        if not (
            parent
            and f" {parent} " in f" {identity_key} "
            and f" {full_base} " in f" {_entity_key(str(page))} "
        ):
            return False
        base = parent
    if not base:
        return False
    generic = {"the", "of", "and", "veterinary", "vet", "clinic", "hospital", "centre", "center", "surgery", "college", "science", "sciences", "animal", "husbandry", "society", "trust", "foundation"}
    # Service/menu headings are not competing institution names. A proper
    # name (e.g. Moonlight Mobile Veterinary Clinic) remains distinctive.
    service_words = {"mobile", "ambulance", "emergency", "critical", "intensive", "care"}
    proper_words = [word for word in re.findall(r"\w+", base) if word not in generic]
    name_words = [word for word in re.findall(r"\w+", base) if word not in {"the", "of", "and"}]
    initialism = "".join(word[0] for word in name_words if word)
    aliases = re.findall(r"\(([A-Z][A-Z0-9&.]{2,10})\)", institution)
    host_names = ["".join(proper_words), initialism, *[word for word in proper_words if len(word) >= 5], *[re.sub(r"\W", "", word.casefold()) for word in aliases]]
    if not any(len(word) >= 3 and word in host for word in host_names):
        return False
    provider_pattern = re.compile(r"\b(?:[A-Z][\w&'.-]*\s+){1,6}(?:Hospital|Clinic|College|Foundation|Shelter)\b")
    own_words = set(re.findall(r"\w+", _entity_key(name)))
    for block in getattr(page, "blocks", ()):
        clean = _without_address_landmarks(str(block))
        if len(clean) < 150 and re.search(r"\b(?:other\s+(?:hospitals|clinics|providers)|partner\s+(?:hospitals|clinics))\b", clean, re.I):
            return False
        matches = list(provider_pattern.finditer(clean))
        if len(clean) < 200:
            # Directory rows do not always use title case. A compact heading
            # shaped like a provider name is checked case-insensitively too.
            row = re.match(r"\s*(?:\d+[.)]?\s+)?((?:[\w&'.-]+\s+){1,6}(?:hospital|clinic|college|foundation|shelter))\b(?=\s*(?:$|[,:(–—]|-\s))", clean, re.I)
            if row:
                matches.append(row)
        for match in matches:
            words = set(re.findall(r"\w+", _entity_key(match.group()))) - generic - service_words
            if words and not words.issubset(own_words):
                return False
    return True


def _publisher_service_excerpt(page: str, institution: str, service: str) -> str:
    if not _publisher_page_context(page, institution):
        return ""
    # Each appended fact is an actual source block. The literal identity occurs
    # in this fetched page and the page's title/heading identifies that publisher.
    # No remote snippet, model paraphrase, or second provider supplies a fact.
    generic_excerpt = ""
    for block in getattr(page, "blocks", ()):
        block = str(block)
        if not 10 <= len(block) <= 1400:
            continue
        excerpt = institution + "\n" + block
        if _provider_service_is_grounded(institution, service, excerpt, require_small_animal=False):
            if service != "clinical_treatment" or _provider_service_is_grounded(institution, service, excerpt, require_small_animal=True):
                return excerpt
            generic_excerpt = generic_excerpt or excerpt
    return generic_excerpt


def _publisher_contact_excerpt(page: str, institution: str, phone: str) -> str:
    if not _publisher_page_context(page, institution):
        return ""
    blocks = tuple(str(block) for block in getattr(page, "blocks", ()))
    key = _phone_key(phone)
    candidates = []
    for index, block in enumerate(blocks):
        if not any(value == key for _, _, value in _answer_phone_spans(block)):
            continue
        section = "\n".join(blocks[max(0, index - 2):index + 1])
        if len(section) > 1200 or not re.search(r"\b(?:phone|tel(?:ephone)?|contact|call|emergency|reception|registered\s+office)\b", section, re.I):
            continue
        # Source-local contact labels remain in the excerpt so a registered
        # office or faculty phone cannot become a clinical line.
        candidates.append(institution + "\n" + section)
    return next((excerpt for excerpt in candidates if _has_clinical_phone_evidence(excerpt, phone)), candidates[0] if candidates else "")


def _publisher_location_is_grounded(page: str, institution: str, place: str) -> bool:
    if not place or not _publisher_page_context(page, institution):
        return False
    wanted = f" {_normalized_location(place)} "
    return any(wanted in f" {_normalized_location(str(block))} " for block in getattr(page, "blocks", ()) if len(str(block)) <= 1600)


def _provider_location_excerpt(page: str, institution: str, place: str) -> str:
    """Bind an address independently of the paragraph that proves treatment.

    A directory can have a service paragraph and an address row for the same
    facility. Keep their evidence separate, without crossing a different
    facility's name to obtain an address.
    """
    name, wanted = _normalized_location(institution), _normalized_location(place)
    if not name or not wanted or len(place) > 240 or re.search(r"[\r\n]", place):
        return ""
    candidates = [str(block) for block in getattr(page, "blocks", ())]
    full = _normalized_location(str(page))
    for match in list(re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", full))[:30]:
        candidates.append(full[match.start():match.end() + 1600])
    for candidate in candidates:
        if len(candidate) > 2400:
            continue
        clean = _normalized_location(candidate)
        start = clean.find(name)
        address = clean.find(wanted, max(0, start + len(name)))
        if start < 0 or address < 0:
            continue
        between = _without_address_landmarks(clean[start + len(name):address])
        # A service heading may refer to its own hospital; another named
        # hospital/college between identity and address is a different record.
        between = re.sub(r"\b(?:its|our|the|this)\s+(?:(?:animal|veterinary|teaching)\s+){0,2}(?:hospital|clinic)\b", "", between)
        if re.search(r"\b(?:hospital|clinic|college|university|institute|shelter)\b", between):
            continue
        return candidate
    if _publisher_location_is_grounded(page, institution, place):
        return institution + "\n" + next(str(block) for block in page.blocks if f" {wanted} " in f" {_normalized_location(str(block))} ")
    return ""


def _publisher_branch_phones(page: str, institution: str, place: str) -> set[str] | None:
    """For a multi-branch publisher, bind numbers to the displayed address.

    None preserves ordinary single-facility contact validation. An empty set
    means branch association was not established, so no phone is attached.
    """
    if not _publisher_page_context(page, institution):
        return None
    blocks = tuple(str(block) for block in getattr(page, "blocks", ()))
    addresses = [i for i, block in enumerate(blocks) if len(block) <= 1600
                 and _literal_source_entity(block, institution)
                 and re.search(r"\b(?:road|street|lane|campus|nagar|sector|ward)\b|\b\d{6}\b", block, re.I)]
    if len(addresses) < 2:
        return None
    wanted = _normalized_location(place)
    phones = set()
    for index in addresses:
        if not wanted or f" {wanted} " not in f" {_normalized_location(blocks[index])} ":
            continue
        # Only the address row and its immediately following contact row;
        # never cross the next facility/address to collect another branch.
        section = [blocks[index]]
        if index + 1 < len(blocks) and index + 1 not in addresses:
            section.append(blocks[index + 1])
        phones.update(key for _, _, key in _answer_phone_spans("\n".join(section)))
    return phones

def _provider_source_excerpt(page: str, institution: str, service: str, suggested: str) -> tuple[str, bool]:
    """Repair quote formatting from fetched blocks, not from model prose."""
    full = _normalized(str(page))
    name = _normalized(institution)
    candidates: list[tuple[str, bool]] = []
    blocks = tuple(str(block) for block in getattr(page, "blocks", ()) or ())
    if suggested and _normalized(suggested) in full and name in _normalized(suggested):
        candidates.append((suggested, any(_normalized(suggested) in _normalized(block) for block in blocks)))
    for index, block in enumerate(blocks):
        if name in _normalized(block):
            candidates.append((block, True))
            # A short heading can own the following service-list rows. Service
            # validation still rejects crossing another institution's heading.
            if len(block) < 240:
                candidates.append(("\n".join(blocks[index:index + 10]), False))
    for match in list(re.finditer(re.escape(name), full))[:20]:
        candidates.append((full[match.start():match.end() + 900], False))
    generic_match = None
    for excerpt, same_block in candidates:
        if len(excerpt) <= 2400 and _provider_service_is_grounded(
            institution, service, excerpt, require_small_animal=False,
            allow_preceding=same_block, veterinary_context=bool(re.search(r"\bveterinary\s+hospitals?\b", str(page), re.I)),
        ):
            if service != "clinical_treatment" or _provider_service_is_grounded(
                institution, service, excerpt, require_small_animal=True, allow_preceding=same_block,
            ):
                return excerpt, same_block
            generic_match = generic_match or (excerpt, same_block)
    if publisher_excerpt := _publisher_service_excerpt(page, institution, service):
        if generic_match is None or _provider_service_is_grounded(institution, service, publisher_excerpt, require_small_animal=True):
            return publisher_excerpt, False
    if generic_match is not None:
        return generic_match
    return next(((text, block) for text, block in candidates if len(text) <= 2400), ("", False))


def _phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    # Some Indian institutions publish both +91 and the domestic trunk zero.
    if len(digits) == 13 and digits.startswith("910"):
        return digits[3:]
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits


def _phone_spans(text: str) -> list[tuple[int, int, str]]:
    spans = []
    for match in _PHONE.finditer(str(text or "")):
        raw = match.group().strip().rstrip(".")
        digits = re.sub(r"\D", "", raw)
        if len(digits) > 15:
            # Flattened directory columns may contain two complete numbers.
            # Split only at written separators, never inside a digit sequence.
            indian = r"(?<!\d)(?:\+?91[ .-]*)?(?:0[1-9]\d{1,4}[ .-]?\d{5,8}|[6-9]\d{9})(?!\d)"
            for part in re.finditer(indian, raw):
                key = _phone_key(part.group())
                if len(key) == 10:
                    spans.append((match.start() + part.start(), match.start() + part.end(), key))
            continue
        if not 7 <= len(digits) <= 15 or re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            continue
        spans.append((match.start(), match.start() + len(raw), _phone_key(raw)))
    return spans


def _number_only_request(message: str) -> bool:
    return bool(re.search(r"\b(?:only|just)\b[^.!?]{0,45}\b(?:number|phone|telephone)\b|\b(?:number|phone)\b[^.!?]{0,20}\bonly\b", message, re.I))


def _address_only_request(message: str) -> bool:
    return text_contract.address_only_request(message)


def _address_target_matches(target: str, institution: str, page: str, source: str) -> bool:
    if _target_matches_claim(target, institution, ""):
        return True
    # A selected parent acronym may be in a university page's identity rather
    # than repeated before every campus row. Keep every campus/name word.
    qualified = re.fullmatch(r"([A-Z][A-Z0-9]{2,12})\s+(.+)", target)
    if not qualified or not _target_matches_claim(qualified.group(2), institution, ""):
        return False
    parent = qualified.group(1).casefold()
    host = str(urlparse(source).hostname or "").split(".")
    return parent in host and bool(re.search(r"\b" + re.escape(parent) + r"\b", str(getattr(page, "identity_text", "")), re.I))


def _has_actionable_address(place: str) -> bool:
    return bool(re.search(r"\b(?:road|street|lane|campus|complex|nagar|sector|ward|opposite|near)\b|\b\d{6}\b|सड़क|मार्ग|नगर", place, re.I))


def _grounded_address_answer(claims, pages, approved_urls, target, language, *, full_address_required=False):
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        source = _url_key(claim.get("source_url"))
        page = pages.get(source)
        if page is None or source not in approved_urls:
            continue
        name = _literal_source_entity(page, str(claim.get("institution") or ""))
        place = str(claim.get("location") or "").strip()
        if not name or not target or not _address_target_matches(target, name, page, source):
            continue
        if not place or len(place) > 240 or re.search(r"[\r\n]", place):
            continue
        # A literal address/locality must belong to the selected facility's
        # own source row, never another campus or an administrative footer.
        blocks = tuple(getattr(page, "blocks", ()) or ()) or (str(page),)
        grounded = any(
            _normalized(name) in _normalized(str(block))
            and f" {_normalized_location(place)} " in f" {_normalized_location(str(block))} "
            and len(str(block)) <= 1600
            and len(re.findall(r"\b(?:hospital|college|clinic|university)\b", _normalized(str(block)).replace(_normalized(name), ""))) == 0
            for block in blocks
        )
        if grounded or _provider_location_excerpt(page, name, place):
            return ContactAnswerReview(answer=place, links=[{"label": name + ": address source", "url": source}], status="approved", request_satisfied=not full_address_required or _has_actionable_address(place),
                validated_facts=[{"kind": "location", "institution": _entity_key(name), "value": _normalized_location(place)}])
    # Repair an overlong or inferred model address using an actual source row
    # shaped as "Selected Facility, Locality, City". This requires no clinical
    # evidence or model paraphrase, and never uses another campus's footer.
    for source in approved_urls:
        page = pages[source]
        candidates = [target, re.sub(r"^[A-Z][A-Z0-9]{2,12}\s+", "", target)]
        for candidate in candidates:
            name = _literal_source_entity(page, candidate)
            if not name or not _address_target_matches(target, name, page, source):
                continue
            for block in tuple(getattr(page, "blocks", ()) or ()) or (str(page),):
                row = str(block).strip()
                if not row.casefold().startswith(name.casefold() + ","):
                    continue
                place = row[len(name):].lstrip(", ").rstrip(" .")
                if not 5 <= len(place) <= 240 or "," not in place:
                    continue
                if re.search(r"\b(?:hospital|college|clinic|university|phone|tel|email|treat\w*|services?|hours?|offers?|provides?|operates?)\b|@|https?://", place, re.I):
                    continue
                return ContactAnswerReview(answer=place, links=[{"label": name + ": address source", "url": source}],
                    status="approved", request_satisfied=not full_address_required or _has_actionable_address(place),
                    validated_facts=[{"kind": "location", "institution": _entity_key(name), "value": _normalized_location(place)}])
    return ContactAnswerReview(
        answer="मैं अनुरोधित संस्था का पता सुनिश्चित नहीं कर सका।" if language == "hi" else "I couldn't confirm the requested institution's address from accessible sources.",
        request_satisfied=False, evidence_issue="requested_address_not_grounded",
    )


def _extract_institution(message: str) -> str:
    """Extract a literal requested name, never an invented official expansion."""
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    selected = list(re.finditer(
        r"\b(?:specifically\s+)?mean\s+|\b(?:phone\s+number|number|phone|contact|details|address)\s+(?:for|of)\s+|\b(?:called|named)\s+",
        text, re.I,
    ))
    explicit_selection = bool(selected)
    if selected:
        text = text[selected[-1].end():]
    elif re.search(r"\b(?:hospital|college|clinic|shelter)\s*(?:,|\bor\b)|\ball\s+acceptable\b", text, re.I):
        # Enumerating acceptable provider types is discovery, not an entity lock.
        return ""
    # A correction replaces an older institution, including a negated earlier name.
    text = re.split(r"\b(?:not|rather than|instead of)\b", text, maxsplit=1, flags=re.I)[0]
    pattern = re.compile(
        r"(?:\b(?:for|of|from|mean|called|named)\s+)?"
        r"((?:[\w&'.-]+\s+){0,8}(?:college|hospital|clinic|university|institute|rescue|foundation|shelter)"
        r"(?:\s+(?:of|and|the|veterinary|animal|medical|sciences|teaching|hospital))*)",
        re.I,
    )
    generic = {"the", "a", "an", "of", "and", "veterinary", "vet", "government", "public", "local", "nearby", "animal", "medical", "teaching", "clinical", "emergency", "college", "hospital", "clinic", "institute", "university", "rescue", "shelter", "foundation", "sciences"}
    definite = bool(re.search(r"\bthe\s+(?:(?:veterinary|animal|government|teaching|public)\s+){0,3}(?:college|hospital|clinic)\s+(?:in|at)\b", message, re.I))
    # Search each sentence independently. Preserve initials and common title
    # abbreviations; a preceding city/state sentence is never part of a name.
    clauses = []
    start = 0
    for boundary in re.finditer(r"[.!?;।](?:\s+|$)", text):
        if boundary.group().startswith("."):
            previous = re.search(r"(\w+)$", text[:boundary.start()])
            word = previous.group(1) if previous else ""
            if (len(word) == 1 and word.isupper()) or word.casefold() in {"dr", "prof", "mr", "mrs", "ms", "st"}:
                continue
        clauses.append(text[start:boundary.start()])
        start = boundary.end()
    clauses.append(text[start:])
    for clause in clauses:
        for match in pattern.finditer(clause):
            target = match.group(1)
            target = re.sub(r"^.*?\b(?:for|from|mean|called|named)\s+", "", target, count=1, flags=re.I)
            target = re.sub(r"^.*?\b(?:phone|number|contact|details)\s+of\s+", "", target, count=1, flags=re.I)
            target = re.sub(r"^(?:(?:give|me|only|just|the|a|an|please|phone|number|contact|find|need|want|i)\s+)+", "", target, flags=re.I)
            target = target.strip(" ,.")
            # Narrative/negation text is not an institution even if it begins
            # with a capitalized location. Explicit router names are handled
            # separately, so this fallback can stay deliberately conservative.
            if re.search(r"\b(?:i|we|you|cannot|can't|couldn't|don't|doesn't|could|would|should|unable|where|which|how|there|looking|find|needs?|requires?|want|know|injured|sick|hurt|no|not)\b", target, re.I):
                continue
            words = re.findall(r"\w+", target)
            distinctive = [word for word in words if word.casefold() not in generic]
            if not explicit_selection and not definite and not any(word[:1].isupper() for word in distinctive):
                continue
            return target
    return ""


def _requested_target(message: str, history: Iterable[dict], provided: str | None = None) -> str:
    # Empty is an explicit routing decision for open provider discovery. None
    # means no routing selection was supplied and permits conservative fallback.
    if provided is not None and not provided.strip():
        return ""
    explicit = _extract_institution(message)
    if provided and _normalized(provided) in _normalized(message):
        if explicit and (
            _normalized(provided) in _normalized(explicit)
            or (_normalized(explicit) not in _normalized(provided) and re.search(r"\b(?:mean|called|named)\s+", message, re.I))
        ):
            return explicit
        return provided.strip()
    if explicit:
        return explicit
    recent = [row for row in list(history)[-12:] if isinstance(row, dict)]
    users = [str(row.get("content") or "") for row in recent if row.get("role") == "user"]
    reference_request = bool(re.search(
        r"\b(?:its?|their|(?:that|this)\s+(?:one|college|hospital|clinic|institution|facility|provider)|"
        r"(?:first|second|third|last|1st|2nd|3rd)\s+(?:listed\s+)?(?:one|college|hospital|clinic|institution|facility|provider))\b",
        message, re.I,
    ) or re.fullmatch(r"\s*(?:please\s+)?(?:only|just)\s+(?:the\s+)?(?:phone\s+)?number[.!?\s]*", message, re.I))
    if re.search(r"\bnot\s+(?:that|this)\s+(?:one|hospital|college|clinic)|\b(?:different|another)\s+(?:one|hospital|college|clinic|provider)\b", message, re.I):
        reference_request = False
    # The user can select a provider we just listed without having typed its
    # full name. Retain that selected identity, but do not treat earlier phone
    # claims as evidence; fresh source validation still follows below.
    recent_wording = [str(row.get("content") or "") for row in recent if row.get("role") in {"user", "assistant"}]
    if provided and reference_request and any(_normalized(provided) in _normalized(text) for text in recent_wording):
        return provided.strip()
    if reference_request:
        for text in reversed(users):
            if target := _extract_institution(text):
                return target
    return ""


def _target_matches_claim(target: str, institution: str, quote: str) -> bool:
    if not target:
        return True
    ignored = {"the", "a", "an", "of", "and", "in", "at", "for", "phone", "number", "contact", "administrative"}
    canonical = lambda word: {"vet": "veterinary", "vety": "veterinary", "hospitals": "hospital"}.get(word, word)
    expected = {canonical(word) for word in re.findall(r"\w+", _normalized(target))} - ignored
    actual = {canonical(word) for word in re.findall(r"\w+", _normalized(institution))} - ignored
    # All selected parent/campus/locality words belong to the target. Dropping
    # an 'at'/'in' suffix would turn a selected campus into a generic unit.
    # The caller supplies a literal identity extracted from fetched evidence;
    # a qualifier elsewhere on the page or in the model quote is insufficient.
    return bool(expected) and expected.issubset(actual)


def _claim_binds_institution(institution: str, phone: str, quote: str) -> bool:
    key = _phone_key(phone)
    clauses = re.split(r"(?<=[.!?;])\s+|[\r\n]+", quote)
    # Include a short address between an institution heading and its phone,
    # while the second-institution and negation checks below still bind identity.
    original_clauses = tuple(clauses)
    clauses.extend(" ".join(original_clauses[index:index+3]) for index in range(max(0, len(original_clauses)-1)))
    institution_key = _normalized(institution).rstrip(".")
    for index, clause in enumerate(clauses):
        if not any(value == key for _, _, value in _answer_phone_spans(clause)):
            # Short helplines can occur after arbitrary source labels.
            if key not in {_phone_key(v) for v in re.findall(r"(?<!\d)\d{3,6}(?!\d)", clause)}:
                continue
        normalized = _normalized(clause)
        name_match = re.search(_literal_name_pattern(institution_key), normalized)
        if not name_match:
            if index == 0 or _normalized(clauses[index - 1]).rstrip(".:") != institution_key:
                continue
            normalized = institution_key + " " + normalized
            name_match = re.search(_literal_name_pattern(institution_key), normalized)
        start = name_match.end()
        tail = normalized[start:]
        spans = _phone_spans(tail)
        positions = [left for left, _, value in spans if value == key]
        if not positions:
            positions = [m.start() for m in re.finditer(r"(?<!\d)\d{3,6}(?!\d)", tail) if _phone_key(m.group()) == key]
        if not positions:
            continue
        binding = tail[:min(positions)]
        # A campus/street address can contain a provider-category word without
        # introducing a different institution's phone.
        binding = re.sub(r"\b(?:veterinary\s+)?poly\s*clinic\s+campus\b|\binstitute\s+road\b", "", binding, flags=re.I)
        binding = _without_address_landmarks(binding)
        if len(binding) > 240 or re.search(r"\b(?:college|hospital|clinic|university|institute|foundation|shelter)\b", binding, re.I):
            continue
        if re.search(r"\b(?:no|not|unavailable|unconfirmed|unknown|closed|formerly|old|previous)\b", binding, re.I):
            continue
        return True
    return False


def _answer_phone_spans(answer: str) -> list[tuple[int, int, str]]:
    spans = _phone_spans(answer)
    for match in re.finditer(r"(?<!\w)\d{3,6}(?!\w)", answer):
        prefix = re.sub(r"[*_`\[\]]", "", answer[max(0, match.start()-60):match.start()])
        plain_answer = re.sub(r"[*_`\[\]]", "", answer).strip()
        line_start = answer.rfind("\n", 0, match.start()) + 1
        line_end = answer.find("\n", match.end())
        plain_line = re.sub(r"[*_`\[\]]", "", answer[line_start:line_end if line_end >= 0 else len(answer)]).strip()
        if plain_answer == match.group() or plain_line == match.group() or re.search(r"\b(?:call|phone|helpline|number|contact)\s*:?\s*$", prefix, re.I):
            if not any(left <= match.start() < right for left, right, _ in spans):
                spans.append((match.start(), match.end(), _phone_key(match.group())))
    return spans


def _has_clinical_phone_evidence(quote: str, phone: str) -> bool:
    """A college switchboard is not a clinical line without service-role evidence."""
    key = _phone_key(phone)
    for start, end, value in _answer_phone_spans(quote):
        if value != key:
            continue
        # Evaluate the label for this phone, not the next department's label.
        previous_phones = [right for left, right, _ in _answer_phone_spans(quote) if right <= start]
        left = max(0, start - 160)
        if previous_phones:
            previous_end = max(previous_phones)
            # A slash/comma-separated pair shares one published contact label.
            # A new department label starts a new role context instead.
            between = quote[previous_end:start]
            if re.sub(r"\band\b|[\s,;/|()&.+-]", "", between, flags=re.I):
                left = max(left, previous_end)
        suffix = re.split(r"[;\n]", quote[end:end + 60], maxsplit=1)[0]
        local = quote[left:start] + quote[start:end] + suffix
        if re.search(r"\b(?:administration|administrative|admissions?|registrar|dean|principal|vice.chancellor|director|office|o/o|fax)\b", local, re.I):
            continue
        if re.search(r"\b(?:clinical|emergency|outpatient|treatment|opd|tvcc|patient|teaching\s+hospital|veterinary\s+(?:hospital|clinic|polyclinic)|(?:hospital|clinic)\s+reception)\b", local, re.I):
            return True
    return False


def _claim_phone_values(value: str) -> list[str]:
    """Normalize a model's accidental multi-number field without inventing digits."""
    spans = _phone_spans(value)
    if not spans:
        spans = [(m.start(), m.end(), _phone_key(m.group())) for m in re.finditer(r"(?<!\d)\d{3,6}(?!\d)", value)]
    if not spans or len(spans) > 4:
        return []
    remainder = value
    for start, end, _ in reversed(spans):
        remainder = remainder[:start] + remainder[end:]
    if re.sub(r"\band\b|[\s,;/|()&.+-]", "", remainder, flags=re.I):
        return []
    return list(dict.fromkeys(value[start:end].strip().rstrip(".") for start, end, _ in spans))


def _source_bound_excerpt(page: str, institution: str, phone: str, suggested_quote: str = "") -> str:
    """Derive proof from actual source text, never from a paraphrased model quote.

    The model supplies discovery hints. The application locates the literal name
    and phone together in a small source passage and reruns identity/role checks.
    This tolerates ellipses in model output while retaining the same evidence bar.
    """
    name = _normalized(institution)
    full_text = _normalized(str(page))
    # A publisher may spell out its name in the title while the model includes
    # a sourced acronym (for example, "Humane Animal Society (HAS)").  The
    # conservative publisher check can establish that identity without making
    # the exact parenthetical string a prerequisite for its contact block.
    publisher_excerpt = _publisher_contact_excerpt(page, institution, phone)
    name_pattern = _literal_name_pattern(institution)
    if not name or (not re.search(name_pattern, full_text) and not publisher_excerpt):
        return ""
    candidates = []
    if suggested_quote and _normalized(suggested_quote) in full_text:
        candidates.append(suggested_quote)
    # Actual paragraph/table-row blocks preserve useful boundaries on HTML pages.
    blocks = tuple(getattr(page, "blocks", ()) or ())
    for index, block in enumerate(blocks):
        if re.search(name_pattern, _normalized(str(block))):
            candidates.append(str(block))
            if index + 1 < len(blocks):
                candidates.append("\n".join(str(item) for item in blocks[index:index + 3]))
    # Text/PDF sources and flattened pages still have a bounded literal passage.
    for match in list(re.finditer(name_pattern, full_text))[:30]:
        candidates.append(full_text[max(0, match.start() - 120):min(len(full_text), match.end() + 400)])
    valid = [excerpt for excerpt in candidates if len(excerpt) <= 2400 and _claim_binds_institution(institution, phone, excerpt)]
    if publisher_excerpt:
        valid.append(publisher_excerpt)
    clinical = next((excerpt for excerpt in valid if _has_clinical_phone_evidence(excerpt, phone)), "")
    administrative = next((excerpt for excerpt in valid if re.search(r"\b(?:office|director|administration|administrative|registrar|dean)\b", excerpt, re.I)), "")
    return clinical or administrative or (valid[0] if valid else "")


def _provider_service_is_grounded(institution: str, service: str, quote: str, *, require_small_animal: bool, allow_preceding: bool = False, veterinary_context: bool = False) -> bool:
    """Bind an explicit service to the named provider within one source passage."""
    normalized, name = _normalized(quote), _normalized(institution)
    if not name or name not in normalized:
        return False
    tail = normalized[normalized.find(name) + len(name):]
    if allow_preceding and normalized.find(name) > 0:
        # Preceding text is considered only within the same fetched HTML block.
        # It may introduce the service and then give that unit's full name.
        tail = normalized[:normalized.find(name)] + "\n" + tail
    if service == "identity_only":
        return True
    if service == "clinical_treatment":
        pattern = r"\b(?:treat(?:s|ments?|ing)?|clinical\s+(?:services?|care)|small[ -]animal\s+(?:services?|practice|clinic)|companion[ -]animal\s+practice|(?:outpatient|opd)\s+(?:services?|clinic)|diagnostic\s+(?:services?|facilities)|(?:emergency\s+)?veterinary\s+care|pet\s+healthcare|emergency\s+surgery|animal\s+hospital|veterinary\s+clinic)\b"
    elif service in {"rescue_pickup", "no_rescue_pickup"}:
        pattern = r"\b(?:(?:rescue|animal)\s+)?(?:pick[ -]?up|ambulance)\b"
    else:
        return False
    for match in re.finditer(pattern, tail):
        before = tail[:match.start()]
        after = re.split(r"[.;\n]", tail[match.end():match.end() + 180], maxsplit=1)[0]
        if len(before) > 500:
            continue
        # A second institution between the entity and service is not evidence
        # for the first. Generic possessive references to its own unit are fine.
        binding = re.sub(r"\b(?:its|our|the|this)\s+(?:(?:veterinary|teaching|clinical|animal)\s+){0,2}(?:hospital|clinic|centre|center)\b", "", before)
        binding = _without_address_landmarks(binding)
        if re.search(r"\b(?:college|hospital|clinic|university|institute|foundation|shelter)\b", binding):
            continue
        local_prefix = re.split(r"[.;\n]", before)[-1][-150:]
        negative = bool(re.search(r"\b(?:no|not|never|cannot|can't|unavailable|unconfirmed|unknown|closed|formerly)\b", local_prefix))
        negative_after = bool(re.match(r"\s*(?:(?:is|are|was|were)\s+)?(?:unavailable|not\s+(?:available|offered|provided)|closed)\b", after))
        if service == "no_rescue_pickup":
            service_passage = local_prefix + match.group() + after
            if re.search(r"\b(?:unconfirmed|unknown|unclear|uncertain|(?:no|not|without)\b.{0,30}\b(?:information|evidence|documentation)|not\s+(?:documented|established|confirmed)|(?:could|can)\s+not\s+(?:confirm|verify))\b", service_passage):
                continue
            explicitly_not_provided = bool(re.search(
                r"\b(?:does\s+not|do\s+not|doesn't|don't|cannot|can't)\s+(?:provide|offer|operate|arrange)\b[^.;]{0,70}\b(?:pick[ -]?up|ambulance)\b|"
                r"\bno\s+(?:rescue\s+)?pick[ -]?up\s+service\b", service_passage,
            ))
            if explicitly_not_provided or negative_after:
                return True
            continue
        if negative or negative_after or re.search(r"\b(?:teach\w*|research|train\w*|study|studies|curriculum)\b", local_prefix):
            continue
        if service == "rescue_pickup":
            if re.search(r"\b(?:provid\w*|offer\w*|operat\w*|availab\w*|service)\b", local_prefix + match.group() + after):
                return True
            continue
        service_passage = local_prefix + match.group() + after
        if require_small_animal and not re.search(r"\b(?:dogs?|canine|cats?|feline|pets?|small[ -]animals?|companion[ -]animals?|all\s+(?:animals|species))\b", service_passage):
            continue
        if veterinary_context or re.search(r"\b(?:animal|veterinary|vet|dog|canine|cat|feline|pet|small[ -]animal|companion[ -]animal)\w*\b", name + " " + service_passage):
            return True
    return False


def _locality_parent_source(locality: str, city: str, pages: dict[str, str], region: str = "") -> str:
    """A fetched address can establish a neighbourhood's city, not a phone alias.

    Only a bounded Address-labelled passage with comma-separated geography is
    accepted. Unrelated city lists, publisher headquarters and navigation do
    not establish a provider's location. The supporting URL is cited separately.
    """
    if not locality or not city or _normalized_location(locality) == _normalized_location(city):
        return ""
    for source, page in pages.items():
        for match in re.finditer(r"\baddress\b\s*:?", str(page), re.I):
            passage = re.split(r"\b(?:copy\s+direction|hours|reviews|categories|popular\s+cities)\b", str(page)[match.end():match.end() + 350], maxsplit=1, flags=re.I)[0]
            hierarchy = re.escape(locality) + r"\s*,\s*(?:[\w .'-]{1,60}\s*,\s*){0,3}" + re.escape(city) + r"\b"
            if re.search(hierarchy, passage, re.I) and text_contract.locality_matches(city, passage, region):
                return source
    return ""


def _publisher_names_region(page: str, region: str) -> bool:
    """Identify a regional directory; this never verifies a facility's city."""
    if not region:
        return False
    tokens = [token for token in _normalized_location(region).split() if token not in {"and", "islands", "union", "territory"}]
    identity = _normalized_location(str(getattr(page, "identity_text", "")))
    return bool(tokens) and all(re.search(r"\b" + re.escape(token) + r"\b", identity) for token in tokens)


def _grounded_provider_answer(claims: list, pages: dict[str, str], approved_urls: list[str], *, requested_institution: str,
                              requested_locality: str,
                              require_small_animal: bool, language: str, phone_displays: dict[str, str],
                              phone_institutions: dict[str, str], phone_sources: dict[str, list[str]], phone_roles: dict[str, str],
                              request_satisfied: bool = True, phone_required: bool = False, address_required: bool = False,
                              requested_region: str = "", contact_or_address: bool = False) -> ContactAnswerReview:
    """Render service claims only from entity-bound fetched evidence, not prose."""
    providers: dict[str, dict] = {}
    for claim in claims[:16]:
        if not isinstance(claim, dict) or set(claim) != {"institution", "location", "service_type", "source_url", "evidence_quote"}:
            continue
        if any(not isinstance(value, str) for value in claim.values()):
            continue
        name, place, quote = (claim[key].strip() for key in ("institution", "location", "evidence_quote"))
        source, service = _url_key(claim["source_url"]), claim["service_type"]
        if source not in approved_urls or not name or len(name) > 240 or not 10 <= len(quote) <= 2400:
            agent_trace.decision("evidence.provider_rejected", reason="invalid_claim_or_unapproved_source", claim=claim)
            continue
        name = _literal_source_entity(pages[source], name)
        if name and requested_institution and not _target_matches_claim(requested_institution, name, ""):
            qualified = _literal_source_entity(pages[source], requested_institution)
            if qualified and _entity_key(name) in _entity_key(qualified):
                name = qualified
        if not name:
            agent_trace.decision("evidence.provider_rejected", reason="institution_not_literal_in_source", claim=claim)
            continue
        quote, same_block = _provider_source_excerpt(pages[source], name, service, quote)
        if not quote:
            agent_trace.decision("evidence.provider_rejected", reason="no_entity_bound_excerpt", claim=claim)
            continue
        veterinary_context = bool(re.search(r"\bveterinary\s+hospitals?\b", str(pages[source]), re.I))
        if not _target_matches_claim(requested_institution, name, quote):
            agent_trace.decision("evidence.provider_rejected", reason="wrong_requested_institution", claim=claim, requested=requested_institution)
            continue
        location_excerpt = _provider_location_excerpt(pages[source], name, place)
        if place and (
            not _normalized_location(place)
            or (f" {_normalized_location(place)} " not in f" {_normalized_location(quote)} "
                and not location_excerpt)
            or len(place) > 240 or re.search(r"[\r\n]", place)
        ):
            # Keep a literal locality when the model appended an unsupported
            # district/state. This never adds a city absent from this passage.
            first = place.split(",", 1)[0].strip()
            place = first if first and f" {_normalized_location(first)} " in f" {_normalized_location(quote)} " else ""
            agent_trace.decision("evidence.address_reduced", institution=name, requested_address=claim["location"], retained_address=place, quote=quote, source=source)
        elif location_excerpt:
            agent_trace.decision("evidence.address_accepted", institution=name, address=place, source=source, quote=location_excerpt)
        locality_source = ""
        regional_lead = False
        if (
            requested_locality
            and not requested_institution
            and not text_contract.locality_matches(requested_locality, f'{quote} {place}', requested_region)
        ):
            if text_contract.locality_matches("", f'{quote} {place}', requested_region):
                locality_source = _locality_parent_source(place, requested_locality, pages, requested_region)
            # General discovery is tied to the current case locality.  An
            # identity-only result from another campus/city is not a useful
            # fallback merely because it appeared in the same search.
            if not locality_source:
                regional_lead = (text_contract.locality_matches("", f'{quote} {place}', requested_region)
                                 and _publisher_names_region(pages[source], requested_region)
                                 and bool(re.search(r"\bveterinary\s+(?:hospital|clinic|polyclinic|dispensary)\b", name, re.I)))
                if not regional_lead:
                    web_operations.record_event("evidence:rejection", "empty", error_type="WrongStateOrLocality")
                    agent_trace.decision("evidence.provider_rejected", reason="wrong_state_or_locality", institution=name,
                                         locality=requested_locality, region=requested_region, place=place, quote=quote, source=source)
                    continue
        if (
            service == "identity_only"
            and not place
            and not re.search(
                r"\b(?:veterinary\s+)?(?:hospital|clinic|college|complex|centre|center|dispensary|polyclinic)\b",
                name, re.I,
            )
        ):
            # A bare department/category heading with no grounded location,
            # treatment role or facility identity is not an actionable lead.
            agent_trace.decision("evidence.provider_rejected", reason="bare_category_without_location", claim=claim)
            continue
        key = _entity_key(name)
        row = providers.setdefault(key, {"name": name, "place": place, "services": set(), "urls": [], "geography_urls": set(), "regional_lead": regional_lead, "small_animal": False, "livestock_only": False})
        row["regional_lead"] = row["regional_lead"] and regional_lead
        if place and not row["place"]:
            row["place"] = place
        if source not in row["urls"]:
            row["urls"].append(source)
        if locality_source and locality_source not in row["urls"]:
            row["urls"].append(locality_source)
            row["geography_urls"].add(locality_source)
        if _provider_service_is_grounded(name, service, quote, require_small_animal=False, allow_preceding=same_block, veterinary_context=veterinary_context):
            row["services"].add(service)
            if service == "clinical_treatment":
                small_animal = _provider_service_is_grounded(name, service, quote, require_small_animal=True, allow_preceding=same_block, veterinary_context=veterinary_context)
                row["small_animal"] = row["small_animal"] or small_animal
                row["livestock_only"] = row["livestock_only"] or (not small_animal and bool(re.search(r"\b(?:cattle|livestock|bovine|ruminants?|equine|horses?|poultry)\b", quote, re.I)))
        agent_trace.decision("evidence.provider_checked", institution=name, place=place, source=source,
                             claimed_service=service, accepted_services=sorted(row["services"]),
                             small_animal=row["small_animal"], regional_lead=regional_lead, quote=quote)
    if any(not row["regional_lead"] for row in providers.values()):
        providers = {key:row for key,row in providers.items() if not row["regional_lead"]}
    has_clinical = any(not row["regional_lead"] and "clinical_treatment" in row["services"] and row["place"] and not (require_small_animal and row["livestock_only"] and not row["small_animal"]) for row in providers.values())
    if not providers:
        answer = ("मैं उपलब्ध स्रोतों से किसी विशिष्ट पशु उपचार सुविधा की पुष्टि नहीं कर सका। इसका मतलब यह नहीं कि वहाँ सेवाएँ नहीं हैं।" if language == "hi" else
                  "I couldn't establish a specific clinical treatment facility from the fetched sources. This does not mean no veterinary services exist in the requested area.")
        answer += "\n\n" + web_search._generic_veterinary_referral(language)
        return ContactAnswerReview(answer=answer, status="approved", request_satisfied=False,
            evidence_issue="no_grounded_provider_claims")
    answer = ("स्रोतों में ये उपचार विकल्प और संस्था संबंधी जानकारी मिली:" if language == "hi" else "The sources identify these treatment options and institution leads:") if has_clinical else (
        "ये संस्थाएँ स्रोतों में सूचीबद्ध हैं, लेकिन उनके यहाँ पशु का इलाज उपलब्ध होने की पुष्टि नहीं हुई:" if language == "hi" else
        "These institutions are listed in the sources, but clinical treatment access remains unconfirmed:")
    links = []
    facts = []
    for key, row in sorted(providers.items(), key=lambda item: "clinical_treatment" not in item[1]["services"]):
        facts.append({"kind": "identity", "institution": key, "value": key})
        if row["place"]:
            facts.append({"kind": "location", "institution": key, "value": _normalized_location(row["place"])})
        facts.extend({"kind": "service", "institution": key, "value": service} for service in sorted(row["services"] - {"identity_only"}))
        description = row["name"] + (f" — {row['place']}" if row["place"] else "")
        if row["regional_lead"]:
            description += (f" — क्षेत्रीय सूची का संपर्क; {requested_locality} में स्थान सुनिश्चित नहीं है।" if language == "hi" else
                            f" — regional directory lead; location in {requested_locality} is unconfirmed.")
        if "clinical_treatment" in row["services"]:
            description += (" — स्रोत में पालतू या छोटे पशुओं के उपचार का विवरण है।" if row["small_animal"] else " — स्रोत में पशु उपचार सेवाओं का विवरण है।") if language == "hi" else (
                " — the source describes treatment for pets or small animals." if row["small_animal"] else " — the source describes veterinary treatment services.")
            if require_small_animal and not row["small_animal"]:
                description += " जाने से पहले पूछें कि इस पशु को देखा जा सकता है या नहीं।" if language == "hi" else " Call ahead to confirm they can see this animal."
        elif re.search(r"\b(?:office|director|o/o)\b", row["name"], re.I):
            description += " — प्रशासनिक कार्यालय; नज़दीकी इलाज की सुविधा का पता पूछें।" if language == "hi" else " — administrative office and referral lead; ask which local facility provides treatment."
        else:
            description += " — संस्था सूचीबद्ध है; इलाज की उपलब्धता सुनिश्चित नहीं हुई।" if language == "hi" else " — institution listed; clinical treatment access is unconfirmed."
        if "rescue_pickup" in row["services"]:
            description += " स्रोत में पशु को लेने आने की सेवा का उल्लेख है; वर्तमान उपलब्धता पूछें।" if language == "hi" else " The source describes rescue pickup; current availability must be checked."
        elif "no_rescue_pickup" in row["services"]:
            description += " स्रोत स्पष्ट रूप से बताता है कि पशु को लेने आने की सेवा नहीं है।" if language == "hi" else " The source explicitly states that rescue pickup is not provided."
        for phone_key, institution in phone_institutions.items():
            identity_matches = re.sub(r"^veterinary ", "", _entity_key(institution)) == re.sub(r"^veterinary ", "", key)
            administrative_identity = bool(re.search(r"\b(?:office|director)\b", key))
            shared_sources = set(phone_sources[phone_key]) & set(row["urls"])
            if not identity_matches:
                def base_key(value: str) -> str:
                    value = re.sub(
                        r"\s*\([A-Z][A-Z0-9&.]{1,10}\)\s*", " ", value
                    ).strip()
                    return re.sub(
                        r"\s+(?:veterinary\s+)?(?:clinic|hospital|centre|center)$",
                        "", _entity_key(value),
                    )
                identity_matches = base_key(institution) == base_key(row["name"]) and any(
                    _publisher_page_context(pages[url], institution) and _publisher_page_context(pages[url], row["name"])
                    for url in shared_sources
                )
            if identity_matches and (not administrative_identity or shared_sources):
                branch_sets = [bound for url in shared_sources
                               if (bound := _publisher_branch_phones(pages[url], row["name"], row["place"])) is not None]
                if branch_sets and not any(phone_key in bound for bound in branch_sets):
                    continue
                role = phone_roles.get(phone_key, "published contact")
                label = ("प्रशासनिक संपर्क" if role == "administrative contact" else "चिकित्सा संपर्क" if role == "clinical contact" else "प्रकाशित संपर्क") if language == "hi" else role.capitalize()
                description += " " + label + ": " + phone_displays[phone_key] + "."
                facts.append({"kind": "phone", "institution": key, "value": phone_key})
                row["urls"].extend(url for url in phone_sources[phone_key] if url not in row["urls"])
        answer += "\n\n- " + description
        for source in row["urls"]:
            if source not in {link["url"] for link in links}:
                links.append({"label": (f"{row['place']}: location source" if source in row["geography_urls"] else f"{row['name']}: source"), "url": source})
    published_general = [phone_displays[fact["value"]] for fact in facts if fact["kind"] == "phone" and phone_roles.get(fact["value"]) != "clinical contact"]
    if published_general and not any(fact["kind"] == "phone" and phone_roles.get(fact["value"]) == "clinical contact" for fact in facts):
        answer += ("\n\nऊपर दिए गए सामान्य या प्रशासनिक संपर्क पर पूछें कि अभी किस इकाई में पशु का इलाज और प्रवेश हो सकता है; यह संपर्क अपने आप में चिकित्सा या पिकअप लाइन नहीं है।" if language == "hi" else
                   "\n\nCall the labelled general or administrative contact above to ask which unit can treat and admit the animal now. That contact is not established as a clinical or pickup line.")
    answer += ("\n\nइलाज की सुविधा सूचीबद्ध होने से पशु को लेने आने की बचाव सेवा या वर्तमान उपलब्धता की पुष्टि नहीं होती।" if language == "hi" else
               "\n\nA treatment listing alone does not establish rescue pickup or current availability.")
    answer += (" जाने से पहले फोन करके इस पशु के इलाज, प्रवेश और सही आपातकालीन स्थान की पुष्टि करें।" if language == "hi" else
               " Call before travelling to confirm treatment and admission for this animal and the correct emergency location.")
    has_phone = any(fact["kind"] == "phone" for fact in facts)
    has_address = any(_has_actionable_address(row["place"]) for row in providers.values())
    satisfied = has_clinical and request_satisfied and (not phone_required or has_phone) and (not address_required or has_address) and (not contact_or_address or has_phone or has_address)
    if phone_required and not has_phone:
        answer += "\n\nअनुरोधित फ़ोन नंबर सुनिश्चित नहीं हुआ।" if language == "hi" else "\n\nThe requested phone number remains unconfirmed."
    if address_required and not has_address:
        answer += "\n\nपूरा पता सुनिश्चित नहीं हुआ।" if language == "hi" else "\n\nA published street or campus address remains unconfirmed."
    return ContactAnswerReview(answer=answer, links=links, status="approved", request_satisfied=satisfied, validated_facts=facts,
                               evidence_issue="regional_leads_only" if providers and all(row["regional_lead"] for row in providers.values()) else "")


def _discovery_locality(literal: str, resolved_place: str, resolved_city: str = "") -> str:
    """Permit the explicitly requested city, without inventing town aliases.

    A neighbourhood followed by its validated city is a search origin, not an
    exclusion of all other clinics in that city. A town plus state remains that
    literal town, even if a geocoder renamed it or included a parent district.
    """
    # The linking word is grammar, not part of a city name. Keep the selected
    # state separately for conflict checking rather than requiring this wording.
    states = "|".join(re.escape(state) for state in sorted(query_router.INDIA_STATES, key=len, reverse=True))
    canonical = re.sub(r"\s+in\s+(?=(?:" + states + r")\b)", ", ", literal, flags=re.I)
    parts = [part.strip() for part in canonical.split(",") if part.strip()]
    city = (resolved_city or resolved_place.split(",", 1)[0]).strip()
    # Use the separately resolved city for cross-script matching, while the
    # validated state is still checked independently. Never replace a Latin
    # town with its parent district just because a geocoder preferred it.
    if city and parts and re.search(r"[^\x00-\x7f]", parts[0]) and re.fullmatch(r"[\x00-\x7f]+", city):
        return city
    if city and any(_normalized_location(part) == _normalized_location(city) for part in parts[1:]):
        return city
    return parts[0] if parts else city


@agent_trace.stage('evidence.validate')
def _validate_review(value: object, pages: dict[str, str], *, requested_institution: str = "", phone_only: bool = False, requested_place: str = "", requested_location_text: str = "", requested_city: str = "", clinical_only: bool = False,
                     clinical_discovery: bool = False, require_small_animal: bool = False, language: str = "en",
                     phone_required: bool = False, address_required: bool = False, address_only: bool = False,
                     contact_or_address: bool = False, full_address_required: bool = False) -> ContactAnswerReview | None:
    if not isinstance(value, dict) or set(value) != {"answer", "request_satisfied", "provider_claims", "contact_claims", "cited_source_urls"}:
        return None
    if type(value["request_satisfied"]) is not bool:
        return None
    satisfied = value["request_satisfied"]
    answer, claims, cited = value["answer"], value["contact_claims"], value["cited_source_urls"]
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 8000:
        return None
    if not isinstance(claims, list) or len(claims) > 12 or not isinstance(cited, list):
        return None
    if not isinstance(value["provider_claims"], list) or len(value["provider_claims"]) > 16:
        return None
    pages = {_url_key(url): page for url, page in pages.items() if _url_key(url)}
    approved_urls = []
    for item in cited:
        if not isinstance(item, str):
            return None
        key = _url_key(item)
        if key not in pages:
            continue
        if key not in approved_urls:
            approved_urls.append(key)
    if address_only:
        return _grounded_address_answer(value["provider_claims"], pages, approved_urls, requested_institution, language, full_address_required=full_address_required)
    if not (clinical_discovery and not phone_only) and re.search(r"https?://|", answer):
        return None
    phone_sources: dict[str, list[str]] = {}
    phone_displays: dict[str, str] = {}
    phone_institutions: dict[str, str] = {}
    phone_roles: dict[str, str] = {}
    source_labels: dict[str, str] = {}
    declared_phone_keys: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {
            "institution", "phone", "source_url", "evidence_quote", "matches_requested_entity",
        }:
            continue
        if type(claim["matches_requested_entity"]) is not bool:
            continue
        if not claim["matches_requested_entity"] and (requested_institution or phone_only or clinical_only):
            continue
        if any(not isinstance(claim[field], str) for field in ("institution", "phone", "source_url", "evidence_quote")):
            continue
        institution, phone_value, quote = claim["institution"].strip(), claim["phone"].strip(), claim["evidence_quote"].strip()
        source = _url_key(claim["source_url"])
        phones = _claim_phone_values(phone_value)
        declared_phone_keys.update(_phone_key(phone) for phone in phones)
        if source not in approved_urls or not institution or not phones:
            continue
        institution = _literal_source_entity(pages[source], institution)
        if institution and requested_institution and not _target_matches_claim(requested_institution, institution, ""):
            qualified = _literal_source_entity(pages[source], requested_institution)
            if qualified and _entity_key(institution) in _entity_key(qualified):
                institution = qualified
        if not institution:
            continue
        for phone in phones:
            excerpt = _source_bound_excerpt(pages[source], institution, phone, quote)
            if not excerpt or not _target_matches_claim(requested_institution, institution, excerpt):
                agent_trace.decision("evidence.phone_rejected", reason="phone_or_entity_not_bound", institution=institution, phone=phone, source=source, quote=quote)
                continue
            if clinical_only and not _has_clinical_phone_evidence(excerpt, phone):
                agent_trace.decision("evidence.phone_rejected", reason="clinical_role_unverified", institution=institution, phone=phone, source=source, quote=excerpt)
                continue
            generic_words = {"the", "a", "of", "and", "veterinary", "vet", "college", "hospital", "hospitals", "clinic", "animal", "science", "sciences", "husbandry", "teaching", "medical", "clinical", "complex", "government", "public"}
            target_words = set(re.findall(r"\w+", _normalized(requested_institution)))
            locality = (requested_location_text or requested_place).split(",", 1)[0].strip()
            if locality and target_words and target_words.issubset(generic_words):
                # The user's literal town can differ from the geocoder's formal
                # city label. Parent districts/states are not locality aliases.
                source_text = _normalized(excerpt)
                source_name = _normalized(institution)
                identity_passages = []
                for start, end, key in _answer_phone_spans(source_text):
                    if key != _phone_key(phone):
                        continue
                    left = source_text.rfind(source_name, 0, start)
                    if left >= 0:
                        passage = source_text[left:end]
                        if _claim_binds_institution(institution, phone, passage):
                            identity_passages.append(passage)
                if not any(web_search._has_standalone_location_occurrence(passage, locality) for passage in identity_passages):
                    continue
            phone_sources.setdefault(_phone_key(phone), []).append(source)
            phone_displays[_phone_key(phone)] = phone
            phone_institutions[_phone_key(phone)] = institution
            role = "clinical contact" if _has_clinical_phone_evidence(excerpt, phone) else "administrative contact" if re.search(r"\b(?:office|director|o/o|administration|administrative|admissions?|registrar|dean)\b", excerpt, re.I) else "published contact"
            phone_roles[_phone_key(phone)] = role
            source_labels[source] = f"{institution}: {role} source"
            agent_trace.decision("evidence.phone_accepted", institution=institution, phone=phone, role=role, source=source, quote=excerpt)

    if clinical_discovery and not phone_only:
        return _grounded_provider_answer(
            value["provider_claims"], pages, approved_urls, requested_institution=requested_institution,
            requested_locality=_discovery_locality(requested_location_text, requested_place, requested_city),
            requested_region=next((state for state in sorted(query_router.INDIA_STATES, key=len, reverse=True)
                                   if re.search(r"\b" + re.escape(state) + r"\b", requested_location_text + " " + requested_place, re.I)), ""),
            require_small_animal=require_small_animal, language=language, phone_displays=phone_displays,
            phone_institutions=phone_institutions, phone_sources=phone_sources, phone_roles=phone_roles,
            request_satisfied=satisfied, phone_required=phone_required, address_required=address_required, contact_or_address=contact_or_address,
        )

    output_phones = _answer_phone_spans(answer)
    # Emergency short codes and unformatted short local numbers also need claims.
    for match in re.finditer(r"(?<!\w)\d{3,6}(?!\w)", answer):
        key = _phone_key(match.group())
        if key in declared_phone_keys or re.search(r"\b(?:call|phone|helpline|number|contact)\s*:?\s*$", answer[max(0, match.start()-40):match.start()], re.I) or answer.strip() == match.group():
            if not any(start <= match.start() < end for start, end, _ in output_phones):
                output_phones.append((match.start(), match.end(), key))
    unsupported = [(start, end, key) for start, end, key in output_phones if key not in phone_sources]
    if unsupported:
        supported = [(start, end, key) for start, end, key in output_phones if key in phone_sources]
        if not supported:
            return None
        if phone_only:
            answer = "\n".join(answer[start:end] for start, end, _ in supported)
        else:
            for start, end, _ in sorted(unsupported, reverse=True):
                answer = answer[:start] + "[number not confirmed]" + answer[end:]
            answer += "\n\nSome other phone details could not be confirmed."
        output_phones = [(start, end, key) for start, end, key in _answer_phone_spans(answer) if key in phone_sources]
    if phone_only and not output_phones and phone_displays:
        answer = "\n".join(phone_displays.values())
        output_phones = _answer_phone_spans(answer)
    if phone_only:
        # A number-only source card must belong to the checked entity, not an
        # unrelated search result that happened to accompany an abstention.
        supported_urls = {url for urls in phone_sources.values() for url in urls}
        approved_urls = [url for url in approved_urls if url in supported_urls or (
            not phone_sources and requested_institution and _literal_source_entity(pages[url], requested_institution)
        )]
    links = [{"label": source_labels.get(url, f"Source: {urlparse(url).netloc}"), "url": url} for url in approved_urls]
    if phone_only and output_phones:
        # Source attribution belongs in the resource cards, not the requested value.
        phones = list(dict.fromkeys(answer[start:end].strip().rstrip(".") for start, end, _ in sorted(output_phones)))
        return ContactAnswerReview(answer="\n".join(phones), links=links, status="approved", request_satisfied=satisfied)
    if not output_phones:
        if phone_only:
            answer = ("मैं उपलब्ध स्रोतों से अनुरोधित संस्था का फ़ोन नंबर सुनिश्चित नहीं कर सका।" if language == "hi" else
                      "I couldn't confirm this institution's clinical phone number from accessible sources." if clinical_only else
                      "I couldn't confirm this institution's phone number from accessible sources.")
        if not approved_urls:
            return ContactAnswerReview(answer=answer.strip(), status="unavailable", request_satisfied=False)
        if phone_only:
            return ContactAnswerReview(answer=answer.strip(), links=links, status="approved", request_satisfied=False)
        sources = " ".join(f"[Source {index}](<{url}>)" for index, url in enumerate(approved_urls, 1))
        return ContactAnswerReview(answer=answer.strip() + " " + sources, links=links, status="approved", request_satisfied=satisfied)
    for start, end, key in sorted(output_phones, reverse=True):
        source = phone_sources[key][0]
        answer = answer[:end] + f" [Source](<{source}>)" + answer[end:]
    return ContactAnswerReview(answer=answer.strip(), links=links, status="approved", request_satisfied=satisfied)


def _reviewed_nonphone_fallback(value: object, pages: dict[str, str], *, language: str, phone_only: bool, phone_requested: bool = True) -> ContactAnswerReview | None:
    """Preserve reviewed factual corrections while withholding failed phone claims."""
    if phone_only or not isinstance(value, dict) or set(value) != {"answer", "request_satisfied", "provider_claims", "contact_claims", "cited_source_urls"}:
        return None
    if type(value["request_satisfied"]) is not bool:
        return None
    satisfied = value["request_satisfied"]
    answer = value.get("answer")
    cited = value.get("cited_source_urls")
    claims = value.get("contact_claims")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 8000 or re.search(r"https?://|", answer):
        return None
    if not isinstance(cited, list) or not isinstance(claims, list) or len(claims) > 12:
        return None
    urls = []
    for value in cited:
        if not isinstance(value, str) or (url := _url_key(value)) not in pages:
            return None
        if url not in urls:
            urls.append(url)
    if not urls:
        return None
    # Short claimed numbers require redaction even if the reviewer omitted a
    # phone label that the usual prose recognizer relies on.
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("phone"), str):
            continue
        key = _phone_key(claim["phone"])
        if 3 <= len(key) <= 6:
            answer = re.sub(r"(?<!\w)" + re.escape(key) + r"(?!\w)", "[number not confirmed]", answer)
    result = _unavailable(language, draft=answer, phone_requested=phone_requested)
    result.request_satisfied = satisfied
    result.links = [{"label": f"Source: {urlparse(url).netloc}", "url": url} for url in urls]
    return result


def _clean_redacted_contact_rows(text: str) -> str:
    """Remove empty staff contact rows while retaining facility descriptions."""
    lines = text.splitlines()
    original_lines = list(lines)
    redacted_rows: set[int] = set()
    marker = r"\[(?:number not confirmed|नंबर की पुष्टि नहीं हुई)\]"
    for index, line in enumerate(lines):
        row = re.fullmatch(
            r"(\s*(?:[-*]|\d+[.)])\s+)(.{1,300}?)\s*[:–—]\s*\*{0,2}" + marker + r"\*{0,2}[.।]?\s*",
            line, re.I,
        )
        if not row:
            continue
        redacted_rows.add(index)
        label = row.group(2).strip(" *")
        without_role = re.sub(
            r"\b(?:hospital\s+superintendent|(?:chief\s+)?veterinary\s+officer|in[ -]charge|"
            r"director|registrar|dean|professor|superintendent)\b", "", label, flags=re.I,
        )
        facility_detail = re.search(
            r"\b(?:hospital|clinic|college|centre|center|dispensary|university|department|office|complex|"
            r"shelter|foundation|trust|ngo|road|street|lane|campus|treat\w*|diagnostic\w*)\b|"
            r"अस्पताल|क्लिनिक|इलाज|सड़क", without_role, re.I,
        )
        staff_label = re.match(r"(?:prof\.?|dr\.?|mr\.?|mrs\.?|ms\.?|shri|smt\.?|डॉ\.?|प्रो\.?)\s", label, re.I) or without_role != label
        lines[index] = "" if staff_label and not facility_detail else row.group(1) + label
    # Drop a contact-list heading when redaction left no list beneath it.
    for index, line in enumerate(lines):
        heading = line.strip().strip("#* ").rstrip("*").strip()
        if len(heading) > 180 or not heading.endswith(":") or not re.search(
            r"\b(?:contacts?(?:\(s\))?|phones?|telephone|mobile)\b|संपर्क|फ़ोन|फोन", heading, re.I,
        ):
            continue
        first_original_row = next((position for position in range(index + 1, len(lines)) if original_lines[position].strip()), None)
        if first_original_row not in redacted_rows:
            continue
        following = next((value for value in lines[index + 1:] if value.strip()), "")
        if not re.match(r"\s*(?:[-*]|\d+[.)])\s+", following):
            lines[index] = ""
        else:
            lines[index] = re.sub(r"\b(?:confirmed|verified)\b", "Source-listed", line, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _unavailable(language: str, *, draft: str = "", phone_only: bool = False, phone_requested: bool = True) -> ContactAnswerReview:
    answer = (
        "मैं उपलब्ध स्रोतों से अनुरोधित संस्था का फ़ोन नंबर सुनिश्चित नहीं कर सका।"
        if language == "hi" else
        "I couldn't confirm the requested phone from accessible sources. This does not mean the service is unavailable."
    )
    if not phone_requested and not phone_only:
        answer = (
            "मैं उपलब्ध स्रोतों से सभी सेवाओं की जानकारी सुनिश्चित नहीं कर सका। इसका मतलब यह नहीं कि सेवाएँ उपलब्ध नहीं हैं।"
            if language == "hi" else
            "I couldn't fully confirm current service details from accessible sources. This does not mean services are unavailable."
        )
        answer += "\n\n" + web_search._generic_veterinary_referral(language)
    if draft and not phone_only:
        # Redact only unconfirmed phone values. Preserve provider rows, names,
        # abbreviations, numbering and source links exactly as researched.
        redacted = draft
        link_ranges = [(match.start(), match.end()) for match in re.finditer(
            r"\]\(<?https?://[^\s<>]+?>?\)", draft,
        )]
        for start, end, _ in sorted(_answer_phone_spans(draft), reverse=True):
            if any(left <= start < right for left, right in link_ranges):
                continue
            replacement = "[नंबर की पुष्टि नहीं हुई]" if language == "hi" else "[number not confirmed]"
            redacted = redacted[:start] + replacement + redacted[end:]
        redacted = _clean_redacted_contact_rows(redacted)
        if redacted.strip():
            # A draft was researched but failed the independent evidence pass.
            # Do not retain its self-certification or instructions to call a
            # value we just removed. Preserve whole provider descriptions.
            lines = []
            for line in redacted.splitlines():
                if re.search(r"\[(?:number not confirmed|नंबर की पुष्टि नहीं हुई)\]", line) and re.match(
                    r"\s*(?:(?:[-*]|\d+[.)])\s+)?(?:\*\*)?(?:phone|telephone|tel\.?|mobile|contact(?:\s+number)?|फ़ोन|फोन|मोबाइल|नंबर)\b",
                    line, re.I,
                ):
                    # An empty contact field adds no useful action. Keep the
                    # provider description on its own line, including names.
                    continue
                if "[number not confirmed]" in line and re.search(r"\bcall\b", line, re.I):
                    if re.match(r"\s*\d+[.)]\s+", line) or re.match(r"\s*(?:[-*]\s+)?(?:\*\*)?call\b", line, re.I):
                        continue
                line = re.sub(r"\b(?:that\s+)?I\s+(?:could|can|have)\s+(?:also\s+)?(?:confirm(?:ed)?|verif(?:y|ied))(?:\s+from\s+current\s+official\s+sources)?", "found in search results", line, flags=re.I)
                line = re.sub(r"\bthe best\b", "possible", line, flags=re.I)
                line = re.sub(r"\b(?:independently\s+)?(?:verified|confirmed)\s+(?:options|providers|contacts|services|information|results|details)\b", "search leads", line, flags=re.I)
                line = re.sub(r"\b(?:options|providers|contacts|services|details)\s+(?:are|have\s+been)\s+(?:independently\s+)?(?:verified|confirmed)\b", "options remain unconfirmed", line, flags=re.I)
                line = line.replace("This is a real ", "The search results describe a ")
                line = re.sub(r"\b(?:you\s+can\s+)?call\s+\*{0,2}\[number not confirmed\]\*{0,2}", "the contact number is unconfirmed", line, flags=re.I)
                lines.append(line)
            redacted = "\n".join(lines).strip()
            redacted = re.sub(r"\n*(?:A practical starting path|Next steps|Contacts to call):\s*$", "", redacted, flags=re.I).strip()
            notice = (
                "ये खोज में मिले संभावित विकल्प हैं; उनके संपर्क विवरण या उपलब्धता की स्वतंत्र पुष्टि नहीं हो सकी।"
                if language == "hi" else
                "These are search leads; I couldn't independently confirm their contact details or availability."
            )
            answer = notice + "\n\n" + redacted + ("\n\n" + answer if phone_requested else "")
    links = []
    for label, url in re.findall(r"\[([^\]\n]+)\]\(<?(https?://[^\s<>]+?)>?\)", answer):
        if _url_key(url) and url not in {link["url"] for link in links}:
            links.append({"label": label[:160], "url": url})
    return ContactAnswerReview(answer=answer, links=links, status="unavailable")
