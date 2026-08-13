"""Parse free-form / dictated text like "450 Groceries" into an expense record."""

import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

# Mirror of add_expense_front_end/src/constants.js
CATEGORIES = [
    "Groceries",
    "Food & Dining",
    "Transport",
    "Bills & Utilities",
    "Shopping",
    "Health",
    "Entertainment",
    "Miscellaneous",
]
DEFAULT_CATEGORY = "Miscellaneous"

# How the money was paid, kept separate from what it bought. A card spend is
# still spending, but it does not move the bank balance until the bill is paid
# — see add_expense_front_end/src/utils/accounting.js.
ACCOUNTS = ["cash", "hdfc", "sbi", "icici", "kotak"]
DEFAULT_ACCOUNT = "cash"

# Spoken back in the confirmation, matching the `short` names in the front end.
ACCOUNT_LABELS = {
    "cash": "Bank",
    "hdfc": "HDFC",
    "sbi": "SBI",
    "icici": "ICICI",
    "kotak": "Kotak",
}

EXPENSE = "expense"
INCOME = "income"
CARD_PAYMENT = "cardPayment"

# Spoken names for an account. Removed from the note once matched: "hdfc" says
# how you paid, not what you bought. Dictation is inconsistent about bank names,
# so the obvious mishearings are listed too.
ACCOUNT_ALIASES = {
    "cash": "cash",
    "bank": "cash",
    "upi": "cash",
    "gpay": "cash",
    "google pay": "cash",
    "phonepe": "cash",
    "paytm": "cash",
    "debit card": "cash",
    "hdfc": "hdfc",
    "hdfc bank": "hdfc",
    "sbi": "sbi",
    "s b i": "sbi",
    "state bank": "sbi",
    "icici": "icici",
    "icic": "icici",
    "icici bank": "icici",
    "kotak": "kotak",
    "kotak bank": "kotak",
}

# Spoken names for a category itself, e.g. "450 food and dining". Matched
# longest-first, so "food and dining" wins over the bare word "food".
CATEGORY_ALIASES = {
    "groceries": "Groceries",
    "grocery": "Groceries",
    "grocerys": "Groceries",
    "grocerie": "Groceries",
    "food and dining": "Food & Dining",
    "food & dining": "Food & Dining",
    "food and drink": "Food & Dining",
    "dining": "Food & Dining",
    "transport": "Transport",
    "transportation": "Transport",
    "travel": "Transport",
    "bills and utilities": "Bills & Utilities",
    "bills & utilities": "Bills & Utilities",
    "utilities": "Bills & Utilities",
    "utility": "Bills & Utilities",
    "shopping": "Shopping",
    "health": "Health",
    "healthcare": "Health",
    "medical": "Health",
    "entertainment": "Entertainment",
    "misc": "Miscellaneous",
    "miscellaneous": "Miscellaneous",
    "other": "Miscellaneous",
    "others": "Miscellaneous",
}
for _c in CATEGORIES:
    CATEGORY_ALIASES.setdefault(_c.lower(), _c)

# Item words that imply a category when no category name is spoken, e.g.
# "400 fish" -> Groceries. Unlike CATEGORY_ALIASES these are kept in the note,
# since "fish" is what you actually bought. Multi-word entries are allowed and
# beat shorter overlapping ones, so "movie ticket" reaches Entertainment rather
# than stopping at Transport's "ticket". Anything unmatched falls back to
# DEFAULT_CATEGORY. Extend with EXPENSE_CATEGORY_KEYWORDS (see _load_extra).
CATEGORY_KEYWORDS = {
    # Raw ingredients and the shops that sell them. Anything eaten out belongs
    # under Food & Dining instead.
    "Groceries": {
        "fish", "milk", "supermarket", "super market", "market", "vegetable",
        "vegetables", "veggies", "fruit", "fruits", "egg", "eggs", "meat",
        "chicken", "mutton", "beef", "pork", "prawn", "prawns", "bread",
        "rice", "wheat", "atta", "oil", "sugar", "salt", "masala", "spices",
        "dal", "pulses", "onion", "onions", "potato", "potatoes", "tomato",
        "tomatoes", "curd", "yogurt", "butter", "cheese", "paneer", "biscuit",
        "biscuits", "water", "provisions", "provision store", "grocer",
        "grocery store", "dmart", "d mart", "bigbasket", "big basket",
        "blinkit", "zepto", "instamart", "ration", "detergent", "soap",
        "shampoo", "toothpaste",
    },
    "Food & Dining": {
        "food", "foods", "snack", "snacks", "restaurant", "restaurants",
        "hotel", "cafe", "coffee", "tea", "chai", "juice", "breakfast",
        "lunch", "dinner", "brunch", "meal", "meals", "swiggy", "zomato",
        "pizza", "burger", "biryani", "dosa", "idli", "sandwich", "shawarma",
        "momos", "parotta", "porotta", "bakery", "cake", "sweets", "mithai",
        "dessert", "ice cream", "icecream", "canteen", "mess", "tiffin",
        "takeaway", "takeout", "buffet", "eatery", "kfc", "mcdonalds",
        "dominos", "starbucks", "barista", "beer", "wine", "drinks",
    },
    "Transport": {
        "taxi", "cab", "uber", "ola", "rapido", "auto", "autorickshaw",
        "rickshaw", "bus", "train", "metro", "railway", "irctc", "flight",
        "flights", "airfare", "petrol", "diesel", "fuel", "parking", "toll",
        "fastag", "ticket", "tickets", "commute", "bike service",
        "car service", "cycle",
    },
    "Bills & Utilities": {
        "electricity", "current bill", "power bill", "water bill", "gas",
        "cylinder", "lpg", "internet", "broadband", "wifi", "jio", "airtel",
        "vodafone", "bsnl", "recharge", "postpaid", "prepaid", "dth", "cable",
        "tata sky", "rent", "maintenance", "insurance", "premium",
        "subscription", "tax", "property tax", "emi", "loan", "fees", "fee",
        "tuition", "school fees", "salary paid", "wifi bill",
    },
    "Shopping": {
        "shopping", "clothes", "clothing", "dress", "shirt", "tshirt",
        "t shirt", "pants", "jeans", "saree", "kurta", "shoes", "shoe",
        "sandals", "chappal", "footwear", "bag", "watch", "amazon",
        "flipkart", "myntra", "ajio", "meesho", "nykaa", "mall", "electronics",
        "laptop", "headphones", "furniture", "gift", "gifts", "cosmetics",
        "jewellery", "jewelry", "gold", "book", "books", "stationery",
        "mobile phone", "new phone",
    },
    "Health": {
        "hospital", "doctor", "clinic", "medicine", "medicines", "pharmacy",
        "chemist", "medicals", "apollo", "tablets", "lab", "lab test",
        "blood test", "scan", "xray", "x ray", "dental", "dentist", "surgery",
        "checkup", "check up", "consultation", "physio", "therapy", "vaccine",
        "injection", "health insurance", "optician", "spectacles", "gym",
        "yoga", "fitness",
    },
    "Entertainment": {
        "movie", "movies", "movie ticket", "cinema", "theatre", "theater",
        "pvr", "inox", "imax", "netflix", "hotstar", "spotify", "prime video",
        "game", "games", "gaming", "concert", "event", "party", "club", "pub",
        "bar", "outing", "picnic", "amusement", "bowling", "museum", "zoo",
        "trip", "resort",
    },
}

# "bill" on its own is too vague to route anywhere. Paired with a card it means
# a statement payment; paired with nothing else it is an ordinary utility bill.
BILL_WORDS = {"bill", "bills", "statement", "outstanding", "dues"}

INCOME_WORDS = {"income", "salary", "credited", "credit", "received", "refund"}

# Words that carry no meaning once amount and category are extracted.
FILLER_WORDS = {
    "rs", "rs.", "inr", "rupee", "rupees", "add", "added", "spent", "spend",
    "paid", "pay", "expense", "for", "on", "in", "to", "of", "at", "the", "a",
    "an", "and", "my", "was", "is", "using", "with", "via", "by", "from",
}

AMOUNT_RE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)")


def _load_extra() -> dict:
    """Keywords added through the environment, no redeploy needed.

    EXPENSE_CATEGORY_KEYWORDS, e.g.
    {"Groceries": ["ghee", "jam"], "Health": ["ayurveda"]}
    """
    raw = os.environ.get("EXPENSE_CATEGORY_KEYWORDS")
    if not raw:
        return {}
    try:
        return {
            str(word).lower(): category
            for category, words in json.loads(raw).items()
            for word in words
        }
    except (ValueError, AttributeError, TypeError):
        return {}  # Bad config should not break expense entry.


def _build_keyword_index() -> dict:
    index = {}
    for category, words in CATEGORY_KEYWORDS.items():
        for word in words:
            index[word] = category
    index.update(_load_extra())
    return index


def _phrase_re(phrase: str) -> re.Pattern:
    """Match a phrase on word boundaries, tolerating extra spaces between words."""
    body = r"\s+".join(re.escape(part) for part in phrase.split())
    return re.compile(r"(?<!\w)" + body + r"(?!\w)", re.IGNORECASE)


def _longest_first(phrases):
    """Longer phrases first so "food and dining" beats "food"; then alphabetical
    so two phrases of equal length always resolve the same way."""
    return sorted(phrases, key=lambda p: (-len(p), p))


def _take(text: str, aliases: dict):
    """Pull the first matching alias out of `text`.

    Returns (value, text with the matched words removed). Used for words that
    name the category or the account outright — they describe the entry rather
    than being part of it, so they should not survive into the note.
    """
    for phrase in _longest_first(aliases):
        match = _phrase_re(phrase).search(text)
        if match:
            return aliases[phrase], text[: match.start()] + " " + text[match.end():]
    return None, text


def _keyword_category(text: str):
    """Infer a category from item words. Returns None when nothing matches."""
    index = _build_keyword_index()
    for phrase in _longest_first(index):
        if _phrase_re(phrase).search(text):
            return index[phrase]
    # "vegetables" -> "vegetable", for plurals not listed outright.
    for word in re.findall(r"\w+", text.lower()):
        if len(word) > 3 and word.endswith("s") and word[:-1] in index:
            return index[word[:-1]]
    return None


def describe(record: dict) -> str:
    """One short line, spoken back by the iPhone shortcut.

    Says the account out loud whenever it is not cash, so a card spend landing
    on the wrong card is caught while you can still fix it.
    """
    amount = record["amount"]
    account = record.get("account", DEFAULT_ACCOUNT)
    label = ACCOUNT_LABELS.get(account, account.upper())
    if record["type"] == CARD_PAYMENT:
        return f"Paid {amount} to {label}"
    if record["type"] == INCOME:
        return f"Added {amount} as income"
    if account != DEFAULT_ACCOUNT:
        return f"Added {amount} to {record['category']} on {label}"
    return f"Added {amount} to {record['category']}"


def _to_date_string(tz_name: str) -> str:
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.now()
    return now.strftime("%Y-%m-%d")


def parse_expense_text(text: str, tz_name: str = "Asia/Kolkata") -> dict:
    """Return a record matching the shape the front end writes to Firebase.

    Raises ValueError when no amount can be found.
    """
    if not text or not text.strip():
        raise ValueError("Empty text")

    raw = text.strip()
    # Strip currency symbols so "₹450" and "$450" still yield a clean amount.
    cleaned = re.sub(r"[₹$]", " ", raw)

    match = AMOUNT_RE.search(cleaned)
    if not match:
        raise ValueError(f"No amount found in {raw!r}")

    amount = float(match.group(1).replace(",", ""))
    if amount <= 0:
        raise ValueError(f"Amount must be positive, got {amount}")

    # Everything except the amount token becomes candidate category/note words.
    remainder = cleaned[: match.start()] + " " + cleaned[match.end():]

    account, remainder = _take(remainder, ACCOUNT_ALIASES)
    category, remainder = _take(remainder, CATEGORY_ALIASES)

    # No category was named outright, so guess one from the item words. These
    # stay in the note — "hospital" is what the money went on.
    if category is None:
        category = _keyword_category(remainder)

    words = {w.strip(".!?").lower() for w in re.split(r"[\s,]+", remainder) if w}

    if words & INCOME_WORDS:
        entry_type = INCOME
        account = DEFAULT_ACCOUNT  # income lands in the bank, never on a card
    elif words & BILL_WORDS and account and account != DEFAULT_ACCOUNT and category is None:
        # "16500 hdfc bill" settles a card: cash out, outstanding down, and no
        # new spending, since the swipes behind it were counted when they were
        # made. A category anywhere in the text means it is an ordinary bill
        # that happens to be paid by card ("2000 hdfc electricity bill").
        entry_type = CARD_PAYMENT
    else:
        entry_type = EXPENSE
        if category is None and words & BILL_WORDS:
            category = "Bills & Utilities"

    note = " ".join(
        w for w in re.split(r"[\s,]+", remainder)
        if w and w.strip(".!?").lower() not in FILLER_WORDS
    ).strip()

    # Amount is stored as int when whole, matching what the form produces.
    return {
        "amount": int(amount) if amount.is_integer() else amount,
        "category": "" if entry_type == CARD_PAYMENT else (category or DEFAULT_CATEGORY),
        "account": account or DEFAULT_ACCOUNT,
        "type": entry_type,
        "date": _to_date_string(tz_name),
        "note": note,
    }
