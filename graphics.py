"""Branded economic-calendar cards for social posts.

The cards are rendered locally with Pillow, so the numbers are always exact and
no AI image service is involved. One 1200x675 PNG/JPEG is produced per event and
attached to Bluesky directly and to X through a public image URL.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from calendar_source import Event
from formatting import summary_result

WIDTH = 1200
HEIGHT = 675

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_FONT_PATH = BASE_DIR / "assets" / "fonts" / "Roboto-Variable.ttf"

BG_TOP = (8, 13, 25)
BG_BOTTOM = (15, 23, 42)
PANEL = (18, 28, 47)
PANEL_BORDER = (36, 52, 80)
TEXT = (241, 245, 249)
MUTED = (148, 163, 184)
BLUE = (79, 124, 255)
BLUE_DARK = (37, 61, 133)
WHITE = (255, 255, 255)

IMPACT_COLORS = {
    "high": (239, 68, 68),
    "medium": (245, 158, 11),
    "low": (34, 197, 94),
    "holiday": (100, 116, 139),
}
IMPACT_LABELS = {
    "high": "HIGH IMPACT",
    "medium": "MEDIUM IMPACT",
    "low": "LOW IMPACT",
    "holiday": "HOLIDAY",
}


@dataclass(frozen=True)
class CardImage:
    """Rendered card plus the metadata Bluesky needs for its embed."""

    data: bytes
    mime: str
    width: int
    height: int
    alt: str


@lru_cache(maxsize=32)
def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    override = os.getenv("SOCIAL_FONT_PATH", "").strip()
    path = Path(override) if override else DEFAULT_FONT_PATH
    font = ImageFont.truetype(str(path), size)
    try:
        font.set_variation_by_name(weight)
    except (AttributeError, OSError, ValueError):
        pass
    return font


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=font)


def _ellipsize(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    suffix = "…"
    while text and _text_width(draw, text + suffix, font) > max_width:
        text = text[:-1].rstrip()
    return text + suffix


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    max_lines: int,
) -> tuple[list[str], bool]:
    """Wrap by pixel width, splitting over-long words. Returns (lines, clipped)."""

    words = text.split()
    lines: list[str] = []
    current = ""
    index = 0

    while index < len(words):
        word = words[index]
        candidate = f"{current} {word}".strip()
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
            index += 1
            continue

        if current:
            lines.append(current)
            current = ""
            if len(lines) >= max_lines:
                break
            continue

        # A single word that is wider than the line: split it by characters.
        chunk = ""
        for char in word:
            if _text_width(draw, chunk + char, font) <= max_width:
                chunk += char
            else:
                if chunk:
                    lines.append(chunk)
                chunk = char
                if len(lines) >= max_lines:
                    break
        current = chunk
        index += 1
        if len(lines) >= max_lines:
            break

    if current and len(lines) < max_lines:
        lines.append(current)

    clipped = index < len(words) or bool(current and len(lines) >= max_lines)
    if lines and clipped:
        lines[-1] = _ellipsize(draw, lines[-1], font, max_width)
    return lines, clipped


def _gradient_background() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG_TOP)
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        colour = tuple(
            int(start + (end - start) * ratio)
            for start, end in zip(BG_TOP, BG_BOTTOM)
        )
        draw.line([(0, y), (WIDTH, y)], fill=colour)
    return image


def _add_glow(image: Image.Image) -> Image.Image:
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    draw.ellipse([760, -260, 1500, 420], fill=(*BLUE, 44))
    draw.ellipse([-240, 420, 320, 900], fill=(37, 99, 235, 24))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    return Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")


def _pill(
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    text_fill: tuple[int, int, int] = WHITE,
    padding_x: int = 18,
    height: int = 44,
) -> int:
    width = int(_text_width(draw, text, font)) + padding_x * 2
    draw.rounded_rectangle(
        [left, top, left + width, top + height],
        radius=height // 2,
        fill=fill,
    )
    text_top = top + (height - (font.getbbox(text)[3] - font.getbbox(text)[1])) // 2 - 2
    draw.text((left + padding_x, text_top), text, font=font, fill=text_fill)
    return width


def _status_text(kind: str, minutes: int) -> str:
    if kind == "reminder":
        return f"IN {minutes} MIN"
    if kind == "release":
        return "RELEASED NOW"
    return "ACTUAL vs FORECAST"


def _status_color(kind: str, event: Event) -> tuple[int, int, int]:
    if kind == "release":
        return IMPACT_COLORS.get(event.impact, BLUE)
    if kind == "summary":
        return (34, 197, 94)
    return BLUE


def _alt_text(event: Event, kind: str, minutes: int, tz: ZoneInfo) -> str:
    when = event.when.astimezone(tz).strftime("%A %d %B %Y at %H:%M %Z")
    parts = [f"{event.currency} {event.title}", f"{event.impact} impact"]
    if kind == "reminder":
        parts.append(f"in {minutes} minutes")
    elif kind == "release":
        parts.append("released now")
    elif kind == "summary":
        parts.append(
            f"actual {event.actual or 'not published'} versus forecast "
            f"{event.forecast or 'not published'}"
        )
    parts.append(when)
    return ". ".join(parts)[:1000]


def _value_or_dash(value: str) -> str:
    cleaned = (value or "").strip()
    return cleaned if cleaned else "—"


def render_card(
    event: Event,
    kind: str,
    tz: ZoneInfo,
    *,
    minutes: int = 15,
    brand: str = "Economic Calendar",
    handle: str = "",
    test: bool = False,
) -> CardImage:
    """Render one event card and return the encoded image."""

    image = _gradient_background()
    image = _add_glow(image)
    draw = ImageDraw.Draw(image)

    # Header.
    brand_font = _font(22, "Bold")
    draw.rounded_rectangle([64, 54, 74, 64], radius=3, fill=BLUE)
    draw.text((86, 46), brand.upper(), font=brand_font, fill=BLUE)

    header_x = 86 + int(_text_width(draw, brand.upper(), brand_font)) + 18
    if test:
        _pill(draw, header_x, 42, "TEST", _font(16, "Bold"), (245, 158, 11), (15, 23, 42), 12, 34)

    impact_label = IMPACT_LABELS.get(event.impact, "IMPACT")
    impact_font = _font(18, "Bold")
    impact_width = int(_text_width(draw, impact_label, impact_font)) + 36
    draw.rounded_rectangle(
        [WIDTH - 64 - impact_width, 44, WIDTH - 64, 88],
        radius=22,
        fill=IMPACT_COLORS.get(event.impact, IMPACT_COLORS["holiday"]),
    )
    draw.text(
        (WIDTH - 64 - impact_width + 18, 54),
        impact_label,
        font=impact_font,
        fill=WHITE,
    )
    draw.line([(64, 118), (WIDTH - 64, 118)], fill=(30, 41, 59), width=2)

    # Currency badge.
    badge = [64, 158, 176, 270]
    draw.rounded_rectangle(badge, radius=26, fill=BLUE)
    currency_font = _font(40, "Black")
    currency_box = draw.textbbox((0, 0), event.currency, font=currency_font)
    currency_x = badge[0] + (badge[2] - badge[0] - (currency_box[2] - currency_box[0])) / 2
    currency_y = badge[1] + (badge[3] - badge[1] - (currency_box[3] - currency_box[1])) / 2 - 2
    draw.text((currency_x, currency_y), event.currency, font=currency_font, fill=WHITE)

    # Title: shrink until it fits in at most three lines.
    title_x = 210
    title_top = 156
    title_width = WIDTH - title_x - 64
    title = " ".join(event.title.split())
    title_lines: list[str] = []
    title_font = _font(56, "Bold")
    for size in (56, 52, 48, 44, 40, 36):
        candidate_font = _font(size, "Bold")
        lines, _clipped = _wrap(draw, title, candidate_font, title_width, 3)
        title_lines = lines
        title_font = candidate_font
        if len(lines) <= 3 and " ".join(lines) == title:
            break

    title_line_height = int(title_font.size * 1.12)
    for offset, line in enumerate(title_lines):
        draw.text(
            (title_x, title_top + offset * title_line_height),
            line,
            font=title_font,
            fill=TEXT,
        )

    title_bottom = title_top + max(1, len(title_lines)) * title_line_height
    time_top = max(310, title_bottom + 8)
    time_font = _font(26, "Regular")
    when = event.when.astimezone(tz).strftime("%a %d %b %Y · %H:%M %Z")
    draw.text((title_x, time_top), when, font=time_font, fill=MUTED)

    status_font = _font(18, "Bold")
    status = _status_text(kind, minutes)
    status_width = int(_text_width(draw, status, status_font)) + 32
    draw.rounded_rectangle(
        [WIDTH - 64 - status_width, time_top - 8, WIDTH - 64, time_top + 34],
        radius=21,
        fill=_status_color(kind, event),
    )
    draw.text(
        (WIDTH - 64 - status_width + 16, time_top - 1),
        status,
        font=status_font,
        fill=WHITE,
    )

    # Value cards.
    card_top = 440
    card_height = 118
    card_width = 340
    gap = 30
    labels = ("FORECAST", "PREVIOUS", "ACTUAL")
    values = (
        _value_or_dash(event.forecast),
        _value_or_dash(event.previous),
        _value_or_dash(event.actual),
    )

    actual_colour = TEXT
    if kind == "summary" and event.actual:
        _headline, _detail, colour = summary_result(event)
        actual_colour = (
            (colour >> 16) & 0xFF,
            (colour >> 8) & 0xFF,
            colour & 0xFF,
        )

    for index, (label, value) in enumerate(zip(labels, values)):
        left = 64 + index * (card_width + gap)
        draw.rounded_rectangle(
            [left, card_top, left + card_width, card_top + card_height],
            radius=20,
            fill=PANEL,
            outline=PANEL_BORDER,
            width=2,
        )
        draw.text((left + 24, card_top + 18), label, font=_font(17, "Bold"), fill=MUTED)
        value_font = _font(34, "Bold")
        value_colour = actual_colour if index == 2 else TEXT
        draw.text(
            (left + 24, card_top + 52),
            _ellipsize(draw, value, value_font, card_width - 48),
            font=value_font,
            fill=value_colour,
        )

    # Footer.
    draw.line([(64, 616), (WIDTH - 64, 616)], fill=(30, 41, 59), width=2)
    footer_font = _font(17, "Regular")
    footer_bold = _font(17, "Bold")
    draw.text((64, 634), brand, font=footer_bold, fill=TEXT)
    footer_x = 64 + int(_text_width(draw, brand, footer_bold)) + 12
    if handle:
        draw.text((footer_x, 634), handle, font=footer_font, fill=BLUE)

    disclaimer = "ForexFactory data · Not financial advice"
    disclaimer_width = int(_text_width(draw, disclaimer, footer_font))
    draw.text(
        (WIDTH - 64 - disclaimer_width, 634),
        disclaimer,
        font=footer_font,
        fill=MUTED,
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    mime = "image/png"

    if len(data) > 1_000_000:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90, optimize=True)
        data = buffer.getvalue()
        mime = "image/jpeg"

    return CardImage(
        data=data,
        mime=mime,
        width=WIDTH,
        height=HEIGHT,
        alt=_alt_text(event, kind, minutes, tz),
    )
