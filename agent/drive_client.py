"""Drive API wrapper: list and download knowledge documents.

PRD references: §5.3 (knowledge loading), §9 (Drive unavailable -> abort run).

Only PDF and DOCX files in the designated folder are considered. Any Drive API
failure raises ``DriveError`` which, left unhandled by the orchestrator, exits
non-zero so GitHub Actions marks the job failed and emails the repo owner.
"""

from __future__ import annotations

import io
import logging

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUPPORTED_MIME_TYPES = frozenset({PDF_MIME, DOCX_MIME})


class DriveError(RuntimeError):
    """Raised when the Drive API is unavailable or returns an error (§9)."""


# --- 2.9 Service --------------------------------------------------------------
def build_service(credentials):
    """Build an authenticated Drive API service from OAuth credentials."""
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


# --- 2.10 List knowledge files ------------------------------------------------
def is_supported(file_meta: dict) -> bool:
    """True if the file is a PDF or DOCX (the only supported types, §5.3)."""
    return file_meta.get("mimeType") in SUPPORTED_MIME_TYPES


def list_knowledge_files(service, folder_id: str) -> list[dict]:
    """List supported files in the folder with id, name, modifiedTime, mimeType.

    Results are sorted by id for a stable cache key (§5.3). Raises DriveError on
    any API failure.
    """
    query = f"'{folder_id}' in parents and trashed = false"
    files: list[dict] = []
    page_token = None
    try:
        while True:
            resp = (
                service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, modifiedTime, mimeType)",
                    pageToken=page_token,
                    pageSize=100,
                )
                .execute()
            )
            files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        raise DriveError(
            f"Failed to list Drive folder {folder_id!r}: {exc}"
        ) from exc

    supported = [f for f in files if is_supported(f)]
    skipped = len(files) - len(supported)
    if skipped:
        logger.info("Skipped %d unsupported file(s) in Drive folder.", skipped)
    supported.sort(key=lambda f: f["id"])
    return supported


# --- 2.11 Download ------------------------------------------------------------
def download_file(service, file_id: str) -> bytes:
    """Download a file's raw bytes. Raises DriveError on failure."""
    try:
        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        return buffer.getvalue()
    except HttpError as exc:
        raise DriveError(f"Failed to download Drive file {file_id!r}: {exc}") from exc
