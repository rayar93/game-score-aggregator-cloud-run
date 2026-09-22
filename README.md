# Game Score Aggregator (Cloud Run)

A video game discovery tool that blends critic and user scores from IGDB, Steam, and Metacritic into a single ranking across **310,480 titles**, searchable and filterable by genre, platform, developer, publisher, release year, and rating-count floors.

Team project - CS 3537 Cloud Computing, Appalachian State University.

**Live:** https://gamedb-web-603466261030.us-east1.run.app

Originally built by **Alan Ray, Anthony Samson, and Aaron White**. Since the course ended it has been re-architected to run at effectively zero cost; the original Cloud SQL design is preserved in `Dockerfile.cloudsql` and described below.

## How it works

The whole application is one Cloud Run container with **no database server**. A read-only SQLite snapshot of the catalog is baked into the image, so a request is served entirely from local disk — no network hop, no connection pool, no instance billing by the hour while nobody is visiting.

```
                    ┌─────────────────────────────────┐
   request  ───────▶│  Cloud Run (scales to zero)     │
                    │  ┌───────────────────────────┐  │
                    │  │ Flask + gunicorn          │  │
                    │  │ web/app.py                │  │
                    │  ├───────────────────────────┤  │
                    │  │ db_sqlite.py (read-only)  │  │
                    │  ├───────────────────────────┤  │
                    │  │ gamedb.sqlite   423 MB    │  │
                    │  └───────────────────────────┘  │
                    └─────────────────────────────────┘
```

Every filter in the UI maps one-to-one onto a query-string parameter, so any view is a shareable URL — `?q=zelda`, `?genre=Adventure&min_score=85`, `?sort=popularity&min_users=4000`.

### The ranking

- **Critic score** — a plain average of the usable critic sources: IGDB's critic rating (dropped from the average when its review count falls below the threshold, as a noise filter) and Metacritic (always used when present; it carries no review count, so it can't be count-weighted).
- **User score** — a count-weighted average of Steam's percent-positive and IGDB's user rating, weighted by how many people are behind each.
- **Final** — `critic × w + user × (1 − w)`, with `w` exposed in the UI as a slider.

A game needs both a critic and a user score to appear in the ranking. Search mode relaxes that, returning partial-score games with nulls.

## Why it's built this way

The graded version ran a Cloud SQL PostgreSQL instance (`db-g1-small`) behind Cloud Run, with the ingestion job on a Cloud Scheduler trigger and credentials in Secret Manager. It worked, and it cost about **$1/day** — because a managed database bills continuously whether or not anyone visits, while Cloud Run bills only per request.

For a project whose data no longer needs to change, that's the wrong trade. The rebuild made three findings:

**The database was mostly staging data.** `raw_fetch` — the landing table holding every raw IGDB and Steam API response as JSONB — was **5,412 MB of the 6,168 MB database, 87.7%**. Nothing in the serving path reads it; its only consumer was an existence check in the ingest pipeline. Excluding it left ~756 MB of actual serving data.

**Postgres row overhead dominated what remained.** `game_attribute` is 3.4M rows of two integers — 16 bytes of payload each — occupying 340 MB, roughly 100 bytes per row once the 23-byte tuple header, alignment padding, and index are counted. SQLite stores the same table far more compactly. The 756 MB became a **423 MB** file including indexes.

**The query layer was already portable.** The ranking query used no Postgres-only syntax — no `ILIKE`, no `array_agg`, no JSONB operators, no casts. Porting the read path meant changing `%(name)s` placeholders to `:name` and swapping the row factory. The SQL itself is unchanged.

Result: **~$1/day → effectively $0**, inside Cloud Run's free tier.

### What was fixed along the way

Comparing the two backends surfaced a real bug: no `ORDER BY` ended in a unique column, so rows tied on the sort key had no defined order — nondeterministic in Postgres too, across query plans. Every ordering now ends in `game_id`. The alphabetical tiebreak also moved from `canonical_title` to `normalized_title`, which is already lowercased and punctuation-stripped, so `Zelda's Adventure` and `Zeldas Adventure` sort together instead of pages apart.

## The snapshot

`gamedb.sqlite` is **not in this repository** — at 423 MB it exceeds GitHub's 100 MB file limit. It's published as a **[Release](../../releases)** asset and downloaded before building.

| Table | Rows |
|---|---:|
| game | 310,480 |
| game_attribute | 3,366,527 |
| game_company | 510,661 |
| game_source_ref | 457,810 |
| game_score | 192,476 |
| company | 137,942 |
| attribute | 9,462 |

Provenance lives in the file itself — `SELECT * FROM snapshot_meta` records when it was taken, what it came from, and what was excluded.

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-frozen.txt

# fetch gamedb.sqlite from the Releases page into the repo root, then:
python db_sqlite.py                # smoke test: prints metadata and the top 5
gunicorn --bind :8080 web.app:app  # http://localhost:8080
```

No credentials, no proxy, no `.env`. The serving path needs nothing but Python and the snapshot file.

## Deploying

```bash
gcloud builds submit --tag us-east1-docker.pkg.dev/gamedb-rayar93/gamedb/gamedb-web:VERSION .

gcloud run deploy gamedb-web \
  --image us-east1-docker.pkg.dev/gamedb-rayar93/gamedb/gamedb-web:VERSION \
  --region us-east1 --allow-unauthenticated --port 8080 \
  --memory 1Gi --cpu 1 \
  --set-env-vars "DB_PATH=/app/gamedb.sqlite"
```

The image is ~208 MB compressed. `gamedb.sqlite` must be in the build context — note that `.dockerignore` excludes `*.db`, so the `.sqlite` extension is load-bearing.

## Refreshing the catalog

The ingestion pipeline is intact but is **not** part of the running service, and refreshing is deliberately a manual, offline operation:

1. Stand up PostgreSQL and apply `schema.sql`
2. Run `ingest/run_refresh.py` with `db.py` (needs Twitch/IGDB credentials from https://dev.twitch.tv/console; Steam needs no key)
3. `python pg_to_sqlite.py --out gamedb.sqlite`
4. `sqlite3 gamedb.sqlite < prepare_snapshot.sql`
5. Publish as a new Release asset, rebuild, redeploy

Both ingest passes are idempotent, so re-running is safe.

## Repository layout

```
Dockerfile                  # frozen build - what runs in production
Dockerfile.cloudsql         # the original Cloud SQL build, kept for reference
requirements-frozen.txt     # Flask + gunicorn (sqlite3 is stdlib)
requirements.txt            # full set, for ingestion and the Postgres path

web/app.py                  # Flask routes and filter parsing
web/templates/              # Jinja2

db_sqlite.py                # read-only SQLite data layer  <- production
db.py                       # PostgreSQL data layer, read + write  <- ingestion
schema.sql                  # PostgreSQL schema
rank.py                     # CLI demo of the ranking query

ingest/                     # IGDB + Steam ingestion (offline)
pg_to_sqlite.py             # builds the snapshot from Postgres
prepare_snapshot.sql        # precomputes the genre list into the snapshot
compare_backends.py         # diffs SQLite output against Postgres
```

## Known limitations

- The catalog is a **frozen snapshot**. Scores and new releases do not update until someone runs the refresh above.
- Title search is a case-insensitive substring match with no accent folding — `pokemon` won't find `Pokémon`. Postgres had `unaccent` + `pg_trgm` available; the SQLite build would need a normalized search column.
- IGDB and Steam name genres differently (`Role-playing (RPG)` vs `RPG`) and are not mapped to a shared taxonomy, so both spellings appear in the filter list.
- Cold starts pull a 208 MB image, so the first request after an idle period takes a few seconds. Subsequent requests are fast.

## Data

Game data is the property of IGDB, Steam, and Metacritic. It was collected for coursework and is redistributed here as a static snapshot for demonstration only.
