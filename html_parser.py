"""Minimal parser for ForexFactory's HTML calendar.

This is a trimmed-down port of the ``forex-pytory`` parser used by the
ForexFactoryScrapper project. It is kept in-tree so the bot does not depend on
``forex-pytory``, which pins ``pandas`` and therefore cannot be installed on
newer Pythons that have no pandas wheel (for example Python 3.14).

The parser returns naive datetimes: ForexFactory renders HTML times in the
requester's timezone, so the caller must apply the site's UTC offset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup

MONTH_ABBR = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}

_TIME_CLASS = "calendar__time"
_CURRENCY_CLASS = "calendar__currency"
_EVENT_CLASS = "calendar__event"
_FORECAST_CLASS = "calendar__forecast"
_ACTUAL_CLASS = "calendar__actual"
_PREVIOUS_CLASS = "calendar__previous"
_IMPACT_CLASS = "calendar__impact"

_COLUMN_ORDER = [
    _TIME_CLASS,
    _CURRENCY_CLASS,
    _EVENT_CLASS,
    _ACTUAL_CLASS,
    _FORECAST_CLASS,
    _PREVIOUS_CLASS,
    _IMPACT_CLASS,
]


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    """One event scraped from HTML, with a naive site-local timestamp."""

    event_id: str
    when: datetime
    currency: str
    title: str
    forecast: str = ""
    actual: str = ""
    previous: str = ""
    impact: str = "low"


def calendar_url(day: date) -> str:
    """Build the daily calendar URL, e.g. ``...?day=Nov6.2026``."""

    month = MONTH_ABBR[day.month]
    return f"https://www.forexfactory.com/calendar?day={month}{day.day}.{day.year}"


def _find_table(soup: BeautifulSoup):
    for selector in (
        "table.calendar__table",
        "table.calendar",
        "table[class*='calendar']",
        "div.calendar__table",
    ):
        table = soup.select_one(selector)
        if table is not None:
            return table

    for table in soup.find_all("table"):
        if table.find("td"):
            return table
    return None


def _cell(row_cells, class_name: str):
    """Find a cell by class, falling back to the known column order."""

    for cell in row_cells:
        if class_name in (cell.get("class") or []):
            return cell

    for cell in row_cells:
        descendant = cell.find(class_=class_name)
        if descendant is not None:
            return descendant

    try:
        index = _COLUMN_ORDER.index(class_name)
    except ValueError:
        return None
    if index < len(row_cells):
        return row_cells[index]
    return None


def _text(cell) -> str:
    if cell is None:
        return ""
    return cell.get_text(" ", strip=True) or ""


def _parse_time(text: str, fallback: datetime | None, day: date) -> datetime | None:
    """Parse a time cell such as ``8:30am``, ``3pm``, ``All Day`` or ``00:00``."""

    value = (text or "").strip().lower()
    if not value:
        return fallback

    if "day" in value:
        return datetime(day.year, day.month, day.day)

    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value)
    if match is None:
        return fallback

    try:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
    except ValueError:
        return fallback

    suffix = match.group(3)
    if suffix == "pm":
        hour = (hour % 12) + 12
    elif suffix == "am":
        hour = hour % 12

    if not (0 <= hour < 24 and 0 <= minute < 60):
        return fallback

    return datetime(day.year, day.month, day.day, hour, minute)


def _impact(cell) -> str:
    if cell is None:
        return "low"

    node = None
    for tag in cell.find_all(True):
        classes = " ".join(tag.get("class") or [])
        if re.search(r"impact|icon", classes, re.IGNORECASE):
            node = tag
            break
    if node is None:
        node = cell.find("img") or cell.find("span") or cell

    combined = " ".join(
        [
            " ".join(node.get("class") or []),
            str(node.get("src") or ""),
            str(node.get("alt") or ""),
            str(node.get("title") or ""),
            node.get_text(" ", strip=True),
        ]
    ).lower()

    if re.search(r"\bred\b|impact-red|impact.*red", combined):
        return "high"
    if re.search(r"\bora\b|\borange\b|impact-ora|impact.*orange", combined):
        return "medium"
    if re.search(r"\byel\b|\byellow\b|impact-yel|impact.*yellow", combined):
        return "low"
    if re.search(r"\bgry\b|\bgray\b|\bgrey\b", combined):
        return "holiday"
    return "low"


def parse_calendar_html(html: str, day: date) -> list[ParsedEvent]:
    """Parse a ForexFactory daily calendar page into events.

    ``day`` supplies the date for the rows (the page URL pins the day), so rows
    with an empty time inherit the previous row's time, matching ForexFactory's
    layout behaviour.
    """

    soup = BeautifulSoup(html, "html.parser")
    table = _find_table(soup)
    if table is None:
        raise ValueError("Calendar table not found in page")

    body = table.find("tbody") or table
    events: list[ParsedEvent] = []
    last_time: datetime | None = None

    for row in body.find_all("tr"):
        row_classes = " ".join(row.get("class") or [])
        if "head" in row_classes:
            continue

        cells = row.find_all("td")
        if not cells:
            continue

        title = _text(_cell(cells, _EVENT_CLASS))
        if not title or title.lower() in {"n/a", "tbd", "tba"}:
            continue

        parsed_time = _parse_time(
            _text(_cell(cells, _TIME_CLASS)), last_time, day
        )
        if parsed_time is not None:
            last_time = parsed_time
        else:
            parsed_time = datetime(day.year, day.month, day.day)

        currency = _text(_cell(cells, _CURRENCY_CLASS)).upper()
        if not currency:
            continue

        events.append(
            ParsedEvent(
                event_id=str(row.get("data-event-id") or ""),
                when=parsed_time,
                currency=currency,
                title=title,
                forecast=_text(_cell(cells, _FORECAST_CLASS)),
                actual=_text(_cell(cells, _ACTUAL_CLASS)),
                previous=_text(_cell(cells, _PREVIOUS_CLASS)),
                impact=_impact(_cell(cells, _IMPACT_CLASS)),
            )
        )

    return events
