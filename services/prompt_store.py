"""Validated text-file releases, cached in memory and pinned for each request.

GitHub is the authoring source; the running application reads local EC2 files.
Publish a complete manifest last, or atomically switch a release symlink. No
network access or executable templates are involved in serving a prompt.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path, PurePosixPath
import re
import threading
from types import MappingProxyType
from typing import Mapping

import config

logger = logging.getLogger(__name__)
MAIN_FILES = (
    "01_text_router_system.txt", "02_care_system_english.txt",
    "03_web_search_system_template_english.txt", "04_contact_evidence_review_system.txt",
    "05_vision_system_english.txt", "06_enrichment_system_english.txt",
    "07_query_analysis_template.txt", "08_place_extraction_template.txt",
    "09_admin_nl_to_sql_system.txt",
)
SUPPORT_FILES = tuple(f"support/{name}.txt" for name in (
    "vision_user", "vision_reporter_context", "enrichment_user", "care_reference",
    "care_context", "prior_conversation", "web_refinement", "ngo_candidates_strict",
    "ngo_candidates_open", "ngo_rules_strict", "ngo_rules_fast", "ngo_rules_regional",
    "ngo_discovery_city", "ngo_discovery_regional", "language_default", "language_hindi",
    "final_language_default", "final_language_hindi", "turn_language", "search_care_only",
))
REQUIRED_FILES = frozenset((*MAIN_FILES, *SUPPORT_FILES))
TOKEN_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
EXPECTED_TOKENS = {
    "support/turn_language.txt": {"REPLY_LANGUAGE"},
    "03_web_search_system_template_english.txt": {"RUNTIME_CONTEXT_JSON"},
    "07_query_analysis_template.txt": {"CURRENT_MESSAGE", "RECENT_CONTEXT"},
    "08_place_extraction_template.txt": {"CURRENT_MESSAGE"},
    "support/vision_reporter_context.txt": {"USER_CONTEXT"},
    "support/enrichment_user.txt": {"SEVERITY", "SEVERITY_SCORE", "SUMMARY", "INDICATORS", "ACTIONS"},
    "support/care_reference.txt": {"RAG_CONTEXT"},
    "support/care_context.txt": {"CONTEXTUAL_REQUEST"},
    "support/prior_conversation.txt": {"RECENT_CONVERSATION_JSON"},
    "support/web_refinement.txt": {"REFINEMENT_CONTEXT_JSON"},
    "support/ngo_candidates_strict.txt": {"CANDIDATES_JSON"},
    "support/ngo_candidates_open.txt": {"CANDIDATES_JSON"},
    "support/ngo_rules_regional.txt": {"REQUIRED_CITY", "REQUIRED_REGION"},
    "support/ngo_discovery_city.txt": {"CITY", "REGION", "MAXIMUM_CANDIDATES"},
    "support/ngo_discovery_regional.txt": {"REGION", "MAXIMUM_CANDIDATES"},
}
SHARED_MARKER = "SHARED ASK DORJEE POLICY\n"
FINAL_MARKER = "FINAL RESPONSE CONTRACT FOR THIS TURN\n"
CONTEXT_WRAPPER = '{"runtime_context": "{{RUNTIME_CONTEXT_JSON}}"}'


class PromptPackageError(ValueError):
    """A candidate cannot be activated as a complete compatible release."""


@dataclass(frozen=True)
class PromptBundle:
    version: str
    digest: str
    texts: Mapping[str, str]

    def get(self, name: str) -> str:
        return self.texts[name]


def render(text: str, **values: object) -> str:
    """Single-pass substitution; inserted user data is never interpreted again."""
    def replace(match):
        key = match.group(1)
        if key not in values:
            raise PromptPackageError(f"Missing template value: {key}")
        return str(values[key])
    return TOKEN_RE.sub(replace, text)


def shared_section(text: str, language_rule: str) -> str:
    if text.count(SHARED_MARKER) != 1 or text.count(language_rule) != 1:
        raise PromptPackageError("Expected exactly one shared policy and language rule")
    start = text.index(SHARED_MARKER)
    stop = text.index(language_rule, start)
    return text[start:stop].rstrip()


def load_bundle(directory: Path) -> PromptBundle:
    """Validate the entire release before any file can become active."""
    directory = Path(directory).resolve(strict=True)
    manifest_path = directory / "manifest.json"
    raw = manifest_path.read_bytes()
    if len(raw) > 64_000:
        raise PromptPackageError("Prompt manifest is too large")
    try:
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise PromptPackageError("Invalid prompt manifest")
        files = manifest["files"]
        version = manifest["version"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PromptPackageError("Invalid prompt manifest") from exc
    if manifest.get("schema_version") != 1 or not isinstance(files, dict):
        raise PromptPackageError("Unsupported prompt manifest schema")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", version):
        raise PromptPackageError("Invalid prompt version")
    if set(files) != REQUIRED_FILES:
        raise PromptPackageError("Manifest must contain exactly the supported runtime files")
    texts = {}
    for name, expected_hash in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise PromptPackageError("Prompt path escapes its release")
        path = (directory / name).resolve(strict=True)
        if directory not in path.parents or path.stat().st_size > 256_000:
            raise PromptPackageError("Invalid prompt path or file size")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise PromptPackageError(f"Hash mismatch: {name}")
        text = content.decode("utf-8")
        if not text.strip():
            raise PromptPackageError(f"Empty prompt: {name}")
        if set(TOKEN_RE.findall(text)) != EXPECTED_TOKENS.get(name, set()):
            raise PromptPackageError(f"Unexpected or missing placeholders: {name}")
        texts[name] = text
    language_rule = texts["support/language_default.txt"]
    shared = [shared_section(texts[name], language_rule) for name in (
        MAIN_FILES[1], MAIN_FILES[2], MAIN_FILES[4], MAIN_FILES[5],
    )]
    if len(set(shared)) != 1:
        raise PromptPackageError("Shared policy differs across care/search/vision/enrichment")
    if texts[MAIN_FILES[1]].count(FINAL_MARKER) != 1:
        raise PromptPackageError("Care prompt must contain one final response contract")
    if texts[MAIN_FILES[2]].count(CONTEXT_WRAPPER) != 1:
        raise PromptPackageError("Web-search JSON context wrapper is missing or duplicated")
    if manifest_path.read_bytes() != raw:
        raise PromptPackageError("Manifest changed during loading; retry the complete release")
    return PromptBundle(version, hashlib.sha256(raw).hexdigest(), MappingProxyType(texts))


class PromptStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._bundle = None
        self._error = None
        self._lock = threading.RLock()

    def current(self) -> PromptBundle:
        with self._lock:
            try:
                raw = (self.directory / "manifest.json").read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if self._bundle is None or self._bundle.digest != digest:
                    candidate = load_bundle(self.directory)
                    self._bundle = candidate
                    logger.info("Activated prompt package %s (%s)", candidate.version, candidate.digest[:12])
                self._error = None
            except (OSError, ValueError) as exc:
                error = type(exc).__name__
                if self._error != error:
                    logger.error("Prompt reload failed: %s; keeping last valid release", error)
                self._error = error
                if self._bundle is None:
                    raise PromptPackageError("No valid prompt package is available") from exc
            return self._bundle

    def status(self) -> dict:
        bundle = self.current()
        return {"version": bundle.version, "sha256": bundle.digest,
                "status": "using_last_valid" if self._error else "ready",
                "source": "local_files", "reload_error": self._error}


_store = PromptStore(config.PROMPT_PACKAGE_DIR)
_pinned: ContextVar[PromptBundle | None] = ContextVar("prompt_bundle", default=None)


def current() -> PromptBundle:
    return _pinned.get() or _store.current()


@contextmanager
def pin(bundle: PromptBundle | None = None):
    selected = bundle or _store.current()
    token = _pinned.set(selected)
    try:
        yield selected
    finally:
        _pinned.reset(token)


def status() -> dict:
    return _store.status()
