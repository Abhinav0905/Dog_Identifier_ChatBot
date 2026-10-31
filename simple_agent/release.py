"""Validate a complete five-file release before exposing it to a turn."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .catalog import PromptCatalog


@dataclass(frozen=True)
class Release:
    version: str
    digest: str
    catalog: PromptCatalog


def load_bundle(directory):
    # Resolve the release symlink once so an activation cannot mix two versions.
    root = Path(directory).resolve(strict=True)
    raw = (root / "manifest.json").read_bytes()
    if len(raw) > 64000:
        raise ValueError("Oversized prompt manifest")
    manifest = json.loads(raw)
    names = {name + ".txt" for name in PromptCatalog.__dataclass_fields__}
    if (manifest.get("schema_version") != 1 or manifest.get("engine") != "simple_agent"
            or set(manifest.get("files", {})) != names
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", manifest.get("version", ""))):
        raise ValueError("Incompatible single-agent prompt manifest")
    values = {}
    for name in names:
        path = root / name
        if path.is_symlink() or path.stat().st_size > 200000:
            raise ValueError("Prompt must be a bounded regular file")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest["files"][name]:
            raise ValueError("Prompt checksum mismatch: " + name)
        text = content.decode("utf-8").strip()
        if not text:
            raise ValueError("Empty prompt: " + name)
        values[name[:-4]] = text
    return Release(manifest["version"], hashlib.sha256(raw).hexdigest(), PromptCatalog(**values))
