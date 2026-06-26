#!/usr/bin/env python3
"""
build_dev_db.py - build the local dev database from the committed schema + starter data.

It reads the .sql files as UTF-8 explicitly, because on Windows open()/read_text() crash 
on accented game titles. Run from anywhere:

    python tools/build_dev_db.py            # build game_library.db (errors if it exists)
    python tools/build_dev_db.py --force    # delete and rebuild it
    
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent     # tools/ is one level under the repo root
SCHEMA = ROOT / "schema.sql"
SEED = ROOT / "sample_data.sql"
DB_PATH = Path(os.environ.get("GAMEDB_DB", ROOT / "game_library.db"))


def main():
    force = "--force" in sys.argv[1:]

    for f in (SCHEMA, SEED):
        if not f.exists():
            sys.exit(f"error: {f.name} not found at {f}\n"
                     f"       Run this from the repo, and make sure the seed has been built.")

    if DB_PATH.exists():
        if not force:
            sys.exit(f"error: {DB_PATH} already exists.\n"
                     f"       Pass --force to delete and rebuild it (it's a disposable dev DB).")
        DB_PATH.unlink()
        print(f"removed existing {DB_PATH.name}")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.executescript(SEED.read_text(encoding="utf-8"))
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM game").fetchone()[0]
    finally:
        conn.close()
    print(f"built {DB_PATH.name}: {n} games loaded from the seed.")


if __name__ == "__main__":
    main()