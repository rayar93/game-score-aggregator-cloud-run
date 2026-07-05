# Dockerfile - packages the Flask web service for Cloud Run.
#
# Build from the repo root, not from web/:
#     docker build -t gamedb-web .
# The build context must be the root because db.py lives there (shared with the
# ingest scripts) and the app imports it. A build context inside web/ can't reach
# a file one level up, so COPY db.py would be impossible.
#
# Run locally to confirm the container works before deploying (Task 8):
#     docker run -p 8080:8080 --env-file .env gamedb-web
# then open http://localhost:8080  (the --env-file passes your DB_* vars in;
# the Cloud SQL Auth Proxy must be reachable from the container - see the notes
# at the bottom about why localhost differs inside a container).

FROM python:3.12-slim

# Don't buffer stdout/stderr - logs show up in Cloud Run immediately. And don't
# write .pyc files into the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies FIRST, as their own layer. Docker caches layers, so as long
# as requirements.txt doesn't change, rebuilds skip re-installing everything even
# when the app code changed. Copy just the requirements, install, THEN copy code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the shared data layer (from the repo root) and the web app. This is the
# whole reason the build context is the root: db.py is not inside web/.
COPY db.py .
COPY schema.sql .
COPY web/ ./web/

# Cloud Run sends traffic to whatever port the container listens on, provided via
# the PORT env var (defaults to 8080). gunicorn is the production WSGI server -
# Flask's built-in server is for development only.
#
# `web.app:app` means: in the module web/app.py, serve the Flask object named `app`.
# The shell form lets $PORT expand at runtime (Cloud Run sets it).
ENV PORT=8080
CMD ["sh", "-c", "exec gunicorn --bind :$PORT --workers 2 --threads 4 --timeout 60 web.app:app"]
