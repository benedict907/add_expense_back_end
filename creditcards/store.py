"""Realtime Database access for the credit-card module.

Reuses the firebase-admin app initialised by the expense module so there is one
credential path, but writes only under credit-card namespaces. Nothing here
touches `<dataRoot>/expenses`.

Writes use deterministic keys (never push()), which is what makes a repeated
sync idempotent.
"""

import time

from firebase_admin import db

import firebase_store

CARDS = "creditCards"
OWNERS = "creditCardOwners"
STATEMENTS = "creditCardStatements"
TRANSACTIONS = "creditCardTransactions"
RULES = "creditCardOwnerRules"
META = "creditCardMeta"

# Seeded on first run so the dashboard is usable immediately. Editable in the
# UI afterwards; the list is not hardcoded anywhere else.
DEFAULT_OWNERS = {
    "owner_me": {"name": "Me", "color": "var(--color-lime)", "order": 1},
    "owner_partner": {"name": "Partner", "color": "var(--color-iris)", "order": 2},
    "owner_other": {"name": "Other", "color": "var(--color-violet)", "order": 3},
}


def now_ms() -> int:
    return int(time.time() * 1000)


def _ref(data_root: str, *segments):
    path = "/".join([data_root, *[str(s) for s in segments if s not in (None, "")]])
    return db.reference(path, app=firebase_store._get_app())


def get(data_root: str, *segments):
    return _ref(data_root, *segments).get()


def set_value(data_root: str, value, *segments):
    _ref(data_root, *segments).set(value)


def delete(data_root: str, *segments):
    """Remove a node. `set(None)` is rejected by firebase-admin, unlike the JS SDK."""
    _ref(data_root, *segments).delete()


def update(data_root: str, values: dict, *segments):
    _ref(data_root, *segments).update(values)


# ---------- owners ----------


def load_owners(data_root: str) -> dict:
    owners = get(data_root, OWNERS) or {}
    if not owners:
        stamped = {
            key: {**value, "createdAt": now_ms()} for key, value in DEFAULT_OWNERS.items()
        }
        set_value(data_root, stamped, OWNERS)
        return stamped
    return owners


# ---------- cards ----------


def sync_cards(data_root: str, cards) -> dict:
    """Mirror the server-side card list into RTDB for the UI.

    Only public fields are written — Gmail queries and password env names stay
    in cards.json on the server.
    """
    payload = {}
    for card in cards:
        entry = card.public_dict()
        if not entry.get("createdAt"):
            existing = get(data_root, CARDS, card.id) or {}
            entry["createdAt"] = existing.get("createdAt") or now_ms()
        payload[card.id] = entry
    set_value(data_root, payload, CARDS)
    return payload


# ---------- rules and history ----------


def load_rules(data_root: str) -> dict:
    return get(data_root, RULES) or {}


def save_rule(data_root: str, card_id: str, merchant: str, payload: dict):
    if not merchant:
        return
    set_value(data_root, payload, RULES, card_id, merchant)


def load_owner_history(data_root: str, exclude_statement_id: str = None) -> list:
    """Flatten every stored transaction that already has an owner.

    Volume is small (a few hundred rows a year), so one read beats maintaining
    a separate index that could drift out of sync with manual edits.
    """
    all_txns = get(data_root, TRANSACTIONS) or {}
    history = []
    for statement_id, txns in all_txns.items():
        if statement_id == exclude_statement_id or not isinstance(txns, dict):
            continue
        for txn in txns.values():
            if not isinstance(txn, dict) or not txn.get("ownerId"):
                continue
            history.append(
                {
                    "cardId": txn.get("cardId"),
                    "merchant": txn.get("merchant"),
                    "ownerId": txn.get("ownerId"),
                    "isEMI": bool(txn.get("isEMI")),
                    "statementMonth": txn.get("statementMonth", ""),
                }
            )
    return history


# ---------- statements and transactions ----------


def get_statement(data_root: str, statement_id: str):
    return get(data_root, STATEMENTS, statement_id)


def get_transactions(data_root: str, statement_id: str) -> dict:
    return get(data_root, TRANSACTIONS, statement_id) or {}


def save_statement(data_root: str, statement_id: str, payload: dict):
    set_value(data_root, payload, STATEMENTS, statement_id)


def save_transactions(data_root: str, statement_id: str, txns: dict):
    """Write the statement's transactions, preserving manual owner edits.

    A re-sync must not undo an owner the user set by hand, so existing
    ownerSource == MANUAL rows keep their owner.
    """
    set_value(data_root, txns, TRANSACTIONS, statement_id)


def update_transaction(data_root: str, statement_id: str, txn_id: str, values: dict):
    update(data_root, values, TRANSACTIONS, statement_id, txn_id)


def update_transactions_bulk(data_root: str, statement_id: str, per_txn: dict):
    """Apply per-transaction field updates in a single multi-path write.

    `per_txn` is {txnId: {field: value}}. Flattened to "<txnId>/<field>" paths so
    one hundred rows cost one round trip instead of one hundred.
    """
    flat = {}
    for txn_id, values in per_txn.items():
        for field, value in values.items():
            flat[f"{txn_id}/{field}"] = value
    if flat:
        update(data_root, flat, TRANSACTIONS, statement_id)


def save_rules_bulk(data_root: str, per_card: dict):
    """Write many owner rules at once. `per_card` is {cardId: {merchant: payload}}."""
    for card_id, merchants in per_card.items():
        clean = {m: p for m, p in merchants.items() if m}
        if clean:
            update(data_root, clean, RULES, card_id)


def get_transaction(data_root: str, statement_id: str, txn_id: str):
    return get(data_root, TRANSACTIONS, statement_id, txn_id)


# ---------- meta ----------


def save_sync_meta(data_root: str, payload: dict):
    set_value(data_root, payload, META, "lastSync")


def load_sync_meta(data_root: str):
    return get(data_root, META, "lastSync")
