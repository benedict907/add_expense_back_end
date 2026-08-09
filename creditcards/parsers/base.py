"""Statement parsing: shared machinery plus the hooks banks override.

Built against real statements from five cards (Kotak Cashback+, Amazon Pay
ICICI, ICICI Sapphiro, Swiggy HDFC, BPCL SBI OCTANE). What they genuinely share
is little: a transaction row starts with a date and ends with an amount. Nearly
everything else differs — credit markers (`Cr`, `C`, a leading `+`), currency
glyphs, reward-point columns, category columns, wrapped descriptions, summary
values printed on the line *after* their label.

So `BaseParser` owns the loop and the shared primitives, and exposes narrow
hooks. Bank subclasses in banks.py override only what their PDF actually does.
"""

import re
from dataclasses import dataclass, field

from .. import normalize

# Transaction types. Sign alone is not trusted: a credit-marked row can be a
# refund, a payment, a cashback or a surcharge waiver, and they differ.
PURCHASE = "PURCHASE"
EMI = "EMI"
REFUND = "REFUND"
PAYMENT = "PAYMENT"
FEE = "FEE"
INTEREST = "INTEREST"
CREDIT = "CREDIT"
OTHER = "OTHER"

# Types representing money spent this cycle. Payments, refunds and cashback
# reduce the balance but are not spending.
SPEND_TYPES = {PURCHASE, EMI, FEE, INTEREST}

# Money as these statements print it: comma-grouped, or plain with 2 decimals.
# Deliberately strict — it must not match reward points ("379"), percentages
# ("18"), or reference numbers ("20089477337934").
MONEY = r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{2}"
MONEY_RE = re.compile(rf"(?<![\d.])({MONEY})")

_EMI_WORDS = re.compile(
    r"\b(emi|instal?lment|installment|amortization|flexipay|smart ?emi|"
    r"dial[- ]?an[- ]?emi)\b",
    re.IGNORECASE,
)
# "(Convert To EMI)" is an offer tag on an ordinary purchase — the card is
# telling you the row is *eligible* for conversion, not that it is an EMI. The
# real instalments are separate rows ("EMI PRIN FOR …", "FP EMI 11/12"). Strip
# the tag before EMI detection so these stay purchases.
_EMI_OFFER = re.compile(r"\(?\s*convert\s+to\s+emi\s*\)?", re.IGNORECASE)


def _emi_text(description: str) -> str:
    """Description with conversion-offer tags removed, for EMI detection."""
    return _EMI_OFFER.sub(" ", description or "")


# "EMI 03/12", "(003/020)", "<17/24>", "3 OF 12"
_EMI_PROGRESS = re.compile(r"[(<]?\b(\d{1,3})\s*(?:/|of)\s*(\d{1,3})\b[)>]?", re.IGNORECASE)
_PAYMENT_WORDS = re.compile(
    r"\b(payment received|payment recd|pymt recd|payment - thank you|thank ?you|"
    r"autopay|auto ?debit|funds transfer|bill ?pay)\b",
    re.IGNORECASE,
)
_REFUND_WORDS = re.compile(
    r"\b(refund|reversal|reversed|chargeback|returned)\b", re.IGNORECASE
)
_FEE_WORDS = re.compile(
    r"\b(fee|charges?|annual|joining|renewal|late payment|surcharge|markup|"
    r"over ?limit|cash advance|gst|igst|cgst|sgst)\b",
    re.IGNORECASE,
)
# "INT-" is how Kotak abbreviates the interest leg of an EMI ("EMI INT-DR FONE").
# The word boundary keeps it from firing on "PRINT-".
_INTEREST_WORDS = re.compile(r"\b(interest|finance charge)\b|\bint-", re.IGNORECASE)
_CREDIT_WORDS = re.compile(
    r"\b(cashback|cash back|reward|redeem|waiver|credit adjustment)\b", re.IGNORECASE
)

_LEADING_DATE = re.compile(
    r"^\s*("
    r"\d{2}[/-]\d{2}[/-]\d{2,4}"
    r"|\d{1,2}[ -][A-Za-z]{3}[,]?[ -]?\d{2,4}"
    r"|[A-Za-z]{3,9} \d{1,2}, \d{4}"
    r")"
)
# Prose carrying a summary phrase ("...if your total amount due is not paid...")
# must never be mistaken for the summary line itself.
_PROSE = re.compile(r"\b(will be|if your|is not|please|shall|kindly|you may|refer)\b", re.I)


@dataclass
class ParsedTransaction:
    transactionDate: str = ""
    postingDate: str = ""
    description: str = ""
    rawDescription: str = ""
    merchant: str = ""
    amount: float = 0.0
    transactionType: str = OTHER
    isEMI: bool = False
    emiAmount: float = None
    emiTenure: int = None
    emiNumber: int = None
    category: str = None


@dataclass
class ParsedStatement:
    statementMonth: str = ""
    statementPeriodStart: str = None
    statementPeriodEnd: str = None
    statementDate: str = None
    dueDate: str = None
    totalAmount: float = None
    minimumDue: float = None
    previousBalance: float = None
    paymentsCredits: float = None
    purchases: float = None
    emiTotal: float = None
    feesInterest: float = None
    creditLimit: float = None
    transactions: list = field(default_factory=list)
    extractionMethod: str = ""
    warnings: list = field(default_factory=list)
    # Filled by BaseParser.reconcile — the statement's own figure vs ours.
    reconciliation: str = "UNKNOWN"
    expectedTotal: float = None
    extractedTotal: float = None
    difference: float = None


class ParseError(RuntimeError):
    pass


class BaseParser:
    """Line-oriented statement parser. Subclass and override the class attrs."""

    key = "generic"
    bank = "Generic"

    # ---- summary ----
    # (label regex, ParsedStatement attribute). Order matters: more specific first.
    SUMMARY_FIELDS = (
        (re.compile(r"total\s+amount\s+due|total\s+dues", re.I), "totalAmount"),
        (re.compile(r"min(?:imum)?\s+amount\s+due|minimum\s+due", re.I), "minimumDue"),
        (re.compile(r"previous\s+(?:statement\s+)?(?:balance|dues)|opening\s+balance", re.I), "previousBalance"),
        (re.compile(r"payments?\s*(?:,|/|and|&)?\s*(?:other\s+)?credits?", re.I), "paymentsCredits"),
        (re.compile(r"purchases?\s+made|purchases?\s*(?:/|and|&)?\s*(?:other\s+)?debits?|new\s+spends?", re.I), "purchases"),
        (re.compile(r"other\s+fees?\s*&?\s*charges?|finance\s+charges?|fee,?\s+taxes", re.I), "feesInterest"),
        (re.compile(r"credit\s+limit", re.I), "creditLimit"),
    )
    DATE_FIELDS = (
        (re.compile(r"payment\s+due\s+date|due\s+date", re.I), "dueDate"),
        (re.compile(r"statement\s+date|billing\s+date|bill(?:ing)?\s+date", re.I), "statementDate"),
    )
    PERIOD_RE = re.compile(
        r"(?:statement|billing|transactions?)\s*(?:details\s*)?(?:period|from)?\s*[:\s]\s*"
        r"(?P<start>\d{1,2}[\s/-][\w]{2,9}[,\s/-]+\d{2,4}|\d{2}[/-]\d{2}[/-]\d{2,4}|[A-Za-z]{3,9} \d{1,2},? \d{4})"
        r"\s*(?:to|-|–|—)\s*"
        r"(?P<end>\d{1,2}[\s/-][\w]{2,9}[,\s/-]+\d{2,4}|\d{2}[/-]\d{2}[/-]\d{2,4}|[A-Za-z]{3,9} \d{1,2},? \d{4})",
        re.I,
    )
    # How many following lines to search when a label carries no value itself.
    SUMMARY_LOOKAHEAD = 1

    # ---- rows ----
    SKIP_LINE = re.compile(
        r"^(page \d|date\s+(transaction|serno|description)|transaction\s+date|"
        r"domestic transactions|international transactions|reward points|"
        r"important (information|messages|notes)|terms and conditions)",
        re.I,
    )
    # Trailing chrome to strip before looking for the amount (HDFC's PI bullet).
    ROW_TRAILING_NOISE = None
    # Leading chrome before the date (ICICI's "100%" reward marker).
    ROW_LEADING_NOISE = None
    # Trailing column that is not part of the description (ICICI reward points).
    DESCRIPTION_TRAILING_NOISE = None
    # Marker tokens after the amount that mean "this is a credit".
    CREDIT_MARKERS = {"cr"}
    # Section headings that set the default type for the rows beneath them.
    SECTION_TYPES = ()
    # Known values of a category column printed just before the amount.
    CATEGORY_WORDS = ()
    # Statements that continue a description onto the next line.
    ALLOW_CONTINUATION = False
    # Rows that omit the date because they belong to the row above (SBI's IGST).
    ALLOW_DATELESS_ROWS = False
    # Leading column that is not part of the description (ICICI's serial number,
    # HDFC's timestamp).
    DESCRIPTION_LEADING_NOISE = None
    # Bounds of the transaction block. When START is set, only lines inside the
    # block are considered — essential for statements that print fee schedules
    # and product tables full of money elsewhere on the page.
    TXN_BLOCK_START = None
    TXN_BLOCK_END = None

    def __init__(self, card):
        self.card = card

    # ---------- public entry point ----------

    def parse(self, extraction, statement_month: str) -> ParsedStatement:
        lines = self.preprocess(list(extraction.lines))
        statement = ParsedStatement(
            statementMonth=statement_month, extractionMethod=extraction.method
        )
        self.parse_summary(lines, statement)
        statement.transactions = self.parse_transactions(lines, statement)
        self.finalise(statement)
        return statement

    def preprocess(self, lines: list) -> list:
        """Hook: clean raw extracted lines before anything else looks at them."""
        return lines

    # ---------- summary ----------

    def _money_after(self, text: str):
        match = MONEY_RE.search(text)
        return float(match.group(1).replace(",", "")) if match else None

    def parse_summary(self, lines, statement: ParsedStatement):
        for index, line in enumerate(lines):
            # Checked before the prose guard: the period pattern needs a label
            # and two dates, so it cannot be faked by surrounding text — and on
            # ICICI the period shares its line with marketing copy.
            if statement.statementPeriodStart is None:
                period = self.PERIOD_RE.search(line)
                if period:
                    start = normalize.parse_date(period.group("start"))
                    end = normalize.parse_date(period.group("end"))
                    if start and end:
                        statement.statementPeriodStart = start
                        statement.statementPeriodEnd = end

            if _PROSE.search(line):
                continue
            # A transaction row can contain summary wording — ICICI's "INFINITY
            # PAYMENT RECEIVED, THANK YOU ... 17,177.72 CR" would otherwise be
            # read as the statement's payments/credits total.
            if _LEADING_DATE.search(line):
                continue
            following = lines[index + 1: index + 1 + self.SUMMARY_LOOKAHEAD]

            for pattern, attr in self.DATE_FIELDS:
                if getattr(statement, attr) is not None:
                    continue
                match = pattern.search(line)
                if not match:
                    continue
                value = normalize.parse_date(line[match.end():])
                for nxt in following:
                    if value:
                        break
                    value = normalize.parse_date(nxt)
                if value:
                    setattr(statement, attr, value)

            for pattern, attr in self.SUMMARY_FIELDS:
                if getattr(statement, attr) is not None:
                    continue
                match = pattern.search(line)
                if not match:
                    continue
                # After the label first; only then the following line(s), which
                # is how ICICI and SBI print their headline figures.
                value = self._money_after(line[match.end():])
                for nxt in following:
                    if value is not None:
                        break
                    value = self._money_after(nxt)
                if value is not None:
                    setattr(statement, attr, value)

    # ---------- transactions ----------

    def parse_transactions(self, lines, statement: ParsedStatement) -> list:
        transactions = []
        section_type = None
        last_date = ""
        inside = self.TXN_BLOCK_START is None

        for line in lines:
            if not inside:
                if self.TXN_BLOCK_START.search(line):
                    inside = True
                continue
            if self.TXN_BLOCK_END is not None and self.TXN_BLOCK_END.search(line):
                inside = False
                continue

            heading = self.section_for(line)
            if heading is not None:
                section_type = heading
                continue

            txn = self.parse_row(line, section_type, last_date)
            if txn is not None:
                transactions.append(txn)
                last_date = txn.transactionDate
                continue

            if self.ALLOW_CONTINUATION and transactions:
                extra = self.continuation_text(line)
                if extra:
                    self.extend_description(transactions[-1], extra)

        return transactions

    def section_for(self, line: str):
        for pattern, txn_type in self.SECTION_TYPES:
            if pattern.search(line):
                return txn_type
        return None

    def continuation_text(self, line: str):
        """Wrapped remainder of the previous description, or None."""
        stripped = line.strip()
        if not stripped or len(stripped) > 60:
            return None
        if MONEY_RE.search(stripped) or _LEADING_DATE.search(stripped):
            return None
        if self.SKIP_LINE.search(stripped):
            return None
        # Wrapped fragments are compact and mostly upper-case or bracketed.
        if re.fullmatch(r"[A-Z0-9*<>/@.,()' &+-]{2,60}", stripped):
            return stripped
        return None

    def extend_description(self, txn: ParsedTransaction, extra: str):
        txn.description = f"{txn.description} {extra}".strip()
        txn.rawDescription = f"{txn.rawDescription} {extra}".strip()
        txn.merchant = normalize.normalize_merchant(txn.description)
        if not txn.isEMI and _EMI_WORDS.search(_emi_text(txn.description)):
            txn.transactionType = EMI
            txn.isEMI = True
        if txn.isEMI:
            self.attach_emi_details(txn)

    def clean_row(self, line: str) -> str:
        text = line.strip()
        if self.ROW_LEADING_NOISE:
            text = self.ROW_LEADING_NOISE.sub("", text, count=1).strip()
        if self.ROW_TRAILING_NOISE:
            text = self.ROW_TRAILING_NOISE.sub("", text).strip()
        return text

    def split_amount(self, text: str):
        """Return (before, amount, marker) for the row's trailing money column."""
        matches = list(MONEY_RE.finditer(text))
        if not matches:
            return None
        match = matches[-1]
        marker = text[match.end():].strip().strip(".").lower()
        # Only a short token can be a marker; anything longer is stray text and
        # means this money value was not the row's amount column.
        if len(marker) > 3:
            return None
        return text[: match.start()], float(match.group(1).replace(",", "")), marker

    def is_credit(self, before: str, marker: str) -> bool:
        return marker in self.CREDIT_MARKERS

    def split_category(self, description: str):
        """Pull a trailing category-column word out of the description."""
        for word in self.CATEGORY_WORDS:
            pattern = re.compile(rf"\s+{re.escape(word)}$", re.IGNORECASE)
            if pattern.search(description):
                return pattern.sub("", description).strip(), word
        return description, None

    def parse_row(self, line: str, section_type=None, last_date: str = ""):
        if not line or self.SKIP_LINE.search(line):
            return None
        text = self.clean_row(line)
        if not text:
            return None

        date_match = _LEADING_DATE.search(text)
        if date_match:
            txn_date = normalize.parse_date(date_match.group(1))
            if not txn_date:
                return None
            rest = text[date_match.end():]
        elif self.ALLOW_DATELESS_ROWS and last_date:
            # Sub-rows such as SBI's "IGST DB @ 18.00% 15.30 D" belong to the
            # transaction above them and would otherwise be dropped, leaving
            # the statement unreconcilable.
            txn_date = last_date
            rest = text
        else:
            return None

        split = self.split_amount(rest)
        if split is None:
            return None
        before, amount, marker = split
        if amount == 0:
            return None

        # A second date is the posting date on two-date layouts.
        posting_date = ""
        second = _LEADING_DATE.search(before.strip())
        if second:
            candidate = normalize.parse_date(second.group(1))
            if candidate:
                posting_date = candidate
                before = before.strip()[second.end():]

        description = re.sub(r"\s{2,}", " ", before).strip(" .|-")
        if self.DESCRIPTION_LEADING_NOISE:
            description = self.DESCRIPTION_LEADING_NOISE.sub("", description, count=1).strip()
        if self.DESCRIPTION_TRAILING_NOISE:
            description = self.DESCRIPTION_TRAILING_NOISE.sub("", description).strip()
        description, category = self.split_category(description)
        if not description:
            return None

        is_credit = self.is_credit(before, marker)
        txn_type = self.classify(description, is_credit, section_type)
        txn = ParsedTransaction(
            transactionDate=txn_date,
            postingDate=posting_date,
            description=description,
            rawDescription=line.strip(),
            merchant=normalize.normalize_merchant(description),
            amount=round(amount, 2),
            transactionType=txn_type,
            # Independent of type: an EMI's interest row is INTEREST *and* EMI.
            # A credit is excluded — an EMI conversion reversal is not an
            # instalment, and counting it would inflate the EMI total.
            isEMI=bool(_EMI_WORDS.search(_emi_text(description))) and not is_credit,
            category=category,
        )
        if txn.isEMI:
            self.attach_emi_details(txn)
        return txn

    def classify(self, description: str, is_credit: bool, section_type=None) -> str:
        """Map a row to a type. Sign is a hint; wording and section decide."""
        # A credit-marked row is never a charge, however it is worded. When a
        # purchase is converted to EMI mid-cycle the bank credits the whole
        # amount back ("EMI CONV … 45,100.00 Cr") and bills instalment one
        # instead; reading that as an EMI would add the sum twice over.
        if is_credit:
            if _REFUND_WORDS.search(description):
                return REFUND
            if _PAYMENT_WORDS.search(description):
                return PAYMENT
            return CREDIT

        if _EMI_WORDS.search(_emi_text(description)):
            # An EMI block bills principal, interest and tax as separate rows.
            # Typing them accurately is what lets fees/interest reconcile; the
            # isEMI flag (set separately) still groups them in the UI.
            if _INTEREST_WORDS.search(description):
                return INTEREST
            if _FEE_WORDS.search(description):
                return FEE
            return EMI
        if _PAYMENT_WORDS.search(description):
            return PAYMENT
        if _REFUND_WORDS.search(description):
            return REFUND
        if _INTEREST_WORDS.search(description):
            return INTEREST
        if _FEE_WORDS.search(description):
            return FEE
        if section_type:
            return section_type
        return PURCHASE

    def attach_emi_details(self, txn: ParsedTransaction):
        """Record instalment progress; bill only the current instalment.

        The billed amount is what the statement charges this cycle, so that is
        what monthly totals use — never the original purchase value.
        """
        txn.emiAmount = txn.amount
        match = _EMI_PROGRESS.search(txn.description)
        if match:
            number, tenure = int(match.group(1)), int(match.group(2))
            if 1 <= number <= tenure <= 60:
                txn.emiNumber = number
                txn.emiTenure = tenure

    # ---------- reconciliation ----------

    # Statements round to the paisa in places; sub-rupee drift is not an error.
    RECONCILE_TOLERANCE = 1.0

    def extracted_for_reconcile(self, statement: ParsedStatement) -> float:
        """Our equivalent of the statement's printed "purchases" figure.

        Default: purchases only. Banks that bucket other rows into that figure
        override this.
        """
        return round(
            sum(t.amount for t in statement.transactions if t.transactionType == PURCHASE), 2
        )

    def reconcile(self, statement: ParsedStatement):
        """Compare what we extracted against the statement's own total."""
        extracted = self.extracted_for_reconcile(statement)
        statement.extractedTotal = extracted
        if statement.purchases is None:
            # Nothing printed to check against — say so rather than implying a
            # clean match.
            statement.reconciliation = "UNKNOWN"
            return
        statement.expectedTotal = statement.purchases
        statement.difference = round(statement.purchases - extracted, 2)
        statement.reconciliation = (
            "RECONCILED"
            if abs(statement.difference) <= self.RECONCILE_TOLERANCE
            else "MISMATCH"
        )

    def finalise(self, statement: ParsedStatement):
        emis = [t for t in statement.transactions if t.isEMI]
        statement.emiTotal = round(sum(t.amount for t in emis), 2) if emis else 0.0
        # Reconcile before any fallback, so a computed figure is never compared
        # against itself.
        self.reconcile(statement)
        if statement.purchases is None:
            spend = [t for t in statement.transactions if t.transactionType in SPEND_TYPES]
            if spend:
                statement.purchases = round(sum(t.amount for t in spend), 2)
        if not statement.transactions:
            statement.warnings.append(
                "No transaction rows were recognised in this statement. The "
                f"'{self.key}' parser may need tuning for this layout — see "
                "GET /credit-cards/debug/extract."
            )
