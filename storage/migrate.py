"""Create a backup and apply pending bot database migrations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .migrations import CURRENT_REVISION, backup, current_revision, import_legacy, upgrade


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("BOT_DB_PATH", "bot.db"), type=Path)
    parser.add_argument("--legacy-db", type=Path, default=None)
    parser.add_argument("--default-role", default=os.environ.get("BOT_LEGACY_DEFAULT_ROLE", ""))
    args = parser.parse_args()

    before = current_revision(args.db)
    if before != CURRENT_REVISION:
        saved = backup(args.db)
        if saved:
            print(f"Backup: {saved}")
        upgrade(args.db)
        print(f"Migration complete: {CURRENT_REVISION}.")
    else:
        print(f"Database is already at {CURRENT_REVISION}; no schema changes made.")

    if args.legacy_db:
        imported = import_legacy(args.legacy_db, args.db, args.default_role)
        print(f"Imported legacy subscribers: {imported}.")


if __name__ == "__main__":
    main()
