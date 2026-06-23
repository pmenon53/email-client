# Acceptance & Verification (Phase 7)

This maps every Phase 7 task and PRD §12 success metric to how it is verified.
Items split into **Automated** (runnable now, no credentials) and **Live
runbook** (require real Google/Groq credentials and the deployed workflow).

## Automated checks

Run the full suite from the repo root:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests        # end-to-end integration
python -m py_compile agent/*.py scripts/*.py # syntax
```

| Task | What it proves | Where |
|---|---|---|
| **7.2** No autonomous send | Only drafts are produced; the code has no send path and the OAuth scope excludes send/compose. | `tests/test_integration.py::test_single_run_routing_and_no_autonomous_send`; scopes in `agent/auth.py` |
| **7.4** `NO_ANSWER_FOUND` path | Unanswerable query → `Needs-Human`, no draft. | `tests/test_integration.py` (same test) |
| **7.5** No hallucination (grounding) | The drafter only ever receives the approved corpus; the system prompt forbids outside info and mandates `NO_ANSWER_FOUND`. | integration test asserts corpus boundary; `agent/drafter.py::build_system_prompt` |
| **7.6** Re-opened threads | A new external customer message re-triggers triage→draft. | `tests/test_integration.py::test_reopened_thread_is_redrafted` |
| **7.7** Cache hit/miss | Unchanged docs → hit; any edit changes the key → miss + re-extract. | verified in Phase 3 against real PDF/DOCX (`compute_cache_key` stability + invalidation) |
| **7.8** Idempotency | A second run over the same inbox creates no duplicate drafts. | `tests/test_integration.py::test_idempotency_no_duplicate_drafts` |

The state-machine routing for every branch (noise, answer, no-answer, both
LLM-error paths, draft-save error, skip filters, watermark replacement,
malformed body) is additionally covered by the Phase 5 verification.

## Live runbook (require credentials + deployed workflow)

These cannot be exercised without a real inbox, knowledge folder, and API keys.
Perform them once after completing the setup in `docs/auth-setup.md` and adding
the five repository secrets.

### 7.1 End-to-end dry run

1. Put 2–3 known PDFs/DOCX in the Drive knowledge folder.
2. Send a few test emails to the inbox: at least one answerable customer query,
   one whose answer is **not** in the docs, and one obvious newsletter.
3. From the GitHub **Actions** tab, run the **Gmail Agent** workflow via
   **Run workflow** (`workflow_dispatch`).
4. Confirm in Gmail:
   - the answerable thread has a draft reply (threaded, reply-all, no Bcc);
   - the unanswerable thread has the `Needs-Human` label and **no** draft;
   - the newsletter is labelled `Agent-Processed` with no draft;
   - **nothing was sent** — every reply is a draft awaiting a human.
5. Re-run the workflow and confirm no duplicate drafts appear (idempotency).

### 7.3 Triage precision / false-positive rate (§12)

1. Assemble a labelled sample of ~50 real emails (mark each query vs noise).
2. Run them through `agent.triage.classify` (or inspect the run logs, which
   print `triage=...` per thread).
3. Compute precision = correct `customer_query` / all predicted `customer_query`
   (target ≥ 90%) and false-positive rate = noise predicted as query
   (target ≤ 10%). If below target, refine the §5.2 prompt.

### 7.9 Run duration (§12)

After a few scheduled runs, open the Actions run logs and confirm wall-clock
duration is roughly ≤ 2 minutes at steady state (10–20 emails/day). The pip and
knowledge caches keep this low after the first run.

## Success-metric tracking (§12)

| Metric | Target | How measured |
|---|---|---|
| Triage precision | ≥ 90% | 7.3 labelled sample |
| Drafts approved without edits | ≥ 70% | reviewer feedback over 30 days |
| `Needs-Human` genuinely needed | ≥ 95% | spot-check flagged threads |
| False positives (noise→query) | ≤ 10% | 7.3 labelled sample |
| Incorrect info sent by agent | 0 (hard) | guaranteed: agent never sends (7.2) |
| Receipt → draft available | ≤ 60 min | hourly cadence + run logs |
