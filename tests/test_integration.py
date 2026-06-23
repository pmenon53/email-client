"""End-to-end integration tests for the orchestrator (PRD Phase 7).

These drive the REAL agent.main.run() / process_thread() with Gmail, Drive and
Groq replaced by an in-memory simulator, so the full state machine is exercised
without any network or credentials.

Covered acceptance items:
  7.2  no email is ever sent autonomously (drafts only)
  7.4  NO_ANSWER_FOUND -> Needs-Human, no draft
  7.5  drafting receives only the approved corpus (grounding boundary)
  7.6  re-opened thread is re-drafted on the new customer message
  7.8  idempotency: a second run creates no duplicate drafts

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import unittest
from contextlib import ExitStack
from unittest import mock

from agent import main
from agent.gmail_client import LABEL_NEEDS_HUMAN, LABEL_PROCESSED

SELF = "support@ourco.com"
CORPUS = "=== policy.docx ===\nRefunds are available within 30 days."


def msg(frm, body, ts, subject):
    return {
        "from": frm,
        "body": body,
        "timestamp_ms": ts,
        "subject": subject,
        "message_id_header": f"<{ts}@x>",
        "references": "",
        "to": SELF,
        "cc": "",
    }


class FakeGmail:
    """Minimal in-memory Gmail. Labels are thread-level; drafts are recorded;
    there is intentionally NO send capability."""

    def __init__(self):
        self.threads = {}   # id -> {messages, label_ids:set, has_draft:bool}
        self.labels = {}    # name -> id
        self.drafts = []    # (thread_id, body)
        self._n = 1

    def add_thread(self, tid, messages):
        self.threads[tid] = {"messages": messages, "label_ids": set(), "has_draft": False}

    # --- patched gmail_client surface ---
    def build_service(self, credentials):
        return object()

    def get_profile_address(self, service):
        return SELF

    def ensure_label(self, service, name, existing=None):
        if name not in self.labels:
            self.labels[name] = f"id_{self._n}"
            self._n += 1
        return self.labels[name]

    def ensure_labels(self, service, names=(LABEL_PROCESSED, LABEL_NEEDS_HUMAN)):
        return {n: self.ensure_label(service, n) for n in names}

    def get_label_id_name_map(self, service):
        return {v: k for k, v in self.labels.items()}

    def fetch_candidate_thread_ids(self, service, window_minutes=90):
        return list(self.threads.keys())

    def get_thread(self, service, tid):
        return self.threads[tid]

    def parse_message(self, m):
        return m

    def thread_has_unsent_draft(self, thread):
        return thread["has_draft"]

    def thread_label_names(self, thread, id_to_name):
        return {id_to_name[i] for i in thread["label_ids"] if i in id_to_name}

    def apply_label(self, service, tid, lid):
        self.threads[tid]["label_ids"].add(lid)

    def remove_label(self, service, tid, lid):
        self.threads[tid]["label_ids"].discard(lid)

    def create_reply_all_draft(self, service, tid, orig, self_addr, body):
        self.threads[tid]["has_draft"] = True
        self.drafts.append((tid, body))
        return {"id": f"draft_{len(self.drafts)}"}

    def names_on(self, tid, service=None):
        return self.thread_label_names(self.threads[tid], self.get_label_id_name_map(None))


def classify_side(subject, body):
    if "noise" in subject.lower():
        return "noise"
    return "customer_query"


def draft_side(subject, body, corpus):
    # Grounding boundary (7.5): the drafter only ever sees the approved corpus.
    assert corpus == CORPUS
    if "unknown" in subject.lower():
        return "NO_ANSWER_FOUND"
    return "Hello,\n\nRefunds are available within 30 days.\n\nBest regards"


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.gm = FakeGmail()
        self.stack = ExitStack()
        p = self.stack.enter_context
        # Patch external collaborators on the real modules main calls.
        p(mock.patch.object(main.auth, "get_credentials", lambda: object()))
        p(mock.patch.object(main.drive_client, "build_service", lambda c: object()))
        p(mock.patch.object(main.knowledge, "build_corpus", lambda d, f: CORPUS))
        p(mock.patch.object(main.triage, "classify", classify_side))
        p(mock.patch.object(main.drafter, "draft_reply", draft_side))
        for name in (
            "build_service", "get_profile_address", "ensure_label", "ensure_labels",
            "get_label_id_name_map", "fetch_candidate_thread_ids", "get_thread",
            "parse_message", "thread_has_unsent_draft", "thread_label_names",
            "apply_label", "remove_label", "create_reply_all_draft",
        ):
            p(mock.patch.object(main.gmail_client, name, getattr(self.gm, name)))
        p(mock.patch.dict(os.environ, {"DRIVE_FOLDER_ID": "folder123"}))

    def tearDown(self):
        self.stack.close()

    def test_single_run_routing_and_no_autonomous_send(self):
        self.gm.add_thread("t_noise", [msg("promo@x.com", "buy now", 1000, "Newsletter noise")])
        self.gm.add_thread("t_answer", [msg("alice@x.com", "what is your refund policy?", 1000, "Refund")])
        self.gm.add_thread("t_unknown", [msg("bob@x.com", "do you ship to Mars?", 1000, "Unknown topic")])

        self.assertEqual(main.run(), 0)

        drafted_threads = {tid for tid, _ in self.gm.drafts}
        # 7.2: the only outbound artifacts are drafts; no send path exists.
        self.assertFalse(hasattr(main.gmail_client, "send_message"))
        self.assertEqual(drafted_threads, {"t_answer"})
        # 7.4: unanswerable -> Needs-Human, no draft.
        self.assertIn(LABEL_NEEDS_HUMAN, self.gm.names_on("t_unknown"))
        self.assertNotIn("t_unknown", drafted_threads)
        # noise -> processed, no draft, not flagged for human.
        self.assertNotIn(LABEL_NEEDS_HUMAN, self.gm.names_on("t_noise"))
        # All handled threads carry Agent-Processed.
        for tid in ("t_noise", "t_answer", "t_unknown"):
            self.assertIn(LABEL_PROCESSED, self.gm.names_on(tid))

    def test_idempotency_no_duplicate_drafts(self):
        self.gm.add_thread("t_answer", [msg("alice@x.com", "refund policy?", 1000, "Refund")])
        self.gm.add_thread("t_noise", [msg("promo@x.com", "sale", 1000, "Newsletter noise")])

        self.assertEqual(main.run(), 0)
        first = len(self.gm.drafts)
        self.assertEqual(first, 1)

        # Second run over the same mailbox: nothing new.
        self.assertEqual(main.run(), 0)
        self.assertEqual(len(self.gm.drafts), first, "duplicate draft on re-run")

    def test_reopened_thread_is_redrafted(self):
        # First run: an unanswerable query -> Needs-Human, no draft.
        self.gm.add_thread("t", [msg("bob@x.com", "do you ship to Mars?", 1000, "Unknown topic")])
        self.assertEqual(main.run(), 0)
        self.assertEqual(self.gm.drafts, [])
        self.assertIn(LABEL_NEEDS_HUMAN, self.gm.names_on("t"))

        # Customer replies with a new, answerable, external message (future ts).
        self.gm.threads["t"]["messages"].append(
            msg("bob@x.com", "actually, what is your refund policy?", 9_999_999_999_000, "Refund follow-up")
        )
        self.assertEqual(main.run(), 0)
        # Re-open detected -> a draft is now created.
        self.assertEqual([tid for tid, _ in self.gm.drafts], ["t"])


if __name__ == "__main__":
    unittest.main()
