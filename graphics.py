"""Pipwire-branded economic-calendar cards for social posts.

The cards are rendered locally with Pillow, so the numbers are always exact and
no AI image service is involved. One 1200x675 PNG/JPEG is produced per event and
attached to Bluesky directly and to X through a public image URL.

Design follows the Pipwire palette and type rules: light-gray post background,
charcoal number badges, teal accents, soft red for bearish/high-impact labels,
Inter at 400-800, and all-caps titles with wide letter-spacing.
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
DEFAULT_FONT_PATH = BASE_DIR / "assets" / "fonts" / "Inter-Variable.ttf"

# Pipwire core palette.
POST_BG = (228, 228, 228)  # #E4E4E4 light-gray post background
PANEL = (246, 246, 247)  # #F6F6F7 card / chart panel white
FOOTER_PILL = (244, 244, 244)  # #F4F4F4
INK = (27, 27, 32)  # #1B1B20 main text / candle outlines
BODY = (58, 58, 66)  # #3A3A42 body text
MUTED = (154, 154, 162)  # #9A9AA2 hairlines / labels
MUTED_DARK = (91, 94, 107)  # #5B5E6B
CHARCOAL = (38, 38, 44)  # #26262C main dark background / number badges
TEAL = (15, 107, 91)  # #0F6B5B main accent / bullish
TEAL_BRIGHT = (15, 157, 127)  # #0F9D7F accent on dark backgrounds
RED = (184, 50, 63)  # #B8323F bearish / SL / manipulation
DARK_BANNER = (23, 23, 27)  # #17171B
LINE = (204, 204, 209)  # derived hairline on the light background
WHITE = (255, 255, 255)

IMPACT_COLORS = {
    "high": RED,
    "medium": MUTED_DARK,
    "low": TEAL,
    "holiday": MUTED,
}
IMPACT_LABELS = {
    "high": "HIGH IMPACT",
    "medium": "MEDIUM IMPACT",
    "low": "LOW IMPACT",
    "holiday": "HOLIDAY",
}

TITLE_TRACKING = 0.20  # em
LABEL_TRACKING = 0.20  # em


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


def _tracked_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    tracking: float,
) -> float:
    if not text:
        return 0.0
    width = sum(draw.textlength(char, font=font) for char in text)
    return width + tracking * (len(text) - 1)


def _draw_tracked(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    tracking: float,
) -> float:
    """Draw text one glyph at a time with the requested letter-spacing."""

    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + tracking
    return x


def _ellipsize(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    *,
    tracking: float = 0.0,
) -> str:
    if _tracked_width(draw, text, font, tracking) <= max_width:
        return text
    suffix = "…"
    while text and _tracked_width(draw, text + suffix, font, tracking) > max_width:
        text = text[:-1].rstrip()
    return text + suffix


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    max_lines: int,
    *,
    tracking: float = 0.0,
) -> tuple[list[str], bool]:
    """Wrap by pixel width, splitting over-long words. Returns (lines, clipped)."""

    words = text.split()
    lines: list[str] = []
    current = ""
    index = 0

    while index < len(words):
        word = words[index]
        candidate = f"{current} {word}".strip()
        if _tracked_width(draw, candidate, font, tracking) <= max_width:
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
            if _tracked_width(draw, chunk + char, font, tracking) <= max_width:
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
        lines[-1] = _ellipsize(
            draw, lines[-1], font, max_width, tracking=tracking
        )
    return lines, clipped


def _background() -> Image.Image:
    """Light-gray Pipwire canvas with a very soft teal corner wash."""

    image = Image.new("RGB", (WIDTH, HEIGHT), POST_BG)
    draw = ImageDraw.Draw(image)
    top = (235, 235, 237)
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        colour = tuple(
            int(start + (end - start) * ratio)
            for start, end in zip(top, POST_BG)
        )
        draw.line([(0, y), (WIDTH, y)], fill=colour)

    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse([700, -280, 1320, 360], fill=(*TEAL_BRIGHT, 18))
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
    tracking: float = 0.0,
) -> int:
    width = int(_tracked_width(draw, text, font, tracking)) + padding_x * 2
    draw.rounded_rectangle(
        [left, top, left + width, top + height],
        radius=height // 2,
        fill=fill,
    )
    text_top = top + (height - (font.getbbox(text)[3] - font.getbbox(text)[1])) // 2 - 2
    _draw_tracked(
        draw,
        (left + padding_x, text_top),
        text,
        font,
        text_fill,
        tracking,
    )
    return width


def _status_text(kind: str, minutes: int) -> str:
    if kind == "reminder":
        return f"IN {minutes} MIN"
    if kind == "release":
        return "RELEASED NOW"
    return "ACTUAL vs FORECAST"


def _status_color(kind: str, event: Event) -> tuple[int, int, int]:
    if kind == "release":
        return IMPACT_COLORS.get(event.impact, TEAL)
    if kind == "summary":
        return TEAL_BRIGHT
    return TEAL


def _actual_color(event: Event) -> tuple[int, int, int]:
    """Map the Discord summary colours onto the Pipwire palette."""

    _headline, _detail, colour = summary_result(event)
    if colour == 0x2ECC71:
        return TEAL_BRIGHT
    if colour == 0xED4245:
        return RED
    return MUTED_DARK


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


def _fit_title(
    draw: ImageDraw.ImageDraw,
    title: str,
    max_width: float,
    max_lines: int = 3,
) -> tuple[list[str], ImageFont.FreeTypeFont, int]:
    """Pick the largest Inter ExtraBold size whose tracked caps still fit."""

    chosen_lines: list[str] = []
    chosen_font = _font(30, "ExtraBold")
    chosen_tracking = 30 * TITLE_TRACKING
    for size in (54, 50, 46, 42, 38, 34, 31, 28):
        candidate = _font(size, "ExtraBold")
        tracking = size * TITLE_TRACKING
        lines, _clipped = _wrap(
            draw,
            title,
            candidate,
            max_width,
            max_lines,
            tracking=tracking,
        )
        chosen_lines = lines
        chosen_font = candidate
        chosen_tracking = tracking
        if len(lines) <= max_lines and " ".join(lines) == title:
            break
    return chosen_lines, chosen_font, int(chosen_tracking)


def render_card(
    event: Event,
    kind: str,
    tz: ZoneInfo,
    *,
    minutes: int = 15,
    brand: str = "Pipwire",
    handle: str = "",
    test: bool = False,
) -> CardImage:
    """Render one Pipwire event card and return the encoded image."""

    image = _background()
    draw = ImageDraw.Draw(image)

    # Dark banner with the wordmark and the small teal brand label.
    draw.rectangle([0, 0, WIDTH, 100], fill=DARK_BANNER)
    draw.rectangle([0, 100, WIDTH, 103], fill=TEAL_BRIGHT)

    brand_text = brand.upper()
    draw.rounded_rectangle([64, 28, 76, 40], radius=2, fill=TEAL_BRIGHT)
    brand_font = _font(26, "ExtraBold")
    brand_width = _draw_tracked(
        draw, (88, 22), brand_text, brand_font, WHITE, 3.0
    )
    draw.text(
        (88, 57),
        "ECONOMIC CALENDAR",
        font=_font(12, "SemiBold"),
        fill=TEAL_BRIGHT,
    )
    if test:
        _pill(
            draw,
            int(brand_width) + 18,
            30,
            "TEST",
            _font(12, "Bold"),
            RED,
            WHITE,
            12,
            28,
            1.2,
        )

    impact_label = IMPACT_LABELS.get(event.impact, "IMPACT")
    impact_font = _font(13, "Bold")
    impact_tracking = 13 * 0.14
    impact_width = int(_tracked_width(draw, impact_label, impact_font, impact_tracking)) + 36
    impact_fill = IMPACT_COLORS.get(event.impact, IMPACT_COLORS["holiday"])
    impact_text = DARK_BANNER if event.impact == "holiday" else WHITE
    draw.rounded_rectangle(
        [WIDTH - 64 - impact_width, 30, WIDTH - 64, 70],
        radius=20,
        fill=impact_fill,
    )
    _draw_tracked(
        draw,
        (WIDTH - 64 - impact_width + 18, 41),
        impact_label,
        impact_font,
        impact_text,
        impact_tracking,
    )

    # Charcoal currency badge with a small bright-teal base accent.
    badge = [64, 150, 168, 254]
    draw.rounded_rectangle(badge, radius=26, fill=CHARCOAL)
    draw.rounded_rectangle([98, 242, 134, 247], radius=2, fill=TEAL_BRIGHT)
    currency_font = _font(40, "ExtraBold")
    currency_box = draw.textbbox((0, 0), event.currency, font=currency_font)
    currency_x = badge[0] + (badge[2] - badge[0] - (currency_box[2] - currency_box[0])) / 2
    currency_y = badge[1] + (badge[3] - badge[1] - (currency_box[3] - currency_box[1])) / 2 - 2
    draw.text((currency_x, currency_y), event.currency, font=currency_font, fill=WHITE)

    # Teal small-caps eyebrow above the all-caps title.
    eyebrow = f"{impact_label} · {event.currency}"
    eyebrow_font = _font(12, "Bold")
    _draw_tracked(
        draw,
        (200, 148),
        eyebrow,
        eyebrow_font,
        TEAL,
        eyebrow_font.size * LABEL_TRACKING,
    )

    title_x = 200
    title_top = 176
    title_width = WIDTH - title_x - 64
    title = " ".join(event.title.split()).upper()
    title_lines, title_font, title_tracking = _fit_title(
        draw, title, title_width, 3
    )
    title_line_height = int(title_font.size * 1.12)
    for offset, line in enumerate(title_lines):
        _draw_tracked(
            draw,
            (title_x, title_top + offset * title_line_height),
            line,
            title_font,
            INK,
            title_tracking,
        )

    title_bottom = title_top + max(1, len(title_lines)) * title_line_height
    time_top = max(358, title_bottom + 14)
    time_font = _font(19, "Medium")
    when = event.when.astimezone(tz).strftime("%a %d %b %Y · %H:%M %Z")
    draw.text((title_x, time_top), when, font=time_font, fill=BODY)

    status_font = _font(13, "Bold")
    status = _status_text(kind, minutes)
    status_tracking = 13 * 0.12
    status_width = int(_tracked_width(draw, status, status_font, status_tracking)) + 36
    status_fill = _status_color(kind, event)
    status_text = DARK_BANNER if status_fill == MUTED else WHITE
    draw.rounded_rectangle(
        [WIDTH - 64 - status_width, time_top - 10, WIDTH - 64, time_top + 30],
        radius=20,
        fill=status_fill,
    )
    _draw_tracked(
        draw,
        (WIDTH - 64 - status_width + 18, time_top - 1),
        status,
        status_font,
        status_text,
        status_tracking,
    )

    # Three panel-white value cards with charcoal number badges.
    card_top = 432
    card_height = 132
    card_width = 340
    gap = 30
    labels = ("FORECAST", "PREVIOUS", "ACTUAL")
    values = (
        _value_or_dash(event.forecast),
        _value_or_dash(event.previous),
        _value_or_dash(event.actual),
    )
    actual_colour = _actual_color(event) if kind == "summary" and event.actual else CHARCOAL

    for index, (label, value) in enumerate(zip(labels, values)):
        left = 64 + index * (card_width + gap)
        accent = actual_colour if index == 2 and kind == "summary" and event.actual else LINE
        draw.rounded_rectangle(
            [left, card_top, left + card_width, card_top + card_height],
            radius=20,
            fill=PANEL,
            outline=accent,
            width=2,
        )
        _draw_tracked(
            draw,
            (left + 24, card_top + 20),
            label,
            _font(12, "SemiBold"),
            MUTED_DARK,
            12 * LABEL_TRACKING,
        )

        value_font = _font(28, "Bold")
        shown = _ellipsize(
            draw, value, value_font, card_width - 76, tracking=0.4
        )
        badge_width = int(_tracked_width(draw, shown, value_font, 0.4)) + 36
        badge_width = min(badge_width, card_width - 48)
        badge_height = 54
        badge_fill = (
            actual_colour
            if index == 2 and kind == "summary" and event.actual
            else CHARCOAL
        )
        draw.rounded_rectangle(
            [
                left + 24,
                card_top + 58,
                left + 24 + badge_width,
                card_top + 58 + badge_height,
            ],
            radius=15,
            fill=badge_fill,
        )
        badge_box = draw.textbbox((0, 0), shown, font=value_font)
        badge_text_y = (
            card_top + 58 + (badge_height - (badge_box[3] - badge_box[1])) / 2 - 3
        )
        _draw_tracked(
            draw,
            (left + 42, badge_text_y),
            shown,
            value_font,
            WHITE,
            0.4,
        )

    # Footer pill and hairline.
    draw.line([(64, 606), (WIDTH - 64, 606)], fill=LINE, width=1)
    footer_font = _font(14, "ExtraBold")
    footer_brand = brand.upper()
    footer_tracking = 14 * 0.12
    footer_width = int(_tracked_width(draw, footer_brand, footer_font, footer_tracking))
    handle_font = _font(14, "Medium")
    handle_width = (
        int(_text_width(draw, handle, handle_font)) + 14 if handle else 0
    )
    pill_width = footer_width + handle_width + 36
    draw.rounded_rectangle(
        [64, 618, 64 + pill_width, 650],
        radius=16,
        fill=FOOTER_PILL,
        outline=LINE,
        width=1,
    )
    _draw_tracked(
        draw,
        (82, 626),
        footer_brand,
        footer_font,
        INK,
        footer_tracking,
    )
    if handle:
        draw.text(
            (82 + footer_width + 14, 626),
            handle,
            font=handle_font,
            fill=TEAL,
        )

    disclaimer = "ForexFactory data · Not financial advice"
    disclaimer_font = _font(12, "Regular")
    disclaimer_width = int(_text_width(draw, disclaimer, disclaimer_font))
    draw.text(
        (WIDTH - 64 - disclaimer_width, 628),
        disclaimer,
        font=disclaimer_font,
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
