"""
Deployment settings, all from the environment.

Nothing here reads the per-user settings file; that is appconfig.py. This
module is about where the machine keeps its state, so it has to be answerable
before any user exists.
"""

import os
from pathlib import Path

HOME_STATE = Path.home() / ".viral_clipper"

# ---------- database ----------

# Postgres is the target. A URL-less run falls back to a SQLite file so a
# fresh clone starts without installing a server, but single-writer SQLite
# will not survive more than one worker, so hosting means setting this.
#   postgresql+psycopg://clipper:pass@localhost:5432/clipper
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

SQL_ECHO = os.environ.get("CLIPPER_SQL_ECHO", "").lower() in ("1", "true", "yes")


def database_url():
    if DATABASE_URL:
        return DATABASE_URL
    HOME_STATE.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{(HOME_STATE / 'clipper.db').as_posix()}"


def is_postgres():
    return database_url().startswith("postgresql")


# ---------- queue ----------

# Redis carries the dispatch signal so a queued job starts in milliseconds
# instead of on the next poll. Job state itself lives in the database, so an
# absent or dead Redis costs latency, never work.
REDIS_URL = os.environ.get("REDIS_URL", "").strip()

QUEUE_KEY = os.environ.get("CLIPPER_QUEUE_KEY", "clipper:jobs")
WORKER_NAME = os.environ.get("CLIPPER_WORKER_NAME", "")

# A worker touches its heartbeat while it holds a job. Miss this window and
# the reaper assumes the process died and hands the job back to the queue.
HEARTBEAT_SECONDS = int(os.environ.get("CLIPPER_HEARTBEAT_SECONDS", "20"))
STALE_AFTER_SECONDS = int(os.environ.get("CLIPPER_STALE_AFTER_SECONDS", "90"))
MAX_ATTEMPTS = int(os.environ.get("CLIPPER_MAX_ATTEMPTS", "3"))

# How long an idle worker waits on the queue before looping to run its
# housekeeping (reaper, stale claims) and waiting again.
QUEUE_BLOCK_SECONDS = int(os.environ.get("CLIPPER_QUEUE_BLOCK_SECONDS", "5"))


# ---------- object storage ----------

# "local" writes under STORAGE_DIR and serves bytes back through the API.
# "s3" speaks to anything S3-shaped: S3, R2, B2, MinIO, Spaces.
STORAGE_BACKEND = os.environ.get("CLIPPER_STORAGE", "local").strip().lower()
STORAGE_DIR = Path(os.environ.get("CLIPPER_STORAGE_DIR", str(HOME_STATE / "media")))

S3_BUCKET = os.environ.get("CLIPPER_S3_BUCKET", "").strip()
S3_ENDPOINT = os.environ.get("CLIPPER_S3_ENDPOINT", "").strip()
S3_REGION = os.environ.get("CLIPPER_S3_REGION", "auto").strip()
S3_PREFIX = os.environ.get("CLIPPER_S3_PREFIX", "clips").strip().strip("/")
S3_ACCESS_KEY = os.environ.get("CLIPPER_S3_ACCESS_KEY", "").strip()
S3_SECRET_KEY = os.environ.get("CLIPPER_S3_SECRET_KEY", "").strip()

# Public base for stored objects, e.g. a CDN or R2 public bucket domain.
# Instagram will only accept a reel from a URL it can fetch itself, so
# posting to it later needs this set to something reachable from outside.
S3_PUBLIC_BASE = os.environ.get("CLIPPER_S3_PUBLIC_BASE", "").strip().rstrip("/")
SIGNED_URL_SECONDS = int(os.environ.get("CLIPPER_SIGNED_URL_SECONDS", "86400"))


# ---------- secrets ----------

# Fernet key for account tokens at rest. Generate with:
#   python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
SECRET_KEY = os.environ.get("CLIPPER_SECRET_KEY", "").strip()


# ---------- web ----------

HOST = os.environ.get("CLIPPER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLIPPER_PORT", "8420"))

# A plain "python server.py" runs a worker inside the web process, so one
# command still gets you a working app. Hosting means setting this to 0 and
# running worker.py separately, as many as the box can feed.
INLINE_WORKER = os.environ.get("CLIPPER_INLINE_WORKER", "1").lower() in ("1", "true", "yes")


def summary():
    """One-line-per-item description for startup logging and /api/health."""
    url = database_url()
    safe = url
    if "@" in url:                        # never log the password
        head, tail = url.split("@", 1)
        safe = head.split("//")[0] + "//***@" + tail
    return {
        "database": safe,
        "database_kind": "postgres" if is_postgres() else "sqlite",
        "redis": bool(REDIS_URL),
        "storage": STORAGE_BACKEND,
        "storage_target": S3_BUCKET if STORAGE_BACKEND == "s3" else str(STORAGE_DIR),
        "secret_key_set": bool(SECRET_KEY),
        "inline_worker": INLINE_WORKER,
    }
