"""Derive "what I owe on the cards" into the existing dues list.

The expenses dashboard already has a dues section, previously filled in by hand
with one row per card. Those rows are exactly this module's output: your share
of each statement, due on the statement's due date.

Boundaries that keep this from becoming a mixing of the two modules:

  * Generated rows use the deterministic key `cc_<cardId>_<statementMonth>`,
    while hand-added dues use Firebase push ids. A regeneration can therefore
    never touch, duplicate or delete anything you typed yourself.
  * Only `amount`, `dueDate` and `name` are recomputed. A row already marked
    paid stays paid.
  * Nothing is read back out of dues into the credit-card module.
"""

from . import store

DUES = "dues"
SELF_OWNER_DEFAULT = "owner_me"
SOURCE = "creditCard"

# A refund reverses a purchase, so it reduces the payer's share; payments and
# cashback belong to the card, not to a person. Mirrors the dashboard.
OWNED_TYPES = {"PURCHASE", "EMI", "FEE", "INTEREST", "REFUND"}


def self_owner_id(data_root: str) -> str:
    """Which owner counts as "me". Stored so it can change without a redeploy."""
    configured = store.get(data_root, store.META, "selfOwnerId")
    return configured or SELF_OWNER_DEFAULT


def _due_key(card_id: str, statement_month: str) -> str:
    return f"cc_{card_id}_{statement_month}"


def _dues_month(due_date: str, statement_month: str) -> str:
    """The month a statement is budgeted for: the one after the statement.

    Not the due-date month. Some cards fall due inside their own statement
    month (SBI bills on the 7th and falls due on the 27th), but the bill is
    still carried in the following month's dues — which is how these rows were
    kept by hand, and what the dues section is for.
    """
    year, month = (int(part) for part in statement_month.split("-"))
    return f"{year + (month == 12):04d}-{(month % 12) + 1:02d}"


def _other_months(statement_month: str, target: str, due_date: str) -> list:
    """Months a generated row could previously have been written to.

    Used to clear a stale copy when this rule changes, so a statement never
    shows up as a due twice.
    """
    candidates = {statement_month, (due_date or "")[:7]}
    return [m for m in candidates if m and m != target]


def compute_share(data_root: str, statement_id: str, owner_id: str) -> float:
    """Sum of this statement's rows belonging to `owner_id`."""
    total = 0.0
    for txn in (store.get_transactions(data_root, statement_id) or {}).values():
        if txn.get("ownerId") != owner_id:
            continue
        if txn.get("transactionType") not in OWNED_TYPES:
            continue
        amount = float(txn.get("amount") or 0)
        total += -amount if txn.get("transactionType") == "REFUND" else amount
    return round(total, 2)


def refresh_statement(data_root: str, statement_id: str, owner_id: str = None) -> dict:
    """Write (or clear) the generated due for one statement.

    Returns a small report; `written` is False when the share is zero, which is
    the case before any owner has been assigned.
    """
    statement = store.get_statement(data_root, statement_id)
    if not statement:
        return {"statementId": statement_id, "written": False, "reason": "no statement"}

    owner_id = owner_id or self_owner_id(data_root)
    card_id = statement.get("cardId")
    statement_month = statement.get("statementMonth", "")
    month = _dues_month(statement.get("dueDate"), statement_month)
    key = _due_key(card_id, statement_month)
    share = compute_share(data_root, statement_id, owner_id)

    # Clear any copy left in another month by an earlier rule, so one statement
    # can never appear as two dues.
    for stale_month in _other_months(statement_month, month, statement.get("dueDate")):
        if store.get(data_root, DUES, stale_month, key):
            store.delete(data_root, DUES, stale_month, key)

    existing = store.get(data_root, DUES, month, key) or {}

    if share <= 0:
        # Nothing attributed yet (or it was reassigned away) — remove the stale
        # row rather than leaving a wrong figure sitting in the dues list.
        if existing:
            store.delete(data_root, DUES, month, key)
        return {
            "statementId": statement_id,
            "duesMonth": month,
            "amount": 0,
            "written": False,
            "removed": bool(existing),
        }

    card = store.get(data_root, store.CARDS, card_id) or {}
    owners = store.load_owners(data_root)
    owner_name = (owners.get(owner_id) or {}).get("name", "My share")

    payload = {
        "name": f"{card.get('cardName', card_id)} — {owner_name}",
        "amount": share,
        "dueDate": statement.get("dueDate") or "",
        # A row you already ticked off stays ticked off.
        "status": existing.get("status") or "pending",
        # Provenance, so the UI and the duplicate-to-next-month action can tell
        # generated rows from hand-typed ones.
        "source": SOURCE,
        "cardId": card_id,
        "statementId": statement_id,
        "ownerId": owner_id,
        "updatedAt": store.now_ms(),
    }
    store.set_value(data_root, payload, DUES, month, key)
    return {
        "statementId": statement_id,
        "duesMonth": month,
        "amount": share,
        "written": True,
        "key": key,
    }


def refresh_month(data_root: str, statement_month: str, owner_id: str = None) -> list:
    """Refresh generated dues for every statement in a statement month."""
    statements = store.get(data_root, store.STATEMENTS) or {}
    owner_id = owner_id or self_owner_id(data_root)
    return [
        refresh_statement(data_root, statement_id, owner_id)
        for statement_id, statement in statements.items()
        if statement.get("statementMonth") == statement_month
    ]
