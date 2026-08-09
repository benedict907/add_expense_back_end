"""Decide who a transaction belongs to, learning from past months.

Signal cascade, strongest first. The first signal that clears its confidence
floor wins; nothing below MIN_CONFIDENCE is assigned at all, because a wrong
owner is worse than an empty one — the dashboard surfaces blanks loudly.

    1.00  manual rule for this exact merchant on this card
    0.85  manual rule for this merchant on any card
    0.75  same merchant, same card, in a previous statement
    0.70  EMI lineage: this EMI merchant was owned before
    0.60  same merchant on any card in a previous statement
"""

from . import normalize

MANUAL = "MANUAL"
MERCHANT_RULE = "MERCHANT_RULE"
PREVIOUS_TRANSACTION = "PREVIOUS_TRANSACTION"
UNKNOWN = "UNKNOWN"

MIN_CONFIDENCE = 0.6

CONF_CARD_RULE = 1.0
CONF_GLOBAL_RULE = 0.85
CONF_PREV_SAME_CARD = 0.75
CONF_PREV_EMI = 0.7
CONF_PREV_ANY_CARD = 0.6


class OwnerIndex:
    """Everything needed to resolve owners, loaded once per sync.

    rules:    {cardId: {normalizedMerchant: {ownerId, confidence, updatedAt}}}
              plus the pseudo-card "*" for cross-card rules.
    history:  [{cardId, merchant, ownerId, isEMI, statementMonth}, ...]
    """

    def __init__(self, rules: dict, history: list, owners: dict):
        self.rules = rules or {}
        self.owners = owners or {}
        self._by_card = {}
        self._by_merchant = {}
        self._emi_by_merchant = {}
        for row in history or []:
            owner_id = row.get("ownerId")
            merchant = row.get("merchant")
            if not owner_id or not merchant:
                continue
            self._by_card.setdefault((row.get("cardId"), merchant), []).append(row)
            self._by_merchant.setdefault(merchant, []).append(row)
            if row.get("isEMI"):
                self._emi_by_merchant.setdefault(merchant, []).append(row)

    def owner_name(self, owner_id: str) -> str:
        entry = self.owners.get(owner_id) or {}
        return entry.get("name", "")

    @staticmethod
    def _latest(rows):
        """Most recent assignment wins, so a corrected owner supersedes."""
        return max(rows, key=lambda r: r.get("statementMonth", ""))

    def _rule(self, card_id: str, merchant: str):
        entry = (self.rules.get(card_id) or {}).get(merchant)
        if entry and entry.get("ownerId"):
            return entry
        return None

    def resolve(self, card_id: str, merchant: str, is_emi: bool) -> dict:
        """Return {ownerId, ownerName, ownerSource, confidence}."""
        if merchant:
            rule = self._rule(card_id, merchant)
            if rule:
                return self._result(rule["ownerId"], MERCHANT_RULE, CONF_CARD_RULE)

            global_rule = self._rule("*", merchant)
            if global_rule:
                return self._result(global_rule["ownerId"], MERCHANT_RULE, CONF_GLOBAL_RULE)

            same_card = self._by_card.get((card_id, merchant))
            if same_card:
                row = self._latest(same_card)
                return self._result(row["ownerId"], PREVIOUS_TRANSACTION, CONF_PREV_SAME_CARD)

            if is_emi:
                emi_rows = self._emi_by_merchant.get(merchant)
                if emi_rows:
                    row = self._latest(emi_rows)
                    return self._result(row["ownerId"], PREVIOUS_TRANSACTION, CONF_PREV_EMI)

            any_card = self._by_merchant.get(merchant)
            if any_card:
                row = self._latest(any_card)
                return self._result(row["ownerId"], PREVIOUS_TRANSACTION, CONF_PREV_ANY_CARD)

        return {
            "ownerId": None,
            "ownerName": "",
            "ownerSource": UNKNOWN,
            "confidence": 0.0,
        }

    def _result(self, owner_id: str, source: str, confidence: float) -> dict:
        if confidence < MIN_CONFIDENCE:
            return {
                "ownerId": None,
                "ownerName": "",
                "ownerSource": UNKNOWN,
                "confidence": confidence,
            }
        return {
            "ownerId": owner_id,
            "ownerName": self.owner_name(owner_id),
            "ownerSource": source,
            "confidence": confidence,
        }


def rule_update(card_id: str, description: str, owner_id: str, now_ms: int) -> tuple:
    """Build the ownerRules entry written when the user assigns manually.

    Returns (cardId, normalizedMerchant, payload). Confidence is 1.0 because a
    human said so; a later manual change simply overwrites it.
    """
    merchant = normalize.normalize_merchant(description)
    payload = {
        "ownerId": owner_id,
        "confidence": 1.0,
        "updatedAt": now_ms,
        "source": MANUAL,
    }
    return card_id, merchant, payload
