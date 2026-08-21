#!/usr/bin/env python3
"""Add the approved-price setup to the multi-line propose_listing call sites.

The previous patch matched single-line calls only. These three span lines
because they pass `required_aspects=`, which is why they were reported for
manual handling rather than silently missed.

Run from the repo root:  python3 patch_oauth_multiline.py
"""

from __future__ import annotations

import pathlib
import re
import sys

tests = pathlib.Path("tests/test_oauth.py")
if not tests.exists():
    sys.exit("run this from the repository root")

text = tests.read_text()

if "_approve_price" not in text:
    sys.exit("run patch_oauth_price_setup.py first; _approve_price is missing")

# The call opens on one line and the arguments follow on the next. Anchored on
# `_valid_proposal`, which prices at 1999, so the seeded price matches the
# proposal the gateway will compare against.
pattern = re.compile(
    r"^(?P<indent>[ \t]*)(?P<open>(?:\w+ = )?gateway\.propose_listing\(\n"
    r"[ \t]*(?P<sku>\w+), _valid_proposal\((?P=sku),)",
    re.MULTILINE,
)


def add_price(match: re.Match) -> str:
    indent = match.group("indent")
    return (
        f"{indent}_approve_price(conn, {match.group('sku')}, 1999)\n"
        f"{indent}{match.group('open')}"
    )


text, count = pattern.subn(add_price, text)
tests.write_text(text)
print(f"patched {count} multi-line propose_listing call site(s)")

still = [
    n for n, line in enumerate(text.splitlines(), 1)
    if "gateway.propose_listing(" in line
]
print(f"{len(still)} propose_listing call(s) in the file; lines: {still}")
