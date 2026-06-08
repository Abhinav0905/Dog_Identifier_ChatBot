#!/usr/bin/env python3
"""
Scrape Dharamsala Animal Rescue pages into markdown files for RAG ingestion.

Usage:
    python3 scripts/scrape_dar_site.py --scope projects --delay 10
    python3 scripts/ingest_docs.py --chroma --clear-chroma
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

import config

DEFAULT_OUTPUT_DIR = Path(__file__).parent.parent / "rag_docs" / "scraped"
DEFAULT_PROJECT_MANIFEST = Path(__file__).parent.parent / "reports" / "projects_scrape_manifest.json"
DEFAULT_PROJECTS_URL = "https://dharamsalaanimalrescue.org/projects/"
USER_AGENT = "Gaia DAR RAG scraper (+https://github.com/Abhinav0905/Dog_Identifier_ChatBot)"
PROJECT_NOISE_PATHS = (
    "/author/",
    "/category/",
    "/cart",
    "/checkout",
    "/donate",
    "/newsletter",
    "/privacy",
    "/shop",
    "/sponsor",
    "/subscribe",
    "/thank-you",
)
PROJECT_NOISE_EXACT_PATHS = ("/", "/about")


def normalize_url(url: str) -> str:
    url, _fragment = urldefrag(url)
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return parsed._replace(path=path, query="").geturl()


def same_site(url: str, base_netloc: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc == base_netloc


def should_skip_url(url: str) -> bool:
    lower = url.lower()
    skip_ext = (
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip",
        ".mp4", ".mov", ".mp3", ".css", ".js", ".xml",
    )
    skip_parts = (
        "/wp-admin", "/wp-json", "/feed", "/cart", "/checkout",
        "/cdn-cgi", "%20", "mailto:", "tel:", "javascript:",
    )
    return lower.endswith(skip_ext) or any(part in lower for part in skip_parts)


def should_skip_scoped_url(
    url: str,
    excluded_path_parts: tuple[str, ...],
    excluded_exact_paths: tuple[str, ...] = (),
) -> bool:
    path = urlparse(url).path.lower()
    exact_paths = {part.lower().rstrip("/") or "/" for part in excluded_exact_paths}
    normalized_path = path.rstrip("/") or "/"
    return (
        should_skip_url(url)
        or normalized_path in exact_paths
        or any(part.lower() in path for part in excluded_path_parts)
    )


def slug_for_url(url: str) -> str:
    parsed = urlparse(url)
    slug = parsed.path.strip("/").replace("/", "__")
    if not slug:
        slug = "home"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-").lower()
    return f"{slug or 'home'}.md"


def fetch_html(session: requests.Session, url: str, timeout: int = 20) -> str | None:
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"  skip {url}: {exc}", flush=True)
        return None

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        return None
    return response.text


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "form"]):
        tag.decompose()
    for selector in ("header", "footer", "nav", ".menu", ".sidebar", ".elementor-location-footer"):
        for tag in soup.select(selector):
            tag.decompose()
    return soup


def _content_container(soup: BeautifulSoup):
    return soup.find("main") or soup.find("article") or soup.body or soup


def extract_markdown(html: str, url: str) -> tuple[str, str]:
    soup = _clean_soup(html)
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    title = clean_text(title) or "Dharamsala Animal Rescue"

    container = _content_container(soup)
    lines: list[str] = [f"# {title}", f"Source: {url}", ""]
    seen = set()
    for tag in container.find_all(["h2", "h3", "h4", "p", "li"]):
        text = clean_text(tag.get_text(" ", strip=True))
        if len(text) < 20 or text in seen:
            continue
        seen.add(text)
        if tag.name in {"h2", "h3", "h4"}:
            level = {"h2": "##", "h3": "###", "h4": "####"}[tag.name]
            lines.extend([f"{level} {text}", ""])
        elif tag.name == "li":
            lines.append(f"- {text}")
        else:
            lines.extend([text, ""])

    return title, "\n".join(lines).strip() + "\n"


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_links(
    html: str,
    current_url: str,
    base_netloc: str,
    *,
    content_only: bool = False,
    excluded_path_parts: tuple[str, ...] = (),
    excluded_exact_paths: tuple[str, ...] = (),
) -> list[str]:
    soup = _clean_soup(html) if content_only else BeautifulSoup(html, "html.parser")
    container = _content_container(soup) if content_only else soup
    links = []
    seen: set[str] = set()
    for anchor in container.find_all("a", href=True):
        href = anchor["href"].strip()
        absolute = normalize_url(urljoin(current_url, href))
        if (
            absolute not in seen
            and same_site(absolute, base_netloc)
            and not should_skip_scoped_url(absolute, excluded_path_parts, excluded_exact_paths)
        ):
            links.append(absolute)
            seen.add(absolute)
    return links


def _write_manifest(manifest_path: Path, payload: dict) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def scrape(
    base_url: str,
    output_dir: Path,
    max_pages: int,
    delay: float,
    timeout: int,
    fresh: bool,
    *,
    content_links_only: bool = False,
    max_depth: int | None = None,
    min_words: int = 80,
    excluded_path_parts: tuple[str, ...] = (),
    excluded_exact_paths: tuple[str, ...] = (),
    manifest_path: Path | None = None,
) -> dict:
    base_url = normalize_url(base_url)
    base_netloc = urlparse(base_url).netloc

    if fresh and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    visited: set[str] = set()
    queue: deque[tuple[str, int, str | None]] = deque([(base_url, 0, None)])
    saved = 0
    pages: list[dict] = []

    while queue and len(visited) < max_pages:
        url, depth, discovered_from = queue.popleft()
        if url in visited or should_skip_scoped_url(url, excluded_path_parts, excluded_exact_paths):
            continue
        visited.add(url)

        print(f"[{len(visited):03d}/{max_pages}] depth={depth} {url}", flush=True)
        html = fetch_html(session, url, timeout=timeout)
        if not html:
            pages.append({
                "url": url,
                "depth": depth,
                "discovered_from": discovered_from,
                "status": "fetch_failed",
            })
            continue

        title, markdown = extract_markdown(html, url)
        word_count = len(markdown.split())
        page_record = {
            "url": url,
            "title": title,
            "depth": depth,
            "discovered_from": discovered_from,
            "word_count": word_count,
            "status": "below_min_words",
        }
        if word_count >= min_words:
            path = output_dir / slug_for_url(url)
            path.write_text(markdown, encoding="utf-8")
            saved += 1
            page_record["status"] = "saved"
            page_record["output_file"] = str(path.relative_to(Path(__file__).parent.parent))
        pages.append(page_record)

        if max_depth is None or depth < max_depth:
            for link in extract_links(
                html,
                url,
                base_netloc,
                content_only=content_links_only,
                excluded_path_parts=excluded_path_parts,
                excluded_exact_paths=excluded_exact_paths,
            ):
                if link not in visited:
                    queue.append((link, depth + 1, url))

        if delay:
            time.sleep(delay)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "content_links_only": content_links_only,
        "max_depth": max_depth,
        "max_pages": max_pages,
        "min_words": min_words,
        "excluded_path_parts": list(excluded_path_parts),
        "excluded_exact_paths": list(excluded_exact_paths),
        "visited_count": len(visited),
        "saved_count": saved,
        "queue_remaining": len(queue),
        "pages": pages,
    }
    if manifest_path:
        _write_manifest(manifest_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description="Scrape DAR website content into rag_docs/scraped")
    parser.add_argument("--scope", choices=("site", "projects"), default="site")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--max-pages", type=int, default=config.DAR_SCRAPE_MAX_PAGES)
    parser.add_argument("--max-depth", type=int, default=None, help="Maximum link depth from the base URL")
    parser.add_argument("--min-words", type=int, default=80, help="Minimum extracted words required to save a page")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--content-links-only", action="store_true", help="Follow links only from cleaned page content")
    parser.add_argument("--exclude-path", action="append", default=[], help="Skip URLs containing this path fragment")
    parser.add_argument("--exclude-exact-path", action="append", default=[], help="Skip URLs with this exact path")
    parser.add_argument("--manifest", default=None, help="Write a JSON crawl coverage manifest")
    parser.add_argument("--fresh", action="store_true", help="Delete existing scraped markdown first")
    args = parser.parse_args()

    base_url = args.base_url or config.DAR_SCRAPE_BASE_URL
    content_links_only = args.content_links_only
    max_depth = args.max_depth
    excluded_path_parts = tuple(args.exclude_path)
    excluded_exact_paths = tuple(args.exclude_exact_path)
    manifest_path = Path(args.manifest) if args.manifest else None
    if args.scope == "projects":
        base_url = args.base_url or DEFAULT_PROJECTS_URL
        content_links_only = True
        max_depth = 3 if max_depth is None else max_depth
        excluded_path_parts = tuple(dict.fromkeys((*PROJECT_NOISE_PATHS, *excluded_path_parts)))
        excluded_exact_paths = tuple(dict.fromkeys((*PROJECT_NOISE_EXACT_PATHS, *excluded_exact_paths)))
        manifest_path = manifest_path or DEFAULT_PROJECT_MANIFEST

    result = scrape(
        base_url=base_url,
        output_dir=Path(args.output_dir),
        max_pages=args.max_pages,
        delay=args.delay,
        timeout=args.timeout,
        fresh=args.fresh,
        content_links_only=content_links_only,
        max_depth=max_depth,
        min_words=args.min_words,
        excluded_path_parts=excluded_path_parts,
        excluded_exact_paths=excluded_exact_paths,
        manifest_path=manifest_path,
    )
    print(
        f"Scrape complete. Visited {result['visited_count']} and saved "
        f"{result['saved_count']} markdown page(s) to {args.output_dir}",
        flush=True,
    )
    if manifest_path:
        print(f"Coverage manifest: {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
