"""Current-turn output and geography contracts, independent of model history."""
from __future__ import annotations

import re
import unicodedata


def address_only_request(message: str) -> bool:
    return bool(re.search(
        r"\b(?:only|just)\s+(?:(?:the|its|their|his|her|verified|full|complete|postal|street|campus|official)\s+){0,6}address\b|"
        r"\baddress\s+only\b|(?:सिर्फ|केवल)\s*(?:पूरा\s*)?पता", message, re.I,
    ))


def explicit_new_institution(message: str, institution: str) -> bool:
    """An explicitly named target owns lookup scope; pronoun follow-ups do not."""
    clean = lambda value: re.sub(r"\W+", " ", value.casefold()).strip()
    target = clean(institution)
    return bool(target and target in clean(message))


def distinctive_institution(institution: str, location: str = "") -> bool:
    generic = {"the", "a", "an", "of", "and", "in", "at", "for", "veterinary", "vet", "college", "hospital", "hospitals",
               "clinic", "clinical", "teaching", "complex", "rescue", "centre", "center", "animal", "government", "public", "university"}
    words = set(normalized_place(institution).split()) - set(normalized_place(location).split()) - generic
    return any(len(word) > 2 and not word.isdigit() for word in words)


def direct_availability_question(message: str) -> bool:
    return bool(re.search(
        r"\bpick\s+(?:it|him|her|them|the\s+(?:dog|cat|animal))\s+up\b|"
        r"\b(?:pickup|pick[ -]?up|admit|admission|accept)\b|लेने\s+आ|प्रवेश", message, re.I,
    ))


def clearly_off_topic_task(message: str) -> bool:
    """Conservative outage/contract backup, not a general animal-keyword bypass."""
    if re.search(
        r"\b(?:write|draft|generate|create)\b.{0,90}\b(?:sales|marketing)\s+(?:e-?mail|copy)\b", message, re.I,
    ) and not re.search(r"\b(?:donat\w*|fundrais\w*|shelters?|animal\s+welfare|NGOs?)\b", message, re.I):
        return True
    return bool(re.search(
        r"\b(?:write|generate|give|create)\b[^.!?]{0,90}\b(?:python|javascript|java|chatbot['’]s?)\b[^.!?]{0,30}\bcode\b|"
        r"\b(?:restaurant|hotel)\s+recommendations?\b", message, re.I,
    ))


def normalized_place(value: str) -> str:
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def locality_matches(locality: str, text: str, selected_region: str = "") -> bool:
    """City evidence cannot override an explicitly different published state."""
    # Parenthetical alternatives supplied in the request are alternatives, not
    # a single postal phrase that every source has to repeat verbatim.
    alternatives = [locality]
    alias = re.fullmatch(r"\s*([^()]+?)\s*\(([^()]+)\)\s*", locality)
    if alias:
        alternatives = [alias.group(1), re.sub(r"^(?:formerly|now|also known as)\s+", "", alias.group(2), flags=re.I)]
    if locality and not any(f" {normalized_place(value)} " in f" {normalized_place(text)} " for value in alternatives):
        return False
    if selected_region:
        from services.query_router import INDIA_STATES
        wanted = normalized_place(selected_region)
        states = {normalized_place(state) for state in INDIA_STATES
                  if f" {normalized_place(state)} " in f" {normalized_place(text)} "}
        # Longer state names subsume shorter names (e.g. Arunachal Pradesh).
        states = {state for state in states if not any(state != other and state in other for other in states)}
        if states and wanted not in states:
            return False
        # A published standard state abbreviation is also conflicting evidence.
        aliases = {"cg": "chhattisgarh", "hp": "himachal pradesh", "up": "uttar pradesh",
                   "mp": "madhya pradesh", "uk": "uttarakhand"}
        for alias, state in aliases.items():
            if re.search(r"(?:,|\bstate\s*:)\s*" + alias + r"(?=\s*(?:,|\d{6}\b|$))", text, re.I) and wanted != state:
                return False
    return True
