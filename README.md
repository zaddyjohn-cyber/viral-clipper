# Viral Clipper

Paste a video link. Get scored, ready to post shorts.

Reads every spoken word, rates each moment for viral potential, and cuts the
winners into vertical clips named by score.

Works with YouTube, Facebook, X, Instagram, TikTok, Reddit, Twitch and Vimeo,
or anything else yt-dlp supports.

---

## How it works

1. **Download** the source video with yt-dlp
2. **Transcribe** it with timestamps, via Groq or locally with faster-whisper
3. **Score** every candidate window with an LLM, rating hook strength, payoff
   and whether the clip stands alone
4. **Cut** the winners to 1080x1920 with a blurred background fill

---

## Install

```bash
pip install -r requirements.txt
```

You also need **ffmpeg** on your PATH:

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
apt install ffmpeg             # Debian / Ubuntu
```

## Run

```bash
python server.py
```

Opens at `http://127.0.0.1:8420`. On first run, open **Settings** and add a key
for whichever engines you want to use.

---

## Engines

Both stages are swappable, and keys are stored per provider so you can switch
back and forth without re-entering anything.

**Transcription** is the slow stage and decides how fast a run finishes.

| Engine | Speed | Cost |
|---|---|---|
| Groq | ~100x realtime | a few cents per hour of audio |
| faster-whisper | CPU bound, GPU used when present | free |

**Scoring** reads the transcript and rates each moment. The transcript is small,
so every option here costs fractions of a cent per video. Pick on judgment
quality, not price.

| Engine | Model |
|---|---|
| DeepSeek | `deepseek-chat` |
| Claude | `claude-haiku-4-5` |
| OpenAI | `gpt-4.1-mini` |

To add another, drop an entry in `PROVIDERS` or `TRANSCRIBERS` at the top of
`engine.py`. Anything speaking the OpenAI chat-completions shape needs only a
`base_url`.

---

## Videos that refuse to download

Some videos return `403 Forbidden` without a signed-in session, and YouTube
throttles anonymous downloads hard. Both are fixed the same way:

1. Install a "Get cookies.txt LOCALLY" browser extension
2. Open the site while signed in and export
3. Point **Settings > Or a cookies.txt file** at the saved path

A cookies.txt file is also the only option that works on a server with no
browser profile. Close your browser fully if you use the browser dropdown
instead, since it locks its own cookie database while running.

---

## Output

Clips are named by score so the best ones sort to the top:

```
91pct-how-i-built-a-40k-agency-the-hiring-mistake.mp4
84pct-how-i-built-a-40k-agency-first-client-in-9-days.mp4
clip-report.txt
```

`clip-report.txt` carries the timestamp, score, hook and reasoning for every
clip in the run.

---

## Running it on a server

```bash
CLIPPER_HOST=0.0.0.0 python server.py
```

Then reach it at `http://<machine-ip>:8420`.

Before exposing it beyond your own network, know what is missing:

- **No authentication.** Anyone who can reach the port can spend your API
  credits and read your clips.
- **Jobs live in memory.** A restart loses in-flight work and all history.
- **One job at a time.** ffmpeg pins a core, and local transcription pins the
  rest. Concurrent users queue behind each other with no feedback.

Those three are the gap between this and something you can charge for.

---

## Layout

```
server.py      FastAPI routes and job orchestration
engine.py      download, transcribe, score, cut. No web dependencies
static/        the dashboard
```

`engine.py` imports nothing from the web layer, so it can move behind a worker
queue when the single-process model runs out.
