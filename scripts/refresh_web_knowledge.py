#!/usr/bin/env python3
"""Refresh current Markdown in SQLite/Chroma without removing PDF knowledge.

Dry-run is the default. Apply only during a quiesced, backed-up release; the two
stores cannot commit atomically. On any failure, keep traffic stopped and rerun.
No model API is called and cached local embeddings are required for Chroma.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]


def _metadata(record: dict) -> dict:
    return {"doc_file": record["doc_file"], "title": record["title"],
            "chunk_index": record["chunk_index"], "source_url": record.get("source_url", "")}


def _vector_rows(collection, doc_file: str) -> dict:
    result = collection.get(where={"doc_file": doc_file}, include=["documents", "metadatas"])
    return {chunk_id: (content, metadata or {}) for chunk_id, content, metadata in
            zip(result["ids"], result["documents"], result["metadatas"])}


def refresh_documents(documents: dict[str, list[dict]], conn: sqlite3.Connection,
                      *, collection=None, encode=None, apply: bool = False) -> dict:
    """Plan or refresh prebuilt chunks; never clears a collection or other docs."""
    plans = []
    for doc_file, records in sorted(documents.items()):
        if not doc_file.endswith(".md") or not records:
            raise ValueError("Only nonempty current Markdown documents may be refreshed")
        if any(row["doc_file"] != doc_file for row in records):
            raise ValueError("Chunk document identity mismatch")
        ids = [record["id"] for record in records]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate chunk IDs")
        expected_sql = [(row["title"], row["chunk_index"], row["content"]) for row in records]
        current_sql = [tuple(row) for row in conn.execute(
            "SELECT title, chunk_index, content FROM rag_chunks WHERE doc_file = ? ORDER BY chunk_index",
            (doc_file,),
        )]
        vectors = _vector_rows(collection, doc_file) if collection is not None else None
        expected_vectors = {row["id"]: (row["content"], _metadata(row)) for row in records}
        if current_sql != expected_sql or (vectors is not None and vectors != expected_vectors):
            plans.append({"doc_file": doc_file, "records": records,
                          "obsolete_ids": sorted(set(vectors or {}) - set(ids)),
                          "sqlite_changed": current_sql != expected_sql})
    result = {"dry_run": not apply, "changed_documents": [
        {"doc_file": plan["doc_file"], "new_chunks": len(plan["records"]),
         "obsolete_vector_ids": len(plan["obsolete_ids"]), "sqlite_changed": plan["sqlite_changed"]}
        for plan in plans], "pdf_or_unrelated_documents_modified": False}
    if not apply or not plans:
        return result

    # Finish every embedding before any store changes. This prevents a missing
    # local model or encoding failure from deleting existing knowledge.
    embeddings = {}
    if collection is not None:
        if encode is None:
            raise ValueError("Local cached embedding encoder is required")
        for plan in plans:
            rows = plan["records"]
            encoded = encode([row["content"] for row in rows])
            if len(encoded) != len(rows) or any(not vector for vector in encoded):
                raise ValueError("Incomplete embeddings")
            embeddings[plan["doc_file"]] = encoded
        # Upsert and verify ALL new chunks before deleting ANY old chunk.
        for plan in plans:
            rows = plan["records"]
            for offset in range(0, len(rows), 64):
                batch = rows[offset:offset + 64]
                collection.upsert(ids=[row["id"] for row in batch],
                                  documents=[row["content"] for row in batch],
                                  metadatas=[_metadata(row) for row in batch],
                                  embeddings=embeddings[plan["doc_file"]][offset:offset + 64])
            actual = _vector_rows(collection, plan["doc_file"])
            if any(actual.get(row["id"]) != (row["content"], _metadata(row)) for row in rows):
                raise RuntimeError("New vector verification failed; old chunks were retained")

    for plan in plans:
        if plan["sqlite_changed"]:
            # Replace one document atomically, retaining unrelated embeddings.
            with conn:
                conn.execute("DELETE FROM rag_chunks WHERE doc_file = ?", (plan["doc_file"],))
                conn.executemany(
                    "INSERT INTO rag_chunks (doc_file, title, chunk_index, content, embedding) VALUES (?, ?, ?, ?, NULL)",
                    [(row["doc_file"], row["title"], row["chunk_index"], row["content"])
                     for row in plan["records"]],
                )
        if collection is not None and plan["obsolete_ids"]:
            collection.delete(ids=plan["obsolete_ids"])
        if collection is not None:
            actual = _vector_rows(collection, plan["doc_file"])
            expected = {row["id"]: (row["content"], _metadata(row)) for row in plan["records"]}
            if actual != expected:
                raise RuntimeError("Obsolete vector cleanup failed; keep traffic stopped and rerun")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", type=Path, default=ROOT / "rag_docs")
    parser.add_argument("--apply", action="store_true", help="Write the reviewed refresh")
    parser.add_argument("--quiesced", action="store_true", help="Confirm app/ingestion writers are stopped")
    args = parser.parse_args()
    if args.apply and not args.quiesced:
        parser.error("--apply requires --quiesced and a verified release backup")
    sys.path.insert(0, str(ROOT))
    import config
    from scripts.ingest_docs import build_chunk_records

    if config.RAG_VECTOR_BACKEND not in {"sqlite", "chroma"}:
        raise RuntimeError("This refresh supports SQLite/Chroma; hosted vectors require their own release workflow")
    paths = sorted(args.docs_dir.rglob("*.md"))
    if not paths:
        raise ValueError("No current Markdown files found")
    # Prepare every document before opening either store for writes.
    documents = {}
    for path in paths:
        records = build_chunk_records(path, docs_dir=args.docs_dir)
        if not records:
            raise ValueError(f"Empty Markdown document: {path.name}")
        documents[records[0]["doc_file"]] = records
    if not config.DB_PATH.is_file():
        raise ValueError("Existing runtime SQLite knowledge database is required")
    collection = None
    encode = None
    if config.RAG_VECTOR_BACKEND == "chroma":
        if not (config.CHROMA_PERSIST_DIR / "chroma.sqlite3").is_file():
            raise ValueError("Existing runtime Chroma collection is required")
        from services import chroma_rag
        collection = chroma_rag._client().get_collection(config.CHROMA_COLLECTION_NAME, embedding_function=None)
        if args.apply:
            if not chroma_rag.warm_up():
                raise RuntimeError("Configured local embedding weights are not cached; no refresh applied")
            encode = chroma_rag._encode_dense
    mode = "rw" if args.apply else "ro"
    conn = sqlite3.connect(config.DB_PATH.resolve().as_uri() + f"?mode={mode}", uri=True)
    try:
        result = refresh_documents(documents, conn, collection=collection, encode=encode, apply=args.apply)
    finally:
        conn.close()
    print(json.dumps({"backend": config.RAG_VECTOR_BACKEND, **result}, indent=2))
    if args.apply:
        print("Restart serving processes to clear their retrieval caches before reopening traffic.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Knowledge refresh failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        raise SystemExit(1)
