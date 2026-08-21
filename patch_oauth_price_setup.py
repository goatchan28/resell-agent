#!/usr/bin/env python3
"""Give test_oauth.py's proposal helpers the approved price the gateway now requires.

Two helpers build listing proposals: `_valid_proposal` at 1999 cents, and
`_build_approved` at 8900. Both now need a matching approved price proposal in
the pricing layer, because propose_listing refuses without one.

`_valid_proposal` only constructs a Proposal -- it has no connection and no SKU
lifecycle -- so the pricing setup goes into a separate helper that each call site
invokes. `_build_approved` owns its whole sequence, so it calls it inline.

Run from the repo root:  python3 patch_oauth_price_setup.py
"""

from __future__ import annotations

import pathlib
import re
import sys

tests = pathlib.Path("tests/test_oauth.py")
if not tests.exists():
    sys.exit("run this from the repository root")

text = tests.read_text()

HELPER = '''def _approve_price(conn, sku: str, price_cents: int) -> None:
    """The minimum pricing-layer setup a listing proposal now requires.

    propose_listing reads the approved price and refuses to accept a listing
    proposal that disagrees with it, so price is a precondition of listing
    content rather than something typed alongside it. These tests are about
    approvals, photos and publishing -- they need the price to exist, not to be
    interesting, so this is the smallest thing that satisfies the gate.
    """
    from datetime import datetime, timezone

    from resell import store_pricing as sp
    from resell.pricing.lifecycle import PriceProposal, PriceReason
    from resell.pricing.proceeds import FeeBasis

    proposal = PriceProposal(
        proposal_id=f"pp_{sku}_{price_cents}",
        sku=sku,
        reason=PriceReason.INITIAL,
        price_cents=price_cents,
        created_at=datetime.now(timezone.utc),
        fee_basis=FeeBasis.CATEGORY_VERIFIED,
        floor_ok=True,
    )
    sp.record_proposal(conn, proposal)
    sp.approve_proposal(conn, proposal)


def _valid_proposal(sku: str, photo_hashes: tuple[str, ...]):'''

OLD_HELPER = "def _valid_proposal(sku: str, photo_hashes: tuple[str, ...]):"
if text.count(OLD_HELPER) != 1:
    sys.exit(f"_valid_proposal anchor appears {text.count(OLD_HELPER)}x")
text = text.replace(OLD_HELPER, HELPER, 1)

# --- _build_approved prices its own item -------------------------------------

OLD_BUILD = '''    gateway.begin_pricing(sku)
    proposal = Proposal(
        sku=sku, marketplace="EBAY_US", title="Blazer 42R", description="Navy wool.",'''
NEW_BUILD = '''    gateway.begin_pricing(sku)
    _approve_price(conn, sku, 8900)
    proposal = Proposal(
        sku=sku, marketplace="EBAY_US", title="Blazer 42R", description="Navy wool.",'''
if text.count(OLD_BUILD) != 1:
    sys.exit(f"_build_approved anchor appears {text.count(OLD_BUILD)}x")
text = text.replace(OLD_BUILD, NEW_BUILD, 1)

# --- every _valid_proposal call site gets a priced item ----------------------
#
# Inserted immediately before the propose_listing call rather than at the point
# the SKU is created, so the pricing row lands after begin_pricing and the item
# is in a state that can carry a price.

# Greedy `.+` before the trailing `))` rather than `[^)]*`: the photo tuple
# contains parentheses of its own -- `(_digest("a"),)` -- and a negated class
# stops at the first one, which silently matched a quarter of the call sites.
pattern = re.compile(
    r"^(?P<indent>[ \t]*)(?P<call>(?:\w+ = )?gateway\.propose_listing\("
    r"(?P<sku>\w+), _valid_proposal\((?P=sku), .+\)\))\s*$",
    re.MULTILINE,
)

def add_price(match: re.Match) -> str:
    """Insert the setup line above; never rewrite the call itself."""
    indent = match.group("indent")
    return (
        f"{indent}_approve_price(conn, {match.group('sku')}, 1999)\n"
        f"{indent}{match.group('call')}"
    )

text, single_line = pattern.subn(add_price, text)

print(f"patched {single_line} single-line propose_listing call(s)")
tests.write_text(text)

# --- report what still needs doing by hand ------------------------------------

remaining = [
    n for n, line in enumerate(text.splitlines(), 1)
    if "propose_listing(" in line and "_approve_price" not in line
]
multiline = [
    n for n in remaining
    if not re.search(r"_valid_proposal\(\w+, [^)]*\)\)", text.splitlines()[n - 1])
]
if multiline:
    print("\nThese propose_listing calls span multiple lines or build their own")
    print("Proposal, so they need _approve_price(conn, sku, PRICE) added by hand")
    print("immediately before them, with PRICE matching the proposal:")
    for n in multiline:
        print(f"  tests/test_oauth.py:{n}: {text.splitlines()[n - 1].strip()[:80]}")

print("""
Two tests to read rather than assume about, since adding a price could mask what
they assert:
  test_rejected_commands_leave_no_trace
  test_preconditions_are_enforced_on_state_entry
If either was relying on ProposeListing failing, the missing price is now one of
the reasons it fails, and the assertion may need to name a different one.
""")
