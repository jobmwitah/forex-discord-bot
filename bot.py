"""Discord bot that posts ForexFactory economic-calendar alerts.

Run ``python bot.py --preview`` to test the data feed without a Discord token.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from calendar_source import (
    KNOWN_CURRENCIES,
    CalendarFeed,
    CalendarService,
    Event,
    events_on_local_date,
    filter_events,
    parse_currency_list,
    parse_pair,
)
from config import (
    ConfigStore,
    StateStore,
    validate_digest,
    validate_reminders,
    validate_timezone,
)
from formatting import (
    IMPACT_CHOICES,
    alert_embed,
    build_event_embeds,
    day_title,
    display_currencies,
    display_impact,
    group_embeds,
    release_alert_embed,
    release_summary_embed,
)
from social import (
    SocialManager,
    load_social_config,
    release_text,
    reminder_text,
    summary_text,
)

try:
    from graphics import CardImage, render_card
except Exception as _graphics_error:  # noqa: BLE001 - images stay optional
    CardImage = None  # type: ignore[assignment,misc]
    render_card = None  # type: ignore[assignment]
    _GRAPHICS_IMPORT_ERROR: Exception | None = _graphics_error
else:
    _GRAPHICS_IMPORT_ERROR = None

load_dotenv()

# Windows consoles default to cp1252, which cannot print the emoji used in
# embeds and previews. Reconfigure before logging is set up.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("forex-bot")

if _GRAPHICS_IMPORT_ERROR is not None:
    log.warning(
        "Social image cards are off (install Pillow): %s",
        _GRAPHICS_IMPORT_ERROR,
    )

try:
    FEED_TTL_SECONDS = max(60, int(os.getenv("FEED_TTL_SECONDS", "600")))
except ValueError:
    FEED_TTL_SECONDS = 600
GUILD_ID = os.getenv("GUILD_ID", "").strip()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        log.warning("Invalid %s; using %d", name, default)
        return default


# Wait this long after the release before looking for the actual value, then
# keep retrying until MAX_WAIT has elapsed. The JSON feed has no actual values,
# so the value is scraped from the HTML calendar on demand.
RELEASE_SUMMARY_DELAY_SECONDS = _env_int(
    "RELEASE_SUMMARY_DELAY_SECONDS", 30, 0
)
RELEASE_SUMMARY_MAX_WAIT_SECONDS = _env_int(
    "RELEASE_SUMMARY_MAX_WAIT_SECONDS", 900, 60
)

feed = CalendarFeed(ttl_seconds=FEED_TTL_SECONDS)
service = CalendarService(feed)
store = ConfigStore()
state = StateStore()
social_manager = SocialManager(load_social_config())

# After a social network rejects a post, wait this long before trying the same
# event/target again instead of retrying every ten seconds.
SOCIAL_RETRY_SECONDS = 120


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - never crash on a stored bad timezone
        return ZoneInfo("UTC")


def _env_channel_id() -> int | None:
    raw = os.getenv("ALERT_CHANNEL_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("ALERT_CHANNEL_ID is not a valid integer; ignoring it")
        return None


def _resolve_day(raw: str | None, tz: ZoneInfo) -> date:
    text = (raw or "").strip().lower()
    today = datetime.now(timezone.utc).astimezone(tz).date()
    if not text or text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            "Date must be YYYY-MM-DD, 'today' or 'tomorrow'."
        ) from exc


def _command_filter(
    pair: str | None, currencies: str | None
) -> set[str] | None:
    if pair:
        return parse_pair(pair)
    if currencies:
        return parse_currency_list(currencies)
    return None


def _released_actual(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned.lower() in {"", "n/a", "na", "-", "—"}:
        return ""
    return cleaned


def _social_card(
    event: Event,
    kind: str,
    tz: ZoneInfo,
    *,
    minutes: int = 15,
    test: bool = False,
) -> CardImage | None:
    """Render one branded card; a drawing failure must never stop alerts."""

    if render_card is None or not social_manager.images_enabled:
        return None
    try:
        return render_card(
            event,
            kind,
            tz,
            minutes=minutes,
            brand=social_manager.config.brand_name,
            handle=social_manager.config.brand_handle,
            test=test,
        )
    except Exception as exc:  # noqa: BLE001 - text-only fallback
        log.warning("Could not render %s social card: %s", kind, exc)
        return None


def _settings_embed(guild: discord.Guild, settings) -> discord.Embed:
    channel_id = settings.alert_channel_id or _env_channel_id()
    channel = guild.get_channel_or_thread(channel_id) if channel_id else None
    if channel_id and channel is None:
        channel_text = "⚠️ configured channel no longer exists"
    elif channel is not None:
        channel_text = channel.mention
    else:
        channel_text = "not set — use `/setup channel:#your-channel`"

    reminders = (
        ", ".join(f"{m} min" for m in settings.reminder_minutes)
        if settings.reminder_minutes
        else "off"
    )
    digest = settings.digest_time or "off"

    embed = discord.Embed(
        title="⚙️ Forex calendar alert settings",
        color=0x5865F2,
    )
    embed.add_field(name="Status", value="✅ Enabled" if settings.enabled else "⛔ Disabled", inline=True)
    embed.add_field(name="Alert channel", value=channel_text, inline=True)
    embed.add_field(name="Timezone", value=settings.timezone, inline=True)
    embed.add_field(name="Currencies", value=display_currencies(settings.currencies), inline=False)
    embed.add_field(name="Minimum impact", value=display_impact(settings.min_impact), inline=True)
    embed.add_field(name="Daily digest", value=digest, inline=True)
    embed.add_field(name="Pre-event reminders", value=reminders, inline=True)
    embed.add_field(
        name="Release alert (T-0)",
        value="on" if settings.release_alerts else "off",
        inline=True,
    )
    embed.add_field(
        name="Release summary",
        value="on" if settings.release_summary else "off",
        inline=True,
    )
    embed.add_field(
        name="Social posts",
        value=social_manager.describe(),
        inline=False,
    )
    embed.set_footer(text="Change these with /setup — e.g. /setup digest:07:30 reminders:15,60")
    return embed


class ForexBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self._synced = False
        self._alert_task: asyncio.Task | None = None
        self._social_cooldowns: dict[str, float] = {}

    async def setup_hook(self) -> None:
        await super().setup_hook()
        log.info("Social publishing: %s", social_manager.describe())
        self._alert_task = asyncio.create_task(self._alert_loop(), name="alerts")

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        if self._synced:
            return
        self._synced = True

        try:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            else:
                synced = await self.tree.sync()
            log.info("Synced %d slash command(s)", len(synced))
        except Exception:
            log.exception("Slash command sync failed")

    async def channel_for(self, guild: discord.Guild, settings):
        channel_id = settings.alert_channel_id or _env_channel_id()
        if not channel_id:
            return None

        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return None
        if getattr(channel, "guild", None) != guild:
            return None
        return channel

    async def send_embeds(self, channel, embeds: list[discord.Embed]) -> None:
        for group in group_embeds(embeds):
            await channel.send(embeds=group)
            await asyncio.sleep(0.5)

    async def _alert_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._alert_tick()
            except Exception:
                log.exception("Alert check failed")
            await asyncio.sleep(10)

    async def _social_publish(
        self,
        kind: str,
        key: str,
        text: str,
        image: CardImage | None = None,
    ) -> dict[str, str]:
        """Publish one message to every target that wants this kind.

        Each target is tracked separately, so a failed Bluesky call never
        suppresses an X post that already succeeded (or the reverse).
        """

        results: dict[str, str] = {}
        for target in social_manager.targets_for(kind):
            state_key = f"social:{target}:{key}"
            if state.seen(state_key):
                continue

            if time.monotonic() < self._social_cooldowns.get(state_key, 0.0):
                results[target] = "retry later"
                continue

            try:
                await asyncio.to_thread(
                    social_manager.publish,
                    target,
                    text,
                    image,
                    key,
                )
            except Exception as exc:  # noqa: BLE001 - keep the alert loop alive
                self._social_cooldowns[state_key] = (
                    time.monotonic() + SOCIAL_RETRY_SECONDS
                )
                log.warning(
                    "Social post %s to %s failed: %s", key, target, exc
                )
                results[target] = f"failed: {exc}"
                continue

            self._social_cooldowns.pop(state_key, None)
            state.mark(state_key)
            log.info("Posted %s to %s", key, target)
            results[target] = "sent"

        return results

    async def _social_tick(self, now: datetime) -> None:
        """Post reminders, releases and summaries to Buffer/Bluesky.

        Social settings come from the environment rather than per-guild
        settings, because one X/Bluesky account serves every Discord server.
        """

        if not social_manager.active:
            return

        cfg = social_manager.config
        tz = _tz(cfg.timezone)
        local_now = now.astimezone(tz)
        today = local_now.date()
        tomorrow = today + timedelta(days=1)

        try:
            events = await service.events_for_range(today, tomorrow)
        except Exception:
            log.exception("Could not load calendar for social posts")
            return

        candidates = [
            event
            for event in events
            if event.when.astimezone(tz).date() in (today, tomorrow)
        ]
        matching = filter_events(candidates, cfg.currencies, cfg.min_impact)

        # Pre-event reminders.
        for event in matching:
            for minutes in cfg.reminder_minutes:
                trigger = event.when - timedelta(minutes=minutes)
                delta = (now - trigger).total_seconds()
                if 0 <= delta <= 90:
                    card = await asyncio.to_thread(
                        _social_card, event, "reminder", tz, minutes=minutes
                    )
                    await self._social_publish(
                        "reminder",
                        f"rem:{event.id}:{minutes}",
                        reminder_text(event, minutes, tz),
                        card,
                    )

        # Release-time alert (T-0).
        for event in matching:
            delta = (now - event.when).total_seconds()
            if 0 <= delta <= 90:
                card = await asyncio.to_thread(
                    _social_card, event, "release", tz
                )
                await self._social_publish(
                    "release",
                    f"release:{event.id}",
                    release_text(event, tz),
                    card,
                )

        # Post-release summary with the actual value. Only scrape when at
        # least one target still needs this event's summary.
        oldest = RELEASE_SUMMARY_DELAY_SECONDS + RELEASE_SUMMARY_MAX_WAIT_SECONDS
        for event in matching:
            elapsed = (now - event.when).total_seconds()
            if elapsed < RELEASE_SUMMARY_DELAY_SECONDS or elapsed > oldest:
                continue

            pending = [
                target
                for target in social_manager.targets_for("summary")
                if not state.seen(f"social:{target}:summary:{event.id}")
            ]
            if not pending:
                continue

            actual = _released_actual(event.actual)
            forecast = event.forecast
            previous = event.previous

            if not actual:
                try:
                    details = await service.event_details(event)
                except Exception:
                    log.exception(
                        "Could not fetch actual for social summary %s",
                        event.title,
                    )
                    details = None
                if details is not None:
                    actual = _released_actual(details.actual)
                    forecast = details.forecast or forecast
                    previous = details.previous or previous

            if not actual:
                # Actual not published yet; retry on the next tick.
                continue

            updated = replace(
                event,
                actual=actual,
                forecast=forecast,
                previous=previous,
            )
            card = await asyncio.to_thread(
                _social_card, updated, "summary", tz
            )
            await self._social_publish(
                "summary",
                f"summary:{event.id}",
                summary_text(updated, tz),
                card,
            )

    async def _alert_tick(self) -> None:
        now = discord.utils.utcnow()

        for guild in self.guilds:
            settings = store.get(guild.id)
            if not settings.enabled or (
                not settings.alert_channel_id and not _env_channel_id()
            ):
                continue

            channel = await self.channel_for(guild, settings)
            if channel is None:
                continue

            tz = _tz(settings.timezone)
            local_now = now.astimezone(tz)
            today = local_now.date()
            tomorrow = today + timedelta(days=1)

            try:
                events = await service.events_for_range(today, tomorrow)
            except Exception:
                log.exception("Could not load calendar for guild %s", guild.id)
                continue

            candidates = [
                event
                for event in events
                if event.when.astimezone(tz).date() in (today, tomorrow)
            ]
            matching = filter_events(
                candidates, settings.currencies, settings.min_impact
            )

            # Pre-event reminders.
            for event in matching:
                for minutes in settings.reminder_minutes:
                    trigger = event.when - timedelta(minutes=minutes)
                    delta = (now - trigger).total_seconds()
                    if 0 <= delta <= 90:
                        key = f"rem:{guild.id}:{event.id}:{minutes}"
                        if not state.seen(key):
                            try:
                                await channel.send(embed=alert_embed(event, minutes))
                                state.mark(key)
                            except discord.HTTPException:
                                log.exception(
                                    "Could not send reminder in guild %s", guild.id
                                )

            # Release-time alert (T-0).
            if settings.release_alerts:
                for event in matching:
                    delta = (now - event.when).total_seconds()
                    if not (0 <= delta <= 90):
                        continue
                    key = f"release:{guild.id}:{event.id}"
                    if state.seen(key):
                        continue
                    try:
                        await channel.send(embed=release_alert_embed(event))
                        state.mark(key)
                    except discord.HTTPException:
                        log.exception(
                            "Could not send release alert in guild %s", guild.id
                        )

            # Post-release summary with the actual value.
            if settings.release_summary:
                oldest = (
                    RELEASE_SUMMARY_DELAY_SECONDS
                    + RELEASE_SUMMARY_MAX_WAIT_SECONDS
                )
                for event in matching:
                    key = f"summary:{guild.id}:{event.id}"
                    if state.seen(key):
                        continue

                    elapsed = (now - event.when).total_seconds()
                    if elapsed < RELEASE_SUMMARY_DELAY_SECONDS:
                        continue
                    if elapsed > oldest:
                        # Too old to be useful after a restart; do not retry.
                        state.mark(key)
                        continue

                    actual = _released_actual(event.actual)
                    forecast = event.forecast
                    previous = event.previous

                    if not actual:
                        try:
                            details = await service.event_details(event)
                        except Exception:
                            log.exception(
                                "Could not fetch actual for %s", event.title
                            )
                            details = None
                        if details is not None:
                            actual = _released_actual(details.actual)
                            forecast = details.forecast or forecast
                            previous = details.previous or previous

                    if not actual:
                        # Actual not published yet; retry on the next tick.
                        continue

                    summary_event = replace(
                        event,
                        actual=actual,
                        forecast=forecast,
                        previous=previous,
                    )
                    try:
                        await channel.send(
                            embed=release_summary_embed(summary_event)
                        )
                        state.mark(key)
                    except discord.HTTPException:
                        log.exception(
                            "Could not send release summary in guild %s", guild.id
                        )

            # Daily digest.
            if settings.digest_time:
                hour, minute = map(int, settings.digest_time.split(":"))
                target = local_now.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                delta = (local_now - target).total_seconds()
                if 0 <= delta <= 90:
                    key = f"digest:{guild.id}:{today.isoformat()}"
                    if not state.seen(key):
                        todays = filter_events(
                            events_on_local_date(events, today, tz),
                            settings.currencies,
                            settings.min_impact,
                        )
                        try:
                            if todays:
                                await self.send_embeds(
                                    channel,
                                    build_event_embeds(day_title(today), todays),
                                )
                            else:
                                await channel.send(
                                    f"📅 No matching events today "
                                    f"({today:%a, %b %d})."
                                )
                            state.mark(key)
                        except discord.HTTPException:
                            log.exception(
                                "Could not send digest in guild %s", guild.id
                            )

        # Social posts run after Discord so their network latency cannot delay
        # time-sensitive alerts.
        await self._social_tick(now)


bot = ForexBot()
tree = bot.tree


@tree.command(name="calendar", description="Show economic calendar events for a day")
@app_commands.describe(
    date="YYYY-MM-DD, 'today' or 'tomorrow' (default: today)",
    pair="Filter by pair, e.g. EURUSD (matches EUR and USD events)",
    currencies="Filter by currencies, e.g. USD,EUR — or 'all'",
    impact="Minimum impact level (default: this server's setting)",
)
@app_commands.choices(impact=IMPACT_CHOICES)
@app_commands.guild_only()
async def calendar_command(
    interaction: discord.Interaction,
    date: Optional[str] = None,
    pair: Optional[str] = None,
    currencies: Optional[str] = None,
    impact: Optional[app_commands.Choice[str]] = None,
) -> None:
    await interaction.response.defer(thinking=True)

    settings = store.get(interaction.guild_id)
    tz = _tz(settings.timezone)
    day = _resolve_day(date, tz)
    currency_filter = _command_filter(pair, currencies)
    min_impact = impact.value if impact else settings.min_impact

    events = await service.events_for_range(day, day)
    selected = filter_events(
        events_on_local_date(events, day, tz), currency_filter, min_impact
    )
    if not selected:
        await interaction.followup.send(
            f"📅 No matching events on {day:%a, %b %d, %Y}."
        )
        return

    for group in group_embeds(build_event_embeds(day_title(day), selected)):
        await interaction.followup.send(embeds=group)
        await asyncio.sleep(0.4)


@tree.command(name="next", description="Show upcoming economic events")
@app_commands.describe(
    hours="How many hours ahead to look (1-168, default: 24)",
    pair="Filter by pair, e.g. EURUSD",
    currencies="Filter by currencies, e.g. USD,EUR — or 'all'",
    impact="Minimum impact level",
)
@app_commands.choices(impact=IMPACT_CHOICES)
@app_commands.guild_only()
async def next_command(
    interaction: discord.Interaction,
    hours: Optional[int] = 24,
    pair: Optional[str] = None,
    currencies: Optional[str] = None,
    impact: Optional[app_commands.Choice[str]] = None,
) -> None:
    await interaction.response.defer(thinking=True)

    settings = store.get(interaction.guild_id)
    window = max(1, min(hours or 24, 168))
    currency_filter = _command_filter(pair, currencies)
    min_impact = impact.value if impact else settings.min_impact

    tz = _tz(settings.timezone)
    now = discord.utils.utcnow()
    end = now + timedelta(hours=window)
    events = await service.events_for_range(
        now.astimezone(tz).date(), end.astimezone(tz).date()
    )
    selected = filter_events(
        [event for event in events if now <= event.when <= end],
        currency_filter,
        min_impact,
    )
    if not selected:
        await interaction.followup.send(
            f"⏭ No matching events in the next {window} hours."
        )
        return

    embeds = build_event_embeds(f"⏭ Upcoming events — next {window}h", selected)
    for group in group_embeds(embeds):
        await interaction.followup.send(embeds=group)
        await asyncio.sleep(0.4)


@tree.command(name="week", description="Show the next seven days of economic events")
@app_commands.describe(
    pair="Filter by pair, e.g. EURUSD",
    currencies="Filter by currencies, e.g. USD,EUR — or 'all'",
    impact="Minimum impact level",
)
@app_commands.choices(impact=IMPACT_CHOICES)
@app_commands.guild_only()
async def week_command(
    interaction: discord.Interaction,
    pair: Optional[str] = None,
    currencies: Optional[str] = None,
    impact: Optional[app_commands.Choice[str]] = None,
) -> None:
    await interaction.response.defer(thinking=True)

    settings = store.get(interaction.guild_id)
    tz = _tz(settings.timezone)
    today = datetime.now(timezone.utc).astimezone(tz).date()
    end_day = today + timedelta(days=6)
    currency_filter = _command_filter(pair, currencies)
    min_impact = impact.value if impact else settings.min_impact

    events = await service.events_for_range(today, end_day)
    selected = filter_events(
        [
            event
            for event in events
            if today <= event.when.astimezone(tz).date() <= end_day
        ],
        currency_filter,
        min_impact,
    )
    if not selected:
        await interaction.followup.send(
            f"🗓 No matching events between {today:%b %d} and {end_day:%b %d}."
        )
        return

    embeds = build_event_embeds(
        f"🗓 Economic calendar — {today:%b %d} to {end_day:%b %d}", selected
    )
    for group in group_embeds(embeds):
        await interaction.followup.send(embeds=group)
        await asyncio.sleep(0.4)


@tree.command(name="pairs", description="How pair filtering works")
async def pairs_command(interaction: discord.Interaction) -> None:
    majors = "EURUSD · GBPUSD · USDJPY · USDCHF · USDCAD · AUDUSD · NZDUSD"
    crosses = (
        "EURGBP · EURJPY · EURCHF · EURAUD · EURCAD · EURNZD · "
        "GBPJPY · GBPCHF · GBPAUD · GBPCAD · GBPNZD · AUDJPY · "
        "AUDCAD · AUDCHF · AUDNZD · CADJPY · CADCHF · CHFJPY · "
        "NZDJPY · NZDCAD · NZDCHF"
    )
    embed = discord.Embed(
        title="💱 Pair filtering",
        description=(
            "Use `pair:EURUSD` with `/calendar`, `/next` or `/week`. "
            "Any six-letter pair works, e.g. `GBPJPY`, `EUR/USD`, `USDTRY`."
        ),
        color=0x5865F2,
    )
    embed.add_field(name="Majors", value=majors, inline=False)
    embed.add_field(name="Crosses", value=crosses, inline=False)
    embed.add_field(
        name="How it works",
        value=(
            "A pair filter matches **both** currencies. `EURUSD` shows every "
            "EUR and USD event, including events that affect the pair indirectly."
        ),
        inline=False,
    )
    embed.add_field(
        name="Currencies",
        value=f"{len(KNOWN_CURRENCIES)} supported codes — you can also pass "
        "`currencies:USD,EUR` instead of a pair.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="settings", description="Show this server's alert settings")
@app_commands.guild_only()
async def settings_command(interaction: discord.Interaction) -> None:
    settings = store.get(interaction.guild_id)
    await interaction.response.send_message(
        embed=_settings_embed(interaction.guild, settings), ephemeral=True
    )


@tree.command(name="setup", description="Configure economic calendar alerts")
@app_commands.describe(
    channel="Channel for daily digests and pre-event reminders",
    currencies="Comma-separated currencies, e.g. USD,EUR,GBP — or 'all'",
    impact="Minimum impact level to alert on",
    digest="Daily digest time in HH:MM (24h), or 'off'",
    reminders="Comma-separated minutes before events, e.g. 15,60 — or 'off'",
    timezone="IANA timezone for digest scheduling, e.g. Europe/Istanbul",
    enabled="Turn alerts on or off",
    release_alerts="Send an alert exactly when an event is released",
    release_summary="Send a post-release summary comparing actual vs forecast",
)
@app_commands.choices(impact=IMPACT_CHOICES)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setup_command(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    currencies: Optional[str] = None,
    impact: Optional[app_commands.Choice[str]] = None,
    digest: Optional[str] = None,
    reminders: Optional[str] = None,
    timezone: Optional[str] = None,
    enabled: Optional[bool] = None,
    release_alerts: Optional[bool] = None,
    release_summary: Optional[bool] = None,
) -> None:
    changes: dict = {}

    if channel is not None:
        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            raise ValueError(
                f"I need **Send Messages** and **Embed Links** in {channel.mention}."
            )
        changes["alert_channel_id"] = channel.id
    if currencies is not None:
        changes["currencies"] = parse_currency_list(currencies)
    if impact is not None:
        changes["min_impact"] = impact.value
    if digest is not None:
        changes["digest_time"] = validate_digest(digest)
    if reminders is not None:
        changes["reminder_minutes"] = validate_reminders(reminders)
    if timezone is not None:
        changes["timezone"] = validate_timezone(timezone)
    if enabled is not None:
        changes["enabled"] = enabled
    if release_alerts is not None:
        changes["release_alerts"] = release_alerts
    if release_summary is not None:
        changes["release_summary"] = release_summary

    if not changes:
        await interaction.response.send_message(
            embed=_settings_embed(interaction.guild, store.get(interaction.guild_id)),
            ephemeral=True,
        )
        return

    updated = store.update(interaction.guild_id, **changes)
    await interaction.response.send_message(
        "✅ Settings saved.",
        embed=_settings_embed(interaction.guild, updated),
        ephemeral=True,
    )


TEST_CHOICES = [
    app_commands.Choice(name="All messages", value="all"),
    app_commands.Choice(name="Pre-event reminder", value="reminder"),
    app_commands.Choice(name="Release alert (T-0)", value="release"),
    app_commands.Choice(name="Release summary", value="summary"),
]


@tree.command(name="test", description="Send test alerts to the configured channel")
@app_commands.describe(
    kind="Which test message(s) to send",
    social="Also publish the test post(s) to X/Bluesky (public)",
)
@app_commands.choices(kind=TEST_CHOICES)
@app_commands.guild_only()
async def test_command(
    interaction: discord.Interaction,
    kind: Optional[app_commands.Choice[str]] = None,
    social: Optional[bool] = None,
) -> None:
    # Discord invalidates interaction tokens after 3 seconds; acknowledge first.
    await interaction.response.defer(ephemeral=True, thinking=True)

    settings = store.get(interaction.guild_id)
    channel = await bot.channel_for(interaction.guild, settings)
    if channel is None:
        await interaction.followup.send(
            "No alert channel configured. Use "
            "`/setup channel:#your-channel` first.",
            ephemeral=True,
        )
        return

    selected = kind.value if kind else "all"
    now = discord.utils.utcnow()

    # Deliberately synthetic: a test must not depend on the live calendar or on
    # whichever real event happens to be next.
    base = Event(
        id="test-event",
        title="Test: Non-Farm Employment Change",
        currency="USD",
        when=now + timedelta(minutes=15),
        impact="high",
        forecast="0.2%",
        previous="0.1%",
    )

    embeds: list[discord.Embed] = []

    if selected in {"all", "reminder"}:
        embeds.append(alert_embed(base, 15, test=True))

    if selected in {"all", "release"}:
        embeds.append(
            release_alert_embed(replace(base, when=now), test=True)
        )

    if selected in {"all", "summary"}:
        summary_sample = replace(
            base,
            when=now - timedelta(minutes=2),
            actual="0.4%",
        )
        embeds.append(release_summary_embed(summary_sample, test=True))

    try:
        await channel.send(embeds=embeds)
    except discord.HTTPException as exc:
        raise ValueError(f"Could not send to {channel.mention}: {exc}") from exc

    kind_labels = {
        "all": "reminder + release alert + release summary",
        "reminder": "pre-event reminder",
        "release": "release alert",
        "summary": "release summary",
    }

    social_report: list[str] = []
    if social:
        if not social_manager.active:
            social_report.append(
                "Social posting is off. Set BUFFER_API_KEY + BUFFER_CHANNEL_ID "
                "or BLUESKY_HANDLE + BLUESKY_APP_PASSWORD in .env."
            )
        else:
            tz = _tz(social_manager.config.timezone)
            samples: list[tuple[str, str, CardImage | None]] = []
            if selected in {"all", "reminder"}:
                card = await asyncio.to_thread(
                    _social_card, base, "reminder", tz, test=True
                )
                samples.append(
                    ("reminder", reminder_text(base, 15, tz, test=True), card)
                )
            if selected in {"all", "release"}:
                release_sample = replace(base, when=now)
                card = await asyncio.to_thread(
                    _social_card, release_sample, "release", tz, test=True
                )
                samples.append(
                    (
                        "release",
                        release_text(release_sample, tz, test=True),
                        card,
                    )
                )
            if selected in {"all", "summary"}:
                card = await asyncio.to_thread(
                    _social_card, summary_sample, "summary", tz, test=True
                )
                samples.append(
                    (
                        "summary",
                        summary_text(summary_sample, tz, test=True),
                        card,
                    )
                )

            for sample_kind, text, card in samples:
                targets = social_manager.targets_for(sample_kind)
                if not targets:
                    continue
                for target in targets:
                    try:
                        await asyncio.to_thread(
                            social_manager.publish,
                            target,
                            text,
                            card,
                            f"test-{sample_kind}",
                        )
                    except Exception as exc:  # noqa: BLE001
                        social_report.append(
                            f"{target} {sample_kind}: failed "
                            f"({str(exc)[:160]})"
                        )
                    else:
                        social_report.append(f"{target} {sample_kind}: sent")

            if not social_report:
                social_report.append(
                    "No social target accepts this message type — check "
                    "X_POST_MODE and BLUESKY_POST_MODE in .env."
                )

    message = (
        f"✅ Test message sent to {channel.mention} "
        f"({kind_labels[selected]})."
    )
    if social_report:
        message += "\n\n" + "\n".join(f"• {line}" for line in social_report)

    await interaction.followup.send(message[:2000], ephemeral=True)


@tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    original = error.original if isinstance(error, app_commands.CommandInvokeError) else error

    if isinstance(original, ValueError):
        message = str(original)
    elif isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Server** permission to use this."
    elif isinstance(error, app_commands.CheckFailure):
        message = "This command can only be used inside a server."
    else:
        command_name = getattr(interaction.command, "name", None)
        log.error(
            "Command /%s failed: %s",
            command_name or interaction.command,
            original,
            exc_info=original,
        )
        message = "Something went wrong. Check the bot logs for details."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.debug("Could not deliver command error to user", exc_info=True)


async def run_preview(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=max(1, args.hours))
    try:
        events = await service.events_for_range(now.date(), end.date())
    except Exception as exc:
        print(f"Could not load the calendar: {exc}")
        return 1

    currencies = (
        parse_pair(args.pair)
        if args.pair
        else parse_currency_list(args.currencies)
        if args.currencies
        else None
    )
    selected = filter_events(
        [event for event in events if now <= event.when <= end],
        currencies,
        args.impact,
    )

    print(f"Loaded {len(events)} events (feed + fallback where needed)")
    print(f"Upcoming in {args.hours}h: {len(selected)} matching event(s)\n")
    for event in selected:
        print(
            f"{event.when:%Y-%m-%d %H:%M UTC}  {event.impact.upper():7}  "
            f"{event.currency}  {event.title}"
        )
        if event.forecast or event.previous:
            print(
                f"    forecast: {event.forecast or '—'}   "
                f"previous: {event.previous or '—'}"
            )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ForexFactory Discord alerts bot")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Print upcoming calendar events and exit (no Discord token needed)",
    )
    parser.add_argument("--hours", type=int, default=24, help="Preview window in hours")
    parser.add_argument("--pair", help="Preview filter, e.g. EURUSD")
    parser.add_argument("--currencies", help="Preview filter, e.g. USD,EUR")
    parser.add_argument(
        "--impact",
        choices=["all", "low", "medium", "high"],
        default="high",
        help="Preview minimum impact",
    )
    args = parser.parse_args()

    if args.preview:
        raise SystemExit(asyncio.run(run_preview(args)))

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token."
        )

    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
