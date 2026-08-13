"""Per-bank parsers, written against real statements.

Each class encodes only what that issuer's PDF actually does differently. Verify
any change with `GET /credit-cards/debug/extract?cardId=<id>&month=<YYYY-MM>`,
which prints the extracted lines and what was recognised, and writes nothing.
"""

import re

from .base import (
    CREDIT,
    EMI,
    FEE,
    INTEREST,
    PAYMENT,
    PURCHASE,
    REFUND,
    SPEND_TYPES,
    BaseParser,
)


class GenericParser(BaseParser):
    key = "generic"
    bank = "Generic"


class KotakParser(BaseParser):
    """Kotak Cashback+.

    Cleanest of the four: one row per line, `Cr` marks credits, and a Spends
    Category column sits between description and amount — the same categories
    the spreadsheet uses, so it is captured rather than discarded.
    """

    key = "kotak"
    bank = "Kotak Mahindra Bank"

    CREDIT_MARKERS = {"cr"}
    CATEGORY_WORDS = (
        "Grocery", "Fuel", "Restaurants", "Computer", "Apparels", "Utilities",
        "Entertainment", "Travel", "Health", "Education", "Insurance",
        "Jewellery", "Departmental Stores", "Electronics", "Services",
        "Hotels", "Others",
    )
    # Section headings carry the type when the wording alone is ambiguous.
    SECTION_TYPES = (
        (re.compile(r"^Payments and Other Credits\s*$", re.I), CREDIT),
        (re.compile(r"^Purchases made in this cycle", re.I), PURCHASE),
        (re.compile(r"^EMI\s*&\s*Loans\s*$", re.I), EMI),
    )
    SKIP_LINE = re.compile(
        r"^(page \d|date\s+description|rewards summary|key updates|"
        r"important information|opening balance|to explore|need help|"
        r"increase credit limit)",
        re.I,
    )
    TXN_BLOCK_START = re.compile(r"^Transactions Details from", re.I)

    def extracted_for_reconcile(self, statement):
        """Kotak's "Purchases made in this cycle" is a net figure.

        It folds in everything booked against an EMI this cycle — principal,
        interest and the one-off conversion fee — and nets off credits: fuel
        surcharge waivers, and the full reversal posted when a purchase is
        converted to EMI mid-cycle. Plain GST is excluded, since Kotak reports
        that separately under "Other fees & charges".
        """
        total = 0.0
        for txn in statement.transactions:
            emi_charge = txn.isEMI and txn.transactionType in (INTEREST, FEE)
            if txn.transactionType in (PURCHASE, EMI) or emi_charge:
                total += txn.amount
            elif txn.transactionType == CREDIT:
                total -= txn.amount
        return round(total, 2)


class ICICIParser(BaseParser):
    """ICICI (Amazon Pay and Sapphiro share this layout).

    Three quirks: headings render with doubled letters ("SSTTAATTEEMMEENNTT"),
    a reward-percentage badge can precede the date, and long descriptions wrap
    onto the next line — which is where the EMI progress marker "<17/24>" lives.
    """

    key = "icici"
    bank = "ICICI Bank"

    CREDIT_MARKERS = {"cr"}
    # A reward-category badge can sit in front of the date, either bare
    # ("100% 30/06/2026 …") or spelled out ("Apparel/Grocery-8% Others-85%
    # 22/06/2026 …"). Strip everything up to the date, but only when a date
    # actually follows, so ordinary descriptions are untouched.
    ROW_LEADING_NOISE = re.compile(r"^.*%\s*(?=\d{2}/\d{2}/\d{4}\b)")
    # Reward-points column sits between description and amount.
    DESCRIPTION_TRAILING_NOISE = re.compile(r"\s+-?\d{1,4}$")
    # Each row opens with an 11-digit serial number.
    DESCRIPTION_LEADING_NOISE = re.compile(r"^\d{8,}\s+")
    TXN_BLOCK_START = re.compile(r"^SPENDS OVERVIEW|^Date\s+SerNo", re.I)
    TXN_BLOCK_END = re.compile(r"^(# International Spends|EARNINGS)\s*$", re.I)
    ALLOW_CONTINUATION = True
    SUMMARY_LOOKAHEAD = 2
    SKIP_LINE = re.compile(
        r"^(page \d|date\s+serno|spends overview|statement summary|"
        r"credit summary|earnings|important messages|# international)",
        re.I,
    )

    _DOUBLED = re.compile(r"\b(?:[A-Z]{2}){3,}\b")

    def reconcile(self, statement):
        """ICICI prints no purchases subtotal, only Total Amount Due.

        With the previous balance cleared, that figure is this cycle's charges
        (purchases, EMI principal, interest and tax) *less* anything credited
        back — a refund, a reversal, cashback. Those credits are not carried in
        the "Payments/Credits" line, so they have to come off here or a card
        with a single refund reports a mismatch the size of the refund.
        """
        charges = sum(
            t.amount for t in statement.transactions if t.transactionType in SPEND_TYPES
        )
        credited = sum(
            t.amount
            for t in statement.transactions
            if t.transactionType in (CREDIT, REFUND)
        )
        extracted = round(charges - credited, 2)
        statement.extractedTotal = extracted
        if statement.totalAmount is None:
            statement.reconciliation = "UNKNOWN"
            return
        statement.expectedTotal = statement.totalAmount
        statement.difference = round(statement.totalAmount - extracted, 2)
        statement.reconciliation = (
            "RECONCILED"
            if abs(statement.difference) <= self.RECONCILE_TOLERANCE
            else "MISMATCH"
        )

    def preprocess(self, lines: list) -> list:
        """Collapse ICICI's doubled-letter headings so labels match.

        "SSTTAATTEEMMEENNTT DDAATTEE" -> "STATEMENT DATE".
        """
        return [self._undouble(line) if self._DOUBLED.search(line) else line for line in lines]

    @staticmethod
    def _undouble(line: str) -> str:
        out = []
        for word in line.split(" "):
            if len(word) >= 6 and word.isalpha() and word.isupper():
                halves = word[0::2], word[1::2]
                if halves[0] == halves[1]:
                    word = halves[0]
            out.append(word)
        return " ".join(out)


class HDFCParser(BaseParser):
    """Swiggy HDFC Bank.

    The rupee glyph extracts as a bare "C" before the amount, credits are marked
    by a "+" *before* it, and every row ends with a Purchase-Indicator bullet.
    """

    key = "hdfc"
    bank = "HDFC Bank"

    # Trailing PI bullet ("l") and any stray marks after the amount.
    ROW_TRAILING_NOISE = re.compile(r"\s+[l|•]\s*$")
    # Rows open with "| HH:MM" after the date.
    DESCRIPTION_LEADING_NOISE = re.compile(r"^\|?\s*\d{2}:\d{2}\s*")
    # The rupee glyph extracts as "C"; a "+" before it marks a credit.
    DESCRIPTION_TRAILING_NOISE = re.compile(r"\s*\+?\s*C\s*$")
    TXN_BLOCK_START = re.compile(r"^Domestic Transactions\s*$", re.I)
    TXN_BLOCK_END = re.compile(r"^(Offers on your card|Cash Back Summary)\s*$", re.I)
    SKIP_LINE = re.compile(
        r"^(page \d|date & time|domestic transactions|international transactions|"
        r"important information|benefits on your card|your card control|"
        r"cash back summary|offers on your card|sr no)",
        re.I,
    )

    # Summary figures print on one line under wrapped headings, in this order.
    _SUMMARY_ROW = re.compile(
        r"^\s*C?(?P<prev>[\d,]+\.\d{2})\s+C?(?P<pay>[\d,]+\.\d{2})\s*\+\s*"
        r"C?(?P<purch>[\d,]+\.\d{2})\s*\+\s*C?(?P<fin>[\d,]+\.\d{2})\s*=?\s*$"
    )
    _TOTAL_ROW = re.compile(r"^\s*_\s*C(?P<total>[\d,]+\.\d{2})\s*$")

    def is_credit(self, before: str, marker: str) -> bool:
        # "... + C 3,589.00" is a credit; "... C 336.00" is a debit.
        return bool(re.search(r"\+\s*C?\s*$", before.strip()))

    def parse_summary(self, lines, statement):
        super().parse_summary(lines, statement)
        for line in lines:
            row = self._SUMMARY_ROW.match(line)
            if row:
                statement.previousBalance = float(row.group("prev").replace(",", ""))
                statement.paymentsCredits = float(row.group("pay").replace(",", ""))
                statement.purchases = float(row.group("purch").replace(",", ""))
                statement.feesInterest = float(row.group("fin").replace(",", ""))
                continue
            total = self._TOTAL_ROW.match(line)
            if total and statement.totalAmount is None:
                statement.totalAmount = float(total.group("total").replace(",", ""))


class AxisParser(BaseParser):
    key = "axis"
    bank = "Axis Bank"

    SKIP_LINE = re.compile(
        r"^(page \d|date\s+transaction|transaction details|edge reward|"
        r"statement summary)",
        re.I,
    )


class SBIParser(BaseParser):
    """BPCL SBI Card OCTANE.

    Single-letter markers (C credit, D debit, M monthly instalment, T temporary
    credit), dates as "30 Jun 26", tax sub-rows printed without a date, and a
    positional ACCOUNT SUMMARY grid whose five figures sit on one line under
    their headings.
    """

    key = "sbi"
    bank = "SBI Cards"

    CREDIT_MARKERS = {"c", "cr", "t"}
    ALLOW_DATELESS_ROWS = True
    SUMMARY_LOOKAHEAD = 2
    SKIP_LINE = re.compile(
        r"^(page \d|date\s+transaction|account summary|shop & smile|"
        r"savings and benefits|value added services|important (messages|notes|information)|"
        r"transactions highlighted|c=credit|gstin of sbi|place of supply|"
        r"treatment of emi|w\.e\.f)",
        re.I,
    )

    # The five figures under ACCOUNT SUMMARY, in printed order.
    _SUMMARY_ORDER = (
        "previousBalance",
        "paymentsCredits",
        "purchases",
        "feesInterest",
        None,  # total outstanding — not the same thing as total amount due
    )
    # Five money values on one line. Not every column is comma-grouped — the
    # fee column is often small ("100.29") — so plain decimals count too.
    _FIVE_MONEY = re.compile(r"^\s*(?:[\d,]+\.\d{2}\s+){4}[\d,]+\.\d{2}\s*$")
    TXN_BLOCK_START = re.compile(r"^Date\s+Transaction Details\s+Amount", re.I)
    TXN_BLOCK_END = re.compile(r"^Transactions highlighted in grey", re.I)

    def parse_summary(self, lines, statement):
        super().parse_summary(lines, statement)
        self._account_summary_grid(lines, statement)

    def _account_summary_grid(self, lines, statement):
        """Read the five-figure ACCOUNT SUMMARY row positionally.

        More reliable than label matching here: SBI's headings wrap across three
        lines, so no label sits on the same line as its value.
        """
        try:
            start = next(
                i for i, line in enumerate(lines) if line.strip().upper() == "ACCOUNT SUMMARY"
            )
        except StopIteration:
            return
        for line in lines[start: start + 8]:
            if not self._FIVE_MONEY.match(line):
                continue
            values = [float(v.replace(",", "")) for v in line.split()]
            for attr, value in zip(self._SUMMARY_ORDER, values):
                if attr:
                    setattr(statement, attr, value)
            return

    def classify(self, description, is_credit, section_type=None):
        # "PAYMENT RECEIVED 000000000DKT50PM1OBQAWG" is a payment, not a credit,
        # and SBI marks it with the same "C" it uses for reward credits.
        if is_credit and re.search(r"payment received", description, re.I):
            return PAYMENT
        return super().classify(description, is_credit, section_type)
