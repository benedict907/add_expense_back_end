"""Print creditcards/cards.json as the one-line CREDIT_CARDS_CONFIG_JSON value.

cards.json is git-ignored, so it is never part of a deploy. Hosts like Render
therefore need the same JSON pasted into the CREDIT_CARDS_CONFIG_JSON
environment variable — that is what this prints.

    python tools/print_cards_env.py                  # one line, ready to paste
    python tools/print_cards_env.py --check          # validate only, print nothing

The output contains no passwords: cards.json holds `passwordEnv` names only.
Those env vars (CC_PASSWORD_*) still have to be set on the host separately.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creditcards import config  # noqa: E402

# Anything that looks like a password rather than an env-var name should never
# reach a paste buffer, let alone a host's env UI. cards.json has no field for
# one, but a hand-edited file can still sprout a stray key.
SECRET_KEYS = ("password", "secret", "token")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the config and report, without printing the JSON",
    )
    args = parser.parse_args()

    path = os.environ.get("CREDIT_CARDS_CONFIG_FILE", config.DEFAULT_CARDS_FILE)
    if not os.path.exists(path):
        print(
            f"No card config at {path}.\n"
            "Copy creditcards/cards.example.json to creditcards/cards.json and "
            "fill it in first — see CREDIT_CARDS_SETUP.md section 3.",
            file=sys.stderr,
        )
        return 1

    with open(path, "r", encoding="utf-8") as handle:
        try:
            raw = json.load(handle)
        except ValueError as exc:
            print(f"{path} is not valid JSON: {exc}", file=sys.stderr)
            return 1

    # Run it through the real loader so a value that fails here can never be
    # pasted into a host and fail there instead.
    try:
        cards = config.load_cards(include_inactive=True)
    except config.ConfigError as exc:
        print(f"Card config is invalid: {exc}", file=sys.stderr)
        return 1

    entries = raw.get("cards") if isinstance(raw, dict) else raw
    for entry in entries:
        for key in entry:
            if key.endswith("Env") or not any(h in key.lower() for h in SECRET_KEYS):
                continue
            print(
                f"Refusing to print: card {entry.get('id')!r} has a {key!r} "
                "field. cards.json stores password env-var NAMES only "
                "(passwordEnv) — remove the literal value first.",
                file=sys.stderr,
            )
            return 1

    active = [c for c in cards if c.active]
    summary = ", ".join(f"{c.id} ({'active' if c.active else 'inactive'})" for c in cards)
    print(
        f"{len(cards)} card(s), {len(active)} active: {summary}",
        file=sys.stderr,
    )

    needed = sorted({c.passwordEnv for c in active if c.passwordEnv})
    if needed:
        print(
            "Also set on the host: " + ", ".join(needed),
            file=sys.stderr,
        )
    missing_env = [c.id for c in active if not c.passwordEnv]
    if missing_env:
        print(
            f"Warning: no passwordEnv for {missing_env} — those cards will fail "
            "to decrypt at sync time.",
            file=sys.stderr,
        )

    if args.check:
        return 0

    # separators= keeps it compact; a host env var is a single-line field.
    print(json.dumps(raw, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
