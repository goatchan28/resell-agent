#!/usr/bin/env python3
"""Open blocking questions for missing required aspects before aborting a publish.

Scoped to publish readiness. Nothing is weakened: the abort still happens, the
aspect requirement is unchanged, and no value is invented. The difference is that
the operator is left with answerable questions instead of a sentence they have to
translate into commands.

Run from the repo root:  python3 patch_publish_questions.py
"""

from __future__ import annotations

import pathlib
import sys

publisher = pathlib.Path("src/resell/ebay/publisher.py")
if not publisher.exists():
    sys.exit("run this from the repository root")

source = publisher.read_text()

# --- 1. the abort now opens questions first ----------------------------------

OLD_ABORT = '''        if missing:
            raise PublishAborted(
                f"category {listing['category_id']} requires aspects that are not "
                f"populated: {', '.join(missing)}"
            )'''

NEW_ABORT = '''        if missing:
            opened = self._open_aspect_questions(listing["sku"], missing)
            raise PublishAborted(
                self._missing_aspects_message(listing, missing, opened)
            )'''

# --- 2. the two methods, inserted above _check_aspects -----------------------

NEW_METHODS = '''    def _open_aspect_questions(self, sku: str, missing: list[str]) -> list[str]:
        """One blocking question per unpopulated required aspect.

        Deliberately plain questions. The publisher knows *that* an aspect is
        absent, not why -- whether nothing was observed, or the evidence is
        ambiguous, or the sources contradict each other. `map-aspects` owns that
        analysis via gaps.py, and guessing at it here would produce a worse
        version of it under a second name. So this asks the flat question and the
        abort message points at map-aspects for the richer treatment.

        Allowed values are not inlined for the same reason: `resell item aspects
        CATEGORY` already prints the Taxonomy form, and duplicating that
        formatting here is a second place for it to drift.

        Idempotent. A name that already has an unanswered question is skipped, so
        re-running publish does not accumulate duplicates.
        """
        existing = {
            row["aspect_name"]
            for row in self.gateway.conn.execute(
                "SELECT aspect_name FROM open_question "
                "WHERE sku = ? AND answered_at IS NULL AND aspect_name IS NOT NULL",
                (sku,),
            )
        }

        opened: list[str] = []
        for name in missing:
            if name in existing:
                opened.append(f"{name} (already asked)")
                continue
            try:
                self.gateway.ask_operator(
                    sku,
                    question=f"What is this item's {name}?",
                    why_it_matters=(
                        f"eBay requires {name} for this category and will not "
                        f"publish the listing without it"
                    ),
                    aspect_name=name,
                )
                opened.append(name)
            except Rejected as exc:
                # Same tolerance as map-aspects: one question failing to open must
                # not hide the others.
                opened.append(f"{name} (could not ask: {exc.reasons[0]})")
        return opened

    def _missing_aspects_message(
        self, listing: sqlite3.Row, missing: list[str], opened: list[str]
    ) -> str:
        """Say what is missing, and the exact commands that resolve it.

        The re-propose steps are part of the instructions rather than an
        afterthought. Aspects live on the listing proposal and the approval binds
        those bytes, so populating one after approval necessarily invalidates it.
        Publishing aspects the approval never covered would be a second authority
        over listing content, which is the thing this design refuses everywhere
        else.
        """
        sku = listing["sku"]
        category = listing["category_id"]
        example = missing[0]

        lines = [
            f"category {category} requires aspects that are not populated: "
            f"{', '.join(missing)}",
        ]
        if opened:
            lines.append(f"blocking question(s): {', '.join(opened)}")
        lines += [
            "",
            "Nothing was sent to eBay. To see and answer them:",
            f"  resell item questions {sku}",
            f"  resell item answer ID 'value'",
            f"  resell item aspects {category}          # eBay's allowed values",
            f"  resell item map-aspects {sku}           # or let the model map them",
            "",
            "Then re-propose. Populating an aspect changes the listing content the",
            "approval binds, so the old approval no longer covers what would be sent:",
            f"  resell item revise {sku}                # -> pricing, voids the approval",
            f"  resell item identify {sku} --aspect '{example}=VALUE'",
            f"  resell item propose {sku} --price-cents CENTS ...",
            f"  resell item approve {sku} --hash HASH",
            f"  resell item publish {sku}",
        ]
        return "\\n".join(lines)

    def _check_aspects(self, listing: sqlite3.Row) -> PublishStep:'''

OLD_SIGNATURE = "    def _check_aspects(self, listing: sqlite3.Row) -> PublishStep:"

edits = [
    (OLD_ABORT, NEW_ABORT),
    (OLD_SIGNATURE, NEW_METHODS),
]

for old, _ in edits:
    count = source.count(old)
    if count != 1:
        sys.exit(f"anchor appears {count} times, expected 1:\n{old[:90]}")

for old, new in edits:
    source = source.replace(old, new, 1)

# --- 3. the import ------------------------------------------------------------

if "Rejected" not in source.split("\n\n")[0] and "import Rejected" not in source:
    marker = "from resell.gateway import"
    if marker in source:
        line_start = source.index(marker)
        line_end = source.index("\n", line_start)
        line = source[line_start:line_end]
        if "Rejected" not in line:
            source = source[:line_end] + source[line_end:]
            source = source.replace(line, line.rstrip() + ", Rejected", 1)
    else:
        # No existing gateway import: add one next to the other absolute imports.
        anchor = "from resell.ebay.client import"
        if anchor not in source:
            sys.exit("could not find a place for the Rejected import; add it by hand")
        source = source.replace(
            anchor, "from resell.gateway import Rejected\n" + anchor, 1
        )

publisher.write_text(source)
print("patched src/resell/ebay/publisher.py")
print("  - _check_aspects opens blocking questions before aborting")
print("  - added _open_aspect_questions and _missing_aspects_message")
print("\nCheck the import landed correctly, then:  uv run pytest -q")
