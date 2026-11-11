"""
Build Zaram Viral Klipa into a standalone Windows app.

    python build.py

Produces dist/Zaram Viral Klipa.exe: one file, no Python installed, no
terminal window. Copy it anywhere and double-click it.

One file costs a few seconds on every launch, because Windows unpacks the
whole app to a temp folder before it runs. That is the trade for having
something you can move around on its own.

Two things stay outside the build on purpose:

  ffmpeg and yt-dlp are separate programs the app shells out to, so they
  still have to be on PATH. Bundling them would double the size and go
  stale the moment YouTube changes something, and yt-dlp needs updating
  far more often than this app does.

  faster-whisper is excluded. It drags in torch and ctranslate2, which is
  well over a gigabyte for a transcriber that only matters if you want to
  run offline. The built app transcribes with Groq. Run from source if you
  want the local engine.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Zaram Viral Klipa"
ICON = ROOT / "static" / "icon.ico"

# Data the app reads at runtime by path rather than importing, so PyInstaller
# cannot find it on its own. Each one lands beside the frozen modules, which
# is where Path(__file__).parent points to once bundled.
DATA = [
    ("static", "static"),
    ("migrations", "migrations"),     # alembic reads these as files
    ("models", "models"),             # the face detector weights
    ("alembic.ini", "."),
]

# Imported by name at runtime, so nothing references them where the
# bundler can see it.
HIDDEN = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

# Big, and not used by the built app.
EXCLUDE = [
    "torch", "faster_whisper", "ctranslate2", "onnxruntime",
    "matplotlib", "tkinter", "PySide6", "PyQt5", "PyQt6", "notebook",
]


def main():
    missing = [d for d, _ in DATA if not (ROOT / d).exists()]
    if missing:
        print("Cannot build, these are not in the project folder: "
              + ", ".join(missing))
        return 1

    for stale in (ROOT / "build", ROOT / "dist"):
        shutil.rmtree(stale, ignore_errors=True)

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--name", NAME, "--windowed", "--onefile"]
    if ICON.exists():
        cmd += ["--icon", str(ICON)]
    else:
        print("No icon yet. Run: python make_icon.py")
    for src, dest in DATA:
        cmd += ["--add-data", f"{ROOT / src}{os_sep()}{dest}"]
    for mod in HIDDEN:
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDE:
        cmd += ["--exclude-module", mod]
    cmd += ["--collect-submodules", "alembic"]
    cmd.append(str(ROOT / "desktop.py"))

    print("Building. This takes a few minutes.\n")
    code = subprocess.call(cmd, cwd=str(ROOT))
    if code != 0:
        print("\nBuild failed. The PyInstaller output above says why.")
        return code

    exe = ROOT / "dist" / f"{NAME}.exe"
    if not exe.exists():
        print("\nBuild finished but the exe is not where it was expected.")
        return 1
    print(f"\nBuilt {exe}  ({exe.stat().st_size / 1_048_576:.0f} MB)")
    print("ffmpeg and yt-dlp still have to be on PATH for it to work.")
    return 0


def os_sep():
    """PyInstaller splits --add-data on ; for Windows and : everywhere else."""
    return ";" if sys.platform == "win32" else ":"


if __name__ == "__main__":
    raise SystemExit(main())
