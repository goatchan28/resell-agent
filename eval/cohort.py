"""Who is in the 30-item evaluation, and what the record says about them.

Read-only. The production database is opened through a `file:...?mode=ro` URI so
a bug here cannot advance an item, write an identification, or change a price.
The baseline is frozen; the evaluation may not alter the schema it is evaluating.

The cohort is **the first 5 qualifying items after the start point, whoever owns
them** -- a V1 baseline ahead of the V2 redesign. An invited tester's item flows through the same orchestrator, the same
gateway and the same tables as the admin's -- MP-000047 has 3 runs, 24 model
calls, 88 steps and a proposal, exactly the shape of any other item -- so there is
no technical reason to exclude one, and excluding it would measure the operator
rather than the product.

Owner identity is preserved for analysis and pseudonymised in anything committed:
comparing "my usage" with "invited-user usage" needs the distinction, and does not
need family members' email addresses in git.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
DB_PATH = ROOT / "data" / "resell.db"

# Everything at or below this sequence predates the evaluation window. Set once,
# at pre-flight, and recorded in FINDINGS.md. MP-000048 was created and abandoned
# before the window opened, so the first evaluation item is MP-000049.
START_SEQ = 48

# Items the operator has declared were not real attempts -- a mis-upload, a
# duplicate, a test. Kept as an explicit list rather than a quiet deletion: the
# report prints these with their reason, so a reader can see the cohort was
# trimmed and by how much. This is for "that was not an attempt at selling
# something", never for "that one went badly" -- outcome is not a criterion, and
# MP-000052 failing to publish is exactly the sort of result the set is for.
NOT_REAL_ATTEMPTS: dict[str, str] = {
    "MP-000050": "operator declared it a mistake, not a real attempt",
}

# Five, not thirty. The run was cut back to a V1 baseline ahead of the lean V2
# redesign: enough to characterise how V1 behaves end to end, and not so much
# that it invests thirty items in an architecture about to be replaced.
COHORT_SIZE = 5


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def admin_emails() -> set[str]:
    """Who counts as the operator rather than an invited tester.

    Reads the same `.env` the application does. Without it every owner looked
    like a tester, including the admin -- which silently destroys the one
    comparison this labelling exists to support.
    """
    try:
        from resell.config import _load_dotenv
        _load_dotenv()
    except Exception:  # noqa: BLE001 - the label degrades, nothing breaks
        pass
    raw = os.environ.get("RESELL_ADMIN_EMAILS", "")
    return {e.strip().casefold() for e in raw.split(",") if e.strip()}


@dataclass
class Item:
    sku: str
    seq: int
    state: str
    owner_email: str
    owner_label: str            # "admin" or "tester-a" -- safe to commit
    created_at: str
    photos: int
    runs: int
    model_calls: int

    # filled by review.py
    detail: dict = field(default_factory=dict)


def _owner_labels(conn) -> dict[str, str]:
    """Stable pseudonyms. The admin is named as such because the whole point is
    to compare their usage against the testers'; testers are lettered by the
    order they first appear, which is stable because items only ever append."""
    admins = admin_emails()
    rows = conn.execute(
        "select owner_email, min(seq) first_seq from item "
        "where owner_email is not null group by owner_email order by first_seq"
    ).fetchall()
    labels, n = {}, 0
    for row in rows:
        email = (row["owner_email"] or "").casefold()
        if email in admins:
            labels[row["owner_email"]] = "admin"
        else:
            labels[row["owner_email"]] = f"tester-{chr(ord('a') + n)}"
            n += 1
    return labels


def qualifying(conn, start_seq: int = START_SEQ, limit: int = COHORT_SIZE) -> list[Item]:
    """The cohort.

    An item qualifies once it has a photograph and the agent has actually run on
    it. That excludes an accidental empty creation -- there are two in the
    existing data -- without excluding anything the agent genuinely attempted.

    Outcome is deliberately not a criterion. An item that ended `blocked` or
    `abandoned` is a result, and dropping those would measure only the runs that
    went well.
    """
    labels = _owner_labels(conn)
    rows = conn.execute(
        """
        select i.sku, i.seq, i.state, i.owner_email, i.created_at,
               (select count(*) from photo p where p.sku = i.sku) photos,
               (select count(*) from agent_run r where r.sku = i.sku) runs,
               (select count(*) from model_call m where m.sku = i.sku) model_calls
        from item i
        where i.seq > ?
        order by i.seq
        """,
        (start_seq,),
    ).fetchall()

    out: list[Item] = []
    for row in rows:
        if row["photos"] < 1 or row["runs"] < 1:
            continue
        if row["sku"] in NOT_REAL_ATTEMPTS:
            continue
        out.append(Item(
            sku=row["sku"], seq=row["seq"], state=row["state"],
            owner_email=row["owner_email"] or "(none)",
            owner_label=labels.get(row["owner_email"], "unknown"),
            created_at=row["created_at"] or "",
            photos=row["photos"], runs=row["runs"], model_calls=row["model_calls"],
        ))
        if len(out) >= limit:
            break
    return out


def excluded(conn, start_seq: int = START_SEQ) -> list[tuple[str, str]]:
    """Items after the start point that did not qualify, and why.

    Reported rather than silently dropped: a cohort that quietly discards items
    is one nobody can check.
    """
    rows = conn.execute(
        """
        select i.sku,
               (select count(*) from photo p where p.sku = i.sku) photos,
               (select count(*) from agent_run r where r.sku = i.sku) runs
        from item i where i.seq > ? order by i.seq
        """,
        (start_seq,),
    ).fetchall()
    out = []
    for row in rows:
        if row["sku"] in NOT_REAL_ATTEMPTS:
            out.append((row["sku"], NOT_REAL_ATTEMPTS[row["sku"]]))
        elif row["photos"] < 1:
            out.append((row["sku"], "no photographs attached"))
        elif row["runs"] < 1:
            out.append((row["sku"], "photographs attached but the agent never ran"))
    return out


def fingerprint(db_path: Path = DB_PATH) -> str:
    h = hashlib.sha256()
    with open(db_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    conn = connect()
    items = qualifying(conn)
    skipped = excluded(conn)
    print(f"cohort: {len(items)}/{COHORT_SIZE} qualifying items after seq {START_SEQ}\n")
    for item in items:
        print(f"  {item.sku}  {item.owner_label:<9} {item.state:<11} "
              f"{item.photos} photos, {item.runs} runs, {item.model_calls} calls")
    if skipped:
        print("\nexcluded:")
        for sku, why in skipped:
            print(f"  {sku}  {why}")
    if not items:
        print("  (none yet -- the evaluation window has not produced an item)")
    print(f"\nowners seen: "
          f"{json.dumps(sorted({i.owner_label for i in items}))}")
