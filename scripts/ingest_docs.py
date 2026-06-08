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
import argparse
import re
from pathlib import Path
from typing import Sequence

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import MODEL_PROVIDER, RAG_SQLITE_EMBEDDINGS
import database as db
from services import ai_client
from services import chroma_rag
from services import pinecone_rag

RAG_DOCS_DIR = Path(__file__).parent.parent / "rag_docs"
SUPPORTED_DOCUMENT_EXTENSIONS = {".md", ".pdf"}

CHUNK_SIZE = 400    # target words per chunk
CHUNK_OVERLAP = 50  # words carried over to the next chunk

_embedding_disabled = False


def _split_into_chunks(content: str, title: str) -> list[str]:
    """Split document content into sentence-aware overlapping chunks."""
    sentences = _split_sentences(content)

    chunks = []
    current_words: list[str] = []

    for sentence in sentences:
        sentence_words = sentence.split()
        if current_words and len(current_words) + len(sentence_words) > CHUNK_SIZE:
            chunks.append(f"{title}\n\n" + " ".join(current_words))
            current_words = current_words[-CHUNK_OVERLAP:] + sentence_words
        else:
            current_words.extend(sentence_words)

    if current_words:
        chunks.append(f"{title}\n\n" + " ".join(current_words))

    return chunks


def _split_sentences(content: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", content).strip()
    if not normalized:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", normalized) if s.strip()]


def _extract_title(text: str, filepath: Path) -> str:
    """Extract title from the first H1 heading, falling back to the filename."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return re.sub(r"[_-]+", " ", filepath.stem).strip().title()


def _strip_source_lines(text: str) -> str:
    """Remove 'Source: ...' metadata lines from document content."""
    lines = [line for line in text.splitlines() if not line.startswith("Source:")]
    return "\n".join(lines)


def _extract_source_url(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("Source:"):
            return line.split(":", 1)[1].strip()
    return ""


def _load_document(
    filepath: Path,
    *,
    ocr_pdfs: bool = False,
    ocr_mode: str = "fast",
    ocr_languages: Sequence[str] | None = None,
) -> tuple[str, str, str]:
    """Return (content, title, source_url) for a supported document."""
    suffix = filepath.suffix.lower()
    if suffix == ".md":
        raw = filepath.read_text(encoding="utf-8")
        return _strip_source_lines(raw), _extract_title(raw, filepath), _extract_source_url(raw)
    if suffix == ".pdf":
        content, pdf_title = _extract_pdf_text(
            filepath,
            ocr_pdfs=ocr_pdfs,
            ocr_mode=ocr_mode,
            ocr_languages=ocr_languages,
        )
        return content, pdf_title or _extract_title(content, filepath), ""
    raise ValueError(f"Unsupported document type: {filepath}")


def _extract_pdf_text(
    filepath: Path,
    *,
    ocr_pdfs: bool,
    ocr_mode: str,
    ocr_languages: Sequence[str] | None,
) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF ingestion requires pypdf. Install it with `pip install pypdf`.") from exc

    reader = PdfReader(str(filepath))
    pdf_title = ""
    if reader.metadata:
        pdf_title = str(getattr(reader.metadata, "title", "") or "").strip()

    page_blocks: list[str] = []
    empty_pages: list[int] = []
    ocr_doc = None
    for page_index, page in enumerate(reader.pages):
        page_number = page_index + 1
        text = (page.extract_text() or "").strip()
        if not text and ocr_pdfs:
            if ocr_doc is None:
                ocr_doc = _open_pdf_for_ocr(filepath)
            text = _ocr_pdf_page(
                ocr_doc,
                page_index,
                recognition_level=ocr_mode,
                languages=ocr_languages,
            )
        if text:
            page_blocks.append(f"Page {page_number}\n{text}")
        else:
            empty_pages.append(page_number)

    if ocr_doc is not None:
        ocr_doc.close()

    if empty_pages:
        page_list = ", ".join(str(p) for p in empty_pages[:12])
        suffix = "..." if len(empty_pages) > 12 else ""
        print(f"    warning: no text extracted from PDF page(s): {page_list}{suffix}")

    if not page_blocks:
        hint = " Re-run with --ocr-pdfs on macOS after installing PyMuPDF and ocrmac." if not ocr_pdfs else ""
        raise ValueError(f"No extractable text found in {filepath}.{hint}")

    return "\n\n".join(page_blocks), pdf_title


def _open_pdf_for_ocr(filepath: Path):
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("OCR PDF ingestion requires PyMuPDF. Install it with `pip install PyMuPDF`.") from exc
    return fitz.open(str(filepath))


def _ocr_pdf_page(
    pdf_doc,
    page_index: int,
    *,
    recognition_level: str,
    languages: Sequence[str] | None,
) -> str:
    try:
        import fitz
        from PIL import Image
        from ocrmac.ocrmac import OCR
    except ImportError as exc:
        raise RuntimeError("OCR PDF ingestion on macOS requires PyMuPDF and ocrmac.") from exc

    page = pdf_doc.load_page(page_index)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    lines = OCR(
        image,
        recognition_level=recognition_level,
        language_preference=list(languages) if languages else None,
    ).recognize()
    return "\n".join(line[0].strip() for line in lines if line and line[0].strip())


def _document_file_id(filepath: Path, docs_dir: Path) -> str:
    try:
        return filepath.resolve().relative_to(docs_dir.resolve()).as_posix()
    except ValueError:
        return filepath.name


def build_chunk_records(
    filepath: Path,
    docs_dir: Path = RAG_DOCS_DIR,
    *,
    ocr_pdfs: bool = False,
    ocr_mode: str = "fast",
    ocr_languages: Sequence[str] | None = None,
) -> list[dict]:
    """Build chunk records from one markdown or PDF file."""
    content, title, source_url = _load_document(
        filepath,
        ocr_pdfs=ocr_pdfs,
        ocr_mode=ocr_mode,
        ocr_languages=ocr_languages,
    )
    doc_file = _document_file_id(filepath, docs_dir)
    chunks = _split_into_chunks(content, title)
    return [
        {
            "id": pinecone_rag.make_chunk_id(doc_file, i, chunk_text),
            "doc_file": doc_file,
            "title": title,
            "chunk_index": i,
            "content": chunk_text,
            "source_url": source_url,
        }
        for i, chunk_text in enumerate(chunks)
    ]


def ingest_file(
    filepath: Path,
    docs_dir: Path = RAG_DOCS_DIR,
    store_sqlite: bool = True,
    *,
    ocr_pdfs: bool = False,
    ocr_mode: str = "fast",
    ocr_languages: Sequence[str] | None = None,
) -> tuple[int, list[dict]]:
    """Ingest one markdown or PDF file. Returns (stored_sqlite_count, chunk_records)."""
    global _embedding_disabled

    records = build_chunk_records(
        filepath,
        docs_dir,
        ocr_pdfs=ocr_pdfs,
        ocr_mode=ocr_mode,
        ocr_languages=ocr_languages,
    )
    doc_file = _document_file_id(filepath, docs_dir)

    if not store_sqlite:
        return 0, records

    db.delete_rag_chunks_for_doc(doc_file)
    stored = 0
    for record in records:
        embedding_json = None
        if (
            RAG_SQLITE_EMBEDDINGS
            and MODEL_PROVIDER == "openai"
            and ai_client.is_available()
            and not _embedding_disabled
        ):
            try:
                embedding = ai_client.create_embedding(record["content"])
                if embedding:
                    embedding_json = json.dumps(embedding)
            except Exception as exc:  # noqa: BLE001 - store text chunks for BM25 fallback
                _embedding_disabled = True
                print(f"    embedding unavailable; storing text chunks without embeddings ({exc})")

        db.insert_rag_chunk(
            doc_file=record["doc_file"],
            title=record["title"],
            chunk_index=record["chunk_index"],
            content=record["content"],
            embedding=embedding_json,
        )
        stored += 1
        print(f"    chunk {record['chunk_index'] + 1}/{len(records)} stored")

    return stored, records


def _collect_document_files(docs_dir: Path, extra_docs: Sequence[str]) -> list[Path]:
    files = sorted(
        path
        for path in docs_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS
    )

    for raw_path in extra_docs:
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Document path not found: {path}")
        if path.is_dir():
            files.extend(
                sorted(
                    child
                    for child in path.rglob("*")
                    if child.is_file() and child.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS
                )
            )
        elif path.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS:
            files.append(path)
        else:
            raise ValueError(f"Unsupported document type: {path}")

    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved not in seen:
            deduped.append(path)
            seen.add(resolved)
    return deduped


def main():
    parser = argparse.ArgumentParser(description="Ingest markdown/PDF RAG docs into SQLite and vector stores.")
    parser.add_argument("--docs-dir", default=str(RAG_DOCS_DIR), help="Directory containing markdown/PDF docs")
    parser.add_argument("--doc", action="append", default=[], help="Additional markdown/PDF file or directory to ingest")
    parser.add_argument("--ocr-pdfs", action="store_true", help="OCR image-only PDF pages using macOS Vision via PyMuPDF + ocrmac")
    parser.add_argument("--ocr-mode", choices=["fast", "accurate"], default="fast", help="macOS Vision OCR recognition mode")
    parser.add_argument("--ocr-language", action="append", default=None, help="OCR language tag such as en-US; can be repeated")
    parser.add_argument("--chroma", action="store_true", help="Upsert chunks into local Chroma vector DB")
    parser.add_argument("--clear-chroma", action="store_true", help="Delete and recreate the Chroma collection before upsert")
    parser.add_argument("--pinecone", action="store_true", help="Upsert chunks into Pinecone hybrid index")
    parser.add_argument("--clear-pinecone-namespace", action="store_true", help="Delete all vectors in the namespace before upsert")
    parser.add_argument("--no-sqlite", action="store_true", help="Skip local SQLite RAG storage")
    args = parser.parse_args()

    docs_dir = Path(args.docs_dir)
    db.init_db()

    doc_files = _collect_document_files(docs_dir, args.doc)
    if not doc_files:
        print(f"No supported documents found in {docs_dir}")
        sys.exit(1)

    mode = (
        "OpenAI text-embedding-3-small"
        if RAG_SQLITE_EMBEDDINGS and MODEL_PROVIDER == "openai"
        else "BM25/text chunks (no SQLite embeddings)"
    )
    print(f"Provider : {MODEL_PROVIDER}")
    print(f"SQLite  : {'disabled' if args.no_sqlite else mode}")
    print(f"Chroma  : {'enabled' if args.chroma else 'disabled'}")
    print(f"Pinecone: {'enabled' if args.pinecone else 'disabled'}")
    print(f"PDF OCR : {'enabled' if args.ocr_pdfs else 'disabled'}")
    print(f"Documents: {len(doc_files)}\n")

    sqlite_total = 0
    all_records: list[dict] = []
    for filepath in doc_files:
        display_path = _document_file_id(filepath, docs_dir)
        print(f"  {display_path}")
        n, records = ingest_file(
            filepath,
            docs_dir,
            store_sqlite=not args.no_sqlite,
            ocr_pdfs=args.ocr_pdfs,
            ocr_mode=args.ocr_mode,
            ocr_languages=args.ocr_language,
        )
        sqlite_total += n
        all_records.extend(records)
        print(f"  → {len(records)} chunk(s)\n")

    if args.chroma:
        if not chroma_rag.is_available():
            print("Chroma skipped: install chromadb and sentence-transformers first")
        else:
            if args.clear_chroma:
                print("Clearing Chroma collection...")
                chroma_rag.clear_collection()
            print(f"Upserting {len(all_records)} chunks to Chroma...")
            chroma_total = chroma_rag.upsert_chunks(all_records)
            print(f"Chroma ingestion complete. Total vectors: {chroma_total}")

    if args.pinecone:
        if not pinecone_rag.is_configured():
            print("Pinecone skipped: set PINECONE_API_KEY and PINECONE_INDEX_NAME first")
        else:
            if args.clear_pinecone_namespace:
                print("Clearing Pinecone namespace...")
                pinecone_rag.clear_namespace()
            print(f"Upserting {len(all_records)} chunks to Pinecone...")
            pinecone_total = pinecone_rag.upsert_chunks(all_records)
            print(f"Pinecone ingestion complete. Total vectors: {pinecone_total}")

    # Invalidate in-memory BM25 cache so the server picks up the new data
    from services.rag import reset_cache
    reset_cache()

    print(f"Ingestion complete. SQLite chunks: {sqlite_total}; built chunks: {len(all_records)}")


if __name__ == "__main__":
    main()
