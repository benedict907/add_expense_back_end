"""Read-only Gmail access for statement emails.

Auth is a single-user refresh token minted once by tools/gmail_oauth_setup.py.
Tokens live in the environment and never leave the server; the front end only
ever sees the sync result.

Structured so a future scheduled poller can reuse `find_statement_message`
unchanged — it takes an explicit month rather than reading the clock.
"""

import base64
import os
from datetime import date, timedelta

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

_service = None


class GmailError(RuntimeError):
    pass


class StatementNotFound(GmailError):
    """No email matched this card for the requested month."""


class AuthExpired(GmailError):
    """The refresh token no longer works — re-consent is the only fix.

    Worth its own type because it is not a per-card problem: no card can sync
    until a human re-mints the token, so the sync stops instead of repeating
    the same failure once per card.
    """


def _credentials() -> Credentials:
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
    missing = [
        name
        for name, value in (
            ("GMAIL_CLIENT_ID", client_id),
            ("GMAIL_CLIENT_SECRET", client_secret),
            ("GMAIL_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        raise GmailError(
            f"Gmail is not configured: missing {', '.join(missing)}. "
            "Run: python tools/gmail_oauth_setup.py"
        )
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise AuthExpired(_refresh_failure_message(exc)) from exc
    return creds


def _refresh_failure_message(exc: RefreshError) -> str:
    """Translate Google's `invalid_grant` into what to actually do about it.

    Google returns the same terse code whatever the cause, so the message names
    the likely one first: an OAuth consent screen left in "Testing" expires
    every refresh token it issues after 7 days, which is the default state for
    the single-test-user setup in CREDIT_CARDS_SETUP.md.
    """
    detail = str(exc)
    if "invalid_grant" not in detail:
        return f"Gmail sign-in failed: {detail}"
    return (
        "Gmail access has expired (invalid_grant). If the OAuth consent screen "
        "is still in Testing, Google expires refresh tokens after 7 days — "
        "publish the app first (Google Cloud Console -> APIs & Services -> "
        "OAuth consent screen -> Publish app), otherwise the replacement "
        "expires too. Then re-run: python tools/gmail_oauth_setup.py, and put "
        "the new GMAIL_REFRESH_TOKEN in your host's environment. Access is also "
        "revoked by changing your Google password or removing the app at "
        "myaccount.google.com/permissions."
    )


def check_auth() -> str:
    """Verify the refresh token before any per-card work. Returns the mailbox.

    Raises AuthExpired / GmailError, so a dead token is reported once, up front,
    instead of as an identical failure on every card.
    """
    return profile_email()


def get_service():
    """Build the Gmail service lazily and reuse it across syncs."""
    global _service
    if _service is None:
        _service = build("gmail", "v1", credentials=_credentials(), cache_discovery=False)
    return _service


def reset_service():
    """Drop the cached client — used after a credential change."""
    global _service
    _service = None


def _or_clause(field: str, values) -> str:
    cleaned = [v.strip() for v in values if v and v.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return f'{field}:"{cleaned[0]}"'
    inner = " OR ".join(f'{field}:"{v}"' for v in cleaned)
    return f"({inner})"


def month_bounds(month_key: str):
    """('2026-08') -> (date(2026,8,1), date(2026,9,1))."""
    year, month = (int(part) for part in month_key.split("-"))
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1)
    return start, end


def build_query(card, month_key: str, trailing_days: int = 0) -> str:
    """Gmail query for one card and one statement month.

    The window never opens *before* the month starts: a statement for August
    cannot be emailed in July, so searching backwards can only ever match the
    previous month's statement. Kotak mails on the 21st, so an August sync run
    on the 9th finds nothing and must report "not found" — not silently import
    July's statement as August's.

    `trailing_days` extends the window *forward* only, for issuers that mail a
    month's statement in the first days of the next month. `find_statement_
    message` uses it only when the strict, in-month pass finds nothing.
    """
    start, end = month_bounds(month_key)
    parts = [
        "has:attachment",
        "filename:pdf",
        f"after:{start.strftime('%Y/%m/%d')}",
        f"before:{(end + timedelta(days=trailing_days)).strftime('%Y/%m/%d')}",
        "in:anywhere",
    ]
    sender = _or_clause("from", card.senders)
    if sender:
        parts.append(sender)
    subject = _or_clause("subject", card.subjectContains)
    if subject:
        parts.append(subject)
    if card.extraQuery:
        parts.append(card.extraQuery)
    return " ".join(parts)


def _header(message: dict, name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _walk_parts(part):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


def _pdf_attachments(message: dict):
    """Yield (filename, attachmentId) for every PDF part in the message."""
    payload = message.get("payload", {})
    for part in _walk_parts(payload):
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        if not filename or not body.get("attachmentId"):
            continue
        mime = (part.get("mimeType") or "").lower()
        if filename.lower().endswith(".pdf") or mime == "application/pdf":
            yield filename, body["attachmentId"]


def search_messages(query: str, limit: int = 10) -> list:
    """Return message stubs for a raw Gmail query."""
    response = (
        get_service()
        .users()
        .messages()
        .list(userId="me", q=query, maxResults=limit)
        .execute()
    )
    return response.get("messages", []) or []


def _candidates(card, query: str, limit: int = 10) -> list:
    """Messages matching `query` that carry a PDF this card would accept."""
    service = get_service()
    wanted = [w.lower() for w in card.attachmentNameContains if w]
    found = []
    for stub in search_messages(query, limit=limit):
        message = (
            service.users()
            .messages()
            .get(userId="me", id=stub["id"], format="full")
            .execute()
        )
        for filename, attachment_id in _pdf_attachments(message):
            if wanted and not any(w in filename.lower() for w in wanted):
                continue
            labels = set(message.get("labelIds", []))
            found.append(
                {
                    "messageId": message["id"],
                    "threadId": message.get("threadId", ""),
                    "subject": _header(message, "Subject"),
                    "sender": _header(message, "From"),
                    "internalDate": int(message.get("internalDate", 0)),
                    "attachmentName": filename,
                    "attachmentId": attachment_id,
                    # Trashed copies still work, but Gmail purges Trash after 30
                    # days — prefer a live copy when both exist.
                    "discardable": bool(labels & {"TRASH", "SPAM"}),
                }
            )
            break  # One statement PDF per email.
    return found


def _distance_days(internal_date_ms: int, month_key: str) -> float:
    """How far a message sits from the target month. 0 means inside it."""
    start, end = month_bounds(month_key)
    when = date.fromtimestamp(internal_date_ms / 1000)
    if start <= when < end:
        return 0.0
    if when < start:
        return (start - when).days
    return (when - end).days + 1


def find_statement_message(card, month_key: str, trailing_days: int = 12) -> dict:
    """Locate this card's statement email for `month_key`.

    Two passes: an in-month search first, then one extended forward for issuers
    whose statement lands in the first days of the following month. Among
    candidates the closest to the target month wins, so a July sync can never
    pick up August's mail just because it is newer, and an August sync can never
    fall back to July's.

    Raises StatementNotFound when nothing matches — callers turn that into a
    visible "Statement not found" row rather than skipping silently.
    """
    strict_query = build_query(card, month_key, trailing_days=0)
    candidates = _candidates(card, strict_query)
    query = strict_query

    if not candidates and trailing_days > 0:
        query = build_query(card, month_key, trailing_days=trailing_days)
        candidates = _candidates(card, query)

    if not candidates:
        raise StatementNotFound(
            f"No email with a matching PDF found for {card.cardName} in "
            f"{month_key}. Query: {query}"
        )

    # Closest to the month wins, then a live copy over a trashed one, then the
    # more recent mail.
    best = min(
        candidates,
        key=lambda c: (
            _distance_days(c["internalDate"], month_key),
            c.get("discardable", False),
            -c["internalDate"],
        ),
    )
    return {**best, "query": query}


def download_attachment(message_id: str, attachment_id: str) -> bytes:
    """Fetch attachment bytes. Never written to disk."""
    data = (
        get_service()
        .users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    raw = data.get("data")
    if not raw:
        raise GmailError(f"Attachment {attachment_id} returned no data")
    return base64.urlsafe_b64decode(raw)


def profile_email() -> str:
    """Which mailbox the refresh token belongs to — shown in the sync UI."""
    return get_service().users().getProfile(userId="me").execute().get("emailAddress", "")
