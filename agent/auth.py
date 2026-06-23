"""OAuth 2.0 authentication for the Gmail Customer Query Agent.

The agent runs unattended in GitHub Actions, so it cannot perform an
interactive consent flow. Instead it exchanges a long-lived refresh token
(obtained once via ``scripts/setup_oauth.py``) for a short-lived access
token at the start of every run.

Required environment variables / secrets:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN

PRD references: §6 (Authentication & Secrets), §9 (revoked-token handling),
§1.5 (minimal scopes).
"""

from __future__ import annotations

import logging
import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

# Google's OAuth 2.0 token endpoint for installed (Desktop) apps.
TOKEN_URI = "https://oauth2.googleapis.com/token"

# --- §1.5 Minimal OAuth scopes -------------------------------------------------
# gmail.modify  -> read inbox, read/get threads, create drafts, add/remove labels.
#                  Deliberately NOT gmail.send or gmail.compose: the agent must
#                  never be able to send mail (PRD hard requirement, §2). modify
#                  grants everything we need without the ability to send.
# drive.readonly -> list and download knowledge documents only.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.readonly",
]


class AuthError(RuntimeError):
    """Raised when credentials are missing or the refresh token is invalid.

    Raising this and letting it propagate causes a non-zero process exit,
    which GitHub Actions reports as a failed job (PRD §9).
    """


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise AuthError(
            f"Missing required environment variable {name!r}. "
            "Set it in GitHub Actions repository secrets (see §6)."
        )
    return value


def get_credentials() -> Credentials:
    """Build and refresh OAuth credentials from environment variables.

    Returns a ``google.oauth2.credentials.Credentials`` object with a valid
    access token, ready to pass to ``googleapiclient.discovery.build``.

    Raises:
        AuthError: if any secret is missing, or the refresh token has been
            revoked/expired. The message tells the maintainer to re-run
            ``scripts/setup_oauth.py`` (PRD §9).
    """
    client_id = _require_env("GOOGLE_CLIENT_ID")
    client_secret = _require_env("GOOGLE_CLIENT_SECRET")
    refresh_token = _require_env("GOOGLE_REFRESH_TOKEN")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )

    try:
        # No access token yet, so force an immediate refresh.
        creds.refresh(Request())
    except RefreshError as exc:
        raise AuthError(
            "Failed to refresh the Google access token. The refresh token is "
            "likely revoked or expired (e.g. account password changed or the "
            "app was de-authorized). Re-run scripts/setup_oauth.py to obtain a "
            f"new GOOGLE_REFRESH_TOKEN and update the secret. Underlying error: {exc}"
        ) from exc

    logger.info("Obtained Google access token (scopes: %s).", ", ".join(SCOPES))
    return creds
