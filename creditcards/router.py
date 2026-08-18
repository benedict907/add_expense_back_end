"""HTTP surface for the credit-card module.

Mounted at /credit-cards. Every route is authenticated. No response ever
contains a PDF password, a Gmail token, or attachment bytes.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import (
    config,
    dues,
    extract,
    gmail_client,
    normalize,
    owner_match,
    parsers,
    pdf_unlock,
    store,
    sync,
)
from .auth import require_auth

router = APIRouter(prefix="/credit-cards", tags=["credit-cards"])

# Rows that belong to a person. Spending, plus refunds — a refund reverses
# someone's purchase, so it has to reduce that person's total rather than sit
# unattributed. Payments and cashback belong to the card, not to anyone.
_OWNED_TYPES = parsers.SPEND_TYPES | {parsers.REFUND}


class SyncRequest(BaseModel):
    month: str | None = None
    cardIds: list[str] | None = None
    # Re-parse a statement that was already imported (after a parser fix).
    force: bool = False


class OwnerAssignment(BaseModel):
    ownerId: str | None = None
    # Also teach the merchant rule, so future months match automatically.
    applyRule: bool = True
    # Scope the learned rule to this card, or to every card.
    ruleScope: str = "card"


class BulkOwnerAssignment(BaseModel):
    ownerId: str
    month: str | None = None
    # Narrow the sweep. Omitted means "every card, this month".
    cardIds: list[str] | None = None
    # Leave rows that already have an owner alone — the usual case when you are
    # filling in the bulk of a statement and have already fixed a few by hand.
    onlyUnassigned: bool = True
    # Skip rows that belong to the card rather than to a person: payments and
    # cashback. Refunds are included — a refund reverses someone's purchase.
    spendOnly: bool = True
    applyRule: bool = True


class OwnerCreate(BaseModel):
    name: str
    color: str | None = None


def _month(value: str | None) -> str:
    if not value:
        return normalize.current_month_key()
    parts = value.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or not value.replace("-", "").isdigit():
        raise HTTPException(status_code=400, detail="month must look like 2026-08")
    return f"{parts[0]}-{int(parts[1]):02d}"


@router.get("/config")
def get_config(_auth=Depends(require_auth)):
    """Everything the dashboard needs to render before any sync runs."""
    try:
        cards = config.load_cards(include_inactive=True)
    except config.ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    data_root = config.data_root()
    gmail_ready = True
    gmail_account = ""
    gmail_error = ""
    try:
        gmail_account = gmail_client.profile_email()
    except Exception as exc:
        gmail_ready = False
        gmail_error = str(exc)

    return {
        "cards": [card.public_dict() for card in cards],
        "owners": store.load_owners(data_root),
        "lastSync": store.load_sync_meta(data_root),
        "currentMonth": normalize.current_month_key(),
        "gmail": {
            "configured": gmail_ready,
            "account": gmail_account,
            "error": gmail_error,
        },
        "parsers": parsers.available(),
    }


@router.post("/sync")
def run_sync(body: SyncRequest, _auth=Depends(require_auth)):
    """Fetch, decrypt, parse and store this month's statements."""
    try:
        return sync.sync_month(_month(body.month), body.cardIds, force=body.force)
    except config.ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except gmail_client.AuthExpired as exc:
        # Same class of problem as a missing config: nothing is wrong with the
        # request, the server needs a human to re-authorise it.
        raise HTTPException(status_code=503, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/preview")
def preview(cardId: str, month: str | None = None, _auth=Depends(require_auth)):
    """Check a card's Gmail query without downloading or decrypting anything.

    Use this to tune senders/subjectContains in cards.json.
    """
    try:
        card = config.get_card(cardId)
    except config.ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    month_key = _month(month)
    query = gmail_client.build_query(card, month_key)
    try:
        email = gmail_client.find_statement_message(card, month_key)
    except gmail_client.StatementNotFound as exc:
        return {"cardId": cardId, "month": month_key, "query": query, "found": False,
                "message": str(exc)}
    except gmail_client.GmailError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return {
        "cardId": cardId,
        "month": month_key,
        "query": query,
        "found": True,
        "subject": email["subject"],
        "sender": email["sender"],
        "attachmentName": email["attachmentName"],
        "passwordConfigured": bool(card.passwordEnv) and _password_present(card),
    }


def _password_present(card) -> bool:
    try:
        card.password()
        return True
    except RuntimeError:
        return False


@router.get("/debug/extract")
def debug_extract(
    cardId: str,
    month: str | None = None,
    limit: int = 80,
    _auth=Depends(require_auth),
):
    """Show what the pipeline actually reads out of a real statement.

    This is the tool for tuning a bank parser: it prints the extracted lines,
    which rows were recognised as transactions, and the parsed summary — without
    writing anything to the database.
    """
    try:
        card = config.get_card(cardId)
    except config.ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    month_key = _month(month)
    try:
        email = gmail_client.find_statement_message(card, month_key)
        raw = gmail_client.download_attachment(email["messageId"], email["attachmentId"])
        encrypted = pdf_unlock.is_encrypted(raw)
        decrypted = pdf_unlock.decrypt(raw, card.password())
        extraction = extract.extract(decrypted)
    except gmail_client.StatementNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (gmail_client.GmailError, pdf_unlock.PdfError, extract.ExtractionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    parser = parsers.get_parser(card)
    statement = parser.parse(extraction, month_key)
    recognised = [line for line in extraction.lines if parser.looks_like_transaction(line)]

    return {
        "cardId": cardId,
        "month": month_key,
        "parser": parser.key,
        "encrypted": encrypted,
        "extractionMethod": extraction.method,
        "pageCount": extraction.page_count,
        "tableCount": len(extraction.tables),
        "lineCount": len(extraction.lines),
        "recognisedRowCount": len(recognised),
        "lines": extraction.lines[:limit],
        "recognisedRows": recognised[:limit],
        "summary": {
            "statementDate": statement.statementDate,
            "dueDate": statement.dueDate,
            "periodStart": statement.statementPeriodStart,
            "periodEnd": statement.statementPeriodEnd,
            "totalAmount": statement.totalAmount,
            "minimumDue": statement.minimumDue,
            "previousBalance": statement.previousBalance,
            "paymentsCredits": statement.paymentsCredits,
            "purchases": statement.purchases,
        },
        "sampleTransactions": [
            {
                "date": t.transactionDate,
                "description": t.description,
                "merchant": t.merchant,
                "amount": t.amount,
                "type": t.transactionType,
                "isEMI": t.isEMI,
            }
            for t in statement.transactions[:20]
        ],
        "warnings": statement.warnings,
    }


@router.patch("/transactions/{statement_id}/{txn_id}/owner")
def assign_owner(
    statement_id: str,
    txn_id: str,
    body: OwnerAssignment,
    _auth=Depends(require_auth),
):
    """Set, change or clear a transaction's owner, and learn from it."""
    data_root = config.data_root()
    txn = store.get_transaction(data_root, statement_id, txn_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    owners = store.load_owners(data_root)
    now = store.now_ms()

    if body.ownerId:
        if body.ownerId not in owners:
            raise HTTPException(status_code=400, detail=f"Unknown owner {body.ownerId!r}")
        values = {
            "ownerId": body.ownerId,
            "ownerName": owners[body.ownerId].get("name", ""),
            "ownerSource": owner_match.MANUAL,
            "confidence": 1.0,
            "updatedAt": now,
        }
    else:
        # Clearing puts it back in the review queue rather than guessing again.
        values = {
            "ownerId": None,
            "ownerName": "",
            "ownerSource": owner_match.UNKNOWN,
            "confidence": 0.0,
            "updatedAt": now,
        }

    store.update_transaction(data_root, statement_id, txn_id, values)

    rule_written = None
    if body.applyRule and body.ownerId:
        scope = "*" if body.ruleScope == "all" else txn.get("cardId")
        card_id, merchant, payload = owner_match.rule_update(
            scope, txn.get("description", ""), body.ownerId, now
        )
        if merchant:
            store.save_rule(data_root, card_id, merchant, payload)
            rule_written = {"scope": card_id, "merchant": merchant}

    # Your share just changed, so the derived due for this statement is stale.
    due = dues.refresh_statement(data_root, statement_id)

    return {
        "status": "ok",
        "transaction": {**txn, **values},
        "ruleWritten": rule_written,
        "due": due,
    }


@router.patch("/transactions/bulk-owner")
def assign_owner_bulk(body: BulkOwnerAssignment, _auth=Depends(require_auth)):
    """Assign one person to a whole month, or to whole cards within it.

    Built for the common shape of a shared card: almost every row belongs to one
    person, so you set that person for everything and then correct the handful
    that don't. Defaults are conservative — only unassigned spending rows are
    touched, so a sweep never overwrites a choice already made by hand.
    """
    data_root = config.data_root()
    owners = store.load_owners(data_root)
    if body.ownerId not in owners:
        raise HTTPException(status_code=400, detail=f"Unknown owner {body.ownerId!r}")

    month_key = _month(body.month)
    owner_name = owners[body.ownerId].get("name", "")
    now = store.now_ms()
    wanted_cards = set(body.cardIds or [])

    statements = store.get(data_root, store.STATEMENTS) or {}
    targets = [
        statement_id
        for statement_id, statement in statements.items()
        if statement.get("statementMonth") == month_key
        and (not wanted_cards or statement.get("cardId") in wanted_cards)
    ]

    updated = 0
    skipped = 0
    rules = {}
    for statement_id in targets:
        per_txn = {}
        for txn_id, txn in (store.get_transactions(data_root, statement_id) or {}).items():
            if body.spendOnly and txn.get("transactionType") not in _OWNED_TYPES:
                skipped += 1
                continue
            if body.onlyUnassigned and txn.get("ownerId"):
                skipped += 1
                continue
            per_txn[txn_id] = {
                "ownerId": body.ownerId,
                "ownerName": owner_name,
                "ownerSource": owner_match.MANUAL,
                "confidence": 1.0,
                "updatedAt": now,
            }
            if body.applyRule:
                card_id, merchant, payload = owner_match.rule_update(
                    txn.get("cardId"), txn.get("description", ""), body.ownerId, now
                )
                if merchant:
                    rules.setdefault(card_id, {})[merchant] = payload

        if per_txn:
            store.update_transactions_bulk(data_root, statement_id, per_txn)
            updated += len(per_txn)

    if rules:
        store.save_rules_bulk(data_root, rules)

    refreshed = dues.refresh_month(data_root, month_key)

    return {
        "status": "ok",
        "month": month_key,
        "dues": [d for d in refreshed if d.get("written")],
        "ownerId": body.ownerId,
        "ownerName": owner_name,
        "statements": len(targets),
        "updated": updated,
        "skipped": skipped,
        "rulesWritten": sum(len(v) for v in rules.values()),
    }


@router.delete("/owners/{owner_id}")
def delete_owner(owner_id: str, _auth=Depends(require_auth)):
    """Remove a person, but only when nothing points at them.

    Deleting an owner that still owns transactions would leave those rows
    pointing at a name that no longer exists, so this refuses and reports where
    the references are.
    """
    data_root = config.data_root()
    owners = store.load_owners(data_root)
    if owner_id not in owners:
        raise HTTPException(status_code=404, detail=f"Unknown owner {owner_id!r}")

    used_by_txns = 0
    for rows in (store.get(data_root, store.TRANSACTIONS) or {}).values():
        for txn in (rows or {}).values():
            if isinstance(txn, dict) and txn.get("ownerId") == owner_id:
                used_by_txns += 1

    used_by_rules = 0
    for merchants in (store.load_rules(data_root) or {}).values():
        for rule in (merchants or {}).values():
            if isinstance(rule, dict) and rule.get("ownerId") == owner_id:
                used_by_rules += 1

    if used_by_txns or used_by_rules:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{owners[owner_id].get('name', owner_id)} is still in use: "
                f"{used_by_txns} transaction(s) and {used_by_rules} merchant "
                "rule(s). Reassign them first."
            ),
        )

    if owner_id == dues.self_owner_id(data_root):
        raise HTTPException(
            status_code=409, detail="This owner is the one your dues are derived from."
        )

    store.delete(data_root, store.OWNERS, owner_id)
    return {"status": "ok", "deleted": owner_id}


@router.get("/owners")
def list_owners(_auth=Depends(require_auth)):
    return store.load_owners(config.data_root())


@router.post("/owners")
def create_owner(body: OwnerCreate, _auth=Depends(require_auth)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Owner name is required")
    data_root = config.data_root()
    owners = store.load_owners(data_root)
    owner_id = "owner_" + normalize.normalize_merchant(name).replace(" ", "_")[:24]
    if not owner_id.strip("_") or owner_id == "owner_":
        raise HTTPException(status_code=400, detail="Owner name must contain letters or digits")
    payload = {
        "name": name,
        "color": body.color or "var(--color-mint)",
        "order": len(owners) + 1,
        "createdAt": store.now_ms(),
    }
    store.set_value(data_root, payload, store.OWNERS, owner_id)
    return {"ownerId": owner_id, **payload}
