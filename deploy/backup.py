"""Both halves of the beta's state, nightly: the database and the photographs.

Run by the project's own interpreter, and that is the point rather than a
preference. This started as a bash script calling `sqlite3` and `rsync`, which
worked perfectly by hand and failed under launchd with

    /bin/bash: .../deploy/backup.sh: Operation not permitted        (exit 126)
    Error: unable to open database ".../data/resell.db": authorization denied

Both are macOS TCC. `~/Documents` is a protected location, and a process launchd
spawns gets its own privacy identity rather than inheriting the shell's -- so
`/bin/bash` could not read the script, and once the script was moved out,
`/usr/bin/sqlite3` could not read the database. Apple's own binaries cannot
practically be granted a Documents exception one by one.

The app's interpreter already holds that grant: after a `kill -9`, launchd
restarted the server on its own and it opened the same database without trouble.
Doing the backup with that interpreter removes the dependency on anything else
being granted, and needs nobody to click through System Settings.

The database alone would not be a backup. A listing's photographs live under
`data/uploads` and are referenced by absolute path, so a restored database
without them is a shelf of items whose pictures 404 -- and they are the only
copy, since the browser resizes to 2048px before uploading and the original
stays on the phone. `data/derivatives` is skipped: every file in it is
regenerated on demand.

`Connection.backup()` rather than copying the file: the database is in WAL mode
and a plain copy without its -wal is a torn read.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(os.environ.get(
    "RESELL_ROOT", Path(__file__).resolve().parent.parent))
DEST = Path(os.environ.get(
    "RESELL_BACKUP_DIR", Path.home() / "Backups" / "resell")).expanduser()
KEEP_DAYS = int(os.environ.get("RESELL_BACKUP_KEEP", "14"))


def say(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')}  {message}", flush=True)


def copy_database(source: Path, target: Path) -> None:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as live, \
            sqlite3.connect(target) as copy:
        live.backup(copy)
    # Read it back before trusting it. A backup nobody has opened is a hope.
    with sqlite3.connect(target) as check:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"integrity check on the copy said {result!r}")
    say(f"database ok ({target.stat().st_size // 1024} KiB)")


def copy_uploads(source: Path, target: Path) -> None:
    if not source.exists():
        say("no uploads directory; nothing to copy")
        return
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    files = sum(1 for _ in target.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    say(f"photographs ok ({files} files, {size // (1024 * 1024)} MiB)")


def prune(root: Path, keep_days: int) -> None:
    """Drop dated folders older than the window, and only those.

    Matched by name against the folder's own date rather than by mtime, so a
    directory touched by a later read is not kept for ever. Anything that is not
    a dated folder is left alone -- this deletes recursively and the guard is
    worth more than the tidiness.
    """
    cutoff = datetime.now(UTC).timestamp() - keep_days * 86400
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            stamp = datetime.strptime(entry.name, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        if stamp.timestamp() < cutoff:
            shutil.rmtree(entry)
            say(f"pruned {entry.name}")


def main() -> int:
    database = ROOT / "data" / "resell.db"
    if not database.exists():
        say(f"no database at {database}")
        return 1
    target = DEST / datetime.now(UTC).strftime("%Y-%m-%d")
    target.mkdir(parents=True, exist_ok=True)

    copy_database(database, target / "resell.db")
    copy_uploads(ROOT / "data" / "uploads", target / "uploads")
    prune(DEST, KEEP_DAYS)
    say(f"backed up to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
