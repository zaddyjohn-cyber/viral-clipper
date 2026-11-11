"""
Draw the app icon: the bolt from the dashboard, on the app's purple.

    python make_icon.py

Writes static/icon.ico at the sizes Windows asks for. Run it again if the
colour or the mark ever changes; build.py picks the file up on its own.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "static" / "icon.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

PURPLE = (124, 92, 252, 255)
WHITE = (255, 255, 255, 255)

# The bolt, as a fraction of the icon so it scales to any size.
BOLT = [(0.56, 0.12), (0.30, 0.54), (0.46, 0.54), (0.40, 0.88),
        (0.68, 0.44), (0.52, 0.44)]


def draw(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(img)
    radius = max(2, round(size * 0.22))
    pen.rounded_rectangle([0, 0, size - 1, size - 1], radius, fill=PURPLE)
    pen.polygon([(x * size, y * size) for x, y in BOLT], fill=WHITE)
    return img


def main():
    base = draw(256)
    base.save(OUT, sizes=[(s, s) for s in SIZES])
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
