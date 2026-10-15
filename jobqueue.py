"""
The queue.

The database is the only record of what work exists. Redis is a doorbell: it
makes a queued job start in milliseconds instead of on the next poll. Lose
Redis and jobs still run, just up to QUEUE_BLOCK_SECONDS later, which is why
nothing here treats a Redis failure as fatal.

A claimed job carries the worker's name and a heartbeat. If that heartbeat
goes quiet the job is assumed orphaned and goes back to queued, where another
worker picks it up and resumes from the checkpoints engine.py already writes.
"""

import os
import socket
import time
from datetime import timedelta

from sqlalchemy import text

import db
import settings

_redis = None
_redis_checked = False


def worker_name():
    return settings.WORKER_NAME or f"{socket.gethostname()}-{os.getpid()}"


# ---------- redis, optional throughout ----------

def redis_client():
    """None whenever Redis is not configured or not answering."""
    global _redis, _redis_checked
    if _redis is not None or _redis_checked:
        return _redis
    _redis_checked = True
    if not settings.REDIS_URL:
        return None
    try:
        import redis as redis_lib
        client = redis_lib.from_url(
            settings.REDIS_URL, socket_timeout=5, socket_connect_timeout=3)
        client.ping()
        _redis = client
    except Exception:
        _redis = None
    return _redis


def redis_ok():
    c = redis_client()
    if not c:
        return False
    try:
        c.ping()
        return True
    except Exception:
        return False


# ---------- putting work in ----------

def enqueue(job_id, task=db.TASK_ANALYZE):
    """Mark a job queued and ring the doorbell."""
    with db.session() as s:
        s.execute(
            text("UPDATE jobs SET status=:q, task=:t, cancel_requested=:c "
                 "WHERE id=:i"),
            {"q": db.QUEUED, "t": task, "c": False, "i": job_id})
    c = redis_client()
    if c:
        try:
            c.rpush(settings.QUEUE_KEY, f"{job_id}:{task}")
        except Exception:
            pass        # the worker's poll will find it anyway


def wait_for_signal(seconds=None):
    """Block until a job is announced, or the timeout. False means timed out.

    The payload is thrown away on purpose: the claim below decides what to run,
    so a stale or duplicated signal can only ever cause one wasted poll.
    """
    seconds = seconds or settings.QUEUE_BLOCK_SECONDS
    c = redis_client()
    if not c:
        time.sleep(min(seconds, 2))
        return False
    try:
        return bool(c.blpop(settings.QUEUE_KEY, timeout=seconds))
    except Exception:
        time.sleep(min(seconds, 2))
        return False


def depth():
    """Signals waiting in Redis. Not the queue length; the database has that."""
    c = redis_client()
    if not c:
        return None
    try:
        return c.llen(settings.QUEUE_KEY)
    except Exception:
        return None


def queued_count():
    with db.session() as s:
        return s.execute(
            text("SELECT COUNT(*) FROM jobs WHERE status=:q"),
            {"q": db.QUEUED}).scalar() or 0


# ---------- taking work out ----------

_CLAIM_PG = """
WITH next_job AS (
    SELECT id FROM jobs
    WHERE status = :queued
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE jobs SET
    status = :running,
    claimed_by = :worker,
    claimed_at = :now,
    heartbeat_at = :now,
    attempts = attempts + 1,
    started_at = COALESCE(started_at, :now),
    error = NULL
WHERE id IN (SELECT id FROM next_job)
RETURNING id, task
"""

# SQLite has one writer at a time, so the guard in the outer WHERE is enough:
# a second worker that picked the same row updates nothing and loops.
_CLAIM_SQLITE = """
UPDATE jobs SET
    status = :running,
    claimed_by = :worker,
    claimed_at = :now,
    heartbeat_at = :now,
    attempts = attempts + 1,
    started_at = COALESCE(started_at, :now),
    error = NULL
WHERE id = (SELECT id FROM jobs WHERE status = :queued ORDER BY created_at LIMIT 1)
  AND status = :queued
"""


def claim(worker=None):
    """Take the oldest queued job. Returns (job_id, task) or None."""
    worker = worker or worker_name()
    args = {"queued": db.QUEUED, "running": db.RUNNING,
            "worker": worker, "now": db.now()}
    with db.session() as s:
        if settings.is_postgres():
            row = s.execute(text(_CLAIM_PG), args).first()
            return (row[0], row[1]) if row else None

        res = s.execute(text(_CLAIM_SQLITE), args)
        if not res.rowcount:
            return None
        row = s.execute(
            text("SELECT id, task FROM jobs WHERE claimed_by=:w AND status=:r "
                 "ORDER BY claimed_at DESC LIMIT 1"),
            {"w": worker, "r": db.RUNNING}).first()
        return (row[0], row[1]) if row else None


def heartbeat(job_id):
    with db.session() as s:
        s.execute(text("UPDATE jobs SET heartbeat_at=:now WHERE id=:i"),
                  {"now": db.now(), "i": job_id})


def reap_stale():
    """Hand back jobs whose worker stopped reporting. Returns how many."""
    cutoff = db.now() - timedelta(seconds=settings.STALE_AFTER_SECONDS)
    with db.session() as s:
        res = s.execute(
            text("UPDATE jobs SET status=:q, claimed_by=NULL, "
                 "message=:msg WHERE status=:r AND "
                 "(heartbeat_at IS NULL OR heartbeat_at < :cut) "
                 "AND attempts < :max"),
            {"q": db.QUEUED, "r": db.RUNNING, "cut": cutoff,
             "max": settings.MAX_ATTEMPTS,
             "msg": "Picking up where the last run left off"})
        requeued = res.rowcount or 0

        # Out of attempts, so stop pretending it is going to finish.
        s.execute(
            text("UPDATE jobs SET status=:e, message=:msg, error=:err, "
                 "finished_at=:now WHERE status=:r AND "
                 "(heartbeat_at IS NULL OR heartbeat_at < :cut) "
                 "AND attempts >= :max"),
            {"e": db.ERROR, "r": db.RUNNING, "cut": cutoff,
             "max": settings.MAX_ATTEMPTS, "now": db.now(),
             "msg": "Stopped after several tries",
             "err": "The worker handling this job stopped responding."})
    return requeued


# ---------- cancellation, across processes ----------

def request_cancel(job_id):
    """The API sets the flag; the worker notices within a second or two."""
    with db.session() as s:
        s.execute(text("UPDATE jobs SET cancel_requested=:c WHERE id=:i"),
                  {"c": True, "i": job_id})


def cancel_requested(job_id):
    with db.session() as s:
        val = s.execute(text("SELECT cancel_requested FROM jobs WHERE id=:i"),
                        {"i": job_id}).scalar()
    return bool(val)


def clear_cancel(job_id):
    with db.session() as s:
        s.execute(text("UPDATE jobs SET cancel_requested=:c WHERE id=:i"),
                  {"c": False, "i": job_id})
