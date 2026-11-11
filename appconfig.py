"""
Per-user settings: API keys, engine choices, clip preferences.

Lifted out of server.py unchanged, because the worker needs the same answers
and neither process should import the other. Still a single JSON file in the
home folder; per-user rows in the database are an auth-time problem, not a
queue-time one.
"""

import json
from pathlib import Path

import engine

CONFIG_FILE = Path.home() / ".viral_clipper.json"
DEFAULT_OUTPUT = Path.home() / "Desktop" / "viral_clips"


def load_config():
    base = {
        "provider": "anthropic", "keys": {},
        "transcriber": "groq", "whisper_model": "base",
        "groq_model": "whisper-large-v3-turbo", "clip_len": 60,
        "max_clips": 6, "cookies": "none", "cookies_file": "",
        "captions": "punch", "vertical": True,
        "blur_pad": True, "outdir": str(DEFAULT_OUTPUT),
        # Look of the export. speaker_track reframes onto whoever is talking
        # and falls back to blur_pad when it finds no face, word_captions
        # times the burn-in to each word, hook_card burns the scorer's
        # written hook over the opening.
        "speaker_track": True, "word_captions": True, "hook_card": True,
        # The end card. cta_text is the line above the tease, which is the
        # next clip's own hook, so the ask names what it is asking for.
        "cta": True, "cta_text": engine.CTA_LEAD,
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
