"""One-time local OAuth setup for the Gmail Customer Query Agent.

Run this ONCE on a developer machine (with a browser) to complete the Google
consent flow and obtain a long-lived refresh token. The refresh token is then
stored as the GitHub Actions secret GOOGLE_REFRESH_TOKEN so the unattended
agent can mint access tokens on every run (see agent/auth.py and PRD §6).

Prerequisites:
    1. A Google Cloud project with the Gmail API and Drive API enabled.
    2. An OAuth 2.0 Client ID of type "Desktop app".
    3. The downloaded client-secret JSON file.

Usage:
    pip install -r requirements.txt
    python scripts/setup_oauth.py path/to/client_secret.json

The script prints CLIENT_ID, CLIENT_SECRET, and REFRESH_TOKEN. Copy these into
your GitHub Actions repository secrets. Do NOT commit any of these values.
"""

from __future__ import annotations

import os
import sys

# Make the sibling `agent` package importable when run as `python scripts/...`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.auth import SCOPES  # noqa: E402  (single source of truth for scopes)

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        print("Error: provide exactly one argument — the client-secret JSON path.")
        return 2

    client_secret_path = sys.argv[1]
    if not os.path.isfile(client_secret_path):
        print(f"Error: file not found: {client_secret_path}")
        return 2

    # access_type=offline + prompt=consent guarantees Google returns a refresh
    # token (it otherwise omits it on repeat authorizations).
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes=SCOPES)
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    if not creds.refresh_token:
        print(
            "\nNo refresh token was returned. Revoke the app's access at "
            "https://myaccount.google.com/permissions and run this script again."
        )
        return 1

    print("\n" + "=" * 70)
    print("OAuth setup complete. Add these to GitHub Actions repository secrets:")
    print("=" * 70)
    print(f"GOOGLE_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 70)
    print("Keep these values secret. Do not commit them to the repository.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
