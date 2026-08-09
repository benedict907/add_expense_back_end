"""Request auth for credit-card endpoints.

Primary path: the browser sends its Firebase ID token, which is verified
server-side and checked against EXPENSE_DATA_ROOT — so only the account that
owns the data can reach it, and no shared secret ships to the front end.

Secondary path: an X-API-Key header for CLI/cron use. Enabled only when
CREDIT_CARD_API_KEY is set.
"""

import os
import secrets

from fastapi import Header, HTTPException
from firebase_admin import auth as firebase_auth

import firebase_store

from . import config


def _matches_data_root(decoded: dict) -> bool:
    root = config.data_root()
    phone = (decoded.get("phone_number") or "").replace(" ", "")
    return root in (phone, decoded.get("uid"))


def require_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    """FastAPI dependency. Raises 401/403 on failure."""
    api_key = os.environ.get("CREDIT_CARD_API_KEY")
    if api_key and x_api_key and secrets.compare_digest(x_api_key, api_key):
        return {"via": "api_key"}

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing credentials: send a Firebase ID token as "
            "'Authorization: Bearer <token>', or an X-API-Key header.",
        )

    token = authorization.split(" ", 1)[1].strip()
    try:
        # Initialise firebase-admin before verifying, same app as the DB writes.
        firebase_store._get_app()
        decoded = firebase_auth.verify_id_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid ID token: {exc}")

    if not _matches_data_root(decoded):
        raise HTTPException(
            status_code=403, detail="This account does not own the configured data root."
        )
    return {"via": "firebase", "uid": decoded.get("uid")}
