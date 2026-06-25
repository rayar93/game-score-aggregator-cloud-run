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
├── schema.sql              # database schema (PostgreSQL)
├── db.py                   # shared data-access layer
├── web/                    # Cloud Run service
│   ├── app.py              # Flask app
│   └── templates/          # Jinja2 pages
└── ingest/                 # Cloud Run Job
    ├── igdb.py
    └── steam.py
```

## Local setup

```bash
# 1. clone, then from the repo folder:
python -m venv .venv

# 2. activate it
#   Windows (PowerShell):  .\.venv\Scripts\Activate.ps1
#   macOS/Linux:           source .venv/bin/activate

# 3. install dependencies
pip install -r requirements.txt

# 4. create your local .env from the template and add your keys
#   copy .env.example .env      (Windows)
#   cp   .env.example .env      (macOS/Linux)
```

The `.venv/` folder and your real `.env` are **git-ignored** - they stay on your
machine. Never commit the database file, the venv, or any API keys.

## Secrets

API keys (Twitch/IGDB) and the Cloud SQL connection string live in a local,
git-ignored `.env` during development and in Cloud Run environment variables when
deployed. `.env.example` lists which variables are needed, with no real values.
(Formal Secret Manager integration is a Sprint 2 task.)

## Sprints

1. **Cloud slice (due 7/7):** Cloud SQL instance up, schema + existing data
   migrated, `db.py` ported to Postgres, a minimal Flask app containerized and
   deployed to Cloud Run, wired to Cloud SQL. *Definition of done: a public Cloud
   Run URL returning real games from Cloud SQL.*
2. **Build-out (due 7/21):** filters as web controls + the user-configurable
   weighting UI; ingestion as a scheduled Cloud Run Job; Secret Manager.
3. **Stretch + presentation (due 8/4):** accounts/ratings, ML recommendations,
   demo and slides.
