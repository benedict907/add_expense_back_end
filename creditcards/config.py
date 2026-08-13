"""Card configuration for the credit-card module.

Cards are described in a JSON file (default ``creditcards/cards.json``) that is
git-ignored, so search patterns and password *references* never reach the repo.
The file holds no secrets itself: ``passwordEnv`` names an environment variable,
and the password is only ever read from the process environment.

Nothing in here is sent to the browser except the fields listed in
``public_dict()`` — Gmail queries and password env names stay server-side.
"""

import json
import os
from dataclasses import dataclass, field

DEFAULT_CARDS_FILE = os.path.join(os.path.dirname(__file__), "cards.json")


@dataclass
class CardConfig:
    id: str
    cardName: str
    bankName: str
    lastFourDigits: str
    # Gmail search fragments. `senders` and `subjectContains` are OR-ed within
    # themselves and AND-ed with each other, so a card matches only its own mail.
    senders: list = field(default_factory=list)
    subjectContains: list = field(default_factory=list)
    # Extra raw Gmail query appended verbatim, e.g. '-subject:"payment received"'.
    extraQuery: str = ""
    attachmentNameContains: list = field(default_factory=list)
    # Name of the env var holding this card's PDF password. Never the password.
    passwordEnv: str = ""
    # Parser key; see creditcards.parsers.registry. Falls back to "generic".
    parser: str = "generic"
    active: bool = True
    createdAt: int = 0

    def public_dict(self) -> dict:
        """Only the fields safe to expose to the front end."""
        return {
            "id": self.id,
            "cardName": self.cardName,
            "bankName": self.bankName,
            "lastFourDigits": self.lastFourDigits,
            "active": self.active,
            "createdAt": self.createdAt,
        }

    def password(self) -> str:
        """Read this card's PDF password from the environment.

        Raises RuntimeError when unconfigured so the sync reports a precise
        reason instead of failing deep inside pikepdf.
        """
        if not self.passwordEnv:
            raise RuntimeError(
                f"Card {self.id!r} has no passwordEnv configured in cards.json"
            )
        value = os.environ.get(self.passwordEnv)
        if not value:
            raise RuntimeError(
                f"Environment variable {self.passwordEnv} is not set "
                f"(PDF password for card {self.id!r})"
            )
        return value


class ConfigError(RuntimeError):
    pass


def _cards_path() -> str:
    return os.environ.get("CREDIT_CARDS_CONFIG_FILE", DEFAULT_CARDS_FILE)


def _unwrap(value: str) -> str:
    """Trim whitespace, and one matching pair of wrapping quotes if present."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def load_cards(include_inactive: bool = False) -> list:
    """Load card configs. Raises ConfigError with an actionable message."""
    inline_raw = os.environ.get("CREDIT_CARDS_CONFIG_JSON")
    # Pasting into a host's env-var UI often picks up surrounding whitespace or
    # a wrapping pair of quotes; neither is meant to be part of the JSON.
    inline = _unwrap(inline_raw) if inline_raw is not None else None
    if inline:
        try:
            raw = json.loads(inline)
        except ValueError as exc:
            raise ConfigError(f"CREDIT_CARDS_CONFIG_JSON is not valid JSON: {exc}")
    elif inline_raw is not None:
        # Set but empty — usually a paste that never saved. Say so, rather than
        # falling through to a file-not-found message that sends the reader
        # looking for the wrong problem.
        raise ConfigError(
            "CREDIT_CARDS_CONFIG_JSON is set but empty. Paste the contents of "
            "creditcards/cards.json into it (python tools/print_cards_env.py "
            "prints the exact one-line value), or unset it to read the file."
        )
    else:
        path = _cards_path()
        if not os.path.exists(path):
            raise ConfigError(
                f"Card config not found at {path!r}. In deployment, set "
                "CREDIT_CARDS_CONFIG_JSON to the contents of your local "
                "creditcards/cards.json (python tools/print_cards_env.py prints "
                "it as one line) — cards.json is git-ignored, so it is never "
                "part of a deploy. Locally, copy creditcards/cards.example.json "
                "to creditcards/cards.json and fill it in."
            )
        with open(path, "r", encoding="utf-8") as handle:
            try:
                raw = json.load(handle)
            except ValueError as exc:
                raise ConfigError(f"{path} is not valid JSON: {exc}")

    cards_raw = raw.get("cards") if isinstance(raw, dict) else raw
    if not isinstance(cards_raw, list):
        raise ConfigError("Card config must be a list, or an object with a 'cards' list")

    known = set(CardConfig.__dataclass_fields__)
    cards = []
    for entry in cards_raw:
        missing = [k for k in ("id", "cardName", "bankName") if not entry.get(k)]
        if missing:
            raise ConfigError(f"Card entry missing required fields {missing}: {entry}")
        cards.append(CardConfig(**{k: v for k, v in entry.items() if k in known}))

    ids = [c.id for c in cards]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ConfigError(f"Duplicate card ids in config: {sorted(dupes)}")

    return cards if include_inactive else [c for c in cards if c.active]


def get_card(card_id: str) -> CardConfig:
    for card in load_cards(include_inactive=True):
        if card.id == card_id:
            return card
    raise ConfigError(f"Unknown card id {card_id!r}")


def data_root() -> str:
    """Realtime Database root for this user — same key the front end uses."""
    root = os.environ.get("EXPENSE_DATA_ROOT", "")
    if not root:
        raise RuntimeError("EXPENSE_DATA_ROOT is not set")
    return root
