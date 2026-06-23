# Gmail Customer Query Agent

Automates triage and reply-drafting for a shared Gmail inbox. Every hour a
GitHub Actions job reads new inbox mail, classifies each thread as a customer
query or noise, and — for genuine queries — drafts a reply grounded **only** in
approved knowledge documents from a Google Drive folder. Drafts are saved into
the Gmail thread; a human always reviews and sends. The agent never sends mail
itself.

See [`gmail-agent-prd.md`](gmail-agent-prd.md) for the full product spec and
[`tasks.md`](tasks.md) for the phased implementation plan.

## How it works

```
Every hour (GitHub Actions cron)
  fetch new inbox threads  ->  triage (cheap LLM)  ->  noise: label Agent-Processed, skip
                                                   ->  customer_query: draft (strong LLM)
                                                          answer in docs   -> save Gmail draft + Agent-Processed
                                                          no answer in docs -> label Needs-Human + Agent-Processed
```

State lives entirely in Gmail labels — there is no external database (PRD §5.6).

## Repository structure

```
/
├── agent/
│   ├── main.py          # Orchestrator: fetch -> triage -> draft   (Phase 5)
│   ├── gmail_client.py  # Gmail API wrapper                        (Phase 2)
│   ├── drive_client.py  # Drive API wrapper                        (Phase 2)
│   ├── knowledge.py     # Text extraction + corpus cache           (Phase 3)
│   ├── triage.py        # Groq triage call                         (Phase 4)
│   ├── drafter.py       # Groq drafting call + NO_ANSWER_FOUND      (Phase 4)
│   └── auth.py          # OAuth token refresh                       (Phase 1 ✓)
├── scripts/
│   └── setup_oauth.py   # One-time local script to get refresh token (Phase 1 ✓)
├── docs/
│   └── auth-setup.md    # One-time auth/secrets setup guide          (Phase 1 ✓)
├── .github/workflows/   # agent.yml hourly cron                      (Phase 6)
├── .knowledge_cache/    # Restored from Actions cache at runtime
├── requirements.txt
└── tasks.md
```

## Setup

1. Complete the one-time auth setup in [`docs/auth-setup.md`](docs/auth-setup.md)
   (create a Google Cloud project, enable Gmail + Drive APIs, create Desktop
   OAuth credentials, run `scripts/setup_oauth.py`).
2. Add these GitHub Actions repository secrets:

   | Secret | Purpose |
   |---|---|
   | `GOOGLE_CLIENT_ID` | OAuth client ID |
   | `GOOGLE_CLIENT_SECRET` | OAuth client secret |
   | `GOOGLE_REFRESH_TOKEN` | Long-lived refresh token from the setup script |
   | `GROQ_API_KEY` | Groq API key for triage/drafting |
   | `DRIVE_FOLDER_ID` | Drive folder holding the knowledge documents |

3. The agent runs automatically every hour via
   [`.github/workflows/agent.yml`](.github/workflows/agent.yml). Trigger it
   manually any time from the Actions tab via **Run workflow**
   (`workflow_dispatch`).

### Continuous operation & notifications

The workflow computes the knowledge-cache key (a hash of the Drive documents'
`modifiedTime`s) before restoring `actions/cache`, so unchanged documents are
never re-downloaded, and any edit invalidates the cache automatically. A
`concurrency` group prevents overlapping runs.

If a run fails — e.g. a revoked refresh token (auth error) or Drive being
unavailable — the job exits non-zero and GitHub emails the repository owner by
default (PRD §9). Transient issues (Groq rate limits, a single Gmail draft-save
error) are handled per-thread and simply retried on the next hourly run.

## Local development

```bash
pip install -r requirements.txt
python -m agent.main          # full run (requires the secrets above in env)
```

OAuth scopes are intentionally minimal: `gmail.modify` (read, draft, label —
**not** send) and `drive.readonly`. The agent is technically incapable of
sending mail.

## Status

Phases 0–6 are complete: scaffolding, authentication, Gmail/Drive clients, the
knowledge corpus + cache, the Groq triage/drafting passes, the orchestrator and
label state machine, and the hourly GitHub Actions workflow. Phase 7
(end-to-end verification against the success metrics) is tracked in
[`tasks.md`](tasks.md).
