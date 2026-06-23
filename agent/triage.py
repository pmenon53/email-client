"""Triage: cheap Groq classification pass (customer_query vs noise).

PRD reference: §5.2. Model: llama-3.1-8b-instant. Only threads classified as
``customer_query`` proceed to the (more expensive) drafting pass.
"""

from __future__ import annotations

import logging
import os

from . import llm

logger = logging.getLogger(__name__)

# Model is configurable via TRIAGE_MODEL; defaults to the PRD §5.2 choice.
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "llama-3.1-8b-instant")

CUSTOMER_QUERY = "customer_query"
NOISE = "noise"

# Input truncation: cap at ~1,500 tokens (~4 chars/token) per §5.2.
MAX_INPUT_TOKENS = 1500
CHARS_PER_TOKEN = 4
MAX_INPUT_CHARS = MAX_INPUT_TOKENS * CHARS_PER_TOKEN

SYSTEM_PROMPT = (
    "You are an email classifier. Respond with exactly one word: "
    "customer_query or noise.\n"
    "A customer_query is any email where a real person is asking for help, "
    "information, pricing, support, or any business-related question directed "
    "at the company.\n"
    "noise covers: newsletters, marketing emails, automated receipts, order "
    "confirmations, calendar invites, internal team emails, delivery "
    "notifications, out-of-office replies, and social media alerts."
)


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_INPUT_CHARS else text[:MAX_INPUT_CHARS]


def _interpret(raw: str) -> str:
    """Map a raw model response to a label.

    Ambiguity rule (§5.2): anything that is not clearly ``customer_query`` is
    treated as ``noise`` — the model gets no benefit of the doubt.
    """
    normalized = raw.strip().strip("\"'`.").lower()
    return CUSTOMER_QUERY if normalized == CUSTOMER_QUERY else NOISE


def classify(subject: str, body: str) -> str:
    """Classify an email as ``customer_query`` or ``noise``.

    Returns ``noise`` for empty input and for any non-affirmative model output.
    """
    content = _truncate(f"Subject: {subject}\n\n{body}".strip())
    if not content:
        return NOISE

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]
    raw = llm.complete_with_retry(
        messages, TRIAGE_MODEL, max_tokens=4, temperature=0.0
    )
    label = _interpret(raw)
    logger.info("Triage -> %s (raw=%r)", label, raw.strip()[:40])
    return label
