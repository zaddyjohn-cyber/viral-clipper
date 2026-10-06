"""
Viral Clipper server.
Run:  python server.py
Then open http://127.0.0.1:8420
"""

import json
import os
import shutil
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import engine

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
CONFIG_FILE = Path.home() / ".viral_clipper.json"
DEFAULT_OUTPUT = Path.home() / "Desktop" / "viral_clips"

# On RDP or a VPS set CLIPPER_HOST=0.0.0.0 to serve beyond localhost.
HOST = os.environ.get("CLIPPER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLIPPER_PORT", "8420"))

STAGES = ["download", "transcribe", "score", "ready"]

app = FastAPI(title="Viral Clipper")
jobs = {}
jobs_lock = threading.Lock()


# ---------- config ----------

def load_config():
    base = {
        "provider": "anthropic", "keys": {},
        "transcriber": "groq", "whisper_model": "base",
        "groq_model": "whisper-large-v3-turbo", "clip_len": 60,
        "max_clips": 6, "cookies": "none", "cookies_file": "",
        "captions": "punch", "vertical": True,
        "blur_pad": True, "outdir": str(DEFAULT_OUTPUT),
    }
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            # Older configs stored a single Anthropic key at the top level.
            if saved.get("api_key") and not saved.get("keys"):
                saved["keys"] = {"anthropic": saved["api_key"]}
            saved.pop("api_key", None)
            base.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    if base["provider"] not in engine.PROVIDERS:
        base["provider"] = "anthropic"
    if base["transcriber"] not in engine.TRANSCRIBERS:
        base["transcriber"] = "groq"
    if base["captions"] not in engine.CAPTION_STYLES:
        base["captions"] = "punch"
    return base


def transcribe_model(cfg):
    return cfg["groq_model"] if cfg["transcriber"] == "groq" else cfg["whisper_model"]


def transcribe_key(cfg):
    return (cfg.get("keys") or {}).get("groq", "") if cfg["transcriber"] == "groq" else ""


def active_key(cfg):
    return (cfg.get("keys") or {}).get(cfg["provider"], "")


def save_config(patch):
    current = load_config()
    # Merge keys so saving one provider's key does not wipe the others.
    incoming_keys = patch.pop("keys", None)
    if incoming_keys:
        merged = dict(current.get("keys") or {})
        merged.update({k: v for k, v in incoming_keys.items() if v})
        current["keys"] = merged
    current.update(patch)
    try:
        CONFIG_FILE.write_text(json.dumps(current, indent=2))
    except OSError:
        pass
    return current


def redacted(cfg):
    out = dict(cfg)
    out["keys_set"] = {p: bool((cfg.get("keys") or {}).get(p))
                       for p in list(engine.PROVIDERS) + ["groq"]}
    out.pop("keys", None)
    out["providers"] = {
        p: {"label": v["label"], "model": v["model"],
            "keys_url": v["keys_url"], "key_prefix": v["key_prefix"]}
        for p, v in engine.PROVIDERS.items()
    }
    out["caption_styles"] = {
        k: {"label": v["label"], "blurb": v["blurb"]}
        for k, v in engine.CAPTION_STYLES.items()
    }
    out["transcribers"] = {
        t: {"label": v["label"], "blurb": v["blurb"], "models": v["models"],
            "needs_key": v["needs_key"], "keys_url": v["keys_url"]}
        for t, v in engine.TRANSCRIBERS.items()
    }
    return out


# ---------- job helpers ----------

def new_job(url):
    jid = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[jid] = {
            "id": jid, "url": url, "stage": "download", "status": "running",
            "message": "Starting", "percent": 0, "error": None,
            "clips": [], "meta": {}, "exporting": False,
            "platform": engine.platform_of(url),
            "temp": tempfile.mkdtemp(prefix="viralclip_"),
            "video": None, "audio": None, "segments": [],
            "fetching_video": False,
            "export_error": None, "control": engine.Control(),
            "started": time.time(),
        }
    return jobs[jid]


def get_job(jid):
    with jobs_lock:
        job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "That job is gone. Run the analysis again.")
    return job


def public_job(job):
    return {
        "id": job["id"], "url": job["url"], "platform": job["platform"],
        "stage": job["stage"], "status": job["status"],
        "message": job["message"], "percent": round(job["percent"], 1),
        "error": job["error"], "clips": job["clips"], "meta": job["meta"],
        "exporting": job["exporting"], "stages": STAGES,
        "fetching_video": job.get("fetching_video", False),
        "export_error": job.get("export_error"),
        "has_video": bool(job.get("video")),
        "elapsed": round(time.time() - job["started"]),
    }


def latest_job():
    """Most recent job, so a page reload can rejoin a run already in flight."""
    with jobs_lock:
        if not jobs:
            return None
        return max(jobs.values(), key=lambda j: j["started"])


# ---------- analysis pipeline ----------

PIPELINE_ATTEMPTS = 3
PERMANENT = ("api key was rejected", "needs a login", "signed-in session",
             "no speech was found", "not a video page", "too large",
             "is not installed")


def run_pipeline(job, cfg):
    """Retries itself on transient failures. Checkpoints make each retry cheap."""
    for attempt in range(1, PIPELINE_ATTEMPTS + 1):
        _run_pipeline_once(job, cfg)

        if job["status"] != "error":
            return
        if job["control"].cancelled:
            return

        err = (job["error"] or "").lower()
        if any(k in err for k in PERMANENT):
            return                      # retrying will not change the answer
        if attempt == PIPELINE_ATTEMPTS:
            job["message"] = "Stopped after several tries"
            return

        job["status"] = "running"
        job["message"] = f"Hit a snag, picking up where it left off ({attempt})"
        job["error"] = None
        time.sleep(3 * attempt)


def _run_pipeline_once(job, cfg):
    jid = job["id"]
    temp = Path(job["temp"])
    ctl = job["control"]

    OPENING = {"download": "Looking up the video",
               "transcribe": "Starting up the transcriber",
               "score": "Preparing the transcript",
               "ready": "Finishing up"}

    def stage(name):
        job["stage"] = name
        job["percent"] = 0
        job["message"] = OPENING.get(name, "Working")

    def progress(msg, percent=None):
        job["message"] = msg
        if percent is not None:
            job["percent"] = percent

    try:
        stage("download")
        audio, meta = engine.download(
            job["url"], temp, cfg["cookies"], progress, ctl,
            cfg.get("cookies_file", ""), audio_only=True)
        job["audio"] = str(audio)
        job["meta"] = meta

        stage("transcribe")
        segments = engine.transcribe(
            audio, cfg["transcriber"], transcribe_model(cfg),
            transcribe_key(cfg), progress, ctl, job["url"])
        if not segments:
            raise RuntimeError(
                "No speech was found in this video. This tool needs talking to work with.")

        job["segments"] = segments

        stage("score")
        progress("Sending the transcript for scoring", 20)
        clips = engine.score_moments(
            segments, cfg["provider"], active_key(cfg), int(cfg["clip_len"]),
            int(cfg["max_clips"]), progress, job["url"])
        if not clips:
            raise RuntimeError("No moments in this video were strong enough to clip.")

        poster = meta.get("thumbnail") or ""
        for c in clips:
            c["thumb"] = poster

        job["clips"] = clips
        stage("ready")
        job["status"] = "done"
        job["percent"] = 100
        job["message"] = f"{len(clips)} moments found"

    except engine.Cancelled:
        job["status"] = "cancelled"
        job["message"] = "Run stopped"
        job["percent"] = 0
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["message"] = "Stopped"


def ensure_video(job, cfg):
    """Pull the picture only now that the user wants actual clips."""
    if job.get("video") and Path(job["video"]).exists():
        return job["video"]

    ctl = job["control"]
    job["fetching_video"] = True

    def progress(msg, percent=None):
        job["message"] = msg
        if percent is not None:
            job["percent"] = percent

    try:
        video, _ = engine.download(
            job["url"], Path(job["temp"]), cfg["cookies"], progress, ctl,
            cfg.get("cookies_file", ""), audio_only=False)
        job["video"] = str(video)
        return job["video"]
    finally:
        job["fetching_video"] = False


def run_export(job, clip_numbers, cfg):
    out = Path(cfg["outdir"])
    out.mkdir(parents=True, exist_ok=True)
    ctl = job["control"]
    title_slug = engine.slugify(job["meta"].get("title", ""), 28)
    picked = [c for c in job["clips"] if c["n"] in clip_numbers]
    job["exporting"] = True

    try:
        video = ensure_video(job, cfg)
    except engine.Cancelled:
        job["exporting"] = False
        job["message"] = "Export stopped"
        return
    except Exception as e:
        job["exporting"] = False
        job["export_error"] = str(e)
        job["message"] = "Could not fetch the video"
        return

    for i, c in enumerate(picked, 1):
        c["exporting"] = True
        c["percent"] = 0
        c.pop("export_error", None)
        job["message"] = f"Exporting clip {i} of {len(picked)}"
        name = f"{c['score']:02d}pct-{title_slug}-{engine.slugify(c['title'], 24)}.mp4"
        dest = out / name
        subs = None
        if cfg["captions"] != "none" and job.get("segments"):
            try:
                subs = engine.build_ass(
                    job["segments"], c["start"], c["end"], cfg["captions"],
                    Path(job["temp"]) / f"caps{c['n']}.ass")
            except OSError:
                subs = None         # a clip without captions beats no clip

        try:
            engine.cut(video, c["start"], c["end"], dest,
                       cfg["vertical"], cfg["blur_pad"],
                       progress=lambda p, c=c: c.__setitem__("percent", round(p)),
                       control=ctl, subs=subs)
            c["exported"] = True
            c["file"] = str(dest)
        except engine.Cancelled:
            c["exporting"] = False
            job["exporting"] = False
            job["message"] = "Export stopped"
            return
        except Exception as e:
            c["export_error"] = str(e)
        finally:
            c["exporting"] = False

    job["exporting"] = False

    try:
        lines = [
            f"Source   : {job['url']}",
            f"Title    : {job['meta'].get('title', '')}",
            f"Platform : {job['platform']}",
            "", "-" * 60, "",
        ]
        for c in picked:
            lines += [
                c.get("file", c["title"]),
                f"  Score     : {c['score']}%",
                f"  Timestamp : {engine.fmt_time(c['start'])} - {engine.fmt_time(c['end'])}",
                f"  Length    : {c['duration']}s",
                f"  Hook      : {c['hook']}",
                f"  Why       : {c['reason']}",
                "",
            ]
        (out / "clip-report.txt").write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass

    done = sum(1 for c in picked if c.get("exported"))
    job["message"] = f"Exported {done} of {len(picked)} clips"


# ---------- API ----------

class AnalyzeBody(BaseModel):
    url: str


class ExportBody(BaseModel):
    clips: list[int]


@app.get("/api/config")
def api_get_config():
    return redacted(load_config())


@app.post("/api/config")
def api_set_config(body: dict):
    if body.get("provider") and body["provider"] not in engine.PROVIDERS:
        raise HTTPException(400, "Unknown scoring provider.")
    if body.get("transcriber") and body["transcriber"] not in engine.TRANSCRIBERS:
        raise HTTPException(400, "Unknown transcription engine.")
    return redacted(save_config(body))


@app.get("/api/preflight")
def api_preflight():
    cfg = load_config()
    return {
        "yt_dlp": bool(shutil.which("yt-dlp")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "api_key": bool(active_key(cfg)),
        "provider": cfg["provider"],
        "provider_label": engine.PROVIDERS[cfg["provider"]]["label"],
        "transcriber": cfg["transcriber"],
        "transcriber_ok": (not engine.TRANSCRIBERS[cfg["transcriber"]]["needs_key"]
                           or bool(transcribe_key(cfg))),
    }


@app.post("/api/analyze")
def api_analyze(body: AnalyzeBody):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "That does not look like a video link.")

    cfg = load_config()
    if not active_key(cfg):
        label = engine.PROVIDERS[cfg["provider"]]["label"]
        raise HTTPException(400, f"Add your {label} API key in Settings first.")
    spec = engine.TRANSCRIBERS[cfg["transcriber"]]
    if spec["needs_key"] and not transcribe_key(cfg):
        raise HTTPException(400, f"Add your {spec['label']} API key in Settings first.")
    for tool, hint in [("yt-dlp", "pip install yt-dlp"),
                       ("ffmpeg", "install ffmpeg and add it to PATH")]:
        if not shutil.which(tool):
            raise HTTPException(400, f"{tool} is missing. To fix: {hint}")

    job = new_job(url)
    threading.Thread(target=run_pipeline, args=(job, cfg), daemon=True).start()
    return public_job(job)


@app.get("/api/job/{jid}")
def api_job(jid: str):
    return public_job(get_job(jid))


@app.get("/api/active")
def api_active():
    """Lets a reloaded page rejoin whatever run is already going."""
    job = latest_job()
    return public_job(job) if job else {}


@app.post("/api/job/{jid}/cancel")
def api_cancel(jid: str):
    job = get_job(jid)
    job["control"].cancel()
    if job["status"] == "running":
        job["status"] = "cancelled"
        job["message"] = "Run stopped"
    job["exporting"] = False
    return {"ok": True}


@app.post("/api/job/{jid}/export")
def api_export(jid: str, body: ExportBody):
    job = get_job(jid)
    if job["stage"] != "ready":
        raise HTTPException(400, "This video is still being analysed.")
    if not body.clips:
        raise HTTPException(400, "Pick at least one clip.")

    # A cancelled analysis leaves a tripped token behind; reset it to export.
    job["control"] = engine.Control()
    cfg = load_config()
    threading.Thread(target=run_export, args=(job, set(body.clips), cfg),
                     daemon=True).start()
    return {"ok": True}


@app.get("/api/thumb/{jid}/{n}")
def api_thumb(jid: str, n: int):
    job = get_job(jid)
    path = Path(job["temp"]) / "thumbs" / f"{n}.jpg"
    if not path.exists():
        raise HTTPException(404, "No preview")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/preview/{jid}/{n}")
def api_preview(jid: str, n: int):
    """Audio of a clip, cut from the file analysis already downloaded.

    Lets you hear a moment before committing to the full video download.
    """
    job = get_job(jid)
    clip = next((c for c in job["clips"] if c["n"] == n), None)
    if not clip:
        raise HTTPException(404, "No such clip.")
    if not job.get("audio") or not Path(job["audio"]).exists():
        raise HTTPException(404, "The audio for this run is no longer around.")

    out = Path(job["temp"]) / "previews"
    out.mkdir(parents=True, exist_ok=True)
    piece = out / f"{n}.mp3"

    if not piece.exists():
        proc = engine._run([
            "ffmpeg", "-y", "-ss", str(clip["start"]), "-i", job["audio"],
            "-t", str(clip["end"] - clip["start"]),
            "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "96k",
            str(piece)])
        if proc.returncode != 0 or not piece.exists():
            raise HTTPException(500, "Could not build the preview.")

    return FileResponse(piece, media_type="audio/mpeg")


@app.get("/api/clip/{jid}/{n}")
def api_clip(jid: str, n: int):
    job = get_job(jid)
    clip = next((c for c in job["clips"] if c["n"] == n), None)
    if not clip or not clip.get("file"):
        raise HTTPException(404, "That clip has not been exported yet.")
    path = Path(clip["file"])
    if not path.exists():
        raise HTTPException(404, "The clip file was moved or deleted.")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.post("/api/reveal")
def api_reveal():
    import subprocess
    import sys
    out = Path(load_config()["outdir"])
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


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    local = f"http://127.0.0.1:{PORT}"
    print(f"\n  Viral Clipper running at {local}")
    if HOST != "127.0.0.1":
        print(f"  Also reachable on this machine's network address at port {PORT}")
    print()
    if HOST == "127.0.0.1":
        threading.Timer(1.2, lambda: webbrowser.open(local)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
