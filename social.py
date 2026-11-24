"""Free social publishing for the economic calendar bot.

Two independent targets:

* **X (Twitter)** through Buffer's free API. Buffer owns the X connection, so
  no paid X developer account is needed.
* **Bluesky** through the public AT Protocol API. Bluesky accounts and app
  passwords are free.

Both publishers are optional. Missing environment variables simply disable
that target; Discord keeps working exactly as before.

Run ``python social.py --channels`` with ``BUFFER_API_KEY`` set to list the
Buffer organization/channel IDs needed for ``BUFFER_CHANNEL_ID``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv

from calendar_source import Event, parse_currency_list
from formatting import impact_emoji, summary_result

load_dotenv()

log = logging.getLogger(__name__)

BUFFER_ENDPOINT = "https://api.buffer.com"
BLUESKY_DEFAULT_PDS = "https://bsky.social"
X_LIMIT = 280
BLUESKY_LIMIT = 300
REQUEST_TIMEOUT = 20

_MODES = {"off", "reminder", "release", "summary", "all"}
_IMPACTS = {"low", "medium", "high"}
_HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_]{1,64})")


class SocialError(RuntimeError):
    """A social network rejected a post."""


def _env_mode(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in _MODES else default


def _env_minutes(name: str, default: str) -> list[int]:
    minutes: list[int] = []
    for part in os.getenv(name, default).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if 1 <= value <= 720 and value not in minutes:
            minutes.append(value)
    return sorted(minutes, reverse=True)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


@dataclass
class SocialConfig:
    x_mode: str = "reminder"
    bluesky_mode: str = "all"
    min_impact: str = "high"
    currencies: set[str] = field(default_factory=set)
    reminder_minutes: list[int] = field(default_factory=lambda: [15])
    timezone: str = "UTC"
    buffer_api_key: str = ""
    buffer_channel_id: str = ""
    bluesky_handle: str = ""
    bluesky_password: str = ""
    bluesky_pds: str = BLUESKY_DEFAULT_PDS


def load_social_config() -> SocialConfig:
    """Read social settings from the environment. It never raises."""

    stored_tz = os.getenv("SOCIAL_TIMEZONE", "UTC").strip() or "UTC"
    try:
        ZoneInfo(stored_tz)
    except (ZoneInfoNotFoundError, ValueError):
        stored_tz = "UTC"

    min_impact = os.getenv("SOCIAL_MIN_IMPACT", "high").strip().lower()
    if min_impact not in _IMPACTS:
        min_impact = "high"

    try:
        currencies = parse_currency_list(os.getenv("SOCIAL_CURRENCIES", ""))
    except ValueError:
        log.warning("Invalid SOCIAL_CURRENCIES; using all currencies")
        currencies = set()

    return SocialConfig(
        x_mode=_env_mode("X_POST_MODE", "reminder"),
        bluesky_mode=_env_mode("BLUESKY_POST_MODE", "all"),
        min_impact=min_impact,
        currencies=currencies,
        reminder_minutes=_env_minutes("SOCIAL_REMINDER_MINUTES", "15"),
        timezone=stored_tz,
        buffer_api_key=os.getenv("BUFFER_API_KEY", "").strip(),
        buffer_channel_id=os.getenv("BUFFER_CHANNEL_ID", "").strip(),
        bluesky_handle=os.getenv("BLUESKY_HANDLE", "").strip(),
        bluesky_password=os.getenv("BLUESKY_APP_PASSWORD", "").strip(),
        bluesky_pds=(
            os.getenv("BLUESKY_PDS", BLUESKY_DEFAULT_PDS).strip()
            or BLUESKY_DEFAULT_PDS
        ),
    )


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rstrip()
    # Do not leave a dangling zero-width joiner at the end of an emoji.
    clipped = clipped.removesuffix("\u200d")
    return clipped + "…"


def _value(value: str) -> str:
    return value.strip() if value and value.strip() else "—"


def _when(event: Event, tz: ZoneInfo) -> str:
    return event.when.astimezone(tz).strftime("%a %d %b, %H:%M %Z")


def _tags(event: Event) -> str:
    return f"#{event.currency} #Forex #EconomicCalendar"


def _test_suffix(test: bool) -> str:
    return "\n(test post)" if test else ""


def reminder_text(
    event: Event, minutes: int, tz: ZoneInfo, *, test: bool = False
) -> str:
    return "".join(
        [
            f"{impact_emoji(event.impact)} {event.currency} · {event.title}\n",
            f"🕒 {_when(event, tz)} (in {minutes} min)\n",
            f"Forecast {_value(event.forecast)} · Previous {_value(event.previous)}\n",
            _tags(event),
            _test_suffix(test),
        ]
    )


def release_text(event: Event, tz: ZoneInfo, *, test: bool = False) -> str:
    return "".join(
        [
            f"🚨 {event.currency} · {event.title} — released\n",
            f"🕒 {_when(event, tz)}\n",
            f"Forecast {_value(event.forecast)} · Previous {_value(event.previous)}\n",
            _tags(event),
            _test_suffix(test),
        ]
    )


def summary_text(event: Event, tz: ZoneInfo, *, test: bool = False) -> str:
    headline, _detail, _colour = summary_result(event)
    return "".join(
        [
            f"📊 {event.currency} · {event.title}\n",
            f"Actual {_value(event.actual)} vs forecast {_value(event.forecast)}\n",
            f"{headline} · {_when(event, tz)}\n",
            _tags(event),
            _test_suffix(test),
        ]
    )


def _hashtag_facets(text: str) -> list[dict]:
    """Return Bluesky rich-text facets so hashtags are clickable."""

    facets: list[dict] = []
    for match in _HASHTAG_RE.finditer(text):
        start = len(text[: match.start()].encode("utf-8"))
        end = len(text[: match.end()].encode("utf-8"))
        facets.append(
            {
                "index": {"byteStart": start, "byteEnd": end},
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#tag",
                        "tag": match.group(1),
                    }
                ],
            }
        )
    return facets


def _buffer_request(api_key: str, query: str) -> dict:
    try:
        response = requests.post(
            BUFFER_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SocialError(f"Buffer request failed: {exc}") from exc

    if response.status_code != 200:
        raise SocialError(
            f"Buffer HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise SocialError("Buffer returned invalid JSON") from exc

    if payload.get("errors"):
        message = payload["errors"][0].get("message", "unknown error")
        raise SocialError(f"Buffer error: {message}")
    return payload


def fetch_buffer_channels(api_key: str) -> list[dict]:
    """List Buffer organizations and channels for the CLI helper."""

    if not api_key:
        raise SocialError("BUFFER_API_KEY is not set")

    payload = _buffer_request(
        api_key,
        "query { account { organizations { id name ownerEmail } } }",
    )
    organizations = (
        (payload.get("data") or {}).get("account", {}).get("organizations") or []
    )

    rows: list[dict] = []
    for organization in organizations:
        org_id = str(organization.get("id") or "")
        if not org_id:
            continue
        query = (
            "query { channels(input: {organizationId: "
            f"{json.dumps(org_id)}"
            "}) { id name displayName service isQueuePaused } }"
        )
        payload = _buffer_request(api_key, query)
        channels = (payload.get("data") or {}).get("channels") or []
        for channel in channels:
            rows.append(
                {
                    "organization_id": org_id,
                    "organization_name": organization.get("name") or "",
                    "id": str(channel.get("id") or ""),
                    "name": channel.get("displayName")
                    or channel.get("name")
                    or "",
                    "service": channel.get("service") or "",
                    "is_queue_paused": bool(channel.get("isQueuePaused")),
                }
            )
    return rows


class BufferPublisher:
    """Publish to X through Buffer's GraphQL API."""

    name = "x"
    limit = X_LIMIT

    def __init__(self, api_key: str, channel_id: str) -> None:
        self.api_key = api_key
        self.channel_id = channel_id

    def publish(self, text: str) -> str:
        text = _clip(text, self.limit)
        text_literal = json.dumps(text, ensure_ascii=False)
        channel_literal = json.dumps(self.channel_id)
        query = (
            "mutation { createPost(input: {"
            f"text: {text_literal}, "
            f"channelId: {channel_literal}, "
            "schedulingType: automatic, "
            "mode: shareNow"
            "}) {"
            " ... on PostActionSuccess { post { id } }"
            " ... on MutationError { message }"
            " } }"
        )

        payload = _buffer_request(self.api_key, query)
        result = (payload.get("data") or {}).get("createPost") or {}
        if result.get("message"):
            raise SocialError(f"Buffer rejected the post: {result['message']}")

        post = result.get("post") or {}
        return str(post.get("id") or "")


class BlueskyPublisher:
    """Publish to Bluesky with the free AT Protocol API."""

    name = "bluesky"
    limit = BLUESKY_LIMIT

    def __init__(self, handle: str, password: str, pds: str) -> None:
        self.handle = handle
        self.password = password
        self.pds = (pds or BLUESKY_DEFAULT_PDS).rstrip("/")
        self._token = ""
        self._did = ""
        self._expires = 0.0

    def _login(self) -> None:
        try:
            response = requests.post(
                f"{self.pds}/xrpc/com.atproto.server.createSession",
                json={"identifier": self.handle, "password": self.password},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SocialError(f"Bluesky login failed: {exc}") from exc

        if response.status_code != 200:
            raise SocialError(
                f"Bluesky login HTTP {response.status_code}: {response.text[:200]}"
            )

        data = response.json()
        token = data.get("accessJwt")
        did = data.get("did")
        if not token or not did:
            raise SocialError("Bluesky login returned no token")

        self._token = token
        self._did = did
        self._expires = time.time() + 3000

    def _ensure_session(self) -> None:
        if not self._token or time.time() >= self._expires:
            self._login()

    def publish(self, text: str) -> str:
        text = _clip(text, self.limit)
        created = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

        for attempt in (1, 2):
            self._ensure_session()
            record = {
                "$type": "app.bsky.feed.post",
                "text": text,
                "createdAt": created,
            }
            facets = _hashtag_facets(text)
            if facets:
                record["facets"] = facets

            try:
                response = requests.post(
                    f"{self.pds}/xrpc/com.atproto.repo.createRecord",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={
                        "repo": self._did,
                        "collection": "app.bsky.feed.post",
                        "record": record,
                    },
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise SocialError(f"Bluesky request failed: {exc}") from exc

            if response.status_code == 401 and attempt == 1:
                self._token = ""
                continue
            if response.status_code >= 300:
                raise SocialError(
                    f"Bluesky HTTP {response.status_code}: {response.text[:200]}"
                )

            data = response.json()
            return str(data.get("uri") or data.get("cid") or "")

        raise SocialError("Bluesky rejected the session twice")


class SocialManager:
    """Route posts to every configured target that wants that event type."""

    def __init__(self, config: SocialConfig) -> None:
        self.config = config
        self.publishers: dict[str, BufferPublisher | BlueskyPublisher] = {}

        if (
            config.x_mode != "off"
            and config.buffer_api_key
            and config.buffer_channel_id
        ):
            self.publishers["x"] = BufferPublisher(
                config.buffer_api_key, config.buffer_channel_id
            )

        if (
            config.bluesky_mode != "off"
            and config.bluesky_handle
            and config.bluesky_password
        ):
            self.publishers["bluesky"] = BlueskyPublisher(
                config.bluesky_handle,
                config.bluesky_password,
                config.bluesky_pds,
            )

    @property
    def active(self) -> bool:
        return bool(self.publishers)

    def targets_for(self, kind: str) -> list[str]:
        modes = {
            "x": self.config.x_mode,
            "bluesky": self.config.bluesky_mode,
        }
        return [
            name
            for name in self.publishers
            if modes.get(name) in {"all", kind}
        ]

    def publish(self, target: str, text: str) -> str:
        publisher = self.publishers.get(target)
        if publisher is None:
            raise SocialError(f"Social target {target!r} is not configured")
        return publisher.publish(text)

    def describe(self) -> str:
        """A secret-free summary for /settings and startup logs."""

        labels = {"x": "X via Buffer", "bluesky": "Bluesky"}
        modes = {"x": self.config.x_mode, "bluesky": self.config.bluesky_mode}
        parts = [
            f"{labels.get(name, name)} ({modes.get(name, 'off')})"
            for name in self.publishers
        ]
        return " · ".join(parts) if parts else "off"


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Social publishing helpers for the calendar bot"
    )
    parser.add_argument(
        "--channels",
        action="store_true",
        help="List Buffer organizations and channels (needs BUFFER_API_KEY)",
    )
    args = parser.parse_args()

    if not args.channels:
        parser.print_help()
        return 0

    try:
        rows = fetch_buffer_channels(os.getenv("BUFFER_API_KEY", "").strip())
    except SocialError as exc:
        print(f"Could not list Buffer channels: {exc}")
        return 1

    if not rows:
        print("No Buffer channels found. Connect X to Buffer first.")
        return 0

    for row in rows:
        print(
            f"{row['organization_name']} ({row['organization_id']})  "
            f"{row['service']}: {row['name']}  id={row['id']}"
            + ("  [queue paused]" if row["is_queue_paused"] else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
