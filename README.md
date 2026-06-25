# Team AAA - Summer 2026 - Game database

A videogame database and discovery tool that's more searchable, filterable, and
complete than existing sites. It ingests game data and ratings from **IGDB**,
**Steam**, and possibly later **OpenCritic** into a normalized **Cloud SQL**
database, then blends critic and user scores with user-configurable weighting,
letting users search and filter by genre, platform, developer, publisher,
release year, and minimum rating counts.

CS 3537 Cloud Computing - **Team AAA**: Alan Ray, Anthony Samson, Aaron White.

## Architecture

The whole application runs on **Cloud Run** as Docker containers:

- **Web service** (`web/` + `db.py`) - a containerized Flask app that serves the
  frontend and all search/filter/ranking queries. The only part that talks to the
  database, and all database access happens in the cloud.
- **Cloud SQL (PostgreSQL)** - the cloud-hosted relational database.
- **Ingestion job** (`ingest/` + `db.py`) - the data-fetching scripts, run as a
  Cloud Run job on a Cloud Scheduler trigger to keep the catalog current.

Stretch goals: user accounts (Identity Platform) + personal ratings, and
ML-based recommendations from per-game metadata.

## Repository layout

```
team-AAA-summer2026/
├── README.md
├── requirements.txt        # you must build your own venv from this
├── .gitignore
├── .env.example            # template; copy to a local .env and add secret keys
├── schema.sql              # database schema (SQLite now; to be ported to Posgres)
├── sample_data.sql         # whole .db won't fit on GitHub - workaround until we move to cloud
├── db.py                   # shared data-access layer
├── rank.py                 # a CLI tool demonstrating how to call ranked_games
├── web/                    # Cloud Run service
│   ├── app.py              # Flask app
│   └── templates/          # Jinja2 pages
├── ingest/                 # Cloud Run Job
│   ├── igdb.py
│   └── steam.py
└── tools/
    └── build_dev_db.py     # builds a small sample database
```

## Local setup

```bash
# 1. clone, then from the repo folder:
python -m venv .venv

# 2. activate it
.\.venv\Scripts\Activate.ps1 # Windows
source .venv/bin/activate    # macOS/Linux

# 3. install dependencies
pip install -r requirements.txt

# 4. build a local dev database (real games to develop against, NO keys needed)
python tools/build_dev_db.py

# 5. (INGESTION ONLY) only if you'll run the scrapers, create your .env and add keys:
copy .env.example .env      # Windows
cp   .env.example .env      # macOS/Linux
```

The `.venv/` folder and your real `.env` are **git-ignored** - they stay on your
machine. Never commit the database file, the venv, or any API keys.

## Secrets

Two kinds, needed by different parts:

- API keys - Twitch client id/secret, which ARE your IGDB credentials (IGDB
  authenticates through Twitch; Steam needs no key). Needed ONLY to run the
  ingestion scripts (ingest/). The web app and local development don't need them.
- Database credentials - the Cloud SQL host/user/password. Needed by whatever
  connects to the cloud database (the deployed web service, and ingestion when
  pointed at Cloud SQL). NOT needed for local dev, which runs against the dev DB
  built above.

Both live in a local, git-ignored .env during development and in Cloud Run
environment variables when deployed. .env.example lists the variable names with
no real values. Never put a real key or connection string in a committed file.
