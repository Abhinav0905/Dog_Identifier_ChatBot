"""Extract and geocode the current case location from a text message."""

from __future__ import annotations

from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from difflib import SequenceMatcher
import json
import logging
import re
import threading
import time
from urllib.parse import quote

from openai import OpenAI
import requests

import config
import database as db
from services import location
from services.prompts import PromptCatalog

logger = logging.getLogger(__name__)

# One small wall-clock budget includes provider fallbacks and queueing. A slow
# provider cannot occupy every chat worker or restart its timeout for each spelling.
_resolution_deadline = ContextVar("place_resolution_deadline", default=None)
_resolution_requests = ContextVar("place_resolution_requests", default=None)
_geocoder_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="place-http")
_geocoder_capacity = threading.BoundedSemaphore(4)


@contextmanager
def geocoding_budget(deadline: float):
    """Share a request deadline across scope and parent-place resolution."""
    inherited = _resolution_deadline.get()
    token = _resolution_deadline.set(min(deadline, inherited or float("inf"), time.monotonic() + 8.0))
    cache = _resolution_requests.get()
    cache_token = _resolution_requests.set(cache if cache is not None else {})
    try:
        yield
    finally:
        _resolution_requests.reset(cache_token)
        _resolution_deadline.reset(token)


def _bounded_resolution(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        supplied = kwargs.pop("deadline", None)
        inherited = _resolution_deadline.get()
        deadline = min(supplied or float("inf"), inherited or float("inf"), time.monotonic() + 8.0)
        token = _resolution_deadline.set(deadline)
        cache = _resolution_requests.get()
        cache_token = _resolution_requests.set(cache if cache is not None else {})
        try:
            return function(*args, **kwargs)
        finally:
            _resolution_requests.reset(cache_token)
            _resolution_deadline.reset(token)
    return wrapped


@contextmanager
def _geocoder_slot(lock, last_request: float, interval: float = 1.05):
    deadline = _resolution_deadline.get() or time.monotonic() + 8.0
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not lock.acquire(timeout=min(0.25, remaining)):
        raise TimeoutError("Geocoder queue budget exhausted")
    try:
        delay = max(0.0, interval - (time.monotonic() - last_request))
        if delay >= deadline - time.monotonic():
            raise TimeoutError("Geocoder pacing budget exhausted")
        if delay:
            time.sleep(delay)
        yield
    finally:
        lock.release()


def _bounded_geocoder_get(url: str, **kwargs):
    deadline = _resolution_deadline.get() or time.monotonic() + 8.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Geocoder deadline exhausted")
    key = (url, json.dumps(kwargs.get("params") or {}, sort_keys=True))
    cache = _resolution_requests.get()
    if cache is not None and key in cache:
        cached = cache[key]
        if isinstance(cached, Exception):
            raise cached
        return cached
    if not _geocoder_capacity.acquire(timeout=min(0.05, remaining)):
        raise TimeoutError("Geocoder concurrency budget exhausted")
    kwargs["timeout"] = (min(2.0, remaining), max(0.05, min(5.0, remaining)))
    future = _geocoder_workers.submit(requests.get, url, **kwargs)
    future.add_done_callback(lambda _: _geocoder_capacity.release())
    try:
        response = future.result(timeout=max(0.001, deadline - time.monotonic()))
        if cache is not None:
            cache[key] = response
        return response
    except Exception as exc:
        if cache is not None:
            cache[key] = exc
        future.cancel()
        raise

INDIA = "india"
OUTSIDE_INDIA = "outside_india"
AMBIGUOUS = "ambiguous"

NAMED_PLACE = "named_place"
NEAR_ME = "near_me"
NONE = "none"

client = (
    OpenAI(
        api_key=config.OPENAI_API_KEY,
        timeout=config.OPENAI_ROUTING_TIMEOUT_SECONDS,
        max_retries=0,
    )
    if config.OPENAI_API_KEY
    else None
)

PLACE_COMPONENT_FIELDS = (
    "house_number",
    "landmark",
    "street",
    "area",
    "city",
    "district",
    "state",
    "postal_code",
    "country",
)

PLACE_COMPONENTS_SCHEMA = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in PLACE_COMPONENT_FIELDS},
    "required": list(PLACE_COMPONENT_FIELDS),
    "additionalProperties": False,
}

PLACE_REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": [NAMED_PLACE, NEAR_ME, NONE, AMBIGUOUS],
        },
        "place": {"type": "string"},
        "components": PLACE_COMPONENTS_SCHEMA,
    },
    "required": ["kind", "place", "components"],
    "additionalProperties": False,
}

NEAR_ME_RE = re.compile(
    r"\b(?:near me|nearby|where i am|my current location|my area|the area|this area|"
    r"my city|my town|around here)\b",
    re.IGNORECASE,
)

CASE_LOCATION_FOLLOW_UP_RE = re.compile(
    r"\b(?:there|that area|the same area|that place|the same place|that location|"
    r"the same location)\b",
    re.IGNORECASE,
)

LOCATION_PREPOSITION_RE = re.compile(
    r"(?=\b(?:in|near|around|from|at|on|outside|opposite|beside)\s+([^?.!;]{2,120}))",
    re.IGNORECASE,
)

CASE_DETAIL_SUFFIX_RE = re.compile(
    r"(?:\s*[,\-\u2013\u2014]\s*)?\b(?:"
    r"needs?|requires?|wants?|seeks?"
    r")\s+(?:urgent\s+|immediate\s+)?(?:help|rescue|assistance|care|treatment)\b.*$|"
    r"(?:\s*[,\-\u2013\u2014]\s*)?\b(?:is|was|looks?|appears?)\s+"
    r"(?:badly\s+)?(?:injured|hurt|sick|bleeding|distressed|stranded|dying)\b.*$",
    re.IGNORECASE,
)

NON_PLACE_PREFIXES = (
    "bad shape",
    "poor shape",
    "good shape",
    "bad condition",
    "poor condition",
    "good condition",
    "pain",
    "danger",
    "distress",
    "need",
    "risk",
    "love",
    "heat",
    "oestrus",
    "estrus",
    "a dog",
    "the dog",
    "an animal",
    "the animal",
    "a dog bite",
    "the photo",
    "this photo",
    "the image",
    "this image",
    "front of",
    "order to",
    "contact with",
    "community dogs",
    "street dogs",
    "stray dogs",
    "dogs",
    "animals",
    "a school",
    "the school",
    "a market",
    "the market",
    "market",
    "a road",
    "the road",
    "road",
    "a street",
    "the street",
    "street",
    "a bus stop",
    "the bus stop",
    "bus stop",
    "a railway station",
    "the railway station",
    "railway station",
    "a park",
    "the park",
    "park",
    "my house",
    "our house",
    "the house",
    "home",
)

MODEL_LOCATION_HINT_RE = re.compile(
    r"\b(?:city|town|village|district|state|country|india|bharat|area|locality|"
    r"neighbou?rhood|sector|block|colony|nagar|chowk|road|rd|street|st|lane|marg|"
    r"bridge|postcode|postal code|pin code|pincode)\b|"
    r"\b(?:ngo|ngos|rescue|shelter)\b[^.!?]{0,60}\b(?:for|serving)\b",
    re.IGNORECASE,
)

# Canonical coordinates for Deb's Dharamsala service-area aliases. These avoid
# a network dependency for the organisation's own locality and common spellings.
DHARAMSALA_ALIASES: tuple[tuple[re.Pattern, str, float, float], ...] = (
    (re.compile(r"\bmcleod\s*ganj\b|\bmc\s*leod\s*ganj\b", re.I), "McLeod Ganj, Himachal Pradesh, India", 32.2425758, 76.3212781),
    (re.compile(r"\brakkar\b", re.I), "Rakkar, Dharamshala, Himachal Pradesh, India", 32.1971000, 76.3901000),
    (re.compile(r"\bkhanyara\b", re.I), "Khanyara, Dharamshala, Himachal Pradesh, India", 32.2123260, 76.3670790),
    (re.compile(r"\bgamru\b", re.I), "Gamru, Dharamshala, Himachal Pradesh, India", 32.2249310, 76.3300610),
    (re.compile(r"\bkharota\b", re.I), "Kharota, Dharamshala, Himachal Pradesh, India", 32.2143420, 76.3797410),
    (re.compile(r"\b(?:dharamsala|dharamshala|dharmasala|dharmsala|dharmshala)\b", re.I), "Dharamshala, Himachal Pradesh, India", 32.2143039, 76.3196717),
)

_geocoder_lock = threading.Lock()
_last_geocoder_request = 0.0
_fuzzy_geocoder_lock = threading.Lock()
_last_fuzzy_geocoder_request = 0.0
_postal_lookup_lock = threading.Lock()
_last_postal_lookup_request = 0.0

GEOGRAPHIC_ADDRESS_TYPES = {
    "country",
    "state",
    "state_district",
    "region",
    "province",
    "county",
    "city",
    "town",
    "village",
    "municipality",
    "hamlet",
    "suburb",
    "neighbourhood",
    "quarter",
    "district",
    "borough",
    "local_authority",
}

# These address-level Nominatim results are safe to use as geographic anchors.
# Deliberately exclude houses, buildings, amenities, shops, offices, tourism,
# and other POI types: a business name must never establish country scope.
ADDRESS_GEOGRAPHIC_TYPES = GEOGRAPHIC_ADDRESS_TYPES | {
    "road",
    "postcode",
}

ADDRESS_QUERY_RE = re.compile(
    r"\b(?:road|rd|street|st|lane|marg|avenue|ave|highway|sector|block|colony|"
    r"nagar|chowk|neighbou?rhood|suburb|quarter|postcode|postal code|pin code|"
    r"pincode)\b|\b\d{6}\b",
    re.IGNORECASE,
)

GENERIC_ADDRESS_PREFIXES = {
    "a road",
    "the road",
    "road",
    "a street",
    "the street",
    "street",
}

SMALL_LOCALITY_ADDRESS_TYPES = {
    "village",
    "hamlet",
    "suburb",
    "neighbourhood",
    "quarter",
    "district",
}

PHOTON_SETTLEMENT_TYPES = {
    "city",
    "town",
    "village",
    "hamlet",
    "suburb",
    "district",
    "county",
    "locality",
}

PHOTON_REGION_TYPES = {"state", "region", "province"}

LOCATIONIQ_AUTOCOMPLETE_CITY_TYPES = {"city", "town", "municipality"}
LOCATIONIQ_AUTOCOMPLETE_REGION_TYPES = {"state", "state_district", "region"}
LOCATIONIQ_AUTOCOMPLETE_CITY_MIN_SCORE = 0.90
LOCATIONIQ_AUTOCOMPLETE_REGION_MIN_SCORE = 0.82
LOCATIONIQ_AUTOCOMPLETE_MIN_MARGIN = 0.05

PLACE_STOP_WORDS = {
    "india",
    "bharat",
    "city",
    "town",
    "village",
    "district",
    "state",
}


@dataclass(frozen=True)
class PlaceComponents:
    """Structured address parts extracted from the current user message."""

    house_number: str = ""
    landmark: str = ""
    street: str = ""
    area: str = ""
    city: str = ""
    district: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""

    def geocoder_query(self) -> str:
        """Return a specific-to-broad query without repeating equivalent parts."""
        parts: list[str] = []
        street = " ".join(part for part in (self.house_number, self.street) if part).strip()
        for part in (
            street,
            self.area,
            self.city,
            self.district,
            self.state,
            self.postal_code,
            self.country,
        ):
            cleaned = re.sub(r"\s+", " ", (part or "").strip(" ,"))
            if cleaned and _normalise_query(cleaned) not in {
                _normalise_query(existing) for existing in parts
            }:
                parts.append(cleaned)
        return ", ".join(parts)


@dataclass(frozen=True)
class PlaceReference:
    kind: str
    place: str = ""
    source: str = "text"
    components: PlaceComponents | dict[str, str] | None = None


@dataclass(frozen=True)
class PlaceResolution:
    scope: str
    display_name: str = ""
    lat: float | None = None
    lng: float | None = None
    country_code: str = ""
    source: str = "geocoder"
    in_dharamsala: bool = False
    city: str = ""
    region: str = ""


@dataclass(frozen=True)
class IndiaPostalPlace:
    name: str
    district: str
    state: str
    pincode: str


def extract_place_reference(message: str) -> PlaceReference:
    """Extract only the location expressed in the current user message."""
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text:
        return PlaceReference(NONE)

    heuristic = _extract_prepositional_place(text)
    if heuristic:
        return PlaceReference(NAMED_PLACE, heuristic, source="heuristic")

    alias = _matching_direct_dharamsala_alias(text, None)
    if alias:
        return PlaceReference(NAMED_PLACE, alias[1], source="dharamsala_alias")

    if NEAR_ME_RE.search(text):
        return PlaceReference(NEAR_ME, source="near_me")

    if CASE_LOCATION_FOLLOW_UP_RE.search(text):
        return PlaceReference(NONE, source="case_location_follow_up")

    if not MODEL_LOCATION_HINT_RE.search(text):
        return PlaceReference(NONE)

    return _extract_place_with_model(text)


@_bounded_resolution
def resolve_named_place(
    place: str | PlaceReference,
    *,
    place_reference: PlaceReference | None = None,
    hint_lat: float | None = None,
    hint_lng: float | None = None,
) -> PlaceResolution:
    """Resolve a place to India or another country using cached geocoding.

    ``place_reference`` is an optional structured override for callers that have
    already extracted address components. A ``PlaceReference`` may also be
    supplied directly as ``place``; existing string callers remain unchanged.
    """
    reference = place_reference or (place if isinstance(place, PlaceReference) else None)
    components = _coerce_place_components(reference.components if reference else None)
    raw_place = reference.place if reference else str(place or "")
    structured_place = components.geocoder_query() if components else ""
    cleaned = _clean_place_candidate(structured_place or raw_place)
    if not cleaned:
        return PlaceResolution(AMBIGUOUS, source="empty")

    if components and _explicit_country_is_outside_india(components.country):
        return PlaceResolution(
            OUTSIDE_INDIA,
            display_name=cleaned,
            country_code=_normalise_query(components.country),
            source="explicit_foreign_country",
        )

    alias = _matching_direct_dharamsala_alias(cleaned, components)
    if alias:
        _, display_name, lat, lng = alias
        return PlaceResolution(
            INDIA,
            display_name=display_name,
            lat=lat,
            lng=lng,
            country_code="in",
            source="dharamsala_alias",
            in_dharamsala=True,
            city=_alias_city(display_name),
            region="Himachal Pradesh",
        )

    if re.fullmatch(r"(?:india|bharat)", cleaned, re.IGNORECASE):
        return PlaceResolution(
            INDIA,
            display_name="India",
            country_code="in",
            source="country_name",
        )

    indian_hint = _valid_india_hint(hint_lat, hint_lng)
    query_key = f"v10:{_primary_geocoder_provider()}:{_normalise_query(cleaned)}"
    cached = _get_cached_resolution(query_key)
    if cached:
        return cached

    if not config.PLACE_GEOCODING_ENABLED:
        return PlaceResolution(AMBIGUOUS, display_name=cleaned, source="disabled")

    variants = _geocoder_query_variants(cleaned, components=components)

    # A global place result must win for an unqualified name. Searching India
    # first can turn "London" into a Pune business called London. An explicit
    # India qualifier is the one case where an India-only lookup is authoritative.
    if _has_explicit_india_qualifier(cleaned):
        for query in variants:
            india_result, result_source = _nominatim_place_search(
                query,
                country_code="in",
                allow_address=_address_query_is_qualified(query, components),
            )
            if india_result:
                resolution = _resolution_from_geocoder(india_result, source=result_source)
                if resolution.scope == INDIA:
                    if _needs_locality_qualification(cleaned, india_result):
                        hinted = _resolve_fuzzy_indian_place(
                            cleaned,
                            hint_lat=indian_hint[0] if indian_hint else None,
                            hint_lng=indian_hint[1] if indian_hint else None,
                            require_nearby_hint=True,
                        )
                        if hinted:
                            return hinted
                        return PlaceResolution(
                            AMBIGUOUS,
                            display_name=cleaned,
                            source="small_locality_needs_parent",
                        )
                    _save_cached_resolution(query_key, resolution)
                    return resolution
        corrected = _resolve_locationiq_india_correction(cleaned, allow_region=True)
        if corrected:
            _save_cached_resolution(query_key, corrected)
            return corrected
        fuzzy = _resolve_fuzzy_indian_place(
            cleaned,
            hint_lat=indian_hint[0] if indian_hint else None,
            hint_lng=indian_hint[1] if indian_hint else None,
        )
        if fuzzy:
            if not fuzzy.source.endswith("_browser_hint"):
                _save_cached_resolution(query_key, fuzzy)
            return fuzzy
        return PlaceResolution(AMBIGUOUS, display_name=cleaned, source="not_found_in_india")

    for query in variants:
        global_result, result_source = _nominatim_place_search(
            query,
            allow_address=_address_query_is_qualified(query, components),
        )
        if global_result:
            resolution = _resolution_from_geocoder(global_result, source=result_source)
            if resolution.scope == INDIA and _needs_locality_qualification(cleaned, global_result):
                hinted = _resolve_fuzzy_indian_place(
                    cleaned,
                    hint_lat=indian_hint[0] if indian_hint else None,
                    hint_lng=indian_hint[1] if indian_hint else None,
                    require_nearby_hint=True,
                )
                if hinted:
                    return hinted
                return PlaceResolution(
                    AMBIGUOUS,
                    display_name=cleaned,
                    source="small_locality_needs_parent",
                )
            if (
                resolution.scope == OUTSIDE_INDIA
                and not _outside_result_is_explicit_match(
                    cleaned,
                    components,
                    global_result,
                )
            ):
                corrected = _resolve_locationiq_india_correction(
                    cleaned,
                    allow_region=False,
                ) or _resolve_fuzzy_indian_place(
                    cleaned,
                    hint_lat=indian_hint[0] if indian_hint else None,
                    hint_lng=indian_hint[1] if indian_hint else None,
                )
                if corrected:
                    if not corrected.source.endswith("_browser_hint"):
                        _save_cached_resolution(query_key, corrected)
                    return corrected
            if resolution.scope in {INDIA, OUTSIDE_INDIA}:
                _save_cached_resolution(query_key, resolution)
            return resolution

    # Country filtering is a final recall fallback for obscure Indian
    # settlements; it is never allowed to override a real global result.
    for query in variants:
        india_result, result_source = _nominatim_place_search(
            query,
            country_code="in",
            allow_address=_address_query_is_qualified(query, components),
        )
        if india_result:
            resolution = _resolution_from_geocoder(india_result, source=result_source)
            if resolution.scope == INDIA:
                _save_cached_resolution(query_key, resolution)
                return resolution

    corrected = _resolve_locationiq_india_correction(cleaned, allow_region=True)
    if corrected:
        _save_cached_resolution(query_key, corrected)
        return corrected

    fuzzy = _resolve_fuzzy_indian_place(
        cleaned,
        hint_lat=indian_hint[0] if indian_hint else None,
        hint_lng=indian_hint[1] if indian_hint else None,
    )
    if fuzzy:
        if not fuzzy.source.endswith("_browser_hint"):
            _save_cached_resolution(query_key, fuzzy)
        return fuzzy

    return PlaceResolution(AMBIGUOUS, display_name=cleaned, source="not_found")


@_bounded_resolution
def resolve_service_city(
    city: str,
    *,
    state: str = "",
    district: str = "",
    country: str = "",
    hint_lat: float | None = None,
    hint_lng: float | None = None,
) -> PlaceResolution:
    """Independently resolve the parent city used for NGO coverage.

    A street or sub-area is useful incident context, but it must never become
    the NGO cache/search boundary. Only a candidate that still matches the
    requested city is accepted here; a provider's broader state/district
    fallback is rejected.
    """
    requested_city = _clean_place_candidate(city)
    if not requested_city:
        return PlaceResolution(AMBIGUOUS, source="service_city_missing")

    components = PlaceComponents(
        city=requested_city,
        district=_clean_place_candidate(district),
        state=_clean_place_candidate(state),
        country=_clean_place_candidate(country),
    )
    query = components.geocoder_query()
    reference = PlaceReference(
        NAMED_PLACE,
        query,
        source="service_city",
        components=components,
    )
    resolution = resolve_named_place(
        reference,
        hint_lat=hint_lat,
        hint_lng=hint_lng,
    )
    if (
        resolution.scope == INDIA
        and not resolution.city
        and resolution.region
        and _normalise_query(resolution.region) == _normalise_query(requested_city)
    ):
        return PlaceResolution(
            AMBIGUOUS,
            display_name=resolution.display_name,
            source="service_city_is_region",
        )

    matched_city = _matching_city_label(requested_city, resolution)

    # A primary provider can return a plausible but different Indian place for
    # a misspelling. Give the fuzzy settlement resolver one strictly validated
    # chance before asking the user to clarify.
    if resolution.scope == INDIA and not matched_city:
        fuzzy = _resolve_fuzzy_indian_place(
            query,
            hint_lat=hint_lat,
            hint_lng=hint_lng,
        )
        fuzzy_city = _matching_city_label(requested_city, fuzzy) if fuzzy else ""
        if fuzzy and fuzzy.scope == INDIA and fuzzy_city:
            resolution = fuzzy
            matched_city = fuzzy_city

    if resolution.scope == OUTSIDE_INDIA and matched_city:
        return resolution
    if resolution.scope != INDIA or not matched_city:
        return PlaceResolution(
            AMBIGUOUS,
            display_name=query or requested_city,
            source="service_city_not_verified",
        )

    canonical_region = (
        resolution.region
        or _region_label_from_display(resolution.display_name, matched_city)
    )
    if not canonical_region:
        canonical_region = _canonical_region_for_verified_city(
            matched_city,
            lat=resolution.lat,
            lng=resolution.lng,
        )
        if canonical_region:
            resolution = replace(
                resolution,
                region=canonical_region,
                source=f"{resolution.source}_canonical_region",
            )
    if not canonical_region:
        # Some providers return a verified Indian city centroid but omit its
        # state/UT (notably New Delhi). Enrich only from an independent India-
        # bounded settlement result that still matches the requested city.
        enriched = _resolve_fuzzy_indian_place(
            query,
            hint_lat=resolution.lat,
            hint_lng=resolution.lng,
        )
        enriched_city = (
            _matching_city_label(requested_city, enriched) if enriched else ""
        )
        enriched_region = ""
        if enriched and enriched_city:
            region_probe = enriched.city or enriched_city
            # Providers commonly return the New Delhi city centroid without
            # its enclosing union-territory label.  The authoritative
            # boundary name is Delhi, so probe that exact boundary rather than
            # looking for a non-existent state named "New Delhi".
            if _normalise_query(requested_city) in {
                "delhi",
                "delhi ncr",
                "new delhi",
            }:
                region_probe = "Delhi"
            enriched_region = enriched.region or _photon_region_near_city(
                region_probe,
                lat=resolution.lat,
                lng=resolution.lng,
            )
        if (
            enriched
            and enriched.scope == INDIA
            and enriched_city
            and enriched_region
        ):
            canonical_region = enriched_region
            resolution = replace(
                resolution,
                region=canonical_region,
                source=f"{resolution.source}_region_enriched",
            )
    if not canonical_region:
        return PlaceResolution(
            AMBIGUOUS,
            display_name=resolution.display_name or query or requested_city,
            source="service_city_region_not_verified",
        )
    canonical_parts = [matched_city]
    if canonical_region and _normalise_query(canonical_region) != _normalise_query(matched_city):
        canonical_parts.append(canonical_region)
    canonical_parts.append("India")
    canonical_resolution = replace(
        resolution,
        display_name=", ".join(canonical_parts),
        country_code="in",
        city=matched_city,
        region=canonical_region,
        # The parent centroid is never evidence that an incident is inside
        # Dharamsala's smaller configured service polygon.
        in_dharamsala=False,
        source=f"{resolution.source}_service_city",
    )
    _save_cached_resolution(
        f"v10:{_primary_geocoder_provider()}:{_normalise_query(query)}",
        canonical_resolution,
    )
    return canonical_resolution


def _extract_place_with_model(message: str) -> PlaceReference:
    if not client:
        return PlaceReference(NONE, source="no_model")

    prompt = PromptCatalog.place_extraction(message)
    try:
        response = client.responses.create(
            model=config.OPENAI_GEOGRAPHY_MODEL,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "case_place_reference",
                    "schema": PLACE_REFERENCE_SCHEMA,
                    "strict": True,
                }
            },
            max_output_tokens=220,
        )
        payload = json.loads((response.output_text or "").strip())
        kind = str(payload.get("kind") or AMBIGUOUS)
        if kind not in {NAMED_PLACE, NEAR_ME, NONE, AMBIGUOUS}:
            kind = AMBIGUOUS
        place = str(payload.get("place") or "").strip()
        components = _coerce_place_components(payload.get("components"))
        if kind == NAMED_PLACE and not place and components:
            place = components.geocoder_query()
        if kind == NAMED_PLACE and not place:
            kind = AMBIGUOUS
        if kind == NAMED_PLACE and _starts_with_non_place_prefix(place):
            return PlaceReference(NONE, source="model_non_place")
        return PlaceReference(kind, place, source="model", components=components)
    except Exception as exc:  # noqa: BLE001 - location failures must degrade safely
        logger.warning("Current-message place extraction failed: %s", exc)
        return PlaceReference(AMBIGUOUS, source="model_error")


def _extract_prepositional_place(message: str) -> str:
    matches = list(LOCATION_PREPOSITION_RE.finditer(message))
    for match in reversed(matches):
        candidate = _clean_place_candidate(match.group(1))
        candidate = re.split(
            r"\b(?:who|what|where|which|why|how|can|could|should|would|please|"
            r"because|while|with|needing|and\s+(?:i|we|a|the|my|needs?|wants?|requires?)|"
            r"a\s+dog|the\s+dog)\b",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" ,")
        candidate = _clean_place_candidate(candidate)
        lowered = candidate.lower()
        if lowered in {
            "me",
            "here",
            "my area",
            "this area",
            "the area",
            "that area",
            "the same area",
            "that location",
            "the same location",
        }:
            continue
        if len(candidate) < 2 or _starts_with_non_place_prefix(candidate):
            continue
        if len(candidate.split()) > 8:
            continue
        return candidate
    return ""


def _matching_dharamsala_alias(text: str):
    for pattern, display_name, lat, lng in DHARAMSALA_ALIASES:
        if pattern.search(text):
            return pattern, display_name, lat, lng
    return None


def _matching_direct_dharamsala_alias(text: str, components=None):
    """A local name is not authority to overwrite its supplied state or address."""
    if not _can_use_direct_alias(components):
        return None
    alias = _matching_dharamsala_alias(text)
    if not alias:
        return None
    match = alias[0].search(text)
    before, after = text[:match.start()], text[match.end():]
    # Permit only an exact locality and known enclosing geography. Streets,
    # countries and different states must go through normal geocoding.
    residual = _normalise_query(before + " " + after)
    residual = re.sub(r"\b(?:dharamshala|dharamsala|himachal\s+pradesh|kangra|india|bharat|h\s*p|hp)\b", "", residual)
    if re.search(r"\w", residual):
        return None
    if alias[1].split(",", 1)[0] in {"Rakkar", "Khanyara", "Gamru", "Kharota"}:
        if not re.search(r"\b(?:dharamshala|dharamsala|himachal|kangra|hp)\b", before + " " + after, re.I):
            return None
    return alias


def is_dharamsala_alias(text: str) -> bool:
    """Return whether text contains a configured DAR service-area alias."""
    return bool(_matching_direct_dharamsala_alias(_clean_place_candidate(text)))


def _explicit_country_is_outside_india(country: str) -> bool:
    country_key = _normalise_query(country)
    return bool(country_key and country_key not in {"india", "bharat"})


def _can_use_direct_alias(components: PlaceComponents | None) -> bool:
    """Use a fixed DAR-area point only for a city/locality-only reference."""
    return not components or not any(
        (
            components.house_number,
            components.landmark,
            components.street,
            components.area,
        )
    )


def _alias_city(display_name: str) -> str:
    parts = [part.strip() for part in display_name.split(",") if part.strip()]
    if len(parts) > 1 and parts[0] in {"Rakkar", "Khanyara", "Gamru", "Kharota"}:
        return "Dharamshala"
    return parts[0] if parts else "Dharamshala"


def _clean_place_candidate(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip(" ,"))
    cleaned = CASE_DETAIL_SUFFIX_RE.sub("", cleaned).strip(" ,-\u2013\u2014")
    return re.sub(r"\b(?:and|but)\s*$", "", cleaned, flags=re.IGNORECASE).strip(" ,")


def _starts_with_non_place_prefix(value: str) -> bool:
    lowered = value.lower()
    for prefix in NON_PLACE_PREFIXES:
        if not re.match(rf"^{re.escape(prefix)}(?:\b|$)", lowered):
            continue
        if prefix in GENERIC_ADDRESS_PREFIXES and _address_query_is_qualified(value):
            return False
        return True
    return False


def _coerce_place_components(
    value: PlaceComponents | dict[str, str] | None,
) -> PlaceComponents | None:
    if isinstance(value, PlaceComponents):
        return value
    if not isinstance(value, dict):
        return None

    aliases = {
        "street": ("street", "road"),
        "area": ("area", "locality", "neighbourhood", "neighborhood", "suburb"),
        "postal_code": ("postal_code", "postcode", "pincode"),
    }
    fields: dict[str, str] = {}
    for field in PLACE_COMPONENT_FIELDS:
        keys = aliases.get(field, (field,))
        fields[field] = next(
            (
                re.sub(r"\s+", " ", str(value.get(key) or "")).strip(" ,")
                for key in keys
                if str(value.get(key) or "").strip()
            ),
            "",
        )
    components = PlaceComponents(**fields)
    return components if any(getattr(components, field) for field in PLACE_COMPONENT_FIELDS) else None


def _normalise_query(value: str) -> str:
    return (
        re.sub(r"[^\w]+", " ", value.lower(), flags=re.UNICODE)
        .replace("_", " ")
        .strip()
    )


def _matching_city_label(
    requested_city: str,
    resolution: PlaceResolution | None,
) -> str:
    if not resolution or resolution.scope not in {INDIA, OUTSIDE_INDIA}:
        return ""
    requested = _normalise_query(requested_city)
    if not requested:
        return ""

    labels: list[str] = []
    for label in (
        resolution.city,
        *[part.strip() for part in resolution.display_name.split(",")],
    ):
        normalised = _normalise_query(label)
        if label and normalised and normalised not in {
            _normalise_query(existing) for existing in labels
        }:
            labels.append(label.strip())

    exact = next((label for label in labels if _normalise_query(label) == requested), "")
    if exact:
        return exact

    scored = [
        (SequenceMatcher(None, requested, _normalise_query(label)).ratio(), label)
        for label in labels
    ]
    scored.sort(reverse=True)
    return scored[0][1] if scored and scored[0][0] >= config.PLACE_FUZZY_MIN_SCORE else ""


def _region_label_from_display(display_name: str, city: str) -> str:
    parts = [part.strip() for part in display_name.split(",") if part.strip()]
    for index, part in enumerate(parts):
        if _normalise_query(part) != _normalise_query(city):
            continue
        trailing = [
            candidate
            for candidate in parts[index + 1 :]
            if _normalise_query(candidate) not in {"india", "bharat"}
            and not re.fullmatch(r"\d{4,6}", candidate)
        ]
        return trailing[-1] if trailing else ""
    return ""


def _canonical_region_for_verified_city(
    city: str,
    *,
    lat: float | None,
    lng: float | None,
) -> str:
    """Fill stable city/UT identity only after a provider verifies its centroid."""
    try:
        parsed_lat = float(lat)
        parsed_lng = float(lng)
    except (TypeError, ValueError):
        return ""

    # New Delhi is a city inside the Delhi union territory, but several
    # geocoders omit the state/UT field for its city centroid.  Requiring both
    # the exact city alias and a coordinate inside Delhi prevents a supplied
    # user state from becoming the NGO cache boundary.
    if (
        _normalise_query(city) in {"delhi", "delhi ncr", "new delhi"}
        and 28.38 <= parsed_lat <= 28.90
        and 76.80 <= parsed_lng <= 77.36
    ):
        return "Delhi"
    return ""


def _looks_like_address_query(
    value: str,
    components: PlaceComponents | None = None,
) -> bool:
    if ADDRESS_QUERY_RE.search(value):
        return True
    if not components:
        return False
    normalised = _normalise_query(value)
    return any(
        component and _normalise_query(component) in normalised
        for component in (
            components.house_number,
            components.street,
            components.area,
            components.postal_code,
        )
    )


def _address_query_is_qualified(
    value: str,
    components: PlaceComponents | None = None,
) -> bool:
    """Require address context before allowing a road/postcode lookup.

    A common street such as ``Main Road`` cannot safely establish country by
    itself. A parent locality, three-part address, postcode, or structured
    component hierarchy makes the lookup sufficiently constrained.
    """
    if not _looks_like_address_query(value, components):
        return False
    if re.search(r"\b\d{6}\b", value):
        return True
    if len([part for part in value.split(",") if part.strip()]) >= 2:
        return True
    if components and any(
        (part or "").strip()
        for part in (
            components.city,
            components.district,
            components.state,
            components.postal_code,
        )
    ):
        return True
    return len(_place_tokens(value)) >= 3


def _geocoder_query_variants(
    value: str,
    *,
    components: PlaceComponents | None = None,
) -> list[str]:
    variants: list[str] = []

    def append(candidate: str) -> None:
        cleaned = _clean_place_candidate(candidate)
        if cleaned and _normalise_query(cleaned) not in {
            _normalise_query(existing) for existing in variants
        }:
            variants.append(cleaned)

    append(value)
    without_ncr = re.sub(
        r"(?:,?\s*)\b(?:ncr|national capital region)\b",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" ,")
    append(without_ncr)

    if components:
        country = components.country
        append(", ".join(part for part in (
            components.area,
            components.city,
            components.district,
            components.state,
            components.postal_code,
            country,
        ) if part))
        append(", ".join(part for part in (
            components.city,
            components.district,
            components.state,
            components.postal_code,
            country,
        ) if part))
        append(", ".join(part for part in (
            components.district,
            components.state,
            country,
        ) if part))

    # When a street-level query cannot be found, progressively verify its
    # explicitly supplied parent locality/city rather than accepting a POI.
    if _looks_like_address_query(value, components):
        comma_parts = [part.strip() for part in value.split(",") if part.strip()]
        for start in range(1, len(comma_parts)):
            append(", ".join(comma_parts[start:]))
    return variants


def _has_explicit_india_qualifier(value: str) -> bool:
    return bool(re.search(r"\b(?:india|bharat)\b", value, re.IGNORECASE))


def _outside_result_is_explicit_match(
    query: str,
    components: PlaceComponents | None,
    result: dict,
) -> bool:
    """Keep an explicitly qualified foreign place authoritative.

    A bare misspelling can collide with a small foreign settlement (for
    example ``Shilong`` in China). A supplied country/state, or a multi-part
    query that is fully present in the foreign result, is explicit enough that
    an India-only spelling correction must not override it.
    """
    if components:
        country = _normalise_query(components.country)
        if country and country not in {"india", "bharat"}:
            return True

        display = _normalise_query(str(result.get("display_name") or ""))
        city = _normalise_query(components.city)
        state = _normalise_query(components.state)
        if city and state and city in display and state in display:
            return True

    query_tokens = _place_tokens(query)
    display_tokens = set(_place_tokens(str(result.get("display_name") or "")))
    return len(query_tokens) >= 2 and all(token in display_tokens for token in query_tokens)


def _valid_india_hint(
    lat: float | None,
    lng: float | None,
) -> tuple[float, float] | None:
    if lat is None or lng is None:
        return None
    try:
        parsed_lat = float(lat)
        parsed_lng = float(lng)
    except (TypeError, ValueError):
        return None
    if not location.is_in_india(parsed_lat, parsed_lng):
        return None
    return parsed_lat, parsed_lng


def _needs_locality_qualification(query: str, result: dict) -> bool:
    """Require context for a bare small locality instead of guessing silently."""
    tokens = _place_tokens(query)
    address_type = str(result.get("addresstype") or result.get("type") or "").lower()
    return len(tokens) == 1 and address_type in SMALL_LOCALITY_ADDRESS_TYPES


def _place_tokens(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[^\W_]+", value.lower(), flags=re.UNICODE)
        if token not in PLACE_STOP_WORDS
    ]


def _resolve_fuzzy_indian_place(
    place: str,
    *,
    hint_lat: float | None = None,
    hint_lng: float | None = None,
    require_nearby_hint: bool = False,
) -> PlaceResolution | None:
    if not config.PLACE_FUZZY_GEOCODING_ENABLED:
        return None

    query_tokens = _place_tokens(place)
    if not query_tokens:
        return None

    features = _photon_search(place, hint_lat=hint_lat, hint_lng=hint_lng)
    if not features:
        return _resolve_verified_india_postal_place(place, [])

    candidates: list[tuple[float, float | None, float, dict]] = []
    for feature in features:
        properties = feature.get("properties") if isinstance(feature, dict) else None
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            continue
        if str(properties.get("countrycode") or "").upper() != "IN":
            continue

        score = _photon_match_score(place, properties)
        if score < config.PLACE_FUZZY_MIN_SCORE:
            continue

        coordinates = geometry.get("coordinates")
        try:
            lng = float(coordinates[0])
            lat = float(coordinates[1])
        except (TypeError, ValueError, IndexError):
            continue

        distance = None
        if hint_lat is not None and hint_lng is not None:
            distance = location.haversine_distance(hint_lat, hint_lng, lat, lng)

        candidate_type = str(properties.get("type") or "").lower()
        is_settlement = candidate_type in PHOTON_SETTLEMENT_TYPES
        has_parent_context = len(query_tokens) >= 2
        is_near_hint = distance is not None and distance <= config.PLACE_LOCALITY_HINT_MAX_KM
        is_small_locality = candidate_type in SMALL_LOCALITY_ADDRESS_TYPES | {"county"}
        if require_nearby_hint and not is_near_hint:
            continue
        if len(query_tokens) == 1 and is_small_locality and not is_near_hint:
            continue
        if not (is_settlement or has_parent_context or is_near_hint):
            continue

        quality = _photon_candidate_quality(properties)
        candidates.append((score, distance, quality, feature))

    if not candidates and len(query_tokens) == 1:
        postal_resolution = _resolve_verified_india_postal_place(place, features)
        if postal_resolution:
            return postal_resolution

    if not candidates:
        return None

    if hint_lat is not None and hint_lng is not None:
        nearby = [
            candidate
            for candidate in candidates
            if candidate[1] is not None
            and candidate[1] <= config.PLACE_LOCALITY_HINT_MAX_KM
        ]
        if nearby:
            candidates = nearby
            candidates.sort(key=lambda candidate: (candidate[1], -candidate[0], -candidate[2]))
            source = "photon_fuzzy_browser_hint"
        else:
            candidates.sort(
                key=lambda candidate: (
                    -candidate[0],
                    -candidate[2],
                    candidate[1] or float("inf"),
                )
            )
            source = "photon_fuzzy"
    else:
        candidates.sort(key=lambda candidate: (-candidate[0], -candidate[2]))
        source = "photon_fuzzy"

    return _resolution_from_photon(candidates[0][3], place, source=source)


def _resolve_verified_india_postal_place(
    place: str,
    features: list[dict],
) -> PlaceResolution | None:
    postal_place = _lookup_unique_india_postal_place(place)
    if not postal_place:
        return None

    verified_features = [
        feature
        for feature in features
        if _photon_agrees_with_postal_place(feature, postal_place)
    ]
    if verified_features:
        verified_features.sort(
            key=lambda feature: -_photon_candidate_quality(feature.get("properties", {}))
        )
        return _resolution_from_postal_place(
            postal_place,
            feature=verified_features[0],
            source="india_postal_photon",
        )
    return _resolution_from_postal_place(postal_place, source="india_postal")


def _lookup_unique_india_postal_place(place: str) -> IndiaPostalPlace | None:
    """Verify a bare locality against India's post-office directory.

    This fallback is deliberately exact and only accepts one unambiguous
    district/state/PIN match. It improves recall for villages that map
    geocoders expose only as a station, post office, or other feature.
    """
    if not config.INDIA_POSTAL_LOOKUP_ENABLED:
        return None

    cleaned = re.sub(r"\s+", " ", (place or "").strip(" ,"))
    if not cleaned or len(_place_tokens(cleaned)) != 1:
        return None

    global _last_postal_lookup_request
    url = config.INDIA_POSTAL_LOOKUP_URL.format(place=quote(cleaned, safe=""))
    try:
        with _geocoder_slot(_postal_lookup_lock, _last_postal_lookup_request, 0.25):
            response = _bounded_geocoder_get(
                url,
                headers={"User-Agent": config.PLACE_GEOCODER_USER_AGENT},
                timeout=config.INDIA_POSTAL_LOOKUP_TIMEOUT_SECONDS,
            )
            _last_postal_lookup_request = time.monotonic()
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - unresolved places get a clarification
        logger.warning("India postal lookup failed for %r: %s", cleaned, exc)
        return None

    matches: dict[tuple[str, str, str, str], IndiaPostalPlace] = {}
    if not isinstance(payload, list):
        return None
    for envelope in payload:
        if not isinstance(envelope, dict) or envelope.get("Status") != "Success":
            continue
        records = envelope.get("PostOffice")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            name = str(record.get("Name") or "").strip()
            district = str(record.get("District") or "").strip()
            state = str(record.get("State") or "").strip()
            country = str(record.get("Country") or "").strip()
            pincode = str(record.get("Pincode") or "").strip()
            if (
                _normalise_query(name) != _normalise_query(cleaned)
                or country.lower() != "india"
                or not district
                or not state
                or not re.fullmatch(r"\d{6}", pincode)
            ):
                continue
            match = IndiaPostalPlace(name, district, state, pincode)
            key = tuple(
                _normalise_query(value)
                for value in (name, district, state, pincode)
            )
            matches[key] = match

    return next(iter(matches.values())) if len(matches) == 1 else None


def _photon_agrees_with_postal_place(
    feature: dict,
    postal_place: IndiaPostalPlace,
) -> bool:
    properties = feature.get("properties") if isinstance(feature, dict) else None
    if not isinstance(properties, dict):
        return False
    if str(properties.get("countrycode") or "").upper() != "IN":
        return False
    if _normalise_query(str(properties.get("name") or "")) != _normalise_query(
        postal_place.name
    ):
        return False
    if _normalise_query(str(properties.get("state") or "")) != _normalise_query(
        postal_place.state
    ):
        return False
    photon_postcode = str(properties.get("postcode") or "").strip()
    return not photon_postcode or photon_postcode == postal_place.pincode


def _resolution_from_postal_place(
    postal_place: IndiaPostalPlace,
    *,
    feature: dict | None = None,
    source: str,
) -> PlaceResolution:
    lat = None
    lng = None
    if feature:
        coordinates = feature.get("geometry", {}).get("coordinates", [])
        try:
            lng = float(coordinates[0])
            lat = float(coordinates[1])
        except (TypeError, ValueError, IndexError):
            lat = None
            lng = None

    display_name = ", ".join(
        (
            postal_place.name,
            postal_place.district,
            postal_place.state,
            postal_place.pincode,
            "India",
        )
    )
    return PlaceResolution(
        INDIA,
        display_name=display_name,
        lat=lat,
        lng=lng,
        country_code="in",
        source=source,
        in_dharamsala=(
            lat is not None
            and lng is not None
            and location.is_in_dharamsala_region(lat, lng)
        ),
        city=postal_place.name,
        region=postal_place.state,
    )


def _photon_match_score(query: str, properties: dict) -> float:
    query_tokens = _place_tokens(query)
    fields = [
        str(properties.get(field) or "")
        for field in ("name", "city", "district", "county", "state", "country")
    ]
    candidate_tokens = _place_tokens(" ".join(fields))
    if not candidate_tokens:
        return 0.0

    token_scores = [
        max(SequenceMatcher(None, query_token, candidate_token).ratio() for candidate_token in candidate_tokens)
        for query_token in query_tokens
    ]
    coverage = sum(token_scores) / len(token_scores)
    name = _normalise_query(str(properties.get("name") or ""))
    name_similarity = SequenceMatcher(None, _normalise_query(query), name).ratio() if name else 0.0

    # A bare typo must resemble the complete settlement/region name, not just
    # one token inside it. Otherwise "Laddak" can score highly against the
    # first word of an unrelated city such as "Ladda Khothi".
    candidate_type = str(properties.get("type") or "").lower()
    if len(query_tokens) == 1 and candidate_type in PHOTON_SETTLEMENT_TYPES | PHOTON_REGION_TYPES:
        return name_similarity
    return max(coverage, name_similarity)


def _photon_candidate_quality(properties: dict) -> float:
    candidate_type = str(properties.get("type") or "").lower()
    if candidate_type in PHOTON_SETTLEMENT_TYPES:
        return 3.0
    city_tokens = _place_tokens(str(properties.get("city") or ""))
    if city_tokens:
        return 2.0 + (1.0 / len(city_tokens))
    return 1.0


def _photon_search(
    place: str,
    *,
    hint_lat: float | None = None,
    hint_lng: float | None = None,
) -> list[dict]:
    global _last_fuzzy_geocoder_request
    # This helper is only used for India verification. Bounding the upstream
    # candidate set prevents similarly named foreign places from crowding a
    # corrected Indian city out of Photon's small result window.
    params: dict[str, object] = {
        "q": place,
        "limit": 10,
        "lang": "en",
        "bbox": "68.0,6.0,98.0,38.0",
    }
    if hint_lat is not None and hint_lng is not None:
        params.update({"lat": hint_lat, "lon": hint_lng})

    try:
        with _geocoder_slot(_fuzzy_geocoder_lock, _last_fuzzy_geocoder_request, 1.05):
            response = _bounded_geocoder_get(
                config.PLACE_FUZZY_GEOCODER_URL,
                params=params,
                headers={"User-Agent": config.PLACE_GEOCODER_USER_AGENT},
                timeout=config.PLACE_GEOCODER_TIMEOUT_SECONDS,
            )
            _last_fuzzy_geocoder_request = time.monotonic()
        response.raise_for_status()
        payload = response.json()
        features = payload.get("features") if isinstance(payload, dict) else None
        return features if isinstance(features, list) else []
    except Exception as exc:  # noqa: BLE001 - callers return a clarification instead
        logger.warning("Fuzzy place geocoding failed for %r: %s", place, exc)
        return []


def _resolution_from_photon(feature: dict, query: str, *, source: str) -> PlaceResolution:
    properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    coordinates = feature.get("geometry", {}).get("coordinates", [])
    try:
        lng = float(coordinates[0])
        lat = float(coordinates[1])
    except (TypeError, ValueError, IndexError):
        return PlaceResolution(AMBIGUOUS, display_name=query, source="invalid_fuzzy_result")

    display_name = _photon_display_name(query, properties)
    candidate_type = str(properties.get("type") or "").lower()
    city = str(properties.get("city") or "").strip()
    if not city and candidate_type in {"city", "town", "village", "municipality"}:
        city = str(properties.get("name") or "").strip()
    region = str(properties.get("state") or "").strip()
    if not region and candidate_type in PHOTON_REGION_TYPES:
        region = str(properties.get("name") or "").strip()
    return PlaceResolution(
        INDIA,
        display_name=display_name,
        lat=lat,
        lng=lng,
        country_code="in",
        source=source,
        in_dharamsala=location.is_in_dharamsala_region(lat, lng),
        city=city,
        region=region,
    )


def _photon_region_near_city(
    region_label: str,
    *,
    lat: float | None,
    lng: float | None,
) -> str:
    """Verify a city-state/UT label from a nearby Photon boundary feature."""
    if lat is None or lng is None or not region_label:
        return ""
    expected = _normalise_query(region_label)
    for feature in _photon_search(region_label, hint_lat=lat, hint_lng=lng):
        properties = feature.get("properties") if isinstance(feature, dict) else None
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            continue
        if str(properties.get("countrycode") or "").upper() != "IN":
            continue
        candidate_type = str(properties.get("type") or "").lower()
        osm_value = str(properties.get("osm_value") or "").lower()
        if candidate_type not in PHOTON_REGION_TYPES and osm_value not in {
            "state",
            "province",
            "union_territory",
        }:
            continue
        name = str(properties.get("name") or "").strip()
        if _normalise_query(name) != expected:
            continue
        coordinates = geometry.get("coordinates") or []
        try:
            candidate_lng = float(coordinates[0])
            candidate_lat = float(coordinates[1])
        except (TypeError, ValueError, IndexError):
            continue
        if location.haversine_distance(lat, lng, candidate_lat, candidate_lng) <= 100:
            return name
    return ""


def _photon_display_name(query: str, properties: dict) -> str:
    candidate_type = str(properties.get("type") or "").lower()
    name = str(properties.get("name") or "").strip()
    city = str(properties.get("city") or "").strip()
    state = str(properties.get("state") or "").strip()

    if candidate_type not in PHOTON_SETTLEMENT_TYPES:
        query_tokens = _place_tokens(query)
        city_tokens = _place_tokens(city)
        city_matches_leading_locality = bool(
            query_tokens
            and city_tokens
            and max(
                SequenceMatcher(None, query_tokens[0], city_token).ratio()
                for city_token in city_tokens
            ) >= config.PLACE_FUZZY_MIN_SCORE
        )
        if city_matches_leading_locality:
            name = city
        else:
            comma_parts = [part.strip() for part in name.split(",") if part.strip()]
            scored_parts = [
                (
                    max(
                        SequenceMatcher(None, query_tokens[0], part_token).ratio()
                        for part_token in _place_tokens(part)
                    ),
                    part,
                )
                for part in comma_parts
                if query_tokens and _place_tokens(part)
            ]
            scored_parts.sort(reverse=True)
            name = scored_parts[0][1] if scored_parts and scored_parts[0][0] >= config.PLACE_FUZZY_MIN_SCORE else city or query

    parts: list[str] = []
    for part in (name, city, state, "India"):
        if part and _normalise_query(part) not in {_normalise_query(existing) for existing in parts}:
            parts.append(part)
    return ", ".join(parts)


def _resolve_locationiq_india_correction(
    place: str,
    *,
    allow_region: bool,
) -> PlaceResolution | None:
    """Correct a likely Indian city/region spelling, then verify it exactly.

    Autocomplete supplies candidates only. The selected spelling is re-run
    through the India-constrained forward geocoder before it can establish
    country scope or become an NGO coverage city.
    """
    candidates = _locationiq_autocomplete_search(place)
    scored: list[tuple[float, str, str, dict]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_type = str(candidate.get("type") or "").strip().lower()
        is_city = candidate_type in LOCATIONIQ_AUTOCOMPLETE_CITY_TYPES
        is_region = allow_region and candidate_type in LOCATIONIQ_AUTOCOMPLETE_REGION_TYPES
        if not (is_city or is_region):
            continue

        address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
        country_code = str(address.get("country_code") or "").strip().lower()
        country = _normalise_query(str(address.get("country") or ""))
        if country_code and country_code != "in":
            continue
        if not country_code and country not in {"india", "bharat"}:
            continue

        try:
            lat = float(candidate["lat"])
            lng = float(candidate["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not location.is_in_india(lat, lng):
            continue

        label = _locationiq_autocomplete_label(candidate, candidate_type)
        if not label:
            continue
        score = _locationiq_autocomplete_match_score(place, candidate)
        minimum = (
            LOCATIONIQ_AUTOCOMPLETE_CITY_MIN_SCORE
            if is_city
            else LOCATIONIQ_AUTOCOMPLETE_REGION_MIN_SCORE
        )
        if score >= minimum:
            scored.append((score, candidate_type, label, candidate))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    if (
        len(scored) > 1
        and scored[0][0] - scored[1][0] < LOCATIONIQ_AUTOCOMPLETE_MIN_MARGIN
    ):
        return None

    _, candidate_type, label, candidate = scored[0]
    address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
    state = str(address.get("state") or "").strip()
    canonical_parts = [label]
    if (
        candidate_type in LOCATIONIQ_AUTOCOMPLETE_CITY_TYPES
        and state
        and _normalise_query(state) != _normalise_query(label)
    ):
        canonical_parts.append(state)
    canonical_parts.append("India")
    canonical_query = ", ".join(canonical_parts)

    verified_result, _ = _nominatim_place_search(
        canonical_query,
        country_code="in",
    )
    if not verified_result:
        if candidate_type in LOCATIONIQ_AUTOCOMPLETE_REGION_TYPES:
            return _verify_locationiq_region_with_photon(label, canonical_query)
        return None
    verified = _resolution_from_geocoder(
        verified_result,
        source="locationiq_autocomplete_verified",
    )
    if (
        verified.scope != INDIA
        or verified.lat is None
        or verified.lng is None
        or not location.is_in_india(verified.lat, verified.lng)
    ):
        return None

    verified_type = str(
        verified_result.get("addresstype") or verified_result.get("type") or ""
    ).lower()
    if candidate_type in LOCATIONIQ_AUTOCOMPLETE_CITY_TYPES:
        matched_city = _matching_city_label(label, verified)
        if not matched_city or verified_type not in GEOGRAPHIC_ADDRESS_TYPES:
            return None
        return replace(verified, city=matched_city)

    if verified_type not in LOCATIONIQ_AUTOCOMPLETE_REGION_TYPES:
        return None
    region = verified.region or str(verified_result.get("name") or "").strip()
    if SequenceMatcher(
        None,
        _normalise_query(label),
        _normalise_query(region),
    ).ratio() < LOCATIONIQ_AUTOCOMPLETE_REGION_MIN_SCORE:
        return None
    return replace(verified, city="", region=region)


def _verify_locationiq_region_with_photon(
    label: str,
    canonical_query: str,
) -> PlaceResolution | None:
    """Cross-check a LocationIQ state suggestion when forward search omits it."""
    for feature in _photon_search(canonical_query):
        properties = feature.get("properties") if isinstance(feature, dict) else None
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            continue
        if str(properties.get("countrycode") or "").upper() != "IN":
            continue
        if str(properties.get("type") or "").lower() not in PHOTON_REGION_TYPES:
            continue
        name = str(properties.get("name") or "").strip()
        if SequenceMatcher(
            None,
            _normalise_query(label),
            _normalise_query(name),
        ).ratio() < 0.95:
            continue
        resolution = _resolution_from_photon(
            feature,
            canonical_query,
            source="locationiq_autocomplete_photon_verified",
        )
        if (
            resolution.scope == INDIA
            and resolution.lat is not None
            and resolution.lng is not None
            and location.is_in_india(resolution.lat, resolution.lng)
        ):
            return replace(resolution, city="", region=name)
    return None


def _locationiq_autocomplete_search(place: str) -> list[dict]:
    if _primary_geocoder_provider() != "locationiq" or not config.LOCATIONIQ_API_KEY:
        return []

    global _last_geocoder_request
    params = {
        "key": config.LOCATIONIQ_API_KEY,
        "q": place,
        "countrycodes": "in",
        "layers": "city,state",
        "normalizecity": 1,
        "dedupe": 1,
        "limit": 10,
        "accept-language": "en",
    }
    try:
        with _geocoder_slot(_geocoder_lock, _last_geocoder_request, 1.05):
            response = _bounded_geocoder_get(
                config.LOCATIONIQ_AUTOCOMPLETE_URL,
                params=params,
                headers={"User-Agent": config.PLACE_GEOCODER_USER_AGENT},
                timeout=config.PLACE_GEOCODER_TIMEOUT_SECONDS,
            )
            _last_geocoder_request = time.monotonic()
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except Exception as exc:  # noqa: BLE001 - unresolved places get clarification
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning(
            "Place autocomplete failed provider=locationiq place=%r status=%s error=%s",
            place,
            status_code if status_code is not None else "none",
            type(exc).__name__,
        )
        return []


def _locationiq_autocomplete_label(candidate: dict, candidate_type: str) -> str:
    address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
    if candidate_type in LOCATIONIQ_AUTOCOMPLETE_CITY_TYPES:
        fields = ("city", "town", "municipality", "village")
    else:
        fields = ("state", "region")
    for field in fields:
        value = str(address.get(field) or "").strip()
        if value:
            return value
    display_place = str(candidate.get("display_place") or "").strip()
    if display_place:
        return display_place
    return str(candidate.get("display_name") or "").split(",", 1)[0].strip()


def _locationiq_autocomplete_match_score(place: str, candidate: dict) -> float:
    query_tokens = _place_tokens(place)
    if not query_tokens:
        return 0.0
    address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
    values = [
        str(candidate.get("display_place") or ""),
        str(candidate.get("display_name") or ""),
        *[
            str(address.get(field) or "")
            for field in ("city", "town", "municipality", "state", "region")
        ],
    ]
    candidate_tokens = _place_tokens(" ".join(values))
    if not candidate_tokens:
        return 0.0
    token_scores = [
        max(
            SequenceMatcher(None, query_token, candidate_token).ratio()
            for candidate_token in candidate_tokens
        )
        for query_token in query_tokens
    ]
    return sum(token_scores) / len(token_scores)


def _nominatim_place_search(
    place: str,
    *,
    country_code: str = "",
    allow_address: bool = False,
) -> tuple[dict | None, str]:
    provider = _primary_geocoder_provider()
    if allow_address:
        address_result = (
            _nominatim_address_search(place, country_code=country_code)
            if country_code
            else _nominatim_address_search(place)
        )
        if address_result:
            return address_result, f"{provider}_address"
    settlement_result = (
        _nominatim_search(place, country_code=country_code)
        if country_code
        else _nominatim_search(place)
    )
    return settlement_result, provider


def _nominatim_search(place: str, country_code: str = "") -> dict | None:
    """Search only settlement-level results, preserving the legacy path."""
    params = {
        "q": place,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 5,
        "featureType": "settlement",
        "accept-language": "en",
    }
    if country_code:
        params["countrycodes"] = country_code
    return _nominatim_request(place, country_code, params, _is_geographic_place_result)


def _nominatim_address_search(place: str, country_code: str = "") -> dict | None:
    """Search geographic address anchors without allowing businesses or POIs."""
    params = {
        "q": place,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 10,
        "accept-language": "en",
    }
    if country_code:
        params["countrycodes"] = country_code
    return _nominatim_request(place, country_code, params, _is_address_place_result)


def _nominatim_request(
    place: str,
    country_code: str,
    params: dict[str, object],
    candidate_filter,
) -> dict | None:
    global _last_geocoder_request
    provider = _primary_geocoder_provider()
    if provider == "locationiq" and not config.LOCATIONIQ_API_KEY:
        logger.error("LocationIQ is selected but LOCATIONIQ_API_KEY is not configured")
        return None

    request_params = dict(params)
    request_url = config.PLACE_GEOCODER_URL
    if provider == "locationiq":
        request_url = config.LOCATIONIQ_SEARCH_URL
        request_params.update(
            {
                "key": config.LOCATIONIQ_API_KEY,
                "format": "json",
                "normalizeaddress": 1,
                "dedupe": 1,
            }
        )
    try:
        with _geocoder_slot(_geocoder_lock, _last_geocoder_request, 1.05):
            response = _bounded_geocoder_get(
                request_url,
                params=request_params,
                headers={"User-Agent": config.PLACE_GEOCODER_USER_AGENT},
                timeout=config.PLACE_GEOCODER_TIMEOUT_SECONDS,
            )
            _last_geocoder_request = time.monotonic()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            return None
        for candidate in payload:
            if candidate_filter(candidate):
                return candidate
        return None
    except Exception as exc:  # noqa: BLE001 - callers return a clarification instead
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning(
            "Place geocoding failed provider=%s place=%r country=%s status=%s error=%s",
            provider,
            place,
            country_code,
            status_code if status_code is not None else "none",
            type(exc).__name__,
        )
        return None


def _primary_geocoder_provider() -> str:
    provider = str(config.PLACE_GEOCODER_PROVIDER or "").strip().lower()
    if provider in {"locationiq", "nominatim"}:
        return provider
    return "locationiq" if config.LOCATIONIQ_API_KEY else "nominatim"


def _is_geographic_place_result(candidate: object) -> bool:
    if not isinstance(candidate, dict):
        return False
    address_type = _candidate_geographic_type(candidate)
    return address_type in GEOGRAPHIC_ADDRESS_TYPES


def _candidate_geographic_type(candidate: dict) -> str:
    """Normalise settlement types across Nominatim and LocationIQ.

    LocationIQ can return a real city with ``type=administrative`` and no
    ``addresstype`` even when ``featureType=settlement`` was requested.  Only
    infer a settlement type when the structured address itself contains that
    settlement component; this keeps businesses and other POIs excluded.
    """
    address_type = str(candidate.get("addresstype") or candidate.get("type") or "").lower()
    if address_type != "administrative" or candidate.get("addresstype"):
        return address_type

    address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
    for field in (
        "city",
        "town",
        "municipality",
        "village",
        "hamlet",
        "suburb",
        "neighbourhood",
        "quarter",
        "district",
    ):
        if str(address.get(field) or "").strip():
            return field
    return address_type


def _is_address_place_result(candidate: object) -> bool:
    if not isinstance(candidate, dict):
        return False
    address_type = str(candidate.get("addresstype") or candidate.get("type") or "").lower()
    if address_type not in ADDRESS_GEOGRAPHIC_TYPES:
        return False

    category = str(candidate.get("category") or candidate.get("class") or "").lower()
    if category in {
        "amenity",
        "building",
        "craft",
        "healthcare",
        "leisure",
        "office",
        "shop",
        "tourism",
    }:
        return False
    if address_type == "road" and category and category not in {"highway", "place"}:
        return False
    if address_type == "postcode" and category and category not in {"boundary", "place"}:
        return False
    return True


def _resolution_from_geocoder(
    result: dict,
    *,
    source: str = "nominatim",
) -> PlaceResolution:
    address = result.get("address") if isinstance(result.get("address"), dict) else {}
    country_code = str(address.get("country_code") or "").lower()
    try:
        lat = float(result["lat"])
        lng = float(result["lon"])
    except (KeyError, TypeError, ValueError):
        return PlaceResolution(AMBIGUOUS, source="invalid_geocoder_result")

    scope = INDIA if country_code == "in" else OUTSIDE_INDIA if country_code else AMBIGUOUS
    city = next(
        (
            str(address.get(field) or "").strip()
            for field in (
                "city",
                "town",
                "municipality",
                "village",
                "city_district",
            )
            if str(address.get(field) or "").strip()
        ),
        "",
    )
    address_type = str(result.get("addresstype") or result.get("type") or "").lower()
    if not city and address_type in {"city", "town", "municipality", "village"}:
        city = str(result.get("name") or "").strip()
        if not city:
            city = str(result.get("display_name") or "").split(",", 1)[0].strip()
    return PlaceResolution(
        scope,
        display_name=str(result.get("display_name") or "").strip(),
        lat=lat,
        lng=lng,
        country_code=country_code,
        source=source,
        in_dharamsala=scope == INDIA and location.is_in_dharamsala_region(lat, lng),
        city=city,
        region=str(address.get("state") or address.get("region") or "").strip(),
    )


def _get_cached_resolution(query_key: str) -> PlaceResolution | None:
    try:
        cached = db.get_place_resolution(query_key, _place_cache_days())
    except Exception as exc:  # noqa: BLE001 - cache failure must not break chat
        logger.warning("Place cache read failed for %r: %s", query_key, exc)
        return None
    if not cached:
        return None
    resolution = PlaceResolution(
        scope=str(cached.get("scope") or AMBIGUOUS),
        display_name=str(cached.get("display_name") or ""),
        lat=cached.get("lat"),
        lng=cached.get("lng"),
        country_code=str(cached.get("country_code") or ""),
        source="cache",
        in_dharamsala=(
            cached.get("lat") is not None
            and cached.get("lng") is not None
            and location.is_in_dharamsala_region(float(cached["lat"]), float(cached["lng"]))
        ),
        city=str(cached.get("city") or ""),
        region=str(cached.get("region") or ""),
    )
    if resolution.scope == INDIA and resolution.city and not resolution.region:
        logger.info("Ignoring incomplete Indian city cache entry key=%s", query_key)
        return None
    return resolution


def _place_cache_days() -> int:
    if _primary_geocoder_provider() == "locationiq":
        return min(config.PLACE_GEOCODER_CACHE_DAYS, config.LOCATIONIQ_CACHE_DAYS)
    return config.PLACE_GEOCODER_CACHE_DAYS


def _save_cached_resolution(query_key: str, resolution: PlaceResolution) -> None:
    if resolution.scope == INDIA and resolution.city and not resolution.region:
        # An incomplete city record is useful for a one-off scope decision but
        # becomes harmful when reused as the city-level NGO cache boundary.
        logger.info("Not caching Indian city without state/UT key=%s", query_key)
        return
    try:
        db.save_place_resolution(
            query_key,
            {
                "scope": resolution.scope,
                "display_name": resolution.display_name,
                "city": resolution.city,
                "region": resolution.region,
                "lat": resolution.lat,
                "lng": resolution.lng,
                "country_code": resolution.country_code,
                "source": resolution.source,
            },
        )
    except Exception as exc:  # noqa: BLE001 - cache failure must not break chat
        logger.warning("Place cache write failed for %r: %s", query_key, exc)
