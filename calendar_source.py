"""Economic calendar data sources for the Discord bot.

Primary source: ForexFactory's official weekly JSON feed. Each entry carries an
explicit UTC offset, so scheduling reminders is unambiguous. The feed only
covers the current week and is occasionally rate-limited, so results are cached
in memory and on disk.

Fallback source: the HTML calendar, parsed by the small in-tree parser in
``html_parser.py`` (a trimmed port of the ForexFactoryScrapper repo's
``forex-pytory`` parser). ForexFactory renders HTML timestamps in the
requester's timezone, so the fallback derives the site's current UTC offset from
the "now" clock in the calendar header (``calendar__header-time``) and applies
it to parsed events. This is used for dates outside the JSON feed's coverage or
when the feed is unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo

import requests

from config import DATA_DIR

log = logging.getLogger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

IMPACT_RANK = {"holiday": 0, "low": 1, "medium": 2, "high": 3}

# Currencies that show up on ForexFactory's forex calendar. Used to catch typos
# in pair/currency filters. The list is deliberately permissive.
KNOWN_CURRENCIES = {
    "AED", "ARS", "AUD", "BDT", "BGN", "BHD", "BRL", "CAD", "CHF", "CLP",
    "CNY", "COP", "CRC", "CZK", "DKK", "DOP", "EGP", "EUR", "GBP", "GHS",
    "GTQ", "HKD", "HUF", "IDR", "ILS", "INR", "ISK", "JOD", "JPY", "KES",
    "KRW", "KWD", "KZT", "LKR", "MAD", "MXN", "MYR", "NGN", "NOK", "NZD",
    "OMR", "PEN", "PHP", "PKR", "PLN", "QAR", "RON", "RSD", "RUB", "SAR",
    "SEK", "SGD", "THB", "TRY", "TWD", "UAH", "USD", "UYU", "VND", "ZAR",
}

_ET = ZoneInfo("America/New_York")
_FEED_CACHE_PATH = DATA_DIR / "feed_cache.json"
_HTML_CACHE_TTL = 6 * 3600  # seconds
_HTML_MIN_INTERVAL = 2.0  # be polite between HTML page fetches


class FeedError(RuntimeError):
    """Base error for calendar feed failures."""


class RateLimited(FeedError):
    def __init__(self, retry_after: float = 60.0) -> None:
        super().__init__(
            f"ForexFactory rate-limited the calendar feed "
            f"(retry in about {int(retry_after)}s)"
        )
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class Event:
    """A single economic calendar event, normalised to UTC."""

    id: str
    title: str
    currency: str
    when: datetime  # timezone-aware, UTC
    impact: str
    forecast: str = ""
    previous: str = ""
    actual: str = ""

    @property
    def impact_rank(self) -> int:
        return IMPACT_RANK.get(self.impact, 1)

    @property
    def unix(self) -> int:
        return int(self.when.timestamp())

    @property
    def dedupe_key(self) -> str:
        return f"{self.currency}|{self.title}|{self.when:%Y-%m-%dT%H:%M}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "currency": self.currency,
            "when": self.when.isoformat(),
            "impact": self.impact,
            "forecast": self.forecast,
            "previous": self.previous,
            "actual": self.actual,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Event | None":
        try:
            when = datetime.fromisoformat(str(raw["when"]))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return cls(
                id=str(raw.get("id") or ""),
                title=str(raw.get("title") or ""),
                currency=str(raw.get("currency") or "").upper(),
                when=when.astimezone(timezone.utc),
                impact=str(raw.get("impact") or "low").lower(),
                forecast=str(raw.get("forecast") or ""),
                previous=str(raw.get("previous") or ""),
                actual=str(raw.get("actual") or ""),
            )
        except Exception:
            return None


def _clean(value: object, limit: int = 512) -> str:
    return str(value or "").strip()[:limit]


def _normalise_feed_event(raw: dict) -> Event | None:
    title = _clean(raw.get("title"), 300)
    currency = _clean(raw.get("country"), 8).upper()
    if not title or not currency:
        return None

    date_raw = _clean(raw.get("date"), 64)
    if not date_raw:
        return None

    try:
        when = datetime.fromisoformat(date_raw)
    except ValueError:
        log.warning("Skipping event with unparseable date: %r", date_raw)
        return None

    # The feed always includes an offset; be defensive for naive timestamps.
    if when.tzinfo is None:
        when = when.replace(tzinfo=_ET)
    when = when.astimezone(timezone.utc)

    impact = _clean(raw.get("impact"), 20).lower() or "low"
    if impact not in IMPACT_RANK:
        impact = "low"

    digest = hashlib.sha1(
        f"{currency}|{title}|{date_raw}".encode("utf-8")
    ).hexdigest()[:16]

    return Event(
        id=digest,
        title=title,
        currency=currency,
        when=when,
        impact=impact,
        forecast=_clean(raw.get("forecast"), 80),
        previous=_clean(raw.get("previous"), 80),
    )


def fetch_events_sync(timeout: float = 25.0) -> list[Event]:
    """Fetch the current week's JSON calendar. Raises FeedError on failure."""

    session = requests.Session()
    session.headers.update(_HTTP_HEADERS)

    try:
        response = session.get(FEED_URL, timeout=timeout)
    except requests.RequestException as exc:
        raise FeedError(f"Could not reach the calendar feed: {exc}") from exc

    if response.status_code == 429:
        try:
            retry_after = float(response.headers.get("Retry-After", "60"))
        except ValueError:
            retry_after = 60.0
        raise RateLimited(retry_after)

    try:
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise FeedError(f"Calendar feed returned invalid data: {exc}") from exc

    if not isinstance(payload, list):
        raise FeedError("Calendar feed did not return a JSON list")

    events: dict[str, Event] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        event = _normalise_feed_event(item)
        if event is not None:
            events[event.id] = event

    if not events:
        raise FeedError("Calendar feed contained no events")

    return sorted(events.values(), key=lambda event: event.when)


# --------------------------------------------------------------------------- #
# HTML fallback (uses the same parser as the ForexFactoryScrapper repo)
# --------------------------------------------------------------------------- #


_header_time_re = re.compile(
    r"calendar__header-time[^>]*>\s*([0-9]{1,2}:[0-9]{2}\s*[ap]m)\s*<",
    re.IGNORECASE,
)


def _site_offset_minutes(html: str) -> int:
    """Derive the page's UTC offset from the live clock in the header."""

    match = _header_time_re.search(html)
    if not match:
        return _last_site_offset()

    raw = re.sub(r"\s+", "", match.group(1)).lower()
    try:
        displayed = datetime.strptime(raw, "%I:%M%p")
    except ValueError:
        return _last_site_offset()

    now = datetime.now(timezone.utc)
    difference = (
        displayed.hour * 60
        + displayed.minute
        - (now.hour * 60 + now.minute)
    )
    # Wrap into [-12h, +14h] to handle crossing midnight.
    while difference > 14 * 60:
        difference -= 24 * 60
    while difference < -12 * 60:
        difference += 24 * 60
    # Real offsets are multiples of 15 minutes.
    offset = int(round(difference / 15.0) * 15)
    _remember_site_offset(offset)
    return offset


_last_offset_minutes = 0
_offset_lock = threading.Lock()


def _last_site_offset() -> int:
    with _offset_lock:
        return _last_offset_minutes


def _remember_site_offset(offset: int) -> None:
    global _last_offset_minutes
    with _offset_lock:
        _last_offset_minutes = offset


_html_cache: dict[date, tuple[float, list[Event]]] = {}
_html_lock = threading.Lock()
_last_html_fetch = 0.0


def _http_get_html(url: str, timeout: float = 25.0) -> str:
    from curl_cffi import requests as curl_requests

    response = curl_requests.get(url, impersonate="chrome", timeout=timeout)
    response.raise_for_status()
    return response.text


def scrape_day_sync(
    day: date, timeout: float = 25.0, cache_ttl: float | None = None
) -> list[Event]:
    """Scrape one calendar day via HTML.

    The default cache is six hours. Callers that need fresher data (for example
    reading a released actual value) can pass a small ``cache_ttl``.
    """

    ttl = _HTML_CACHE_TTL if cache_ttl is None else max(0.0, float(cache_ttl))

    cached = _html_cache.get(day)
    if cached and (time.monotonic() - cached[0]) < ttl:
        return list(cached[1])

    global _last_html_fetch
    with _html_lock:
        cached = _html_cache.get(day)
        if cached and (time.monotonic() - cached[0]) < ttl:
            return list(cached[1])

        wait = _HTML_MIN_INTERVAL - (time.monotonic() - _last_html_fetch)
        if wait > 0:
            time.sleep(wait)

        from html_parser import calendar_url, parse_calendar_html

        url = calendar_url(day)
        try:
            html = _http_get_html(url, timeout=timeout)
            offset_minutes = _site_offset_minutes(html)
            records = parse_calendar_html(html, day)
        finally:
            _last_html_fetch = time.monotonic()

    site_tz = timezone(timedelta(minutes=offset_minutes))
    events: list[Event] = []

    for record in records:
        when = record.when.replace(tzinfo=site_tz).astimezone(timezone.utc)
        title = _clean(record.title, 300)
        currency = _clean(record.currency, 8).upper()
        if not title or not currency:
            continue

        impact = _clean(record.impact, 20).lower()
        if impact not in IMPACT_RANK:
            impact = "low"

        digest = _clean(record.event_id, 40) or hashlib.sha1(
            f"{currency}|{title}|{when.isoformat()}".encode("utf-8")
        ).hexdigest()[:16]

        events.append(
            Event(
                id=digest,
                title=title,
                currency=currency,
                when=when,
                impact=impact,
                forecast=_clean(record.forecast, 80),
                previous=_clean(record.previous, 80),
                actual=_clean(record.actual, 80),
            )
        )

    with _html_lock:
        _html_cache[day] = (time.monotonic(), list(events))
    return events


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #


def parse_pair(raw: str) -> set[str]:
    """Turn ``EURUSD`` / ``EUR/USD`` into ``{"EUR", "USD"}``."""

    cleaned = raw.strip().upper().replace("/", "").replace("-", "").replace(" ", "")
    if len(cleaned) != 6 or not cleaned.isalpha():
        raise ValueError("Pair must look like EURUSD or EUR/USD.")
    base, quote = cleaned[:3], cleaned[3:]
    if base == quote:
        raise ValueError("The two currencies in a pair must differ.")
    unknown = [code for code in (base, quote) if code not in KNOWN_CURRENCIES]
    if unknown:
        raise ValueError(f"Unknown currency code(s): {', '.join(unknown)}.")
    return {base, quote}


def parse_currency_list(raw: str) -> set[str]:
    """Parse a comma/space separated currency list. Empty or ``all`` means all."""

    cleaned = raw.strip().upper()
    if not cleaned or cleaned == "ALL":
        return set()

    codes = {part for part in cleaned.replace(",", " ").split() if part}
    unknown = sorted(code for code in codes if code not in KNOWN_CURRENCIES)
    if unknown:
        raise ValueError(f"Unknown currency code(s): {', '.join(unknown)}.")
    return codes


def filter_events(
    events: Iterable[Event],
    currencies: set[str] | None = None,
    min_impact: str = "high",
) -> list[Event]:
    """Filter by currency set and minimum impact, preserving time order."""

    threshold = IMPACT_RANK.get(min_impact.lower(), 3)
    result = [
        event
        for event in events
        if (not currencies or event.currency in currencies)
        and event.impact_rank >= threshold
    ]
    return sorted(result, key=lambda event: event.when)


def events_on_local_date(events: Iterable[Event], day: date, tz: ZoneInfo) -> list[Event]:
    return sorted(
        (event for event in events if event.when.astimezone(tz).date() == day),
        key=lambda event: event.when,
    )


def chunked(items: Sequence[Event], size: int) -> Iterator[Sequence[Event]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _date_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


class CalendarFeed:
    """Async-friendly, cached accessor for the weekly feed (with disk cache)."""

    def __init__(
        self,
        ttl_seconds: int = 600,
        timeout: float = 25.0,
        cache_path: Path | str | None = None,
    ) -> None:
        self._ttl = max(60, int(ttl_seconds))
        self._timeout = timeout
        self._cache_path = Path(cache_path) if cache_path else _FEED_CACHE_PATH
        self._events: list[Event] = []
        self._fetched_at = 0.0
        self._retry_at = 0.0
        self._lock = asyncio.Lock()
        self._load_disk_cache()

    def _load_disk_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            events = [
                event
                for event in (
                    Event.from_dict(raw) for raw in payload.get("events", [])
                )
                if event is not None
            ]
            if events:
                self._events = sorted(events, key=lambda event: event.when)
                log.info(
                    "Loaded %d cached calendar event(s) from %s",
                    len(self._events),
                    self._cache_path,
                )
        except Exception:
            log.warning("Could not read feed cache %s", self._cache_path, exc_info=True)

    def _save_disk_cache(self, events: list[Event]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "events": [event.to_dict() for event in events],
            }
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._cache_path)
        except Exception:
            log.warning("Could not write feed cache %s", self._cache_path, exc_info=True)

    async def events(self, force: bool = False) -> list[Event]:
        async with self._lock:
            now = time.monotonic()
            fresh = self._events and (now - self._fetched_at) < self._ttl
            if fresh and not force:
                return self._events
            if self._events and now < self._retry_at:
                return self._events

            try:
                events = await asyncio.to_thread(fetch_events_sync, self._timeout)
            except RateLimited as exc:
                self._retry_at = time.monotonic() + max(30.0, exc.retry_after)
                if self._events:
                    log.warning(
                        "Calendar feed rate-limited; serving cached events for "
                        "another %ds",
                        int(self._retry_at - time.monotonic()),
                    )
                    return self._events
                raise
            except Exception as exc:
                self._retry_at = time.monotonic() + 60.0
                if self._events:
                    log.warning(
                        "Calendar refresh failed (%s); serving cached events", exc
                    )
                    return self._events
                raise

            self._events = events
            self._fetched_at = now
            self._retry_at = 0.0
            await asyncio.to_thread(self._save_disk_cache, events)
            return events


def _normalise_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


class CalendarService:
    """Combines the JSON feed with the HTML fallback for date coverage."""

    def __init__(self, feed: CalendarFeed) -> None:
        self.feed = feed

    async def events_for_range(self, start_day: date, end_day: date) -> list[Event]:
        """Return events covering the given local date range.

        Dates outside the JSON feed's coverage are scraped from HTML. The
        caller is responsible for filtering by timezone/date afterwards.
        """

        try:
            feed_events = await self.feed.events()
        except Exception as exc:
            log.warning("Using HTML calendar fallback only: %s", exc)
            feed_events = []

        merged: dict[str, Event] = {
            event.dedupe_key: event for event in feed_events
        }

        feed_days = sorted(
            {event.when.astimezone(_ET).date() for event in feed_events}
        )
        if feed_days:
            first_covered, last_covered = feed_days[0], feed_days[-1]
        else:
            first_covered = last_covered = None

        # Fetch one extra day on each side: an event can fall on a different
        # calendar day in the guild's timezone than it does in New York.
        wanted = list(
            _date_range(
                start_day - timedelta(days=1), end_day + timedelta(days=1)
            )
        )
        missing = [
            day
            for day in wanted
            if first_covered is None
            or day < first_covered
            or day > last_covered
        ]

        for day in missing[:10]:
            try:
                scraped = await asyncio.to_thread(scrape_day_sync, day)
            except Exception as exc:
                log.warning("HTML fallback failed for %s: %s", day, exc)
                continue
            for event in scraped:
                merged.setdefault(event.dedupe_key, event)

        return sorted(merged.values(), key=lambda event: event.when)

    async def event_details(self, event: Event) -> Event | None:
        """Re-scrape the event's day page to read the released actual value.

        The weekly JSON feed contains no actual values, so this bypasses the
        normal six-hour HTML cache and uses a short one-minute cache instead.
        The site's calendar day can differ from the UTC date, so adjacent days
        are tried as well.
        """

        day = event.when.date()
        target_title = _normalise_title(event.title)

        for page_day in (day, day + timedelta(days=1), day - timedelta(days=1)):
            try:
                scraped = await asyncio.to_thread(
                    scrape_day_sync, page_day, 25.0, 60.0
                )
            except Exception as exc:
                log.warning("Could not refresh HTML for %s: %s", page_day, exc)
                continue

            matches = [
                item
                for item in scraped
                if item.currency == event.currency
                and _normalise_title(item.title) == target_title
            ]
            if not matches:
                # Titles occasionally change slightly between feed and page.
                matches = [
                    item
                    for item in scraped
                    if item.currency == event.currency
                    and (
                        target_title in _normalise_title(item.title)
                        or _normalise_title(item.title) in target_title
                    )
                ]
            if matches:
                return min(
                    matches,
                    key=lambda item: abs(
                        (item.when - event.when).total_seconds()
                    ),
                )

        return None
