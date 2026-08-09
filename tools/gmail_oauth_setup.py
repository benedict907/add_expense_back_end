"""One-time Gmail consent: mint a refresh token for the credit-card sync.

Run locally (needs a browser). The printed refresh token goes into .env for
local runs and into your host's environment variables for deployment.

    python tools/gmail_oauth_setup.py

Prerequisites, in Google Cloud Console:
  1. Create/select a project.
  2. APIs & Services -> Library -> enable "Gmail API".
  3. APIs & Services -> OAuth consent screen -> External, add yourself as a
     Test user. No verification review is needed for a single test user.
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app.
     Download the JSON, or copy the client id/secret into the env vars below.

The scope requested is gmail.readonly. This script never touches your PDF
passwords.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

from creditcards.gmail_client import SCOPES

load_dotenv()

CLIENT_SECRET_FILE = os.environ.get("GMAIL_CLIENT_SECRET_FILE", "gmail_client_secret.json")


def _client_config():
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    if client_id and client_secret:
        return {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
    if os.path.exists(CLIENT_SECRET_FILE):
        with open(CLIENT_SECRET_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    raise SystemExit(
        "Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env, or download the "
        f"OAuth client JSON to {CLIENT_SECRET_FILE!r}."
    )


def main():
    flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
    # access_type=offline + prompt=consent is what actually returns a refresh
    # token; Google omits it on repeat consents otherwise.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", open_browser=True
    )
    if not creds.refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. Revoke this app at "
            "https://myaccount.google.com/permissions and run again."
        )
    print("\nAdd these to .env (local) and to your host's env vars (deployment):\n")
    print(f"GMAIL_CLIENT_ID={creds.client_id}")
    print(f"GMAIL_CLIENT_SECRET={creds.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print("\nDo not commit these.")


if __name__ == "__main__":
    main()
