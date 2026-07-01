"""
steam.py - enrich the library with Steam data, using the AppIDs already linked
from IGDB. No API key needed for these endpoints.

Run from the repo ROOT so `import db` resolves and the DB is found:

  python ingest/steam.py                # reviews pass: Steam % positive + review count
  python ingest/steam.py --details      # ALSO the details pass (Metacritic, categories,
                                        #   Steam genres, developer/publisher)
  python ingest/steam.py --details-only # only the details pass
  python ingest/steam.py --refresh      # re-fetch games already enriched (default skips them)
  python ingest/steam.py 500            # cap to 500 games (handy for a test run)

Both passes are incremental: they only hit games that haven't been fetched yet
(tracked via the staging table), so they're safe to stop and re-run. The full 
raw JSON of every fetch is staged, so fields we don't extract now 
(price, release date, languages, ...) are preserved for later.

RATE LIMITS:
  - reviews  (store.steampowered.com/appreviews)  tolerates ~10 req/sec.
  - details  (store.steampowered.com/api/appdetails) is ~200 req / 5 min - so the
    --details pass is SLOW over the whole catalog. Lean on the resume behavior.
"""
import os
import sys
import json
import time

import requests

# This script lives in ingest/, but db.py lives at the repo root. Put the root on
# the import path so `import db` works when run as `python ingest/steam.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db

REVIEWS_URL = ("https://store.steampowered.com/appreviews/{appid}"
               "?json=1&language=all&purchase_type=all&num_per_page=0")
DETAILS_URL = "https://store.steampowered.com/api/appdetails?appids={appid}"

REVIEWS_INTERVAL = 0.2    # ~5/sec (endpoint tolerates ~10/sec; we stay polite)
DETAILS_INTERVAL = 1.6    # ~37/min, under the ~200/5min appdetails ceiling
HEADERS = {"User-Agent": "personal-game-library/1.0"}

_last_call = {"t": 0.0}


def _throttle(interval):
    wait = interval - (time.time() - _last_call["t"])
    if wait > 0:
        time.sleep(wait)
    _last_call["t"] = time.time()


def _get(url, interval, max_retries=4):
    """GET a URL with throttling + backoff. Returns parsed JSON, or None on failure
    (None means 'not a definitive answer' -> caller leaves it for a later re-run)."""
    for attempt in range(max_retries):
        _throttle(interval)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None
    return None


def _games_needing(conn, endpoint, refresh):
    """
    Games that have a Steam AppID but no staged fetch for this endpoint yet.
    Using the staging table as the 'already done' marker means a game with no
    reviews (or no store page) still counts as done and won't be re-fetched,
    while a transient failure (never staged) gets retried next run.
    """
    skip = "" if refresh else """
        AND NOT EXISTS (
            SELECT 1 FROM raw_fetch rf
            WHERE rf.source_id = (SELECT source_id FROM source WHERE name='steam')
              AND rf.source_native_id = gsr.source_native_id
              AND rf.endpoint = %s
        )"""
    params = [] if refresh else [endpoint]
    return conn.execute(f"""
        SELECT g.game_id AS game_id, gsr.source_native_id AS appid
        FROM game_source_ref gsr
        JOIN source s ON s.source_id = gsr.source_id AND s.name = 'steam'
        JOIN game g ON g.game_id = gsr.game_id
        WHERE 1=1 {skip}
        ORDER BY g.game_id
    """, params).fetchall()


# ---------------------------------------------------------------------------
# Pass 1: reviews - the Steam user-rating signal
# ---------------------------------------------------------------------------
def enrich_reviews(conn, limit=None, refresh=False, progress=True):
    todo = _games_needing(conn, "appreviews", refresh)
    if limit:
        todo = todo[:limit]
    total = len(todo)
    if progress:
        print(f"  {total} games need a Steam review fetch")

    fetched = scored = attempted = 0
    for row in todo:
        attempted += 1
        appid = row["appid"]
        data = _get(REVIEWS_URL.format(appid=appid), REVIEWS_INTERVAL)
        if data is not None:                   # transient failures stay unstaged -> retried later
            db.stage_raw(conn, "steam", appid, "appreviews", 200, json.dumps(data))
            fetched += 1
            if data.get("success") == 1:
                qs = data.get("query_summary", {})
                total_reviews = qs.get("total_reviews", 0) or 0
                if total_reviews > 0:
                    pct = qs.get("total_positive", 0) / total_reviews * 100
                    db.add_score(conn, row["game_id"], "steam", "steam_positive_pct",
                                 round(pct, 2), sample_count=total_reviews)
                    scored += 1

        if progress and (attempted == 1 or attempted % 25 == 0):
            print(f"  reviews: {attempted}/{total} tried, {fetched} fetched, {scored} scored",
                  flush=True)

    if progress:
        print(f"  reviews done: {fetched} fetched, {scored} got a score", flush=True)
    return scored


# ---------------------------------------------------------------------------
# Pass 2: details - Metacritic score, categories, Steam genres, dev/publisher
# ---------------------------------------------------------------------------
def enrich_details(conn, limit=None, refresh=False, progress=True):
    db.ensure_source(conn, "metacritic", "Metacritic")
    todo = _games_needing(conn, "appdetails", refresh)
    if limit:
        todo = todo[:limit]
    total = len(todo)
    if progress:
        print(f"  {total} games need a Steam details fetch (this pass is slow)")

    fetched = attempted = 0
    for row in todo:
        attempted += 1
        appid = row["appid"]
        data = _get(DETAILS_URL.format(appid=appid), DETAILS_INTERVAL)
        if data is not None:
            db.stage_raw(conn, "steam", appid, "appdetails", 200, json.dumps(data))
            fetched += 1
            entry = (data.get(str(appid)) or {}) if isinstance(data, dict) else {}
            if entry.get("success") and isinstance(entry.get("data"), dict):
                d = entry["data"]
                game_id = row["game_id"]

                mc = (d.get("metacritic") or {}).get("score")
                if mc is not None:
                    db.add_score(conn, game_id, "metacritic", "metacritic_critic", mc)

                db.add_attributes(conn, game_id, "steam_category",
                                  [c["description"] for c in d.get("categories", []) if c.get("description")])
                db.add_attributes(conn, game_id, "steam_genre",
                                  [g["description"] for g in d.get("genres", []) if g.get("description")])

                companies = ([(n, "developer") for n in d.get("developers", []) if n] +
                             [(n, "publisher") for n in d.get("publishers", []) if n])
                if companies:
                    db.add_companies(conn, game_id, companies)

        if progress and (attempted == 1 or attempted % 10 == 0):
            print(f"  details: {attempted}/{total} tried, {fetched} fetched", flush=True)

    if progress:
        print(f"  details done: {fetched} fetched", flush=True)
    return fetched


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    conn = db.get_connection()
    db.ensure_schema(conn)

    refresh = "--refresh" in args
    do_details = ("--details" in args) or ("--details-only" in args)
    do_reviews = "--details-only" not in args
    nums = [a for a in args if a.isdigit()]
    cap = int(nums[0]) if nums else None

    if do_reviews:
        print("Fetching Steam review summaries (your Steam user-rating signal)...")
        n = enrich_reviews(conn, limit=cap, refresh=refresh)
        print(f"Reviews: {n} games scored.\n")

    if do_details:
        print("Fetching Steam app details (Metacritic, categories, genres, dev/publisher)...")
        print("NOTE: appdetails is rate-limited to ~200/5min - expect this to be slow.")
        n = enrich_details(conn, limit=cap, refresh=refresh)
        print(f"Details: {n} games processed.")

    conn.close()