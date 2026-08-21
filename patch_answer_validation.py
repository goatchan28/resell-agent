#!/usr/bin/env python3
"""Seam 2a: operator answers are validated, then enter the candidate pipeline.

Four changes:

  domain.values_not_in_allowed  -- the casefolded comparison, extracted so the
  publisher and the gateway share one implementation rather than two that drift.

  open_question.allowed_values_json -- the values eBay listed when the question
  was asked. Stored so the gateway can validate offline: Taxonomy stays the
  source of truth, but the state machine never makes a network call, and gaps.py
  never learns about marketplace constraints.

  ask_operator(allowed_values=...) -- the publisher passes the list when the
  aspect is SELECTION_ONLY.

  answer_question -- refuses a value outside the list unless the operator says
  it is not listed, returns the evidence id, and records an aspect_candidate
  citing it. That last part is what makes answering a question resolve an aspect
  instead of just filing a note.

The override is recorded, not merely permitted. eBay does not guarantee
aspectValues is exhaustive -- publisher.py:120 says so -- so refusing outright
would block legitimate answers. The evidence payload carries the override flag
and the list as it stood, which makes the disagreement an auditable operator
decision rather than a silent bypass.

Run from the repo root:  python3 patch_answer_validation.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

root = pathlib.Path(".")
paths = {
    "domain": root / "src/resell/domain.py",
    "publisher": root / "src/resell/ebay/publisher.py",
    "db": root / "src/resell/db.py",
    "gateway": root / "src/resell/gateway.py",
}
for name, path in paths.items():
    if not path.exists():
        sys.exit(f"{path} not found; run from the repository root")


def swap(path: pathlib.Path, old: str, new: str, what: str) -> None:
    text = path.read_text()
    if text.count(old) != 1:
        sys.exit(f"{what}: anchor appears {text.count(old)}x, expected 1")
    path.write_text(text.replace(old, new, 1))


# --- 1. one comparison, two callers -------------------------------------------

domain_text = paths["domain"].read_text()
if "def values_not_in_allowed" not in domain_text:
    paths["domain"].write_text(domain_text.rstrip("\n") + '''


def values_not_in_allowed(
    allowed: tuple[str, ...] | list[str], supplied: list[str]
) -> list[str]:
    """Supplied values absent from an aspect's allowed list, compared casefolded.

    Extracted from AspectSpec.unknown_values so the publisher's warning and the
    gateway's refusal apply the same rule. Two implementations of "is this value
    allowed" would eventually disagree, and the one that disagreed quietly would
    be the one that let a bad value through.

    An empty allowed list yields nothing: FREE_TEXT aspects and aspects eBay
    publishes no values for are unconstrained, and validating against an empty
    list would refuse every answer.
    """
    if not allowed:
        return []
    known = {value.casefold() for value in allowed}
    return [value for value in supplied if value.casefold() not in known]
''')
    print("added domain.values_not_in_allowed")

# --- 2. the publisher delegates and stores the list ---------------------------

swap(
    paths["publisher"],
    '''        if not self.selection_only or not self.allowed_values:
            return []
        allowed = {value.casefold() for value in self.allowed_values}
        return [value for value in supplied if value.casefold() not in allowed]''',
    '''        if not self.selection_only:
            return []
        from resell.domain import values_not_in_allowed

        return values_not_in_allowed(self.allowed_values, supplied)''',
    "AspectSpec.unknown_values",
)

swap(
    paths["publisher"],
    '''            try:
                self.gateway.ask_operator(
                    sku,
                    question=f"What is this item's {name}?",''',
    '''            spec = next(
                (s for s in (self._last_schema or ()) if s.name == name), None
            )
            # Only SELECTION_ONLY aspects constrain the answer. Storing a list for
            # a FREE_TEXT aspect would refuse every value the operator typed.
            allowed = (
                tuple(spec.allowed_values)
                if spec is not None and spec.selection_only
                else ()
            )
            try:
                self.gateway.ask_operator(
                    sku,
                    question=f"What is this item's {name}?",
                    allowed_values=allowed,''',
    "publisher question opens with allowed values",
)

# --- 3. the column, appended to the end of MIGRATIONS -------------------------
#
# Appended by locating the list through the syntax tree. Inserting mid-list would
# renumber every migration after it, and user_version has already passed those --
# every existing database would silently skip one and apply another twice.

db_text = paths["db"].read_text()
if "allowed_values_json" in db_text:
    print("db.py already has allowed_values_json; skipping")
else:
    tree = ast.parse(db_text)
    node = None
    for item in tree.body:
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "MIGRATIONS":
                    node = item.value
    if node is None or not isinstance(node, (ast.List, ast.Tuple)):
        sys.exit("could not find the MIGRATIONS list; add the column by hand")

    lines = db_text.splitlines(keepends=True)
    end_line = node.end_lineno - 1          # 0-based, the line holding ] or )
    closing = lines[end_line]
    indent = " " * (len(closing) - len(closing.lstrip()) + 4)
    entry = (
        f'{indent}# Values eBay listed for the aspect when the question was asked,\n'
        f'{indent}# so an answer can be validated without a Taxonomy call.\n'
        f'{indent}("ALTER TABLE open_question ADD COLUMN allowed_values_json TEXT",),\n'
    )
    lines.insert(end_line, entry)
    paths["db"].write_text("".join(lines))
    print(f"appended migration at line {end_line + 1} of db.py (end of MIGRATIONS)")

# --- 4. ask_operator carries the list -----------------------------------------

swap(
    paths["gateway"],
    '''    def ask_operator(
        self, sku: str, *, question: str, why_it_matters: str = "",
        blocking: bool = True, aspect_name: str | None = None,
    ) -> Accepted:
        """The operator-as-tool call. A blocking question moves the item to needs_info."""
        if not question.strip():
            raise Rejected("AskOperator", ["question is empty"])
        get_item(self.conn, sku)
        self.conn.execute(
            "INSERT INTO open_question (sku, question, why_it_matters, blocking, "
            "asked_at, aspect_name) VALUES (?, ?, ?, ?, ?, ?)",
            (sku, question, why_it_matters, 1 if blocking else 0, now_iso(), aspect_name),
        )''',
    '''    def ask_operator(
        self, sku: str, *, question: str, why_it_matters: str = "",
        blocking: bool = True, aspect_name: str | None = None,
        allowed_values: tuple[str, ...] | None = None,
    ) -> Accepted:
        """The operator-as-tool call. A blocking question moves the item to needs_info.

        `allowed_values` is eBay's list for the aspect, captured now so the answer
        can be checked later without a Taxonomy call. Empty for FREE_TEXT aspects
        and for anything that is not about an aspect at all.
        """
        if not question.strip():
            raise Rejected("AskOperator", ["question is empty"])
        get_item(self.conn, sku)
        self.conn.execute(
            "INSERT INTO open_question (sku, question, why_it_matters, blocking, "
            "asked_at, aspect_name, allowed_values_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sku, question, why_it_matters, 1 if blocking else 0, now_iso(), aspect_name,
             json.dumps(list(allowed_values)) if allowed_values else None),
        )''',
    "ask_operator",
)

# --- 5. answering validates, records evidence, and creates the candidate ------

swap(
    paths["gateway"],
    '''    def answer_question(self, question_id: int, answer: str, *, operator: bool = False) -> Accepted:
        """Operator-only. Answering own questions would defeat the whole loop."""''',
    '''    def answer_question(
        self, question_id: int, answer: str, *, operator: bool = False,
        value_not_listed: bool = False,
    ) -> Accepted:
        """Operator-only. Answering own questions would defeat the whole loop.

        When the question carries eBay's allowed values, an answer outside them is
        refused. `value_not_listed` overrides that -- eBay does not guarantee its
        value list is exhaustive -- and the override is written into the evidence
        payload along with the list as it stood, so a disagreement with eBay is an
        operator decision on the record rather than an invisible exception.

        An answer about an aspect also becomes an aspect_candidate citing this
        evidence row, which is what lets resolution treat it as an adjudication
        instead of filing it and carrying on.
        """''',
    "answer_question signature",
)

swap(
    paths["gateway"],
    '''        sku = row["sku"]
        self.conn.execute(
            "UPDATE open_question SET answer = ?, answered_at = ? WHERE id = ?",
            (answer, now_iso(), question_id),
        )
        # basis='operator' is what lets this answer adjudicate a contradiction later.
        self.conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
            "recorded_at, basis, subject) "
            "VALUES (?, 'operator_answer', 'operator', ?, 1, ?, ?, ?)",
            (sku, json.dumps({"question": row["question"], "answer": answer}), now_iso(),
             str(Basis.OPERATOR), str(Subject.THIS_ITEM)),
        )''',
    '''        sku = row["sku"]
        value = answer.strip()

        keys = row.keys()
        allowed = (
            json.loads(row["allowed_values_json"])
            if "allowed_values_json" in keys and row["allowed_values_json"]
            else []
        )
        from resell.domain import values_not_in_allowed

        unlisted = values_not_in_allowed(allowed, [value]) if allowed else []
        if unlisted and not value_not_listed:
            shown = ", ".join(allowed[:12])
            more = "" if len(allowed) <= 12 else f", and {len(allowed) - 12} more"
            raise Rejected("AnswerQuestion", [
                f"{value!r} is not one of the values eBay lists for "
                f"{row['aspect_name']}",
                f"eBay accepts: {shown}{more}",
                "if that list is incomplete, pass value_not_listed to record the "
                "answer as an explicit operator override",
            ])

        self.conn.execute(
            "UPDATE open_question SET answer = ?, answered_at = ? WHERE id = ?",
            (answer, now_iso(), question_id),
        )

        payload = {"question": row["question"], "answer": answer}
        if unlisted:
            # The override is the record, not the permission.
            payload["value_not_listed"] = True
            payload["allowed_values_at_answer"] = allowed
        # basis='operator' is what lets this answer adjudicate a contradiction later.
        cursor = self.conn.execute(
            "INSERT INTO evidence (sku, kind, source, payload, send_to_model, "
            "recorded_at, basis, subject) "
            "VALUES (?, 'operator_answer', 'operator', ?, 1, ?, ?, ?)",
            (sku, json.dumps(payload), now_iso(),
             str(Basis.OPERATOR), str(Subject.THIS_ITEM)),
        )
        evidence_id = cursor.lastrowid

        aspect_name = row["aspect_name"] if "aspect_name" in keys else None
        if aspect_name:
            identification = current_identification(self.conn, sku)
            if identification is not None:
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO aspect_candidate "
                    "(identification_id, aspect_name, value, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (identification["id"], aspect_name, value, now_iso()),
                )
                candidate_id = cursor.lastrowid or self.conn.execute(
                    "SELECT id FROM aspect_candidate WHERE identification_id = ? "
                    "AND aspect_name = ? AND value = ?",
                    (identification["id"], aspect_name, value),
                ).fetchone()[0]
                self.conn.execute(
                    "INSERT OR IGNORE INTO aspect_candidate_evidence "
                    "(candidate_id, evidence_id) VALUES (?, ?)",
                    (candidate_id, evidence_id),
                )''',
    "answer_question body",
)

print("""
patched. Still to do by hand -- I do not have the parser or command function:

  `resell item answer` needs a --value-not-listed flag passed through to
  gateway.answer_question(..., value_not_listed=args.value_not_listed):

    grep -n "def cmd_item_answer" -A 12 src/resell/cli_item.py
    grep -n "answer.add_argument\\|\\"answer\\"" src/resell/cli_item.py

Then:  uv run pytest -q
""")
