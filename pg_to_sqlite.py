#!/usr/bin/env python3
"""
pg_to_sqlite.py - build a frozen SQLite snapshot of the serving tables.

Streams the eight serving tables out of Cloud SQL Postgres and writes them into
a single SQLite file that the web app can ship inside its container image. The
raw_fetch staging table is deliberately NOT copied: it is 87% of the database
(5.4 GB of 6.2 GB), the serving path never reads it, and its only consumer is an
existence check in ingest/steam.py, which a frozen deployment never runs.

Why a direct connection rather than parsing a dump: CSV export loses the
distinction between NULL and empty string, which matters for score_value and
sample_count, where NULL means "no score" and 0 means "a score of zero". Reading
rows through psycopg preserves types and NULLs exactly.

Run it in Cloud Shell (same region as the instance, so the transfer is fast):

    pip install --quiet "psycopg[binary]"
    export PGPASSWORD=$(gcloud secrets versions access latest --secret=db-password-web)
    ./cloud-sql-proxy rayar-cs3537-2026:us-east1:gamedb-pg &
    python3 pg_to_sqlite.py --out gamedb.sqlite

Or locally, with the Cloud SQL Auth Proxy already running (see the repo README).
Connection settings come from the same DB_* environment variables db.py uses.
"""

import argparse
import os
import sqlite3
import sys
import time

import psycopg

# ---------------------------------------------------------------------------
# Tables to copy, in foreign-key-safe order. (column list, source table)
# Explicit column lists rather than SELECT *, so a schema drift on either side
# fails loudly here instead of silently shifting values into wrong columns.
# ---------------------------------------------------------------------------
TABLES = [
    ("source", [
        "source_id", "name", "display_name", "base_url", "notes",
    ]),
    ("game", [
        "game_id", "canonical_title", "normalized_title", "release_year",
        "release_date", "summary", "cover_image_id", "created_at", "updated_at",
    ]),
    ("attribute", [
        "attribute_id", "kind", "name",
    ]),
    ("company", [
        "company_id", "name",
    ]),
    ("game_source_ref", [
        "ref_id", "game_id", "source_id", "source_native_id", "source_url",
        "match_method", "match_confidence", "linked_at",
    ]),
    ("game_score", [
        "score_id", "game_id", "source_id", "score_type", "score_value",
        "sample_count", "captured_at",
    ]),
    ("game_attribute", [
        "game_id", "attribute_id",
    ]),
    ("game_company", [
        "game_id", "company_id", "role",
    ]),
    ("user_game", [
        "user_game_id", "game_id", "status", "user_rating", "hours_played",
        "notes", "added_at", "updated_at",
    ]),
]

# SQLite mirror of schema.sql. BIGINT -> INTEGER, DOUBLE PRECISION -> REAL,
# DATE/TIMESTAMPTZ -> TEXT (SQLite has no date type; nothing in the read path
# does date arithmetic, it only compares release_year as an integer).
SCHEMA = """
CREATE TABLE source (
    source_id    INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    base_url     TEXT,
    notes        TEXT
);

CREATE TABLE game (
    game_id          INTEGER PRIMARY KEY,
    canonical_title  TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    release_year     INTEGER,
    release_date     TEXT,
    summary          TEXT,
    cover_image_id   TEXT,
    created_at       TEXT,
    updated_at       TEXT
);

CREATE TABLE attribute (
    attribute_id INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    UNIQUE (kind, name)
);

CREATE TABLE company (
    company_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE
);

CREATE TABLE game_source_ref (
    ref_id           INTEGER PRIMARY KEY,
    game_id          INTEGER NOT NULL,
    source_id        INTEGER NOT NULL,
    source_native_id TEXT NOT NULL,
    source_url       TEXT,
    match_method     TEXT,
    match_confidence REAL,
    linked_at        TEXT,
    UNIQUE (source_id, source_native_id),
    UNIQUE (game_id, source_id)
);

CREATE TABLE game_score (
    score_id     INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL,
    source_id    INTEGER NOT NULL,
    score_type   TEXT NOT NULL,
    score_value  REAL,
    sample_count INTEGER,
    captured_at  TEXT,
    UNIQUE (game_id, source_id, score_type)
);

CREATE TABLE game_attribute (
    game_id      INTEGER NOT NULL,
    attribute_id INTEGER NOT NULL,
    PRIMARY KEY (game_id, attribute_id)
);

CREATE TABLE game_company (
    game_id    INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    role       TEXT NOT NULL,
    PRIMARY KEY (game_id, company_id, role)
);

CREATE TABLE user_game (
    user_game_id INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'want-to-play',
    user_rating  INTEGER,
    hours_played REAL,
    notes        TEXT,
    added_at     TEXT,
    updated_at   TEXT
);

-- Provenance for a frozen dataset: when this snapshot was taken, and what of.
CREATE TABLE snapshot_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Built AFTER the bulk load - inserting into an indexed table is far slower than
# indexing once at the end.
INDEXES = [
    "CREATE INDEX idx_game_match ON game(normalized_title, release_year)",
    "CREATE INDEX idx_game_year ON game(release_year)",
    "CREATE INDEX idx_score_game ON game_score(game_id, source_id)",
    "CREATE INDEX idx_game_attribute_game ON game_attribute(game_id)",
    # Reverse of the PK: all_genres() and the genre filters start from the
    # attribute side, which the (game_id, attribute_id) PK cannot serve.
    "CREATE INDEX idx_game_attribute_attr ON game_attribute(attribute_id, game_id)",
    "CREATE INDEX idx_attribute_kind ON attribute(kind, name)",
    "CREATE INDEX idx_game_company_game ON game_company(game_id)",
    "CREATE INDEX idx_gsr_game ON game_source_ref(game_id, source_id)",
]

BATCH = 20_000


def pg_connect():
    """Same environment variables db.py reads, so no new configuration to learn."""
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "gamedb"),
        user=os.environ.get("DB_USER", "gamedb_web"),
        password=os.environ.get("DB_PASSWORD") or os.environ.get("PGPASSWORD", ""),
    )


def copy_table(pg, lite, table, columns):
    """Stream one table across in batches, so memory stays flat regardless of size."""
    collist = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    insert = f"INSERT INTO {table} ({collist}) VALUES ({placeholders})"

    started = time.time()
    total = 0

    # A named cursor keeps the result set server-side; a plain cursor would pull
    # all 3.4M game_attribute rows into memory at once.
    with pg.cursor(name=f"cur_{table}") as cur:
        cur.itersize = BATCH
        cur.execute(f"SELECT {collist} FROM {table}")
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            lite.executemany(insert, rows)
            total += len(rows)
            print(f"  {table}: {total:,} rows", end="\r", flush=True)

    lite.commit()
    elapsed = time.time() - started
    print(f"  {table}: {total:,} rows in {elapsed:.1f}s" + " " * 20)
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="gamedb.sqlite", help="output SQLite file")
    ap.add_argument("--force", action="store_true", help="overwrite an existing output file")
    args = ap.parse_args()

    if os.path.exists(args.out):
        if not args.force:
            sys.exit(f"{args.out} already exists. Use --force to overwrite.")
        os.remove(args.out)

    print(f"Connecting to Postgres at {os.environ.get('DB_HOST', '127.0.0.1')} ...")
    pg = pg_connect()

    lite = sqlite3.connect(args.out)
    # Bulk-load settings: no journal and no fsync makes this several times
    # faster. Safe here because a crash just means deleting the file and
    # re-running - there is no data to lose that Postgres doesn't still hold.
    lite.execute("PRAGMA journal_mode = OFF")
    lite.execute("PRAGMA synchronous = OFF")
    lite.execute("PRAGMA cache_size = -200000")   # ~200 MB page cache
    lite.executescript(SCHEMA)

    print("\nCopying tables:")
    counts = {}
    for table, columns in TABLES:
        counts[table] = copy_table(pg, lite, table, columns)

    print("\nBuilding indexes:")
    for stmt in INDEXES:
        name = stmt.split()[2]
        started = time.time()
        lite.execute(stmt)
        print(f"  {name} ({time.time() - started:.1f}s)")

    lite.executemany(
        "INSERT INTO snapshot_meta (key, value) VALUES (?, ?)",
        [("taken_at_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
         ("source", "Cloud SQL rayar-cs3537-2026:us-east1:gamedb-pg"),
         ("excluded_tables", "raw_fetch"),
         *[(f"rows_{t}", str(n)) for t, n in counts.items()]],
    )
    lite.commit()

    print("\nOptimizing (ANALYZE + VACUUM, this takes a minute) ...")
    lite.execute("ANALYZE")
    lite.execute("VACUUM")
    lite.close()
    pg.close()

    size_mb = os.path.getsize(args.out) / (1024 * 1024)
    print(f"\nDone. {args.out} is {size_mb:.1f} MB")
    print(f"Games: {counts.get('game', 0):,}")


if __name__ == "__main__":
    main()
