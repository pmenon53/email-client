"""Knowledge corpus: text extraction (PDF, DOCX), caching, corpus builder.

PRD references: §5.3 (loading & caching), §10 (size limit known constraint).

Flow (per run):
    files = drive_client.list_knowledge_files(folder_id)   # id + modifiedTime
    key   = compute_cache_key(files)
    hit?  -> restore corpus text from .knowledge_cache/, skip download
    miss? -> download + extract (PDF->pdfplumber, DOCX->python-docx),
             concatenate, write corpus text to cache under the new key.

The cache key is a hash of every (fileId, modifiedTime) pair, so it changes
whenever any document is added, removed, or edited — stale content is never
served (§5.3). The GitHub Actions Cache layer persists .knowledge_cache/ across
runs; this module manages the corpus file inside it.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os

import pdfplumber
from docx import Document

from . import drive_client

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = ".knowledge_cache"

# Bump when extraction/concatenation logic changes so old cached corpora are
# invalidated even if the documents themselves are unchanged.
CORPUS_VERSION = "1"

# §5.3 / §10: warn (but proceed) above ~80,000 tokens. We estimate ~4 chars per
# token, so ~320k characters. v1 passes the full corpus in context; chunking is
# a v2 concern.
MAX_CORPUS_TOKENS = 80_000
CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


# --- 3.1 Text extraction ------------------------------------------------------
def extract_text(file_bytes: bytes, mime_type: str) -> str:
    """Extract plain text from a PDF or DOCX byte payload.

    PDF  -> pdfplumber (page-by-page text).
    DOCX -> python-docx (paragraph text).

    Raises ValueError for any unsupported mime type (callers pre-filter via
    drive_client.is_supported, so this is a defensive guard).
    """
    if mime_type == drive_client.PDF_MIME:
        return _extract_pdf(file_bytes)
    if mime_type == drive_client.DOCX_MIME:
        return _extract_docx(file_bytes)
    raise ValueError(f"Unsupported mime type for extraction: {mime_type!r}")


def _extract_pdf(file_bytes: bytes) -> str:
    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages).strip()


def _extract_docx(file_bytes: bytes) -> str:
    document = Document(io.BytesIO(file_bytes))
    paragraphs = [para.text for para in document.paragraphs]
    return "\n".join(paragraphs).strip()


# --- 3.3 Cache key ------------------------------------------------------------
def compute_cache_key(files: list[dict]) -> str:
    """Hash of all (fileId, modifiedTime) pairs, sorted by fileId (§5.3).

    Includes CORPUS_VERSION so an extraction-logic change invalidates the cache.
    """
    parts = sorted((f["id"], f.get("modifiedTime", "")) for f in files)
    payload = CORPUS_VERSION + "|" + "|".join(f"{fid}:{mtime}" for fid, mtime in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _corpus_path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, f"corpus-{key}.txt")


# --- 3.4 / 3.5 Cache read / write ---------------------------------------------
def read_cache(cache_dir: str, key: str) -> str | None:
    """Return the cached corpus for ``key``, or None on a miss."""
    path = _corpus_path(cache_dir, key)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            logger.info("Knowledge cache HIT (%s).", key[:12])
            return fh.read()
    logger.info("Knowledge cache MISS (%s).", key[:12])
    return None


def write_cache(cache_dir: str, key: str, corpus: str) -> None:
    """Persist the corpus text under ``key`` in the cache directory."""
    os.makedirs(cache_dir, exist_ok=True)
    with open(_corpus_path(cache_dir, key), "w", encoding="utf-8") as fh:
        fh.write(corpus)


# --- 3.2 / 3.6 Corpus builder + size guard ------------------------------------
def _check_size(corpus: str) -> None:
    tokens = _estimate_tokens(corpus)
    if tokens > MAX_CORPUS_TOKENS:
        logger.warning(
            "Knowledge corpus is ~%d tokens, above the ~%d-token v1 limit. "
            "Proceeding anyway (PRD §10); consider chunking/retrieval for v2.",
            tokens,
            MAX_CORPUS_TOKENS,
        )


def build_corpus(
    drive_service,
    folder_id: str,
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> str:
    """Build (or restore from cache) the full knowledge corpus string.

    On a cache hit the documents are not re-downloaded. On a miss every
    supported file is downloaded, extracted, concatenated with a labelled
    separator per document, cached, and returned.
    """
    files = drive_client.list_knowledge_files(drive_service, folder_id)
    if not files:
        logger.warning("No supported knowledge documents found in folder %r.", folder_id)
        return ""

    key = compute_cache_key(files)
    cached = read_cache(cache_dir, key)
    if cached is not None:
        return cached

    sections: list[str] = []
    for meta in files:
        raw = drive_client.download_file(drive_service, meta["id"])
        text = extract_text(raw, meta["mimeType"])
        name = meta.get("name", meta["id"])
        sections.append(f"=== {name} ===\n{text}")
        logger.info("Extracted %d chars from %r.", len(text), name)

    corpus = "\n\n".join(sections).strip()
    _check_size(corpus)
    write_cache(cache_dir, key, corpus)
    return corpus
