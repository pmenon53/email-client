"""Gmail API wrapper: fetch threads, manage labels, create reply-all drafts.

PRD references: §5.1 (fetch & skip filters), §5.4 (reply-all draft), §5.6
(labels). State lives entirely in Gmail labels — there is no external database.

Design notes:
- Labels are applied at the THREAD level (``threads().modify``) because the PRD
  treats the thread as the unit of state.
- The agent never sends mail: it only creates drafts. The OAuth scope
  (``gmail.modify``) does not grant send permission.
- Pure helpers (recipient computation, MIME parsing, draft building) are kept
  free of network calls so they can be unit-tested without Gmail.
"""

from __future__ import annotations

import base64
import logging
import time
from email.message import EmailMessage
from email.utils import getaddresses

from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Labels the agent owns (PRD §5.6). The watermark label (§5.5) is created
# on demand in Phase 5 via ensure_label().
LABEL_PROCESSED = "Agent-Processed"
LABEL_NEEDS_HUMAN = "Needs-Human"
REQUIRED_LABELS = (LABEL_PROCESSED, LABEL_NEEDS_HUMAN)

# Gmail's built-in label marking a message as an unsent draft.
DRAFT_LABEL = "DRAFT"

# Prefix for the per-thread re-open watermark label (PRD §5.5/§5.6). The full
# name encodes the processing time, e.g. "Agent-Last-Run-2026-06-22T10:00:00Z".
WATERMARK_PREFIX = "Agent-Last-Run-"


# --- 2.1 Service --------------------------------------------------------------
def build_service(credentials):
    """Build an authenticated Gmail API service from OAuth credentials."""
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def get_profile_address(service) -> str:
    """Return the authenticated mailbox's own email address.

    Used to exclude ourselves from reply-all recipients.
    """
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


# --- 2.2 Labels ---------------------------------------------------------------
def _list_labels(service) -> dict[str, str]:
    resp = service.users().labels().list(userId="me").execute()
    return {lbl["name"]: lbl["id"] for lbl in resp.get("labels", [])}


def ensure_label(service, name: str, existing: dict[str, str] | None = None) -> str:
    """Return the id of label ``name``, creating it if it does not exist."""
    existing = _list_labels(service) if existing is None else existing
    if name in existing:
        return existing[name]
    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    logger.info("Created Gmail label %r (id=%s).", name, created["id"])
    return created["id"]


def ensure_labels(service, names=REQUIRED_LABELS) -> dict[str, str]:
    """Ensure the required labels exist; return a {name: id} mapping."""
    existing = _list_labels(service)
    return {name: ensure_label(service, name, existing) for name in names}


def get_label_id_name_map(service) -> dict[str, str]:
    """Return a {labelId: name} map for all labels in the mailbox.

    Needed to resolve a thread's message ``labelIds`` back to names — in
    particular the per-cycle watermark labels (§5.5), whose names vary and so
    cannot be pre-enumerated.
    """
    resp = service.users().labels().list(userId="me").execute()
    return {lbl["id"]: lbl["name"] for lbl in resp.get("labels", [])}


def thread_label_names(thread: dict, id_to_name: dict[str, str]) -> set[str]:
    """Return the set of label *names* applied to any message in the thread."""
    names: set[str] = set()
    for msg in thread.get("messages", []):
        for label_id in msg.get("labelIds", []):
            name = id_to_name.get(label_id)
            if name:
                names.add(name)
    return names


# --- 2.3 Fetch candidate threads ----------------------------------------------
def fetch_candidate_thread_ids(service, window_minutes: int = 90) -> list[str]:
    """Return thread ids for INBOX messages received in the last N minutes.

    The 90-minute window overlaps the hourly cadence to avoid gaps from job
    latency (PRD §5.1). Gmail's ``after:`` query operator accepts a Unix epoch
    (seconds).
    """
    after_epoch = int(time.time()) - window_minutes * 60
    query = f"in:inbox after:{after_epoch}"

    thread_ids: list[str] = []
    request = service.users().threads().list(userId="me", q=query)
    while request is not None:
        resp = request.execute()
        thread_ids.extend(t["id"] for t in resp.get("threads", []))
        request = service.users().threads().list_next(request, resp)
    return thread_ids


def get_thread(service, thread_id: str) -> dict:
    """Fetch a full thread resource (all messages, headers, bodies)."""
    return (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )


# --- 2.4 / 2.5 Skip filters (pure, operate on a fetched thread resource) ------
def thread_has_label(thread: dict, label_id: str) -> bool:
    """True if any message in the thread carries ``label_id``.

    Gmail labels are per-message; a thread "has" a label if any of its messages
    do. Basic presence check for the §5.1 skip filter; the re-open watermark
    comparison (§5.5) is layered on in Phase 5.
    """
    return any(
        label_id in msg.get("labelIds", []) for msg in thread.get("messages", [])
    )


def thread_has_unsent_draft(thread: dict) -> bool:
    """True if the thread already has an unsent draft attached (§5.1)."""
    return any(
        DRAFT_LABEL in msg.get("labelIds", []) for msg in thread.get("messages", [])
    )


# --- 2.6 Parse messages -------------------------------------------------------
def _decode_b64url(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_plain_text(payload: dict) -> str:
    """Depth-first search for the first text/plain part; return decoded text."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data")
        return _decode_b64url(data) if data else ""
    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text
    return ""


def parse_message(message: dict) -> dict:
    """Flatten a Gmail message resource into a convenient dict.

    Returns sender, recipients, subject, timestamp, threading headers, and the
    plain-text body (PRD §5.1, §5.5).
    """
    payload = message.get("payload", {})
    headers = {
        h["name"].lower(): h["value"] for h in payload.get("headers", [])
    }
    return {
        "id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "label_ids": message.get("labelIds", []),
        "timestamp_ms": int(message.get("internalDate", 0)),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "message_id_header": headers.get("message-id", ""),
        "references": headers.get("references", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "body": _extract_plain_text(payload),
    }


def get_thread_messages(service, thread_id: str) -> list[dict]:
    """Return the thread's messages parsed and ordered oldest -> newest."""
    thread = get_thread(service, thread_id)
    messages = [parse_message(m) for m in thread.get("messages", [])]
    messages.sort(key=lambda m: m["timestamp_ms"])
    return messages


# --- 2.7 Label helpers (thread-level) -----------------------------------------
def apply_label(service, thread_id: str, label_id: str) -> None:
    service.users().threads().modify(
        userId="me", id=thread_id, body={"addLabelIds": [label_id]}
    ).execute()


def remove_label(service, thread_id: str, label_id: str) -> None:
    service.users().threads().modify(
        userId="me", id=thread_id, body={"removeLabelIds": [label_id]}
    ).execute()


# --- 2.8 Reply-all draft ------------------------------------------------------
def compute_reply_all_recipients(
    orig: dict, self_addr: str
) -> tuple[list[str], list[str]]:
    """Compute reply-all To/Cc lists from the original message.

    To  = original sender + original To recipients.
    Cc  = original Cc recipients.
    Our own address is removed; duplicates are de-duplicated case-insensitively;
    Bcc is never included (PRD §5.4).
    """
    self_lower = self_addr.strip().lower()
    seen: set[str] = set()

    def collect(*raw_headers: str) -> list[str]:
        out: list[str] = []
        for _name, addr in getaddresses(list(raw_headers)):
            key = addr.strip().lower()
            if not key or key == self_lower or key in seen:
                continue
            seen.add(key)
            out.append(addr)
        return out

    to_addrs = collect(orig.get("from", ""), orig.get("to", ""))
    cc_addrs = collect(orig.get("cc", ""))
    return to_addrs, cc_addrs


def _reply_subject(subject: str) -> str:
    subject = subject or ""
    return subject if subject.strip().lower().startswith("re:") else f"Re: {subject}"


def build_reply_all_message(orig: dict, self_addr: str, body: str) -> EmailMessage:
    """Build the reply-all MIME message (pure; no network)."""
    to_addrs, cc_addrs = compute_reply_all_recipients(orig, self_addr)

    msg = EmailMessage()
    if to_addrs:
        msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = _reply_subject(orig.get("subject", ""))

    # Threading headers so Gmail nests the reply in the original thread.
    orig_msg_id = orig.get("message_id_header", "")
    if orig_msg_id:
        msg["In-Reply-To"] = orig_msg_id
        references = f"{orig.get('references', '')} {orig_msg_id}".strip()
        msg["References"] = references

    msg.set_content(body)
    return msg


def create_reply_all_draft(
    service, thread_id: str, orig: dict, self_addr: str, body: str
) -> dict:
    """Create a Gmail draft replying-all to ``orig`` within ``thread_id``.

    Returns the created draft resource. The draft is NOT sent — a human reviews
    and sends it from the Gmail UI (PRD §2, §5.4).
    """
    msg = build_reply_all_message(orig, self_addr, body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return (
        service.users()
        .drafts()
        .create(
            userId="me",
            body={"message": {"raw": raw, "threadId": thread_id}},
        )
        .execute()
    )
