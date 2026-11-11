"""
Zaram Viral Klipa API.

Run:  python server.py
Then open http://127.0.0.1:8420

This process validates requests, writes job rows and reads them back. It never
downloads, transcribes, scores or cuts: worker.py does that, and by default one
runs in a thread here so a single command still gets you a working app. Set
CLIPPER_INLINE_WORKER=0 and run worker.py separately to spread the load.
"""

import os
import shutil
import threading
import webbrowser
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

import appconfig
import db
import engine
import jobqueue
import settings
import storage
import worker

ROOT = Path(__file__).parent
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(_app):
    # Either process can be first to start, so both migrate. Alembic locks.
    db.upgrade()
    stop = worker.start_inline() if settings.INLINE_WORKER else None
    try:
        yield
    finally:
        if stop:
            stop.set()


app = FastAPI(title="Zaram Viral Klipa", lifespan=lifespan)


# ---------- shaping rows for the dashboard ----------

def clip_json(c):
    return {
        "n": c.n, "score": c.score, "title": c.title, "hook": c.hook,
        "hook_card": c.hook_card,
        "reason": c.reason, "text": c.text, "tags": c.tags or [],
        "thumb": c.thumb, "start": c.start_s, "end": c.end_s,
        "duration": c.duration,
        "exporting": c.status == db.CLIP_EXPORTING,
        "exported": c.status == db.CLIP_EXPORTED,
        "percent": round(c.percent or 0),
        "export_error": c.export_error,
        "url": c.asset_url,
    }


def job_json(job, clips):
    started = db.as_utc(job.started_at or job.created_at)
    return {
        "id": job.id, "url": job.url, "platform": job.platform,
        "stage": job.stage, "status": job.status,
        "message": job.message, "percent": round(job.percent or 0, 1),
        "error": job.error, "clips": [clip_json(c) for c in clips],
        "meta": job.meta or {}, "exporting": job.exporting,
        "stages": db.STAGES,
        "fetching_video": job.fetching_video,
        "export_error": job.export_error,
        # Checked on disk, not just recorded: a cleared cache leaves the path
        # behind, and the dashboard uses this to decide it can seek the source.
        "has_video": bool(job.video_path and Path(job.video_path).exists()),
        "elapsed": round((db.now() - started).total_seconds()),
    }


def read_job(jid):
    with db.session() as s:
        job = s.get(db.Job, jid)
        if not job:
            raise HTTPException(404, "That job is gone. Run the analysis again.")
        clips = list(s.scalars(
            select(db.Clip).where(db.Clip.job_id == jid).order_by(db.Clip.n)))
        return job_json(job, clips)


# ---------- config ----------

@app.get("/api/config")
def api_get_config():
    return appconfig.redacted(appconfig.load_config())


@app.post("/api/config")
def api_set_config(body: dict):
    # Pasting the cookie file's contents here would store live session
    # credentials in plain text, so only accept something path-shaped.
    cf = (body.get("cookies_file") or "").strip()
    if cf:
        if len(cf) > 400 or chr(10) in cf or chr(9) in cf or "# Netscape" in cf:
            raise HTTPException(
                400, "That looks like the contents of the cookie file. "
                     "Paste the path to the file instead, for example "
                     "C:\\Users\\you\\Downloads\\cookies.txt")
        if not Path(cf).is_file():
            raise HTTPException(400, f"No file found at {cf}")
        body["cookies_file"] = cf
    if body.get("provider") and body["provider"] not in engine.PROVIDERS:
        raise HTTPException(400, "Unknown scoring provider.")
    if body.get("transcriber") and body["transcriber"] not in engine.TRANSCRIBERS:
        raise HTTPException(400, "Unknown transcription engine.")
    return appconfig.redacted(appconfig.save_config(body))


@app.get("/api/preflight")
def api_preflight():
    cfg = appconfig.load_config()
    return {
        "yt_dlp": bool(shutil.which("yt-dlp")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "api_key": bool(appconfig.active_key(cfg)),
        "provider": cfg["provider"],
        "provider_label": engine.PROVIDERS[cfg["provider"]]["label"],
        "transcriber": cfg["transcriber"],
        "transcriber_ok": (not engine.TRANSCRIBERS[cfg["transcriber"]]["needs_key"]
                           or bool(appconfig.transcribe_key(cfg))),
    }


@app.get("/api/health")
def api_health():
    """What the machine is wired to, and whether work is moving."""
    cutoff = db.now() - timedelta(seconds=settings.STALE_AFTER_SECONDS)
    with db.session() as s:
        running = list(s.scalars(
            select(db.Job).where(db.Job.status == db.RUNNING)))
        live = [j.claimed_by for j in running
                if db.as_utc(j.heartbeat_at) and db.as_utc(j.heartbeat_at) > cutoff]
    out = dict(settings.summary())
    out.update({
        "queued": jobqueue.queued_count(),
        "running": len(running),
        "redis_reachable": jobqueue.redis_ok(),
        "signals_waiting": jobqueue.depth(),
        "live_workers": sorted(set(w for w in live if w)),
    })
    return out


# ---------- jobs ----------

class AnalyzeBody(BaseModel):
    url: str


class ExportBody(BaseModel):
    clips: list[int]


@app.post("/api/analyze")
def api_analyze(body: AnalyzeBody):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "That does not look like a video link.")

    cfg = appconfig.load_config()
    if not appconfig.active_key(cfg):
        label = engine.PROVIDERS[cfg["provider"]]["label"]
        raise HTTPException(400, f"Add your {label} API key in Settings first.")
    spec = engine.TRANSCRIBERS[cfg["transcriber"]]
    if spec["needs_key"] and not appconfig.transcribe_key(cfg):
        raise HTTPException(400, f"Add your {spec['label']} API key in Settings first.")
    for tool, hint in [("yt-dlp", "pip install yt-dlp"),
                       ("ffmpeg", "install ffmpeg and add it to PATH")]:
        if not shutil.which(tool):
            raise HTTPException(400, f"{tool} is missing. To fix: {hint}")

    with db.session() as s:
        job = db.Job(url=url, platform=engine.platform_of(url),
                     task=db.TASK_ANALYZE, status=db.QUEUED,
                     message="Queued", stage="download")
        s.add(job)
        s.flush()
        jid = job.id
    jobqueue.enqueue(jid, db.TASK_ANALYZE)
    return read_job(jid)


@app.get("/api/job/{jid}")
def api_job(jid: str):
    return read_job(jid)


@app.get("/api/active")
def api_active():
    """Lets a reloaded page rejoin whatever run is already going.

    Anything in flight counts whatever its age; finished runs only for a day,
    so a reload next week does not reopen last week's library.
    """
    fresh = db.now() - timedelta(hours=24)
    with db.session() as s:
        job = s.scalars(
            select(db.Job)
            .where(db.Job.status.in_([db.QUEUED, db.RUNNING]))
            .order_by(db.Job.created_at.desc()).limit(1)).first()
        if not job:
            job = s.scalars(
                select(db.Job).where(db.Job.created_at > fresh)
                .order_by(db.Job.created_at.desc()).limit(1)).first()
        if not job:
            return {}
        clips = list(s.scalars(
            select(db.Clip).where(db.Clip.job_id == job.id).order_by(db.Clip.n)))
        return job_json(job, clips)


@app.get("/api/jobs")
def api_jobs(limit: int = 25):
    """Run history. Nothing calls this yet; it is what the DB bought us."""
    limit = max(1, min(limit, 100))
    with db.session() as s:
        jobs = list(s.scalars(
            select(db.Job).order_by(db.Job.created_at.desc()).limit(limit)))
        counts = {}
        if jobs:
            rows = s.execute(
                select(db.Clip.job_id, db.Clip.status)
                .where(db.Clip.job_id.in_([j.id for j in jobs]))).all()
            for job_id, status in rows:
                c = counts.setdefault(job_id, {"clips": 0, "exported": 0})
                c["clips"] += 1
                if status == db.CLIP_EXPORTED:
                    c["exported"] += 1
        return [{
            "id": j.id, "url": j.url, "platform": j.platform,
            "title": (j.meta or {}).get("title", ""),
            "status": j.status, "stage": j.stage,
            "created_at": db.as_utc(j.created_at).isoformat(),
            "clips": counts.get(j.id, {}).get("clips", 0),
            "exported": counts.get(j.id, {}).get("exported", 0),
        } for j in jobs]


@app.post("/api/job/{jid}/cancel")
def api_cancel(jid: str):
    with db.session() as s:
        job = s.get(db.Job, jid)
        if not job:
            raise HTTPException(404, "That job is gone. Run the analysis again.")
        # A job still waiting has no worker to tell, so stop it here.
        if job.status == db.QUEUED:
            job.status = db.CANCELLED
            job.message = "Run stopped"
            job.exporting = False
            return {"ok": True}
    jobqueue.request_cancel(jid)
    return {"ok": True}


@app.post("/api/job/{jid}/export")
def api_export(jid: str, body: ExportBody):
    if not body.clips:
        raise HTTPException(400, "Pick at least one clip.")
    with db.session() as s:
        job = s.get(db.Job, jid)
        if not job:
            raise HTTPException(404, "That job is gone. Run the analysis again.")
        if job.stage != "ready":
            raise HTTPException(400, "This video is still being analysed.")
        if job.exporting:
            raise HTTPException(400, "That export is already running.")
        job.export_request = sorted(set(body.clips))
        job.export_error = None
        job.cancel_requested = False
    jobqueue.enqueue(jid, db.TASK_EXPORT)
    return {"ok": True}


# ---------- media ----------

@app.get("/api/thumb/{jid}/{n}")
def api_thumb(jid: str, n: int):
    with db.session() as s:
        job = s.get(db.Job, jid)
    if not job or not job.temp_dir:
        raise HTTPException(404, "No preview")
    path = Path(job.temp_dir) / "thumbs" / f"{n}.jpg"
    if not path.exists():
        raise HTTPException(404, "No preview")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/preview/{jid}/{n}")
def api_preview(jid: str, n: int):
    """Audio of a clip, cut from the file analysis already downloaded.

    Reads the worker's scratch folder, so this answers only while the API and
    the worker that ran the job share a filesystem. On a split deployment it
    404s rather than lying, and the clip itself still comes from storage.
    """
    with db.session() as s:
        job = s.get(db.Job, jid)
        if not job:
            raise HTTPException(404, "That job is gone. Run the analysis again.")
        clip = s.scalars(select(db.Clip).where(
            db.Clip.job_id == jid, db.Clip.n == n)).first()
    if not clip:
        raise HTTPException(404, "No such clip.")
    if not job.audio_path or not Path(job.audio_path).exists():
        raise HTTPException(404, "The audio for this run is no longer around.")

    out = Path(job.temp_dir or ".") / "previews"
    out.mkdir(parents=True, exist_ok=True)
    piece = out / f"{n}.mp3"

    if not piece.exists():
        proc = engine._run([
            "ffmpeg", "-y", "-ss", str(clip.start_s), "-i", job.audio_path,
            "-t", str(clip.end_s - clip.start_s),
            "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "96k",
            str(piece)])
        if proc.returncode != 0 or not piece.exists():
            raise HTTPException(500, "Could not build the preview.")

    return FileResponse(piece, media_type="audio/mpeg")


@app.get("/api/source/{jid}")
def api_source(jid: str):
    """The downloaded source, served in byte ranges.

    Once the full video is here, watching a moment is a seek rather than a
    render: the browser asks for the bytes around it and nothing has to be
    cut first.
    """
    with db.session() as s:
        job = s.get(db.Job, jid)
    if not job or not job.video_path or not Path(job.video_path).exists():
        raise HTTPException(404, "The video for this run is not downloaded.")
    return FileResponse(job.video_path, media_type="video/mp4")


@app.get("/api/clip/{jid}/{n}")
def api_clip(jid: str, n: int):
    """The rendered file: from disk where it still is, otherwise from storage."""
    with db.session() as s:
        clip = s.scalars(select(db.Clip).where(
            db.Clip.job_id == jid, db.Clip.n == n)).first()
    if not clip or clip.status != db.CLIP_EXPORTED:
        raise HTTPException(404, "That clip has not been exported yet.")

    if clip.file_path and Path(clip.file_path).exists():
        path = Path(clip.file_path)
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    if clip.asset_key:
        store = storage.backend()
        local = store.local_path(clip.asset_key)
        if local:
            return FileResponse(local, media_type="video/mp4",
                                filename=Path(clip.asset_key).name)
        return RedirectResponse(store.url(clip.asset_key))

    raise HTTPException(404, "The clip file was moved or deleted.")


@app.get("/api/media/{key:path}")
def api_media(key: str):
    """Serves objects held by the local storage backend."""
    store = storage.backend()
    if store.name != "local":
        raise HTTPException(404, "Not served from here.")
    try:
        path = store.local_path(key)
    except ValueError:
        raise HTTPException(400, "Bad key.")
    if not path:
        raise HTTPException(404, "No such object.")
    return FileResponse(path, media_type=storage.guess_type(path))


@app.post("/api/reveal")
def api_reveal():
    import subprocess
    import sys
    out = Path(appconfig.load_config()["outdir"])
    out.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(out)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(out)])
        else:
            subprocess.Popen(["xdg-open", str(out)])
    except OSError as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@app.exception_handler(HTTPException)
def http_error(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.middleware("http")
async def fresh_dashboard(request, call_next):
    """Make the browser check the dashboard files every load.

    This app updates underneath itself, and a cached app.js after an update
    looks exactly like a feature that silently does not work. The files still
    carry an ETag, so revalidating usually costs a 304 and no bytes.
    """
    res = await call_next(request)
    if request.url.path in ("/", "/index.html", "/app.js", "/styles.css"):
        res.headers["Cache-Control"] = "no-cache"
    return res


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    local = f"http://127.0.0.1:{settings.PORT}"
    info = settings.summary()
    print(f"\n  Zaram Viral Klipa running at {local}")
    print(f"  database {info['database_kind']}, storage {info['storage']}, "
          f"redis {'on' if info['redis'] else 'off'}")
    if settings.INLINE_WORKER:
        print("  worker running in this process "
              "(set CLIPPER_INLINE_WORKER=0 to run worker.py separately)")
    else:
        print("  no inline worker. Start one with: python worker.py")
    if settings.HOST != "127.0.0.1":
        print(f"  Also reachable on this machine's network address at port "
              f"{settings.PORT}")
    print()
    if settings.HOST == "127.0.0.1":
        threading.Timer(1.2, lambda: webbrowser.open(local)).start()
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="warning")
