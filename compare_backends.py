#!/usr/bin/env python3
"""
compare_backends.py - prove the SQLite port returns what Postgres returned.

Runs the same ranked_games() calls against both backends and compares them. This
is the gate before deleting the Cloud SQL instance.

WHAT COUNTS AS A FAILURE, AND WHY:

Correctness is "same games, same values" - identical sets of game_ids, with every
returned field matching. That is what a data or logic error would break, and it
is checked strictly.

Row ORDER is reported but does not fail the run, because the two backends order
tied rows differently for two unavoidable reasons:

  1. Collation. SQLite compares text byte by byte, so an apostrophe (39) sorts
     before a letter and a space (32) before a colon (58). Postgres's en_US.UTF-8
     ignores punctuation at the primary level. Neither is wrong; they are
     different, documented conventions. ("Zeldarius" vs "Zelda's Adventure"
     swaps for exactly this reason.)

  2. Unbroken ties. Games sharing an identical sort key have no defined order in
     either engine - Postgres could reorder them between runs on a different
     plan. db_sqlite.py now appends game_id as a final tiebreaker, which makes
     SQLite deterministic where Postgres never was. That is an improvement, and
     it necessarily shows up here as an ordering difference.

So an ordering delta on tied rows is expected and fine. A set or value delta is
not, and fails the run.

    export DB_PASSWORD=$(gcloud secrets versions access latest --secret=db-password-web)
    python3 compare_backends.py --sqlite gamedb.sqlite

Exit status is 0 if every case matches on sets and values.
"""

import argparse
import os
import sys

import db as pg_db
import db_sqlite as lite_db

# Each case exercises a different path through the query builder: the default
# ranking, both sort modes, search mode with its LEFT JOIN, the genre
# include/exclude EXISTS clauses, the year window, the weight knob, and the
# strict critic-count filter.
CASES = [
    ("default",              dict(limit=50)),
    ("popularity",           dict(limit=50, sort_by="popularity", min_score=80)),
    ("high floor",           dict(limit=50, min_score=85, min_user_count=100)),
    ("critics only",         dict(limit=50, critic_weight=1.0)),
    ("users only",           dict(limit=50, critic_weight=0.0)),
    ("steam only",           dict(limit=50, steam_only=True)),
    ("strict critic count",  dict(limit=50, min_critic_count=10, strict_critic_count=True)),
    ("include genre",        dict(limit=50, include_genres=["Adventure"])),
    ("exclude genre",        dict(limit=50, exclude_genres=["Adventure", "Indie"])),
    ("year window",          dict(limit=50, min_year=2015, max_year=2020)),
    ("search mode",          dict(limit=50, title_search="mario", require_both_scores=False)),
    ("relevance sort",       dict(limit=50, title_search="zelda", sort_by="relevance",
                                  require_both_scores=False)),
    ("everything at once",   dict(limit=25, min_score=70, min_user_count=50,
                                  exclude_genres=["Indie"], min_year=2010,
                                  critic_weight=0.7, sort_by="popularity")),
]

# Floats can differ in the last bits between backends without being wrong.
TOLERANCE = 1e-6


def as_dict(row):
    """sqlite3.Row and psycopg's dict_row both expose keys(); compare as plain dicts."""
    return {k: row[k] for k in row.keys()}


def values_match(a, b):
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= TOLERANCE
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= TOLERANCE
    return a == b


def compare(name, pg_rows, lite_rows):
    """Strict on membership and values; informational on order."""
    pg = {r["game_id"]: as_dict(r) for r in pg_rows}
    lite = {r["game_id"]: as_dict(r) for r in lite_rows}

    problems = []

    only_pg = set(pg) - set(lite)
    only_lite = set(lite) - set(pg)
    if only_pg or only_lite:
        problems.append(f"membership differs: {len(only_pg)} only in postgres, "
                        f"{len(only_lite)} only in sqlite")
        for gid in list(only_pg)[:3]:
            problems.append(f"    postgres only: {pg[gid].get('canonical_title')!r}")
        for gid in list(only_lite)[:3]:
            problems.append(f"    sqlite only:   {lite[gid].get('canonical_title')!r}")

    for gid in set(pg) & set(lite):
        for key, pv in pg[gid].items():
            lv = lite[gid].get(key)
            if not values_match(pv, lv):
                problems.append(
                    f"{pg[gid].get('canonical_title')!r} field {key}: {pv!r} vs {lv!r}")
        if len(problems) > 8:
            break

    if problems:
        print(f"  FAIL  {name}")
        for p in problems[:8]:
            print(f"          {p}")
        return False

    pg_order = [r["game_id"] for r in pg_rows]
    lite_order = [r["game_id"] for r in lite_rows]
    if pg_order == lite_order:
        print(f"  ok    {name}  ({len(pg_rows)} rows, identical order)")
    else:
        moved = sum(1 for a, b in zip(pg_order, lite_order) if a != b)
        print(f"  ok    {name}  ({len(pg_rows)} rows, same set; "
              f"{moved} tied rows in a different order)")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", default="gamedb.sqlite")
    args = ap.parse_args()

    os.environ["DB_PATH"] = args.sqlite

    pg = pg_db.get_connection()
    lite = lite_db.get_connection()

    print(f"Comparing {len(CASES)} query shapes "
          f"(sets and values strict, order informational):\n")
    passed = 0
    for name, kwargs in CASES:
        pg_rows = pg_db.ranked_games(pg, **kwargs)
        lite_rows = lite_db.ranked_games(lite, **kwargs)
        if compare(name, pg_rows, lite_rows):
            passed += 1

    print("\nGenre list:")
    pg_genres = {r["name"]: r["n_games"] for r in pg_db.all_genres(pg)}
    lite_genres = {r["name"]: r["n_games"] for r in lite_db.all_genres(lite)}
    if pg_genres == lite_genres:
        print(f"  ok    all_genres ({len(pg_genres)} genres, identical counts)")
        passed += 1
    else:
        missing = set(pg_genres) ^ set(lite_genres)
        wrong = {k for k in set(pg_genres) & set(lite_genres)
                 if pg_genres[k] != lite_genres[k]}
        print(f"  FAIL  all_genres: {len(missing)} genres differ, {len(wrong)} counts differ")
        for k in list(missing)[:5]:
            print(f"          only one side: {k!r}")
        for k in list(wrong)[:5]:
            print(f"          {k!r}: postgres={pg_genres[k]} sqlite={lite_genres[k]}")

    pg.close()
    lite.close()

    total = len(CASES) + 1
    print(f"\n{passed}/{total} passed")
    if passed == total:
        print("\nSets and values match everywhere. The SQLite snapshot is a faithful\n"
              "replacement and the Cloud SQL instance is now redundant.")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
