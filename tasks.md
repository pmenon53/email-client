# Tasks: Gmail Customer Query Agent

Atomic task breakdown derived from `gmail-agent-prd.md` v1.0. Tasks are grouped into phases ordered by dependency: each phase depends on the phases before it. Within a phase, tasks can largely proceed in parallel unless an explicit dependency is noted.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done. Each task lists the PRD section it traces to.

---

## Phase 0 — Project Scaffolding & Tooling

No dependencies. Establishes the repo so later code has a home.

- [x] **0.1** Create repository directory structure per §8: `agent/`, `.github/workflows/`, `scripts/`, `.knowledge_cache/`. _(§8)_
- [x] **0.2** Add `.gitignore` ignoring `.knowledge_cache/` contents, `__pycache__/`, `.env`, `*.pyc`, local token files (cache dir kept via `.gitkeep`). _(§8)_
- [x] **0.3** Create `requirements.txt` with: `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `groq`, `pdfplumber`, `python-docx`. _(§7, §8)_
- [x] **0.4** Add `README.md` documenting setup steps, run model, and repo structure. _(§6)_
- [x] **0.5** Create module files: `agent/__init__.py`, `main.py`, `gmail_client.py`, `drive_client.py`, `knowledge.py`, `triage.py`, `drafter.py`, `auth.py` (stubs with PRD-traced docstrings; `auth.py` fully implemented). _(§8)_

---

## Phase 1 — Authentication

Depends on Phase 0. Everything that calls Google APIs needs working auth first.

- [x] **1.1** Write `scripts/setup_oauth.py`: one-time local OAuth consent flow (Desktop app type), prints/saves the refresh token. _(§6)_
- [x] **1.2** Document the one-time setup: create GCP project, enable Gmail API + Drive API, create OAuth 2.0 Desktop credentials. _(§6 → `docs/auth-setup.md`)_
- [x] **1.3** Implement `auth.py::get_credentials()`: read `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` from env; exchange refresh token for an access token at run start. _(§6)_
- [x] **1.4** Handle revoked/expired refresh token: surface a clear auth error that fails the run with non-zero exit. _(§6, §9)_
- [x] **1.5** Define the minimal OAuth scopes needed (Gmail `modify` + Drive read-only; `send`/`compose` deliberately excluded) and document them. _(§6)_

---

## Phase 2 — Google API Client Wrappers

Depends on Phase 1 (`get_credentials`). Two independent tracks: Gmail (2.x) and Drive (2.y).

### Gmail (`gmail_client.py`)

- [x] **2.1** Build authenticated Gmail service object from credentials. _(§5.1 → `build_service`)_
- [x] **2.2** `ensure_labels()`: create `Agent-Processed` and `Needs-Human` labels if absent; return their IDs (plus generic `ensure_label` for the §5.5 watermark). _(§5.6)_
- [x] **2.3** `fetch_candidate_thread_ids()`: query `INBOX` for messages in the last 90 minutes via `after:` epoch, paginated. _(§5.1)_
- [x] **2.4** Skip filter `thread_has_label()` — thread carries `Agent-Processed` (presence check; watermark comparison layered in Phase 5). _(§5.1)_
- [x] **2.5** Skip filter `thread_has_unsent_draft()` — thread already has a `DRAFT` message. _(§5.1)_
- [x] **2.6** `get_thread_messages()` + `parse_message()`: ordered messages with sender, recipients, timestamp, subject, threading headers, plain-text body. _(§5.1, §5.5)_
- [x] **2.7** `apply_label()` / `remove_label()` thread-level helpers. _(§5.6)_
- [x] **2.8** `create_reply_all_draft()` (+ pure `compute_reply_all_recipients` / `build_reply_all_message`): `To` = sender + original To, `Cc` preserved, `Bcc` dropped, self excluded; `In-Reply-To`/`References` set; attached to thread via Drafts API. _(§5.4)_

### Drive (`drive_client.py`)

- [x] **2.9** Build authenticated Drive service object from credentials. _(§5.3 → `build_service`)_
- [x] **2.10** `list_knowledge_files(folder_id)`: paginated list with `id`, `name`, `modifiedTime`, `mimeType`; filtered to PDF and DOCX; sorted by id for a stable cache key. _(§5.3)_
- [x] **2.11** `download_file(file_id)`: download raw bytes via `MediaIoBaseDownload`. _(§5.3)_
- [x] **2.12** Drive-unavailable handling: `DriveError` raised on any API failure, propagating to a non-zero exit. _(§9)_

---

## Phase 3 — Knowledge Corpus & Caching

Depends on Phase 2 Drive track (2.9–2.11).

- [x] **3.1** `knowledge.py::extract_text()`: PDF → `pdfplumber`, DOCX → `python-docx`, returning plain text per file (defensive guard on unsupported mime). _(§5.3)_
- [x] **3.2** `build_corpus()`: concatenate per-file text into a single corpus string with a labelled separator per document. _(§5.3)_
- [x] **3.3** `compute_cache_key()`: SHA-256 of sorted `(fileId, modifiedTime)` pairs + `CORPUS_VERSION`. _(§5.3)_
- [x] **3.4** `read_cache()`: check `.knowledge_cache/` for the key; on hit, restore the pre-extracted corpus and skip download. _(§5.3)_
- [x] **3.5** `write_cache()`: on miss, download + extract + concatenate, then save corpus to cache under the new key. _(§5.3)_
- [x] **3.6** Corpus size guard: if estimated tokens exceed ~80,000, log a warning and proceed. _(§5.3, §10)_

---

## Phase 4 — LLM Passes (Groq)

Triage (4.1–4.3) depends on Phase 2 Gmail (message bodies). Drafting (4.4–4.8) depends on Phase 3 (corpus) and Phase 2 Gmail.

### Triage (`triage.py`)

- [x] **4.1** Groq client init using `GROQ_API_KEY` (shared `llm.get_client`); model `llama-3.1-8b-instant`. _(§5.2)_
- [x] **4.2** `classify(subject, body)`: truncate input to ~1,500 tokens; system+user prompt per §5.2 contract; return `customer_query` | `noise`. _(§5.2)_
- [x] **4.3** Ambiguity rule (`_interpret`): any response not clearly `customer_query` is treated as `noise`. _(§5.2)_

### Drafting (`drafter.py`)

- [x] **4.4** Groq client config for model `llama-3.3-70b-versatile`. _(§5.4)_
- [x] **4.5** `build_system_prompt(corpus)` embedding the full corpus + the three §5.4 rules (answer only from docs; `NO_ANSWER_FOUND` if absent; never mention docs/AI/process). _(§5.4)_
- [x] **4.6** `draft_reply(subject, body, corpus)`: return drafted reply text or the sentinel `NO_ANSWER_FOUND`. _(§5.4)_
- [x] **4.7** `is_no_answer()`: reliably detect the `NO_ANSWER_FOUND` sentinel (exact match, tolerant of quotes/whitespace; not triggered when embedded in prose). _(§5.4)_
- [x] **4.8** `llm.complete_with_retry`: exponential backoff (3 attempts) on Groq rate-limit/transient errors; raises `LLMError` so the orchestrator skips the thread without applying `Agent-Processed`. _(§9)_

> Note: shared Groq client + retry logic factored into `agent/llm.py` (used by both passes) — a small addition beyond the §8 file list, keeping rate-limit handling in one place.

---

## Phase 5 — Orchestration & State Machine

Depends on Phases 2, 3, 4. Wires the pieces together in `main.py`.

- [x] **5.1** `main.py::run()` entrypoint: get creds → build services → ensure labels → load corpus (cache-aware) → fetch candidate threads; requires `DRIVE_FOLDER_ID`; Auth/Drive failures propagate to non-zero exit. _(§4)_
- [x] **5.2** Per-thread loop (`process_thread`) applying skip filters (existing draft, already-processed) before any LLM call; one bad thread can't kill the run. _(§5.1)_
- [x] **5.3** Triage branch: `noise` → apply `Agent-Processed` (+ watermark), skip. _(§4, §5.2)_
- [x] **5.4** Draft branch — answer found: create reply-all draft, apply `Agent-Processed`. _(§5.4)_
- [x] **5.5** Draft branch — `NO_ANSWER_FOUND`: no draft; apply `Needs-Human` + `Agent-Processed`; log. _(§5.4)_
- [x] **5.6** Re-open watermark (`_set_watermark`): `Agent-Last-Run-{ISO8601}` label, removing the previous watermark each cycle. _(§5.5, §5.6)_
- [x] **5.7** Re-open detection (`_should_reprocess`): re-process only when the newest message is external and newer than the watermark (our own replies don't trigger it). _(§5.5)_
- [x] **5.8** Malformed email (no body): treat as noise, apply `Agent-Processed`, log. _(§9)_
- [x] **5.9** Gmail draft-save error: log with thread ID, do not apply `Agent-Processed` (retried next run). Triage/draft LLM errors handled the same way. _(§9)_
- [x] **5.10** Structured stdout logging per thread (`_log`): thread ID, truncated subject, triage result, outcome, errors. _(§9)_

---

## Phase 6 — CI/CD Deployment

Depends on Phase 5 (a runnable `main.py`).

- [x] **6.1** Authored `.github/workflows/agent.yml`: hourly cron `0 * * * *` + `workflow_dispatch` (+ `concurrency` guard against overlap). _(§7)_
- [x] **6.2** Workflow steps: checkout, setup Python 3.11 (with pip cache), `pip install -r requirements.txt`. _(§7)_
- [x] **6.3** Cache key computed up front by `scripts/compute_corpus_hash.py` (resolving the §7 ordering gap), wired to `actions/cache@v4` on `.knowledge_cache/` with `restore-keys: knowledge-` (cache@v4 saves automatically post-job). _(§7, §5.3)_
- [x] **6.4** Run step env vars wired from secrets: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GROQ_API_KEY`, `DRIVE_FOLDER_ID`. _(§7)_
- [x] **6.5** All five secrets documented in `README.md` and `docs/auth-setup.md`. _(§6, §7)_
- [x] **6.6** Failure-notification behavior documented (Actions default email to repo owner on non-zero exit). _(§9)_

---

## Phase 7 — Verification & Acceptance

Depends on all prior phases. Validates against PRD success metrics and guarantees.

Automated (runnable now, no credentials) — see `tests/test_integration.py` and `docs/acceptance.md`:

- [x] **7.2** Verified the hard guarantee: no email is ever sent autonomously (drafts only; no send path; scope excludes send). _(§2, §12)_
- [x] **7.4** Verified `NO_ANSWER_FOUND` path applies `Needs-Human` and creates no draft. _(§5.4, §12)_
- [x] **7.5** Verified grounding boundary: the drafter only ever receives the approved corpus; prompt forbids outside info. _(§2, §5.4)_
- [x] **7.6** Verified re-opened-thread re-drafting on a new customer reply. _(§5.5)_
- [x] **7.7** Verified cache hit/miss: unchanged docs → hit; modified doc → key change → miss + re-extract (Phase 3, real PDF/DOCX). _(§5.3)_
- [x] **7.8** Verified idempotency: a second run on the same inbox creates no duplicate drafts. _(§5.1)_

Live runbook (require credentials + deployed workflow) — documented in `docs/acceptance.md`:

- [ ] **7.1** End-to-end dry run via `workflow_dispatch` against a test inbox; confirm drafts appear in correct threads. _(§7, §12 — manual)_
- [ ] **7.3** Verify triage on a labelled sample (≥90% precision, ≤10% false positives). _(§12 — manual)_
- [ ] **7.9** Confirm run duration is under ~2 minutes at steady state. _(§7 — manual)_

---

## Dependency Summary

```
Phase 0  Scaffolding
   │
Phase 1  Auth
   │
Phase 2  Gmail + Drive clients
   ├───────────────┐
Phase 3 Knowledge  │
   │               │
   └────► Phase 4  LLM passes (triage uses Gmail; draft uses corpus)
                   │
            Phase 5  Orchestration & state machine
                   │
            Phase 6  CI/CD
                   │
            Phase 7  Verification
```
