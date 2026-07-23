import subprocess
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

CARD_W, CARD_H = 300, 450
# Cards slightly overlap so the dark outer edge of the photographed card
# cannot form a visible stripe between neighboring cards.
GAP = -2
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


def _fit_card(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resize and crop a card to fill its frame without photo margins."""
    target_w, target_h = size

    # The uploaded back is a photo of a card, so it may contain a dark
    # background around the physical card. Remove only the outer dark side
    # margins; otherwise those margins become black stripes in the collage.
    gray = image.convert("L")
    sample_top = round(image.height * 0.08)
    sample_bottom = round(image.height * 0.92)
    column_means = [
        sum(gray.getpixel((x, y)) for y in range(sample_top, sample_bottom, 8))
        / max(1, len(range(sample_top, sample_bottom, 8)))
        for x in range(image.width)
    ]
    # The card artwork is substantially brighter than the dark studio
    # background, so this threshold trims the photographed side margins.
    threshold = 35
    active = [x for x, mean in enumerate(column_means) if mean > threshold]
    if active:
        left = max(0, active[0] - 2)
        right = min(image.width, active[-1] + 3)
        if right - left >= image.width * 0.7:
            image = image.crop((left, 0, right, image.height))

    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_size = (round(src_w * scale), round(src_h * scale))
    resized = image.resize(new_size, Image.LANCZOS)
    left = max(0, (resized.width - target_w) // 2)
    top = max(0, (resized.height - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))


def build_collage(back_url: str | None, spread_id: int) -> str:
    """Build a 3×2 grid of face-down cards (rубашки) numbered 1-6."""
    if back_url:
        try:
            card_back = _fit_card(_load_image(back_url), (CARD_W, CARD_H))
        except Exception:
            card_back = _default_back(CARD_W, CARD_H)
    else:
        card_back = _default_back(CARD_W, CARD_H)

    count = 6
    rows = -(-count // COLS)
    width = COLS * CARD_W + (COLS - 1) * GAP
    height = rows * CARD_H + (rows - 1) * GAP

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    font = _font(BADGE_SIZE)

    for idx in range(count):
        col, row = idx % COLS, idx // COLS
        x = col * (CARD_W + GAP)
        y = row * (CARD_H + GAP)
        canvas.paste(card_back, (x, y))

        draw = ImageDraw.Draw(canvas)

        label = str(idx + 1)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        badge = 80
        badge_x = x + CARD_W // 2
        badge_y = y + 76
        draw.ellipse(
            [
                badge_x - badge // 2,
                badge_y - badge // 2,
                badge_x + badge // 2,
                badge_y + badge // 2,
            ],
            fill=(0, 0, 0),
            outline=(255, 255, 255),
            width=4,
        )
        draw.text(
            (badge_x - tw / 2 - bbox[0], badge_y - th / 2 - bbox[1]),
            label,
            fill="white",
            font=font,
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"spread_{spread_id}.jpg"
    canvas.save(out_path, quality=90)
    return str(out_path)
