"""Deterministic answers over the latest verified NGO result snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Iterable
from urllib.parse import urlparse


FOLLOW_UP_RE = re.compile(
    r"\b(?:it|its|they|them|their|those|that one|this one|which one|first|1st|"
    r"second|2nd|third|3rd|another|more|closest|nearest|distance|phone|number|"
    r"call|contact|address|location|website|link|email|hours?|open|rescue)\b",
    re.IGNORECASE,
)
CONTACT_RE = re.compile(
    r"\b(?:phone|number|call|contact|address|location|website|link|email)\b",
    re.IGNORECASE,
)
HOURS_RE = re.compile(r"\b(?:hours?|open|close[sd]?)\b", re.IGNORECASE)
CLOSEST_RE = re.compile(r"\b(?:closest|nearest|distance|how far)\b", re.IGNORECASE)
MORE_RE = re.compile(r"\b(?:another|more)\b", re.IGNORECASE)

ORDINALS = (
    (re.compile(r"\b(?:first|1st|number\s+1)\b", re.IGNORECASE), 0),
    (re.compile(r"\b(?:second|2nd|number\s+2)\b", re.IGNORECASE), 1),
    (re.compile(r"\b(?:third|3rd|number\s+3)\b", re.IGNORECASE), 2),
)


@dataclass(frozen=True)
class FollowUpAnswer:
    response: str
    resource_links: list[dict[str, str]] = field(default_factory=list)
    organizations: list[dict[str, str]] = field(default_factory=list)
    selected_ngo_index: int | None = None
    service_city: str = ""


def answer_from_history(
    message: str,
    history: Iterable[dict],
    *,
    language: str = "en",
) -> FollowUpAnswer | None:
    """Answer a true follow-up without rerunning or hallucinating NGO research."""
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text or not FOLLOW_UP_RE.search(text):
        return None

    context = _latest_context(history)
    if not context:
        return None
    organizations, prior_selection, service_city = context

    selected = _requested_index(text)
    if selected is None and re.search(r"\b(?:it|its|that one|this one)\b", text, re.I):
        selected = prior_selection
    if selected is None and len(organizations) == 1:
        selected = 0
    if selected is not None and not 0 <= selected < len(organizations):
        return FollowUpAnswer(
            response=(
                f"I only have {len(organizations)} verified option"
                f"{'s' if len(organizations) != 1 else ''} for "
                f"{_md(service_city) if service_city else 'this city'}, "
                f"so option {selected + 1} is not available."
            ),
            resource_links=_links(organizations),
            organizations=organizations,
            service_city=service_city,
        )

    targets = [organizations[selected]] if selected is not None else organizations
    if CLOSEST_RE.search(text):
        response = (
            "I cannot reliably rank these organisations by distance from the verified information "
            "I have. The linked sources do not provide comparable coordinates for every option. "
            "Use the source links below to confirm the address and call before travelling."
        )
    elif HOURS_RE.search(text):
        response = _hours_response(targets)
    elif CONTACT_RE.search(text):
        response = _contact_response(targets)
    elif selected is not None:
        response = _detail_response(targets[0])
    elif MORE_RE.search(text):
        response = (
            f"These are the options that passed the source-backed checks for "
            f"{_md(service_city) if service_city else 'this city'} in the latest search. "
            "I do not have another verified "
            "organisation in the current result."
        )
    else:
        response = _detail_response(targets[0]) if len(targets) == 1 else _summary_response(targets)

    if language == "hi":
        # Keep verified names and contact values unchanged; only add a compact
        # notice rather than machine-translating safety-critical details.
        response = "सत्यापित जानकारी:\n\n" + response

    return FollowUpAnswer(
        response=response,
        resource_links=_links(targets),
        organizations=organizations,
        selected_ngo_index=selected,
        service_city=service_city,
    )


def latest_snapshot(history: Iterable[dict]) -> FollowUpAnswer | None:
    """Return the latest verified NGO snapshot without generating UI text.

    This lets a care-only follow-up retain contact context for a later explicit
    phone/address question without repeating the NGO directory in the current
    response.
    """
    context = _latest_context(history)
    if not context:
        return None
    organizations, selected, service_city = context
    return FollowUpAnswer(
        response="",
        organizations=organizations,
        selected_ngo_index=selected,
        service_city=service_city,
    )


def _latest_context(
    history: Iterable[dict],
) -> tuple[list[dict[str, str]], int | None, str] | None:
    """Read only the immediately preceding assistant snapshot.

    Each successful follow-up persists the same structured snapshot again, so
    scanning further back would only revive context that an unrelated turn has
    intentionally cleared.
    """
    for entry in reversed(list(history)):
        if not isinstance(entry, dict) or str(entry.get("role") or "") != "assistant":
            continue
        metadata = _metadata(entry.get("metadata"))
        candidate = metadata.get("selected_ngo_index")
        selected = candidate if isinstance(candidate, int) and candidate >= 0 else None
        service_city = _clean(metadata.get("service_city"), 120)
        raw_organizations = metadata.get("organizations")
        if not isinstance(raw_organizations, list):
            return None
        organizations = [
            organization
            for item in raw_organizations
            if (organization := _organization(item))
        ]
        if organizations:
            return organizations, selected, service_city
        return None
    return None


def _metadata(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _organization(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    official_url = str(value.get("official_url") or "").strip()
    parsed = urlparse(official_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    organization = {
        "name": _clean(value.get("name"), 160),
        "service_area": _clean(value.get("service_area"), 200),
        "animal_rescue_evidence": _clean(value.get("animal_rescue_evidence"), 500),
        "official_url": official_url,
        "phone": _clean(value.get("phone"), 80),
        "address": _clean(value.get("address"), 300),
        "opening_hours": _clean(value.get("opening_hours"), 200),
    }
    return organization if organization["name"] else None


def _requested_index(message: str) -> int | None:
    for pattern, index in ORDINALS:
        if pattern.search(message):
            return index
    return None


def _contact_response(organizations: list[dict[str, str]]) -> str:
    lines = ["**Verified contact details**"]
    for index, organization in enumerate(organizations, start=1):
        details = [f"{index}. **{_md(organization['name'])}**"]
        if organization["address"]:
            details.append(f"Address: {_md(organization['address'])}.")
        elif organization["service_area"]:
            details.append(f"Service area: {_md(organization['service_area'])}.")
        details.append(
            f"Phone: {_md(organization['phone'])}."
            if organization["phone"]
            else "Phone: not verified on the linked source used for this result."
        )
        details.append(f"[Source/contact]({organization['official_url']}).")
        lines.append(" ".join(details))
    return "\n\n".join(lines)


def _hours_response(organizations: list[dict[str, str]]) -> str:
    lines = ["**Opening-hours check**"]
    for index, organization in enumerate(organizations, start=1):
        hours = organization["opening_hours"]
        if hours:
            statement = f"Published hours: {_md(hours)}."
        else:
            statement = (
                "Current opening hours were not verified in the linked source used for this result; "
                "please call or check the source page before travelling."
            )
        lines.append(
            f"{index}. **{_md(organization['name'])}** — {statement} "
            f"[Source/contact]({organization['official_url']})."
        )
    return "\n\n".join(lines)


def _detail_response(organization: dict[str, str]) -> str:
    parts = [f"**{_md(organization['name'])}**"]
    if organization["service_area"]:
        parts.append(f"Service area: {_md(organization['service_area'])}.")
    if organization["animal_rescue_evidence"]:
        parts.append(_md(organization["animal_rescue_evidence"].rstrip(".")) + ".")
    if organization["address"]:
        parts.append(f"Address: {_md(organization['address'])}.")
    if organization["phone"]:
        parts.append(f"Phone: {_md(organization['phone'])}.")
    if organization["opening_hours"]:
        parts.append(f"Published hours: {_md(organization['opening_hours'])}.")
    parts.append(f"[Source/contact]({organization['official_url']}).")
    return " ".join(parts)


def _summary_response(organizations: list[dict[str, str]]) -> str:
    return "\n".join(
        f"{index}. **{_md(item['name'])}** — "
        f"{_md(item['animal_rescue_evidence'] or item['service_area'])}"
        for index, item in enumerate(organizations, start=1)
    )


def _links(organizations: list[dict[str, str]]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for item in organizations:
        link = {"label": item["name"], "url": item["official_url"]}
        for field in ("phone", "address", "opening_hours"):
            if item.get(field):
                link[field] = item[field]
        links.append(link)
    return links


def _clean(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _md(value: str) -> str:
    return re.sub(r"([`*_{}\[\]<>])", r"\\\1", str(value or ""))


__all__ = ["FollowUpAnswer", "answer_from_history"]
