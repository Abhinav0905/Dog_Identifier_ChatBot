"""
Local Chroma vector store for RAG.

Uses sentence-transformers to create local dense embeddings and persists the
collection on disk, so the app can retrieve knowledge without a hosted vector DB.
"""

from __future__ import annotations

import logging
import math
import time
import threading
from functools import lru_cache
from typing import Iterable

import config

logger = logging.getLogger(__name__)
_warmed_embedder = None
_warm_lock = threading.Lock()


def is_available() -> bool:
    try:
        import chromadb  # noqa: F401
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


@lru_cache(maxsize=1)
def _client():
    import chromadb

    config.CHROMA_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(config.CHROMA_PERSIST_DIR))


@lru_cache(maxsize=1)
def _collection():
    return _client().get_or_create_collection(
        name=config.CHROMA_COLLECTION_NAME,
        metadata=_hnsw_metadata(),
    )


def _hnsw_metadata() -> dict:
    """Legacy Chroma metadata keys used to create a persisted HNSW index."""
    return {
        "hnsw:space": config.CHROMA_HNSW_SPACE,
        "hnsw:construction_ef": config.CHROMA_HNSW_CONSTRUCTION_EF,
        "hnsw:search_ef": config.CHROMA_HNSW_SEARCH_EF,
        "hnsw:M": config.CHROMA_HNSW_M,
    }


@lru_cache(maxsize=1)
def _embedder():
    from sentence_transformers import SentenceTransformer

    return _warmed_embedder or SentenceTransformer(config.RAG_EMBEDDING_MODEL)


def warm_up() -> bool:
    """Prepare serving from locally cached weights without downloading in a request."""
    global _warmed_embedder
    from services import web_operations
    with _warm_lock:
        if _embedder.cache_info().currsize:
            return True
        try:
            from sentence_transformers import SentenceTransformer
            _warmed_embedder = SentenceTransformer(config.RAG_EMBEDDING_MODEL, local_files_only=True)
            _embedder()
            web_operations.record_event("rag:warmup", "ok")
            return True
        except Exception as exc:
            web_operations.record_event("rag:warmup", "error", error_type=type(exc).__name__)
            logger.warning("Local embeddings unavailable; SQLite retrieval remains available (%s)", type(exc).__name__)
            return False


def clear_collection() -> None:
    if not is_available():
        return
    client = _client()
    try:
        client.delete_collection(config.CHROMA_COLLECTION_NAME)
    except Exception:
        pass
    _collection.cache_clear()


def upsert_chunks(chunks: list[dict], batch_size: int = 64) -> int:
    """Embed and upsert chunks into local Chroma."""
    if not chunks:
        return 0
    if not is_available():
        raise RuntimeError("Install chromadb and sentence-transformers to use Chroma RAG")

    collection = _collection()
    stored = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        documents = [chunk["content"] for chunk in batch]
        embeddings = _encode_dense(documents)
        collection.upsert(
            ids=[chunk["id"] for chunk in batch],
            documents=documents,
            embeddings=embeddings,
            metadatas=[
                {
                    "title": chunk.get("title", ""),
                    "doc_file": chunk.get("doc_file", ""),
                    "chunk_index": int(chunk.get("chunk_index", 0)),
                    "source_url": chunk.get("source_url", ""),
                }
                for chunk in batch
            ],
        )
        stored += len(batch)
    return stored


def retrieve(query: str, k: int = 3, deadline: float | None = None) -> list[dict]:
    from services.rag import MIN_RELEVANCE

    # Cold local model loading may download weights and exceed an HTTP budget.
    # A request with a deadline can use the SQLite lexical fallback instead.
    if deadline is not None and (_embedder.cache_info().currsize == 0 or deadline - time.monotonic() < 3):
        return []
    if not is_available():
        return []

    try:
        collection = _collection()
        if collection.count() == 0:
            return []
        result = collection.query(
            query_embeddings=_encode_dense([query]),
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:  # noqa: BLE001 - local fallback should continue
        logger.warning("Chroma retrieval failed: %s", exc)
        return []

    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]

    chunks = []
    for document, metadata, distance in zip(documents, metadatas, distances):
        metadata = metadata or {}
        score = 1.0 - float(distance)
        if not math.isfinite(score) or score < MIN_RELEVANCE:
            continue
        chunks.append(
            {
                "title": metadata.get("title", "Dharamsala Animal Rescue"),
                "content": document,
                "doc_file": metadata.get("doc_file", ""),
                "chunk_index": metadata.get("chunk_index", 0),
                "source_url": metadata.get("source_url", ""),
                "score": score,
                "relevance_score": score,
                "retrieval_backend": "chroma",
            }
        )
    return chunks


def _encode_dense(texts: Iterable[str]) -> list[list[float]]:
    vectors = _embedder().encode(
        list(texts),
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [vector.tolist() for vector in vectors]
