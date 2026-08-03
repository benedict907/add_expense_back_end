"""Write expenses into the same Realtime Database path the front end uses.

Path layout (see add_expense_front_end/src/context/BudgetContext.jsx):
    <dataRoot>/expenses/<pushId>
where dataRoot is the signed-in user's phone number without spaces.
"""

import json
import os
import time

import firebase_admin
from firebase_admin import credentials, db

_app = None


def _load_credentials():
    inline = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if inline:
        return credentials.Certificate(json.loads(inline))
    path = os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT_FILE", "firebase-service-account.json"
    )
    if not os.path.exists(path):
        raise RuntimeError(
            f"Firebase service account not found at {path!r}. Set "
            "FIREBASE_SERVICE_ACCOUNT_FILE or FIREBASE_SERVICE_ACCOUNT_JSON."
        )
    return credentials.Certificate(path)


def _get_app():
    """Initialise firebase-admin lazily so the API boots without credentials."""
    global _app
    if _app is not None:
        return _app
    database_url = os.environ.get("FIREBASE_DATABASE_URL")
    if not database_url:
        raise RuntimeError("FIREBASE_DATABASE_URL is not set")
    _app = firebase_admin.initialize_app(
        _load_credentials(), {"databaseURL": database_url}
    )
    return _app


def add_expense(record: dict, data_root: str) -> str:
    """Push one expense and return its Firebase key."""
    if not data_root:
        raise RuntimeError("EXPENSE_DATA_ROOT is not set")
    payload = {**record, "createdAt": int(time.time() * 1000)}
    ref = db.reference(f"{data_root}/expenses", app=_get_app())
    return ref.push(payload).key
