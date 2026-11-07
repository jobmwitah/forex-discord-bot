"""Discord embed formatting for calendar events."""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Sequence

import discord

from calendar_source import Event

IMPACT_EMOJI = {
    "high": "🔴",
    "medium": "🟠",
    "low": "🟡",
    "holiday": "⚪",
    "unknown": "⚫",
}

IMPACT_COLOR = {
    "high": 0xED4245,
    "medium": 0xE67E22,
    "low": 0xF1C40F,
    "holiday": 0x95A5A6,
    "unknown": 0x747F8D,
}

IMPACT_CHOICES = [
    discord.app_commands.Choice(name="High", value="high"),
    discord.app_commands.Choice(name="Medium", value="medium"),
    discord.app_commands.Choice(name="Low", value="low"),
    discord.app_commands.Choice(name="All (incl. holidays)", value="all"),
]

MAX_FIELDS_PER_EMBED = 20
MAX_EMBED_CHARS = 4200
MAX_MESSAGE_EMBED_CHARS = 5600
MAX_EMBEDS = 6


def impact_emoji(impact: str) -> str:
    return IMPACT_EMOJI.get(impact, IMPACT_EMOJI["unknown"])


def impact_color(events: Sequence[Event]) -> int:
    if not events:
        return IMPACT_COLOR["unknown"]
    strongest = max(events, key=lambda event: event.impact_rank)
    return IMPACT_COLOR.get(strongest.impact, IMPACT_COLOR["unknown"])


def _value_or_dash(value: str) -> str:
    cleaned = (value or "").strip()
    return cleaned if cleaned else "—"


def event_field(event: Event) -> tuple[str, str]:
    """Return a (name, value) pair for an embed field."""

    name = f"{impact_emoji(event.impact)} {event.currency} · {event.title}"[:256]
    value = (
        f"<t:{event.unix}:t> · <t:{event.unix}:R>\n"
        f"Forecast `{_value_or_dash(event.forecast)}` · "
        f"Previous `{_value_or_dash(event.previous)}`"
    )[:1024]
    return name, value


def build_event_embeds(
    title: str,
    events: Sequence[Event],
    *,
    footer: str = "ForexFactory economic calendar",
    max_embeds: int = MAX_EMBEDS,
) -> list[discord.Embed]:
    """Build one or more embeds, respecting Discord's size limits."""

    if not events:
        return []

    # Chunk events by field count *and* character budget so a single embed
    # cannot exceed Discord's 6000-character embed limit.
    chunks: list[list[tuple[Event, str, str]]] = []
    current: list[tuple[Event, str, str]] = []
    current_chars = 0
    for event in events:
        name, value = event_field(event)
        size = len(name) + len(value) + 40
        if current and (
            len(current) >= MAX_FIELDS_PER_EMBED
            or current_chars + size > MAX_EMBED_CHARS
        ):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append((event, name, value))
        current_chars += size
    if current:
        chunks.append(current)

    shown = min(len(chunks), max_embeds)

    embeds: list[discord.Embed] = []
    for index, chunk in enumerate(chunks[:shown]):
        embed = discord.Embed(
            title=title if index == 0 else f"{title} (cont.)",
            color=impact_color([item[0] for item in chunk]),
            timestamp=discord.utils.utcnow(),
        )
        for _event, name, value in chunk:
            embed.add_field(name=name, value=value, inline=False)

        truncated = len(chunks) > shown and index == shown - 1
        note = ""
        if truncated:
            note = (
                f" · showing first {sum(len(c) for c in chunks[:shown])} "
                f"of {len(events)}"
            )
        embed.set_footer(
            text=(
                f"{footer} · {len(events)} event(s){note} · "
                "times shown in your local timezone"
            )
        )
        embeds.append(embed)

    return embeds


def embed_char_count(embed: discord.Embed) -> int:
    total = len(embed.title or "") + len(embed.description or "")
    if embed.footer.text:
        total += len(embed.footer.text)
    if embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        total += len(field.name) + len(field.value)
    return total


def group_embeds(
    embeds: Sequence[discord.Embed],
    budget: int = MAX_MESSAGE_EMBED_CHARS,
) -> list[list[discord.Embed]]:
    """Group embeds so each message stays under Discord's 6000-char limit."""

    groups: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    current_chars = 0

    for embed in embeds:
        size = embed_char_count(embed)
        if current and current_chars + size > budget:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(embed)
        current_chars += size

    if current:
        groups.append(current)
    return groups


def alert_embed(
    event: Event, minutes_before: int, *, test: bool = False
) -> discord.Embed:
    when_text = (
        "now"
        if minutes_before <= 0
        else f"in {minutes_before} minute{'s' if minutes_before != 1 else ''}"
    )
    embed = discord.Embed(
        title=f"{impact_emoji(event.impact)} {event.currency} · {event.title}"[:256],
        description=(
            f"**{event.impact.title()} impact** event {when_text}\n"
            f"<t:{event.unix}:f> · <t:{event.unix}:R>"
        ),
        color=IMPACT_COLOR.get(event.impact, IMPACT_COLOR["unknown"]),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Forecast", value=f"`{_value_or_dash(event.forecast)}`", inline=True)
    embed.add_field(name="Previous", value=f"`{_value_or_dash(event.previous)}`", inline=True)
    embed.set_footer(
        text="ForexFactory economic calendar alert"
        + (" · TEST" if test else "")
    )
    return embed


SURPRISE_COLOR = {
    "better": 0x2ECC71,
    "worse": 0xED4245,
    "inline": 0x95A5A6,
    "unknown": 0x5865F2,
}

# Indicators where a lower reading is usually the stronger outcome for the
# currency. This is only a heuristic for the release summary.
_LOWER_IS_BETTER = (
    "unemployment",
    "jobless",
    "claims",
    "layoff",
    "bankrupt",
)

_METRIC_RE = re.compile(
    r"^\s*([+-]?\d[\d,]*(?:\.\d+)?)\s*([%KMBT]?)\s*$",
    re.IGNORECASE,
)

_METRIC_SCALE = {"": 1.0, "%": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _metric(value: str) -> tuple[float, str] | None:
    """Parse ``0.3%``, ``250K`` or ``-5.8B`` into (number, unit)."""

    match = _METRIC_RE.match(value or "")
    if match is None:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = match.group(2).upper()
    if unit not in _METRIC_SCALE:
        return None
    return number, unit


def summary_result(event: Event) -> tuple[str, str, int]:
    """Return (headline, explanation, colour) for a released event.

    Compares actual against forecast with a rule-of-thumb direction for the
    currency. It is deliberately not a trading recommendation.
    """

    actual = _metric(event.actual)
    forecast = _metric(event.forecast)

    if actual is None or forecast is None or actual[1] != forecast[1]:
        return (
            "Result published",
            "Compare the actual value with the forecast above. Direction "
            "depends on the market's positioning and reaction.",
            SURPRISE_COLOR["unknown"],
        )

    difference = actual[0] - forecast[0]
    if abs(difference) < 1e-9:
        return (
            "In line with expectations",
            "No meaningful surprise versus forecast. Watch the initial "
            "market reaction rather than the number alone.",
            SURPRISE_COLOR["inline"],
        )

    title = event.title.lower()
    lower_is_better = any(word in title for word in _LOWER_IS_BETTER)
    higher = difference > 0
    better = (not higher) if lower_is_better else higher

    direction = "above" if higher else "below"
    if better:
        return (
            "Better than expected",
            f"Actual came in {direction} forecast, which is typically "
            f"supportive for {event.currency}.",
            SURPRISE_COLOR["better"],
        )
    return (
        "Worse than expected",
        f"Actual came in {direction} forecast, which is typically a "
        f"headwind for {event.currency}.",
        SURPRISE_COLOR["worse"],
    )


def release_alert_embed(event: Event, *, test: bool = False) -> discord.Embed:
    """Embed sent at the exact moment an event is released."""

    embed = discord.Embed(
        title=(
            f"{impact_emoji(event.impact)} {event.currency} · "
            f"{event.title} — releasing now"
        )[:256],
        description=(
            f"**{event.impact.title()} impact** event is being released now\n"
            f"<t:{event.unix}:f> · <t:{event.unix}:R>"
        ),
        color=IMPACT_COLOR.get(event.impact, IMPACT_COLOR["unknown"]),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Forecast", value=f"`{_value_or_dash(event.forecast)}`", inline=True
    )
    embed.add_field(
        name="Previous", value=f"`{_value_or_dash(event.previous)}`", inline=True
    )
    embed.set_footer(
        text="ForexFactory economic calendar · release alert"
        + (" · TEST" if test else "")
    )
    return embed


def release_summary_embed(event: Event, *, test: bool = False) -> discord.Embed:
    """Post-release embed with the actual value and a brief impact summary."""

    headline, explanation, color = summary_result(event)
    embed = discord.Embed(
        title=(
            f"📊 {event.currency} · {event.title} — release summary"
        )[:256],
        description=f"**{headline}**\n{explanation}",
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Actual", value=f"`{_value_or_dash(event.actual)}`", inline=True
    )
    embed.add_field(
        name="Forecast", value=f"`{_value_or_dash(event.forecast)}`", inline=True
    )
    embed.add_field(
        name="Previous", value=f"`{_value_or_dash(event.previous)}`", inline=True
    )
    embed.add_field(
        name="Released",
        value=f"<t:{event.unix}:f> · <t:{event.unix}:R>",
        inline=False,
    )
    embed.set_footer(
        text="ForexFactory economic calendar · actual vs forecast heuristic"
        + (" · TEST" if test else "")
    )
    return embed


def day_title(day: date) -> str:
    return f"📅 Economic calendar — {day.strftime('%a, %b %d, %Y')}"


def display_currencies(currencies: Iterable[str]) -> str:
    codes = sorted(set(currencies))
    return ", ".join(codes) if codes else "All"


def display_impact(min_impact: str) -> str:
    return "All (incl. holidays)" if min_impact == "all" else min_impact.title()
