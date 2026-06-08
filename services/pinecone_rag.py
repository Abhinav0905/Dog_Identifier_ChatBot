"""
Pinecone-backed hybrid retrieval.

Dense vectors are generated locally with sentence-transformers. Sparse vectors
use a stable hashing encoder so the same text always maps to the same lexical
dimensions without needing a fitted vocabulary file.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from functools import lru_cache
from typing import Iterable

import config

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"[a-z0-9]+")


def is_configured() -> bool:
    return bool(config.PINECONE_API_KEY and config.PINECONE_INDEX_NAME)


@lru_cache(maxsize=1)
def _pinecone_client():
    from pinecone import Pinecone

    return Pinecone(api_key=config.PINECONE_API_KEY)


@lru_cache(maxsize=1)
def _index():
    return _pinecone_client().Index(config.PINECONE_INDEX_NAME)


@lru_cache(maxsize=1)
def _embedder():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(config.RAG_EMBEDDING_MODEL)


def ensure_index() -> None:
    """Create the configured Pinecone hybrid index if it does not exist."""
    if not is_configured():
        raise RuntimeError("PINECONE_API_KEY and PINECONE_INDEX_NAME are required")

    from pinecone import ServerlessSpec

    pc = _pinecone_client()
    if pc.has_index(config.PINECONE_INDEX_NAME):
        return

    pc.create_index(
        name=config.PINECONE_INDEX_NAME,
        vector_type="dense",
        dimension=config.RAG_DENSE_DIMENSION,
        metric="dotproduct",
        spec=ServerlessSpec(
            cloud=config.PINECONE_CLOUD,
            region=config.PINECONE_REGION,
        ),
    )


def clear_namespace() -> None:
    if not is_configured():
        return
    ensure_index()
    _index().delete(delete_all=True, namespace=config.PINECONE_NAMESPACE)


def upsert_chunks(chunks: list[dict], batch_size: int = 64) -> int:
    """Embed and upsert chunks into Pinecone.

    Expected chunk fields: id, content, title, doc_file, chunk_index, source_url.
    """
    if not chunks:
        return 0
    ensure_index()

    stored = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        texts = [chunk["content"] for chunk in batch]
        dense_vectors = _encode_dense(texts)
        vectors = []
        for chunk, dense in zip(batch, dense_vectors):
            vectors.append(
                {
                    "id": chunk["id"],
                    "values": dense,
                    "sparse_values": encode_sparse(chunk["content"]),
                    "metadata": {
                        "title": chunk.get("title", ""),
                        "content": chunk.get("content", ""),
                        "doc_file": chunk.get("doc_file", ""),
                        "chunk_index": chunk.get("chunk_index", 0),
                        "source_url": chunk.get("source_url", ""),
                    },
                }
            )
        _index().upsert(vectors=vectors, namespace=config.PINECONE_NAMESPACE)
        stored += len(vectors)
    return stored


def retrieve(query: str, k: int = 3) -> list[dict]:
    if not is_configured():
        return []

    try:
        ensure_index()
        dense = _encode_dense([query])[0]
        sparse = encode_sparse(query)
        dense, sparse = hybrid_score_norm(dense, sparse, config.RAG_HYBRID_ALPHA)
        results = _index().query(
            namespace=config.PINECONE_NAMESPACE,
            top_k=k,
            vector=dense,
            sparse_vector=sparse,
            include_metadata=True,
            include_values=False,
        )
    except Exception as exc:  # noqa: BLE001 - Pinecone should not break chat
        logger.warning("Pinecone hybrid retrieval failed: %s", exc)
        return []

    chunks = []
    for match in getattr(results, "matches", []) or []:
        metadata = match.get("metadata", {}) if isinstance(match, dict) else (match.metadata or {})
        chunks.append(
            {
                "title": metadata.get("title", "Dharamsala Animal Rescue"),
                "content": metadata.get("content", ""),
                "doc_file": metadata.get("doc_file", ""),
                "chunk_index": metadata.get("chunk_index", 0),
                "source_url": metadata.get("source_url", ""),
                "score": match.get("score") if isinstance(match, dict) else match.score,
            }
        )
    return chunks


def encode_sparse(text: str) -> dict:
    counts = Counter(_tokenize(text))
    if not counts:
        return {"indices": [], "values": []}

    weights: dict[int, float] = {}
    for token, count in counts.items():
        idx = _stable_hash(token) % config.RAG_SPARSE_DIMENSION
        weights[idx] = weights.get(idx, 0.0) + 1.0 + math.log(count)

    norm = math.sqrt(sum(value * value for value in weights.values())) or 1.0
    items = sorted(weights.items())
    return {
        "indices": [idx for idx, _ in items],
        "values": [value / norm for _, value in items],
    }


def hybrid_score_norm(dense: list[float], sparse: dict, alpha: float) -> tuple[list[float], dict]:
    alpha = max(0.0, min(1.0, alpha))
    return (
        [value * alpha for value in dense],
        {
            "indices": sparse["indices"],
            "values": [value * (1.0 - alpha) for value in sparse["values"]],
        },
    )


def make_chunk_id(doc_file: str, chunk_index: int, content: str) -> str:
    digest = hashlib.sha1(f"{doc_file}:{chunk_index}:{content}".encode("utf-8")).hexdigest()
    return f"dar-{digest[:24]}"


def _encode_dense(texts: Iterable[str]) -> list[list[float]]:
    vectors = _embedder().encode(
        list(texts),
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [vector.tolist() for vector in vectors]


def _tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _stable_hash(token: str) -> int:
    return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
