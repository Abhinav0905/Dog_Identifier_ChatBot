#!/usr/bin/env python3
"""Validate, seal or atomically activate a local/GitHub prompt release."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.prompt_store import REQUIRED_FILES, load_bundle


def seal(directory: Path, version: str):
    manifest_path = directory / "manifest.json"
    original = manifest_path.read_bytes() if manifest_path.exists() else None
    manifest = {"schema_version": 1, "version": version, "files": {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in sorted(REQUIRED_FILES)
    }}
    # Seal only staging/checkout directories, never the active release in place.
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    try:
        return load_bundle(directory)
    except Exception:
        if original is None:
            manifest_path.unlink()
        else:
            manifest_path.write_bytes(original)
        raise


@contextmanager
def activation_lock(current: Path):
    lock = current.parent / (current.name + ".activation-lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError("Another activation is in progress; check the activation lock")
    try:
        yield
    finally:
        lock.rmdir()


def activate(source: Path, current: Path):
    """Validate a snapshot, then change the pointer with one atomic filesystem call."""
    source = source.resolve(strict=True)
    current = current.absolute()
    current.parent.mkdir(parents=True, exist_ok=True)
    with activation_lock(current):
        if current.exists() and not current.is_symlink():
            raise ValueError("The current-release path must be absent or a symlink, never a directory")
        release_root = current.parent / (current.name + ".releases")
        release_root.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=release_root))
        try:
            shutil.copytree(source, staging, dirs_exist_ok=True)
            bundle = load_bundle(staging)
            target = release_root / (bundle.version + "-" + bundle.digest[:12])
            if target.exists():
                existing = load_bundle(target)
                if existing.digest != bundle.digest:
                    raise ValueError("Existing release has conflicting content")
            else:
                os.replace(staging, target)
            previous = current.resolve() if current.is_symlink() else None
            temporary = current.parent / ("." + current.name + "-" + uuid.uuid4().hex)
            try:
                # Relative link works both on the host and inside a directory mount.
                temporary.symlink_to(os.path.relpath(target, current.parent), target_is_directory=True)
                os.replace(temporary, current)
            finally:
                temporary.unlink(missing_ok=True)
            return {"version": bundle.version, "sha256": bundle.digest,
                    "current": str(current), "previous_release": str(previous) if previous else None}
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("directory", type=Path)
    sealing = sub.add_parser("seal", help="Regenerate hashes after editing a staging package")
    sealing.add_argument("directory", type=Path)
    sealing.add_argument("--version", required=True)
    activation = sub.add_parser("activate")
    activation.add_argument("directory", type=Path)
    activation.add_argument("--current", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "activate":
            result = activate(args.directory, args.current)
        else:
            bundle = seal(args.directory, args.version) if args.command == "seal" else load_bundle(args.directory)
            result = {"version": bundle.version, "sha256": bundle.digest, "runtime_files": len(bundle.texts)}
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Prompt package rejected: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
