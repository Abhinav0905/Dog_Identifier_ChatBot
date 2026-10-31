#!/usr/bin/env python3
"""Run an isolated localhost copy without changing .env or sending rescue alerts."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "local-validation.json"
RAG_COLUMNS = "id, doc_file, title, chunk_index, content, embedding"


@contextmanager
def readonly_database(path: Path):
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def source_settings() -> dict:
    """Read only the source paths/settings needed before importing config."""
    from dotenv import dotenv_values

    values = dotenv_values(ROOT / ".env")

    def setting(name: str, default: str) -> str:
        return (os.environ.get(name, values.get(name)) or default).strip()

    return {
        "database": Path(setting("DB_PATH", str(ROOT / "dharamsala.db"))).resolve(),
        "chroma": Path(setting("CHROMA_PERSIST_DIR", str(ROOT / "chroma_db"))).resolve(),
        "storage": Path(setting("STORAGE_DIR", str(ROOT / "storage"))).resolve(),
        "collection": setting("CHROMA_COLLECTION_NAME", "dar-rag"),
    }


def rag_count(path: Path) -> int:
    with readonly_database(path) as connection:
        return connection.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0]


def copy_rag(source: Path, destination: Path) -> int:
    """Copy knowledge rows only, never operational tables or cached answers."""
    with readonly_database(source) as original:
        rows = original.execute(f"SELECT {RAG_COLUMNS} FROM rag_chunks ORDER BY id").fetchall()
    with closing(sqlite3.connect(destination)) as local, local:
        if local.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0]:
            raise RuntimeError("Refusing to overwrite existing local RAG data.")
        local.executemany(
            f"INSERT INTO rag_chunks ({RAG_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)", rows,
        )
    if rag_count(destination) != len(rows):
        raise RuntimeError("Local SQLite RAG count did not match the source snapshot.")
    return len(rows)


def chroma_count(directory: Path, collection: str) -> int:
    database = directory / "chroma.sqlite3"
    if not database.is_file():
        return 0
    with readonly_database(database) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM embeddings e JOIN segments s ON e.segment_id = s.id "
            "JOIN collections c ON s.collection = c.id WHERE c.name = ?", (collection,),
        ).fetchone()[0]


def tree_state(directory: Path) -> dict:
    """Detect writes during snapshotting, including HNSW files and SQLite WAL."""
    result = {}
    for path in directory.rglob("*"):
        if path.is_file() and not path.name.endswith("-shm"):
            stat = path.stat()
            result[str(path.relative_to(directory))] = (stat.st_size, stat.st_mtime_ns)
    return result


def copy_chroma(source: Path, destination: Path, collection: str) -> int:
    """Copy a quiescent vector store, using SQLite backup for its database."""
    if not (source / "chroma.sqlite3").is_file():
        destination.mkdir()
        return 0
    before = tree_state(source)
    expected = chroma_count(source, collection)
    shutil.copytree(
        source, destination,
        ignore=shutil.ignore_patterns("chroma.sqlite3", "chroma.sqlite3-wal", "chroma.sqlite3-shm"),
    )
    with readonly_database(source / "chroma.sqlite3") as original:
        with closing(sqlite3.connect(destination / "chroma.sqlite3")) as copied, copied:
            original.backup(copied)
    if before != tree_state(source):
        raise RuntimeError(
            "The source Chroma store changed during copying. Stop local ingestion/writers "
            "and retry with a new --state-dir."
        )
    if chroma_count(destination, collection) != expected:
        raise RuntimeError("Copied Chroma vector count did not match the source snapshot.")
    return expected


def configure_local(state: Path, collection: str):
    """Apply local-only settings before any service imports capture configuration."""
    os.environ.update({
        "DB_PATH": str(state / "dharamsala.db"),
        "STORAGE_DIR": str(state / "storage"),
        "CHROMA_PERSIST_DIR": str(state / "chroma_db"),
        "CHROMA_COLLECTION_NAME": collection,
        "RAG_VECTOR_BACKEND": "chroma",
        "ALERT_WEBHOOK_URL": "",
        "SLACK_WEBHOOK_URL": "",
        "CONVERSATION_COOKIE_SECURE": "false",
        "HOST": "127.0.0.1",
    })
    sys.path.insert(0, str(ROOT))
    import config

    # These settings prefer .env over the process environment in config.py.
    config.CONVERSATION_COOKIE_SECURE = False
    config.TWILIO_ACCOUNT_SID = ""
    config.TWILIO_AUTH_TOKEN = ""
    config.TWILIO_WEBHOOK_BASE_URL = ""
    config.ALERT_WEBHOOK_URL = ""
    config.SLACK_WEBHOOK_URL = ""
    config.HOST = "127.0.0.1"
    return config


def prepare(state: Path, source: dict) -> dict:
    state = state.resolve()
    source = {
        **source,
        **{name: source[name].resolve() for name in ("database", "chroma", "storage")},
    }
    for protected in (source["chroma"], source["storage"]):
        if state == protected or protected in state.parents:
            raise RuntimeError("Choose a --state-dir outside the original Chroma/uploads directories.")
    if state / "dharamsala.db" == source["database"]:
        raise RuntimeError("Choose a --state-dir separate from the original database.")

    manifest_path = state / MANIFEST
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("version") != 1:
            raise RuntimeError("Unrecognized local validation state version.")
        collection = manifest["collection"]
        if rag_count(state / "dharamsala.db") != manifest["rag_chunks"]:
            raise RuntimeError("Existing local RAG count changed; use a new --state-dir for a fresh copy.")
        if chroma_count(state / "chroma_db", collection) != manifest["chroma_vectors"]:
            raise RuntimeError("Existing local Chroma count changed; use a new --state-dir.")
        configure_local(state, collection)
    else:
        if any((state / name).exists() for name in ("dharamsala.db", "chroma_db", "storage")):
            raise RuntimeError("Unrecognized existing local data; use a new --state-dir. Nothing was overwritten.")
        if not source["database"].is_file():
            raise RuntimeError("The source SQLite knowledge database is missing.")
        state.mkdir(parents=True, exist_ok=True)
        configure_local(state, source["collection"])
        import database

        database.init_db()
        chunks = copy_rag(source["database"], state / "dharamsala.db")
        vectors = copy_chroma(source["chroma"], state / "chroma_db", source["collection"])
        manifest = {
            "version": 1, "collection": source["collection"],
            "rag_chunks": chunks, "chroma_vectors": vectors,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    import config
    from services import chroma_rag

    if manifest["chroma_vectors"]:
        if not chroma_rag.is_available():
            raise RuntimeError("Copied Chroma data exists, but Chroma/sentence-transformers are unavailable in this Python environment.")
        # Open only the isolated store to verify the actual retrieval integration.
        if chroma_rag._collection().count() != manifest["chroma_vectors"]:
            raise RuntimeError("The application Chroma collection did not match the copied vector count.")
        backend = "Chroma (copied collection verified; embedding model loads on first query)"
    else:
        config.RAG_VECTOR_BACKEND = "sqlite"
        os.environ["RAG_VECTOR_BACKEND"] = "sqlite"
        backend = "SQLite fallback (no populated Chroma collection was copied)"
    return {**manifest, "backend": backend, "state_dir": str(state)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Run with .venv/bin/python scripts/run_local_validation.py --prepare-only first. "
            "Then reuse the printed directory with --state-dir /path/to/state to start the server. "
            "State persists between runs; a new directory gives a fresh conversation/cache database. "
            "Preparing performs no model/search calls. Chatting uses the configured API services."
        ),
    )
    parser.add_argument("--port", type=int, default=8001, help="Loopback HTTP port (default: 8001)")
    parser.add_argument("--state-dir", type=Path, help="Reuse isolated state (default: a new persistent temporary directory)")
    parser.add_argument("--prepare-only", action="store_true", help="Prepare and verify local state without starting the server")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    state = args.state_dir or Path(tempfile.mkdtemp(prefix="gaia-local-validation-"))
    try:
        result = prepare(state, source_settings())
    except (RuntimeError, OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Local validation setup failed: {exc}\nState directory: {state.resolve()}\n")
    print(f"State directory: {result['state_dir']}")
    print(f"SQLite: {state.resolve() / 'dharamsala.db'} ({result['rag_chunks']} knowledge chunks)")
    print(f"Uploads: {state.resolve() / 'storage'}")
    print(f"Chroma: {state.resolve() / 'chroma_db'} ({result['chroma_vectors']} vectors)")
    print(f"Retrieval: {result['backend']}")
    print("Local HTTP cookies enabled; Slack, rescue webhooks and outbound WhatsApp disabled.")
    if args.prepare_only:
        return
    import app
    import uvicorn

    print(f"Local browser: http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(app.app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
