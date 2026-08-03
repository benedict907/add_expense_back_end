"""Parse free-form / dictated text like "450 Groceries" into an expense record."""

import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

# Mirror of add_expense_front_end/src/constants.js
CATEGORIES = ["Groceries", "HDFC", "SBI", "ICIC", "KOTAK", "Miscellaneous"]
DEFAULT_CATEGORY = "Miscellaneous"

# Spoken/dictated variants -> canonical category. Voice dictation is inconsistent
# about bank names ("icici", "eye see eye see eye") and plurals.
CATEGORY_ALIASES = {
    "grocery": "Groceries",
    "groceries": "Groceries",
    "grocerys": "Groceries",
    "grocerie": "Groceries",
    "food": "Groceries",
    "hdfc": "HDFC",
    "sbi": "SBI",
    "icici": "ICIC",
    "icic": "ICIC",
    "kotak": "KOTAK",
    "misc": "Miscellaneous",
    "miscellaneous": "Miscellaneous",
    "other": "Miscellaneous",
    "others": "Miscellaneous",
}
for _c in CATEGORIES:
    CATEGORY_ALIASES.setdefault(_c.lower(), _c)

# Item words that imply a category when no category name is spoken, e.g.
# "400 fish" -> Groceries. Unlike CATEGORY_ALIASES these are kept in the note,
# since "fish" is what you actually bought. Anything unmatched falls back to
# DEFAULT_CATEGORY. Extend with EXPENSE_CATEGORY_KEYWORDS (see _load_extra_keywords).
CATEGORY_KEYWORDS = {
    "Groceries": {
        "fish", "milk", "supermarket", "market", "vegetable", "vegetables",
        "veggies", "fruit", "fruits", "egg", "eggs", "meat", "chicken",
        "mutton", "beef", "pork", "prawn", "prawns", "bread", "rice", "wheat",
        "atta", "oil", "sugar", "salt", "masala", "spices", "dal", "pulses",
        "onion", "onions", "potato", "potatoes", "tomato", "tomatoes",
        "curd", "yogurt", "butter", "cheese", "paneer", "snacks", "biscuit",
        "biscuits", "tea", "coffee", "juice", "water", "provisions",
        "dmart", "bigbasket", "blinkit", "zepto", "instamart", "grocer",
    },
}

# Multi-word hints matched against the whole remaining text.
CATEGORY_PHRASES = {
    "super market": "Groceries",
    "grocery store": "Groceries",
    "big basket": "Groceries",
    "d mart": "Groceries",
    "provision store": "Groceries",
}

INCOME_WORDS = {"income", "salary", "credited", "credit", "received", "refund"}

# Words that carry no meaning once amount and category are extracted.
FILLER_WORDS = {
    "rs", "rs.", "inr", "rupee", "rupees", "add", "added", "spent", "spend",
    "paid", "pay", "expense", "for", "on", "in", "to", "of", "at", "the", "a",
    "an", "and", "my", "was", "is",
}

AMOUNT_RE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)")


def _build_keyword_index() -> dict:
    """Flatten CATEGORY_KEYWORDS, plus any extras from the environment.

    EXPENSE_CATEGORY_KEYWORDS lets you add keywords without a redeploy, e.g.
    {"Groceries": ["ghee", "jam"], "KOTAK": ["emi"]}
    """
    index = {}
    for category, words in CATEGORY_KEYWORDS.items():
        for word in words:
            index[word] = category
    raw = os.environ.get("EXPENSE_CATEGORY_KEYWORDS")
    if raw:
        try:
            for category, words in json.loads(raw).items():
                for word in words:
                    index[str(word).lower()] = category
        except (ValueError, AttributeError, TypeError):
            pass  # Bad config should not break expense entry.
    return index


def _keyword_category(words, full_text: str):
    """Infer a category from item words. Returns None when nothing matches."""
    index = _build_keyword_index()
    for word in words:
        key = word.strip(".!?").lower()
        if key in index:
            return index[key]
        # "vegetables" -> "vegetable"; guard against mangling short words.
        if len(key) > 3 and key.endswith("s") and key[:-1] in index:
            return index[key[:-1]]
    for phrase, category in CATEGORY_PHRASES.items():
        if phrase in full_text:
            return category
    return None


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
    words = [w for w in re.split(r"[\s,]+", remainder) if w]

    category = None
    note_words = []
    for word in words:
        key = word.strip(".!?").lower()
        if category is None and key in CATEGORY_ALIASES:
            category = CATEGORY_ALIASES[key]
            continue
        note_words.append(word)

    # No category was named outright, so guess one from the item words.
    if category is None:
        category = _keyword_category(words, remainder.lower())

    lowered = {w.strip(".!?").lower() for w in words}
    entry_type = "income" if lowered & INCOME_WORDS else "expense"

    note = " ".join(
        w for w in note_words if w.strip(".!?").lower() not in FILLER_WORDS
    ).strip()

    # Amount is stored as int when whole, matching what the form produces.
    return {
        "amount": int(amount) if amount.is_integer() else amount,
        "category": category or DEFAULT_CATEGORY,
        "type": entry_type,
        "date": _to_date_string(tz_name),
        "note": note,
    }
