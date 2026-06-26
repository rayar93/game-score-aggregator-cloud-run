"""
db.py - shared data-access layer for the game database.

The one place SQL lives. Both the web service (web/) and the ingestion scripts
(ingest/) import this instead of writing queries directly, so the schema is only
spoken to from here. Run it directly to create an empty database from schema.sql:

    python db.py

What's in here:
    Connection / setup   get_connection, init_db, ensure_schema
    Writing data         (ingestion) add_game, update_game, add_score, set_status, 
                         stage_raw, link_source, add_attributes, add_companies,
                         ensure_source, get_source_id
    Reading data         (web app) find_game_by_source, attributes_for, genres_for,
                         all_genres, unsorted_games, games_by_status
    Ranking              (web app) ranked_games  (the critic/user blend - the main query)
"""

import os
import sqlite3
from pathlib import Path

# Anchor paths to THIS file's directory (the repo root, where db.py lives) so the
# ingest/ scripts resolve the schema and DB identically no matter which folder
# they're launched from. The DB path is overridable via the GAMEDB_DB env var.
_HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("GAMEDB_DB", _HERE / "game_library.db"))
SCHEMA_PATH = _HERE / "schema.sql"


# ---------------------------------------------------------------------------
# Connection + setup
# ---------------------------------------------------------------------------
def get_connection(db_path=DB_PATH):
    """Open a connection. row_factory makes rows act like dicts (row['title'])."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite needs this EVERY connection
    return conn


def init_db(db_path=DB_PATH, schema_path=SCHEMA_PATH):
    """Create all tables by running the schema file. Run once per database."""
    conn = get_connection(db_path)
    conn.executescript(Path(schema_path).read_text(encoding="utf-8"))
    conn.commit()
    conn.close()


def ensure_schema(conn, schema_path=SCHEMA_PATH):
    """
    Create the tables if they don't exist yet. Safe to call on every run - if the
    schema is already there it does nothing, so tools like igdb.py can call it at
    startup and stop caring whether `python db.py` was run first.
    """
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source'"
    ).fetchone()
    if not has_tables:
        conn.executescript(Path(schema_path).read_text(encoding="utf-8"))
        conn.commit()


# ---------------------------------------------------------------------------
# Writing data
# ---------------------------------------------------------------------------
def get_source_id(conn, name):
    row = conn.execute("SELECT source_id FROM source WHERE name = ?", (name,)).fetchone()
    return row["source_id"] if row else None


def ensure_source(conn, name, display_name=None, base_url=None):
    """Create a source row if it doesn't exist yet (e.g. 'metacritic' on a DB built before it was seeded)."""
    conn.execute(
        "INSERT OR IGNORE INTO source (name, display_name, base_url) VALUES (?, ?, ?)",
        (name, display_name or name.title(), base_url),
    )
    conn.commit()


def add_game(conn, canonical_title, normalized_title,
             release_year=None, release_date=None, summary=None, cover_image_id=None):
    """Insert a canonical game and return its new game_id."""
    cur = conn.execute(
        """INSERT INTO game (canonical_title, normalized_title,
                             release_year, release_date, summary, cover_image_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (canonical_title, normalized_title, release_year, release_date, summary, cover_image_id),
    )
    conn.commit()
    return cur.lastrowid


def add_score(conn, game_id, source_name, score_type, score_value, sample_count=None):
    """
    Upsert one score metric for a game from one source. Re-running updates the
    value in place instead of piling up duplicate rows, so it's safe to call on 
    every re-import. sample_count is how many critics/users the score is based 
    on (None if unknown).
    """
    conn.execute(
        """INSERT INTO game_score (game_id, source_id, score_type, score_value, sample_count)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(game_id, source_id, score_type) DO UPDATE
               SET score_value = excluded.score_value,
                   sample_count = excluded.sample_count,
                   captured_at = datetime('now')""",
        (game_id, get_source_id(conn, source_name), score_type, score_value, sample_count),
    )
    conn.commit()


def set_status(conn, game_id, status):
    """
    Put a game on one of the user's lists (want-to-play / playing / played).
    Because user_game has at most one row per game, calling this again just
    moves the game to the new list. A game with no row here is 'unsorted'.
    """
    conn.execute(
        """INSERT INTO user_game (game_id, status)
           VALUES (?, ?)
           ON CONFLICT(game_id) DO UPDATE
               SET status = excluded.status,
                   updated_at = datetime('now')""",
        (game_id, status),
    )
    conn.commit()


def stage_raw(conn, source_name, source_native_id, endpoint, http_status, payload):
    """Drop a raw API response into the staging table before reconciliation."""
    conn.execute(
        """INSERT INTO raw_fetch (source_id, source_native_id, endpoint, http_status, payload)
           VALUES (?, ?, ?, ?, ?)""",
        (get_source_id(conn, source_name), source_native_id, endpoint, http_status, payload),
    )
    conn.commit()


def link_source(conn, game_id, source_name, source_native_id,
                source_url=None, match_method=None, match_confidence=None):
    """
    Record that a canonical game corresponds to a given source's native id.
    Re-running won't create duplicates (the UNIQUE constraints catch it).
    """
    conn.execute(
        """INSERT INTO game_source_ref
               (game_id, source_id, source_native_id, source_url, match_method, match_confidence)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT DO NOTHING""",
        (game_id, get_source_id(conn, source_name), source_native_id,
         source_url, match_method, match_confidence),
    )
    conn.commit()


def add_attributes(conn, game_id, kind, names):
    """
    Upsert tag-like attributes of a given KIND ('genre','platform','theme',
    'game_mode','player_perspective','franchise','keyword', ...) and attach them
    to a game. Re-running won't create duplicates.
    """
    for name in names:
        conn.execute("INSERT OR IGNORE INTO attribute (kind, name) VALUES (?, ?)", (kind, name))
        aid = conn.execute(
            "SELECT attribute_id FROM attribute WHERE kind = ? AND name = ?", (kind, name)
        ).fetchone()["attribute_id"]
        conn.execute(
            "INSERT OR IGNORE INTO game_attribute (game_id, attribute_id) VALUES (?, ?)",
            (game_id, aid),
        )
    conn.commit()


def add_companies(conn, game_id, companies):
    """
    Attach (name, role) company pairs to a game. role is 'developer' or 'publisher'. 
    Re-running won't create duplicates.
    """
    for name, role in companies:
        conn.execute("INSERT OR IGNORE INTO company (name) VALUES (?)", (name,))
        cid = conn.execute("SELECT company_id FROM company WHERE name = ?", (name,)).fetchone()["company_id"]
        conn.execute(
            "INSERT OR IGNORE INTO game_company (game_id, company_id, role) VALUES (?, ?, ?)",
            (game_id, cid, role),
        )
    conn.commit()


def attributes_for(conn, game_id, kind):
    """Return the list of attribute names of a given kind attached to a game."""
    return [r["name"] for r in conn.execute(
        """SELECT a.name FROM game_attribute ga
           JOIN attribute a ON a.attribute_id = ga.attribute_id
           WHERE ga.game_id = ? AND a.kind = ? ORDER BY a.name""",
        (game_id, kind),
    )]


# Genres arrive under different attribute kinds depending on the source: IGDB
# genres are kind='genre', Steam's --details genres are kind='steam_genre'. We
# treat both as "genres" everywhere a unified view is wanted. (These are code
# constants, not user input, so they're safe to inline into SQL.)
GENRE_KINDS = ("genre", "steam_genre")
_GENRE_KINDS_SQL = "(" + ",".join(f"'{k}'" for k in GENRE_KINDS) + ")"


def genres_for(conn, game_id):
    """All genres on a game, unioned across sources (IGDB + Steam), de-duplicated."""
    return [r["name"] for r in conn.execute(
        f"""SELECT DISTINCT a.name FROM game_attribute ga
            JOIN attribute a ON a.attribute_id = ga.attribute_id
            WHERE ga.game_id = ? AND a.kind IN {_GENRE_KINDS_SQL}
            ORDER BY a.name""",
        (game_id,),
    )]


def all_genres(conn):
    """
    Every distinct genre name across all sources, with how many games carry each,
    most-common first. Names are merged across sources only when they're spelled
    identically - IGDB's "Role-playing (RPG)" and Steam's "RPG" stay separate,
    since we don't (yet) map between the two taxonomies.
    """
    return conn.execute(
        f"""SELECT a.name, COUNT(DISTINCT ga.game_id) AS n_games
            FROM attribute a
            JOIN game_attribute ga ON ga.attribute_id = a.attribute_id
            WHERE a.kind IN {_GENRE_KINDS_SQL}
            GROUP BY a.name
            ORDER BY n_games DESC, a.name""",
    ).fetchall()


def update_game(conn, game_id, canonical_title, normalized_title,
                release_year=None, release_date=None, summary=None, cover_image_id=None):
    """Refresh a canonical game's fields - used when re-importing a game we already have."""
    conn.execute(
        """UPDATE game
           SET canonical_title = ?, normalized_title = ?, release_year = ?,
               release_date = ?, summary = ?, cover_image_id = ?, updated_at = datetime('now')
           WHERE game_id = ?""",
        (canonical_title, normalized_title, release_year, release_date, summary, cover_image_id, game_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Reading data
# ---------------------------------------------------------------------------
def unsorted_games(conn):
    """Games in the DB the user hasn't triaged into any list yet."""
    return conn.execute("""
        SELECT g.game_id, g.canonical_title, g.release_year
        FROM game g
        LEFT JOIN user_game ug ON ug.game_id = g.game_id
        WHERE ug.game_id IS NULL
        ORDER BY g.canonical_title
    """).fetchall()


def games_by_status(conn, status):
    """One of the user's lists: want-to-play / playing / played."""
    return conn.execute("""
        SELECT g.game_id, g.canonical_title, g.release_year, ug.user_rating
        FROM user_game ug
        JOIN game g ON g.game_id = ug.game_id
        WHERE ug.status = ?
        ORDER BY g.canonical_title
    """, (status,)).fetchall()


def find_game_by_source(conn, source_name, source_native_id):
    """Return the game_id already linked to this source's id, or None if unseen."""
    row = conn.execute(
        """SELECT gsr.game_id
           FROM game_source_ref gsr
           JOIN source s ON s.source_id = gsr.source_id
           WHERE s.name = ? AND gsr.source_native_id = ?""",
        (source_name, str(source_native_id)),
    ).fetchone()
    return row["game_id"] if row else None


def ranked_games(conn, min_critic_count=0, min_user_count=0, limit=50,
                 steam_only=False, sort_by="score", min_score=0,
                 exclude_genres=None, min_critic_score=0, min_user_score=0):
    """
    Rank games by a critic/user blend.

      critic = simple average of the usable critic sources:
                 - IGDB critic rating (dropped from the average if its review
                   count is below min_critic_count as a noise filter.)
                 - Metacritic score (always used when present; it has no critic
                   count, so it can't be weighted - it just averages in)
               With both present it's a straight 50/50 average; with one, it's that one.
      user   = count-weighted average of Steam %-positive and IGDB user rating,
               weighted by review count / rating count.
      final  = (critic + user) / 2

    A game needs a critic score AND a user score to appear. min_user_count filters
    by the COMBINED user count (steam reviews + igdb ratings).

    steam_only=True restricts results to games with a linked Steam appid - i.e. a
    row in game_source_ref under the 'steam' source - which means the game has a
    Steam store page (useful for "what's worth buying in the sale"). This is a
    store-presence check, not a platform check: it's stricter than IGDB's "PC" 
    platform tag, since a PC game can be Epic/GOG-only.

    sort_by selects the ordering:
      "score"      (default) - best blended score first (the quality ranking).
      "popularity"           - most-rated first, by combined user count (steam
                               reviews + igdb ratings). This is a reach/ownership
                               proxy, not a quality measure, so it's almost always
                               paired with min_score.

    min_score drops any game whose blended final score is below it (0-100, default
    0 = no floor). Its main use is with sort_by="popularity": "the most popular
    games that are still at least this good", e.g. min_score=80.

    min_critic_score drops any game whose CRITIC score alone is below it (0-100,
    default 0). min_user_score does the same for the USER score alone. Note the
    nearby knobs are different kinds of filter. Two are count floors (minimum number 
    of people behind a score): min_critic_count on the IGDB critic review count, 
    min_user_count on the combined user review count. Three are value floors for 
    the score itself: min_critic_score and min_user_score on the critic and user 
    values, and min_score on the blended value.

    exclude_genres is an optional list of genre names to leave out: any game
    carrying a matching genre (from EITHER source, matched case-insensitively) is
    dropped. Because IGDB and Steam name genres differently, you may need to list
    both spellings (e.g. "RPG" and "Role-playing (RPG)"); all_genres() shows what
    spellings actually exist.

    FUTURE (when OpenCritic lands): OpenCritic + IGDB critic become count-weighted
    together as 2/3 of the critic side, with Metacritic a fixed 1/3 - i.e. replace
    the simple critic average below with (2/3)*weighted(oc,igdb) + (1/3)*metacritic.
    """
    params = {"minc": min_critic_count, "minu": min_user_count, "minscore": min_score,
              "mincritscore": min_critic_score, "minuserscore": min_user_score}

    order_clause = ("ORDER BY user_n DESC, final_score DESC"
                    if sort_by == "popularity"
                    else "ORDER BY final_score DESC, user_n DESC")
    limit_clause = "LIMIT :lim" if limit else ""
    steam_clause = ("""
          AND EXISTS (SELECT 1 FROM game_source_ref gsr
                      JOIN source s2 ON s2.source_id = gsr.source_id
                      WHERE gsr.game_id = scored.game_id AND s2.name = 'steam')
    """ if steam_only else "")

    exclude_clause = ""
    if exclude_genres:
        ex_keys = []
        for i, name in enumerate(exclude_genres):
            key = f"exg{i}"
            params[key] = name.lower()
            ex_keys.append(f":{key}")
        exclude_clause = f"""
          AND NOT EXISTS (SELECT 1 FROM game_attribute gax
                          JOIN attribute ax ON ax.attribute_id = gax.attribute_id
                          WHERE gax.game_id = scored.game_id
                            AND ax.kind IN {_GENRE_KINDS_SQL}
                            AND LOWER(ax.name) IN ({",".join(ex_keys)}))
        """
    query = f"""
        WITH pivot AS (
            SELECT g.game_id, g.canonical_title, g.release_year,
                MAX(CASE WHEN s.name='igdb'       AND sc.score_type='igdb_critic_rating' THEN sc.score_value  END) AS igdb_critic,
                MAX(CASE WHEN s.name='igdb'       AND sc.score_type='igdb_critic_rating' THEN sc.sample_count END) AS igdb_critic_n,
                MAX(CASE WHEN s.name='metacritic' AND sc.score_type='metacritic_critic'  THEN sc.score_value  END) AS metacritic,
                MAX(CASE WHEN s.name='igdb'       AND sc.score_type='igdb_user_rating'   THEN sc.score_value  END) AS igdb_user,
                MAX(CASE WHEN s.name='igdb'       AND sc.score_type='igdb_user_rating'   THEN sc.sample_count END) AS igdb_user_n,
                MAX(CASE WHEN s.name='steam'      AND sc.score_type='steam_positive_pct' THEN sc.score_value  END) AS steam_user,
                MAX(CASE WHEN s.name='steam'      AND sc.score_type='steam_positive_pct' THEN sc.sample_count END) AS steam_user_n
            FROM game g
            JOIN game_score sc ON sc.game_id = g.game_id
            JOIN source s ON s.source_id = sc.source_id
            GROUP BY g.game_id
        ),
        calc AS (
            SELECT *,
                -- IGDB critic only counts toward the critic average if it clears the threshold
                CASE WHEN igdb_critic IS NOT NULL AND COALESCE(igdb_critic_n, 0) >= :minc
                     THEN igdb_critic END AS igdb_critic_ok,
                (COALESCE(igdb_user * igdb_user_n, 0) + COALESCE(steam_user * steam_user_n, 0)) AS user_num,
                (COALESCE(CASE WHEN igdb_user  IS NOT NULL THEN igdb_user_n  END, 0)
                 + COALESCE(CASE WHEN steam_user IS NOT NULL THEN steam_user_n END, 0)) AS user_den
            FROM pivot
        ),
        agg AS (
            SELECT *,
                (COALESCE(igdb_critic_ok, 0) + COALESCE(metacritic, 0)) AS critic_num,
                ((CASE WHEN igdb_critic_ok IS NOT NULL THEN 1 ELSE 0 END)
                 + (CASE WHEN metacritic   IS NOT NULL THEN 1 ELSE 0 END)) AS critic_den
            FROM calc
        ),
        scored AS (
            SELECT *,
                CASE WHEN critic_den > 0 THEN critic_num * 1.0 / critic_den END AS critic_score,
                CASE WHEN user_den   > 0 THEN user_num   * 1.0 / user_den   END AS user_score
            FROM agg
        )
        SELECT game_id, canonical_title, release_year,
               igdb_critic, igdb_critic_n, metacritic, critic_score,
               igdb_user, igdb_user_n, steam_user, steam_user_n, user_den AS user_n, user_score,
               (critic_score + user_score) / 2.0 AS final_score
        FROM scored
        WHERE critic_score IS NOT NULL AND user_score IS NOT NULL
          AND user_den >= :minu
          AND (critic_score + user_score) / 2.0 >= :minscore
          AND critic_score >= :mincritscore
          AND user_score >= :minuserscore
          {steam_clause}
          {exclude_clause}
        {order_clause}
        {limit_clause}
    """
    if limit:
        params["lim"] = limit
    return conn.execute(query, params).fetchall()


# ---------------------------------------------------------------------------
# Running this file just creates the database (empty) if it doesn't exist.
# It will NOT wipe an existing database or seed any demo data.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if DB_PATH.exists():
        conn = get_connection()
        n = conn.execute("SELECT COUNT(*) c FROM game").fetchone()["c"]
        conn.close()
        print(f"{DB_PATH} already exists ({n} games). Leaving it untouched.")
        print("Delete it manually if you want to start fresh.")
    else:
        init_db()
        print(f"Created {DB_PATH} with empty tables. Import games with: python ingest/igdb.py --all 200")