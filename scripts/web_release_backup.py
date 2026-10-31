#!/usr/bin/env python3
"""Create and verify a private, quiesced web release backup; never overwrite data."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def create_backup(database: Path, storage: Path, output: Path, *, chroma: Path | None = None, quiesced: bool = False) -> dict:
    if not quiesced:
        raise ValueError("Stop writes first, then explicitly pass --quiesced")
    storage_aliases = sorted({str(storage.absolute()), str(storage.resolve())})
    database, storage, output = database.resolve(), storage.resolve(), output.resolve()
    if not database.is_file() or not storage.is_dir():
        raise ValueError("Existing database and storage directory are required")
    if output.exists():
        raise ValueError("Backup destination must not already exist")
    for source in (storage, chroma.resolve() if chroma else None):
        if source and (output == source or source in output.parents):
            raise ValueError("Backup destination must be outside source directories")
    output.mkdir(parents=True, mode=0o700)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(output / "database.sqlite3")) as snapshot:
                original.backup(snapshot)
        shutil.copytree(storage, output / "storage", symlinks=True)
        if chroma:
            if not chroma.is_dir():
                raise ValueError("Chroma directory does not exist")
            shutil.copytree(chroma, output / "chroma", symlinks=True)
        files = {}
        for path in output.rglob("*"):
            if path.is_symlink():
                raise ValueError("Backup sources contain a symlink; review it before release")
            if path.is_file():
                files[path.relative_to(output).as_posix()] = _digest(path)
        manifest = {"version": 1, "database_original_name": database.name, "storage_original_root": str(storage),
                    "storage_original_aliases": storage_aliases,
                    "chroma_included": bool(chroma), "files": files, "environment_file_included": False,
                    "requirements": "Restore matching application image and separately preserved runtime environment."}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        verify_backup(output)
        return {"backup": str(output), "files": len(files), "restore_check": "passed"}
    except Exception:
        # Keep a clearly incomplete artifact for inspection; never touch source data.
        (output / "INCOMPLETE").write_text("Backup failed validation. Do not restore this directory.\n")
        raise


def verify_backup(backup: Path) -> dict:
    backup = backup.resolve()
    if (backup / "INCOMPLETE").exists():
        raise ValueError("This backup is marked incomplete")
    manifest = json.loads((backup / "manifest.json").read_text())
    for relative, expected in manifest["files"].items():
        path = backup / relative
        if path.is_symlink() or backup not in path.resolve().parents or not path.is_file() or _digest(path) != expected:
            raise ValueError("Backup file integrity failed")
    with tempfile.TemporaryDirectory(prefix="gaia-restore-check-") as directory:
        restored = Path(directory) / "database.sqlite3"
        shutil.copy2(backup / "database.sqlite3", restored)
        with closing(sqlite3.connect(restored.as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored SQLite integrity check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Restored SQLite foreign-key check failed")
            original_roots = [Path(value) for value in manifest.get("storage_original_aliases", [manifest["storage_original_root"]])]
            for (value,) in conn.execute("SELECT image_blob_path FROM incidents WHERE image_blob_path IS NOT NULL"):
                relative = None
                for original_storage in original_roots:
                    try:
                        relative = Path(value).relative_to(original_storage)
                        break
                    except ValueError:
                        continue
                if relative is None:
                    raise ValueError("Incident image path is outside the backed-up storage root")
                if not (backup / "storage" / relative).is_file():
                    raise ValueError("A restored incident image is missing")
    return {"restore_check": "passed", "files": len(manifest["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--chroma", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiesced", action="store_true")
    args = parser.parse_args()
    if args.verify:
        result = verify_backup(args.verify)
    else:
        if not all((args.database, args.storage, args.output)):
            parser.error("--database, --storage and --output are required")
        result = create_backup(args.database, args.storage, args.output, chroma=args.chroma, quiesced=args.quiesced)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
