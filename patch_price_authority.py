#!/usr/bin/env python3
"""Seam 1: one authority for listing price.

Three changes, all enforcement. The CLI convenience (dropping --price-cents)
follows separately once the propose command's name is known.

  1. domain.Proposal.canonical() no longer hashes price_cents, so the listing
     approval binds *content*. Repricing therefore cannot void it.

  2. gateway.propose_listing refuses unless the price matches the live approved
     price proposal. That is what actually removes the second authority: before
     this, any caller could type any number.

  3. store_pricing.record_applied syncs listing.price_cents, giving that column
     a single writer and a defined meaning -- the price currently on the
     marketplace -- instead of silently meaning "whatever was typed at propose
     time, forever".

Run from the repo root:  python3 patch_price_authority.py
"""

from __future__ import annotations

import pathlib
import sys

root = pathlib.Path(".")
domain = root / "src/resell/domain.py"
gateway = root / "src/resell/gateway.py"
store = root / "src/resell/store_pricing.py"
for path in (domain, gateway, store):
    if not path.exists():
        sys.exit(f"{path} not found; run from the repository root")


def patch(path: pathlib.Path, edits: list[tuple[str, str]]) -> None:
    text = path.read_text()
    for old, _ in edits:
        found = text.count(old)
        if found != 1:
            sys.exit(f"{path}: anchor appears {found}x, expected 1:\n{old[:100]}")
    for old, new in edits:
        text = text.replace(old, new, 1)
    path.write_text(text)
    print(f"patched {path}")


# --- 1. price leaves the content hash ------------------------------------------

patch(domain, [(
    '''    def canonical(self) -> str:
        """Stable serialisation. Key order and list order must not affect the hash."""
        return json.dumps(
            {
                "sku": self.sku,''',
    '''    def canonical(self) -> str:
        """Stable serialisation. Key order and list order must not affect the hash.

        `price_cents` is deliberately absent. This hash is what a listing approval
        binds, and a listing approval is an approval of *content*: the title, the
        description, the category, the condition, the aspects, the photos and the
        policies. Price has its own approval with its own hash in the pricing
        layer, and it is expected to change after publication.

        Hashing price here would mean every markdown voided the approval of a
        title nobody had touched, which is the coupling the two-approval design
        exists to remove. The field remains on the Proposal -- validate() still
        checks it against the publication floor, and the listing row still stores
        it -- it simply is not part of what the operator approved.
        """
        return json.dumps(
            {
                "sku": self.sku,'''
), (
    '''                "aspects": {k: sorted(v) for k, v in sorted(self.aspects.items())},
                "price_cents": self.price_cents,
                "currency": self.currency,''',
    '''                "aspects": {k: sorted(v) for k, v in sorted(self.aspects.items())},
                "currency": self.currency,'''
)])


# --- 2. the gateway enforces the pricing layer's answer ------------------------

patch(gateway, [(
    '''        if proposal.sku != sku:
            reasons.append(f"proposal sku {proposal.sku} does not match {sku}")''',
    '''        if proposal.sku != sku:
            reasons.append(f"proposal sku {proposal.sku} does not match {sku}")

        # The pricing layer owns price. Before this check, `item propose
        # --price-cents` and `price approve` were two independent authorities over
        # the same number and nothing reconciled them -- whichever the publisher
        # read is what reached eBay, and the other was decoration.
        #
        # Imported here rather than at module scope to keep the gateway's import
        # graph shallow; the pricing layer is a peer, not a dependency of the
        # state machine itself.
        from resell.store_pricing import approved_price_cents

        approved = approved_price_cents(self.conn, sku)
        if approved is None:
            reasons.append(
                "no approved price for this item; price it first with "
                "`resell price recommend`, `resell price propose --objective ...` "
                "and `resell price approve`"
            )
        elif proposal.price_cents != approved:
            reasons.append(
                f"price {proposal.price_cents} does not match the approved price "
                f"{approved}; approve a new price rather than typing a different one"
            )'''
)])


# --- 3. listing.price_cents gets one writer and one meaning --------------------

patch(store, [(
    "def already_applied(",
    '''def approved_price_cents(conn: sqlite3.Connection, sku: str) -> int | None:
    """The price this item currently has approval to charge, or None.

    The most recent proposal carrying a live approval that still covers it. A
    proposal whose content changed after approval does not count -- the approval
    is bound to a hash, and a hash that no longer matches is not an approval.
    """
    rows = conn.execute(
        "SELECT proposal_id FROM price_proposal WHERE sku = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (sku,),
    ).fetchall()
    for row in rows:
        proposal = load_proposal(conn, row["proposal_id"])
        if proposal is None:
            continue
        approval = live_approval(conn, proposal.proposal_id)
        if approval is not None and approval.covers(proposal):
            return proposal.price_cents
    return None


def _sync_listing_price(conn: sqlite3.Connection, sku: str, price_cents: int) -> None:
    """Keep listing.price_cents meaning "the price currently on the marketplace".

    That column is read by the publisher when building an offer, so leaving it at
    whatever was proposed originally means a relist would send a price the item
    has not carried for weeks. It is a derived cache of the pricing layer's
    answer, and this is its only writer.

    estimated_fees_cents is recomputed from fee_rate_used and fee_fixed_cents_used
    -- the columns stored beside it at proposal time -- so the row stays internally
    consistent rather than pairing a new price with fees for the old one.
    """
    row = conn.execute(
        "SELECT buyer_shipping_charge_cents, fee_rate_used, fee_fixed_cents_used "
        "FROM listing WHERE sku = ? AND active = 1",
        (sku,),
    ).fetchone()
    if row is None:
        return

    fees = None
    if row["fee_rate_used"] is not None:
        base = price_cents + (row["buyer_shipping_charge_cents"] or 0)
        fees = round(base * row["fee_rate_used"]) + (row["fee_fixed_cents_used"] or 0)

    conn.execute(
        "UPDATE listing SET price_cents = ?, estimated_fees_cents = ?, updated_at = ? "
        "WHERE sku = ? AND active = 1",
        (price_cents, fees, _now(), sku),
    )


def already_applied('''
), (
    '''    _event(conn, proposal.sku, proposal.proposal_id, PriceEventType.APPLIED,
           proposal.price_cents, marketplace_ref=marketplace_ref)
    _set_state(conn, proposal.sku, PriceState.LIVE,
               proposal_id=proposal.proposal_id, price_cents=proposal.price_cents)
    conn.commit()''',
    '''    _event(conn, proposal.sku, proposal.proposal_id, PriceEventType.APPLIED,
           proposal.price_cents, marketplace_ref=marketplace_ref)
    _set_state(conn, proposal.sku, PriceState.LIVE,
               proposal_id=proposal.proposal_id, price_cents=proposal.price_cents)
    _sync_listing_price(conn, proposal.sku, proposal.price_cents)
    conn.commit()'''
)])

print("""
Done. What changed:

  - listing approvals no longer cover price, so a reprice cannot void one
  - item propose refuses unless the price matches the live approved price
  - price apply now updates listing.price_cents (and its estimated fees)

Not yet done: --price-cents is still required on `item propose`. It now has to
match the approved price, so the duplicate authority is closed; the convenience
of omitting it needs the CLI propose function, which grep did not find.

Expect test churn: every test calling item propose in isolation now needs an
approved price proposal first. Run `uv run pytest -q` and send the failures.

MP-000003's listing row is still stale at 19520. Fix it by replaying the apply:
  uv run resell price apply price_5234817c33dc --from-listing
which will report reconciled or no_change, and sync the row.
""")
