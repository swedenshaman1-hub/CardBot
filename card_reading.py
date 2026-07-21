"""Render an original card and its description as one Telegram-friendly image."""

from io import BytesIO
import os
from pathlib import Path
import tempfile

import requests
from PIL import Image, ImageDraw, ImageFont


def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = words.pop(0)
        for word in words:
            candidate = f"{line} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def build_card_reading(image_url: str, meaning: str, title: str = "Расшифровка") -> str:
    """Return one JPG: untouched card artwork above a text panel of exactly its width."""
    response = requests.get(image_url, timeout=30)
    response.raise_for_status()
    card = Image.open(BytesIO(response.content)).convert("RGB")

    width = card.width
    margin = max(42, width // 18)
    body_font = _font(max(25, width // 28))
    title_font = _font(max(24, width // 32), bold=True)
    line_gap = max(12, width // 65)

    measure = Image.new("RGB", (width, 100), "white")
    measure_draw = ImageDraw.Draw(measure)
    lines = _wrap(measure_draw, meaning.strip(), body_font, width - margin * 2)
    line_height = int(body_font.getbbox("Ag")[3] * 1.35)
    title_height = int(title_font.getbbox(title)[3] * 1.3)
    panel_height = margin + 8 + title_height + 26 + len(lines) * line_height + max(0, len(lines) - 1) * line_gap + margin

    canvas = Image.new("RGB", (width, card.height + panel_height), "#17130f")
    canvas.paste(card, (0, 0))
    draw = ImageDraw.Draw(canvas)
    top = card.height
    draw.rectangle((0, top, width, top + panel_height), fill="#17130f")
    draw.line((margin, top + margin, width - margin, top + margin), fill="#C49352", width=max(2, width // 450))
    y = top + margin + 24
    draw.text((margin, y), title, fill="#D9B67A", font=title_font)
    y += int(title_font.getbbox(title)[3] * 1.5) + 20
    for line in lines:
        draw.text((margin, y), line, fill="#F7F1E7", font=body_font)
        y += line_height + line_gap

    handle, output = tempfile.mkstemp(suffix=".jpg")
    os.close(handle)
    Path(output).unlink(missing_ok=True)
    canvas.save(output, format="JPEG", quality=95, subsampling=0)
    return output
