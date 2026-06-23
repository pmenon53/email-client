"""Drafter: strong Groq pass that answers from the knowledge corpus only.

PRD references: §5.4 (drafting contract), §9 (retry handling via llm.py).
Model: llama-3.3-70b-versatile.

Returns either a finished reply body or the sentinel NO_ANSWER_FOUND, which the
orchestrator turns into a Needs-Human flag (PRD §5.4).
"""

from __future__ import annotations

import logging
import os

from . import llm

logger = logging.getLogger(__name__)

# Model is configurable via DRAFTING_MODEL; defaults to the PRD §5.4 choice.
DRAFT_MODEL = os.environ.get("DRAFTING_MODEL", "llama-3.3-70b-versatile")
NO_ANSWER_SENTINEL = "NO_ANSWER_FOUND"

# Generous room for a full reply; the prompt keeps it focused.
MAX_OUTPUT_TOKENS = 1024


def build_system_prompt(corpus: str) -> str:
    """System prompt embedding the knowledge corpus and the §5.4 rules."""
    return (
        "You are a customer support assistant that drafts email replies for a "
        "human to review and send.\n\n"
        "RULES:\n"
        "1. Answer using ONLY information present in the DOCUMENTS below. If the "
        "answer to the customer's question is clearly present, write a "
        "professional reply email with a greeting, the answer, and a closing. "
        "Do not invent or assume any information.\n"
        f"2. If the answer is not in the DOCUMENTS, respond with exactly: "
        f"{NO_ANSWER_SENTINEL}\n"
        "3. Do not mention the documents, the AI, or any internal process in the "
        "reply.\n\n"
        "DOCUMENTS:\n"
        f"{corpus}"
    )


def is_no_answer(text: str) -> bool:
    """Reliably detect the NO_ANSWER_FOUND sentinel (§5.4).

    The contract says the model replies with *exactly* the sentinel, but we
    tolerate surrounding quotes/whitespace/trailing punctuation.
    """
    cleaned = text.strip().strip("\"'`. \n\t").upper()
    return cleaned == NO_ANSWER_SENTINEL


def draft_reply(subject: str, body: str, corpus: str) -> str:
    """Draft a reply grounded only in ``corpus``.

    Returns the reply body, or ``NO_ANSWER_SENTINEL`` if the answer is not in
    the documents. Raises ``llm.LLMError`` if Groq cannot be reached after
    retries (handled by the orchestrator).
    """
    messages = [
        {"role": "system", "content": build_system_prompt(corpus)},
        {"role": "user", "content": f"Subject: {subject}\n\n{body}".strip()},
    ]
    raw = llm.complete_with_retry(
        messages, DRAFT_MODEL, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.2
    )

    if is_no_answer(raw):
        logger.info("Drafter -> NO_ANSWER_FOUND")
        return NO_ANSWER_SENTINEL
    return raw.strip()
