"""
The worker. Claims jobs from the queue and runs engine.py against them.

Run:  python worker.py            one worker, as many as the box can feed
      CLIPPER_WORKER_NAME=cutter-1 python worker.py

The API never does this work itself unless CLIPPER_INLINE_WORKER is on, in
which case start() runs the same loop in a thread inside the web process.

Nothing here keeps state in memory. Progress, stage and cancellation all go
through the job row, so stopping this process mid-run loses at most the
current ffmpeg call: the next worker reclaims the job and engine.py's
checkpoints skip the download and transcription that already finished.
"""

import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

from sqlalchemy import delete, select

import appconfig
import db
import engine
import jobqueue
import settings
import storage

# Errors where a retry changes nothing, so the job stops and says why.
PERMANENT = ("api key was rejected", "needs a login", "signed-in session",
             "no speech was found", "not a video page", "too large",
             "is not installed", "did not point at any usable part",
             "not a video this tool", "keep it under 3 hours")

OPENING = {"download": "Looking up the video",
           "transcribe": "Starting up the transcriber",
           "score": "Preparing the transcript",
           "ready": "Finishing up"}


def log(*parts):
    print(f"[{time.strftime('%H:%M:%S')}]", *parts, flush=True)


class Progress:
    """Writes stage, message and percent to the job row, but not too often.

    engine.py reports progress per ffmpeg line, which is far more than a
    dashboard polling once a second can use, so writes are throttled. Stage
    changes and terminal messages always go through immediately.
    """

    MIN_GAP = 0.5           # seconds between ordinary writes
    MIN_STEP = 1.0          # or a percent move this big

    def __init__(self, job_id):
        self.job_id = job_id
        self.stage_name = "download"
        self.message = ""
        self.percent = 0.0
        self._last_write = 0.0

    def _write(self, **fields):
        with db.session() as s:
            s.execute(
                db.Job.__table__.update()
                .where(db.Job.id == self.job_id)
                .values(**fields))
        self._last_write = time.time()

    def stage(self, name):
        self.stage_name = name
        self.percent = 0.0
        self.message = OPENING.get(name, "Working")
        self._write(stage=name, percent=0.0, message=self.message)

    def __call__(self, message, percent=None):
        """The callback shape engine.py expects."""
        changed_msg = message != self.message
        self.message = message
        moved = percent is not None and abs(percent - self.percent) >= self.MIN_STEP
        if percent is not None:
            self.percent = float(percent)
        if changed_msg or moved or time.time() - self._last_write > self.MIN_GAP:
            self._write(message=self.message, percent=self.percent)

    def say(self, message, **fields):
        self.message = message
        self._write(message=message, **fields)


def _watch_for_cancel(job_id, control, stop):
    """Bridge the database flag onto the in-process cancel token."""
    while not stop.wait(1.5):
        try:
            if jobqueue.cancel_requested(job_id):
                control.cancel()
                return
        except Exception:
            pass            # a blip in the database is not a reason to stop


def _beat(job_id, stop):
    while not stop.wait(settings.HEARTBEAT_SECONDS):
        try:
            jobqueue.heartbeat(job_id)
        except Exception:
            pass


def _temp_dir(job):
    """Reuse the job's scratch folder across retries where it survived."""
    if job.temp_dir and Path(job.temp_dir).is_dir():
        return Path(job.temp_dir)
    path = Path(tempfile.mkdtemp(prefix="viralclip_"))
    with db.session() as s:
        s.execute(db.Job.__table__.update()
                  .where(db.Job.id == job.id).values(temp_dir=str(path)))
    return path


def _permanent(message):
    low = (message or "").lower()
    return any(k in low for k in PERMANENT)


def _finish(job_id, **fields):
    fields.setdefault("finished_at", db.now())
    with db.session() as s:
        s.execute(db.Job.__table__.update()
                  .where(db.Job.id == job_id).values(**fields))


# ---------- analysis ----------

def run_analyze(job_id, cfg):
    with db.session() as s:
        job = s.get(db.Job, job_id)
        if not job:
            return
        attempts = job.attempts

    prog = Progress(job_id)
    control = engine.Control()
    stop = threading.Event()
    threading.Thread(target=_watch_for_cancel, args=(job_id, control, stop),
                     daemon=True).start()

    try:
        with db.session() as s:
            job = s.get(db.Job, job_id)
        temp = _temp_dir(job)

        prog.stage("download")
        audio, meta = engine.download(
            job.url, temp, cfg["cookies"], prog, control,
            cfg.get("cookies_file", ""), audio_only=True)
        with db.session() as s:
            s.execute(db.Job.__table__.update().where(db.Job.id == job_id)
                      .values(audio_path=str(audio), meta=meta))

        prog.stage("transcribe")
        segments = engine.transcribe(
            audio, cfg["transcriber"], appconfig.transcribe_model(cfg),
            appconfig.transcribe_key(cfg), prog, control, job.url)
        if not segments:
            raise RuntimeError(
                "No speech was found in this video. "
                "This tool needs talking to work with.")

        words = sum(len((sg.get("text") or "").split()) for sg in segments)
        with db.session() as s:
            s.merge(db.Transcript(job_id=job_id, segments=segments,
                                  word_count=words,
                                  engine_used=cfg["transcriber"]))

        prog.stage("score")
        prog("Sending the transcript for scoring", 20)
        clips = engine.score_moments(
            segments, cfg["provider"], appconfig.active_key(cfg),
            int(cfg["clip_len"]), int(cfg["max_clips"]), prog, job.url)
        if not clips:
            raise RuntimeError(
                "No moments in this video were strong enough to clip.")

        poster = (meta or {}).get("thumbnail") or ""
        with db.session() as s:
            # A re-run of the same job replaces its clips rather than doubling
            # them up. Rendered files from a previous run stay on disk.
            s.execute(delete(db.Clip).where(db.Clip.job_id == job_id))
            for c in clips:
                s.add(db.Clip(
                    job_id=job_id, n=c["n"], title=c["title"], hook=c["hook"],
                    hook_card=c.get("hook_card", ""),
                    reason=c["reason"], score=c["score"], text=c.get("text", ""),
                    tags=c.get("tags") or [], lines=c.get("lines") or [],
                    start_s=c["start"], end_s=c["end"],
                    duration=int(round(c["duration"])), thumb=poster))

        _finish(job_id, stage="ready", status=db.DONE, percent=100.0,
                message=f"{len(clips)} moments found", error=None)
        log(f"analyze {job_id}: {len(clips)} clips")

    except engine.Cancelled:
        _finish(job_id, status=db.CANCELLED, message="Run stopped", percent=0.0)
        jobqueue.clear_cancel(job_id)
        log(f"analyze {job_id}: cancelled")

    except Exception as e:                      # noqa: BLE001 - reported to the user
        message = str(e)
        if _permanent(message) or attempts >= settings.MAX_ATTEMPTS:
            _finish(job_id, status=db.ERROR, error=message, message="Stopped")
            log(f"analyze {job_id}: failed, {message[:120]}")
        else:
            # Back to the queue. Checkpoints mean the retry resumes rather
            # than starting the download again.
            with db.session() as s:
                s.execute(db.Job.__table__.update()
                          .where(db.Job.id == job_id)
                          .values(status=db.QUEUED, claimed_by=None, error=None,
                                  message="Hit a snag, picking up where it "
                                          f"left off ({attempts})"))
            time.sleep(min(3 * attempts, 15))
            jobqueue.enqueue(job_id, db.TASK_ANALYZE)
            log(f"analyze {job_id}: retry {attempts}, {message[:80]}")
    finally:
        stop.set()


# ---------- export ----------

def ensure_video(job_id, url, temp, cfg, prog, control):
    """Fetch the picture, which analysis skipped. Cached between exports."""
    with db.session() as s:
        job = s.get(db.Job, job_id)
        if job.video_path and Path(job.video_path).exists():
            return job.video_path
        s.execute(db.Job.__table__.update()
                  .where(db.Job.id == job_id).values(fetching_video=True))
    try:
        video, _ = engine.download(url, temp, cfg["cookies"], prog, control,
                                   cfg.get("cookies_file", ""), audio_only=False)
        with db.session() as s:
            s.execute(db.Job.__table__.update()
                      .where(db.Job.id == job_id).values(video_path=str(video)))
        return str(video)
    finally:
        with db.session() as s:
            s.execute(db.Job.__table__.update()
                      .where(db.Job.id == job_id).values(fetching_video=False))


def run_export(job_id, cfg):
    prog = Progress(job_id)
    control = engine.Control()
    stop = threading.Event()
    threading.Thread(target=_watch_for_cancel, args=(job_id, control, stop),
                     daemon=True).start()

    with db.session() as s:
        job = s.get(db.Job, job_id)
        wanted = set(job.export_request or [])
        picked = list(s.scalars(
            select(db.Clip).where(db.Clip.job_id == job_id,
                                  db.Clip.n.in_(wanted)).order_by(db.Clip.n)))
        transcript = s.get(db.Transcript, job_id)
        segments = transcript.segments if transcript else []
        url, meta, temp_dir = job.url, job.meta or {}, job.temp_dir

    if not picked:
        _finish(job_id, status=db.DONE, exporting=False,
                message="Nothing to export")
        stop.set()
        return

    out = Path(cfg["outdir"])
    out.mkdir(parents=True, exist_ok=True)
    temp = Path(temp_dir) if temp_dir and Path(temp_dir).is_dir() else Path(
        tempfile.mkdtemp(prefix="viralclip_"))
    title_slug = engine.slugify(meta.get("title", ""), 28)

    with db.session() as s:
        s.execute(db.Job.__table__.update().where(db.Job.id == job_id)
                  .values(exporting=True, export_error=None))

    try:
        video = ensure_video(job_id, url, temp, cfg, prog, control)
    except engine.Cancelled:
        _finish(job_id, status=db.DONE, exporting=False, message="Export stopped")
        jobqueue.clear_cancel(job_id)
        stop.set()
        return
    except Exception as e:                      # noqa: BLE001
        _finish(job_id, status=db.DONE, exporting=False, export_error=str(e),
                message="Could not fetch the video")
        stop.set()
        return

    # Each clip's end card names the clip meant to follow it, so the chain is
    # worked out across the run rather than per clip. It covers everything
    # that will exist as a file once this export finishes, which is what is
    # being cut now plus whatever was cut before: a clip must not promise a
    # file that was never written, but exporting them one at a time should
    # still build the same chain as exporting them together.
    chain = {}
    if cfg.get("cta", True):
        with db.session() as s:
            every = list(s.scalars(
                select(db.Clip).where(db.Clip.job_id == job_id)
                .order_by(db.Clip.n)))
        wanted = {c.n for c in picked}
        chain = engine.cta_chain(
            [{"n": c.n, "score": c.score, "start": c.start_s,
              "hook_card": c.hook_card, "title": c.title} for c in every
             if c.n in wanted or c.status == db.CLIP_EXPORTED],
            lead=cfg.get("cta_text") or engine.CTA_LEAD)

    done = 0
    try:
        for i, clip in enumerate(picked, 1):
            with db.session() as s:
                s.execute(db.Clip.__table__.update().where(db.Clip.id == clip.id)
                          .values(status=db.CLIP_EXPORTING, percent=0.0,
                                  export_error=None))
            prog.say(f"Exporting clip {i} of {len(picked)}")

            name = (f"{clip.score:02d}pct-{title_slug}-"
                    f"{engine.slugify(clip.title, 24)}.mp4")
            dest = out / name

            # Where the face is decides which side the end card takes, so
            # the track is worked out here and handed to the cut as well,
            # rather than being paid for twice.
            track = None
            if cfg["vertical"] and cfg.get("speaker_track", True):
                try:
                    track = engine.face_track(video, clip.start_s, clip.end_s,
                                              control)
                except engine.Cancelled:
                    raise
                except Exception:       # noqa: BLE001 - falls back to the fill
                    track = None

            card = clip.hook_card if cfg.get("hook_card", True) else ""
            cta = chain.get(clip.n)
            subs = None
            if (cfg["captions"] != "none" and segments) or card or cta:
                try:
                    subs = engine.build_ass(
                        segments, clip.start_s, clip.end_s, cfg["captions"],
                        temp / f"caps{clip.n}.ass", hook_card=card, cta=cta,
                        word_level=cfg.get("word_captions", True),
                        cta_at=engine.cta_side(
                            track,
                            max(0.0, clip.end_s - clip.start_s - engine.CTA_SECONDS),
                            clip.end_s - clip.start_s))
                except OSError:
                    subs = None     # a clip without captions beats no clip

            # Percent goes to the clip row, which is what the grid reads.
            last = {"t": 0.0}

            def on_percent(p, clip_id=clip.id):
                if time.time() - last["t"] < 0.5:
                    return
                last["t"] = time.time()
                with db.session() as s:
                    s.execute(db.Clip.__table__.update()
                              .where(db.Clip.id == clip_id)
                              .values(percent=round(p)))

            try:
                engine.cut(video, clip.start_s, clip.end_s, dest,
                           cfg["vertical"], cfg["blur_pad"],
                           progress=on_percent, control=control, subs=subs,
                           speaker_track=cfg.get("speaker_track", True),
                           track=track)
            except engine.Cancelled:
                with db.session() as s:
                    s.execute(db.Clip.__table__.update()
                              .where(db.Clip.id == clip.id)
                              .values(status=db.CLIP_PENDING, percent=0.0))
                _finish(job_id, status=db.DONE, exporting=False,
                        message="Export stopped")
                jobqueue.clear_cancel(job_id)
                return
            except Exception as e:              # noqa: BLE001
                with db.session() as s:
                    s.execute(db.Clip.__table__.update()
                              .where(db.Clip.id == clip.id)
                              .values(status=db.CLIP_FAILED, percent=0.0,
                                      export_error=str(e)))
                continue

            # The file on disk is what the user asked for. The copy in storage
            # is what a posting adapter will hand to a platform later, and on
            # a hosted box it is the only copy that outlives the container.
            key = url_ = size = None
            try:
                key, url_, size = storage.store_clip(job_id, clip.n, dest, name)
            except Exception as e:               # noqa: BLE001
                log(f"storage put failed for {name}: {e}")

            with db.session() as s:
                s.execute(db.Clip.__table__.update().where(db.Clip.id == clip.id)
                          .values(status=db.CLIP_EXPORTED, percent=100.0,
                                  file_path=str(dest), asset_key=key,
                                  asset_url=url_, bytes=size))
            done += 1

        _write_report(out, url, meta, picked, job_id)
        _finish(job_id, status=db.DONE, exporting=False,
                message=f"Exported {done} of {len(picked)} clips")
        log(f"export {job_id}: {done}/{len(picked)}")
    finally:
        stop.set()
        with db.session() as s:
            s.execute(db.Job.__table__.update().where(db.Job.id == job_id)
                      .values(exporting=False))


def _write_report(out, url, meta, picked, job_id):
    try:
        with db.session() as s:
            rows = list(s.scalars(
                select(db.Clip).where(db.Clip.job_id == job_id,
                                      db.Clip.n.in_([c.n for c in picked]))
                .order_by(db.Clip.n)))
        lines = [
            f"Source   : {url}",
            f"Title    : {meta.get('title', '')}",
            f"Platform : {meta.get('uploader', '')}",
            "", "-" * 60, "",
        ]

        # The order matters more than the files do: the first clip is the one
        # that has to earn the follow, and every clip after it is being
        # watched by someone who followed for exactly that.
        order = engine.post_order([{"n": c.n, "score": c.score,
                                    "start": c.start_s, "title": c.title}
                                   for c in rows])
        if len(order) > 1:
            lines.append("Post them in this order:")
            lines += [f"  {c['post_order']}. {c['title']} ({c['score']}%)"
                      for c in order]
            lines += ["", "-" * 60, ""]
        rank = {c["n"]: c["post_order"] for c in order}

        for c in rows:
            lines += [
                c.file_path or c.title,
                f"  Post      : {rank.get(c.n, 1)} of {len(rows)}",
                f"  Score     : {c.score}%",
                f"  Timestamp : {engine.fmt_time(c.start_s)} - "
                f"{engine.fmt_time(c.end_s)}",
                f"  Length    : {c.duration}s",
                f"  Hook      : {c.hook}",
                f"  Hook card : {c.hook_card}",
                f"  Why       : {c.reason}",
            ]
            if c.asset_url:
                lines.append(f"  Stored    : {c.asset_url}")
            lines.append("")
        (out / "clip-report.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception:                           # a missing report is cosmetic
        pass


# ---------- the loop ----------

def handle(job_id, task):
    cfg = appconfig.load_config()
    stop = threading.Event()
    threading.Thread(target=_beat, args=(job_id, stop), daemon=True).start()
    try:
        if task == db.TASK_EXPORT:
            run_export(job_id, cfg)
        else:
            run_analyze(job_id, cfg)
    finally:
        stop.set()


def loop(stop_event=None, name=None):
    name = name or jobqueue.worker_name()
    stop_event = stop_event or threading.Event()
    log(f"worker {name} up. database {settings.summary()['database_kind']}, "
        f"redis {'yes' if jobqueue.redis_ok() else 'no'}, "
        f"storage {settings.STORAGE_BACKEND}")

    last_reap = 0.0
    while not stop_event.is_set():
        try:
            if time.time() - last_reap > settings.STALE_AFTER_SECONDS / 2:
                requeued = jobqueue.reap_stale()
                if requeued:
                    log(f"requeued {requeued} orphaned job(s)")
                last_reap = time.time()

            taken = jobqueue.claim(name)
            if not taken:
                jobqueue.wait_for_signal()
                continue

            job_id, task = taken
            log(f"claim {job_id} ({task})")
            handle(job_id, task)
        except KeyboardInterrupt:
            break
        except Exception as e:                  # noqa: BLE001
            log(f"worker error: {e}")
            time.sleep(2)

    log(f"worker {name} down")


def start_inline():
    """Run the loop in a thread, for a single-process local run."""
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(stop, jobqueue.worker_name()),
                         daemon=True, name="clipper-worker")
    t.start()
    return stop


if __name__ == "__main__":
    stop = threading.Event()

    def _bye(*_):
        log("stopping after the current job")
        stop.set()

    signal.signal(signal.SIGINT, _bye)
    signal.signal(signal.SIGTERM, _bye)
    try:
        loop(stop)
    except KeyboardInterrupt:
        pass
    sys.exit(0)
