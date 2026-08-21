#!/usr/bin/env python3
"""Seam 2b: operator answers participate in aspect resolution.

`aspect_candidate` is currently write-only. `_apply_mapping` resolves from the
model's fresh proposal and *then* records candidates, so anything stored there --
including an operator's answer -- sits in the table, satisfies its foreign keys,
appears in an audit, and changes nothing.

This makes stored operator candidates an input to `analyse`, so they win through
`has_adjudicating_support` exactly as an operator adjudication already does.

**Only operator-basis candidates are merged, deliberately.** Model proposals are
recomputed opinions and should come fresh from the model on every run; reloading
old ones would resurrect values the model has since abandoned and manufacture
contradictions out of its own history. An operator's statement about the object
in their hand is a durable fact, and durable facts are what should persist across
runs. The two are different kinds of thing and this is where that shows.

For the same reason the query spans every identification version for the SKU: a
statement about the object does not expire because the model re-identified it.

Run from the repo root:  python3 patch_operator_candidates.py
"""

from __future__ import annotations

import pathlib
import sys

target = pathlib.Path("src/resell/reasoning/mapping.py")
if not target.exists():
    sys.exit("run this from the repository root")

text = target.read_text()

FUNCTIONS = '''def load_operator_candidates(
    conn: sqlite3.Connection, sku: str
) -> dict[str, list[Candidate]]:
    """Stored candidates whose support is an operator statement.

    Spans identification versions on purpose. An operator answering "40R" was
    describing the object, not the model's current guess about it, so the answer
    survives re-identification. Bases come from the evidence row, never from
    anything a caller asserted.

    Assumes `identification.sku`; if that column is named otherwise, this join is
    the only thing to change.
    """
    rows = conn.execute(
        """
        SELECT ac.aspect_name AS aspect_name, ac.value AS value,
               e.id AS evidence_id, e.basis AS basis
        FROM aspect_candidate ac
        JOIN identification i ON i.id = ac.identification_id
        JOIN aspect_candidate_evidence ace ON ace.candidate_id = ac.id
        JOIN evidence e ON e.id = ace.evidence_id
        WHERE i.sku = ? AND e.basis = ?
        """,
        (sku, str(Basis.OPERATOR)),
    ).fetchall()

    support: dict[tuple[str, str], list[EvidenceRef]] = {}
    for row in rows:
        key = (row["aspect_name"], row["value"])
        support.setdefault(key, []).append(
            EvidenceRef(row["evidence_id"], Basis(row["basis"]))
        )

    out: dict[str, list[Candidate]] = {}
    for (aspect_name, value), refs in support.items():
        out.setdefault(aspect_name, []).append(
            Candidate(value=value, support=tuple(refs))
        )
    return out


def _merge_operator_candidates(
    conn: sqlite3.Connection,
    sku: str,
    resolved: dict[str, list[Candidate]],
    known_aspects: set[str],
) -> dict[str, list[Candidate]]:
    """Fold operator statements into the model's candidates before resolution.

    Same value as one the model proposed: the support is unioned, so that
    candidate gains an adjudicating basis and wins rather than competing with
    itself. Different value: it joins as a competing candidate and wins on
    adjudication. Two operator statements naming different values: both are
    adjudicating, `analyse` finds more than one, and it falls through to
    contradicted -- which is correct, because the operator has contradicted
    themselves and inventing a winner would hide that.

    Aspects outside the category's form are dropped; `analyse` is given a
    cardinality map keyed on the form, and an aspect missing from it has no
    defined behaviour.
    """
    stored = load_operator_candidates(conn, sku)
    if not stored:
        return resolved

    merged = {name: list(candidates) for name, candidates in resolved.items()}
    for aspect_name, operator_candidates in stored.items():
        if aspect_name not in known_aspects:
            continue
        existing = merged.setdefault(aspect_name, [])
        for candidate in operator_candidates:
            match = next(
                (c for c in existing if c.value == candidate.value), None
            )
            if match is None:
                existing.append(candidate)
                continue
            union = {(ref.evidence_id, ref.basis): ref for ref in match.support}
            union.update(
                {(ref.evidence_id, ref.basis): ref for ref in candidate.support}
            )
            existing[existing.index(match)] = Candidate(
                value=match.value, support=tuple(union.values())
            )
    return merged


def map_aspects('''

OLD_DEF = "def map_aspects("
if text.count(OLD_DEF) != 1:
    sys.exit(f"map_aspects definition appears {text.count(OLD_DEF)}x, expected 1")
text = text.replace(OLD_DEF, FUNCTIONS, 1)

OLD_CALL = '''    required_names = [spec.name for spec in specs if spec.required]
    cardinality = {spec.name: spec.cardinality for spec in specs}
    outcomes, gaps = analyse('''
NEW_CALL = '''    required_names = [spec.name for spec in specs if spec.required]
    cardinality = {spec.name: spec.cardinality for spec in specs}
    # Operator statements are inputs to resolution, not audit records written
    # after it. Without this, answering a blocking question changed nothing.
    resolved = _merge_operator_candidates(conn, sku, resolved, set(cardinality))
    outcomes, gaps = analyse('''
if text.count(OLD_CALL) != 1:
    sys.exit(f"analyse call anchor appears {text.count(OLD_CALL)}x, expected 1")
text = text.replace(OLD_CALL, NEW_CALL, 1)

if "ADJUDICATING_BASES" not in text:
    pass  # not needed here; adjudication happens inside gaps.analyse

target.write_text(text)
print("patched src/resell/reasoning/mapping.py")
print("""
Check the imports at the top of mapping.py cover: sqlite3, Basis, Candidate,
EvidenceRef. _rehydrate_basis already uses Basis and EvidenceRef, and the module
takes a conn, so all four are probably present -- but `Candidate` may be imported
from gaps rather than defined locally.

  uv run pytest -q
  uv run python -c "from resell.reasoning.mapping import load_operator_candidates"
""")
