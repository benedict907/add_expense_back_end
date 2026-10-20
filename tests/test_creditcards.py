"""Tests for the credit-card parsing, fingerprinting and owner-matching logic.

No network, no Firebase, no PDFs — these cover the pure logic, which is where
the correctness risk actually lives. Run with:

    python tests/test_creditcards.py
    (or: python -m pytest tests/test_creditcards.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creditcards import normalize, owner_match, parsers  # noqa: E402
from creditcards.config import CardConfig  # noqa: E402
from creditcards.extract import Extraction  # noqa: E402

CARD = CardConfig(
    id="kotak",
    cardName="Kotak Cashback+",
    bankName="Kotak Mahindra Bank",
    lastFourDigits="5678",
    parser="kotak",
)

# Mirrors the real Kotak layout — summary block, a bounded transaction block,
# section headings, a Spends Category column, `Cr` credit markers and an
# EMI & Loans section. Merchants and figures are invented, but the arithmetic
# is internally consistent so reconciliation is genuinely exercised:
#   purchases line = purchases + EMI - surcharge waivers
#                  = (1,000.00 + 2,000.50) + (1,250.00 + 500.00) - 25.50
#                  = 4,725.00
STATEMENT_LINES = [
    "Benedict T Monthly statement for your Cashback+ Credit Card X5678",
    "Credit limit ₹ 1,26,000.00",
    "Billing Date 20-Jul-2026",
    "Total Amount Due (TAD) ₹ 4,825.00",
    "Minimum Amount Due (MAD) ₹ 500.00",
    "Due Date 07-Aug-2026",
    "Details of Total Amount Due (in ₹)",
    "Previous statement dues 5,000.00",
    "Purchases made in this cycle 4,725.00",
    "Other fees & charges 100.00",
    "Payments and Other Credits 5,000.00",
    "Total Amount Due 4,825.00",
    "Rewards Summary",
    "Opening balance Points earned Points redeemed",
    "Transactions Details from 21-Jun-2026 to 20-Jul-2026",
    "Date Description Spends Category Amount (₹)",
    "Payments and Other Credits",
    "01-Jul-2026 PAYMENT RECEIVED-ONLINE FUNDS TRANSFER 5,000.00 Cr",
    "Purchases made in this cycle - Primary Card X5678",
    "04-Jul-2026 EXAMPLE STORE COCHIN IN Grocery 1,000.00",
    "07-Jul-2026 EXAMPLE FUELS COCHIN IN Fuel 2,000.50",
    "11-Jul-2026 FUEL SURCHARGE WAIVER 25.50 Cr",
    "16-Jul-2026 EXAMPLE UTILITY GURGAON IN (Convert To EMI) Utilities 1,250.00",
    "EMI & Loans",
    "11-Jul-2026 EMI PRIN FOR EXAMPLE DEVICE (003/020) 400.00",
    "11-Jul-2026 EMI INT-EXAMPLE DEVICE (003/020) 100.00",
    "18-Jul-2026 GST 100.00",
    "Page 2 of 4",
    "Reward Points 1250",
]


def _parse():
    parser = parsers.get_parser(CARD)
    extraction = Extraction("text", STATEMENT_LINES, [], 4)
    return parser.parse(extraction, "2026-07")


def test_summary_fields():
    s = _parse()
    assert s.statementDate == "2026-07-20", s.statementDate
    assert s.dueDate == "2026-08-07", s.dueDate
    assert s.statementPeriodStart == "2026-06-21", s.statementPeriodStart
    assert s.statementPeriodEnd == "2026-07-20", s.statementPeriodEnd
    assert s.totalAmount == 4825.00, s.totalAmount
    assert s.minimumDue == 500.00, s.minimumDue
    assert s.previousBalance == 5000.00, s.previousBalance
    assert s.paymentsCredits == 5000.00, s.paymentsCredits
    assert s.purchases == 4725.00, s.purchases
    assert s.creditLimit == 126000.00, s.creditLimit


def test_transaction_rows_and_noise():
    s = _parse()
    descriptions = [t.description for t in s.transactions]
    assert len(s.transactions) == 8, descriptions
    # Chrome inside and outside the transaction block never becomes a row.
    assert not any("Page 2" in d or "Reward Points" in d for d in descriptions)
    assert not any("Previous statement dues" in d for d in descriptions)


def test_category_column_is_captured():
    s = _parse()
    by_desc = {t.description: t for t in s.transactions}
    assert by_desc["EXAMPLE STORE COCHIN IN"].category == "Grocery"
    assert by_desc["EXAMPLE FUELS COCHIN IN"].category == "Fuel"
    # The category word is stripped out of the description itself.
    assert "Grocery" not in by_desc["EXAMPLE STORE COCHIN IN"].description


def test_types_are_not_inferred_from_sign_alone():
    s = _parse()
    by_desc = {t.description: t for t in s.transactions}

    store = by_desc["EXAMPLE STORE COCHIN IN"]
    assert store.transactionType == parsers.PURCHASE
    assert store.amount == 1000.00

    # Both are marked "Cr", but they are not the same kind of thing.
    payment = next(t for d, t in by_desc.items() if "PAYMENT RECEIVED" in d)
    assert payment.transactionType == parsers.PAYMENT
    waiver = next(t for d, t in by_desc.items() if "SURCHARGE WAIVER" in d)
    assert waiver.transactionType == parsers.CREDIT

    assert by_desc["GST"].transactionType == parsers.FEE


def test_convert_to_emi_is_an_offer_not_an_emi():
    """"(Convert To EMI)" marks a purchase as *eligible* for conversion.

    The row is still an ordinary purchase billed in full this cycle; the real
    instalments are the separate EMI PRIN / EMI INT rows.
    """
    s = _parse()
    offer = next(t for t in s.transactions if "Convert To EMI" in t.description)
    assert offer.transactionType == parsers.PURCHASE
    assert offer.isEMI is False
    assert offer.emiAmount is None


def test_emi_uses_billed_instalment_not_purchase_value():
    s = _parse()
    emis = {t.description: t for t in s.transactions if t.isEMI}
    assert len(emis) == 2, list(emis)

    principal = next(t for d, t in emis.items() if "EMI PRIN" in d)
    assert principal.emiNumber == 3
    assert principal.emiTenure == 20

    # The interest leg of an EMI is INTEREST, but still flagged as an EMI so the
    # UI groups it with its principal.
    interest = next(t for d, t in emis.items() if "EMI INT" in d)
    assert interest.transactionType == parsers.INTEREST
    assert interest.isEMI is True

    # Only the two genuine instalment legs, not the conversion-offer purchase.
    assert s.emiTotal == 500.00, s.emiTotal


def test_reconciles_against_the_statements_own_purchases_figure():
    s = _parse()
    assert s.reconciliation == "RECONCILED", (s.expectedTotal, s.extractedTotal)
    assert s.expectedTotal == 4725.00
    assert s.extractedTotal == 4725.00
    assert s.difference == 0.0


def test_mismatch_is_reported_not_swallowed():
    """A dropped row must surface as a mismatch, never as a clean import."""
    lines = [line for line in STATEMENT_LINES if "EXAMPLE FUELS" not in line]
    s = parsers.get_parser(CARD).parse(Extraction("text", lines, [], 4), "2026-07")
    assert s.reconciliation == "MISMATCH"
    assert s.difference == 2000.50, s.difference


def test_spend_total_excludes_payments_and_credits():
    s = _parse()
    spend = sum(t.amount for t in s.transactions if t.transactionType in parsers.SPEND_TYPES)
    # 1000 + 2000.50 + 1250 (EMI) + 400 (principal) + 100 (interest) + 100 (GST)
    assert round(spend, 2) == 4850.50, spend


def test_manual_owner_survives_a_reclassifying_resync():
    """A parser fix must not strand owners you set by hand.

    Transaction ids include the type, so reclassifying a row (EMI -> Purchase)
    mints a new id. The owner has to follow the row, not the id.
    """
    from creditcards import owner_match, sync
    from creditcards.parsers.base import ParsedStatement, ParsedTransaction

    card = CardConfig(id="kotak", cardName="K", bankName="K", lastFourDigits="1")
    statement = ParsedStatement(statementMonth="2026-07")
    statement.transactions = [
        ParsedTransaction(
            transactionDate="2026-07-18",
            description="PLAYSTATION (Convert To EMI)",
            merchant="playstation",
            amount=2757.24,
            transactionType="PURCHASE",
        )
    ]
    existing = {
        "old_fingerprint": {
            "transactionId": "old_fingerprint",
            "transactionDate": "2026-07-18",
            "amount": 2757.24,
            "merchant": "playstation",
            "ownerId": "owner_jithin",
            "ownerName": "Jithin",
            "ownerSource": owner_match.MANUAL,
            "createdAt": 1,
        }
    }
    index = owner_match.OwnerIndex({}, [], {"owner_jithin": {"name": "Jithin"}})
    payload, detected, review = sync._build_transactions(
        statement, card, index, "kotak_2026-07", existing, 999
    )

    new_id = next(iter(payload))
    assert new_id != "old_fingerprint"
    row = payload[new_id]
    assert row["ownerId"] == "owner_jithin"
    assert row["ownerSource"] == owner_match.MANUAL
    assert (detected, review) == (1, 0)


def test_merchant_normalisation_is_stable_across_months():
    # Reference numbers and city/country suffixes drop out, so a merchant key
    # stays the same month to month even when the raw line changes.
    assert normalize.normalize_merchant("UPI/AMAZON PAY INDIA 4408123 BANGALORE IN") == (
        "amazon pay"
    )
    # Same merchant, different reference number and city -> same key.
    assert normalize.normalize_merchant("AMAZON PAY INDIA 9912 MUMBAI") == "amazon pay"
    assert normalize.normalize_merchant("NETFLIX COM 1,25668 MUMBAI IN") == "netflix com"
    # Digits inside a name survive; only trailing reference fragments go.
    assert normalize.normalize_merchant("7 ELEVEN BANGALORE IN") == "7 eleven"
    assert normalize.normalize_merchant("RZP*SWIGGY 998877 BANGALORE IN") == "swiggy"
    assert normalize.normalize_merchant("POS SWIGGY BENGALURU") == "swiggy"


def test_transaction_id_is_stable_and_distinct():
    args = ("kotak", "2026-08", "2026-07-18", 2400.0, "amazon", "PURCHASE")
    assert normalize.transaction_id(*args) == normalize.transaction_id(*args)
    other = ("kotak", "2026-08", "2026-07-18", 2401.0, "amazon", "PURCHASE")
    assert normalize.transaction_id(*args) != normalize.transaction_id(*other)


def test_statement_id_is_card_plus_period():
    assert normalize.statement_id("kotak", "2026-08") == "kotak_2026-08"


def test_owner_rule_beats_history_and_low_signal_stays_unassigned():
    owners = {"o1": {"name": "Ben"}, "o2": {"name": "Jithin"}}
    index = owner_match.OwnerIndex(
        rules={"kotak": {"netflix com": {"ownerId": "o2", "confidence": 1.0}}},
        history=[
            {"cardId": "kotak", "merchant": "netflix com", "ownerId": "o1",
             "isEMI": False, "statementMonth": "2026-07"},
            {"cardId": "icici", "merchant": "swiggy", "ownerId": "o1",
             "isEMI": False, "statementMonth": "2026-07"},
        ],
        owners=owners,
    )

    # Rule wins over the older history row.
    netflix = index.resolve("kotak", "netflix com", False)
    assert netflix["ownerId"] == "o2"
    assert netflix["ownerSource"] == owner_match.MERCHANT_RULE
    assert netflix["confidence"] == 1.0

    # Same merchant seen on another card is a weaker but usable signal.
    swiggy = index.resolve("kotak", "swiggy", False)
    assert swiggy["ownerId"] == "o1"
    assert swiggy["ownerSource"] == owner_match.PREVIOUS_TRANSACTION

    # Never seen before -> explicitly unassigned, not a guess.
    unseen = index.resolve("kotak", "brand new merchant", False)
    assert unseen["ownerId"] is None
    assert unseen["ownerSource"] == owner_match.UNKNOWN


def test_latest_assignment_supersedes_older_one():
    index = owner_match.OwnerIndex(
        rules={},
        history=[
            {"cardId": "kotak", "merchant": "netflix", "ownerId": "o1",
             "isEMI": False, "statementMonth": "2026-07"},
            {"cardId": "kotak", "merchant": "netflix", "ownerId": "o2",
             "isEMI": False, "statementMonth": "2026-09"},
        ],
        owners={"o1": {"name": "Ben"}, "o2": {"name": "Jithin"}},
    )
    assert index.resolve("kotak", "netflix", False)["ownerId"] == "o2"


def test_emi_lineage_matches_previous_emi_owner():
    index = owner_match.OwnerIndex(
        rules={},
        history=[
            {"cardId": "hdfc", "merchant": "abc appliances emi", "ownerId": "o2",
             "isEMI": True, "statementMonth": "2026-07"},
        ],
        owners={"o2": {"name": "Jithin"}},
    )
    result = index.resolve("kotak", "abc appliances emi", True)
    assert result["ownerId"] == "o2"
    assert result["confidence"] >= owner_match.MIN_CONFIDENCE


def test_manual_rule_payload():
    card_id, merchant, payload = owner_match.rule_update(
        "kotak", "RZP*SWIGGY 998877 BANGALORE IN", "o1", 1234
    )
    assert (card_id, merchant) == ("kotak", "swiggy")
    assert payload == {
        "ownerId": "o1",
        "confidence": 1.0,
        "updatedAt": 1234,
        "source": owner_match.MANUAL,
    }


def test_sbi_credit_marker_variant():
    from creditcards.parsers.banks import SBIParser

    card = CardConfig(id="sbi", cardName="SBI", bankName="SBI Cards",
                      lastFourDigits="3456", parser="sbi")
    lines = [
        "Date Transaction Details Amount",
        "12/08/2026 PAYMENT RECEIVED 5,000.00 C",
        "13/08/2026 BIG BAZAAR CHENNAI 1,200.00",
    ]
    statement = SBIParser(card).parse(Extraction("text", lines, [], 1), "2026-08")
    types = {t.description[:7]: t.transactionType for t in statement.transactions}
    assert types["PAYMENT"] == parsers.PAYMENT
    assert types["BIG BAZ"] == parsers.PURCHASE


def test_statement_month_matching_prefers_the_right_email():
    """SBI mails August's statement on 8 August.

    A sync for July must not pick that message up just because it is newer than
    July's own 8 July mail.
    """
    from datetime import date, datetime, timezone

    from creditcards import gmail_client

    def ms(year, month, day):
        return int(
            datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000
        )

    aug_mail = ms(2026, 8, 8)
    jul_mail = ms(2026, 7, 8)

    # In-month mail scores 0; out-of-month mail scores its distance in days.
    assert gmail_client._distance_days(aug_mail, "2026-08") == 0
    assert gmail_client._distance_days(jul_mail, "2026-07") == 0
    assert gmail_client._distance_days(aug_mail, "2026-07") > 0

    candidates = [
        {"internalDate": aug_mail, "id": "aug"},
        {"internalDate": jul_mail, "id": "jul"},
    ]
    pick = lambda month: min(  # noqa: E731
        candidates,
        key=lambda c: (gmail_client._distance_days(c["internalDate"], month), -c["internalDate"]),
    )["id"]
    assert pick("2026-07") == "jul"
    assert pick("2026-08") == "aug"

    # The strict query really is confined to the calendar month.
    card = CardConfig(id="sbi", cardName="SBI", bankName="SBI Cards",
                      lastFourDigits="8160", senders=["statements@sbicard.com"])
    strict = gmail_client.build_query(card, "2026-07")
    assert "after:2026/07/01" in strict
    assert "before:2026/08/01" in strict
    assert 'from:"statements@sbicard.com"' in strict
    assert date  # imported for clarity above


def test_widened_search_never_reaches_into_the_previous_month():
    """Kotak mails on the 21st.

    An August sync run before the 21st must report "not found" rather than
    widening backwards and importing July's statement as August's.
    """
    from creditcards import gmail_client

    card = CardConfig(id="kotak", cardName="Kotak", bankName="Kotak",
                      lastFourDigits="2533", senders=["cardstatement@kotak.bank.in"])

    widened = gmail_client.build_query(card, "2026-08", trailing_days=12)
    # Opens on the 1st regardless of how far the window extends forward.
    assert "after:2026/08/01" in widened
    assert "before:2026/09/13" in widened
    assert "2026/07" not in widened


# --- Card config loading -----------------------------------------------------
# cards.json is git-ignored, so a deploy has only CREDIT_CARDS_CONFIG_JSON to
# go on. These cover the ways that env var arrives in practice.

CONFIG_ENV = "CREDIT_CARDS_CONFIG_JSON"
CONFIG_FILE_ENV = "CREDIT_CARDS_CONFIG_FILE"
MINIMAL_CONFIG = (
    '{"cards":[{"id":"sbi","cardName":"BPCL SBI OCTANE","bankName":"SBI Cards",'
    '"lastFourDigits":"8160","passwordEnv":"CC_PASSWORD_SBI","parser":"sbi"}]}'
)


def _load_with_env(value, cards_file="/nonexistent/cards.json"):
    """Load cards with the config env var set to `value` (None = unset)."""
    from creditcards import config

    saved = {k: os.environ.get(k) for k in (CONFIG_ENV, CONFIG_FILE_ENV)}
    try:
        os.environ.pop(CONFIG_ENV, None)
        if value is not None:
            os.environ[CONFIG_ENV] = value
        os.environ[CONFIG_FILE_ENV] = cards_file
        return config.load_cards()
    finally:
        for key, old in saved.items():
            os.environ.pop(key, None)
            if old is not None:
                os.environ[key] = old


def test_inline_config_json_is_used_when_no_file_exists():
    cards = _load_with_env(MINIMAL_CONFIG)
    assert [c.id for c in cards] == ["sbi"]
    assert cards[0].passwordEnv == "CC_PASSWORD_SBI"


def test_inline_config_survives_paste_whitespace_and_wrapping_quotes():
    """Host env-var UIs commonly add both. Neither is part of the JSON."""
    for value in (
        f"  {MINIMAL_CONFIG}\n",
        f"'{MINIMAL_CONFIG}'",
        f'  "{MINIMAL_CONFIG}"  ',
    ):
        assert [c.id for c in _load_with_env(value)] == ["sbi"], value


def test_empty_config_env_is_reported_as_empty_not_as_a_missing_file():
    """A blank env var is a paste that never saved — say so precisely."""
    from creditcards import config

    for blank in ("", "   "):
        try:
            _load_with_env(blank)
        except config.ConfigError as exc:
            assert CONFIG_ENV in str(exc) and "empty" in str(exc)
        else:
            raise AssertionError(f"blank {blank!r} should raise ConfigError")


def test_missing_config_points_at_the_deployment_fix():
    from creditcards import config

    try:
        _load_with_env(None)
    except config.ConfigError as exc:
        assert CONFIG_ENV in str(exc)
    else:
        raise AssertionError("a missing config file should raise ConfigError")


ICICI_CARD = CardConfig(id="icici_sapphiro", cardName="ICICI Sapphiro",
                        bankName="ICICI Bank", lastFourDigits="8000", parser="icici")


def test_icici_rows_survive_a_missing_block_heading():
    from creditcards.parsers.banks import ICICIParser

    # No "SPENDS OVERVIEW" / "Date SerNo" heading: a reworded heading used to
    # drop every row and fail the sync with nothing saved.
    lines = [
        "Total Amount Due 1,500.00",
        "Transaction Details",
        "02/09/2026 12345678901 EXAMPLE CAFE MUMBAI IN 12 1,000.00",
        "100% 05/09/2026 12345678902 EXAMPLE STORE PUNE IN 6 500.00",
    ]
    statement = ICICIParser(ICICI_CARD).parse(Extraction("text", lines, [], 1), "2026-09")
    assert len(statement.transactions) == 2
    assert statement.transactions[0].description == "EXAMPLE CAFE MUMBAI IN"
    assert statement.reconciliation == "RECONCILED"
    assert any("heading was not found" in w for w in statement.warnings)


def test_icici_heading_with_spaced_serial_column():
    from creditcards.parsers.banks import ICICIParser

    lines = [
        "Date Ser. No. Transaction Details Reward Points Amount",
        "02/09/2026 12345678901 EXAMPLE CAFE MUMBAI IN 12 1,000.00",
    ]
    statement = ICICIParser(ICICI_CARD).parse(Extraction("text", lines, [], 1), "2026-09")
    assert len(statement.transactions) == 1
    assert not any("heading was not found" in w for w in statement.warnings)


def test_no_rows_reports_a_diagnosis():
    from creditcards.parsers.banks import ICICIParser

    lines = ["Total Amount Due 1,500.00", "Sep 02 EXAMPLE CAFE 1,000.00"]
    statement = ICICIParser(ICICI_CARD).parse(Extraction("text", lines, [], 1), "2026-09")
    assert not statement.transactions
    diagnosis = [w for w in statement.warnings if w.startswith("Diagnosis:")]
    assert diagnosis and "block heading NOT found" in diagnosis[0]
    assert "0 lines start with a date" in diagnosis[0]


def test_icici_rows_with_side_banner_text_in_front():
    from creditcards.parsers.banks import ICICIParser

    # Upright letters stacked down the page's side banner are extracted onto
    # the start of whichever row shares their height, so no row starts with
    # its date — the October 2026 Sapphiro statement failed this way.
    lines = [
        "A Date SerNo. Transaction Details Reward Points Amount",
        "B 25/09/2026 14236658090 EXAMPLE STORE MUMBAI IN 1138 56,925.00",
        "C 29/09/2026 14253321006 Principal Amount Amortization - 0 4,365.01",
        "D <1/12>EXAMPLE STORE",
        "01/10/2026 14275490705 EXAMPLE CLINIC KOCHI IN 5 250.00",
        "Merchant EMI 29/09/2026 29/08/2027 12 56,925.00 11 57,407.72 5,218.88",
    ]
    statement = ICICIParser(ICICI_CARD).parse(Extraction("text", lines, [], 1), "2026-10")
    txns = statement.transactions
    assert [t.transactionDate for t in txns] == ["2026-09-25", "2026-09-29", "2026-10-01"]
    assert txns[0].description == "EXAMPLE STORE MUMBAI IN"
    assert txns[1].transactionType == parsers.EMI and txns[1].emiTenure == 12


def test_diagnosis_shows_the_rows_under_the_heading():
    from creditcards.parsers.banks import ICICIParser

    lines = ["SPENDS OVERVIEW", "Sep 18 EXAMPLE CAFE 1,000.00"]
    statement = ICICIParser(ICICI_CARD).parse(Extraction("text", lines, [], 1), "2026-10")
    diagnosis = [w for w in statement.warnings if w.startswith("Diagnosis:")][0]
    assert "'Sep 18 EXAMPLE CAFE 1,000.00'" in diagnosis
    assert "[v2] no line after the heading has a long number" in diagnosis

    lines = ["SPENDS OVERVIEW", "\u200b25 / 09 / 2026 14236658090 EXAMPLE CAFE 1,000.00"]
    statement = ICICIParser(ICICI_CARD).parse(Extraction("text", lines, [], 1), "2026-10")
    diagnosis = [w for w in statement.warnings if w.startswith("Diagnosis:")][0]
    assert "'\\u200b25 / 09 / 2026 14236658090" in diagnosis


def test_icici_card_subheading_is_not_a_wrapped_description():
    from creditcards.parsers.banks import ICICIParser

    lines = [
        "Date SerNo. Transaction Details Reward Points Amount",
        "4501XXXXXXXX1111",
        "02/10/2026 12345678901 INFINITY PAYMENT RECEIVED, THANK YOU 0 1,000.00 CR",
        "6530XXXXXXXX2222",
        "03/10/2026 12345678902 EXAMPLE CAFE MUMBAI IN 5 500.00",
    ]
    statement = ICICIParser(ICICI_CARD).parse(Extraction("text", lines, [], 1), "2026-10")
    assert [t.description for t in statement.transactions] == [
        "INFINITY PAYMENT RECEIVED, THANK YOU",
        "EXAMPLE CAFE MUMBAI IN",
    ]


def test_icici_fake_bold_rows_are_undoubled():
    from creditcards.parsers.banks import ICICIParser

    # Glyphs painted twice for bold extract doubled; the October 2026
    # Sapphiro statement printed its transaction rows this way.
    lines = [
        "Date SerNo. Transaction Details Reward Points Amount",
        "2255//0099//22002266 1144223366665588009900 EXAMPLE STORE MUMBAI IINN "
        "11113388 5566,,992255..0000",
        "2299//0099//22002266 1144225533332200999933 EXAMPLE STORE MUMBAI IINN "
        "--11113388 5566,,992255..0000 CCRR",
        "01/10/2026 14275490705 EXAMPLE CLINIC KOCHI IN 5 250.00",
    ]
    statement = ICICIParser(ICICI_CARD).parse(Extraction("text", lines, [], 1), "2026-10")
    txns = statement.transactions
    assert [(t.transactionDate, t.amount, t.transactionType) for t in txns] == [
        ("2026-09-25", 56925.0, parsers.PURCHASE),
        ("2026-09-29", 56925.0, parsers.CREDIT),
        ("2026-10-01", 250.0, parsers.PURCHASE),
    ]
    assert txns[0].description == "EXAMPLE STORE MUMBAI IN"


def test_extraction_drops_glyphs_painted_twice():
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        return  # optional: only needed to build the PDF for this test
    import io

    from creditcards import extract

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.setFont("Helvetica", 9)
    for dx in (0, 0.4):
        pdf.drawString(60 + dx, 680, "25/09/2026 14236658090 BILL PAYMENT 1,100.00")
    pdf.drawString(60, 400, "x" * 250)
    pdf.save()
    assert extract.extract(buf.getvalue()).lines[0] == (
        "25/09/2026 14236658090 BILL PAYMENT 1,100.00"
    )


def test_icici_ciphered_amount_column_is_decoded():
    from creditcards.parsers.banks import ICICIParser

    # Real extracted rows from the October 2026 Sapphiro statement, whose
    # amount column extracts as letters (B=1 … J=9, K=0) mixed with stray digits.
    lines = [
        "Date SerNo. Transaction Details Reward Points Amount",
        "25/09/2026 14236658090 EXAMPLE STORE MUMBAI IN 1138 F4G7,4J3C7F..5K0K",
        "29/09/2026 14253320993 EXAMPLE STORE MUMBAI IN -1138 F4G7,4J3C7F..5K0K CR",
        "29/09/2026 14253320998 EXAMPLE STORE MUMBAI IN 0 0K..0K0K CR",
        "01/10/2026 14275490705 EXAMPLE CLINIC KOCHI IN 5 250.00",
    ]
    statement = ICICIParser(ICICI_CARD).parse(Extraction("text", lines, [], 1), "2026-10")
    assert [(t.amount, t.transactionType) for t in statement.transactions] == [
        (56925.0, parsers.PURCHASE),
        (56925.0, parsers.CREDIT),
        (250.0, parsers.PURCHASE),
    ]
    assert statement.transactions[0].description == "EXAMPLE STORE MUMBAI IN"


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
