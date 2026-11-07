"""Environment configuration and per-guild settings persistence."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))

_IMPACTS = {"all", "low", "medium", "high"}
_DIGEST_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

DEFAULT_CURRENCIES = {
    code.strip().upper()
    for code in os.getenv(
        "DEFAULT_CURRENCIES", "EUR,USD,GBP,JPY,AUD,CAD,CHF,NZD"
    ).split(",")
    if code.strip()
}


def _env_min_impact() -> str:
    value = os.getenv("DEFAULT_MIN_IMPACT", "high").strip().lower()
    return value if value in _IMPACTS else "high"


def _env_timezone() -> str:
    value = os.getenv("DEFAULT_TIMEZONE", "UTC").strip() or "UTC"
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Invalid DEFAULT_TIMEZONE %r; falling back to UTC", value)
        return "UTC"
    return value


def _env_digest() -> str | None:
    value = os.getenv("DEFAULT_DIGEST_TIME", "08:00").strip()
    if not value or value.lower() in {"off", "none", "disabled"}:
        return None
    if not _DIGEST_RE.match(value):
        log.warning("Invalid DEFAULT_DIGEST_TIME %r; using 08:00", value)
        return "08:00"
    return value


def _env_reminders() -> list[int]:
    raw = os.getenv("DEFAULT_REMINDERS", "15,60")
    minutes: list[int] = []
    for part in raw.replace(";", ",").split(","):
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


@dataclass
class GuildSettings:
    alert_channel_id: int | None = None
    currencies: set[str] = field(default_factory=lambda: set(DEFAULT_CURRENCIES))
    min_impact: str = "high"
    digest_time: str | None = "08:00"
    reminder_minutes: list[int] = field(default_factory=lambda: [15, 60])
    timezone: str = "UTC"
    enabled: bool = True

    def copy(self) -> "GuildSettings":
        return replace(
            self,
            currencies=set(self.currencies),
            reminder_minutes=list(self.reminder_minutes),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_channel_id": self.alert_channel_id,
            "currencies": sorted(self.currencies),
            "min_impact": self.min_impact,
            "digest_time": self.digest_time,
            "reminder_minutes": sorted(set(self.reminder_minutes), reverse=True),
            "timezone": self.timezone,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GuildSettings":
        channel = data.get("alert_channel_id")
        raw_currencies = data.get("currencies")
        # An explicitly empty list means "all currencies" and must be preserved.
        currencies = (
            {
                str(c).upper()
                for c in raw_currencies
                if str(c).strip()
            }
            if raw_currencies is not None
            else set(DEFAULT_CURRENCIES)
        )
        reminders = data.get("reminder_minutes")
        impact = str(data.get("min_impact", "high")).lower()
        timezone_name = str(data.get("timezone", "UTC"))

        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            timezone_name = "UTC"

        digest = data.get("digest_time")
        if digest is not None:
            digest = str(digest)
            if not _DIGEST_RE.match(digest):
                digest = None

        return cls(
            alert_channel_id=int(channel) if channel else None,
            currencies=currencies,
            min_impact=impact if impact in _IMPACTS else "high",
            digest_time=digest,
            reminder_minutes=[
                int(m)
                for m in (reminders or [])
                if str(m).lstrip("-").isdigit() and 1 <= int(m) <= 720
            ],
            timezone=timezone_name,
            enabled=bool(data.get("enabled", True)),
        )


def default_settings() -> GuildSettings:
    return GuildSettings(
        currencies=set(DEFAULT_CURRENCIES),
        min_impact=_env_min_impact(),
        digest_time=_env_digest(),
        reminder_minutes=_env_reminders(),
        timezone=_env_timezone(),
    )


def validate_timezone(name: str) -> str:
    name = name.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"Unknown timezone {name!r}. Use an IANA name like Europe/Istanbul."
        ) from exc
    return name


def validate_digest(value: str) -> str | None:
    value = value.strip()
    if value.lower() in {"off", "none", "disabled"}:
        return None
    if not _DIGEST_RE.match(value):
        raise ValueError("Digest time must be HH:MM (24h), for example 08:00, or off.")
    return value


def validate_reminders(value: str) -> list[int]:
    value = value.strip()
    if value.lower() in {"off", "none", "disabled"}:
        return []

    minutes: list[int] = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError("Reminders must be comma-separated minutes, e.g. 15,60.")
        minutes.append(int(part))

    invalid = [m for m in minutes if m < 1 or m > 720]
    if invalid:
        raise ValueError("Reminder offsets must be between 1 and 720 minutes.")
    return sorted(set(minutes), reverse=True)


class ConfigStore:
    """JSON-backed per-guild settings."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "settings.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._guilds: dict[str, GuildSettings] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            for guild_id, raw in (payload.get("guilds") or {}).items():
                if isinstance(raw, dict):
                    self._guilds[str(guild_id)] = GuildSettings.from_dict(raw)
        except Exception:
            log.exception("Could not read settings file %s", self.path)

    def _save_locked(self) -> None:
        payload = {
            "guilds": {
                guild_id: settings.to_dict()
                for guild_id, settings in sorted(self._guilds.items())
            }
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, guild_id: int) -> GuildSettings:
        with self._lock:
            stored = self._guilds.get(str(guild_id))
            if stored is None:
                return default_settings()
            return stored.copy()

    def update(self, guild_id: int, **changes: Any) -> GuildSettings:
        with self._lock:
            current = self._guilds.get(str(guild_id), default_settings())
            merged = replace(current, **changes)
            self._guilds[str(guild_id)] = merged
            self._save_locked()
            return merged.copy()


class StateStore:
    """Tracks sent alerts so restarts do not duplicate messages."""

    def __init__(self, path: Path | str | None = None, retention_days: int = 3) -> None:
        self.path = Path(path) if path else DATA_DIR / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._sent: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            sent = payload.get("sent") or {}
            if isinstance(sent, dict):
                self._sent = {str(k): str(v) for k, v in sent.items()}
        except Exception:
            log.exception("Could not read state file %s", self.path)
        self._prune_locked()

    def _prune_locked(self) -> None:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        kept: dict[str, str] = {}
        for key, stamp in self._sent.items():
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= cutoff:
                kept[key] = stamp
        self._sent = kept

    def _save_locked(self) -> None:
        payload = {"sent": self._sent}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def seen(self, key: str) -> bool:
        with self._lock:
            return key in self._sent

    def mark(self, key: str) -> None:
        from datetime import datetime, timezone

        with self._lock:
            self._prune_locked()
            self._sent[key] = datetime.now(timezone.utc).isoformat()
            self._save_locked()
