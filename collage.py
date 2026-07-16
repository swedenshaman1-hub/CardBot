import subprocess
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

CARD_W, CARD_H = 300, 500
GAP = 20
COLS = 3
BADGE_SIZE = 72

OUTPUT_DIR = Path(__file__).parent / "data" / "spreads"


def _font(size: int) -> ImageFont.FreeTypeFont:
    # Try fc-match for Linux/Nix environments
    try:
        r = subprocess.run(
            ["fc-match", "--format=%{file}", "sans-serif:bold"],
            capture_output=True, text=True, timeout=3
        )
        if r.returncode == 0 and r.stdout.strip():
            return ImageFont.truetype(r.stdout.strip(), size)
    except Exception:
        pass
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/ariblk.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _default_back(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), (35, 15, 75))
    d = ImageDraw.Draw(img)
    d.rectangle([5, 5, w - 6, h - 6], outline=(140, 80, 220), width=3)
    d.rectangle([14, 14, w - 15, h - 15], outline=(100, 60, 180), width=1)
    for y in range(28, h - 18, 24):
        for x in range(28, w - 18, 24):
            d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(90, 50, 160))
    return img


def _load_image(url: str) -> Image.Image:
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content)).convert("RGB")


def build_collage(back_url: str | None, spread_id: int) -> str:
    """Build a 3×2 grid of face-down cards (rубашки) numbered 1-6."""
    if back_url:
        try:
            card_back = _load_image(back_url).resize((CARD_W, CARD_H), Image.LANCZOS)
        except Exception:
            card_back = _default_back(CARD_W, CARD_H)
    else:
        card_back = _default_back(CARD_W, CARD_H)

    count = 6
    rows = -(-count // COLS)
    width = COLS * CARD_W + (COLS + 1) * GAP
    height = rows * CARD_H + (rows + 1) * GAP

    canvas = Image.new("RGB", (width, height), (18, 8, 38))
    font = _font(BADGE_SIZE)

    for idx in range(count):
        col, row = idx % COLS, idx // COLS
        x = GAP + col * (CARD_W + GAP)
        y = GAP + row * (CARD_H + GAP)
        canvas.paste(card_back, (x, y))

        draw = ImageDraw.Draw(canvas)

        # Dark strip at card bottom for number
        draw.rectangle([x, y + CARD_H - 90, x + CARD_W, y + CARD_H], fill=(0, 0, 0))

        label = str(idx + 1)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (x + CARD_W / 2 - tw / 2 - bbox[0], y + CARD_H - 45 - th / 2 - bbox[1]),
            label,
            fill="white",
            font=font,
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"spread_{spread_id}.jpg"
    canvas.save(out_path, quality=90)
    return str(out_path)
