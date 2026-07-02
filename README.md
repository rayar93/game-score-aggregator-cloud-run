# Team AAA - Summer 2026 - Game database
CS 3537 Cloud Computing - **Team AAA**: Alan Ray, Anthony Samson, Aaron White.

A videogame database and discovery tool that's more searchable, filterable, and
complete than existing sites. It ingests game data and ratings from **IGDB**,
**Steam**, and possibly later **OpenCritic** into a normalized **Cloud SQL**
database, then blends critic and user scores with user-configurable weighting,
letting users search and filter by genre, platform, developer, publisher,
release year, and minimum rating counts.

## Architecture

The whole application runs on **Cloud Run** as Docker containers:

- **Web service** (`web/` + `db.py`) - a containerized Flask app that serves the
  frontend and all search/filter/ranking queries. The only part that talks to the
  database, and all database access happens in the cloud.

  Building the web app? Start with `rank.py` - it's a worked example of every
  `db.py` call you'll need (get_connection, ranked_games, genres_for,
  attributes_for), so you don't have to read the whole data layer.
- **Cloud SQL (PostgreSQL)** - the cloud-hosted relational database. There is one
  shared instance; everyone develops against it directly (see below).
- **Ingestion job** (`ingest/` + `db.py`) - the data-fetching scripts, run as a
  Cloud Run job on a Cloud Scheduler trigger to keep the catalog current.

Stretch goals: user accounts (Identity Platform) + personal ratings, and
ML-based recommendations from per-game metadata.

## Repository layout
```
team-AAA-summer2026/
├── README.md
├── requirements.txt           # install into your venv: pip install -r requirements.txt
├── .gitignore
├── .env.example               # template; copy to a local .env (see Setup)
├── schema.sql                 # database schema (PostgreSQL)
├── db.py                      # shared data-access layer
├── rank.py                    # a CLI tool demonstrating how to call ranked_games
├── web/                       # Cloud Run service
│   ├── app.py                 # Flask app
│   └── templates/             # Jinja2 pages
└── ingest/                    # Cloud Run Job - WRITES to the shared DB; be careful
    ├── igdb.py
    └── steam.py

```

## Setup

We develop directly against the shared **Cloud SQL** instance through the Cloud
SQL Auth Proxy - a small local program that opens a secure tunnel so your code can
reach the cloud database. It authenticates with your Google account, so there's 
no IP allowlisting or public exposure to deal with.

One-time setup:

```bash
# 1. clone, then from the repo folder, make a venv:
python -m venv .venv

# 2. activate it
.\.venv\Scripts\Activate.ps1   # Windows (PowerShell)
source .venv/bin/activate      # macOS/Linux

# 3. install dependencies
pip install -r requirements.txt

# 4. authenticate for the proxy (opens a browser)
gcloud auth application-default login

# 5. install the Cloud SQL Auth Proxy
gcloud components install cloud-sql-proxy

# 6. create your .env from the template, then get the DB password from Alan
copy .env.example .env         # Windows
cp   .env.example .env         # macOS/Linux
```

**Every time you develop:** start the proxy in its own terminal and leave it open:

```bash
cloud-sql-proxy rayar-cs3537-2026:us-east1:gamedb-pg
```

It should print that it's listening and then sit quietly. 
In a second terminal (venv active), run `python rank.py 20`;
if you get 20 real games back, you're wired up and can start the app.

If the proxy errors about credentials, your login expired - re-run
`gcloud auth application-default login`.

## Secrets

Two kinds, needed by different parts:

- **Database credentials** - the Cloud SQL password, in your `.env`. Needed by
  **anything that connects to the database, which now includes local development**,
  since we develop against the shared instance. Get the password from Alan; don't
  commit it.
- **API keys** - Twitch client id/secret, which are also your IGDB credentials (IGDB
  authenticates through Twitch; Steam needs no key). Needed **only** to run the
  ingestion scripts (`ingest/`). The web app doesn't touch them.

Both live in a local, git-ignored `.env` during development and in Cloud Run
environment variables when deployed. `.env.example` lists the variable names with
no real values. **Never put a real key or password in a committed file.**

## Working against the shared database

There is **one** Cloud SQL instance and we all develop against it. Two things
follow from that:

- **Reads are safe to share.** The web app and `rank.py` only read, so everyone
  querying at once is fine.
- **Do NOT run the ingestion scripts (`ingest/igdb.py`, `ingest/steam.py`) casually.**
  They **write** to the shared database - the same data the app and the demo depend
  on. Running them (especially `--all`) mutates everyone's data. The catalog is
  already loaded; ingestion only needs to run deliberately, when we've agreed to
  refresh it.
