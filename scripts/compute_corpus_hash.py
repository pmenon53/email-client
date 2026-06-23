"""Print the knowledge-corpus cache key for the GitHub Actions cache step.

The workflow needs the cache key *before* it restores the cache, but the key
depends on the current Drive document set (PRD §5.3). This script performs a
single cheap Drive ``files.list`` call and prints the same key that
``knowledge.compute_cache_key`` uses, so the cache layer and the in-process
corpus cache agree.

Usage (in CI):
    echo "hash=$(python scripts/compute_corpus_hash.py)" >> "$GITHUB_OUTPUT"

Requires the same env as the main run: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
GOOGLE_REFRESH_TOKEN, DRIVE_FOLDER_ID.
"""

from __future__ import annotations

import os
import sys

# Make the sibling `agent` package importable when run as `python scripts/...`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load a local .env if present (no-op in CI).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from agent import auth, drive_client, knowledge  # noqa: E402


def main() -> int:
    folder_id = os.environ.get("DRIVE_FOLDER_ID")
    if not folder_id:
        print("Missing DRIVE_FOLDER_ID", file=sys.stderr)
        return 1

    credentials = auth.get_credentials()
    drive = drive_client.build_service(credentials)
    files = drive_client.list_knowledge_files(drive, folder_id)
    print(knowledge.compute_cache_key(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
