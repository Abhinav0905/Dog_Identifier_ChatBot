"""India-only scope decisions for text conversations and photo uploads."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import re
from typing import Iterable

import config
from services import location, place_resolver, query_router

logger = logging.getLogger(__name__)

INDIA = place_resolver.INDIA
OUTSIDE_INDIA = place_resolver.OUTSIDE_INDIA
UNSPECIFIED = "unspecified"
AMBIGUOUS = place_resolver.AMBIGUOUS

INDIA_ONLY_RESPONSE = (
    "I'm sorry, but Ask Dorjee can only answer dog and animal-rescue questions for locations "
    "within India. I cannot assess cases or recommend services for locations outside India."
)

INDIA_LOCATION_CLARIFICATION_RESPONSE = (
    "I couldn't verify that location. Please include the city and state in India, or share your "
    "location if the dog is near you."
)

INDIA_LOCAL_HELP_LOCATION_REQUIRED_RESPONSE = (
    "Which city or district and state in India is the animal in? This helps me find suitable "
    "veterinary, public-service or rescue options. A location pin is also useful if you are with the animal."
)

INDIA_PHOTO_LOCATION_REQUIRED_RESPONSE = (
    "**Location verification required**\n\n"
    "Ask Dorjee can assess dog photos only when the photo's GPS location or your shared location "
    "is within India. Please upload a GPS-tagged photo or share your location and upload the photo again."
)

FOLLOW_UP_RE = re.compile(
    r"\b(?:ngo|ngos|rescue|rescues|shelter|shelters|contact|phone|number|address|"
    r"website|link|email|hours?|open|those|them|there|nearby|same place|same area|"
    r"which one|first one|second one|third one|closest|nearest|more options?|"
    r"another(?: one| option)?|who can i call)\b",
    re.IGNORECASE,
)

CASE_AREA_REFERENCE_RE = re.compile(
    r"\b(?:there|that area|the same area|that place|the same place|that location|"
    r"the same location)\b",
    re.IGNORECASE,
)
CASE_RELATIVE_NEARBY_RE = re.compile(
    r"\b(?:that|the same|this)\s+(?:dog|animal|puppy|case)|"
    r"\b(?:for|near)\s+(?:him|her|it|them)\b|\bthere\b|"
    r"(?:उस|उसी|इस)\s*(?:कुत्ते|पशु|जगह)|वहाँ", re.I,
)
REPORTER_LOCATION_RE = re.compile(
    r"\bnear\s+me\b|\bmy\s+(?:current\s+)?location\b|\bwhere\s+i\s+am\b|"
    r"मेरे\s+(?:पास|आसपास)|मेरी\s+(?:अभी\s+की\s+)?(?:लोकेशन|जगह)", re.I,
)

PLACE_ONLY_RE = re.compile(r"[\w\u0900-\u097f][\w\u0900-\u097f .,'()/-]{0,100}")
LOCATION_CORRECTION_RE = re.compile(
    r"\b(?:actually|instead|i\s+mean|i\s+meant|not\s+there|different\s+(?:city|town|place)|"
    r"now\s+(?:in|at)|new\s+(?:city|town|place))\b|^\s*no\b|असल\s+में|नहीं[,।]?\s|दूसर[ाे]\s+(?:शहर|गाँव)", re.I,
)
PLACE_CLARIFICATION_PREFIX_RE = re.compile(
    r"^(?:it(?:'s| is)|the (?:place|location) is|located)\s+(?:in\s+)?(.+)$",
    re.IGNORECASE,
)
NON_PLACE_CLARIFICATION_RE = re.compile(
    r"\b(?:dog|animal|ngo|rescue|shelter|help|contact|phone|number|website|link|"
    r"injured|sick|healthy|distress|what|who|where|how|why|please|give|find|yes|no|okay|ok|thanks|anything)\b|कुत्त|पशु|मदद|बताइ|क्या|कैसे|^हाँ$|^ठीक$|^धन्यवाद$",
    re.IGNORECASE,
)

@dataclass(frozen=True)
class ScopeDecision:
    scope: str
    place: str = ""
    source: str = "text"
    lat: float | None = None
    lng: float | None = None
    country_code: str = ""
    city: str = ""
    region: str = ""
    in_dharamsala: bool = False
    reference_kind: str = place_resolver.NONE
    explicit_location: bool = False
    clarification_asked: bool = False

    @property
    def is_outside_india(self) -> bool:
        return self.scope == OUTSIDE_INDIA

    @property
    def needs_clarification(self) -> bool:
        return self.scope == AMBIGUOUS

    def as_case_location(self) -> dict:
        return {
            "scope": self.scope,
            "place": self.place,
            "source": self.source,
            "lat": self.lat,
            "lng": self.lng,
            "country_code": self.country_code,
            "city": self.city,
            "region": self.region,
            "in_dharamsala": self.in_dharamsala,
            "clarification_asked": self.clarification_asked,
        }


def classify_text_scope(
    message: str,
    history: Iterable[dict] = (),
    *,
    lat: float | None = None,
    lng: float | None = None,
    case_location: dict | None = None,
    place_reference: place_resolver.PlaceReference | None = None,
) -> ScopeDecision:
    """Resolve case geography with current named place taking precedence.

    Current place text decides geography. History only tells us whether a
    distinguishing location was already requested; it never supplies a country.
    """
    history = list(history)
    if not config.INDIA_ONLY_SCOPE_ENABLED:
        return ScopeDecision(UNSPECIFIED, source="disabled")

    text = (message or "").strip()
    # A structured query router may supply the place text it extracted, but it
    # is never trusted to decide country scope.  Every named place still passes
    # through the deterministic geocoder and India boundary checks below.
    reference = place_reference or place_resolver.extract_place_reference(text)
    pending_ambiguity = _has_pending_ambiguous_location(case_location)
    if pending_ambiguity and query_router._location_was_requested(history):
        case_location = {**case_location, "clarification_asked": True}
    pending_clarification = (
        _pending_location_clarification(text, reference)
        if pending_ambiguity
        else ""
    )
    if LOCATION_CORRECTION_RE.search(text):
        pending_clarification = ""

    def resolve_reference(current_reference: place_resolver.PlaceReference, *, clarified: bool = False) -> ScopeDecision:
        enriched = _with_explicit_address_components(current_reference)
        # An explicitly named current place must never be pulled towards a
        # reporter's old/browser coordinates. Ambiguity is clarified, not guessed.
        try:
            resolution = place_resolver.resolve_named_place(enriched)
        except Exception as exc:
            logger.warning("Case location resolution unavailable: %s", type(exc).__name__)
            resolution = place_resolver.PlaceResolution(
                AMBIGUOUS, display_name=current_reference.place, source="geocoder_unavailable",
            )
        decision = _decision_from_resolution(resolution, enriched)
        components = enriched.components
        is_india_qualified = isinstance(components, place_resolver.PlaceComponents) and (
            components.country.casefold() in {"india", "bharat", "भारत"}
            or components.state.casefold() in query_router.INDIA_STATES - {"punjab", "jammu and kashmir"}
        )
        if decision.scope == AMBIGUOUS and is_india_qualified and (
            clarified or (isinstance(components, place_resolver.PlaceComponents) and (components.state or components.district))
        ):
            return ScopeDecision(
                INDIA, place=current_reference.place, source="india_place_unresolved",
                country_code="in", region=components.state, reference_kind=current_reference.kind,
                explicit_location=True, clarification_asked=clarified,
            )
        return replace(decision, clarification_asked=clarified)

    # A current explicit place replaces stale ambiguity. Only a genuine small
    # locality awaiting its district/state may combine with a terse follow-up.
    if (
        reference.kind == place_resolver.NAMED_PLACE
        and (
            not pending_ambiguity
            or not (str(case_location.get("source") or "") == "small_locality_needs_parent"
                    or case_location.get("clarification_asked"))
            or not pending_clarification
        )
    ):
        return resolve_reference(reference)

    if pending_ambiguity:
        clarification = pending_clarification
        if clarification:
            pending_place = str(case_location.get("place") or "").strip()
            if (
                _normalise_place(pending_place) not in _normalise_place(clarification)
            ):
                clarification = f"{pending_place}, {clarification}"
            pending_reference = place_resolver.PlaceReference(
                place_resolver.NAMED_PLACE,
                clarification,
                source="pending_location_clarification",
            )
            return resolve_reference(pending_reference, clarified=True)

        if _is_location_follow_up(text) or CASE_AREA_REFERENCE_RE.search(text):
            return _decision_from_case_location(case_location)

    if reference.kind == place_resolver.NAMED_PLACE:
        return resolve_reference(reference)

    if reference.kind == place_resolver.NEAR_ME:
        # Nearby for an established case means near the animal, even if the
        # reporter's browser pin is elsewhere. Only an explicit self-location
        # request without a remote-case referent switches to that browser pin.
        if case_location and (
            CASE_RELATIVE_NEARBY_RE.search(text)
            or CASE_AREA_REFERENCE_RE.search(text)
            or not REPORTER_LOCATION_RE.search(text)
        ):
            return _decision_from_case_location(case_location)
        if lat is None or lng is None:
            return ScopeDecision(
                AMBIGUOUS,
                source="near_me_without_coordinates",
                reference_kind=reference.kind,
                explicit_location=True,
            )
        return _coordinate_decision(lat, lng, source="browser_near_me", explicit=True)

    if reference.kind == place_resolver.AMBIGUOUS:
        return ScopeDecision(
            AMBIGUOUS,
            place=reference.place,
            source=reference.source,
            reference_kind=reference.kind,
            explicit_location=True,
        )

    if case_location and _is_location_follow_up(text):
        return _decision_from_case_location(case_location)

    if lat is not None and lng is not None:
        return _coordinate_decision(lat, lng, source="browser_case", explicit=False)

    return ScopeDecision(UNSPECIFIED, reference_kind=place_resolver.NONE)


def upload_is_in_india(
    verification: dict | None,
    lat: float | None,
    lng: float | None,
) -> bool:
    """Use photo EXIF first, then the selected browser/manual coordinates."""
    if verification:
        exif_candidates = [
            candidate
            for candidate in verification.get("candidates", [])
            if candidate.get("source") == "exif"
        ]
        if exif_candidates:
            return bool(exif_candidates[0].get("in_india"))
    if lat is None or lng is None:
        return False
    return location.is_in_india(lat, lng)


def location_clarification_response(decision: ScopeDecision, language: str = "en") -> str:
    if decision.clarification_asked:
        return (
            "बताए गए स्थान की सटीक पहचान अभी नहीं हुई है। इसका मतलब यह नहीं कि वहाँ सेवाएँ नहीं हैं; उपलब्ध भारतीय स्रोतों में उसी स्थान के आधार पर विकल्प खोजे जा सकते हैं।"
            if language == "hi" else
            "The exact locality is still unconfirmed. That does not mean no services exist; the place details you supplied can still guide a search of Indian sources."
        )
    if language == "hi":
        return (
            f"{decision.place} किस जिले और राज्य में है? सही स्थानीय पशु चिकित्सा या बचाव विकल्प खोजने के लिए यह जानकारी चाहिए।"
            if decision.place else
            "पशु भारत के किस शहर या जिले और राज्य में है?"
        )
    if decision.source == "small_locality_needs_parent" and decision.place:
        return (
            f"I found more than one possible match for {decision.place}. Please include its "
            "district and state in India so I can find suitable veterinary or rescue options."
        )
    if decision.place:
        return (
            f"I couldn't verify {decision.place}. Please check the spelling and include the city, "
            "district, or state in India."
        )
    return INDIA_LOCATION_CLARIFICATION_RESPONSE


def _decision_from_resolution(
    resolution: place_resolver.PlaceResolution,
    reference: place_resolver.PlaceReference,
) -> ScopeDecision:
    return ScopeDecision(
        scope=resolution.scope,
        place=resolution.display_name or reference.place,
        source=resolution.source,
        lat=resolution.lat,
        lng=resolution.lng,
        country_code=resolution.country_code,
        city=resolution.city,
        region=resolution.region,
        in_dharamsala=resolution.in_dharamsala,
        reference_kind=reference.kind,
        explicit_location=True,
    )


def _decision_from_case_location(case_location: dict) -> ScopeDecision:
    return ScopeDecision(
        scope=str(case_location.get("scope") or UNSPECIFIED),
        place=str(case_location.get("place") or ""),
        source="session_case",
        lat=case_location.get("lat"),
        lng=case_location.get("lng"),
        country_code=str(case_location.get("country_code") or ""),
        city=str(case_location.get("city") or ""),
        region=str(case_location.get("region") or ""),
        in_dharamsala=bool(case_location.get("in_dharamsala")),
        reference_kind="session",
        explicit_location=False,
        clarification_asked=bool(case_location.get("clarification_asked")),
    )


def _coordinate_decision(
    lat: float,
    lng: float,
    *,
    source: str,
    explicit: bool,
) -> ScopeDecision:
    in_india = location.is_in_india(lat, lng)
    return ScopeDecision(
        INDIA if in_india else OUTSIDE_INDIA,
        place=f"{lat:.6f}, {lng:.6f}",
        source=source,
        lat=lat,
        lng=lng,
        country_code="in" if in_india else "",
        in_dharamsala=in_india and location.is_in_dharamsala_region(lat, lng),
        reference_kind=place_resolver.NEAR_ME if explicit else "browser_case",
        explicit_location=explicit,
    )


def _is_location_follow_up(message: str) -> bool:
    return len(message) <= 180 and bool(FOLLOW_UP_RE.search(message))


def _has_pending_ambiguous_location(case_location: dict | None) -> bool:
    return bool(
        case_location
        and str(case_location.get("scope") or "") == AMBIGUOUS
        and str(case_location.get("place") or "").strip()
    )


def _pending_location_clarification(
    message: str,
    reference: place_resolver.PlaceReference,
) -> str:
    """Return a short place-only reply used to clarify a pending locality."""
    text = re.sub(r"\s+", " ", (message or "").strip(" .!?"))
    if not text or len(text.split()) > 8:
        return ""

    prefix_match = PLACE_CLARIFICATION_PREFIX_RE.fullmatch(text)
    candidate = prefix_match.group(1).strip(" ,") if prefix_match else text
    if reference.kind == place_resolver.NAMED_PLACE:
        candidate = reference.place
    elif prefix_match is None and (
        not PLACE_ONLY_RE.fullmatch(candidate)
        or NON_PLACE_CLARIFICATION_RE.search(candidate)
    ):
        return ""

    return candidate if PLACE_ONLY_RE.fullmatch(candidate) else ""


def _normalise_place(value: str) -> str:
    return re.sub(r"[^\w\u0900-\u097f]+", " ", value.casefold()).strip()


def _with_explicit_address_components(reference: place_resolver.PlaceReference) -> place_resolver.PlaceReference:
    """Carry stated country/state through geocoding without inventing geography."""
    if reference.components or reference.kind != place_resolver.NAMED_PLACE:
        return reference
    raw = reference.place.strip()
    country = ""
    country_aliases = {**query_router.COUNTRY_ALIASES, "भारत": "India", "नेपाल": "Nepal", "पाकिस्तान": "Pakistan"}
    for alias in sorted(country_aliases, key=len, reverse=True):
        match = re.search(r"(?<!\w)" + re.escape(alias) + r"\s*$", raw, re.I)
        if match:
            country = country_aliases[alias]
            raw = raw[:match.start()].strip(" ,")
            break
    state = ""
    for name in sorted(query_router.INDIA_STATES, key=len, reverse=True):
        match = re.search(r"(?<!\w)" + re.escape(name) + r"\s*$", raw, re.I)
        if match:
            state = raw[match.start():].strip()
            raw = raw[:match.start()].strip(" ,")
            break
    if not country and not state:
        return reference
    return replace(reference, components=place_resolver.PlaceComponents(city=raw, state=state, country=country))
