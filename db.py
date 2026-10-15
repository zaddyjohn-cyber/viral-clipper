"""
Schema and session handling.

Six tables, in the order work flows through them: a job downloads and scores a
video, which produces clips, which get posted to accounts, which report
metrics back. The last two are not driven by anything yet; they are here
because posting is the next layer and a schema change costs more later.

Engine code never imports this. Only server.py and worker.py do.
"""

import uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint,
                        create_engine)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker)

import settings

# JSONB where we have it, plain JSON on SQLite.
Json = JSON().with_variant(JSONB, "postgresql")


def now():
    return datetime.now(timezone.utc)


def new_id():
    return uuid.uuid4().hex[:12]


def as_utc(dt):
    """SQLite hands back naive datetimes; Postgres hands back aware ones.

    Everything written here is UTC, so a naive value only ever needs its
    label put back on before it can be compared with now().
    """
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------- statuses ----------

QUEUED, RUNNING, DONE, ERROR, CANCELLED = (
    "queued", "running", "done", "error", "cancelled")
STAGES = ["download", "transcribe", "score", "ready"]

CLIP_PENDING, CLIP_EXPORTING, CLIP_EXPORTED, CLIP_FAILED = (
    "pending", "exporting", "exported", "failed")

TASK_ANALYZE, TASK_EXPORT = "analyze", "export"


class Job(Base):
    """One source video being worked on, from link to exported clips.

    Also the queue record: status, attempts and the claim fields below are what
    a worker locks against, so a job is never lost to a restart.
    """
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)

    url: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(String(32), default="Video")

    task: Mapped[str] = mapped_column(String(16), default=TASK_ANALYZE)
    stage: Mapped[str] = mapped_column(String(16), default="download")
    status: Mapped[str] = mapped_column(String(16), default=QUEUED, index=True)
    message: Mapped[str] = mapped_column(Text, default="Queued")
    percent: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)

    meta: Mapped[dict] = mapped_column(Json, default=dict)

    # Paths on the worker that produced them. Kept so a retry can skip work
    # that already finished; never trusted without an exists() check.
    temp_dir: Mapped[str | None] = mapped_column(Text)
    audio_path: Mapped[str | None] = mapped_column(Text)
    video_path: Mapped[str | None] = mapped_column(Text)

    exporting: Mapped[bool] = mapped_column(Boolean, default=False)
    fetching_video: Mapped[bool] = mapped_column(Boolean, default=False)
    export_error: Mapped[str | None] = mapped_column(Text)
    export_request: Mapped[list | None] = mapped_column(Json)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    claimed_by: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    clips: Mapped[list["Clip"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Clip.n")
    transcript: Mapped["Transcript | None"] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False)


Index("ix_jobs_claim", Job.status, Job.created_at)


class Transcript(Base):
    """Word-timed segments, split off so reading job status stays cheap.

    Kept after the run finishes: captions need it at export time, and the
    scoring feedback loop will need it to learn which windows actually worked.
    """
    __tablename__ = "transcripts"

    job_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    segments: Mapped[list] = mapped_column(Json, default=list)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    engine_used: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now)

    job: Mapped["Job"] = relationship(back_populates="transcript")


class Clip(Base):
    """A scored window, and the render of it once exported."""
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    n: Mapped[int] = mapped_column(Integer)

    title: Mapped[str] = mapped_column(Text, default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    # The written hook burned across the top of the clip, as opposed to
    # `hook`, which is the opening line the speaker actually said.
    hook_card: Mapped[str] = mapped_column(Text, default="", server_default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[int] = mapped_column(Integer, default=0, index=True)

    # What was said, the scorer's tags, and the transcript line range this came
    # from. The line range is what lets a later pass re-cut the same moment.
    text: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(Json, default=list)
    lines: Mapped[list] = mapped_column(Json, default=list)

    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    thumb: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(String(16), default=CLIP_PENDING)
    percent: Mapped[float] = mapped_column(Float, default=0.0)
    export_error: Mapped[str | None] = mapped_column(Text)

    file_path: Mapped[str | None] = mapped_column(Text)
    asset_key: Mapped[str | None] = mapped_column(Text)
    asset_url: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now)

    job: Mapped["Job"] = relationship(back_populates="clips")
    posts: Mapped[list["Post"]] = relationship(
        back_populates="clip", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("job_id", "n", name="uq_clips_job_n"),)


class Account(Base):
    """A connected destination: one channel, page or profile.

    Tokens are encrypted by crypto.py before they land here, and a run without
    CLIPPER_SECRET_KEY refuses to write them rather than storing them bare.
    """
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)

    platform: Mapped[str] = mapped_column(String(32), index=True)
    handle: Mapped[str] = mapped_column(String(190), default="")
    display_name: Mapped[str] = mapped_column(Text, default="")
    external_id: Mapped[str | None] = mapped_column(String(190))

    status: Mapped[str] = mapped_column(String(16), default="active")
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str | None] = mapped_column(Text)

    # Per-platform ceilings differ and move, so they are data, not constants:
    # Instagram answers its own limit on request, TikTok caps posts per day.
    daily_post_limit: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict] = mapped_column(Json, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now)

    posts: Mapped[list["Post"]] = relationship(back_populates="account")

    __table_args__ = (
        UniqueConstraint("platform", "external_id",
                         name="uq_accounts_platform_external"),
    )


class Post(Base):
    """One clip sent to one account. Variants of the same clip are separate rows."""
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    clip_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)

    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    external_post_id: Mapped[str | None] = mapped_column(String(190))
    permalink: Mapped[str | None] = mapped_column(Text)

    caption: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[str] = mapped_column(Text, default="")
    # Which cut this was, so metrics can say which hook won.
    variant: Mapped[str | None] = mapped_column(String(40))

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now)

    clip: Mapped["Clip"] = relationship(back_populates="posts")
    account: Mapped["Account"] = relationship(back_populates="posts")


class Metric(Base):
    """A reading of one post's numbers at a fixed age.

    Fixed windows rather than a running total, so two posts are comparable and
    the scorer can be trained on outcomes instead of raw view counts.
    """
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    window: Mapped[str] = mapped_column(String(8))        # 1h, 24h, 7d, final
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now)

    views: Mapped[int | None] = mapped_column(BigInteger)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    watch_time_ms: Mapped[int | None] = mapped_column(BigInteger)
    avg_watch_pct: Mapped[float | None] = mapped_column(Float)

    raw: Mapped[dict] = mapped_column(Json, default=dict)

    __table_args__ = (
        UniqueConstraint("post_id", "window", name="uq_metrics_post_window"),
    )


# ---------- engine and sessions ----------

_engine = None
_Session = None


def engine():
    global _engine
    if _engine is None:
        url = settings.database_url()
        kwargs = {"echo": settings.SQL_ECHO, "future": True}
        if url.startswith("sqlite"):
            # The API and the worker are separate processes sharing one file.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=5)
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            from sqlalchemy import event

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")   # concurrent readers
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
    return _engine


def Session():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _Session()


@contextmanager
def session():
    s = Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def upgrade():
    """Bring the database up to head. Called on API and worker startup.

    Both processes do this on purpose: either can be the first to start, and
    alembic takes its own lock, so whichever loses the race just waits.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).parent / "alembic.ini"))
    cfg.set_main_option("script_location",
                        str(Path(__file__).parent / "migrations"))
    command.upgrade(cfg, "head")
