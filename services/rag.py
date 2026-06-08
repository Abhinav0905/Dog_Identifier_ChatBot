"""
RAG (Retrieval-Augmented Generation) service.

Retrieval strategy:
  - MODEL_PROVIDER=openai  → semantic search using text-embedding-3-small
                              stored in SQLite, cosine similarity at query time.
  - MODEL_PROVIDER=claude  → BM25 keyword retrieval (rank_bm25).
                              No embedding API required.

Run scripts/ingest_docs.py once to populate rag_chunks before starting the server.
"""

import json
import math
import logging
import re
from config import MODEL_PROVIDER, RAG_VECTOR_BACKEND
import database as db

logger = logging.getLogger(__name__)

# In-memory BM25 index (built lazily on first query, reset after ingestion)
_bm25_cache: tuple | None = None


def retrieve(query: str, k: int = 3) -> list[dict]:
    """Return top-k relevant knowledge chunks for the given query."""
    if RAG_VECTOR_BACKEND == "chroma":
        chroma_chunks = _retrieve_chroma(query, k)
        if chroma_chunks:
            return chroma_chunks

    if RAG_VECTOR_BACKEND == "pinecone":
        pinecone_chunks = _retrieve_pinecone(query, k)
        if pinecone_chunks:
            return pinecone_chunks

    if MODEL_PROVIDER == "openai":
        return _retrieve_semantic(query, k)
    return _retrieve_bm25(query, k)


def format_context(chunks: list[dict]) -> str:
    """Format retrieved chunks as a KNOWLEDGE BASE block for system prompt injection."""
    if not chunks:
        return ""
    parts = ["## KNOWLEDGE BASE (Dharamsala Animal Rescue)\n"]
    for chunk in chunks:
        parts.append(f"**{chunk['title']}**:\n{chunk['content']}\n")
    return "\n".join(parts)


def reset_cache():
    """Invalidate the in-memory BM25 index (call after ingesting new documents)."""
    global _bm25_cache
    _bm25_cache = None


def _retrieve_chroma(query: str, k: int) -> list[dict]:
    try:
        from services import chroma_rag
        return chroma_rag.retrieve(query, k)
    except Exception as exc:  # noqa: BLE001 - local retrieval remains available
        logger.warning("Chroma retrieval unavailable: %s", exc)
        return []


def _retrieve_pinecone(query: str, k: int) -> list[dict]:
    try:
        from services import pinecone_rag
        return pinecone_rag.retrieve(query, k)
    except Exception as exc:  # noqa: BLE001 - local retrieval remains available
        logger.warning("Pinecone retrieval unavailable: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Semantic retrieval (OpenAI)
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _retrieve_semantic(query: str, k: int) -> list[dict]:
    from services import ai_client

    chunks = db.get_all_rag_chunks()
    if not chunks:
        return []

    if not any(chunk["embedding"] for chunk in chunks):
        return _retrieve_bm25(query, k)

    try:
        query_embedding = ai_client.create_embedding(query)
    except Exception as exc:  # noqa: BLE001 - retrieval should not break chat
        logger.warning("Semantic RAG retrieval skipped: embedding failed: %s", exc)
        return _retrieve_bm25(query, k)

    if not query_embedding:
        return _retrieve_bm25(query, k)

    scored = []
    for chunk in chunks:
        if not chunk["embedding"]:
            continue
        stored_emb = json.loads(chunk["embedding"])
        score = _cosine_similarity(query_embedding, stored_emb)
        scored.append((score, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    # Threshold: cosine > 0.3 to avoid injecting irrelevant content
    return [chunk for score, chunk in scored[:k] if score > 0.3]


# ---------------------------------------------------------------------------
# BM25 keyword retrieval (Anthropic / fallback)
# ---------------------------------------------------------------------------

def _get_bm25_index():
    global _bm25_cache
    if _bm25_cache is None:
        chunks = db.get_all_rag_chunks()
        if not chunks:
            return None, []
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            return None, []
        corpus = [_tokenize(chunk["content"]) for chunk in chunks]
        _bm25_cache = (BM25Okapi(corpus), chunks)
    return _bm25_cache


def _retrieve_bm25(query: str, k: int) -> list[dict]:
    index, chunks = _get_bm25_index()
    if index is None or not chunks:
        return []

    tokenized_query = _tokenize(query)
    scores = index.get_scores(tokenized_query)
    top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    # Only return chunks with a non-trivial score
    return [chunks[i] for i in top_indices if scores[i] > 0.5]


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())
