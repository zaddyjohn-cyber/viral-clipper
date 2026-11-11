"""
Clip engine: download, transcribe, score, cut.
Pure functions, no UI. Used by server.py.
"""

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
    """Run a command, feeding each output line to on_line. Returns (code, tail).

    stderr is merged into stdout deliberately. Draining only stdout while
    ffmpeg writes to a separate stderr pipe deadlocks as soon as that pipe
    fills: ffmpeg blocks on stderr, stops emitting progress, and the reader
    waits forever on stdout. Burning captions produces more than enough log
    output to hit that.
    """
    control.check()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True, bufsize=1, creationflags=NO_WINDOW)
    control.attach(proc)
    tail = collections.deque(maxlen=40)
    for line in proc.stdout:
        line = line.rstrip()
        tail.append(line)
        on_line(line)
    proc.wait()
    control.check()
    return proc.returncode, chr(10).join(tail)


def probe_duration(path):
    proc = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


CACHE_DIR = Path.home() / ".viral_clipper_cache"


def cached_media(url, audio_only):
    """Downloads are expensive on a weak line, so keep them between runs."""
    import hashlib
    key = hashlib.sha1(f"{url}|{'a' if audio_only else 'v'}".encode()).hexdigest()[:16]
    CACHE_DIR.mkdir(exist_ok=True)
    hits = sorted(CACHE_DIR.glob(f"{key}.*"))
    return key, (hits[0] if hits else None)


def _key(*parts):
    import hashlib
    return hashlib.sha1("|".join(str(x) for x in parts).encode()).hexdigest()[:16]


def checkpoint_dir(url):
    d = CACHE_DIR / f"run_{_key(url)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_checkpoint(url, name):
    f = checkpoint_dir(url) / f"{name}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_checkpoint(url, name, data):
    try:
        (checkpoint_dir(url) / f"{name}.json").write_text(
            json.dumps(data), encoding="utf-8")
    except OSError:
        pass            # a lost checkpoint only costs time, never correctness


def cookie_args(cookies_browser, cookies_file):
    """A cookies.txt file wins. It is the only option that works headless."""
    if cookies_file and Path(cookies_file).is_file():
        return ["--cookies", str(cookies_file)]
    if cookies_browser and cookies_browser != "none":
        return ["--cookies-from-browser", cookies_browser]
    return []


def download(url, out_dir, cookies_browser, progress, control=NOOP, cookies_file="",
             audio_only=False):
    """Download to out_dir/source.*, return (path, meta dict).

    audio_only pulls roughly 1% of the bytes. Transcription never needs the
    picture, so analysis uses it and the video is fetched later, only if the
    user actually exports something.
    """
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

    key, hit = cached_media(url, audio_only)
    if hit:
        progress(f"Reusing the {'audio' if audio_only else 'video'} "
                 "from an earlier run", 100)
        if not meta.get("duration"):
            meta["duration"] = probe_duration(hit)
        meta.setdefault("title", "Untitled")
        return hit, meta

    progress("Starting download", 0)
    stem = "audio" if audio_only else "source"
    fmt = ("bestaudio[abr<=70]/bestaudio/best" if audio_only else
           # YouTube serves video-only and audio-only streams for most videos,
           # so the merge selector has to come before any combined selector.
           "bestvideo[height<=720]+bestaudio/best[height<=720]"
           "/bestvideo+bestaudio/best")
    dl_cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--newline",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--progress-template", "PCT %(progress._percent_str)s %(progress._speed_str)s",
        "-o", str(out_dir / (stem + ".%(ext)s")),
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
        speed = parts[2] if len(parts) > 2 and parts[2] != "Unknown" else ""
        if audio_only:
            label, base, span = "audio", 0, 100
        else:
            label = "video" if seen["n"] == 0 else "audio"
            base, span = (0, 50) if seen["n"] == 0 else (50, 50)
        progress(f"Downloading {label}" + (f" at {speed}" if speed else ""),
                 min(99, base + pct * span / 100))

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

    files = [f for f in out_dir.iterdir() if f.stem == stem]
    if not files:
        raise RuntimeError("Download finished but produced no file.")

    path = files[0]
    try:
        keep = CACHE_DIR / f"{key}{path.suffix}"
        shutil.move(str(path), keep)
        path = keep
    except OSError:
        pass            # cache is a convenience, carry on with the temp copy

    if not meta.get("duration"):
        meta["duration"] = probe_duration(path)
    meta.setdefault("title", "Untitled")
    return path, meta


GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
# Whisper only listens at 16kHz mono, so a low bitrate loses nothing that
# matters and keeps whole hours inside one upload.
GROQ_BITRATE_K = 24
GROQ_TARGET_MB = 5        # small enough to survive an unstable upload
GROQ_CHUNK_SECONDS = int(GROQ_TARGET_MB * 1024 * 8 / GROQ_BITRATE_K)
GROQ_ATTEMPTS = 4
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


def _clean_words(raw, offset=0.0):
    """Normalise word timings to {start,end,word}, dropping anything unusable.

    Both back ends can hand back a word with no text, or with an end before
    its start. Captions key every pop off these times, so one bad entry is
    worth more than it costs to throw away.
    """
    out = []
    for w in raw or []:
        if isinstance(w, dict):
            word, a, b = w.get("word"), w.get("start"), w.get("end")
        else:
            word, a, b = (getattr(w, "word", None), getattr(w, "start", None),
                          getattr(w, "end", None))
        word = (word or "").strip()
        if not word or a is None or b is None:
            continue
        try:
            a, b = float(a) + offset, float(b) + offset
        except (TypeError, ValueError):
            continue
        if b <= a:
            b = a + 0.08
        out.append({"start": a, "end": b, "word": word})
    return out


def _attach_words(segments, words):
    """Groq reports words for the whole chunk, so hand each one to its line."""
    if not words:
        return
    i = 0
    for sg in segments:
        mine = []
        while i < len(words) and words[i]["end"] <= sg["start"] + 0.01:
            i += 1                      # a word that ended before this line
        j = i
        while j < len(words) and words[j]["start"] < sg["end"]:
            mine.append(words[j])
            j += 1
        if mine:
            sg["words"] = mine
            i = j


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
                                    word_timestamps=True,
                                    condition_on_previous_text=False)

    total = info.duration or probe_duration(video_path) or 1
    out = []
    for seg in stream:
        control.check()
        text = seg.text.strip()
        if text:
            line = {"start": seg.start, "end": seg.end, "text": text}
            words = _clean_words(getattr(seg, "words", None) or [])
            if words:
                line["words"] = words
            out.append(line)
        progress(f"Transcribed {fmt_time(seg.end)} of {fmt_time(total)}",
                 min(99, seg.end / total * 100))

    progress(f"Read {len(out)} lines of speech", 100)
    return out


def _transcribe_groq(video_path, model_name, api_key, progress, control=NOOP, url=""):
    """Chunked upload. Long videos exceed Groq's per-file cap in one piece."""
    import httpx
    if not api_key:
        raise RuntimeError("Add your Groq API key in Settings first.")

    total = probe_duration(video_path) or 1
    # Scope the workspace to this exact source file. A shared folder let one
    # video's compressed audio be picked up and transcribed for another.
    work = Path(video_path).parent / f"work_{Path(video_path).stem}"
    work.mkdir(parents=True, exist_ok=True)

    # One decode pass for the whole file. Slicing the original per chunk meant
    # re-decoding the source dozens of times, which was the real bottleneck.
    compact = work / "speech.mp3"
    if not compact.exists():
        progress("Compressing audio for upload", 0)
        partial = work / "speech.part.mp3"
        proc = _run(["ffmpeg", "-y", "-i", str(video_path), "-vn",
                     "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame",
                     "-b:a", f"{GROQ_BITRATE_K}k", str(partial)], control)
        if proc.returncode != 0 or not partial.exists():
            partial.unlink(missing_ok=True)
            raise RuntimeError(
                f"Could not prepare the audio. {(proc.stderr or '')[-200:]}")
        partial.replace(compact)        # only named speech.mp3 once complete
    source = compact

    starts = [s for s in range(0, max(1, int(total)), GROQ_CHUNK_SECONDS)]
    out = []
    want_words = {"yes": True}      # cleared if the endpoint refuses the ask

    # Each chunk is saved the moment it lands, so a dropped connection costs
    # one chunk rather than the whole transcript.
    done = {}
    if url:
        done = (load_checkpoint(url, f"chunks_{model_name}") or {})
        if done:
            progress(f"Picking up {len(done)} chunks from the last run", 0)

    for i, start in enumerate(starts):
        control.check()
        span = min(GROQ_CHUNK_SECONDS, total - start)
        if span <= 0.5:
            break

        if str(i) in done:
            out += done[str(i)]
            progress(f"Already had {fmt_time(start)} to {fmt_time(start + span)}",
                     min(99, (i + 1) / len(starts) * 100))
            continue

        base = i / len(starts) * 100
        step = 100 / len(starts)
        progress(f"Preparing audio {i + 1} of {len(starts)}", base)
        piece = work / f"part{i}.mp3"
        proc = _run(["ffmpeg", "-y", "-ss", str(start), "-i", str(source),
                     "-t", str(span), "-c", "copy", str(piece)], control)
        if proc.returncode != 0 or not piece.exists():
            piece = extract_audio(source, piece, control, start, span)

        if piece.stat().st_size > GROQ_MAX_BYTES:
            raise RuntimeError(
                "An audio chunk came out too large to upload. "
                "Switch to faster-whisper in Settings for this one.")

        label = f"{fmt_time(start)} to {fmt_time(start + span)}"
        res = None
        for attempt in range(1, GROQ_ATTEMPTS + 1):
            control.check()
            note = "" if attempt == 1 else f", retry {attempt - 1}"
            progress(f"Transcribing {label}{note}", base + step * 0.35)
            form = {"model": model_name, "response_format": "verbose_json",
                    "temperature": "0"}
            if want_words["yes"]:
                form["timestamp_granularities[]"] = ["segment", "word"]
            try:
                with open(piece, "rb") as fh:
                    res = httpx.post(
                        GROQ_URL,
                        headers={"Authorization": f"Bearer {api_key}"},
                        files={"file": (piece.name, fh, "audio/mpeg")},
                        data=form,
                        timeout=600.0)
            except httpx.HTTPError as e:
                # A dropped upload is normal on a weak line, so keep trying.
                if attempt == GROQ_ATTEMPTS:
                    piece.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"Lost the connection to Groq {GROQ_ATTEMPTS} times while "
                        f"sending {label}. Last error: {e}")
                time.sleep(2 ** attempt)
                continue

            # Not every model on the endpoint serves word timings. Losing the
            # pop on each caption word is not worth losing the transcript, so
            # drop the ask and carry on without them.
            if (res.status_code == 400 and want_words["yes"]
                    and "granularit" in res.text.lower()):
                want_words["yes"] = False
                continue

            # A dropped upload reaches Groq as a truncated, unreadable file,
            # so treat that 400 as transient too.
            truncated = (res.status_code == 400
                         and "valid media file" in res.text.lower())
            if ((res.status_code in (429, 500, 502, 503, 504) or truncated)
                    and attempt < GROQ_ATTEMPTS):
                time.sleep(2 ** attempt)
                continue
            break

        piece.unlink(missing_ok=True)

        if res.status_code == 401:
            raise RuntimeError("Your Groq API key was rejected. Check it in Settings.")
        if res.status_code == 429:
            raise RuntimeError("Groq rate limit hit. Wait a few minutes and retry.")
        if res.status_code == 413:
            raise RuntimeError("Groq rejected the audio as too large.")
        if res.status_code >= 400:
            raise RuntimeError(f"Groq returned {res.status_code}. {res.text[:200]}")

        try:
            body = res.json()
            segments = body.get("segments") or []
        except json.JSONDecodeError:
            raise RuntimeError("Groq sent back a response that could not be read.")

        got = []
        for seg in segments:
            text = (seg.get("text") or "").strip()
            if text:
                got.append({"start": seg["start"] + start,
                            "end": seg["end"] + start, "text": text})
        _attach_words(got, _clean_words(body.get("words"), start))
        out += got

        if url:
            done[str(i)] = got
            save_checkpoint(url, f"chunks_{model_name}", done)

        progress(f"Transcribed {fmt_time(min(start + span, total))} of {fmt_time(total)}",
                 min(99, (i + 1) / len(starts) * 100))

    progress(f"Read {len(out)} lines of speech", 100)
    return out


def transcribe(video_path, transcriber, model_name, api_key, progress,
               control=NOOP, url=""):
    """Returns timestamped segments, resuming from checkpoints where possible."""
    if url:
        cached = load_checkpoint(url, f"transcript_{transcriber}_{model_name}")
        if cached:
            progress(f"Reusing the transcript from an earlier run "
                     f"({len(cached)} lines)", 100)
            return cached

    if transcriber == "groq":
        out = _transcribe_groq(video_path, model_name, api_key, progress,
                               control, url)
    else:
        out = _transcribe_local(video_path, model_name, progress, control)

    if url and out:
        save_checkpoint(url, f"transcript_{transcriber}_{model_name}", out)
    return out


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


# The prompt asks for three to six words. The character budget is what the
# frame actually enforces: past this the card runs to a third line and
# covers the face. The word cap is only a guard against a rambling answer.
HOOK_CARD_WORDS = 8
HOOK_CARD_CHARS = 38


def _trim_card(text):
    """Cut a hook down to size, or give up and say so.

    Dropping the last word off a line the model wrote too long leaves a
    dangling phrase on screen: "He has to shoot from way" is worse than no
    card at all. So a line that only fits by losing a word is refused, and
    the caller falls back to the title, which is already short and whole.
    """
    text = re.sub(r"\s+", " ", (text or "").strip(" \"'`*#.,!?-"))
    if not text:
        return ""
    # A line ending on a word that cannot end a sentence was cut off, whoever
    # cut it. Burning it in looks like a bug to everyone who sees it.
    if re.search(r"\b(the|a|an|and|or|but|to|of|for|from|with|on|in|at|by|as"
                 r"|is|was|that|this|his|her|their|my|your|our|its)$",
                 text, re.I):
        return ""
    words = text.split()
    if len(words) > HOOK_CARD_WORDS or len(text) > HOOK_CARD_CHARS:
        # Unless it breaks cleanly at punctuation the model put there itself.
        head = re.split(r"[,;:.!?]", text)[0].strip()
        if (head and head != text and len(head.split()) >= 3
                and len(head.split()) <= HOOK_CARD_WORDS
                and len(head) <= HOOK_CARD_CHARS):
            return head
        return ""
    return text


def _hook_card(clip):
    """The written hook, or the title when the model would not keep it short."""
    return (_trim_card(clip.get("hook_card"))
            or _trim_card(clip.get("title"))
            or "")


def score_moments(segments, provider, api_key, clip_len, max_clips, progress, url=""):
    """Scores moments by line number.

    Asking a model for raw timestamps invites it to invent them, and on a long
    transcript it will. Citing line numbers forces every clip to point at real
    speech, and the times and quotes are then read back from the transcript
    rather than taken on trust.
    """
    stamp = f"clips_{provider}_{clip_len}_{max_clips}"
    if url:
        cached = load_checkpoint(url, stamp)
        if cached:
            progress(f"Reusing {len(cached)} moments scored earlier", 100)
            return cached

    spec = PROVIDERS.get(provider)
    if not spec:
        raise RuntimeError(f"Unknown scoring provider '{provider}'.")
    progress(f"Scoring moments with {spec['label']}", 20)

    numbered = "\n".join(
        f"{i}\t{fmt_time(sg['start'])}\t{sg['text']}"
        for i, sg in enumerate(segments))
    total = segments[-1]["end"]
    lo, hi = max(15, clip_len - 25), clip_len + 20

    prompt = f"""You are a short-form video editor who has shipped thousands of viral clips.

Below is a transcript of a {fmt_time(total)} video. Every line is numbered. Find the strongest standalone moments to cut into vertical shorts of roughly {clip_len} seconds.

What makes a clip work:
- Lands a hook in the first 2 seconds, no slow build
- One complete thought, story or payoff that stands alone without the rest of the video
- Carries tension, a surprising claim, a reveal, strong emotion, a specific number, or a contrarian take
- Starts and ends on clean sentence boundaries

TRANSCRIPT, as "line<TAB>time<TAB>text":
{numbered}

Return up to {max_clips} clips, best first, as line ranges. start_line and end_line must be real line numbers from above, between 0 and {len(segments) - 1}. Each clip must run {lo} to {hi} seconds and must not overlap another clip.

Do not invent or paraphrase. The hook you report must be copied exactly from the text of start_line.

Score viral potential 0 to 100 and be honest. Reserve 85 and above for clips you would personally bet money on. Most clips in an average video land between 40 and 70.

Also give a title of 3 to 6 words, a reason of at most 16 words, and 1 to 3 tags from: hook, story, emotion, insight, conflict, humor, data, howto, reveal.

Write a hook_card as well: 3 to 6 words that get burned across the top of the screen for the first second and a half. This is yours to write, not a quote. Make it the promise that stops the scroll: a stake, a number, a contradiction or an open loop the clip then closes. No hashtags, no emoji, no end punctuation, no quotation marks.

Respond with raw JSON only, no markdown fence:
{{"clips":[{{"start_line":412,"end_line":436,"viral_score":87,"title":"The hiring mistake","hook":"exact text of line 412","hook_card":"He hired the wrong guy twice","reason":"why it performs","tags":["insight","story"]}}]}}"""

    if provider == "anthropic":
        text = _call_anthropic(prompt, api_key, spec["model"])
    else:
        text = _call_openai_compatible(
            prompt, api_key, spec["model"], spec["base_url"])

    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        proposed = json.loads(raw)["clips"]
    except (json.JSONDecodeError, KeyError):
        raise RuntimeError("The AI response could not be read. Try running it again.")

    proposed.sort(key=lambda c: -c.get("viral_score", 0))
    clean, used = [], []

    for c in proposed:
        try:
            a = int(c["start_line"])
            b = int(c["end_line"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= a < len(segments)) or not (0 <= b < len(segments)) or b <= a:
            continue

        start = segments[a]["start"]
        end = min(segments[b]["end"], total)

        # Grow or trim to the requested length, always landing on a real line.
        while end - start < lo and b + 1 < len(segments):
            b += 1
            end = min(segments[b]["end"], total)
        while end - start > hi and b - 1 > a:
            b -= 1
            end = segments[b]["end"]
        if end - start < 10:
            continue
        if any(start < u_end and end > u_start for u_start, u_end in used):
            continue

        used.append((start, end))
        clean.append({
            "n": len(clean) + 1,
            "start": round(start, 2),
            "end": round(end, 2),
            "duration": round(end - start, 1),
            "score": max(0, min(100, int(c.get("viral_score", 0)))),
            "title": (c.get("title") or f"Moment {len(clean) + 1}")[:70],
            "hook": segments[a]["text"][:160],
            "hook_card": _hook_card(c),
            "text": " ".join(sg["text"] for sg in segments[a:b + 1])[:1400],
            "reason": (c.get("reason") or "")[:140],
            "tags": [t for t in (c.get("tags") or [])[:3] if isinstance(t, str)],
            "lines": [a, b],
            "exported": False,
        })
        if len(clean) >= max_clips:
            break

    if not clean:
        raise RuntimeError(
            "The scoring model did not point at any usable part of the "
            "transcript. Try running it again, or switch provider in Settings.")

    if url:
        save_checkpoint(url, stamp, clean)
    return clean


# Caption looks. ASS colours are &HAABBGGRR, so the byte order is reversed.
CAPTION_STYLES = {
    "none": {"label": "No captions", "blurb": "Clean video, nothing burned in."},
    "punch": {
        "label": "Punch",
        "blurb": "Big white caps with a heavy black stroke. The default look on Shorts.",
        "font": "Arial Black", "size": 92, "primary": "&H00FFFFFF",
        "outline": "&H00000000", "back": "&H00000000", "border": 1,
        "outline_w": 7, "shadow": 3, "upper": True, "margin_v": 420,
        "accent": "&H0000F0FF",
    },
    "pop": {
        "label": "Pop",
        "blurb": "Yellow on black outline. Reads well over busy footage.",
        "font": "Arial Black", "size": 88, "primary": "&H0000F0FF",
        "outline": "&H00000000", "back": "&H00000000", "border": 1,
        "outline_w": 6, "shadow": 2, "upper": True, "margin_v": 420,
        "accent": "&H00FFFFFF",
    },
    "clean": {
        "label": "Clean",
        "blurb": "White on a soft dark band. Calmer, good for talking heads.",
        "font": "Arial", "size": 74, "primary": "&H00FFFFFF",
        # BorderStyle 3 draws the box, and its padding comes from Outline,
        # so that has to be non-zero or the band never appears.
        "outline": "&H00000000", "back": "&HB0000000", "border": 3,
        "outline_w": 14, "shadow": 0, "upper": False, "margin_v": 400,
        "accent": "&H0066E0FF",
    },
}

CAPTION_WORDS = 4          # short lines read faster on a phone

# Word-level captions. Two to four words at a time is the short-form norm:
# enough to read in one glance, few enough that the next one lands before
# attention wanders.
CAPTION_GROUP_MAX = 4
CAPTION_GROUP_MIN = 2
# Arial Black at the sizes these styles use fits about eighteen characters
# across a 1080 frame once the side margins are taken off. Past that the line
# wraps, and a two-line caption covers the chin of whoever is talking.
CAPTION_GROUP_CHARS = 18
CAPTION_GROUP_GAP = 0.45   # a pause this long ends the line
CAPTION_GROUP_SPAN = 2.4   # and so does holding one line this long
CAPTION_TAIL = 0.2         # how long the finished line stays up

CAPTION_POP = 118          # percent scale a word lands at
CAPTION_SETTLE = 130       # ms it takes to settle back to 100

# Phone chrome: the caption, handle, button rail and progress bar all live in
# the bottom fifth of a 1920-tall frame, and the top eighth holds the search
# and follow row. Nothing we burn in is allowed inside either.
CAPTION_SAFE_V = 420
CAPTION_SAFE_H = 130

# The call to action at the end. A clip that names what comes next earns a
# follow in a way "follow for more" never does, so each one teases the clip
# that is meant to be posted after it.
CTA_SECONDS = 3.0
CTA_LEAD = "Follow and subscribe for"
CTA_LAST = "more like this"      # when there is no next clip to point at
CTA_STYLE = {
    # A column down one side rather than a banner across the middle, so it
    # never lands on the face of the person it is asking you to follow.
    "font": "Arial Black", "size": 46, "primary": "&H00FFFFFF",
    # The app's accent, as BGR: a filled box reads as something to do,
    # where the hook card's dark box reads as something to know.
    "outline": "&H00FC5C7C", "back": "&H00FC5C7C", "border": 3,
    "outline_w": 12, "shadow": 0, "margin_v": 300,
}
CTA_LEAD_SIZE = 32
CTA_COL_W = 430           # how wide the column is allowed to run
CTA_EDGE = 60             # and how far it sits off the side of the frame
CTA_CLEAR = 40            # space it keeps between itself and a face

# The written hook, burned across the top third for the first moment or two.
HOOK_SECONDS = 1.5
HOOK_STYLE = {
    "font": "Arial Black", "size": 82, "primary": "&H00FFFFFF",
    # BorderStyle 3 fills a box behind the text; libass takes that fill from
    # the outline colour and the padding from its width, so both are set.
    "outline": "&H28101010", "back": "&H28101010", "border": 3,
    "outline_w": 18, "shadow": 0, "margin_v": 300,
}


def _ass_time(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{sec:05.2f}"


def _caption_lines(segments, start, end):
    """Transcript lines inside the clip, split short and timed clip-relative."""
    out = []
    for sg in segments:
        if sg["end"] <= start or sg["start"] >= end:
            continue
        words = sg["text"].split()
        if not words:
            continue

        a = max(sg["start"], start) - start
        b = min(sg["end"], end) - start
        if b - a < 0.08:
            continue

        groups = [words[i:i + CAPTION_WORDS]
                  for i in range(0, len(words), CAPTION_WORDS)]
        step = (b - a) / len(groups)
        for i, g in enumerate(groups):
            out.append((a + i * step, a + (i + 1) * step, " ".join(g)))
    return out


def _ass_text(s):
    """Strip anything libass would read as an override block."""
    return (str(s or "").replace("\\", "").replace("{", "(")
            .replace("}", ")").replace("\n", " ").strip())


def _ass_colour(c):
    """Inline colour tags need the closing ampersand the style fields omit."""
    c = (c or "").strip()
    return c if c.endswith("&") else c + "&"


def _clip_words(segments, start, end):
    """Word timings inside the clip, clip-relative.

    Whisper reports a word at a time when asked, which is what the captions
    are keyed off. A transcript from before that was turned on, or from a
    back end that will not serve it, still has to produce captions, so the
    words are spread evenly across their line instead.
    """
    out = []
    span = end - start
    for sg in segments:
        if sg["end"] <= start or sg["start"] >= end:
            continue
        words = sg.get("words") or []
        if words:
            for w in words:
                a = w["start"] - start
                b = w["end"] - start
                if b <= 0 or a >= span:
                    continue
                text = _ass_text(w["word"])
                if text:
                    out.append((max(0.0, a), min(span, b), text))
            continue

        pieces = [_ass_text(p) for p in (sg.get("text") or "").split()]
        pieces = [p for p in pieces if p]
        if not pieces:
            continue
        a = max(sg["start"], start) - start
        b = min(sg["end"], end) - start
        if b - a < 0.08:
            continue
        step = (b - a) / len(pieces)
        for i, piece in enumerate(pieces):
            out.append((a + i * step, a + (i + 1) * step, piece))

    out.sort(key=lambda w: w[0])
    return out


def _group_words(words):
    """Break the word stream into the two to four word lines that go up."""
    groups, current = [], []

    def flush():
        if current:
            groups.append(list(current))
            current.clear()

    for a, b, text in words:
        if current:
            gap = a - current[-1][1]
            chars = sum(len(w[2]) for w in current) + len(current) + len(text)
            long_enough = len(current) >= CAPTION_GROUP_MIN
            if (len(current) >= CAPTION_GROUP_MAX
                    or (long_enough and gap > CAPTION_GROUP_GAP)
                    or (long_enough and b - current[0][0] > CAPTION_GROUP_SPAN)
                    or chars > CAPTION_GROUP_CHARS):
                flush()
            # A sentence that ended wants the next line to itself.
            elif long_enough and re.search(r"[.!?]$", current[-1][2]):
                flush()
        current.append((a, b, text))
    flush()
    return groups


def _word_events(groups, st):
    """One event per word: the whole line, with the word that just landed popped.

    Showing the finished line and animating only the live word keeps the text
    from reflowing under the reader, which is what makes word-by-word captions
    feel jumpy when each word is drawn on its own.
    """
    accent = _ass_colour(st.get("accent") or st.get("primary"))
    events = []
    for g, group in enumerate(groups):
        tail = group[-1][1] + CAPTION_TAIL
        if g + 1 < len(groups):
            tail = min(tail, groups[g + 1][0][0])
        for i, (a, _b, _w) in enumerate(group):
            stop = group[i + 1][0] if i + 1 < len(group) else tail
            if stop - a < 0.02:
                continue
            parts = []
            for j, (_, _, word) in enumerate(group):
                word = word.upper() if st.get("upper") else word
                if j == i:
                    parts.append(
                        "{" + rf"\1c{accent}\fscx{CAPTION_POP}\fscy{CAPTION_POP}"
                        rf"\t(0,{CAPTION_SETTLE},\fscx100\fscy100)" + "}"
                        + word + r"{\r}")
                else:
                    parts.append(word)
            events.append((a, stop, " ".join(parts)))
    return events


def _style_line(name, st, align, margin_v, margin_h):
    return (f"Style: {name},{st['font']},{st['size']},{st['primary']},"
            f"&H000000FF,{st['outline']},{st['back']},-1,0,0,0,100,100,0,0,"
            f"{st['border']},{st['outline_w']},{st['shadow']},{align},"
            f"{margin_h},{margin_h},{margin_v},1")


def post_order(clips):
    """The order to post a run's clips in, strongest first.

    The first one has to earn the follow, so it is the one the scorer rated
    highest, and every clip after that is already being watched by someone
    who followed for it.
    """
    ranked = sorted(clips, key=lambda c: (-int(c.get("score") or 0),
                                          float(c.get("start") or 0)))
    return [dict(c, post_order=i + 1) for i, c in enumerate(ranked)]


def cta_chain(clips, lead=CTA_LEAD, last=CTA_LAST):
    """What each clip should promise, which is the next clip by name.

    Returns {clip number: (lead line, tease)}. The tease is the next clip's
    written hook, so the promise and the payoff are the same words.
    """
    ranked = post_order(clips)
    out = {}
    for i, c in enumerate(ranked):
        tease = last
        if i + 1 < len(ranked):
            nxt = ranked[i + 1]
            # Same rule as the hook card: a promise that reads as a cut-off
            # sentence is worse than a shorter one that reads whole.
            tease = (_trim_card(nxt.get("hook_card"))
                     or _trim_card(nxt.get("title")) or last)
        out[c["n"]] = (lead, _ass_text(tease))
    return out


def cta_side(track, at, until):
    """Which side of the frame the end card can have without covering a face.

    Measured over the seconds the card is actually up, in the coordinates of
    the exported frame. With no face to work around, or a face wide enough to
    leave no room either side, it goes right, which is the side the platforms
    keep their own buttons off.
    """
    faces = (track or {}).get("face_at") or []
    window = [f for f in faces if at - 0.5 <= f[0] <= until + 0.5] or faces
    if not window:
        return "right"

    # Count the frames each side would actually land on a face. One stray
    # sample should not decide this, and the side that clashes least is the
    # answer whether or not either side is perfectly clear.
    def clashes(x0, x1):
        return sum(1 for _, cx, half in window
                   if cx - half - CTA_CLEAR < x1 and cx + half + CTA_CLEAR > x0)

    on_left = clashes(CTA_EDGE, CTA_EDGE + CTA_COL_W)
    on_right = clashes(OUT_W - CTA_EDGE - CTA_COL_W, OUT_W - CTA_EDGE)
    return "left" if on_left < on_right else "right"


def build_ass(segments, start, end, style_name, out_path, width=1080, height=1920,
              hook_card="", word_level=True, cta=None, cta_at="right"):
    """Write the subtitle file burned over a clip.

    cta is the (lead line, tease) pair for the end card, as cta_chain builds it.
    """
    st = CAPTION_STYLES.get(style_name)
    captions = bool(st) and style_name != "none"
    hook = _ass_text(hook_card)[:HOOK_CARD_CHARS]
    lead, tease = (cta or ("", ""))
    lead, tease = _ass_text(lead), _ass_text(tease)
    if not captions and not hook and not tease:
        return None

    styles, body = [], []

    if captions:
        margin_v = max(int(st.get("margin_v") or 0), CAPTION_SAFE_V)
        styles.append(_style_line("Cap", st, 2, margin_v, CAPTION_SAFE_H))
        if word_level:
            events = _word_events(_group_words(_clip_words(segments, start, end)), st)
        else:
            events = [(a, b, _ass_text(t).upper() if st.get("upper")
                       else _ass_text(t))
                      for a, b, t in _caption_lines(segments, start, end)]
        for a, b, text in events:
            if text:
                body.append(
                    f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Cap,,0,0,0,,{text}")

    if hook:
        styles.append(_style_line("Hook", HOOK_STYLE, 8,
                                  HOOK_STYLE["margin_v"], 160))
        # Layer 1 so it sits over a caption if one happens to run under it.
        body.append(
            f"Dialogue: 1,{_ass_time(0)},{_ass_time(HOOK_SECONDS)},Hook,,0,0,0,,"
            + "{" + r"\fad(140,260)\fscx84\fscy84\t(0,200,\fscx100\fscy100)" + "}"
            + hook.upper())

    if tease:
        # Pinned to one side, top aligned, in a column narrow enough that it
        # cannot reach the middle of the frame where the face is. Alignment
        # 7 is top left, 9 top right; the wide margin on the other side is
        # what holds the column to its width.
        rest = width - CTA_COL_W - CTA_EDGE
        left_m, right_m = ((CTA_EDGE, rest) if cta_at == "left"
                           else (rest, CTA_EDGE))
        styles.append(
            f"Style: Cta,{CTA_STYLE['font']},{CTA_STYLE['size']},"
            f"{CTA_STYLE['primary']},&H000000FF,{CTA_STYLE['outline']},"
            f"{CTA_STYLE['back']},-1,0,0,0,100,100,0,0,{CTA_STYLE['border']},"
            f"{CTA_STYLE['outline_w']},{CTA_STYLE['shadow']},"
            f"{7 if cta_at == 'left' else 9},{left_m},{right_m},"
            f"{CTA_STYLE['margin_v']},1")
        # Late enough to have earned the ask, early enough that it is still
        # on screen when the thumb starts moving.
        at = max(0.0, (end - start) - CTA_SECONDS)
        body.append(
            f"Dialogue: 1,{_ass_time(at)},{_ass_time(end - start)},Cta,,0,0,0,,"
            + "{" + r"\fad(180,120)\fscx86\fscy86\t(0,220,\fscx100\fscy100)"
            + rf"\fs{CTA_LEAD_SIZE}" + "}"
            + (lead or CTA_LEAD).upper()
            + r"\N{" + rf"\fs{CTA_STYLE['size']}" + "}"
            + tease.upper())

    if not body:
        return None

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
{chr(10).join(styles)}

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    out_path.write_text(head + "\n".join(body) + "\n", encoding="utf-8")
    return out_path


def make_thumb(video_path, at_seconds, out_path):
    _run(["ffmpeg", "-y", "-ss", str(at_seconds + 1), "-i", str(video_path),
          "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(out_path)])
    return out_path.exists()


def _ass_filter(path):
    """ffmpeg reads the drive-letter colon as an option separator, so escape it."""
    p = str(path).replace("\\", "/")
    p = p.replace(":", chr(92) + ":").replace("'", chr(92) + "'")
    return "ass='" + p + "'"


# ---------- speaker-tracking reframe ----------

# YuNet: one small ONNX file, a few milliseconds a frame on a plain CPU, and
# no runtime to install beside it. The file lives next to the code so a box
# with no outbound network still reframes; the download is only there for a
# checkout that arrived without it.
FACE_MODEL_NAME = "face_detection_yunet_2023mar.onnx"
FACE_MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                  "face_detection_yunet/" + FACE_MODEL_NAME)
FACE_MODEL_DIRS = [Path(__file__).resolve().parent / "models",
                   Path.home() / ".viral_clipper" / "models"]

OUT_W, OUT_H = 1080, 1920   # the exported frame everything is measured against

FACE_FPS = 6              # detection rate, nothing to do with the output
FACE_WIDTH = 480          # detection runs on a small copy of the picture
FACE_SCORE = 0.6
FACE_COVERAGE = 0.3       # below this share of frames with a face, do not try

# The crop window chases the face rather than following it exactly: a window
# that tracks every small head movement reads as a handheld camera.
TRACK_CHASE = 0.3         # share of the remaining distance per sample
TRACK_DEADZONE = 0.03     # of source width; smaller drifts are ignored
TRACK_MAX_STEP = 0.045    # of source width per sample, so pans stay slow
TRACK_CUT = 0.2           # a target this far off is a camera cut, so jump
TRACK_KEY_STEP = 0.003    # of source width; smaller moves need no keyframe
TRACK_MAX_KEYS = 110      # keeps the filter expression a sane length
TRACK_SHARE = 12.0        # share of the export progress bar detection owns

# Telling two people apart by how much their mouths move is crude, so the
# frame has to be slow to change its mind: the other face has to move a good
# deal more, for several samples running, and never twice in quick succession.
SPEAKER_HOLD = 1.1        # seconds a face keeps the frame before it may move
SPEAKER_LEAD = 1.35       # how much more the other mouth has to be moving
SPEAKER_SUSTAIN = 3       # samples it has to keep that lead


def _face_model():
    """Path to the detector weights, fetching them once if they are missing."""
    for d in FACE_MODEL_DIRS:
        f = d / FACE_MODEL_NAME
        if f.is_file():
            return f

    target = FACE_MODEL_DIRS[-1] / FACE_MODEL_NAME
    try:
        import urllib.request
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(FACE_MODEL_URL, timeout=60) as r:
            data = r.read()
        if len(data) < 100_000:         # a proxy's error page, not the model
            return None
        part = target.with_suffix(".part")
        part.write_bytes(data)
        part.replace(target)
        return target
    except (OSError, ValueError):
        return None


def video_size(path):
    proc = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=p=0:s=x", str(path)])
    try:
        w, h = proc.stdout.strip().splitlines()[0].split("x")
        return int(w), int(h)
    except (IndexError, ValueError):
        return 0, 0


def _crop_box(iw, ih):
    """The largest 9:16 window that fits inside the source frame."""
    if iw * 16 <= ih * 9:
        cw, ch = iw, int(iw * 16 / 9)
    else:
        ch, cw = ih, int(ih * 9 / 16)
    return min(iw, cw - cw % 2), min(ih, ch - ch % 2)


def _sample_frames(path, start, duration, sw, sh, control=NOOP):
    """Decode the clip once, small and slow, straight into memory."""
    import numpy as np
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path),
           "-t", str(duration), "-an", "-sn",
           "-vf", f"fps={FACE_FPS},scale={sw}:{sh}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    control.attach(proc)
    size = sw * sh * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(sh, sw, 3)
    finally:
        proc.stdout.close()
        proc.wait()


def _mouth_patch(cv2, gray, f):
    """A fixed-size crop of the mouth, so two samples can be compared."""
    mx, my = (f[10] + f[12]) / 2, (f[11] + f[13]) / 2
    bw = max(8.0, abs(f[12] - f[10]) * 2.2, f[2] * 0.45)
    bh = max(6.0, bw * 0.6)
    x0, x1 = int(max(0, mx - bw / 2)), int(min(gray.shape[1], mx + bw / 2))
    y0, y1 = int(max(0, my - bh / 2)), int(min(gray.shape[0], my + bh / 2))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return cv2.resize(gray[y0:y1, x0:x1], (24, 16)).astype("float32")


def _collect_faces(cv2, path, start, duration, sw, sh, model, control, progress):
    """One row of detected faces per sample, with each mouth cut out."""
    det = cv2.FaceDetectorYN.create(str(model), "", (sw, sh), FACE_SCORE, 0.3, 5000)
    expected = max(1, int(duration * FACE_FPS))
    rows = []
    for i, img in enumerate(_sample_frames(path, start, duration, sw, sh, control)):
        control.check()
        _, faces = det.detect(img)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        row = []
        for f in (faces if faces is not None else []):
            w, h = float(f[2]), float(f[3])
            if w < sw * 0.04:                   # a face in the crowd, not a subject
                continue
            row.append({"cx": float(f[0]) + w / 2, "cy": float(f[1]) + h / 2,
                        "w": w, "h": h, "patch": _mouth_patch(cv2, gray, f),
                        "act": 0.0, "smooth": 0.0})
        rows.append(row)
        if progress and i % 5 == 0:
            progress(min(99.0, i / expected * 100))
    return rows


def _link_tracks(np, rows):
    """Follow each face from sample to sample and score its mouth movement."""
    tracks, seen = [], []
    for i, row in enumerate(rows):
        here, taken = {}, set()
        for d in sorted(row, key=lambda d: -d["w"]):
            best, best_gap = None, None
            for ti, tr in enumerate(tracks):
                if ti in taken or i - tr["last"] > FACE_FPS:
                    continue
                gap = abs(d["cx"] - tr["cx"]) + abs(d["cy"] - tr["cy"]) * 0.5
                if gap < max(d["w"], tr["w"]) * 0.8 and (best_gap is None
                                                         or gap < best_gap):
                    best, best_gap = ti, gap
            if best is None:
                tracks.append({"last": i, "cx": d["cx"], "cy": d["cy"],
                               "w": d["w"], "patch": None})
                best = len(tracks) - 1
            tr = tracks[best]
            if (tr["patch"] is not None and d["patch"] is not None
                    and i - tr["last"] <= 2):
                d["act"] = float(np.abs(d["patch"] - tr["patch"]).mean())
            tr.update(last=i, cx=d["cx"], cy=d["cy"], w=d["w"], patch=d["patch"])
            taken.add(best)
            here[best] = d
        seen.append(here)

    # Mouth movement over a single pair of samples is noise; over most of a
    # second it is speech.
    span = 5
    for i, here in enumerate(seen):
        for ti, d in here.items():
            vals = [seen[j][ti]["act"] for j in range(max(0, i - span + 1), i + 1)
                    if ti in seen[j]]
            d["smooth"] = sum(vals) / len(vals) if vals else 0.0
    return seen


def _pick_speaker(seen):
    """Which face holds the frame at each sample."""
    chosen, current, since, lead = [], None, -999, 0
    for i, here in enumerate(seen):
        if not here:
            chosen.append(None)
            continue
        if current not in here:
            current, since, lead = max(here, key=lambda t: here[t]["w"]), i, 0
        elif len(here) > 1:
            rival = max((t for t in here if t != current),
                        key=lambda t: here[t]["smooth"])
            quiet = here[current]["smooth"]
            lead = lead + 1 if here[rival]["smooth"] > quiet * SPEAKER_LEAD else 0
            if lead >= SPEAKER_SUSTAIN and (i - since) / FACE_FPS >= SPEAKER_HOLD:
                current, since, lead = rival, i, 0
        chosen.append(current)
    return chosen


def _smooth(targets, snaps, limit, reach):
    """Chase the target with a deadzone and a speed limit; snap on a cut."""
    cur, out = targets[0], []
    for i, want in enumerate(targets):
        if i in snaps or abs(want - cur) > TRACK_CUT * reach:
            cur = want
            snaps.add(i)
        elif abs(want - cur) > TRACK_DEADZONE * reach:
            step = (want - cur) * TRACK_CHASE
            cap = TRACK_MAX_STEP * reach
            cur += max(-cap, min(cap, step))
        out.append(min(max(cur, 0.0), limit))
    return out


def _keyframes(values, snaps, min_step):
    """Thin the per-sample track down to the points the crop actually turns."""
    keys = [(0.0, values[0])]
    last = len(values) - 1
    for i in range(1, len(values)):
        t, v = i / FACE_FPS, values[i]
        if i in snaps:
            keys.append((max(0.0, t - 0.5 / FACE_FPS), keys[-1][1]))
            keys.append((t, v))                 # two keys close together: a cut
        elif abs(v - keys[-1][1]) >= min_step:
            keys.append((t, v))
    if keys[-1][0] < last / FACE_FPS:
        keys.append((last / FACE_FPS, values[last]))
    return keys


_CROP_EVAL = None


def _crop_eval():
    """ffmpeg 8 evaluates a crop expression every frame and dropped the option
    that used to ask for it; older builds evaluate once unless asked. Passing
    an option a build does not have is fatal, so find out which one this is."""
    global _CROP_EVAL
    if _CROP_EVAL is None:
        proc = _run(["ffmpeg", "-hide_banner", "-h", "filter=crop"])
        _CROP_EVAL = (":eval=frame" if re.search(r"^\s+eval\s", proc.stdout or "",
                                                 re.M) else "")
    return _CROP_EVAL


def _ramp_expr(keys):
    """A time expression ffmpeg can evaluate per frame, ramping between keys."""
    if len(keys) < 2:
        return f"{keys[0][1]:.1f}"
    expr = f"{keys[-1][1]:.1f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(keys, keys[1:]))):
        if abs(v1 - v0) < 0.5 or t1 - t0 < 1e-4:
            seg = f"{v0:.1f}"
        else:
            seg = f"({v0:.1f}+({v1 - v0:.1f})*(t-{t0:.3f})/{t1 - t0:.4f})"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return expr


def _plan(values, snaps, reach):
    """Keyframes for one axis, thinned until the expression is a sane size."""
    step = TRACK_KEY_STEP * reach
    keys = _keyframes(values, snaps, step)
    while len(keys) > TRACK_MAX_KEYS and step < reach:
        step *= 1.8
        keys = _keyframes(values, snaps, step)
    return keys


def face_track(video_path, start, end, control=NOOP, progress=None):
    """Plan a 9:16 crop window that follows whoever is talking.

    Returns the crop size and a per-frame x and y expression, or None when
    there is nothing worth following, which is the caller's cue to fall back
    to the blurred fill.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    model = _face_model()
    if not model:
        return None

    iw, ih = video_size(video_path)
    if not iw or not ih:
        return None
    cw, ch = _crop_box(iw, ih)
    if cw >= iw and ch >= ih:
        return None                 # already 9:16, nothing to reframe

    sw = min(FACE_WIDTH, iw - iw % 2)
    sh = max(2, int(round(ih * sw / iw / 2)) * 2)
    rows = _collect_faces(cv2, video_path, start, end - start, sw, sh,
                          model, control, progress)
    if len(rows) < 3:
        return None

    seen = _link_tracks(np, rows)
    chosen = _pick_speaker(seen)
    found = [i for i, t in enumerate(chosen) if t is not None]
    if len(found) / len(chosen) < FACE_COVERAGE:
        return None

    # Hold the last known position through the frames with no face in them.
    scale = iw / sw
    xs, ys, ws, last = [], [], [], None
    for i, t in enumerate(chosen):
        if t is not None:
            d = seen[i][t]
            last = (d["cx"] * scale, d["cy"] * scale, d["w"] * scale)
        elif last is None:
            d = seen[found[0]][chosen[found[0]]]
            last = (d["cx"] * scale, d["cy"] * scale, d["w"] * scale)
        xs.append(last[0])
        ys.append(last[1])
        ws.append(last[2])

    snaps = {i for i in range(1, len(chosen))
             if chosen[i] is not None and chosen[i - 1] is not None
             and chosen[i] != chosen[i - 1]}

    # A face sitting dead centre looks like a passport photo, so leave more
    # room under the chin than over the head.
    left = _smooth([x - cw / 2 for x in xs], snaps, iw - cw, iw)
    top = _smooth([y - ch * 0.42 for y in ys], snaps, ih - ch, ih)

    # Where the face lands in the exported frame, which is what anything
    # burned on top has to stay out of the way of.
    out_scale = OUT_W / cw
    faces = [(i / FACE_FPS,
              (xs[i] - left[i]) * out_scale,
              ws[i] / 2 * out_scale)
             for i in range(len(xs))]

    return {
        "crop": (cw, ch),
        "x": _ramp_expr(_plan(left, snaps, iw)),
        "y": _ramp_expr(_plan(top, snaps, ih)),
        "coverage": len(found) / len(chosen),
        "faces": len({t for t in chosen if t is not None}),
        "cuts": len(snaps),
        "face_at": faces,
    }


def cut(video_path, start, end, out_path, vertical, blur_pad=True,
        progress=None, control=NOOP, subs=None, speaker_track=True, track=None):
    """Cut one clip. track is a face_track result, when the caller already
    has one: it is also what decides where the end card goes, so the caller
    works it out first and hands it over rather than paying for it twice."""
    duration = end - start

    detected = False
    if vertical and speaker_track and track is None:
        detected = True
        try:
            track = face_track(
                video_path, start, end, control,
                (lambda p: progress(p * TRACK_SHARE / 100)) if progress else None)
        except Cancelled:
            raise
        except Exception:               # noqa: BLE001 - never lose a clip to this
            track = None                # anything odd falls back to the fill

    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats",
           "-ss", str(start), "-i", str(video_path), "-t", str(duration)]

    caption = _ass_filter(subs) if subs else ""
    if vertical and track:
        cw, ch = track["crop"]
        vf = (f"crop=w={cw}:h={ch}:x='{track['x']}':y='{track['y']}'"
              # The crop is a slice of the source, so this is always an
              # upscale. Lanczos is the one that keeps an edge looking like
              # an edge at three or four times the size.
              f"{_crop_eval()},scale=1080:1920:flags=lanczos,setsar=1")
        if caption:
            vf += "," + caption
        cmd += ["-vf", vf]
    elif vertical:
        if blur_pad:
            # Blur a thumbnail and scale it up rather than blurring at full
            # size. Same look, and it does not pin a weak CPU for minutes.
            vf = ("[0:v]split=2[bg][fg];"
                  "[bg]scale=135:240:force_original_aspect_ratio=increase,"
                  "crop=135:240,gblur=sigma=4,"
                  "scale=1080:1920:flags=bilinear[bgb];"
                  "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
                  "[bgb][fgs]overlay=(W-w)/2:(H-h)/2")
            if caption:
                vf += f"[v];[v]{caption}"
            cmd += ["-filter_complex", vf]
        else:
            vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
                  "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black")
            if caption:
                vf += "," + caption
            cmd += ["-vf", vf]
    elif caption:
        cmd += ["-vf", caption]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(out_path)]

    # Detection already moved the bar, so the render fills what is left of it.
    base = TRACK_SHARE if (track and detected) else 0.0

    def on_line(line):
        if progress and line.startswith("out_time_us="):
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                return
            progress(min(99, base + done / duration * (100 - base)))

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
