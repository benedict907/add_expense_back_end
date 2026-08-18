"""The sync pipeline: Gmail -> decrypt -> extract -> parse -> match -> save.

One card at a time, with a per-card result that names the exact stage that
failed. A card failing never aborts the others, and a failed card writes
nothing — half-parsed transactions are worse than none.
"""

import logging

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
)

log = logging.getLogger("creditcards.sync")

# Stage names, in order. Surfaced to the UI so the progress list matches what
# the server actually did.
STAGES = ("finding", "downloading", "decrypting", "extracting", "matching", "saving")

STATUS_IMPORTED = "IMPORTED"
STATUS_UPDATED = "UPDATED"
STATUS_ALREADY_IMPORTED = "ALREADY_IMPORTED"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_FAILED = "FAILED"

RECONCILED = "RECONCILED"
MISMATCH = "MISMATCH"
UNKNOWN_TOTAL = "UNKNOWN"

# Statement totals include previous balance and exclude nothing, so an exact
# match is rare; treat sub-rupee drift as reconciled.
RECONCILE_TOLERANCE = 1.0


class CardResult(dict):
    """Plain dict so it serialises straight to JSON."""

    def __init__(self, card):
        super().__init__(
            cardId=card.id,
            cardName=card.cardName,
            bankName=card.bankName,
            lastFourDigits=card.lastFourDigits,
            status=STATUS_FAILED,
            stage=None,
            message="",
            statementId=None,
            transactionCount=0,
            ownersDetected=0,
            needsReview=0,
            reconciliation=UNKNOWN_TOTAL,
            expectedTotal=None,
            extractedTotal=None,
            difference=None,
            extractionMethod=None,
            warnings=[],
        )


def _reconcile(statement, result: CardResult):
    """Copy the parser's reconciliation verdict onto the sync result.

    The comparison itself lives in the parser, because which printed figure is
    comparable differs per bank: Kotak folds EMI instalments and surcharge
    waivers into its purchases line, HDFC and SBI print a clean purchases
    subtotal, and ICICI prints none at all.
    """
    result["reconciliation"] = statement.reconciliation
    result["expectedTotal"] = statement.expectedTotal
    result["extractedTotal"] = statement.extractedTotal
    result["difference"] = statement.difference


def _build_transactions(statement, card, index, statement_id, existing: dict, now: int):
    """Turn parsed rows into RTDB payloads, keeping manual owner edits.

    Returns (payload_dict, owners_detected, needs_review).
    """
    payload = {}
    detected = 0
    review = 0

    # A transaction's id includes its type, so a parser fix that reclassifies a
    # row (EMI -> Purchase, say) mints a new id and would strand the owner set
    # on the old one. This fallback index carries owners across that change by
    # matching on the parts that do not depend on classification.
    by_identity = {}
    for prior in existing.values():
        if not isinstance(prior, dict) or not prior.get("ownerId"):
            continue
        key = (
            prior.get("transactionDate"),
            round(float(prior.get("amount") or 0), 2),
            prior.get("merchant"),
        )
        by_identity.setdefault(key, prior)

    for txn in statement.transactions:
        txn_id = normalize.transaction_id(
            card.id,
            statement.statementMonth,
            txn.transactionDate,
            txn.amount,
            txn.merchant,
            txn.transactionType,
        )
        prior = existing.get(txn_id) or by_identity.get(
            (txn.transactionDate, round(txn.amount, 2), txn.merchant), {}
        )

        # A hand-set owner is the strongest signal there is; never overwrite it.
        if prior.get("ownerSource") == owner_match.MANUAL and prior.get("ownerId"):
            owner = {
                "ownerId": prior["ownerId"],
                "ownerName": index.owner_name(prior["ownerId"]) or prior.get("ownerName", ""),
                "ownerSource": owner_match.MANUAL,
                "confidence": 1.0,
            }
        else:
            owner = index.resolve(card.id, txn.merchant, txn.isEMI)

        if owner["ownerId"]:
            detected += 1
        else:
            review += 1

        payload[txn_id] = {
            "transactionId": txn_id,
            "statementId": statement_id,
            "cardId": card.id,
            "statementMonth": statement.statementMonth,
            "transactionDate": txn.transactionDate,
            "postingDate": txn.postingDate or None,
            "description": txn.description,
            "rawDescription": txn.rawDescription,
            "merchant": txn.merchant,
            "amount": txn.amount,
            "transactionType": txn.transactionType,
            "category": txn.category,
            "isEMI": txn.isEMI,
            "emiAmount": txn.emiAmount,
            "emiTenure": txn.emiTenure,
            "emiNumber": txn.emiNumber,
            "ownerId": owner["ownerId"],
            "ownerName": owner["ownerName"],
            "ownerSource": owner["ownerSource"],
            "confidence": owner["confidence"],
            "createdAt": prior.get("createdAt") or now,
            "updatedAt": now,
        }
    return payload, detected, review


def sync_card(card, month_key: str, data_root: str, index, force: bool = False) -> CardResult:
    """Run the full pipeline for one card. Never raises; failures land in the result."""
    result = CardResult(card)
    statement_id = normalize.statement_id(card.id, month_key)
    result["statementId"] = statement_id

    existing_statement = store.get_statement(data_root, statement_id) or {}
    if existing_statement and not force:
        existing_txns = store.get_transactions(data_root, statement_id)
        result.update(
            status=STATUS_ALREADY_IMPORTED,
            stage=None,
            message=f"{month_key} statement already imported.",
            transactionCount=len(existing_txns),
            ownersDetected=sum(1 for t in existing_txns.values() if t.get("ownerId")),
            needsReview=sum(1 for t in existing_txns.values() if not t.get("ownerId")),
            reconciliation=existing_statement.get("reconciliation", UNKNOWN_TOTAL),
            expectedTotal=existing_statement.get("expectedTotal"),
            extractedTotal=existing_statement.get("extractedTotal"),
            difference=existing_statement.get("difference"),
            extractionMethod=existing_statement.get("extractionMethod"),
        )
        return result

    try:
        result["stage"] = "finding"
        email = gmail_client.find_statement_message(card, month_key)

        result["stage"] = "downloading"
        raw = gmail_client.download_attachment(email["messageId"], email["attachmentId"])

        result["stage"] = "decrypting"
        decrypted = pdf_unlock.decrypt(raw, card.password())

        result["stage"] = "extracting"
        extraction = extract.extract(decrypted)
        result["extractionMethod"] = extraction.method
        statement = parsers.get_parser(card).parse(extraction, month_key)
        result["warnings"] = list(statement.warnings)

        # The email window can only narrow things down so far. If a month has no
        # statement at all, the forward-widened search will happily return the
        # next month's mail — so make the PDF itself prove which month it is.
        belongs_to = statement.statementDate or statement.statementPeriodEnd
        if belongs_to and belongs_to[:7] != month_key:
            result.update(
                status=STATUS_NOT_FOUND,
                stage=None,
                message=(
                    f"Statement not found for {month_key}. The closest email "
                    f"({email['attachmentName']}) is the {belongs_to[:7]} statement."
                ),
            )
            return result

        if not statement.transactions:
            result.update(
                status=STATUS_FAILED,
                message=(
                    "No transactions could be extracted from the PDF "
                    f"(extraction method: {extraction.method}). Nothing was saved."
                ),
            )
            return result

        _reconcile(statement, result)

        result["stage"] = "matching"
        now = store.now_ms()
        existing_txns = store.get_transactions(data_root, statement_id)
        txn_payload, detected, review = _build_transactions(
            statement, card, index, statement_id, existing_txns, now
        )
        result.update(
            transactionCount=len(txn_payload),
            ownersDetected=detected,
            needsReview=review,
        )

        result["stage"] = "saving"
        store.save_transactions(data_root, statement_id, txn_payload)
        store.save_statement(
            data_root,
            statement_id,
            {
                "statementId": statement_id,
                "cardId": card.id,
                "statementMonth": month_key,
                "statementPeriodStart": statement.statementPeriodStart,
                "statementPeriodEnd": statement.statementPeriodEnd,
                "statementDate": statement.statementDate,
                "dueDate": statement.dueDate,
                "totalAmount": statement.totalAmount,
                "minimumDue": statement.minimumDue,
                "previousBalance": statement.previousBalance,
                "paymentsCredits": statement.paymentsCredits,
                "purchases": statement.purchases,
                "emiTotal": statement.emiTotal,
                "feesInterest": statement.feesInterest,
                "creditLimit": statement.creditLimit,
                "transactionCount": len(txn_payload),
                "reconciliation": result["reconciliation"],
                "expectedTotal": result["expectedTotal"],
                "extractedTotal": result["extractedTotal"],
                "difference": result["difference"],
                "extractionMethod": extraction.method,
                "warnings": result["warnings"],
                # Provenance only — no attachment bytes, no password.
                "gmailMessageId": email["messageId"],
                "emailSubject": email["subject"],
                "attachmentName": email["attachmentName"],
                "importedAt": existing_statement.get("importedAt") or now,
                "updatedAt": now,
            },
        )

        result.update(
            status=STATUS_UPDATED if existing_statement else STATUS_IMPORTED,
            stage=None,
            message=(
                f"{month_key} statement "
                f"{'re-imported' if existing_statement else 'imported'} — "
                f"{len(txn_payload)} transactions."
            ),
        )
        return result

    except gmail_client.StatementNotFound as exc:
        result.update(status=STATUS_NOT_FOUND, message="Statement not found. " + str(exc))
    except pdf_unlock.WrongPassword as exc:
        result.update(status=STATUS_FAILED, message=str(exc))
    except (gmail_client.GmailError, pdf_unlock.PdfError, extract.ExtractionError) as exc:
        result.update(status=STATUS_FAILED, message=str(exc))
    except RuntimeError as exc:
        # Covers missing password env vars and other configuration gaps.
        result.update(status=STATUS_FAILED, message=str(exc))
    except Exception as exc:  # pragma: no cover - unexpected, still per-card
        log.exception("Unexpected failure syncing %s", card.id)
        result.update(status=STATUS_FAILED, message=f"Unexpected error: {exc}")
    return result


def sync_month(month_key: str, card_ids=None, force: bool = False) -> dict:
    """Sync every active card (or a subset) for one statement month."""
    data_root = config.data_root()
    cards = config.load_cards()
    if card_ids:
        wanted = set(card_ids)
        cards = [c for c in cards if c.id in wanted]

    # Check the mailbox once. Gmail auth is shared by every card, so a dead
    # token is one problem, not one per card — and finding out here costs a
    # single API call instead of a failed download per statement.
    gmail_client.check_auth()

    store.sync_cards(data_root, config.load_cards(include_inactive=True))
    owners = store.load_owners(data_root)
    index = owner_match.OwnerIndex(
        store.load_rules(data_root),
        store.load_owner_history(data_root),
        owners,
    )

    results = [sync_card(card, month_key, data_root, index, force=force) for card in cards]

    # Keep the expenses dashboard's dues list in step with what was imported.
    # Only generated rows are touched; hand-added dues are never modified.
    dues.refresh_month(data_root, month_key)

    meta = {
        "month": month_key,
        "syncedAt": store.now_ms(),
        "cards": {r["cardId"]: r["status"] for r in results},
        "needsReview": sum(r["needsReview"] for r in results),
    }
    store.save_sync_meta(data_root, meta)

    return {
        "month": month_key,
        "syncedAt": meta["syncedAt"],
        "results": results,
        "needsReview": meta["needsReview"],
        "imported": sum(
            1 for r in results if r["status"] in (STATUS_IMPORTED, STATUS_UPDATED)
        ),
        "failed": sum(1 for r in results if r["status"] == STATUS_FAILED),
        "notFound": sum(1 for r in results if r["status"] == STATUS_NOT_FOUND),
    }
