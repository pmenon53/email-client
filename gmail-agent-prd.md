# PRD: Gmail Customer Query Agent

**Version:** 1.0  
**Status:** Draft  
**Last Updated:** June 2026

---

## 1. Purpose & Problem Statement

Customer queries arriving in a shared Gmail inbox require manual triage and response drafting. At 10–20 emails per day this is manageable today, but it is slow, inconsistent, and pulls team attention away from higher-value work.

This agent automates the two most tedious steps — deciding whether an email needs a response, and drafting that response from approved knowledge documents — while keeping a human in the loop for every send. The team's existing Gmail workflow does not change: they review and send drafts exactly as they do today.

---

## 2. Goals & Non-Goals

### Goals
- Automatically classify every inbound email as a customer query or noise (newsletters, receipts, internal mail, automated notifications)
- For confirmed customer queries, draft a reply grounded exclusively in approved knowledge documents
- Save drafts into the Gmail thread so the team reviews and sends from the standard Gmail UI
- Apply Gmail labels as the sole state store — no external database
- Never send any email autonomously
- Never fabricate an answer; flag unanswerable emails for human handling instead
- Run cost-efficiently on Groq's free LLM tier via an hourly GitHub Actions job

### Non-Goals
- No new dashboard, web app, or admin UI
- No real-time or webhook-triggered execution (hourly polling is sufficient at this volume)
- No multi-language support in v1 (English only)
- No outbound email of any kind without human approval
- No fine-tuning or embedding model training

---

## 3. Users & Stakeholders

| Role | Interaction with the agent |
|---|---|
| **Team member (reviewer)** | Opens Gmail, sees drafted replies in threads, edits if needed, hits Send |
| **Business owner / operator** | Maintains the knowledge documents in the Drive folder; monitors `Needs-Human` label |
| **Developer / maintainer** | Manages GitHub Actions secrets, monitors run logs, updates knowledge folder |

---

## 4. System Overview

```
Every hour (GitHub Actions cron)
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│  1. FETCH  Gmail full inbox, skip threads with          │
│           label Agent-Processed or existing draft       │
│           (unless newest message is unprocessed)        │
└───────────────────────┬─────────────────────────────────┘
                        │ candidate threads
                        ▼
┌─────────────────────────────────────────────────────────┐
│  2. TRIAGE  Groq fast model reads email body            │
│             → "customer_query" | "noise"                │
└───────────┬───────────────────────┬─────────────────────┘
            │ noise                 │ customer_query
            ▼                       ▼
     Apply Agent-Processed   ┌──────────────────────────┐
     label, skip             │  3. LOAD KNOWLEDGE       │
                             │  Pull docs from Drive    │
                             │  (GitHub Actions Cache   │
                             │  keyed by mod timestamp) │
                             └──────────┬───────────────┘
                                        │
                                        ▼
                             ┌──────────────────────────┐
                             │  4. DRAFT  Groq strong   │
                             │  model answers from docs │
                             │  only; no hallucination  │
                             └──────┬──────────┬────────┘
                                    │          │
                            answer  │          │ no answer
                            found   │          │ in docs
                                    ▼          ▼
                             Save Gmail    Apply Needs-Human
                             draft (reply  label
                             all) + apply  Apply Agent-Processed
                             Agent-        label
                             Processed
                             label
```

---

## 5. Detailed Requirements

### 5.1 Email Fetching & Scope

- **Inbox scope:** Full Gmail inbox (`INBOX` label). No label pre-filter.
- **Skip conditions (checked before any LLM call):**
  - Thread already carries the `Agent-Processed` label, **unless** the newest message in the thread is newer than the most recent `Agent-Processed` timestamp (to handle re-opened threads — see §5.5).
  - Thread already has an unsent draft attached to it.
- **Fetch window:** Emails received in the last 90 minutes on each run (overlapping slightly with the 60-minute cadence to avoid gaps from job latency).
- **Batch size:** No hard cap needed at 10–20 emails/day; process all qualifying threads per run.

### 5.2 Triage (Cheap LLM Pass)

**Model:** `llama-3.1-8b-instant` on Groq (fast, free tier, sufficient for classification)

**Input to model:** Email subject + body (truncated to 1,500 tokens if necessary)

**Prompt contract:**
- System: You are an email classifier. Respond with exactly one word: `customer_query` or `noise`.
- A `customer_query` is any email where a real person is asking for help, information, pricing, support, or any business-related question directed at the company.
- `noise` covers: newsletters, marketing emails, automated receipts, order confirmations, calendar invites, internal team emails, delivery notifications, OOO replies, social media alerts.

**Output:** Single token classification. Any response that is not clearly `customer_query` is treated as `noise` — the model is given no opportunity to be ambiguous.

**Cost gate:** Only threads that return `customer_query` proceed to the drafting step.

### 5.3 Knowledge Document Loading & Caching

**Source:** A single designated Google Drive folder. Supported file types: PDF, `.docx`.

**Cache strategy:**
- On each run, call the Drive API to list files in the folder and retrieve each file's `modifiedTime`.
- Compute a cache key: a hash of all `(fileId, modifiedTime)` pairs sorted by fileId.
- Check GitHub Actions Cache for this key.
  - **Cache hit:** Restore the pre-extracted text corpus from cache. Skip re-downloading.
  - **Cache miss:** Download all files, extract plain text (PDF → `pdfplumber`; DOCX → `python-docx`), concatenate into a single corpus string, save to cache under the new key.
- Cache TTL: 7 days (GitHub Actions default max). Because the key changes whenever any document is modified, stale content is never served.
- **Hard limit:** If the total corpus exceeds ~80,000 tokens (~300 KB of text), log a warning. In v1 the full corpus is passed in context; chunking/retrieval is a v2 concern given the stated document count of under 10.

### 5.4 Drafting (Strong LLM Pass)

**Model:** `llama-3.3-70b-versatile` on Groq (strongest available on free tier, appropriate for response quality)

**Input to model:**
- System prompt containing the full knowledge corpus
- The customer's email subject + body
- Explicit instruction: answer using only information present in the provided documents

**Prompt contract — key rules embedded in system prompt:**
1. If the answer to the customer's question is clearly present in the documents, write a professional reply-all email draft. Include a greeting, the answer, and a closing. Do not invent information.
2. If the answer is not in the documents, respond with exactly: `NO_ANSWER_FOUND`
3. Do not mention the knowledge documents, the AI, or any internal process in the reply.

**On `NO_ANSWER_FOUND`:**
- Do not create a draft
- Apply `Needs-Human` label to the thread
- Apply `Agent-Processed` label
- Log the thread ID and subject for observability

**On a successful draft:**
- Create a Gmail draft using the Drafts API, attached to the correct thread
- Draft is a reply-all: `To` = all original recipients, `Cc` preserved, `Bcc` dropped
- `In-Reply-To` and `References` headers set correctly so it threads in Gmail
- Apply `Agent-Processed` label

### 5.5 Re-opened Thread Handling

When a customer replies to a thread that the team has already responded to:

- The new customer message will not carry `Agent-Processed` (that label was applied to the thread when the previous draft was created, but the label state is on the thread, not the message)
- **Detection logic:** On each run, for threads labelled `Agent-Processed`, check if the newest message is from an external sender and has a received timestamp newer than the last time the agent processed that thread.
- If so, treat as a fresh query: run triage → draft cycle again on the newest message only.
- To track "last processed time" without a database: store it as a special Gmail label named `Agent-Last-Run-{ISO8601-date}` applied to the thread, replaced on each processing cycle. This keeps everything inside Gmail labels as agreed.

> **Alternative considered and rejected:** Using a separate state file in the repo. Rejected because it introduces merge conflicts and complicates the stateless GitHub Actions model.

### 5.6 Gmail Labels (State Machine)

| Label | Meaning | Applied by |
|---|---|---|
| `Agent-Processed` | Agent has handled this thread in this cycle | Agent, on every processed thread |
| `Needs-Human` | Agent could not find an answer; human must respond | Agent, on unanswerable queries |
| `Agent-Last-Run-{date}` | Watermark of last agent processing time for re-open detection | Agent, replaces previous watermark |

Labels are created automatically by the agent on first run if they do not exist.

---

## 6. Authentication & Secrets

**Auth model:** OAuth 2.0 with a stored refresh token (required for consumer `@gmail.com` accounts; service accounts cannot access consumer Gmail).

**Setup (one-time, done by developer):**
1. Create a Google Cloud project, enable Gmail API and Drive API.
2. Create OAuth 2.0 credentials (Desktop app type).
3. Run a local auth script once to complete the consent flow and obtain a refresh token.
4. Store in GitHub Actions repository secrets:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GOOGLE_REFRESH_TOKEN`
   - `GROQ_API_KEY`

**Token refresh:** The agent exchanges the refresh token for an access token at the start of each run. No manual re-auth should be needed unless the refresh token is revoked (e.g. if the Google account password changes or the app is de-authorized).

---

## 7. GitHub Actions Workflow

```yaml
name: Gmail Agent

on:
  schedule:
    - cron: '0 * * * *'   # Every hour
  workflow_dispatch:        # Manual trigger for testing

jobs:
  run-agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Restore knowledge cache
        uses: actions/cache@v4
        with:
          path: .knowledge_cache/
          key: knowledge-${{ env.DRIVE_CORPUS_HASH }}   # computed in next step
          restore-keys: knowledge-

      - name: Run agent
        env:
          GOOGLE_CLIENT_ID: ${{ secrets.GOOGLE_CLIENT_ID }}
          GOOGLE_CLIENT_SECRET: ${{ secrets.GOOGLE_CLIENT_SECRET }}
          GOOGLE_REFRESH_TOKEN: ${{ secrets.GOOGLE_REFRESH_TOKEN }}
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
          DRIVE_FOLDER_ID: ${{ secrets.DRIVE_FOLDER_ID }}
        run: python agent/main.py

      - name: Save knowledge cache
        uses: actions/cache/save@v4
        with:
          path: .knowledge_cache/
          key: knowledge-${{ env.DRIVE_CORPUS_HASH }}
```

**Run duration estimate:** Under 2 minutes at steady state (10–20 emails/day, most triaged as noise quickly).

---

## 8. Repository Structure

```
/
├── agent/
│   ├── main.py              # Orchestrator: fetch → triage → draft
│   ├── gmail_client.py      # Gmail API wrapper (fetch, draft, label)
│   ├── drive_client.py      # Drive API wrapper (list, download, cache)
│   ├── knowledge.py         # Text extraction (PDF, DOCX) + corpus builder
│   ├── triage.py            # Groq triage call
│   ├── drafter.py           # Groq drafting call + NO_ANSWER_FOUND handling
│   └── auth.py              # OAuth token refresh
├── .knowledge_cache/        # Gitignored; used by Actions cache
├── requirements.txt
├── .github/
│   └── workflows/
│       └── agent.yml
└── scripts/
    └── setup_oauth.py       # One-time local script to obtain refresh token
```

---

## 9. Observability & Error Handling

| Scenario | Behaviour |
|---|---|
| Groq API rate limit hit | Exponential backoff, 3 retries; if still failing, skip thread and log; thread will be retried next run since `Agent-Processed` label not yet applied |
| Gmail API error on draft save | Log error with thread ID; do not apply `Agent-Processed`; thread retried next run |
| Drive API unavailable | Abort run with non-zero exit (Actions marks job as failed, sends email notification to repo owner) |
| Knowledge corpus too large (>80k tokens) | Log warning, proceed anyway in v1; treat as known limitation |
| Refresh token revoked | Job fails with auth error; developer must re-run `setup_oauth.py` |
| Malformed email (no body) | Treat as noise, apply `Agent-Processed`, log |

**Logging:** All runs print structured logs to stdout (captured by GitHub Actions). Log lines include: thread ID, subject (truncated), triage result, draft outcome, any errors.

---

## 10. Constraints & Known Limitations (v1)

| Constraint | Impact | v2 Mitigation |
|---|---|---|
| Full corpus passed in context | Breaks if docs exceed ~80k tokens combined | Add chunking + semantic retrieval |
| No real-time triggering | Up to 59-minute delay on response drafting | Acceptable at stated volume; webhook trigger is a v2 option |
| GitHub Actions Cache is best-effort | Cache can be evicted; causes a full re-download on next run (not a correctness issue) | Acceptable |
| Groq free tier rate limits | Could cause retries / skipped threads under burst conditions | Upgrade to paid tier if needed |
| Re-open detection via date labels | Slightly verbose label management; thread can accumulate old date labels | Clean up stale date labels on each processing cycle |
| OAuth refresh token | Manual re-auth if revoked | Implement token health check + alert |

---

## 11. Out of Scope for v1 (Potential v2 Features)

- Webhook / push notification trigger (near-real-time processing)
- Semantic chunking and vector retrieval for larger document sets
- Draft confidence scoring (e.g. "how well does this answer match the docs?")
- Support for additional email languages
- Analytics dashboard (query volume, answer rate, Needs-Human rate)
- Slack/Teams notification when a `Needs-Human` email is flagged
- Automatic document refresh trigger when Drive files are updated

---

## 12. Success Metrics

| Metric | Target (30 days post-launch) |
|---|---|
| % of customer queries correctly identified (triage precision) | ≥ 90% |
| % of drafted replies approved without edits | ≥ 70% |
| % of `Needs-Human` emails that genuinely required human knowledge | ≥ 95% (i.e. the agent is not over-flagging) |
| False positive rate (noise classified as query) | ≤ 10% |
| Agent-caused send of incorrect information | 0 (hard requirement; human always sends) |
| Average time from email receipt to draft available | ≤ 60 minutes |

---

## 13. Open Questions (Resolved)

| Question | Decision |
|---|---|
| Inbox scope | Full inbox |
| Knowledge cache location | GitHub Actions Cache, keyed by Drive file mod timestamps |
| Drive re-index frequency | Only when Drive files change (cache miss) |
| Reply-to behaviour | Reply-all |
| Re-opened threads | Re-draft on new customer message in thread |
| LLM models | Triage: `llama-3.1-8b-instant`; Drafting: `llama-3.3-70b-versatile` (Groq free tier) |
| State store | Gmail labels only, no external DB |
| Auth type | OAuth 2.0 with stored refresh token |
| Autonomous sending | Never |

---

*End of PRD v1.0*
