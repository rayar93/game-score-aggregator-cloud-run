"""
igdb.py - fetch games from IGDB and write them into the library database.

IGDB is our canonical spine: it gives us the title/year/summary and, via its
external_games field, the Steam AppID. We never have to fuzzy-match Steam ourselves.

SETUP (one time):
  1. Make a free Twitch account, then register an app at https://dev.twitch.tv/console/apps
     (OAuth Redirect URL can be http://localhost; Client Type = Confidential).
     You'll get a Client ID and a Client Secret. These are also your IGDB credentials -
     IGDB authenticates through Twitch; there is no separate IGDB key.
  2. Give the app your credentials WITHOUT putting them in this file. Easiest:
     create a file named .env in the repo root (see .env.example) containing:
         TWITCH_CLIENT_ID=your_id
         TWITCH_CLIENT_SECRET=your_secret
     .env is already in .gitignore so it's never committed.
     (Alternative: set them as shell environment variables instead.)
  3. pip install requests python-dotenv  (or: pip install -r requirements.txt)
  4. Run from the repo ROOT so `import db` resolves and the DB/schema are found:
         python ingest/igdb.py "Hades"        # import a single game by name
         python ingest/igdb.py --all 200       # bulk import, capped at 200 (test run)
         python ingest/igdb.py --all           # bulk import the whole filtered catalog

The short-lived access token is cached in .igdb_token.json (repo root) so you
don't re-authenticate every run. That file and *.db are git-ignored.
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# This script lives in ingest/, but db.py lives at the repo root. Put the root on
# the import path so `import db` works when run as `python ingest/igdb.py`.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import db   # our data-access layer

# Load TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET from a local .env file if one
# exists.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- constants --------------------------------------------------------------
IGDB_BASE = "https://api.igdb.com/v4"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
# Runtime state files live at the repo root, not next to this script, so they
# stay in one place regardless of the working directory. Both are git-ignored.
TOKEN_CACHE = _ROOT / ".igdb_token.json"

MIN_INTERVAL = 0.25       # seconds between calls -> 4 requests/second, IGDB's cap

# Fields requested for every game. We get the Steam AppID by parsing
# external_games.url (e.g. store.steampowered.com/app/<id>) - this is the path
# proven to work on the free tier. IGDB's newer `external.steam` field would be
# more direct, but requesting it currently returns HTTP 400, so we don't.
_GAME_FIELDS = (
    "fields name, slug, first_release_date, summary, "
    "genres.name, platforms.name, themes.name, game_modes.name, player_perspectives.name, "
    "franchises.name, keywords.name, "
    "cover.image_id, "
    "involved_companies.company.name, involved_companies.developer, involved_companies.publisher, "
    "rating, rating_count, aggregated_rating, aggregated_rating_count, "
    "total_rating, total_rating_count, "
    "external_games.url, external_games.uid;"
)

# Default bulk filter: main games only (game_type 0 skips DLC, bundles, mods...).
# Loosen to include more types, e.g. game_type = (0,4,8,9,10,11) for remasters/ports.
DEFAULT_WHERE = "game_type = 0"

_last_call = 0.0          # module-level throttle state


# ---------------------------------------------------------------------------
# Auth: Twitch OAuth2 client-credentials flow, with token caching
# ---------------------------------------------------------------------------
def _credentials():
    try:
        return os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"]
    except KeyError:
        raise RuntimeError(
            "Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET environment variables "
            "(see the setup notes at the top of this file)."
        )


def _get_token():
    """Return a valid access token, fetching/caching one if needed."""
    if TOKEN_CACHE.exists():
        cached = json.loads(TOKEN_CACHE.read_text())
        if cached["expires_at"] > time.time() + 60:   # 60s safety margin
            return cached["access_token"]

    client_id, client_secret = _credentials()
    resp = requests.post(TWITCH_TOKEN_URL, params={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    TOKEN_CACHE.write_text(json.dumps({
        "access_token": payload["access_token"],
        "expires_at": time.time() + payload["expires_in"],
    }))
    return payload["access_token"]


# ---------------------------------------------------------------------------
# Core request: rate-limited + retry with exponential backoff
# ---------------------------------------------------------------------------
def _throttle():
    """Keep outgoing calls at or under 4/second."""
    global _last_call
    wait = MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def igdb_query(endpoint, body, max_retries=4):
    """
    POST an Apicalypse query to an IGDB endpoint and return parsed JSON.
    Retries on 429 (rate limited) and 5xx with exponential backoff.
    """
    client_id, _ = _credentials()
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {_get_token()}",
        "Accept": "application/json",
    }
    url = f"{IGDB_BASE}/{endpoint}"

    for attempt in range(max_retries):
        _throttle()
        resp = requests.post(url, headers=headers, data=body, timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(2 ** attempt)          # 1s, 2s, 4s, 8s
            continue
        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"IGDB request to /{endpoint} failed after {max_retries} retries")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def search_games(name, limit=5):
    """Search IGDB by name; results come back ranked by relevance."""
    body = f'search "{name}";{_GAME_FIELDS}limit {limit};'
    return igdb_query("games", body)


# ---------------------------------------------------------------------------
# Field extraction / normalization
# ---------------------------------------------------------------------------
def extract_steam_appid(game):
    """
    Pull the Steam AppID out of a game, or None.
    Primary: IGDB's modern `external.steam` field (the AppID, directly).
    Fallback: parse it from a Steam store URL in external_games.
    """
    external = game.get("external")
    if isinstance(external, dict) and external.get("steam"):
        return str(external["steam"])
    for eg in game.get("external_games", []):
        match = re.search(r"steampowered\.com/app/(\d+)", eg.get("url", "") or "")
        if match:
            return match.group(1)
    return None


def normalize_title(title):
    """
    Lowercase + strip trademark marks, edition suffixes, and punctuation.
    This becomes the fallback match key when we later reconcile OpenCritic,
    which has no shared ID with IGDB. It'll evolve as we see real edge cases.
    """
    t = title.lower().replace("\u2122", "").replace("\u00ae", "")  # (TM), (R)
    t = re.sub(r"\b(game of the year|goty|definitive|complete|deluxe|"
               r"ultimate|remastered|remake)\b.*", "", t)
    t = re.sub(r"[^\w\s]", " ", t)        # punctuation -> space
    return re.sub(r"\s+", " ", t).strip()


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _names(game, key):
    """Pull the .name out of a nested array field (genres, platforms, themes, ...)."""
    return [item["name"] for item in game.get(key, [])
            if isinstance(item, dict) and item.get("name")]


def _companies(game):
    """Return (company_name, role) pairs from involved_companies; a company can be both."""
    pairs = []
    for ic in game.get("involved_companies", []):
        name = (ic.get("company") or {}).get("name")
        if not name:
            continue
        if ic.get("developer"):
            pairs.append((name, "developer"))
        if ic.get("publisher"):
            pairs.append((name, "publisher"))
    return pairs


def _release_fields(game):
    """
    IGDB stores first_release_date as a unix timestamp; split into year + ISO date.
    We add seconds to the epoch rather than using datetime.fromtimestamp(), because
    fromtimestamp() raises OSError on Windows for negative (pre-1970) timestamps -
    and some classic games predate 1970. The try/except means a single bad or
    out-of-range date is stored as "undated" instead of killing the whole run.
    """
    ts = game.get("first_release_date")
    if ts is None:
        return None, None
    try:
        dt = _EPOCH + timedelta(seconds=ts)
        return dt.year, f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
    except (OverflowError, ValueError, TypeError):
        return None, None


# ---------------------------------------------------------------------------
# Storage: turn one IGDB game dict into rows across our tables
# ---------------------------------------------------------------------------
def _store_game(conn, game):
    """
    Write one IGDB game dict into the DB, idempotently.

    If this IGDB id has been imported before, the existing canonical row is
    UPDATED (title/year/summary refreshed) and reused - no duplicate is created.
    Returns the game_id (new or existing).
    """
    igdb_id = str(game["id"])

    # always log the fetch in staging (append-only history of what we pulled)
    db.stage_raw(conn, "igdb", igdb_id, "games", 200, json.dumps(game))

    year, date = _release_fields(game)
    title = game["name"]
    norm = normalize_title(title)
    cover_image_id = (game.get("cover") or {}).get("image_id")

    existing = db.find_game_by_source(conn, "igdb", igdb_id)
    if existing is not None:
        # seen before: refresh the canonical fields, keep the same game_id
        db.update_game(conn, existing, title, norm, year, date,
                       game.get("summary"), cover_image_id)
        game_id = existing
    else:
        # new game: create the canonical row and its IGDB link
        game_id = db.add_game(
            conn,
            canonical_title=title,
            normalized_title=norm,
            release_year=year,
            release_date=date,
            summary=game.get("summary"),
            cover_image_id=cover_image_id,
        )
        db.link_source(conn, game_id, "igdb", igdb_id,
                       source_url=f"https://www.igdb.com/games/{game.get('slug', '')}",
                       match_method="igdb", match_confidence=1.0)

    # Steam link + attributes are already idempotent, so applying them on every
    # run is safe and keeps them current whether the game is new or refreshed.
    appid = extract_steam_appid(game)
    if appid:
        db.link_source(conn, game_id, "steam", appid,
                       source_url=f"https://store.steampowered.com/app/{appid}",
                       match_method="igdb_external", match_confidence=1.0)

    # tag-like attributes - same generic mechanism for each kind
    db.add_attributes(conn, game_id, "genre",              _names(game, "genres"))
    db.add_attributes(conn, game_id, "platform",           _names(game, "platforms"))
    db.add_attributes(conn, game_id, "theme",              _names(game, "themes"))
    db.add_attributes(conn, game_id, "game_mode",          _names(game, "game_modes"))
    db.add_attributes(conn, game_id, "player_perspective", _names(game, "player_perspectives"))
    db.add_attributes(conn, game_id, "franchise",          _names(game, "franchises"))
    db.add_attributes(conn, game_id, "keyword",            _names(game, "keywords"))
    db.add_companies(conn, game_id, _companies(game))

    # IGDB's own ratings. Each is paired with the count it's based on (stored as sample_count) 
    # so we can weight or filter low-confidence scores later. Skips any rating IGDB doesn't 
    # have for this game.
    for score_type, value_key, count_key in (
        ("igdb_user_rating",   "rating",            "rating_count"),
        ("igdb_critic_rating", "aggregated_rating", "aggregated_rating_count"),
        ("igdb_total_rating",  "total_rating",      "total_rating_count"),
    ):
        value = game.get(value_key)
        if value is not None:
            db.add_score(conn, game_id, "igdb", score_type, value, game.get(count_key))

    return game_id


def import_game(conn, name):
    """Search IGDB for `name`, store the top-ranked match. Returns game_id or None."""
    results = search_games(name)
    if not results:
        return None
    return _store_game(conn, results[0])


PROGRESS_FILE = _ROOT / ".igdb_bulk_progress.json"


def _load_progress(where):
    """
    Return (last_id, total) to resume a full run from, or (0, 0) for a fresh start.
    Only resumes if the saved run used the SAME filter - changing the filter
    means a different walk, so we start over rather than resume into it.
    """
    if not PROGRESS_FILE.exists():
        return 0, 0
    try:
        data = json.loads(PROGRESS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return 0, 0
    if data.get("where") != where:
        return 0, 0
    return data.get("last_id", 0), data.get("total", 0)


def _save_progress(where, last_id, total):
    PROGRESS_FILE.write_text(json.dumps({"where": where, "last_id": last_id, "total": total}))


def _clear_progress():
    PROGRESS_FILE.unlink(missing_ok=True)


def bulk_import(conn, where=DEFAULT_WHERE, page_size=500, max_games=None,
                progress=True, resume=True, steam_only=False):
    """
    Walk IGDB's catalog and store every matching game.

    KEYSET pagination (sort by id, then 'where id > last') is used instead of
    offset: offset is capped on the free tier (~10k rows) and slows down the
    deeper you page, whereas walking by id has no ceiling and stays fast.

    steam_only: default False stores ALL main games (each gets a Steam link only
    if it has one - e.g. Alan Wake 2, an Epic exclusive, comes in without one but
    still carries its IGDB ratings). Set True to store only games that are on Steam.

    RESUME: full runs save the last id reached to PROGRESS_FILE after every page,
    so a crash/sleep mid-run can be continued by just re-running - it picks up
    where it left off instead of re-walking from zero. The file is cleared on
    clean completion. This is only of use locally, as the container that igdb.py 
    runs in is stateless. Capped runs (max_games set) are test runs and stay stateless. 
    

    max_games lets you cap a test run; leave it None to pull the whole filtered set.
    """

    use_progress = resume and max_games is None    # only full runs are resumable
    if use_progress:
        last_id, total = _load_progress(where)
        if last_id and progress:
            print(f"  resuming from IGDB id {last_id} ({total} already stored)")
    else:
        last_id, total = 0, 0

    seen = 0   # games walked past (incl. skipped non-Steam), for progress context
    while True:
        body = (
            f"{_GAME_FIELDS}"
            f"where id > {last_id} & {where};"
            f"sort id asc;"
            f"limit {page_size};"
        )
        page = igdb_query("games", body)
        if not page:
            break

        for game in page:
            seen += 1
            if steam_only and extract_steam_appid(game) is None:
                continue           # not on Steam - skip storing, but we still walked its id
            _store_game(conn, game)
            total += 1
            if max_games and total >= max_games:
                if progress:
                    print(f"  reached max_games={max_games}")
                return total

        last_id = page[-1]["id"]      # keyset cursor: advances past the whole page
        if use_progress:
            _save_progress(where, last_id, total)   # checkpoint after each page
        if progress:
            print(f"  stored {total} games  ({seen} scanned, through IGDB id {last_id})")
        if len(page) < page_size:     # short page == last page
            break

    if use_progress:
        _clear_progress()             # finished cleanly - nothing to resume next time
    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    conn = db.get_connection()
    db.ensure_schema(conn)   # create tables if this DB is brand new - order-independent

    if args and args[0] == "--all":
        rest = args[1:]
        if "--fresh" in rest:                     # ignore any saved progress, restart
            rest = [a for a in rest if a != "--fresh"]
            _clear_progress()
            print("Cleared saved progress; starting from the beginning.")
        steam_only = "--steam-only" in rest        # opt in to Steam-only
        rest = [a for a in rest if a != "--steam-only"]
        cap = int(rest[0]) if rest else None
        scope = "Steam games only" if steam_only else "all main games"
        print(f"Bulk importing from IGDB ({scope}, cap = {cap if cap else 'none'}) ...")
        n = bulk_import(conn, max_games=cap, steam_only=steam_only)
        print(f"Done. {n} games stored.")
    else:
        name = args[0] if args else "Hades"
        gid = import_game(conn, name)
        if gid is None:
            print(f'No IGDB match for "{name}".')
        else:
            steam = conn.execute(
                """SELECT source_native_id FROM game_source_ref gsr
                   JOIN source s ON s.source_id = gsr.source_id
                   WHERE gsr.game_id = %s AND s.name = 'steam'""", (gid,)).fetchone()
            row = conn.execute("SELECT canonical_title, release_year FROM game WHERE game_id = %s",
                               (gid,)).fetchone()
            print(f"Imported #{gid}: {row['canonical_title']} ({row['release_year']})")
            print(f"  Steam AppID: {steam['source_native_id'] if steam else 'not found'}")

    conn.close()