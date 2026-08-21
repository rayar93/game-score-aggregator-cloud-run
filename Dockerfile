# Dockerfile.frozen - the whole application in one image, no database server.
#
# The original Dockerfile builds a container that talks to Cloud SQL over the
# Cloud Run <-> Cloud SQL socket. This one carries the data with it: a 423 MB
# read-only SQLite snapshot sits inside the image, so the running service has no
# database to connect to, no password to inject, and nothing billing by the hour
# while nobody is visiting.
#
# Build from the repo root, with gamedb.sqlite present there:
#
#     sqlite3 gamedb.sqlite < prepare_snapshot.sql     # once, before building
#     docker build -f Dockerfile.frozen -t gamedb-web .
#
# gamedb.sqlite is deliberately NOT in git - at 423 MB it exceeds GitHub's
# 100 MB file limit. It lives as a GitHub Release asset (2 GB limit) and is
# downloaded before the build. See README.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Only Flask and gunicorn now. The frozen build never imports db.py, so psycopg
# and its bundled libpq are dead weight - dropping them takes tens of MB off an
# image that is already large because of the data.
COPY requirements-frozen.txt .
RUN pip install --no-cache-dir -r requirements-frozen.txt

# Application code. db.py and schema.sql are intentionally absent: the write
# path and the Postgres DDL have no role in a read-only deployment.
COPY db_sqlite.py .
COPY web/ ./web/

# The data. Last, and in its own layer, because it is by far the largest thing
# here and almost never changes - so edits to the app rebuild in seconds instead
# of re-pushing 423 MB.
COPY gamedb.sqlite .

# Cloud Run's docs are explicit that image size does not count against the
# instance memory limit, so the snapshot rides along without consuming RAM.
# Only what the process allocates does.
ENV DB_PATH=/app/gamedb.sqlite

ENV PORT=8080
CMD ["sh", "-c", "exec gunicorn --bind :$PORT --workers 2 --threads 4 --timeout 60 web.app:app"]
