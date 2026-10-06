"""
Clip engine: download, transcribe, score, cut.
Pure functions, no UI. Used by server.py.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Scoring providers. Add an entry here and it shows up in Settings.
PROVIDERS = {
    "anthropic": {
        "label": "Claude Haiku 4.5",
        "model": "claude-haiku-4-5-20251001",
        "keys_url": "console.anthropic.com",
        "key_prefix": "sk-ant-",
    },
    "deepseek": {
        "label": "DeepSeek Chat",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "keys_url": "platform.deepseek.com",
        "key_prefix": "sk-",
    },
    "openai": {
        "label": "GPT-4.1 mini",
        "model": "gpt-4.1-mini",
        "base_url": "https://api.openai.com/v1",
        "keys_url": "platform.openai.com",
        "key_prefix": "sk-",
    },
}

PLATFORMS = [
    ("youtu", "YouTube"), ("facebook", "Facebook"), ("fb.watch", "Facebook"),
    ("twitter", "X"), ("x.com", "X"), ("instagram", "Instagram"),
    ("tiktok", "TikTok"), ("reddit", "Reddit"), ("vimeo", "Vimeo"),
    ("twitch", "Twitch"), ("linkedin", "LinkedIn"),
]


def fmt_time(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def platform_of(url):
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
    for key, name in PLATFORMS:
        if key in host:
            return name
    return host.split(".")[0].title() if host else "Unknown"


class Cancelled(Exception):
    """Raised inside a pipeline stage when the user stops the run."""


class Control:
    """Cancel token. Holds the live subprocess so a stop kills it immediately."""

    def __init__(self):
        self.cancelled = False
        self._proc = None

    def attach(self, proc):
        self._proc = proc
        if self.cancelled:
            self.kill()

    def kill(self):
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def cancel(self):
        self.cancelled = True
        self.kill()

    def check(self):
        if self.cancelled:
            raise Cancelled()


NOOP = Control()


def _run(cmd, control=NOOP):
    control.check()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, creationflags=NO_WINDOW)
    control.attach(proc)
    out, err = proc.communicate()
    control.check()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _stream(cmd, control, on_line):
    """Run a command, feeding each stdout line to on_line. Returns (code, stderr)."""
    control.check()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, creationflags=NO_WINDOW)
    control.attach(proc)
    for line in proc.stdout:
        on_line(line.strip())
    proc.wait()
    err = proc.stderr.read()
    control.check()
    return proc.returncode, err


def probe_duration(path):
    proc = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def cookie_args(cookies_browser, cookies_file):
    """A cookies.txt file wins. It is the only option that works headless."""
    if cookies_file and Path(cookies_file).is_file():
        return ["--cookies", str(cookies_file)]
    if cookies_browser and cookies_browser != "none":
        return ["--cookies-from-browser", cookies_browser]
    return []


def download(url, out_dir, cookies_browser, progress, control=NOOP, cookies_file=""):
    """Download to out_dir/source.*, return (path, meta dict)."""
    progress("Fetching video details", 0)
    ck = cookie_args(cookies_browser, cookies_file)

    info_cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--dump-single-json",
                "--skip-download"] + ck
    proc = _run(info_cmd + [url], control)

    meta = {}
    if proc.returncode == 0:
        try:
            raw = json.loads(proc.stdout)
            meta = {
                "title": raw.get("title") or "Untitled",
                "uploader": raw.get("uploader") or raw.get("channel") or "",
                "duration": raw.get("duration") or 0,
                "thumbnail": raw.get("thumbnail") or "",
            }
        except json.JSONDecodeError:
            pass

    if meta.get("duration", 0) > 10800:
        raise RuntimeError(
            f"That video is {fmt_time(meta['duration'])} long. "
            "Keep it under 3 hours so transcription finishes in reasonable time."
        )

    progress("Starting download", 0)
    dl_cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--newline",
        # YouTube now serves video-only and audio-only streams for most videos,
        # so the merge selector has to come before any combined-stream selector.
        "-f", ("bestvideo[height<=720]+bestaudio/best[height<=720]"
               "/bestvideo+bestaudio/best"),
        "--merge-output-format", "mp4",
        "--progress-template", "PCT %(progress._percent_str)s %(progress._speed_str)s",
        "-o", str(out_dir / "source.%(ext)s"),
    ] + ck

    # Video and audio arrive as separate streams, so map each one onto half the bar.
    seen = {"n": 0, "last": 0.0}
    captured = []

    def on_line(line):
        captured.append(line)
        if not line.startswith("PCT "):
            return
        parts = line.split()
        try:
            pct = float(parts[1].rstrip("%"))
        except (IndexError, ValueError):
            return
        if pct + 1 < seen["last"]:
            seen["n"] += 1
        seen["last"] = pct
        half = 50 if seen["n"] == 0 else 50
        base = 0 if seen["n"] == 0 else 50
        speed = parts[2] if len(parts) > 2 and parts[2] != "Unknown" else ""
        progress(f"Downloading {'video' if seen['n'] == 0 else 'audio'}"
                 + (f" at {speed}" if speed else ""),
                 min(99, base + pct * half / 100))

    code, stderr = _stream(dl_cmd + [url], control, on_line)
    progress("Merging streams", 99)

    if code != 0:
        err = (stderr or "\n".join(captured)).strip()
        low = err.lower()
        if "could not copy" in low and "cookie" in low:
            raise RuntimeError(
                "Your browser is holding its cookie file open. Close the browser "
                "fully, or export a cookies.txt and point Settings at it.")
        if ("403" in err or any(k in low for k in
                ("login", "private", "cookies", "sign in", "age"))):
            raise RuntimeError(
                "This video will not hand over its data without a signed-in "
                "session. Export a cookies.txt from a logged-in browser and set "
                "it under Settings, then try again.")
        if "unsupported url" in low:
            raise RuntimeError("That link is not a video page this tool can read.")
        raise RuntimeError(f"Download failed. {err[-300:]}")

    files = [f for f in out_dir.iterdir() if f.stem == "source"]
    if not files:
        raise RuntimeError("Download finished but produced no file.")

    path = files[0]
    if not meta.get("duration"):
        meta["duration"] = probe_duration(path)
    meta.setdefault("title", "Untitled")
    return path, meta


GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHUNK_SECONDS = 600          # keeps each upload well under the size cap
GROQ_MAX_BYTES = 24 * 1024 * 1024

TRANSCRIBERS = {
    "groq": {
        "label": "Groq",
        "blurb": "Hosted. Roughly 100x realtime, a few cents per hour of audio.",
        "models": ["whisper-large-v3-turbo", "whisper-large-v3"],
        "default_model": "whisper-large-v3-turbo",
        "needs_key": True,
        "keys_url": "console.groq.com",
    },
    "local": {
        "label": "faster-whisper",
        "blurb": "Runs on this machine. Free and private, but CPU bound.",
        "models": ["tiny", "base", "small", "medium"],
        "default_model": "base",
        "needs_key": False,
        "keys_url": "",
    },
}


def pick_device():
    """Use CUDA when the box has it. Falls back to int8 on CPU."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", "float16"
    except ImportError:
        pass
    return "cpu", "int8"


def extract_audio(video_path, out_path, control=NOOP, start=None, span=None):
    """16kHz mono mp3. Small enough to upload, plenty for speech recognition."""
    cmd = ["ffmpeg", "-y"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(video_path)]
    if span is not None:
        cmd += ["-t", str(span)]
    cmd += ["-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "libmp3lame", "-b:a", "48k", str(out_path)]
    proc = _run(cmd, control)
    if proc.returncode != 0:
        raise RuntimeError(f"Could not extract audio. {(proc.stderr or '')[-200:]}")
    return out_path


def _transcribe_local(video_path, model_name, progress, control=NOOP):
    progress("Loading the speech engine", 0)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install faster-whisper")

    progress("Checking for a GPU", 0)
    device, compute = pick_device()
    progress(f"Loading the {model_name} model", 0)
    model = WhisperModel(model_name, device=device, compute_type=compute,
                         cpu_threads=os.cpu_count() or 4)

    control.check()
    progress("Reading audio", 1)
    stream, info = model.transcribe(str(video_path), beam_size=1, vad_filter=True,
                                    condition_on_previous_text=False)

    total = info.duration or probe_duration(video_path) or 1
    out = []
    for seg in stream:
        control.check()
        text = seg.text.strip()
        if text:
            out.append({"start": seg.start, "end": seg.end, "text": text})
        progress(f"Transcribed {fmt_time(seg.end)} of {fmt_time(total)}",
                 min(99, seg.end / total * 100))

    progress(f"Read {len(out)} lines of speech", 100)
    return out


def _transcribe_groq(video_path, model_name, api_key, progress, control=NOOP):
    """Chunked upload. Long videos exceed Groq's per-file cap in one piece."""
    import httpx
    if not api_key:
        raise RuntimeError("Add your Groq API key in Settings first.")

    total = probe_duration(video_path) or 1
    work = Path(video_path).parent / "audio"
    work.mkdir(exist_ok=True)

    starts = [s for s in range(0, max(1, int(total)), GROQ_CHUNK_SECONDS)]
    out = []

    for i, start in enumerate(starts):
        control.check()
        span = min(GROQ_CHUNK_SECONDS, total - start)
        if span <= 0.5:
            break

        base = i / len(starts) * 100
        step = 100 / len(starts)
        progress(f"Preparing audio {i + 1} of {len(starts)}", base)
        piece = extract_audio(video_path, work / f"part{i}.mp3", control, start, span)

        if piece.stat().st_size > GROQ_MAX_BYTES:
            raise RuntimeError(
                "An audio chunk came out too large to upload. "
                "Switch to faster-whisper in Settings for this one.")

        progress(f"Transcribing {fmt_time(start)} to {fmt_time(start + span)}",
                 base + step * 0.35)
        control.check()
        try:
            with open(piece, "rb") as fh:
                res = httpx.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (piece.name, fh, "audio/mpeg")},
                    data={"model": model_name, "response_format": "verbose_json",
                          "temperature": "0"},
                    timeout=600.0)
        except httpx.HTTPError as e:
            raise RuntimeError(f"Could not reach Groq. {e}")
        finally:
            piece.unlink(missing_ok=True)

        if res.status_code == 401:
            raise RuntimeError("Your Groq API key was rejected. Check it in Settings.")
        if res.status_code == 429:
            raise RuntimeError("Groq rate limit hit. Wait a moment and retry.")
        if res.status_code == 413:
            raise RuntimeError("Groq rejected the audio as too large.")
        if res.status_code >= 400:
            raise RuntimeError(f"Groq returned {res.status_code}. {res.text[:200]}")

        try:
            segments = res.json().get("segments") or []
        except json.JSONDecodeError:
            raise RuntimeError("Groq sent back a response that could not be read.")

        for seg in segments:
            text = (seg.get("text") or "").strip()
            if text:
                out.append({"start": seg["start"] + start,
                            "end": seg["end"] + start, "text": text})

        progress(f"Transcribed {fmt_time(min(start + span, total))} of {fmt_time(total)}",
                 min(99, (i + 1) / len(starts) * 100))

    progress(f"Read {len(out)} lines of speech", 100)
    return out


def transcribe(video_path, transcriber, model_name, api_key, progress, control=NOOP):
    if transcriber == "groq":
        return _transcribe_groq(video_path, model_name, api_key, progress, control)
    return _transcribe_local(video_path, model_name, progress, control)


def _api_error(e):
    name = type(e).__name__
    if "Authentication" in name:
        return "Your API key was rejected. Check it in Settings."
    if "RateLimit" in name:
        return "Rate limit hit. Wait a moment and retry."
    if "Connection" in name or "Timeout" in name:
        return "Could not reach the scoring API. Check your connection."
    return f"Scoring failed. {e}"


def _call_anthropic(prompt, api_key, model):
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("Anthropic SDK is not installed. Run: pip install anthropic")

    client = anthropic.Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model=model, max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise RuntimeError(_api_error(e))
    return msg.content[0].text


def _call_openai_compatible(prompt, api_key, model, base_url):
    """DeepSeek, OpenAI and anything else speaking the chat-completions shape."""
    import httpx
    try:
        res = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "max_tokens": 4096,
                "temperature": 0.4,
            },
            timeout=180.0,
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach the scoring API. {e}")

    if res.status_code == 401:
        raise RuntimeError("Your API key was rejected. Check it in Settings.")
    if res.status_code == 429:
        raise RuntimeError("Rate limit hit. Wait a moment and retry.")
    if res.status_code >= 400:
        raise RuntimeError(f"Scoring API returned {res.status_code}. {res.text[:200]}")

    try:
        return res.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError):
        raise RuntimeError("The scoring API sent back an unexpected response.")


def score_moments(segments, provider, api_key, clip_len, max_clips, progress):
    spec = PROVIDERS.get(provider)
    if not spec:
        raise RuntimeError(f"Unknown scoring provider '{provider}'.")
    progress(f"Scoring moments with {spec['label']}")

    transcript = "\n".join(f"[{fmt_time(s['start'])}] {s['text']}" for s in segments)
    total = segments[-1]["end"]
    lo, hi = max(15, clip_len - 25), clip_len + 20

    prompt = f"""You are a short-form video editor who has shipped thousands of viral clips.

Below is a timestamped transcript of a {fmt_time(total)} video. Find the strongest standalone moments to cut into vertical shorts of roughly {clip_len} seconds.

What makes a clip work:
- Lands a hook in the first 2 seconds, no slow build
- Contains one complete thought, story, or payoff that stands alone without the rest of the video
- Carries tension, a surprising claim, a reveal, strong emotion, a specific number, or a contrarian take
- Starts and ends on clean sentence boundaries

TRANSCRIPT (timestamps are MM:SS or H:MM:SS from the start):
{transcript}

Return up to {max_clips} windows, best first. Each window must be {lo} to {hi} seconds long and must not overlap another window.

Score viral potential from 0 to 100 and be honest. Reserve 85 and above for clips you would personally bet money on. Most clips in an average video score between 40 and 70. Do not inflate.

For each clip also give:
- title: a 3 to 6 word label for the moment
- hook: the actual opening words of the clip, quoted from the transcript
- reason: at most 16 words on why it performs
- tags: 1 to 3 single words from this set only: hook, story, emotion, insight, conflict, humor, data, howto, reveal

Respond with raw JSON only. No markdown fence, no commentary.
{{"clips":[{{"start_seconds":42.0,"end_seconds":102.0,"viral_score":87,"title":"The hiring mistake","hook":"Everybody told me to hire slow","reason":"Contrarian claim up front, concrete story, clean payoff","tags":["insight","story"]}}]}}"""

    if provider == "anthropic":
        text = _call_anthropic(prompt, api_key, spec["model"])
    else:
        text = _call_openai_compatible(
            prompt, api_key, spec["model"], spec["base_url"])

    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        clips = json.loads(raw)["clips"]
    except (json.JSONDecodeError, KeyError):
        raise RuntimeError("The AI response could not be read. Try running it again.")

    clips.sort(key=lambda c: -c.get("viral_score", 0))
    clean = []
    for i, c in enumerate(clips[:max_clips], 1):
        start = max(0.0, float(c["start_seconds"]))
        end = min(float(c["end_seconds"]), total)
        if end - start < 8:
            continue
        clean.append({
            "n": i,
            "start": round(start, 2),
            "end": round(end, 2),
            "duration": round(end - start, 1),
            "score": int(c.get("viral_score", 0)),
            "title": c.get("title") or f"Moment {i}",
            "hook": c.get("hook") or "",
            "reason": c.get("reason") or "",
            "tags": [t for t in (c.get("tags") or [])[:3] if isinstance(t, str)],
            "exported": False,
        })
    return clean


def make_thumb(video_path, at_seconds, out_path):
    _run(["ffmpeg", "-y", "-ss", str(at_seconds + 1), "-i", str(video_path),
          "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(out_path)])
    return out_path.exists()


def cut(video_path, start, end, out_path, vertical, blur_pad=True,
        progress=None, control=NOOP):
    duration = end - start
    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats",
           "-ss", str(start), "-i", str(video_path), "-t", str(duration)]
    if vertical:
        if blur_pad:
            vf = ("[0:v]split=2[bg][fg];"
                  "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                  "crop=1080:1920,gblur=sigma=28[bgb];"
                  "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
                  "[bgb][fgs]overlay=(W-w)/2:(H-h)/2")
            cmd += ["-filter_complex", vf]
        else:
            cmd += ["-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
                           "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(out_path)]

    def on_line(line):
        if progress and line.startswith("out_time_us="):
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                return
            progress(min(99, done / duration * 100))

    code, stderr = _stream(cmd, control, on_line)
    if code != 0:
        raise RuntimeError(stderr[-250:] if stderr else "ffmpeg failed")
    if progress:
        progress(100)
    return out_path


def slugify(text, limit=40):
    s = re.sub(r"[^\w\s-]", "", text or "").strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:limit].strip("-") or "clip"
