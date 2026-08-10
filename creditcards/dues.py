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

# Rows that belong to a person. Credits are included because cashback and
# surcharge waivers are earned by a specific purchase — assign the credit to
# whoever made that purchase and it comes off their bill. Payments are excluded:
# they settle the previous cycle rather than belonging to anyone's spending.
OWNED_TYPES = {"PURCHASE", "EMI", "FEE", "INTEREST", "REFUND", "CREDIT"}

# Types that reduce their owner's bill rather than adding to it.
REDUCING_TYPES = {"REFUND", "CREDIT"}


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


def _signed(txn) -> float:
    """Credits and refunds reduce what their owner owes; charges increase it."""
    amount = float(txn.get("amount") or 0)
    return -amount if txn.get("transactionType") in REDUCING_TYPES else amount


# Words that say what kind of credit a row is, not what earned it.
_CREDIT_NOISE = {
    "cashback", "cash", "back", "waiver", "surcharge", "reward", "rewards",
    "credit", "adjustment", "reversal", "on", "the", "of", "for",
}


def _charge_rows(rows) -> list:
    return [r for r in rows if r.get("transactionType") in OWNED_TYPES - REDUCING_TYPES]


def credit_owner(credit: dict, rows) -> str:
    """Work out whose purchase earned a credit, or None if it is not clear.

    Cashback and surcharge waivers are not card-level rebates to divide up —
    each one is earned by a particular purchase. Two signals, both drawn from
    what has already been assigned:

      1. Every charge on the statement belongs to one person, so the credit does
         too. This is the common case on a card only one of you uses.
      2. The credit names a merchant that appears in the charges — "10% Swiggy
         CashBack" against a Swiggy purchase, a fuel-surcharge waiver against a
         fuel station — and those matching charges have a single owner.

    Anything less certain returns None and the credit is simply not netted off
    anyone, which is why these never appear in the review queue.
    """
    charges = [r for r in _charge_rows(rows) if r.get("ownerId")]
    if not charges:
        return None

    owners = {r["ownerId"] for r in charges}
    if len(owners) == 1:
        return owners.pop()

    tokens = {
        word
        for word in (credit.get("merchant") or "").split()
        if len(word) >= 4 and word not in _CREDIT_NOISE
    }
    if not tokens:
        return None

    matched = {
        r["ownerId"]
        for r in charges
        if any(token[:5] in (r.get("merchant") or "") for token in tokens)
    }
    return matched.pop() if len(matched) == 1 else None


def compute_share(data_root: str, statement_id: str, owner_id: str) -> float:
    """What `owner_id` actually owes on this statement.

    Their charges, less the credits assigned to them — cashback and surcharge
    waivers are earned by a particular purchase, so they belong to whoever made
    it. A fuel-surcharge waiver on someone else's fuel is not your discount, and
    splitting credits by spend share would hand you one.

    A credit nobody has claimed simply is not netted off anyone, which keeps
    each person's figure honest while a statement is still being reviewed.

    Payments are excluded: they settle the *previous* cycle, not this one.
    """
    rows = list((store.get_transactions(data_root, statement_id) or {}).values())

    total = 0.0
    for txn in rows:
        if txn.get("transactionType") not in OWNED_TYPES:
            continue
        if txn.get("transactionType") in REDUCING_TYPES and not txn.get("ownerId"):
            # Nobody claimed it, so work out whose purchase earned it rather
            # than making you assign cashback by hand.
            if credit_owner(txn, rows) != owner_id:
                continue
        elif txn.get("ownerId") != owner_id:
            continue
        total += _signed(txn)
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
