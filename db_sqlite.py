"""
db_sqlite.py - read-only SQLite data-access layer for the frozen deployment.

A drop-in replacement for the read half of db.py. The web app switches over with
one line:

    import db_sqlite as db          # was: import db

Everything web/app.py calls - get_connection, all_genres, genres_for,
ranked_games - keeps the same name, signature and return shape, so app.py itself
needs no other change.

WHAT IS NOT HERE: the write functions (add_game, add_score, stage_raw,
link_source, update_game, set_status, ensure_source, add_attributes,
add_companies). A frozen snapshot has nothing to write. Those live on in db.py,
which the ingest scripts still use if you ever refresh the catalog from
Postgres and rebuild the snapshot.

THE PORT, IN FULL: psycopg's %(name)s placeholders become SQLite's :name, the
one %s positional becomes ?, and rows come back as sqlite3.Row instead of
psycopg's dict_row. sqlite3.Row already supports row["column"], so every caller
keeps working untouched. The SQL itself is unchanged - the ranking query used no
Postgres-only syntax. NULLS LAST needs SQLite 3.30+ (Python 3.12 ships 3.37+).
"""

import os
import sqlite3
from pathlib import Path

# The snapshot ships inside the container image, next to this file. Override
# with DB_PATH when running locally against a snapshot somewhere else.
DEFAULT_DB = Path(__file__).resolve().parent / "gamedb.sqlite"


def get_connection():
    """
    Open the snapshot read-only.

    mode=ro rejects writes outright; immutable=1 additionally promises SQLite
    that nothing will change the file while it is open, which lets it skip all
    locking and change-detection. Both are true by construction here - the file
    is baked into a read-only container image - and together they make queries
    meaningfully faster.
    """
    path = os.environ.get("DB_PATH", str(DEFAULT_DB))
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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


def _attr_filter(params, key_prefix, kinds, names, exclude=False):
    """
    Build an `AND [NOT] EXISTS (...)` clause testing whether a game carries ANY
    attribute of the given kinds whose name matches one of `names`
    (case-insensitive) - i.e. multiple names are OR, not AND. Adds the name
    bindings to `params` as a side effect.
    """
    keys = []
    for i, name in enumerate(names):
        key = f"{key_prefix}{i}"
        params[key] = name.lower()
        keys.append(f":{key}")
    kinds_sql = "(" + ",".join(f"'{k}'" for k in kinds) + ")"
    op = "NOT EXISTS" if exclude else "EXISTS"
    return f"""
          AND {op} (SELECT 1 FROM game_attribute gax
                    JOIN attribute ax ON ax.attribute_id = gax.attribute_id
                    WHERE gax.game_id = scored.game_id
                      AND ax.kind IN {kinds_sql}
                      AND LOWER(ax.name) IN ({",".join(keys)}))
    """


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
    most-common first.

    app.py's comment notes this "aggregates millions of rows (~10s)" and caches it
    for 6 hours. On a frozen snapshot the answer can never change, so it does not
    need computing at all - prepare_snapshot.sql materializes it into
    genre_counts, and this reads that instead. It matters because Cloud Run
    scales to zero: without it, the first visitor after an idle period waits for
    the aggregate on top of the cold start.

    Falls back to computing live if genre_counts is absent, so a snapshot built
    without the prepare step still works.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='genre_counts'"
    ).fetchone()
    if has_table:
        return conn.execute(
            "SELECT name, n_games FROM genre_counts ORDER BY n_games DESC, name"
        ).fetchall()

    return conn.execute(
        f"""SELECT a.name, COUNT(DISTINCT ga.game_id) AS n_games
            FROM attribute a
            JOIN game_attribute ga ON ga.attribute_id = a.attribute_id
            WHERE a.kind IN {_GENRE_KINDS_SQL}
            GROUP BY a.name
            ORDER BY n_games DESC, a.name""",
    ).fetchall()


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
                 exclude_genres=None, include_genres=None, min_critic_score=0,
                 min_user_score=0, title_search=None, require_both_scores=True,
                 critic_weight=0.5, strict_critic_count=False, min_year=None,
                 max_year=None):
    """
    Rank games by a critic/user blend. Semantics are identical to db.ranked_games -
    see that docstring for the full contract. Only the placeholder syntax changed.
    """
    params = {"minc": min_critic_count, "minu": min_user_count, "minscore": min_score,
              "mincritscore": min_critic_score, "minuserscore": min_user_score}

    # clamp so a bad caller can't invert the blend or push scores out of range
    params["cw"] = max(0.0, min(1.0, critic_weight))

    # the blended score - written once, interpolated everywhere it appears
    blend = "(critic_score * :cw + user_score * (1 - :cw))"

    limit_clause = "LIMIT :lim" if limit else ""
    steam_clause = ("""
          AND EXISTS (SELECT 1 FROM game_source_ref gsr
                      JOIN source s2 ON s2.source_id = gsr.source_id
                      WHERE gsr.game_id = scored.game_id AND s2.name = 'steam')
    """ if steam_only else "")

    include_clause = _attr_filter(params, "ing", GENRE_KINDS, include_genres) if include_genres else ""
    exclude_clause = _attr_filter(params, "exg", GENRE_KINDS, exclude_genres, exclude=True) if exclude_genres else ""

    # title_search: case-insensitive substring match on the canonical title.
    # Lives inside the pivot CTE so search mode's LEFT JOIN only aggregates
    # matching games, not the whole catalog.
    pivot_conds = []
    if title_search:
        params["title_q"] = f"%{title_search.lower()}%"
        pivot_conds.append("LOWER(g.canonical_title) LIKE :title_q")
    if min_year is not None:
        params["miny"] = min_year
        pivot_conds.append("g.release_year >= :miny")
    if max_year is not None:
        params["maxy"] = max_year
        pivot_conds.append("g.release_year <= :maxy")
    title_where = ("WHERE " + " AND ".join(pivot_conds)) if pivot_conds else ""

    # Every ordering ends in game_id. Without it, rows tied on the sort key come
    # back in whatever order the query plan happens to produce - which is not
    # stable across backends, and was never stable across plans in Postgres
    # either. A unique final key makes pagination and repeat queries repeatable.
    #
    # The alphabetical key is normalized_title, not canonical_title. It is
    # already lowercased and stripped of punctuation and edition suffixes, so it
    # sorts identically under SQLite's byte comparison and Postgres's en_US
    # collation - which disagree about canonical_title, where SQLite ranks an
    # apostrophe (byte 39) before a letter and en_US ignores it entirely. It is
    # also the better sort: "Zelda's Adventure" and "Zeldas Adventure" land
    # together instead of pages apart.
    if sort_by == "relevance" and title_search:
        params["title_exact"] = title_search.lower()
        params["title_prefix"] = f"{title_search.lower()}%"
        order_clause = f"""ORDER BY CASE
                              WHEN LOWER(canonical_title) = :title_exact THEN 0
                              WHEN LOWER(canonical_title) LIKE :title_prefix THEN 1
                              ELSE 2
                          END,
                          COALESCE({blend}, critic_score, user_score) DESC NULLS LAST,
                          normalized_title,
                          game_id"""
    elif sort_by == "popularity":
        order_clause = "ORDER BY user_n DESC, final_score DESC NULLS LAST, game_id"
    else:
        order_clause = "ORDER BY final_score DESC NULLS LAST, user_n DESC, game_id"

    # Ranked mode drops unscored games anyway, so it keeps the cheaper INNER
    # JOIN; search mode needs LEFT JOIN so score-less games survive the pivot.
    score_join = "JOIN" if require_both_scores else "LEFT JOIN"

    if require_both_scores:
        score_filters = f"""
          AND critic_score IS NOT NULL AND user_score IS NOT NULL
          AND user_den >= :minu
          AND {blend} >= :minscore
          AND critic_score >= :mincritscore
          AND user_score >= :minuserscore"""
    else:
        score_filters = f"""
          AND (user_score IS NULL OR user_den >= :minu)
          AND (critic_score IS NULL OR user_score IS NULL
               OR {blend} >= :minscore)
          AND (critic_score IS NULL OR critic_score >= :mincritscore)
          AND (user_score IS NULL OR user_score >= :minuserscore)"""

    if strict_critic_count:
        strict_clause = ("AND COALESCE(igdb_critic_n, 0) >= :minc"
                         if require_both_scores else
                         "AND (igdb_critic IS NULL OR COALESCE(igdb_critic_n, 0) >= :minc)")
    else:
        strict_clause = ""

    query = f"""
        WITH pivot AS (
            SELECT g.game_id, g.canonical_title, g.normalized_title, g.release_year, g.summary, g.cover_image_id,
                MAX(CASE WHEN s.name='igdb'       AND sc.score_type='igdb_critic_rating' THEN sc.score_value  END) AS igdb_critic,
                MAX(CASE WHEN s.name='igdb'       AND sc.score_type='igdb_critic_rating' THEN sc.sample_count END) AS igdb_critic_n,
                MAX(CASE WHEN s.name='metacritic' AND sc.score_type='metacritic_critic'  THEN sc.score_value  END) AS metacritic,
                MAX(CASE WHEN s.name='igdb'       AND sc.score_type='igdb_user_rating'   THEN sc.score_value  END) AS igdb_user,
                MAX(CASE WHEN s.name='igdb'       AND sc.score_type='igdb_user_rating'   THEN sc.sample_count END) AS igdb_user_n,
                MAX(CASE WHEN s.name='steam'      AND sc.score_type='steam_positive_pct' THEN sc.score_value  END) AS steam_user,
                MAX(CASE WHEN s.name='steam'      AND sc.score_type='steam_positive_pct' THEN sc.sample_count END) AS steam_user_n
            FROM game g
            {score_join} game_score sc ON sc.game_id = g.game_id
            {score_join} source s ON s.source_id = sc.source_id
            {title_where}
            GROUP BY g.game_id
        ),
        calc AS (
            SELECT *,
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
        SELECT game_id, canonical_title, release_year, summary, cover_image_id,
               igdb_critic, igdb_critic_n, metacritic, critic_score,
               igdb_user, igdb_user_n, steam_user, steam_user_n, user_den AS user_n, user_score,
               {blend} AS final_score
        FROM scored
        WHERE TRUE
          {score_filters}
          {strict_clause}
          {steam_clause}
          {include_clause}
          {exclude_clause}
        {order_clause}
        {limit_clause}
    """
    if limit:
        params["lim"] = limit
    return conn.execute(query, params).fetchall()


# ---------------------------------------------------------------------------
# Running this file directly prints a summary of the snapshot - a quick check
# that the file is present, readable, and populated.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    conn = get_connection()
    for row in conn.execute("SELECT key, value FROM snapshot_meta ORDER BY key"):
        print(f"{row['key']:<28} {row['value']}")
    top = ranked_games(conn, limit=5)
    print("\nTop 5 by blended score:")
    for r in top:
        print(f"  {r['final_score']:.1f}  {r['canonical_title']} ({r['release_year']})")
    conn.close()
