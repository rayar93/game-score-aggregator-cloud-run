# Team AAA - Summer 2026 - Game database
CS 3537 Cloud Computing - **Team AAA**: Alan Ray, Anthony Samson, Aaron White.

A videogame database and discovery tool that's more searchable, filterable, and
complete than existing sites. It ingests game data and ratings from **IGDB**,
**Steam**, and possibly later **OpenCritic** into a normalized **Cloud SQL**
database, then blends critic and user scores, letting users search and filter 
by genre, platform, developer, publisher, release year, and minimum rating counts.

**Live app:** https://gamedb-web-849131695635.us-east1.run.app/

## Architecture

The whole application runs on **Cloud Run** as Docker containers:

- **Web service** (`web/` + `db.py`) - a containerized Flask app that serves the
  frontend and all search/filter/ranking queries. The only part that talks to the
  database, and all database access happens in the cloud.
- **Cloud SQL (PostgreSQL)** - the cloud-hosted relational database. There is one
  shared instance; everyone develops against it directly (see [Working against the shared 
  database](#working-against-the-shared-database)).
- **Ingestion job** (`ingest/` + `db.py`) - the data-fetching scripts, run as a
  Cloud Run job on a Cloud Scheduler trigger to keep the catalog current (see 
  [Ingestion](#ingestion)).

## Repository layout
```
team-AAA-summer2026/
├── README.md
├── requirements.txt                # install into your venv: pip install -r requirements.txt
├── .gitignore
├── Dockerfile                      # builds the Cloud Run image
├── Dockerfile.ingest               # builds the ingestion image
├── .dockerignore
├── Dockerfile.ingest.dockerignore
├── .env.example                    # template; copy to a local .env (see Setup)
├── schema.sql                      # database schema (PostgreSQL)
├── db.py                           # shared data-access layer
├── rank.py                         # a CLI tool demonstrating how to call ranked_games
├── web/                            # Cloud Run service
│   ├── app.py                      # Flask app
│   └── templates/                  # Jinja2 pages
└── ingest/                         # Cloud Run Job - WRITES to the shared DB; be careful
    ├── run_refresh.py
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

# 6. create your .env from the template, then fill in DB_PASSWORD (see Secrets below)
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

**Google Secret Manager is the canonical home of every secret.** Nothing is
distributed by hand: deployed containers get secrets injected as environment
variables at container start (`--set-secrets`), and for local development you
pull the same values into a git-ignored `.env` yourself:

    gcloud secrets versions access latest --secret=[SECRET NAME]

The code can't tell the difference - `db.py` and the ingest scripts just read
environment variables in both worlds.

There are four secrets. Which ones you need depends on what you're running:

| Secret Manager name    | Goes in env var        | Needed by                                  |
|------------------------|------------------------|--------------------------------------------|
| `db-password-web`      | `DB_PASSWORD`          | web app + **all local development** (DB user `gamedb_web`) |
| `db-password-ingest`   | `DB_PASSWORD`          | ingestion job only (DB user `gamedb_ingest`) |
| `twitch-client-id`     | `TWITCH_CLIENT_ID`     | ingestion job only (IGDB authenticates through Twitch; Steam needs no key) |
| `twitch-client-secret` | `TWITCH_CLIENT_SECRET` | ingestion job only                         |

For normal development you only need `db-password-web` - it pairs with
`DB_USER=gamedb_web`, which `.env.example` already sets. The web app never
touches the Twitch keys.

The ingest credentials matter only in the rare, deliberate case of running the
ingestion scripts locally (see [Working against the shared
database](#working-against-the-shared-database)): set `DB_USER=gamedb_ingest`,
with `DB_PASSWORD` from `db-password-ingest`, plus both Twitch values.

`.env.example` lists the variable names with no real values. **Never put a
real key or password in a committed file.** If `gcloud secrets versions access`
gives a permissions error, you're missing the Secret Manager accessor role on
the project - fix the IAM grant rather than sharing values over chat.

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

## Deployment (web service)

The web service runs on Cloud Run and connects to Cloud SQL through the
Cloud Run <-> Cloud SQL socket integration, so it needs no proxy - that's
local-dev only.

Redeploy after a code change (build from the repo root):

    docker build -t gamedb-web .
    docker tag gamedb-web us-east1-docker.pkg.dev/rayar-cs3537-2026/gamedb/gamedb-web:[VERSION TAG]
    docker push us-east1-docker.pkg.dev/rayar-cs3537-2026/gamedb/gamedb-web:[VERSION TAG]
    gcloud run deploy gamedb-web \
      --image us-east1-docker.pkg.dev/rayar-cs3537-2026/gamedb/gamedb-web:[VERSION TAG] \
      --region us-east1 --allow-unauthenticated --port 8080 \
      --add-cloudsql-instances rayar-cs3537-2026:us-east1:gamedb-pg \
      --set-env-vars "DB_HOST=/cloudsql/rayar-cs3537-2026:us-east1:gamedb-pg,DB_NAME=gamedb,DB_USER=gamedb_web" \
      --set-secrets "DB_PASSWORD=db-password-web:latest"

**The `--set-secrets` line is not optional.** It's what injects the database
password from Secret Manager. A deploy that instead passes a plaintext
`DB_PASSWORD` env var reverts the service to the pre-Secret-Manager setup.

## Ingestion

The ingestion job (`ingest/run_refresh.py` in the `gamedb-ingest` image) refreshes
the catalog in two passes: an IGDB catalog walk that upserts every game, then a
self-limiting Steam pass that fills in reviews/details for whatever's missing.
Both passes are idempotent, so re-running is safe.

**Schedule: it runs as a Cloud Run job every other day at 3:00 AM ET**, triggered
by Cloud Scheduler (job `gamedb-ingest-scheduler-trigger`, cron `0 3 */2 * *`,
America/New_York). If you change the schedule, update this paragraph too - it's
the only place the cadence is written down.

To change the schedule:

    gcloud scheduler jobs update http gamedb-ingest-scheduler-trigger \
      --location=us-east1 --schedule="0 3 */2 * *" --time-zone="America/New_York"

(`gcloud scheduler jobs list --location=us-east1` shows the job name.)

Redeploy the ingestion image after a code change (build from the repo root;
`gcloud run jobs update` with only `--image` keeps the job's existing secrets
and env vars from Secret Manager):

    docker build -f Dockerfile.ingest -t gamedb-ingest .
    docker tag gamedb-ingest us-east1-docker.pkg.dev/rayar-cs3537-2026/gamedb/gamedb-ingest:[VERSION TAG]
    docker push us-east1-docker.pkg.dev/rayar-cs3537-2026/gamedb/gamedb-ingest:[VERSION TAG]
    gcloud run jobs update gamedb-ingest \
      --image us-east1-docker.pkg.dev/rayar-cs3537-2026/gamedb/gamedb-ingest:[VERSION TAG] \
      --region us-east1

(`gcloud run jobs list --region=us-east1` shows the job name.)

To force a refresh right now instead of waiting for the schedule:
`gcloud run jobs execute gamedb-ingest --region=us-east1` - but remember
this **writes to the shared database**, so treat it like running the ingest
scripts by hand: only when the team has agreed to refresh.
