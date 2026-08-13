"""Tests for dictated-expense parsing.

No network, no Firebase — this is pure text handling, which is where the
correctness risk lives. Run with:

    python tests/test_expense_text.py
    (or: python -m pytest tests/test_expense_text.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from expense_text import describe, parse_expense_text  # noqa: E402


def parse(text):
    return parse_expense_text(text, tz_name="Asia/Kolkata")


def test_amount_and_default_category():
    record = parse("450")
    assert record["amount"] == 450
    assert record["category"] == "Miscellaneous"
    assert record["account"] == "cash"
    assert record["type"] == "expense"


def test_unrecognised_words_fall_back_to_miscellaneous():
    record = parse("300 qwerty widget")
    assert record["category"] == "Miscellaneous"
    assert record["note"] == "qwerty widget"


def test_food_goes_to_food_and_dining():
    # The reported bug: "food" and "snacks" were landing in Groceries.
    assert parse("250 food")["category"] == "Food & Dining"
    assert parse("120 snacks")["category"] == "Food & Dining"


def test_hospital_goes_to_health():
    assert parse("500 hospital")["category"] == "Health"


def test_each_category_has_a_reachable_keyword():
    cases = {
        "200 vegetables": "Groceries",
        "350 restaurant": "Food & Dining",
        "80 auto": "Transport",
        "1200 electricity": "Bills & Utilities",
        "2000 shoes": "Shopping",
        "600 medicine": "Health",
        "400 movie": "Entertainment",
    }
    for text, expected in cases.items():
        assert parse(text)["category"] == expected, f"{text} -> {parse(text)['category']}"


def test_category_can_be_named_outright():
    assert parse("450 groceries")["category"] == "Groceries"
    assert parse("450 food and dining")["category"] == "Food & Dining"
    assert parse("450 bills and utilities")["category"] == "Bills & Utilities"


def test_named_category_is_dropped_from_the_note():
    # "groceries" says which bucket, it is not a thing you bought.
    assert parse("450 groceries")["note"] == ""
    # An item word is what you bought, so it stays.
    assert parse("450 fish")["note"] == "fish"


def test_longer_phrase_wins_over_shorter_one():
    # "food and dining" must not stop at "food"; both reach the same place
    # here, but "movie ticket" would otherwise be caught by Transport's
    # "ticket" and "health insurance" by Bills & Utilities' "insurance".
    assert parse("400 movie ticket")["category"] == "Entertainment"
    assert parse("9000 health insurance")["category"] == "Health"


def test_card_name_sets_the_account_not_the_category():
    record = parse("500 HDFC")
    assert record["account"] == "hdfc"
    assert record["category"] == "Miscellaneous"
    assert record["type"] == "expense"
    assert record["note"] == ""


def test_card_and_category_can_both_be_spoken():
    record = parse("1200 hospital hdfc")
    assert record["account"] == "hdfc"
    assert record["category"] == "Health"
    assert record["note"] == "hospital"


def test_misheard_bank_names_still_land():
    assert parse("500 icic")["account"] == "icici"
    assert parse("500 icici")["account"] == "icici"
    assert parse("500 state bank")["account"] == "sbi"


def test_upi_apps_count_as_cash():
    assert parse("300 gpay vegetables")["account"] == "cash"


def test_card_bill_is_a_transfer_not_spending():
    record = parse("16500 hdfc bill")
    assert record["type"] == "cardPayment"
    assert record["account"] == "hdfc"
    # A transfer belongs to no category — it is not spending.
    assert record["category"] == ""


def test_a_utility_bill_paid_by_card_is_still_spending():
    # The card is how it was paid; electricity is what it was for.
    record = parse("2000 hdfc electricity bill")
    assert record["type"] == "expense"
    assert record["account"] == "hdfc"
    assert record["category"] == "Bills & Utilities"


def test_bare_bill_without_a_card_is_a_utility():
    record = parse("1200 bill")
    assert record["type"] == "expense"
    assert record["category"] == "Bills & Utilities"


def test_income_is_detected_and_lands_in_the_bank():
    record = parse("50000 salary credited")
    assert record["type"] == "income"
    assert record["account"] == "cash"


def test_currency_symbols_and_separators():
    assert parse("₹1,250 groceries")["amount"] == 1250
    assert parse("99.5 snacks")["amount"] == 99.5


def test_filler_words_are_stripped_from_the_note():
    record = parse("add 450 for fish at the market")
    assert record["amount"] == 450
    assert record["category"] == "Groceries"
    assert record["note"] == "fish market"


def test_plural_falls_back_to_singular_keyword():
    assert parse("300 prawns")["category"] == "Groceries"


def test_no_amount_raises():
    for text in ("", "   ", "groceries"):
        try:
            parse(text)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {text!r}")


def test_zero_and_negative_amounts_rejected():
    try:
        parse("0 groceries")
    except ValueError:
        return
    raise AssertionError("expected ValueError for a zero amount")


def test_env_keywords_extend_the_index():
    os.environ["EXPENSE_CATEGORY_KEYWORDS"] = '{"Health": ["ayurveda"]}'
    try:
        assert parse("700 ayurveda")["category"] == "Health"
    finally:
        del os.environ["EXPENSE_CATEGORY_KEYWORDS"]


def test_bad_env_keywords_do_not_break_parsing():
    os.environ["EXPENSE_CATEGORY_KEYWORDS"] = "not json"
    try:
        assert parse("250 food")["category"] == "Food & Dining"
    finally:
        del os.environ["EXPENSE_CATEGORY_KEYWORDS"]


def test_spoken_confirmation_names_the_card():
    # The shortcut reads this back, so a spend landing on the wrong card should
    # be audible while it is still easy to fix.
    assert describe(parse("450 groceries")) == "Added 450 to Groceries"
    assert describe(parse("450 groceries hdfc")) == "Added 450 to Groceries on HDFC"
    assert describe(parse("16500 hdfc bill")) == "Paid 16500 to HDFC"
    assert describe(parse("50000 salary credited")) == "Added 50000 as income"


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL {test.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERR  {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
