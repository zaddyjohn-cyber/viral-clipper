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
CLIPPER_HOST=0.0.0.0 CLIPPER_INLINE_WORKER=0 python server.py
python worker.py                      # one or more, same box or another
```

Then reach it at `http://<machine-ip>:8420`.

One gap is left between this and something you can charge for:

- **No authentication.** Anyone who can reach the port can spend your API
  credits and read your clips. Every table that will need an owner already has
  a nullable `owner_id`, so adding it is a login screen and a filter, not a
  migration.

---

## State

Three pieces, each swappable, each with a default that needs nothing installed.

**Database.** Postgres is the target; `DATABASE_URL` unset falls back to a
SQLite file at `~/.viral_clipper/clipper.db` so a fresh clone runs. SQLite
takes one writer at a time, so more than one worker means Postgres.

```bash
DATABASE_URL=postgresql+psycopg://clipper:secret@localhost:5432/clipper
```

Migrations run themselves on startup. To drive them by hand:

```bash
python -m alembic upgrade head
python -m alembic revision --autogenerate -m "what changed"
```

Six tables, in the order work flows through them:

| Table | Holds |
|---|---|
| `jobs` | one source video, and the queue record a worker claims |
| `transcripts` | word-timed segments, kept for captions and for scoring feedback |
| `clips` | every scored window, and the render of it once exported |
| `accounts` | a connected channel, page or profile, with encrypted tokens |
| `posts` | one clip sent to one account, with its scheduled time and permalink |
| `metrics` | that post's numbers at 1h, 24h and 7d |

The last three are not driven by anything yet. They are here because posting
is the next layer, and because the point of collecting metrics is to feed real
outcomes back into scoring.

**Queue.** `python worker.py` claims the oldest queued job, writes progress to
its row, and heartbeats while it holds it. Miss the heartbeat window and the
job goes back to the queue, where the next worker resumes it from the
checkpoints `engine.py` already writes, so a killed process costs one ffmpeg
call rather than a whole run.

```bash
REDIS_URL=redis://localhost:6379/0
```

Redis is a doorbell, not the queue. With it a queued job starts in
milliseconds; without it workers poll the database every few seconds. Nothing
is lost either way, so Redis being down is a latency problem, never a data
one.

**Storage.** Rendered clips are written to your output folder as before, then
copied to object storage. Local disk by default; anything S3-shaped works.

```bash
CLIPPER_STORAGE=s3
CLIPPER_S3_BUCKET=clips
CLIPPER_S3_ENDPOINT=https://<account>.r2.cloudflarestorage.com
CLIPPER_S3_ACCESS_KEY=...
CLIPPER_S3_SECRET_KEY=...
CLIPPER_S3_PUBLIC_BASE=https://clips.example.com
```

`CLIPPER_S3_PUBLIC_BASE` matters more than it looks: Instagram will not take a
reel as bytes, it fetches a URL itself, so posting there later needs a public
base set. Without one, URLs are signed and expire after a day.

**Tokens.** Account access tokens are encrypted with Fernet before they are
stored, and writing one without a key set is an error rather than a silent
downgrade to plain text.

```bash
CLIPPER_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
```

`GET /api/health` reports what all of the above resolved to, how many jobs are
queued and which workers are alive.

---

## Layout

```
server.py      FastAPI routes. Validates, writes job rows, reads them back
worker.py      claims jobs and runs the engine against them
engine.py      download, transcribe, score, cut. No web dependencies
db.py          schema and sessions
jobqueue.py    claim, heartbeat, reap, cancel
storage.py     local disk or any S3-compatible bucket
crypto.py      encryption for stored account tokens
settings.py    deployment settings, all from the environment
appconfig.py   per-user settings: keys, engines, clip preferences
migrations/    alembic revisions
static/        the dashboard
```

`engine.py` imports nothing from the web layer or the database, which is what
let the pipeline move behind a worker queue without touching it.
