#!/usr/bin/env python3
"""`item propose` reads the approved price instead of being told it.

The gateway already refuses any listing price the pricing layer did not approve,
so the number typed here has exactly one legal value. Typing a value with one
legal answer is a transcription step, and transcription steps are where digits
get dropped -- MP-000003 was published from a hand-copied $195.20.

Enforcement is unchanged. propose_listing still checks, and if the CLI and the
gateway ever disagree the gateway wins. This only removes the typing.

  omitted           -> read the approved price, print where it came from
  given, matching   -> proceed; scripts may still assert the number
  given, differing  -> refuse here, with the gateway's own wording
  omitted, no price -> refuse, pointing at `resell price propose`

Run from the repo root:  python3 patch_propose_price_optional.py
"""

from __future__ import annotations

import pathlib
import sys

target = pathlib.Path("src/resell/cli_item.py")
if not target.exists():
    sys.exit("run this from the repository root")

text = target.read_text()


def swap(old: str, new: str, what: str) -> None:
    global text
    if text.count(old) != 1:
        sys.exit(f"{what}: anchor appears {text.count(old)}x, expected 1")
    text = text.replace(old, new, 1)


# --- resolve the price before building the Proposal ---------------------------

swap(
    '''    def policy(key: str) -> str:
        return db.kv_get(conn, f"ebay.{key}:{config.env.name}") or ""
    proposal = Proposal(''',
    '''    def policy(key: str) -> str:
        return db.kv_get(conn, f"ebay.{key}:{config.env.name}") or ""

    # Price comes from the pricing layer. Refusing here as well as in the gateway
    # is not belt and braces -- it means the operator sees the problem before a
    # Proposal is built, and the message names the same cause either way.
    from resell.store_pricing import approved_price_cents

    approved = approved_price_cents(conn, args.sku)
    if args.price_cents is None:
        if approved is None:
            print(
                f"no approved price for {args.sku}. Price it first:\\n"
                f"  resell price recommend {args.sku} ...\\n"
                f"  resell price propose {args.sku} ... --objective OBJECTIVE\\n"
                f"  resell price approve PROPOSAL_ID",
                file=sys.stderr,
            )
            return 2
        price_cents = approved
        print(f"price ${approved / 100:.2f} from the approved price proposal")
    else:
        if approved is not None and args.price_cents != approved:
            print(
                f"price {args.price_cents} does not match the approved price "
                f"{approved}; approve a new price rather than typing a different "
                f"one, or omit --price-cents to use the approved one",
                file=sys.stderr,
            )
            return 2
        price_cents = args.price_cents

    proposal = Proposal(''',
    "price resolution",
)

swap(
    "        price_cents=args.price_cents,",
    "        price_cents=price_cents,",
    "Proposal price field",
)

# --- the flag stops being required --------------------------------------------

for old, new, what in [
    (
        '''    c.add_argument("--price-cents", type=int, required=True)''',
        '''    c.add_argument(
        "--price-cents", type=int,
        help="defaults to the approved price; given, it must match",
    )''',
        "propose parser (spaced form)",
    ),
]:
    if text.count(old) == 1:
        text = text.replace(old, new, 1)
        print(f"  updated {what}")
        break
else:
    # The parser line may be written differently; report rather than guess.
    print(
        "  NOTE: could not find `--price-cents ... required=True` on the item\n"
        "        propose parser. Remove `required=True` from it by hand:\n"
        "          grep -n 'price-cents' src/resell/cli_item.py"
    )

# --- the next-step hint reflects the new order --------------------------------

swap(
    '''    "pricing": ("propose a price",
                "resell item propose {sku} --price-cents N --seller-shipping-cents N"),''',
    '''    "pricing": ("approve a price, then propose the listing",
                "resell price recommend {sku} ... && resell price approve ID, "
                "then resell item propose {sku} --seller-shipping-cents N"),''',
    "next-step hint",
)

target.write_text(text)
print("patched src/resell/cli_item.py")
print("""
`item propose` now reads the approved price when --price-cents is omitted.
Check the parser change landed, then:

  uv run pytest -q
  uv run resell item propose --help
""")
