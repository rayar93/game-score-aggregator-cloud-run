"""
app.py - web front-end for the game database.

Serves the ranked game list (the same blend rank.py prints, but as an HTML page)
and a /health check for Cloud Run. This file turns db calls into web pages.

Run locally with the Cloud SQL Auth Proxy running:

    python web/app.py            # dev server on http://127.0.0.1:8080

In production it's served by gunicorn (see the Dockerfile); Cloud Run sets the
PORT env var, which the __main__ block below respects.
"""
import os
import sys

from pathlib import Path

from flask import Flask, render_template, request, g

# db.py lives at the repo root; this file lives in web/. Put the root on the import
# path so `import db` works whether run as `python web/app.py` or by gunicorn.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import db

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Connection lifecycle: one connection per request, closed when the request ends.
# ---------------------------------------------------------------------------
def get_db():
    if "conn" not in g:
        g.conn = db.get_connection()
    return g.conn


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """
    The ranked list. A couple of filters are wired straight to query-string args
    so we can see the db.py filters working without any form UI yet:

        /                       top 50, review-count floors applied
        /?limit=100             top 100
        /?min_users=10000       require >= 10k combined user ratings
        /?min_critics=10        IGDB critic score needs >= 10 reviews
        /?genre=Roguelike       only games carrying that genre (IGDB or Steam)
        /?steam=1               only games with a Steam store page

    """
    conn = get_db()

    # read filters from the URL, with safe fallbacks
    def _int_arg(name, default, cap=None):
        try:
            v = int(request.args.get(name, default))
        except (ValueError, TypeError):
            return default
        return min(v, cap) if cap is not None else v

    limit = _int_arg("limit", 50, cap=1000)
    min_critics = _int_arg("min_critics", 5)   # hard floor on IGDB critic review count
    min_users = _int_arg("min_users", 4000)    # combined user ratings must total >= this
    critic_weight = max(0, min(100, _int_arg("critic_weight", 50)))
    min_score = _int_arg("min_score", 0)
    min_critic_score = _int_arg("min_critic_score", 0)
    min_user_score = _int_arg("min_user_score", 0)
    min_year = _int_arg("min_year", None)
    max_year = _int_arg("max_year", None)
    steam_only = request.args.get("steam") in ("1", "true", "yes")

    sort = request.args.get("sort")
    if sort not in ("score", "popularity"):
        sort = "score"

    genres = [g for g in request.args.getlist("genre") if g.strip()]
    exclude_raw = (request.args.get("exclude") or "").strip()
    exclude_genres = [g.strip() for g in exclude_raw.split(",") if g.strip()] or None

    search_query = (request.args.get("q") or "").strip()

    rows = db.ranked_games(
        conn,
        limit=limit,
        steam_only=steam_only,
        min_critic_count=min_critics,
        min_user_count=min_users,
        min_score=min_score,
        min_critic_score=min_critic_score,
        min_user_score=min_user_score,
        min_year=min_year,
        max_year=max_year,
        strict_critic_count=True,
        critic_weight=critic_weight / 100.0,
        include_genres=genres or None,
        exclude_genres=exclude_genres,
        title_search=search_query or None,
        require_both_scores=not search_query,
        sort_by="relevance" if search_query else sort,
    )

    # genre list for the form checkboxes (most-common first, with counts)
    genre_options = db.all_genres(conn)
    top_names = [g["name"] for g in genre_options[:15]]
    more_open = any(g not in top_names for g in genres)

    return render_template(
        "index.html",
        games=rows,
        count=len(rows),
        limit=limit,
        genre_options=genre_options,
        selected_genres=genres,
        more_open=more_open,
        exclude=exclude_raw,
        steam_only=steam_only,
        min_critics=min_critics,
        min_users=min_users,
        min_score=min_score,
        min_critic_score=min_critic_score,
        min_user_score=min_user_score,
        min_year=min_year,
        max_year=max_year,
        critic_weight=critic_weight,
        sort=sort,
        search_query=search_query,
    )


@app.route("/health")
def health():
    """Liveness check for Cloud Run. Returns 200 'ok' without touching the DB."""
    return "ok", 200


if __name__ == "__main__":
    # Cloud Run provides PORT; default to 8080 for local runs.
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)