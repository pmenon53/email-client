"""Orchestrator: fetch -> triage -> draft, with the Gmail-label state machine.

PRD references: §4 (system overview), §5 (all detailed requirements), §9
(observability & error handling). State lives entirely in Gmail labels.

Per-thread flow:
    skip filters (existing draft; already-processed w/o re-open)
      -> malformed (no body)            -> Agent-Processed
      -> triage == noise                -> Agent-Processed
      -> triage == customer_query
            -> draft answer found        -> reply-all draft + Agent-Processed
            -> NO_ANSWER_FOUND           -> Needs-Human + Agent-Processed
    Re-opened threads (new external message newer than the watermark) are
    re-processed on the newest message only.

Transient failures (LLM retries exhausted, Gmail draft-save error) leave the
thread WITHOUT Agent-Processed so it is retried next run.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from email.utils import parseaddr

# Load a local .env if present. No-op in CI, where the vars come from secrets.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from . import auth, drafter, drive_client, gmail_client, knowledge, llm, triage
from .gmail_client import (
    LABEL_NEEDS_HUMAN,
    LABEL_PROCESSED,
    WATERMARK_PREFIX,
)

logger = logging.getLogger(__name__)

# Default fetch window (PRD §5.1): overlap the hourly cadence to avoid gaps.
DEFAULT_WINDOW_MINUTES = 90


# --- Config helpers -----------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using %d.", name, raw, default)
        return default


def _fetch_window_and_cap(first_run: bool) -> tuple[int, int]:
    """Choose (window_minutes, max_emails) for this run.

    On the very first run (no Agent-Processed label exists yet) and when
    FIRST_RUN_HOURS is set, look back further and use FIRST_RUN_MAX_EMAILS so
    the inbox can be backfilled. Otherwise use the steady-state 90-minute window
    and MAX_EMAILS_PER_RUN. A cap of 0 means unlimited (PRD §5.1 default).
    """
    first_run_hours = _env_int("FIRST_RUN_HOURS", 0)
    if first_run and first_run_hours > 0:
        return first_run_hours * 60, _env_int("FIRST_RUN_MAX_EMAILS", 0)
    return DEFAULT_WINDOW_MINUTES, _env_int("MAX_EMAILS_PER_RUN", 0)


def _apply_cap(thread_ids: list[str], cap: int) -> list[str]:
    return thread_ids if cap <= 0 else thread_ids[:cap]


# --- Pure helpers (no network; unit-tested) -----------------------------------
def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _iso_to_ms(iso: str) -> int:
    """Parse an ISO-8601 watermark timestamp to epoch milliseconds (0 on error)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return 0


def _watermark_ms(label_names: set[str]) -> int:
    """Latest watermark time (ms) encoded in the thread's labels, else 0."""
    stamps = [
        _iso_to_ms(name[len(WATERMARK_PREFIX):])
        for name in label_names
        if name.startswith(WATERMARK_PREFIX)
    ]
    return max(stamps, default=0)


def _is_external(from_header: str, self_addr: str) -> bool:
    """True if the message sender is someone other than our own mailbox."""
    _name, addr = parseaddr(from_header or "")
    addr = addr.strip().lower()
    return bool(addr) and addr != self_addr.strip().lower()


def _should_reprocess(newest_is_external: bool, newest_ts_ms: int, watermark_ms: int) -> bool:
    """Re-open rule (§5.5): re-process only if the newest message is external
    and arrived after the last time we processed this thread."""
    return newest_is_external and newest_ts_ms > watermark_ms


# --- Orchestration context ----------------------------------------------------
class Context:
    def __init__(self, gmail, self_addr, processed_id, needs_human_id, corpus):
        self.gmail = gmail
        self.self_addr = self_addr
        self.processed_id = processed_id
        self.needs_human_id = needs_human_id
        self.corpus = corpus


def _set_watermark(ctx: Context, thread_id: str, existing_names: set[str]) -> None:
    """Apply a fresh watermark label, removing any previous ones (§5.6)."""
    new_name = WATERMARK_PREFIX + _iso_now()
    for name in existing_names:
        if name.startswith(WATERMARK_PREFIX) and name != new_name:
            old_id = gmail_client.ensure_label(ctx.gmail, name)
            gmail_client.remove_label(ctx.gmail, thread_id, old_id)
    new_id = gmail_client.ensure_label(ctx.gmail, new_name)
    gmail_client.apply_label(ctx.gmail, thread_id, new_id)


def _mark_processed(ctx: Context, thread_id: str, existing_names: set[str]) -> None:
    gmail_client.apply_label(ctx.gmail, thread_id, ctx.processed_id)
    _set_watermark(ctx, thread_id, existing_names)


def _log(thread_id: str, subject: str, **fields) -> None:
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("thread=%s subject=%r %s", thread_id, (subject or "")[:60], parts)


# --- Per-thread processing (§5.2–§5.9) ----------------------------------------
def process_thread(ctx: Context, thread_id: str, id_to_name: dict[str, str]) -> None:
    thread = gmail_client.get_thread(ctx.gmail, thread_id)
    messages = sorted(
        (gmail_client.parse_message(m) for m in thread.get("messages", [])),
        key=lambda m: m["timestamp_ms"],
    )
    if not messages:
        _log(thread_id, "", outcome="skip", reason="empty_thread")
        return

    newest = messages[-1]
    subject = newest["subject"]
    names = gmail_client.thread_label_names(thread, id_to_name)

    # 5.5/2.5: skip if a draft already exists.
    if gmail_client.thread_has_unsent_draft(thread):
        _log(thread_id, subject, outcome="skip", reason="existing_draft")
        return

    # 5.7/2.4: already processed — only continue on a genuine re-open.
    if LABEL_PROCESSED in names:
        newest_external = _is_external(newest["from"], ctx.self_addr)
        if not _should_reprocess(newest_external, newest["timestamp_ms"], _watermark_ms(names)):
            _log(thread_id, subject, outcome="skip", reason="already_processed")
            return
        _log(thread_id, subject, event="reopen_detected")

    # 5.8: malformed (no body) -> treat as noise.
    if not newest["body"].strip():
        _mark_processed(ctx, thread_id, names)
        _log(thread_id, subject, triage="noise", outcome="processed", reason="no_body")
        return

    # 5.3: triage. LLM failure -> skip without marking processed (retry next run).
    try:
        label = triage.classify(subject, newest["body"])
    except llm.LLMError as exc:
        _log(thread_id, subject, outcome="skip", reason="triage_llm_error", error=str(exc)[:80])
        return

    if label == triage.NOISE:
        _mark_processed(ctx, thread_id, names)
        _log(thread_id, subject, triage="noise", outcome="processed")
        return

    # 5.4/5.5: draft. LLM failure -> skip without marking processed.
    try:
        reply = drafter.draft_reply(subject, newest["body"], ctx.corpus)
    except llm.LLMError as exc:
        _log(thread_id, subject, triage="customer_query", outcome="skip",
             reason="draft_llm_error", error=str(exc)[:80])
        return

    if reply == drafter.NO_ANSWER_SENTINEL:
        gmail_client.apply_label(ctx.gmail, thread_id, ctx.needs_human_id)
        _mark_processed(ctx, thread_id, names)
        _log(thread_id, subject, triage="customer_query", outcome="needs_human")
        return

    # 5.9: draft-save error -> log, do NOT mark processed (retry next run).
    try:
        gmail_client.create_reply_all_draft(
            ctx.gmail, thread_id, newest, ctx.self_addr, reply
        )
    except Exception as exc:  # noqa: BLE001 - any Gmail API error must not mark processed
        _log(thread_id, subject, triage="customer_query", outcome="error",
             reason="draft_save_failed", error=str(exc)[:120])
        return

    _mark_processed(ctx, thread_id, names)
    _log(thread_id, subject, triage="customer_query", outcome="drafted")


# --- Run entrypoint (§5.1) ----------------------------------------------------
def run() -> int:
    folder_id = os.environ.get("DRIVE_FOLDER_ID")
    if not folder_id:
        logger.error("Missing required environment variable 'DRIVE_FOLDER_ID'.")
        return 1

    # Auth / Drive failures propagate -> non-zero exit (PRD §9).
    credentials = auth.get_credentials()
    gmail = gmail_client.build_service(credentials)
    drive = drive_client.build_service(credentials)

    self_addr = gmail_client.get_profile_address(gmail)

    # First run = the Agent-Processed label does not exist yet. Detect before
    # ensure_labels creates it.
    first_run = LABEL_PROCESSED not in set(
        gmail_client.get_label_id_name_map(gmail).values()
    )
    labels = gmail_client.ensure_labels(gmail)
    corpus = knowledge.build_corpus(drive, folder_id)  # cache-aware; DriveError aborts

    window_minutes, cap = _fetch_window_and_cap(first_run)
    thread_ids = _apply_cap(
        gmail_client.fetch_candidate_thread_ids(gmail, window_minutes=window_minutes),
        cap,
    )
    id_to_name = gmail_client.get_label_id_name_map(gmail)
    logger.info(
        "Run mode=%s window=%dm cap=%s; processing %d thread(s).",
        "first_run" if first_run else "steady",
        window_minutes,
        cap or "unlimited",
        len(thread_ids),
    )

    ctx = Context(
        gmail=gmail,
        self_addr=self_addr,
        processed_id=labels[LABEL_PROCESSED],
        needs_human_id=labels[LABEL_NEEDS_HUMAN],
        corpus=corpus,
    )

    for thread_id in thread_ids:
        try:
            process_thread(ctx, thread_id, id_to_name)
        except Exception as exc:  # noqa: BLE001 - one bad thread must not kill the run
            logger.exception("Unhandled error on thread %s: %s", thread_id, exc)

    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    raise SystemExit(run())
