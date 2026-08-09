"""Shared text/amount normalisation and the fingerprints that prevent duplicates.

The fingerprints here are the whole duplicate-detection strategy: statement and
transaction ids are derived from content, so a re-sync writes to the same keys
instead of pushing new ones.
"""

import hashlib
import re
from datetime import date, datetime

# Payment-network and gateway noise that appears in front of the real merchant.
_PREFIX_NOISE = (
    "upi/", "pos ", "pos/", "ecom ", "ecom/", "imps/", "neft/", "atw/", "ath/",
    "vps*", "razorpay*", "razp*", "rzp*", "payu*", "ccavenue*", "bharatpe*",
    "paytm*", "phonepe*", "gpay*", "billdesk*", "instamojo*",
)

# Trailing city/state/country tokens common on Indian statements.
_SUFFIX_NOISE = re.compile(
    r"\b(in|ind|india|bangalore|bengaluru|mumbai|delhi|new delhi|chennai|"
    r"hyderabad|pune|kolkata|kochi|cochin|ernakulam|kerala|karnataka|"
    r"maharashtra|tamil nadu|telangana|gurgaon|gurugram|noida|sgp|singapore|"
    r"us|usa|gb|nl|ie)\b\s*$",
    re.IGNORECASE,
)

_MULTISPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
# Long digit runs are transaction references, not part of the merchant name.
_LONG_DIGITS = re.compile(r"\b\d{4,}\b")
_TRAILING_DIGITS = re.compile(r"\s+\d{1,3}\s*$")

AMOUNT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")

DATE_PATTERNS = (
    ("%d/%m/%Y", re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")),
    ("%d-%m-%Y", re.compile(r"\b(\d{2}-\d{2}-\d{4})\b")),
    ("%d/%m/%y", re.compile(r"\b(\d{2}/\d{2}/\d{2})\b")),
    ("%d-%m-%y", re.compile(r"\b(\d{2}-\d{2}-\d{2})\b")),
    ("%d %b %Y", re.compile(r"\b(\d{1,2} [A-Za-z]{3} \d{4})\b")),
    ("%d %b, %Y", re.compile(r"\b(\d{1,2} [A-Za-z]{3}, \d{4})\b")),
    ("%d-%b-%Y", re.compile(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b")),
    ("%d-%b-%y", re.compile(r"\b(\d{1,2}-[A-Za-z]{3}-\d{2})\b")),
    # Full month names: ICICI prints "June 13, 2026".
    ("%B %d, %Y", re.compile(r"\b([A-Za-z]{4,9} \d{1,2}, \d{4})\b")),
    ("%b %d, %Y", re.compile(r"\b([A-Za-z]{3} \d{1,2}, \d{4})\b")),
    ("%B %d %Y", re.compile(r"\b([A-Za-z]{4,9} \d{1,2} \d{4})\b")),
    # Two-digit years last, and only when no third digit follows, so "07 Jul
    # 2026" is never truncated to "07 Jul 20".
    ("%d %b %y", re.compile(r"\b(\d{1,2} [A-Za-z]{3} \d{2})(?!\d)")),
)


def parse_amount(text: str):
    """First money-looking number in `text`, or None."""
    if not text:
        return None
    match = AMOUNT_RE.search(text.replace("₹", " ").replace("Rs.", " "))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_date(text: str, default_year: int = None):
    """Return an ISO date string for the first date found, else None."""
    if not text:
        return None
    for fmt, pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            parsed = datetime.strptime(match.group(1), fmt).date()
        except ValueError:
            continue
        # Two-digit years land in 19xx for pre-2069 values; nudge to this century.
        if parsed.year < 2000:
            parsed = parsed.replace(year=parsed.year + 100)
        return parsed.isoformat()
    return None


def normalize_merchant(description: str) -> str:
    """Collapse a raw statement line into a stable merchant key.

    "UPI/AMAZON PAY INDIA 4408123 BANGALORE IN" -> "amazon pay india"
    This key is what owner rules are stored against, so it must stay stable
    month to month while ignoring reference numbers and city suffixes.
    """
    if not description:
        return ""
    text = description.lower().strip()
    for noise in _PREFIX_NOISE:
        if text.startswith(noise):
            text = text[len(noise):]
    # Punctuation first, so "1,25668" becomes one digit run rather than two
    # tokens where only the long half gets stripped.
    text = _NON_ALNUM.sub(" ", text)
    text = _LONG_DIGITS.sub(" ", text)
    text = _MULTISPACE.sub(" ", text).strip()
    previous = None
    while previous != text:
        previous = text
        text = _SUFFIX_NOISE.sub("", text).strip()
        # Leftover reference fragments trail the name; digits inside a name
        # ("7 eleven", "24 seven") are kept because only the tail is trimmed.
        text = _TRAILING_DIGITS.sub("", text).strip()
    return text[:64]


def month_key(value) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value).date()
    if isinstance(value, datetime):
        value = value.date()
    return f"{value.year:04d}-{value.month:02d}"


def current_month_key(tz_name: str = "Asia/Kolkata") -> str:
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.now()
    return month_key(now.date())


def statement_id(card_id: str, statement_month: str) -> str:
    """card + period is the statement's identity, per the dedupe requirement."""
    safe_card = re.sub(r"[.#$/\[\]]", "_", card_id)
    return f"{safe_card}_{statement_month}"


def transaction_id(
    card_id: str,
    statement_month: str,
    txn_date: str,
    amount: float,
    normalized_merchant: str,
    txn_type: str,
) -> str:
    """Content fingerprint. Same statement re-synced -> same key -> no duplicate.

    A short hash suffix keeps two same-day, same-amount, same-merchant charges
    distinct only if something else differs; identical ones collapse, which is
    the intended behaviour for a re-import.
    """
    raw = "|".join(
        [
            card_id,
            statement_month,
            txn_date or "",
            f"{round(float(amount), 2):.2f}",
            normalized_merchant,
            txn_type,
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def today_iso() -> str:
    return date.today().isoformat()
