"""
migrate_to_postgres.py - one-time migration of the local SQLite scrape into Cloud SQL.

NOT part of the running app. A build/setup tool: it reads the full scraped SQLite
database and copies every row into the Postgres (Cloud SQL) instance, in
foreign-key-safe order, preserving the original ids. Safe to re-run - every insert
uses ON CONFLICT DO NOTHING, so a run interrupted midway (e.g. the proxy drops on a
long load) can just be run again; already-loaded rows are skipped, not duplicated.

PREREQUISITES
  - The Cloud SQL Auth Proxy is running (listens on 127.0.0.1:5432), and .env
    points DB_HOST at it - same setup db.py uses. This script imports db.py's
    connection, so if `python db.py` works, this can connect too.
  - schema.sql has already been loaded into the target (the tables exist).
  - Nothing else has the source SQLite file open (close VS Code's SQLite viewer),
    and the WAL has been checkpointed.

USAGE (from the repo root, venv active, proxy running):
    python tools/migrate_to_postgres.py --source "D:/programs/gamedb/game_library.db" --limit 500
    python tools/migrate_to_postgres.py --source "D:/programs/gamedb/game_library.db"

  --limit N   migrate only the first N games (and just their related rows) as a
              dry run. Do this FIRST to prove the pipeline on a small slice before
              turning it loose on the full ~304k. Drop the flag for the real run.
  --source    path to the full SQLite scrape. Defaults to the GAMEDB_SOURCE env
              var, then to game_library.db at the repo root.

WHAT IT DOES NOT DO
  - It does not create the schema (run schema.sql first) and it does not delete
    anything. It only inserts.
"""

import os
import sys
import json
import argparse
import sqlite3
from pathlib import Path

# db.py lives at the repo root; this script lives in tools/. Put root on the path
# so we can reuse db.get_connection() (which reads DB_* from .env / the environment).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import db  # noqa: E402

import psycopg  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402


# ---------------------------------------------------------------------------
# Table load order - PARENTS BEFORE CHILDREN. A child inserted before its
# parent violates the foreign key. This order is the whole point of the task.
#
# Each entry: (table, columns, per-row transform or None).
# The transform receives a sqlite3.Row and returns a tuple of values in the same
# column order - the hook where we fix DATE strictness and wrap JSON for JSONB.
# ---------------------------------------------------------------------------

def _clean_date(v):
    """
    Postgres DATE rejects '', 'TBA', and other junk that SQLite TEXT tolerated.
    Anything that isn't a plausible ISO date becomes NULL (the column is nullable).
    We don't try to parse - just pass through real-looking dates, null the rest.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # crude ISO shape check: starts YYYY-MM-DD. Good enough; the scraper only ever
    # wrote ISO dates or nothing, so this just catches stray '' / 'TBA' etc.
    if len(s) >= 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit():
        return s
    return None


def _game_row(r):
    return (r["game_id"], r["canonical_title"], r["normalized_title"],
            r["release_year"], _clean_date(r["release_date"]),
            r["summary"], r["cover_image_id"], r["created_at"], r["updated_at"])


def _raw_fetch_row(r):
    # payload is stored as a JSON *string* in SQLite; the target column is JSONB.
    # Wrap valid JSON in Jsonb() so psycopg sends it as jsonb. If a row somehow
    # isn't valid JSON, store NULL rather than crash the whole migration.
    payload = r["payload"]
    if payload is not None:
        try:
            payload = Jsonb(json.loads(payload))
        except (ValueError, TypeError):
            payload = None
    return (r["raw_fetch_id"], r["source_id"], r["source_native_id"],
            r["endpoint"], r["http_status"], payload, r["fetched_at"])


# table, column list, transform
TABLES = [
    ("source",
     ["source_id", "name", "display_name", "base_url", "notes"], None),
    ("game",
     ["game_id", "canonical_title", "normalized_title", "release_year",
      "release_date", "summary", "cover_image_id", "created_at", "updated_at"], _game_row),
    ("company",
     ["company_id", "name"], None),
    ("attribute",
     ["attribute_id", "kind", "name"], None),
    ("game_source_ref",
     ["ref_id", "game_id", "source_id", "source_native_id", "source_url",
      "match_method", "match_confidence", "linked_at"], None),
    ("game_score",
     ["score_id", "game_id", "source_id", "score_type", "score_value",
      "sample_count", "captured_at"], None),
    ("game_attribute",
     ["game_id", "attribute_id"], None),
    ("game_company",
     ["game_id", "company_id", "role"], None),
    ("raw_fetch",
     ["raw_fetch_id", "source_id", "source_native_id", "endpoint",
      "http_status", "payload", "fetched_at"], _raw_fetch_row),
    ("user_game",
     ["user_game_id", "game_id", "status", "user_rating", "hours_played",
      "notes", "added_at", "updated_at"], None),
]

# The identity sequences to reset after loading explicit ids. Each is
# (table, pk_column). Composite-PK join tables (game_attribute, game_company)
# have no identity sequence, so they're not here.
SEQUENCES = [
    ("source", "source_id"), ("game", "game_id"), ("company", "company_id"),
    ("attribute", "attribute_id"), ("game_source_ref", "ref_id"),
    ("game_score", "score_id"), ("raw_fetch", "raw_fetch_id"),
    ("user_game", "user_game_id"),
]

BATCH = 1000  # rows per executemany round-trip


def _limited_game_ids(sconn, limit):
    """Return the set of game_ids to migrate when --limit is used (first N by id)."""
    rows = sconn.execute("SELECT game_id FROM game ORDER BY game_id LIMIT ?", (limit,)).fetchall()
    return {r["game_id"] for r in rows}


def _select_sql(table, columns, limit_ids):
    """
    Build the SELECT against SQLite. With --limit, restrict child tables to the
    chosen game_ids so we don't pull rows referencing games we skipped (which
    would violate FKs on the Postgres side). source/attribute/company are
    dimension tables shared across games - we load them whole regardless, since
    the children reference them.
    """
    cols = ", ".join(columns)
    if limit_ids is None:
        return f"SELECT {cols} FROM {table}", ()
    ids = ",".join(str(i) for i in limit_ids)
    if table == "game":
        return f"SELECT {cols} FROM game WHERE game_id IN ({ids})", ()
    if "game_id" in columns:
        return f"SELECT {cols} FROM {table} WHERE game_id IN ({ids})", ()
    if table == "raw_fetch":
        # raw_fetch has no game_id - it links to games only indirectly, by
        # (source_id, source_native_id) matching game_source_ref. Under --limit we
        # must NOT load it whole (it's the gigabytes table; that would defeat the
        # point of a fast dry run). Scope it to the fetches whose native id belongs
        # to one of the sliced games. On the full run this branch isn't taken.
        qcols = ", ".join(f"rf.{c}" for c in columns)
        return (
            f"SELECT {qcols} FROM raw_fetch rf "
            f"WHERE EXISTS (SELECT 1 FROM game_source_ref gsr "
            f"             WHERE gsr.source_id = rf.source_id "
            f"               AND gsr.source_native_id = rf.source_native_id "
            f"               AND gsr.game_id IN ({ids}))",
            (),
        )
    # remaining dimension tables with no game_id (source, attribute, company):
    # load fully - children reference them and they're small.
    return f"SELECT {cols} FROM {table}", ()


def migrate(source_path, limit=None):
    sconn = sqlite3.connect(source_path)
    sconn.row_factory = sqlite3.Row
    pconn = db.get_connection()   # psycopg connection to Cloud SQL via the proxy

    limit_ids = _limited_game_ids(sconn, limit) if limit else None
    if limit:
        print(f"DRY RUN: migrating {len(limit_ids)} games and their related rows.\n")

    grand_total = 0
    try:
        for table, columns, transform in TABLES:
            sql, params = _select_sql(table, columns, limit_ids)
            placeholders = ", ".join(["%s"] * len(columns))
            collist = ", ".join(columns)
            insert = (f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
                      f"ON CONFLICT DO NOTHING")

            cur = sconn.execute(sql, params)
            copied = 0
            with pconn.cursor() as pcur:
                while True:
                    rows = cur.fetchmany(BATCH)
                    if not rows:
                        break
                    batch = [transform(r) if transform else tuple(r) for r in rows]
                    pcur.executemany(insert, batch)
                    copied += len(batch)
                    print(f"  {table}: {copied} rows...", end="\r", flush=True)
            pconn.commit()
            print(f"  {table}: {copied} rows loaded.        ")
            grand_total += copied

        # --- reset identity sequences so the next auto-insert doesn't collide ---
        # After loading explicit ids, each sequence still sits at 1. Bump each to
        # MAX(id)+1. setval(..., MAX+1, false) means "next value returned is MAX+1".
        print("\nResetting identity sequences...")
        with pconn.cursor() as pcur:
            for table, pk in SEQUENCES:
                pcur.execute(
                    f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                    f"COALESCE((SELECT MAX({pk}) FROM {table}), 0) + 1, false)",
                    (table, pk),
                )
                nextval = pcur.fetchone()
                print(f"  {table}.{pk} -> next id {nextval['setval'] if isinstance(nextval, dict) else nextval[0]}")
        pconn.commit()

        print(f"\nDone. {grand_total} rows migrated"
              + (" (dry run)." if limit else "."))
    finally:
        sconn.close()
        pconn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Migrate the SQLite scrape into Cloud SQL Postgres.")
    ap.add_argument("--source", default=os.environ.get("GAMEDB_SOURCE",
                    str(_ROOT / "game_library.db")),
                    help="path to the source SQLite database")
    ap.add_argument("--limit", type=int, default=None,
                    help="dry run: migrate only the first N games")
    args = ap.parse_args()

    if not Path(args.source).exists():
        sys.exit(f"Source DB not found: {args.source}")
    print(f"Source: {args.source}")
    print(f"Target: Postgres via db.get_connection() (proxy on 127.0.0.1)\n")
    migrate(args.source, limit=args.limit)