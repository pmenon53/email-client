# Authentication Setup (one-time)

The agent runs unattended in GitHub Actions and authenticates to Gmail and
Drive using OAuth 2.0 with a stored **refresh token**. A service account will
not work — consumer `@gmail.com` accounts can only be accessed via the OAuth
user-consent flow (PRD §6).

You only do this once. After the refresh token is stored as a secret, the agent
mints a fresh access token automatically on every run.

## 1. Create a Google Cloud project and enable APIs

1. Go to <https://console.cloud.google.com/> and create (or select) a project.
2. Enable the **Gmail API**: APIs & Services → Library → "Gmail API" → Enable.
3. Enable the **Google Drive API**: same Library page → "Google Drive API" → Enable.

## 2. Configure the OAuth consent screen

1. APIs & Services → OAuth consent screen.
2. User type: **External** (required for a personal Gmail account), then fill in
   the app name and support email.
3. Add the account that owns the shared inbox as a **Test user** (this avoids
   needing Google verification while the app stays in "Testing" mode).
4. Add the scopes the agent uses (see section 4) — or simply let the consent
   flow request them at runtime.

## 3. Create OAuth credentials (Desktop app)

1. APIs & Services → Credentials → Create credentials → **OAuth client ID**.
2. Application type: **Desktop app**.
3. Download the resulting **client-secret JSON** file. Keep it private; never
   commit it.

## 4. Scopes used (least privilege)

The agent requests exactly two scopes (defined once in `agent/auth.py`):

| Scope | Why |
|---|---|
| `https://www.googleapis.com/auth/gmail.modify` | Read the inbox and threads, create **drafts**, and add/remove labels. It deliberately does **not** grant send permission — the agent can never send mail (PRD §2). |
| `https://www.googleapis.com/auth/drive.readonly` | List and download the knowledge documents only. |

## 5. Obtain the refresh token

On a machine with a browser:

```bash
pip install -r requirements.txt
python scripts/setup_oauth.py path/to/client_secret.json
```

A browser window opens for consent (sign in as the inbox-owning account and
approve). The script then prints:

```
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...
```

If no refresh token is printed, revoke the app at
<https://myaccount.google.com/permissions> and run the script again (Google only
returns a refresh token on first authorization unless forced).

## 6. Store the secrets in GitHub

Repository → Settings → Secrets and variables → Actions → New repository secret.
Add each of:

| Secret | Source |
|---|---|
| `GOOGLE_CLIENT_ID` | printed by the setup script |
| `GOOGLE_CLIENT_SECRET` | printed by the setup script |
| `GOOGLE_REFRESH_TOKEN` | printed by the setup script |
| `GROQ_API_KEY` | from your Groq account (used later, §5.2/§5.4) |
| `DRIVE_FOLDER_ID` | the ID of the Drive folder holding the knowledge docs |

## 7. Token lifetime and re-auth

The refresh token is long-lived and does not need rotation under normal
operation. It is invalidated only if:

- the Google account password changes,
- the app's access is revoked at <https://myaccount.google.com/permissions>, or
- the OAuth app is left in "Testing" mode for an extended period (Google may
  expire test-mode refresh tokens after ~7 days — publish the app to avoid this).

When the token is invalid, the agent fails the run with a clear `AuthError`
(GitHub Actions emails the repo owner). To recover, re-run
`python scripts/setup_oauth.py ...` and update the `GOOGLE_REFRESH_TOKEN` secret.
