"""Discover real statement emails, so cards.json is filled from actual data.

Uses the Gmail refresh token already in .env. Prints the sender, subject and
attachment name of every candidate statement email — paste those into
creditcards/cards.json as `senders` / `subjectContains` / `attachmentNameContains`.

    python tools/find_statements.py                  # last 120 days, all banks
    python tools/find_statements.py sbi              # only mail mentioning "sbi"
    python tools/find_statements.py sbi --days 365

Read-only. Downloads nothing, decrypts nothing.
"""

import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from creditcards import gmail_client  # noqa: E402

# Broad enough to catch any issuer, narrow enough to skip marketing mail.
BASE_QUERY = 'has:attachment filename:pdf in:anywhere'
STATEMENT_HINTS = (
    'subject:statement OR subject:"credit card" OR subject:bill OR '
    'subject:"e-statement" OR subject:estatement'
)


def _address(sender: str) -> str:
    match = re.search(r"<([^>]+)>", sender)
    return (match.group(1) if match else sender).strip().lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("term", nargs="?", default="", help="extra filter, e.g. sbi")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    query = f"{BASE_QUERY} newer_than:{args.days}d ({STATEMENT_HINTS})"
    if args.term:
        query += f" {args.term}"

    print(f"Mailbox: {gmail_client.profile_email()}")
    print(f"Query:   {query}\n")

    stubs = gmail_client.search_messages(query, limit=args.limit)
    if not stubs:
        print("No matching mail. Try --days 365, or a different search term.")
        return 0

    service = gmail_client.get_service()
    by_sender = defaultdict(list)

    for stub in stubs:
        message = (
            service.users().messages().get(userId="me", id=stub["id"], format="full").execute()
        )
        sender = gmail_client._header(message, "From")
        subject = gmail_client._header(message, "Subject")
        date = gmail_client._header(message, "Date")
        attachments = [name for name, _ in gmail_client._pdf_attachments(message)]
        if not attachments:
            continue
        by_sender[_address(sender)].append((date, subject, attachments))

    for address, rows in sorted(by_sender.items()):
        print(f"\n=== {address}  ({len(rows)} message(s))")
        for date, subject, attachments in rows[:5]:
            print(f"  date:       {date}")
            print(f"  subject:    {subject}")
            print(f"  attachment: {', '.join(attachments)}")
            print()

    print("\n--- suggested cards.json fragment per sender ---")
    for address, rows in sorted(by_sender.items()):
        # Longest common prefix of the subjects is the stable part to match on;
        # the trailing month/date is what varies between statements.
        subjects = [subject for _, subject, _ in rows]
        common = os.path.commonprefix(subjects).strip() if len(subjects) > 1 else subjects[0]
        common = re.sub(r"[\s\-–:]+$", "", common)
        if len(common) < 12:
            # Issuers that lead with the month ("Jul-2026 Statement for …")
            # share almost no prefix; the distinctive words sit at the end.
            common = "REPLACE_ME — pick a phrase common to the subjects above"
        print(f'''
  {{
    "id": "CHANGE_ME",
    "cardName": "CHANGE_ME",
    "bankName": "CHANGE_ME",
    "lastFourDigits": "0000",
    "senders": ["{address}"],
    "subjectContains": ["{common}"],
    "attachmentNameContains": [".pdf"],
    "passwordEnv": "CC_PASSWORD_CHANGE_ME",
    "parser": "generic",
    "active": true
  }},''')
    return 0


if __name__ == "__main__":
    sys.exit(main())
