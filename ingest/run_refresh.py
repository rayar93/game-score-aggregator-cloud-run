"""
run_refresh.py - the entrypoint for the scheduled ingestion Cloud Run Job.

Runs the full refresh as two ordered passes:
  1. igdb.py --all    walk the IGDB catalog, upserting every game (picks up new
                      releases at the tail; idempotent, so re-walking is safe).
  2. steam.py         enrich with Steam reviews + details. steam.py is
                      self-limiting (_games_needing only touches games missing a
                      given endpoint), so this only does real work for games that
                      lack it - new games from pass 1, plus any prior gaps.

Run locally exactly as the container will:
    python -m ingest.run_refresh
"""
import subprocess
import sys
import time


def _run(label, args):
    """Run one pass as a subprocess, streaming its output, and time it."""
    print(f"\n===== {label}: starting =====", flush=True)
    start = time.time()
    # sys.executable = the same python running this script, so it works the same
    # locally and in the container. -m runs the module form (path-independent).
    result = subprocess.run([sys.executable, "-m", *args])
    elapsed = time.time() - start
    print(f"===== {label}: exited {result.returncode} after {elapsed:.0f}s =====", flush=True)
    return result.returncode


def main():
    # Pass 1: IGDB catalog walk. If it fails, stop - no point enriching nothing new.
    rc = _run("IGDB catalog walk", ["ingest.igdb", "--all"])
    if rc != 0:
        print("IGDB pass failed; skipping Steam pass.", flush=True)
        return rc

    # Pass 2: Steam enrichment (self-limiting; only fills gaps).
    rc = _run("Steam enrichment", ["ingest.steam", "--details"])
    return rc


if __name__ == "__main__":
    sys.exit(main())