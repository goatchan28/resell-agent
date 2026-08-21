#!/usr/bin/env python3
"""Price the verify-safeguards fixture, and probe the new price-authority rule.

Two changes to cmd_item_verify_safeguards:

  The fixture is priced before it proposes a listing. Without this the command
  fails on its own setup -- which is the exact failure its docstring exists to
  describe: a check whose premise was never established. The price is seeded from
  proposal.price_cents rather than a second literal, so the two cannot drift.

  Two new probes, placed where their premises are naturally true rather than
  contrived. Before the price is seeded, "this item has no approved price" is a
  fact; after it, "8900 is the only approved price" is a fact. Each probe asserts
  the gateway refuses, and a rejected command leaves no trace, so the fixture is
  unharmed and the real proposal follows.

Run from the repo root:  python3 patch_verify_safeguards.py
"""

from __future__ import annotations

import pathlib
import sys

target = pathlib.Path("src/resell/cli_item.py")
if not target.exists():
    sys.exit("run this from the repository root")

text = target.read_text()

OLD = '''    accepted = gateway.propose_listing(sku, proposal)
    good_hash = accepted.data["proposal_hash"]'''

NEW = '''    # --- price authority -----------------------------------------------------
    # The pricing layer owns price. Both probes run here because this is where
    # their premises hold without being manufactured.

    def seed_approved_price(cents: int) -> None:
        from datetime import datetime, timezone

        from resell import store_pricing as sp
        from resell.pricing.lifecycle import PriceProposal, PriceReason
        from resell.pricing.proceeds import FeeBasis

        priced = PriceProposal(
            proposal_id=f"pp_{sku}",
            sku=sku,
            reason=PriceReason.INITIAL,
            price_cents=cents,
            created_at=datetime.now(timezone.utc),
            fee_basis=FeeBasis.CATEGORY_VERIFIED,
            floor_ok=True,
            rationale="verify-safeguards fixture",
        )
        sp.record_proposal(conn, priced)
        sp.approve_proposal(conn, priced)

    check(
        "listing proposed with no approved price",
        lambda: gateway.propose_listing(sku, proposal),
    )

    seed_approved_price(proposal.price_cents)

    check(
        "listing proposed at a price the pricing layer never approved",
        lambda: gateway.propose_listing(
            sku, dataclasses.replace(proposal, price_cents=proposal.price_cents + 100)
        ),
    )

    accepted = gateway.propose_listing(sku, proposal)
    good_hash = accepted.data["proposal_hash"]'''

if text.count(OLD) != 1:
    sys.exit(f"anchor appears {text.count(OLD)}x, expected 1")
text = text.replace(OLD, NEW, 1)

# dataclasses.replace is used by the second probe
if "\nimport dataclasses" not in text and "import dataclasses\n" not in text:
    OLD_IMPORT = "    import sqlite3\n\n    config, conn, gateway = _open()"
    NEW_IMPORT = (
        "    import dataclasses\n    import sqlite3\n\n"
        "    config, conn, gateway = _open()"
    )
    if text.count(OLD_IMPORT) != 1:
        sys.exit(
            "could not place the dataclasses import; add `import dataclasses` "
            "inside cmd_item_verify_safeguards by hand"
        )
    text = text.replace(OLD_IMPORT, NEW_IMPORT, 1)

target.write_text(text)
print("patched src/resell/cli_item.py")
print("  - fixture is priced before proposing a listing")
print("  - two new safeguard probes for the price-authority rule")
print("\nExpect two extra PASS lines from: uv run resell item verify-safeguards")
