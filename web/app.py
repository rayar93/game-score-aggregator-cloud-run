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

    limit = _int_arg("limit", 50, cap=200)
    min_critics = _int_arg("min_critics", 5)   # IGDB critic rating needs >= this many reviews
    min_users = _int_arg("min_users", 4000)    # combined user ratings must total >= this
    genre = request.args.get("genre") or None
    steam_only = request.args.get("steam") in ("1", "true", "yes")
    
    search_query = (request.args.get("q")or "").strip()
    
    if search_query:
        rows = db.search_games(
            conn, 
            title_query = search_query,
            limit=limit,
        )
        search_mode = True
    else:
        rows = db.ranked_games(
        
            conn,
            limit=limit,
            steam_only=steam_only,
            min_critic_count= min_critics,
            min_user_count= min_users,
        )
        search_mode = False
    
    if genre:
        wanted = genre.lower()
        rows = [
            r for r in rows
            if wanted in(
                name.lower()
                for name in db.genres_for(conn, r["game_id"])
            )
        ]
    
    return render_template(
        "index.html",
        games=rows,
        count=len(rows),
        limit=limit,
        genre=genre,
        steam_only = steam_only,
        min_critics=min_critics,
        min_users=min_users,
        search_query=search_query,
        search_mode = search_mode    
    )


@app.route("/health")
def health():
    """Liveness check for Cloud Run. Returns 200 'ok' without touching the DB."""
    return "ok", 200


if __name__ == "__main__":
    # Cloud Run provides PORT; default to 8080 for local runs.
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)