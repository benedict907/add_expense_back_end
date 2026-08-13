# Credit Card Dashboard — setup

Separate module from expenses. Nothing here reads or writes
`<dataRoot>/expenses`.

```
Dashboard  ->  POST /credit-cards/sync
                 -> Gmail search (per card)
                 -> download PDF attachment
                 -> pikepdf decrypt (password from env)
                 -> pdfplumber text -> tables -> OCR
                 -> bank parser -> reconcile vs statement totals
                 -> owner matching (rules + history)
                 -> Realtime Database
Dashboard  <-  live RTDB subscription
```

## 1. Install

```bash
pip install -r requirements.txt
```

OCR fallback only (scanned statements):

```bash
brew install tesseract poppler
```

## 2. Gmail OAuth

1. [Google Cloud Console](https://console.cloud.google.com) → create/select a project.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **OAuth consent screen** → External → add your own address under **Test users**.
   No verification review is needed for a single test user.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**.
5. Put the client id/secret in `.env`, then run once, locally:

```bash
python tools/gmail_oauth_setup.py
```

It prints `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`. Put all
three in `.env` locally and in your host's environment variables for deployment.
Scope is `gmail.readonly`. The browser never sees these.

## 3. Cards

```bash
cp creditcards/cards.example.json creditcards/cards.json
```

Edit it — the file is git-ignored and contains **no passwords**:

| Field | What |
|---|---|
| `id` | stable slug, used in URLs and Firebase keys |
| `cardName`, `bankName`, `lastFourDigits` | display only |
| `senders`, `subjectContains` | Gmail search, OR-ed within each list |
| `extraQuery` | raw Gmail syntax, e.g. `-subject:"payment received"` |
| `attachmentNameContains` | narrows which PDF part is the statement |
| `passwordEnv` | **name** of the env var holding the PDF password |
| `parser` | `icici` `kotak` `hdfc` `axis` `sbi` `generic` |
| `active` | inactive cards are skipped by sync |

To find the right `senders`/`subjectContains`: open a real statement email in
Gmail, **Show original**, copy the `From` and `Subject`.

`cards.json` is git-ignored, so it is **never part of a deploy**. On the host,
paste the same JSON into `CREDIT_CARDS_CONFIG_JSON` instead:

```bash
python tools/print_cards_env.py        # one line, ready to paste
python tools/print_cards_env.py --check  # validate only
```

It validates through the same loader the server uses, lists the `CC_PASSWORD_*`
vars the host still needs, and prints no passwords (`cards.json` holds env-var
*names* only). Without this, a sync fails with
`Card config not found at '/opt/render/project/src/creditcards/cards.json'`.

## 4. Statement passwords

One env var per card, named by that card's `passwordEnv`:

```
CC_PASSWORD_KOTAK=...            # Kotak Cashback+ ····2533
CC_PASSWORD_ICICI_AMAZON=...     # Amazon Pay ICICI ····1009
CC_PASSWORD_ICICI_SAPPHIRO=...   # ICICI Sapphiro ····8000
CC_PASSWORD_HDFC=...             # Swiggy HDFC Bank ····9976
CC_PASSWORD_SBI=...              # BPCL SBI Card OCTANE ····8160
```

Statement arrival dates differ per issuer, which matters when you sync:

| Card | Statement mailed | In-month? |
|---|---|---|
| Amazon Pay ICICI | ~13th | yes |
| Swiggy HDFC | ~14th | yes |
| ICICI Sapphiro | ~19th | yes |
| Kotak Cashback+ | ~21st | yes |
| BPCL SBI OCTANE | ~8th | yes |

Syncing a month before its statement has been emailed correctly reports
"Statement not found" — the search never widens backwards into the previous
month, so it cannot import the wrong month's statement.

These live in `.env` locally and in the host's env vars in deployment. They are
never written to Firebase, never returned by any endpoint, and never logged.

## 5. Firebase rules

Merge `firebase.rules.json` into **Firebase Console → Realtime Database → Rules**.
It restricts every credit-card node to the signed-in owner of that `dataRoot`.

## 6. Front end

`add_expense_front_end/.env` needs `VITE_API_URL` pointing at this backend.
Routes: `/credit-cards` and `/credit-cards/:cardId`.

The browser authenticates with its Firebase ID token, verified server-side and
checked against `EXPENSE_DATA_ROOT`. No API key ships to the browser.
`CREDIT_CARD_API_KEY` is optional and only for CLI/cron use.

## 7. Deploy (Render / Railway)

`Procfile` is included: `uvicorn app:app --host 0.0.0.0 --port $PORT`.

Environment variables the host needs: everything in `.env.example`, plus
`CREDIT_CARDS_CONFIG_JSON` — `cards.json` is git-ignored and so is absent from
every deploy. Generate the value with `python tools/print_cards_env.py` (section
3). A sync that reports `Card config not found at
'/opt/render/project/src/creditcards/cards.json'` means this variable is unset;
one that reports it is "set but empty" means the paste did not save.

Note: `pdfplumber` and `pikepdf` ship wheels, so no build tooling is needed.
OCR needs `tesseract` and `poppler` as system packages — if your host cannot
install them, the first two extraction tiers still work for digitally generated
statements, and OCR failures are reported per card rather than crashing a sync.

## Parser status

Verified against real July 2026 statements — every extracted total matches the
statement's own printed figure to the paisa:

| Card | Rows | Reconciles against | Difference |
|---|---|---|---|
| Kotak Cashback+ | 26 | Purchases made in this cycle (₹29,789.97) | ₹0.00 |
| Amazon Pay ICICI | 6 | Total Amount Due (₹14,003.02) | ₹0.00 |
| Swiggy HDFC | 11 | Purchases/Debit (₹3,756.00) | ₹0.00 |
| BPCL SBI OCTANE | 9 | Purchases & Other Debits (₹1,03,524.60) | ₹0.00 |
| ICICI Sapphiro | 13 | Total Amount Due (₹10,063.55) | ₹0.00 |

Also verified across June and August, and March/April for Kotak — 13 statements
in all, every one reconciled.

What each bank needed, and why the parsers are per-bank:

- **Kotak** — carries a Spends Category column (Grocery, Fuel, Restaurants…),
  captured into `category`. Its "purchases" figure is net: EMI instalments in,
  fuel surcharge waivers out.
- **ICICI** — headings render with doubled letters (`SSTTAATTEEMMEENNTT`), rows
  open with an 11-digit serial, descriptions wrap onto the next line (which is
  where the EMI marker `<17/24>` lives), and a `100%` reward badge can precede
  the date. Prints no purchases subtotal, so it reconciles on Total Amount Due.
- **HDFC** — the rupee glyph extracts as a bare `C`, credits are marked by a
  `+` *before* it, rows carry a `| HH:MM` timestamp and end with a bullet.
- **SBI** — single-letter markers (`C` credit, `D` debit, `M` instalment,
  `T` temporary credit), dates as `30 Jun 26`, tax sub-rows printed with no date
  at all, and a five-figure ACCOUNT SUMMARY grid read positionally.

## Tuning a parser against a real statement

```
GET /credit-cards/debug/extract?cardId=kotak&month=2026-08
```

Returns the extracted lines, which rows were recognised as transactions, the
parsed summary figures and sample transactions — and writes nothing. Adjust the
bank class in `creditcards/parsers/banks.py` until `recognisedRowCount` matches
what the PDF actually contains.

Check a Gmail query without downloading anything:

```
GET /credit-cards/preview?cardId=kotak&month=2026-08
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/credit-cards/config` | cards, owners, last sync, Gmail status |
| POST | `/credit-cards/sync` | `{month, cardIds, force}` — run the pipeline |
| GET | `/credit-cards/preview` | show/validate a card's Gmail query |
| GET | `/credit-cards/debug/extract` | parser tuning dump, writes nothing |
| PATCH | `/credit-cards/transactions/{statementId}/{txnId}/owner` | assign/clear owner, learn the rule |
| GET/POST | `/credit-cards/owners` | list / add a person |

## Data model

```
<dataRoot>/creditCards/<cardId>                    display metadata only
<dataRoot>/creditCardOwners/<ownerId>              name, colour
<dataRoot>/creditCardStatements/<cardId>_<YYYY-MM> one per card per month
<dataRoot>/creditCardTransactions/<statementId>/<txnId>
<dataRoot>/creditCardOwnerRules/<cardId>/<merchant>   "*" cardId = all cards
<dataRoot>/creditCardMeta/lastSync
```

**Duplicate protection** is structural: statement id is `cardId_YYYY-MM`, and
transaction id is a SHA-1 of card + period + date + amount + normalised merchant
+ type. Re-syncing writes to the same keys. Already-imported statements return
`ALREADY_IMPORTED` and are not re-fetched unless you pass `force: true`.

**Owner matching** cascade (first hit above 0.6 confidence wins):

| Confidence | Signal |
|---|---|
| 1.00 | manual rule, this merchant on this card |
| 0.85 | manual rule, this merchant on any card |
| 0.75 | same merchant + same card in an earlier statement |
| 0.70 | EMI lineage: same EMI merchant owned before |
| 0.60 | same merchant on any card in an earlier statement |

Below that, `ownerId` stays `null` and the transaction shows **Owner required**.
A re-sync never overwrites an owner you set by hand.

## Tests

```bash
python tests/test_creditcards.py
```

Covers row parsing, credit-vs-payment-vs-refund classification, EMI instalment
handling, fingerprint stability and the owner cascade. No network needed.
