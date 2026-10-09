"""What the identification claims, and whether the record earns it.

This module used to hold a three-stage research loop -- plan, retrieve, judge --
with two model calls at the ends. That loop is gone; [identity.py](identity.py)
decides identity deterministically now, and the history behind that change is
written up there. What remains here is the half that always worked: the gate that
checks a declared mode against stored facts, and the derivation of whether anyone
ever resolved the identifiers to a real product.

The rule this module exists to enforce is unchanged, and outlived the loop:
**confidence is not authority.** What an identification is permitted to claim is
computed from things nothing in the reasoning plane controls -- whether a brand was
read off the object, whether a product-denoting code was, whether an external
source confirmed it, and where that source came from.
"""

from __future__ import annotations

from dataclasses import dataclass

from resell.db import log_event


class ResearchLoopError(RuntimeError):
    """Raised when an identification round cannot proceed."""


@dataclass
class ModeDecision:
    proposed: str
    accepted: str
    supported: bool
    reason: str


def identity_resolution(conn, sku: str):
    """Whether the identifiers were ever resolved to a real product.

    Computed from the record, never stored as an opinion.

    One way to be RESOLVED: a match of identifier strength, marked `is_match`, from
    a source good enough that `donation_scope` lets it contribute. That is the
    original rule and it is deliberately the only one.

    A corroboration branch briefly lived here -- two independent domains naming the
    same identifier -- on the reasoning that *which product this is* and *whose
    description may attach to it* are different questions. The reasoning still
    looks right and the rule was not: replayed against all 54 historical items it
    resolved 13 and got 6 of them wrong, including a suit jacket resolved as a
    sewage pump. See `EXACT_RESOLUTION_SHIPPED` in
    [identity.py](identity.py) for the full account.

    So this stays narrow, and `is_match` is the seam: nothing sets it while exact
    resolution is held closed, which makes RESOLVED unreachable by construction
    rather than by a flag somebody could flip. The comparability ceiling therefore
    holds at `same_family_variant`, exactly where the LLM research system left it
    after resolving 0 of 57 items -- so waiting costs nothing that was ever
    available.
    """
    from resell.reasoning.schema import IdentityResolution

    qualifying = conn.execute(
        "SELECT COUNT(*) FROM product_match WHERE sku = ? AND is_match = 1 "
        "AND donation_scope IS NOT NULL AND donation_scope != 'none' "
        "AND strength IN ('identifier_verified', 'identifier_asserted')",
        (sku,),
    ).fetchone()[0]
    if qualifying:
        return IdentityResolution.RESOLVED

    attempted = conn.execute(
        "SELECT COUNT(*) FROM research_lookup WHERE sku = ? AND scope = 'identity'",
        (sku,),
    ).fetchone()[0]
    return (
        IdentityResolution.SEARCHED_NOT_FOUND if attempted
        else IdentityResolution.UNATTEMPTED
    )


def mode_evidence(conn, sku: str) -> dict:
    """Assemble what the mode gate needs, from structured facts only.

    This used to run `LIKE '%brand%'` and `LIKE '%line%'` over observation prose,
    which meant a mode depended on whether the vision model happened to use a
    particular English word. It was wrong in both directions on real items:
    MP-000057's brand *is* `Dell` and it scored zero brand support because no
    observation contained the string "brand"; MP-000058's only "line" support was
    the sentence *"the right eyebrow is a short straight horizontal black line"*.
    MP-000056 cited its own `research not pursued` summary back to itself.

    So both are read from the record instead. The brand is a field on the
    identification, and the evidence for it is the observations that actually name
    that brand -- a search for `Swingline`, not for the word "brand". A product
    line is a product-denoting identifier, which `observe` already classifies by
    scheme; a `makers_mark` is a brand and a `serial` denotes one unit, and neither
    establishes a family.
    """
    from resell.reasoning.identity import observed_brand, strong_identifiers
    from resell.reasoning.schema import (
        Basis, EvidenceRef, IdentificationEffort, IdentityResolution,
    )

    def naming(value: str) -> tuple:
        """Observations of this item that name a specific string."""
        if not value.strip():
            return ()
        return tuple(
            EvidenceRef(row["id"], Basis(row["basis"] or "inference"))
            for row in conn.execute(
                "SELECT id, basis FROM evidence WHERE sku = ? AND subject = 'this_item' "
                "AND kind IN ('vision_observation', 'identifier_observation') "
                "AND lower(payload) LIKE ?",
                (sku, f"%{value.strip().casefold()}%"),
            )
        )

    finding = None
    stored = db_kv_negative(conn, sku)
    if stored:
        from resell.reasoning.schema import NegativeFinding

        finding = NegativeFinding(
            surfaces_examined=tuple(stored.get("surfaces_examined") or ()),
            photos_reviewed=int(stored.get("photos_reviewed", 0)),
            note=str(stored.get("note", "")),
        )

    identification = conn.execute(
        "SELECT brand, model FROM identification WHERE sku = ? AND superseded_at IS NULL",
        (sku,),
    ).fetchone()
    brand = ((identification["brand"] if identification else None) or "").strip()
    model = ((identification["model"] if identification else None) or "").strip()
    # The same fallback `tier_for` uses, for the same reason: this gate runs before
    # `map_aspects` writes `identification.brand`, so at the moment it matters that
    # column is empty. Supplying the brand to the tier and not to the gate is worse
    # than supplying it to neither -- the tier proposed `branded_generic`, the gate
    # could not cite a brand for it, and every item in the replay came out
    # `unresolved`, which is the one mode that still asks the seller a question.
    if not brand:
        brand = (observed_brand(conn, sku) or "").strip()

    # A product-denoting code establishes a family on its own; so does a model the
    # identification carries, when an observation actually names it.
    line = tuple(
        EvidenceRef(identifier.evidence_id, Basis.TEXT_READ)
        for identifier in strong_identifiers(conn, sku)
    ) + naming(model)

    return {
        "effort": IdentificationEffort(
            conn.execute(
                "SELECT identification_effort FROM item WHERE sku = ?", (sku,)
            ).fetchone()[0]
        ),
        "negative_finding": finding,
        "brand_support": naming(brand),
        "line_support": tuple(dict.fromkeys(line)),
        # The same question `identity_resolution` answers, asked once. It used to
        # be a second copy of the donation-based half of that query, so once
        # corroboration could resolve an identity the two disagreed: a live replay
        # produced items reading `resolution=resolved, mode=unresolved`, which is
        # not a position anything downstream knows how to read.
        "qualifying_match": identity_resolution(conn, sku) is IdentityResolution.RESOLVED,
    }


def declare_mode(conn, gateway, sku: str, proposed: str, rationale: str) -> ModeDecision:
    """Record the identification mode, if the evidence earns it.

    An unsupported proposal is refused rather than downgraded to whatever would have
    passed -- silently accepting a lesser mode would make the declaration look
    considered when it was salvaged. The modes that ARE supported are named, so the
    refusal is actionable.

    Nothing proposes a mode from a model call any more. The tier does, and a tier
    is computed from the same evidence this gate checks, so a refusal now means the
    tiering and the gate genuinely disagree -- which is worth seeing, rather than
    the routine event it was when a planner guessed `exact_product` 21 times in a
    row and was refused 21 times.
    """
    from resell.reasoning.gaps import mode_is_supported, supported_modes
    from resell.reasoning.schema import IdentificationMode

    try:
        mode = IdentificationMode(proposed)
    except ValueError:
        return ModeDecision(proposed, str(IdentificationMode.UNRESOLVED), False,
                            f"unknown mode {proposed!r}")

    evidence = mode_evidence(conn, sku)
    supported, why = mode_is_supported(mode, **evidence)
    available = [str(m) for m in supported_modes(**evidence)]

    if not supported:
        why = (
            f"{why} Supported by the current evidence: "
            f"{', '.join(available) if available else 'nothing beyond unresolved'}."
        )

    accepted = mode if supported else IdentificationMode.UNRESOLVED
    resolution = identity_resolution(conn, sku)

    # Through the shared merge rather than a column list of our own.
    #
    # This stage does not change the identification -- it records the mode and the
    # resolution against it -- so everything must survive the new version intact.
    # It used to carry the fields forward by name, and the name it did not know
    # about was `category_path`: written by `suggest_category` at v1 and gone from
    # v2 onward on every item, because this list was written before that column
    # existed. What it costs is invisible until pricing, where the path chooses
    # the retention rate a retail-derived anchor is reasoned down with.
    #
    # `merged_identification` is that list, maintained in one place, and its own
    # docstring says why -- "One implementation, so that cannot happen again."
    # This was the second implementation.
    from resell.cli_item import merged_identification

    fields, _ = merged_identification(conn, sku)
    gateway.propose_identification(sku, **fields)
    conn.execute(
        "UPDATE identification SET mode = ?, mode_rationale = ?, identity_resolution = ? "
        "WHERE sku = ? AND superseded_at IS NULL",
        (str(accepted), f"{rationale[:1500]}\n\n[gate] {why}", str(resolution), sku),
    )
    log_event(
        conn, "identification.mode_declared",
        {"proposed": str(mode), "accepted": str(accepted), "supported": supported,
         "resolution": str(resolution), "supported_modes": available, "why": why},
        item_id=sku,
    )
    return ModeDecision(str(mode), str(accepted), supported, why)


def db_kv_negative(conn, sku) -> dict | None:
    """The identity-search summary recorded by the observation stage, if any."""
    import json as _json

    from resell.db import kv_get

    raw = kv_get(conn, f"identity_search:{sku}")
    if not raw:
        return None
    try:
        return _json.loads(raw)
    except ValueError:
        return None
