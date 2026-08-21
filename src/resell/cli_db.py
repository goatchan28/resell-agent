"""Transport for schema changes: `resell db migrate | verify | status`.

Schema changes go through here and nowhere else. `schema_pricing.sql` is an
implementation detail read by the migrator, not a file anyone is meant to pipe
into sqlite3 by hand -- doing that is what produced a `duplicate column name`
error against a database that was already correct, and it is also what makes it
possible to apply half a script and never find out.

Three commands, all safe to run repeatedly:

    db migrate    bring the database to the current schema; no-op when current
    db verify     read-only check; exits non-zero when out of date, so it gates
    db status     what the database actually contains, and what has been applied

Two guards worth naming. `sqlite3.connect` **creates** a database file that is
not there, so a mistyped path silently produces an empty database and a cheerful
success message; `--create` is required to make a new one. And a migration that
would change anything takes a backup first, while a no-op does not, so running
`db migrate` out of habit does not fill the disk with copies.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .migrate import (
    EXPECTED_TABLES,
    applied_versions,
    columns_of,
    migrate,
    plan,
    verify,
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    """Online backup through sqlite's own API, so it is consistent by construction."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = db_path.parent / "backups" / f"{db_path.stem}-{stamp}-pre-migrate.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(target)
    with dest:
        conn.backup(dest)
    dest.close()
    return target


def cmd_migrate(args, conn: sqlite3.Connection, db_path: Path | None) -> int:
    pending = plan(conn)

    if pending.is_empty:
        print("schema already current; no changes")
        return 0

    print("pending:")
    for line in pending.lines():
        print(f"  {line}")

    if args.dry_run:
        print("dry run; nothing applied")
        return 0

    if db_path is not None and not args.no_backup:
        target = _backup(conn, db_path)
        print(f"backup: {target}")

    report = migrate(conn)
    print(report.describe())

    ok, why = verify(conn)
    print(f"verify: {why}")
    return 0 if ok else 1


def cmd_verify(args, conn: sqlite3.Connection, db_path: Path | None) -> int:
    """Read-only, and exits non-zero when out of date so it can gate other work."""
    ok, why = verify(conn)
    print(why)
    if not ok:
        for line in plan(conn).lines():
            print(f"  pending: {line}")
        print("run `resell db migrate`")
    return 0 if ok else 1


def cmd_status(args, conn: sqlite3.Connection, db_path: Path | None) -> int:
    if db_path is not None:
        size = db_path.stat().st_size if db_path.exists() else 0
        print(f"database  {db_path}  ({size / 1024:.0f} KB)")

    known = applied_versions(conn)
    print(f"migrations applied: {', '.join(sorted(known)) if known else 'none'}")

    ok, why = verify(conn)
    print(f"schema: {why}")

    print("\ntable                      rows  cols")
    for table in EXPECTED_TABLES:
        cols = columns_of(conn, table)
        if not cols:
            print(f"  {table:<24} {'absent':>6}     -")
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<24} {n:>6}  {len(cols):>4}")

    pending = plan(conn)
    if not pending.is_empty:
        print("\npending:")
        for line in pending.lines():
            print(f"  {line}")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="resell db", description="schema migration and inspection"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("migrate", help="bring the database to the current schema")
    c.add_argument("--dry-run", action="store_true",
                   help="report what would change and apply nothing")
    c.add_argument("--no-backup", action="store_true")
    c.add_argument("--create", action="store_true",
                   help="allow creating a database file that does not exist")
    c.set_defaults(fn=cmd_migrate)

    c = sub.add_parser("verify", help="read-only check; non-zero exit when out of date")
    c.set_defaults(fn=cmd_verify)

    c = sub.add_parser("status", help="tables, row counts, and applied migrations")
    c.set_defaults(fn=cmd_status)

    return ap


def main(argv: list[str], db_path: str | Path | None = None, *,
         conn: sqlite3.Connection | None = None) -> int:
    """Entry point.

    Prefers a path so it can guard existence and take backups. Accepts an already
    open connection for callers whose top-level parser opens one first, in which
    case those two protections are unavailable and it says so rather than
    pretending otherwise.
    """
    args = build_parser().parse_args(argv)

    if conn is not None:
        return args.fn(args, conn, None)

    if db_path is None:
        print("no database path given", file=sys.stderr)
        return 2

    path = Path(db_path).expanduser()
    if not path.exists() and not getattr(args, "create", False):
        print(
            f"{path} does not exist.\n"
            "sqlite would create an empty database here and report success. "
            "Check the path, or pass --create if a new database is intended.",
            file=sys.stderr,
        )
        return 2

    path.parent.mkdir(parents=True, exist_ok=True)
    # Deliberately not `with _connect(path)`: a sqlite3 connection used as a
    # context manager commits on exit rather than closing, which would both
    # collide with the close below and quietly commit partial work when a
    # command fails. The migrator commits its own transaction.
    conn_ = _connect(path)
    try:
        return args.fn(args, conn_, path)
    finally:
        conn_.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:], db_path="data/resell.db"))
