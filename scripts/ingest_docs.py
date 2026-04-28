#!/usr/bin/env python3
"""
Ingest RAG documents into the database.

Reads markdown files from rag_docs/, splits them into chunks,
optionally embeds them (OpenAI provider), and saves to the
rag_chunks SQLite table.

Usage:
    cd /path/to/gaia-chatbot
    python3 scripts/ingest_docs.py

Run this once before starting the server, and re-run whenever
rag_docs/ content is updated.
"""

import sys
import json
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import MODEL_PROVIDER
import database as db
from services import ai_client

RAG_DOCS_DIR = Path(__file__).parent.parent / "rag_docs"

CHUNK_SIZE = 400    # target words per chunk
CHUNK_OVERLAP = 50  # words carried over to the next chunk


def _split_into_chunks(content: str, title: str) -> list[str]:
    """Split document content into overlapping paragraph-aware chunks."""
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]

    chunks = []
    current_words: list[str] = []

    for para in paragraphs:
        para_words = para.split()
        if current_words and len(current_words) + len(para_words) > CHUNK_SIZE:
            chunks.append(f"{title}\n\n" + " ".join(current_words))
            current_words = current_words[-CHUNK_OVERLAP:] + para_words
        else:
            current_words.extend(para_words)

    if current_words:
        chunks.append(f"{title}\n\n" + " ".join(current_words))

    return chunks


def _extract_title(text: str, filepath: Path) -> str:
    """Extract title from the first H1 heading, falling back to the filename."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return filepath.stem.replace("_", " ").title()


def _strip_source_lines(text: str) -> str:
    """Remove 'Source: ...' metadata lines from document content."""
    lines = [line for line in text.splitlines() if not line.startswith("Source:")]
    return "\n".join(lines)


def ingest_file(filepath: Path) -> int:
    """Ingest one markdown file. Returns the number of chunks stored."""
    raw = filepath.read_text(encoding="utf-8")
    title = _extract_title(raw, filepath)
    content = _strip_source_lines(raw)
    chunks = _split_into_chunks(content, title)

    db.delete_rag_chunks_for_doc(filepath.name)

    stored = 0
    for i, chunk_text in enumerate(chunks):
        embedding_json = None
        if MODEL_PROVIDER == "openai" and ai_client.is_available():
            embedding = ai_client.create_embedding(chunk_text)
            if embedding:
                embedding_json = json.dumps(embedding)

        db.insert_rag_chunk(
            doc_file=filepath.name,
            title=title,
            chunk_index=i,
            content=chunk_text,
            embedding=embedding_json,
        )
        stored += 1
        print(f"    chunk {i + 1}/{len(chunks)} stored")

    return stored


def main():
    db.init_db()

    doc_files = sorted(RAG_DOCS_DIR.glob("*.md"))
    if not doc_files:
        print(f"No markdown files found in {RAG_DOCS_DIR}")
        sys.exit(1)

    mode = "OpenAI text-embedding-3-small" if MODEL_PROVIDER == "openai" else "BM25 (no embeddings)"
    print(f"Provider : {MODEL_PROVIDER}")
    print(f"Retrieval: {mode}")
    print(f"Documents: {len(doc_files)}\n")

    total = 0
    for filepath in doc_files:
        print(f"  {filepath.name}")
        n = ingest_file(filepath)
        total += n
        print(f"  → {n} chunk(s)\n")

    # Invalidate in-memory BM25 cache so the server picks up the new data
    from services.rag import reset_cache
    reset_cache()

    print(f"Ingestion complete. Total chunks: {total}")


if __name__ == "__main__":
    main()
