"""Parser registry. `cards.json` picks a parser per card by key."""

from .base import (  # noqa: F401  (re-exported for callers)
    CREDIT,
    EMI,
    FEE,
    INTEREST,
    OTHER,
    PAYMENT,
    PURCHASE,
    REFUND,
    SPEND_TYPES,
    BaseParser,
    ParsedStatement,
    ParsedTransaction,
    ParseError,
)
from .banks import (
    AxisParser,
    GenericParser,
    HDFCParser,
    ICICIParser,
    KotakParser,
    SBIParser,
)

REGISTRY = {
    parser.key: parser
    for parser in (
        GenericParser,
        ICICIParser,
        KotakParser,
        HDFCParser,
        AxisParser,
        SBIParser,
    )
}


def get_parser(card):
    """Parser instance for a card. Unknown keys fall back to generic."""
    return REGISTRY.get(card.parser or "generic", GenericParser)(card)


def available() -> list:
    return sorted(REGISTRY)
