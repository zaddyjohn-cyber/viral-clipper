"""
End-to-end check of the plumbing, with the engine faked out.

    python selftest.py

Runs a real job through the real API, the real queue and the real storage
backend, but with download, transcribe, score and cut replaced by stand-ins.
So it spends no API credits, touches no network and finishes in seconds, while
still proving the parts that are easy to get wrong: a worker claims the job,
progress lands on the row, clips persist, an export writes a file and copies
it to storage, cancellation crosses from the API to a running worker, and an
orphaned job goes back to the queue.

Everything runs against a throwaway database in the temp folder, so this never
touches the real one.
"""

import os
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="clipper_selftest_"))
OUT = TMP / "out"

# Settings are read at import time, so they have to be set before the imports.
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{(TMP / 'test.db').as_posix()}"
os.environ["CLIPPER_STORAGE"] = "local"
os.environ["CLIPPER_STORAGE_DIR"] = str(TMP / "media")
os.environ["CLIPPER_INLINE_WORKER"] = "1"
os.environ["CLIPPER_QUEUE_BLOCK_SECONDS"] = "1"
os.environ["CLIPPER_HEARTBEAT_SECONDS"] = "2"
os.environ["CLIPPER_STALE_AFTER_SECONDS"] = "6"
os.environ.pop("REDIS_URL", None)

from fastapi.testclient import TestClient            # noqa: E402

import appconfig                                      # noqa: E402
import db                                             # noqa: E402
import engine                                         # noqa: E402
import jobqueue                                       # noqa: E402
import server                                         # noqa: E402
import storage                                        # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    mark = "ok  " if ok else "FAIL"
    print(f"  {mark} {name}" + (f"  [{detail}]" if detail and not ok else ""))
    return ok


# ---------- stand-ins for the engine ----------

SEGMENTS = [{"start": i * 5.0, "end": i * 5.0 + 5.0,
             "text": f"This is line {i} of the test transcript."}
            for i in range(40)]

SLOW = threading.Event()          # when set, the download blocks until cancelled


def fake_download(url, out_dir, cookies, progress, control=None,
                  cookies_file="", audio_only=False):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("audio.m4a" if audio_only else "source.mp4")
    path.write_bytes(b"\0" * 4096)
    progress("Fetching video details", 0)

    if SLOW.is_set():
        for _ in range(100):               # 10 seconds at most
            if control and control.cancelled:
                raise engine.Cancelled()
            time.sleep(0.1)

    progress("Downloading audio at 4MiB/s", 60)
    progress("Merging streams", 99)
    return path, {"title": "Self Test Video", "uploader": "Tester",
                  "duration": 200, "thumbnail": ""}


def fake_transcribe(path, transcriber, model, key, progress, control=None, url=""):
    progress("Transcribing", 10)
    progress("Transcribing", 90)
    return SEGMENTS


def fake_score(segments, provider, key, clip_len, max_clips, progress, url=""):
    progress("Reading the transcript back", 60)
    out = []
    for i in range(2):
        a, b = i * 6, i * 6 + 5
        out.append({
            "n": i + 1,
            "start": segments[a]["start"], "end": segments[b]["end"],
            "duration": round(segments[b]["end"] - segments[a]["start"], 1),
            "score": 90 - i * 10,
            "title": f"Test moment {i + 1}",
            "hook": segments[a]["text"],
            "hook_card": f"Test hook card {i + 1}",
            "text": " ".join(s["text"] for s in segments[a:b + 1]),
            "reason": "Because this is a test.",
            "tags": ["test", "plumbing"],
            "lines": [a, b],
            "exported": False,
        })
    return out


def fake_cut(video, start, end, dest, vertical, blur_pad=True,
             progress=None, control=None, subs=None, **kw):
    if progress:
        progress(10)
        progress(100)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(b"\0" * 8192)


CFG = {
    "provider": "anthropic", "keys": {"anthropic": "sk-ant-test", "groq": "gsk-test"},
    "transcriber": "groq", "whisper_model": "base",
    "groq_model": "whisper-large-v3-turbo", "clip_len": 30,
    "max_clips": 4, "cookies": "none", "cookies_file": "",
    "captions": "punch", "vertical": True, "blur_pad": True,
    "speaker_track": True, "word_captions": True, "hook_card": True,
    "outdir": str(OUT),
}

engine.download = fake_download
engine.transcribe = fake_transcribe
engine.score_moments = fake_score
engine.cut = fake_cut
appconfig.load_config = lambda: dict(CFG)
server.shutil = shutil                      # keep the real which() if present
if not shutil.which("ffmpeg") or not shutil.which("yt-dlp"):
    server.shutil = type("W", (), {"which": staticmethod(lambda n: "/fake/" + n)})


def wait_for(client, jid, predicate, timeout=30, label=""):
    deadline = time.time() + timeout
    job = {}
    while time.time() < deadline:
        job = client.get(f"/api/job/{jid}").json()
        if predicate(job):
            return job
        time.sleep(0.25)
    print(f"       timed out waiting for {label}: "
          f"status={job.get('status')} stage={job.get('stage')} "
          f"message={job.get('message')!r}")
    return job


def main():
    print(f"\nViral Clipper self test\n  scratch: {TMP}\n")

    with TestClient(server.app) as client:
        print("health")
        health = client.get("/api/health").json()
        check("health answers", health.get("database_kind") == "sqlite")
        check("inline worker on", health.get("inline_worker") is True)
        check("storage is local", health.get("storage") == "local")

        print("\nanalysis")
        r = client.post("/api/analyze", json={"url": "https://youtu.be/selftest"})
        check("analyze accepted", r.status_code == 200, r.text[:200])
        jid = r.json().get("id")
        check("job got an id", bool(jid))
        check("queued at first",
              r.json().get("status") in (db.QUEUED, db.RUNNING))

        job = wait_for(client, jid, lambda j: j.get("stage") == "ready",
                       label="stage ready")
        check("reached ready", job.get("stage") == "ready", job.get("message"))
        check("status done", job.get("status") == db.DONE, job.get("status"))
        check("two clips", len(job.get("clips", [])) == 2)
        check("clip fields kept",
              all(k in job["clips"][0] for k in
                  ("n", "score", "title", "hook", "hook_card", "reason", "text",
                   "tags", "start", "end", "duration", "exporting", "exported",
                   "percent", "export_error")))
        check("hook card stored",
              job["clips"][0].get("hook_card") == "Test hook card 1",
              job["clips"][0].get("hook_card"))
        check("meta stored", job.get("meta", {}).get("title") == "Self Test Video")
        check("transcript stored", bool(
            db.Session().get(db.Transcript, jid)))

        print("\nexport")
        r = client.post(f"/api/job/{jid}/export", json={"clips": [1, 2]})
        check("export accepted", r.status_code == 200, r.text[:200])
        job = wait_for(
            client, jid,
            lambda j: (not j.get("exporting")
                       and all(c.get("exported") or c.get("export_error")
                               for c in j.get("clips", []))
                       and any(c.get("exported") for c in j.get("clips", []))),
            label="export to finish")
        exported = [c for c in job.get("clips", []) if c.get("exported")]
        check("both clips exported", len(exported) == 2,
              f"{len(exported)} of 2, message={job.get('message')!r}")
        check("files written to the output folder",
              len(list(OUT.glob("*.mp4"))) == 2)
        check("report written", (OUT / "clip-report.txt").exists())

        with db.session() as s:
            rows = s.query(db.Clip).filter(db.Clip.job_id == jid).all()
        check("asset keys recorded", all(c.asset_key for c in rows))
        check("sizes recorded", all((c.bytes or 0) > 0 for c in rows))
        check("objects in storage",
              all(storage.backend().local_path(c.asset_key) for c in rows))

        first = rows[0]
        check("clip downloads", client.get(f"/api/clip/{jid}/1").status_code == 200)
        check("media route serves the object",
              client.get(f"/api/media/{first.asset_key}").status_code == 200)
        check("media route refuses traversal",
              client.get("/api/media/../../settings.py").status_code in (400, 404))

        # build_ass is not faked out, so the subtitle file the export just
        # burned in is the real one. Read it back rather than trusting it.
        print("\nburned-in text")
        words = [{"start": 0.0, "end": 0.4, "word": "This"},
                 {"start": 0.4, "end": 0.8, "word": "is"},
                 {"start": 0.8, "end": 1.4, "word": "word"},
                 {"start": 1.4, "end": 2.0, "word": "timed."}]
        timed = [{"start": 0.0, "end": 2.0, "text": "This is word timed.",
                  "words": words}]
        ass = engine.build_ass(timed, 0, 2, "punch", TMP / "caps.ass",
                               hook_card="Three to six words")
        body = ass.read_text(encoding="utf-8")
        check("one event per word",
              body.count("Dialogue: 0,") == len(words), body.count("Dialogue"))
        check("the live word pops", r"\fscx" in body)
        check("hook card burned in", "THREE TO SIX WORDS" in body)
        check("captions clear the phone chrome",
              all(int(line.rsplit(",", 2)[-2]) >= engine.CAPTION_SAFE_V
                  for line in body.splitlines() if line.startswith("Style: Cap")))
        check("no captions still gives a hook card",
              engine.build_ass(timed, 0, 2, "none", TMP / "hook.ass",
                               hook_card="Only the card") is not None)
        check("nothing to burn writes nothing",
              engine.build_ass([], 0, 2, "none", TMP / "empty.ass") is None)
        check("a short hook card is kept whole",
              engine._hook_card({"hook_card": "He has to shoot from way back"})
              == "He has to shoot from way back")
        check("a rambling hook card falls back to the title",
              engine._hook_card({
                  "hook_card": "this one goes on and on and on well past the frame",
                  "title": "The impossible shot"}) == "The impossible shot")
        check("a hook card breaks at its own punctuation",
              engine._hook_card({"hook_card": "One inch beam, and the whole prize "
                                              "on the line"}) == "One inch beam")

        print("\nhistory and rejoin")
        jobs = client.get("/api/jobs").json()
        check("history lists the run", any(j["id"] == jid for j in jobs))
        check("history counts exports",
              next(j for j in jobs if j["id"] == jid)["exported"] == 2)
        active = client.get("/api/active").json()
        check("active rejoins the run", active.get("id") == jid)

        print("\ncancellation")
        SLOW.set()
        r = client.post("/api/analyze", json={"url": "https://youtu.be/slowtest"})
        slow_id = r.json()["id"]
        running = wait_for(client, slow_id,
                           lambda j: j.get("status") == db.RUNNING,
                           timeout=15, label="slow job to start")
        check("slow job started", running.get("status") == db.RUNNING)
        client.post(f"/api/job/{slow_id}/cancel")
        stopped = wait_for(client, slow_id,
                           lambda j: j.get("status") == db.CANCELLED,
                           timeout=15, label="cancellation")
        check("worker honoured the cancel",
              stopped.get("status") == db.CANCELLED, stopped.get("status"))
        SLOW.clear()

    # The inline worker is stopped now, so an orphan stays orphaned long
    # enough to watch the reaper deal with it.
    time.sleep(1.5)
    print("\nreaper")
    with db.session() as s:
        orphan = db.Job(url="https://youtu.be/orphan", platform="YouTube",
                        status=db.RUNNING, attempts=1, claimed_by="dead-worker",
                        heartbeat_at=db.now() - timedelta(minutes=10),
                        message="was running")
        s.add(orphan)
        s.flush()
        orphan_id = orphan.id
    jobqueue.reap_stale()
    with db.session() as s:
        back = s.get(db.Job, orphan_id)
        check("orphaned job requeued", back.status == db.QUEUED, back.status)
        check("claim released", back.claimed_by is None)

    with db.session() as s:
        burned = db.Job(url="https://youtu.be/burned", platform="YouTube",
                        status=db.RUNNING, attempts=99, claimed_by="dead-worker",
                        heartbeat_at=db.now() - timedelta(minutes=10),
                        message="was running")
        s.add(burned)
        s.flush()
        burned_id = burned.id
    jobqueue.reap_stale()
    with db.session() as s:
        gone = s.get(db.Job, burned_id)
        check("job out of attempts gives up", gone.status == db.ERROR, gone.status)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    print(f"scratch left at {TMP}\n" if FAIL else "")
    if not FAIL:
        shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
