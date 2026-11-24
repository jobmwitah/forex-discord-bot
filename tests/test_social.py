"""Tests for the free social publishers and their alert-loop hook."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

# Set DATA_DIR before importing the bot so tests never touch live state.
_TEMP_DATA = tempfile.mkdtemp(prefix="forex-bot-tests-")
os.environ["DATA_DIR"] = _TEMP_DATA

import bot as bot_module  # noqa: E402
import social  # noqa: E402
from calendar_source import Event  # noqa: E402


def _event(
    event_id: str,
    when: datetime,
    *,
    currency: str = "USD",
    actual: str = "",
) -> Event:
    return Event(
        id=event_id,
        title=f"Test event {event_id}",
        currency=currency,
        when=when,
        impact="high",
        forecast="0.2%",
        previous="0.1%",
        actual=actual,
    )


class FormattingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event = _event("e1", datetime(2026, 11, 24, 13, 30, tzinfo=timezone.utc))

    def test_reminder_text_contains_values_and_tags(self) -> None:
        text = social.reminder_text(
            self.event, 15, ZoneInfo("UTC"), test=True
        )
        self.assertIn("USD", text)
        self.assertIn("Forecast 0.2%", text)
        self.assertIn("#USD #Forex #EconomicCalendar", text)
        self.assertIn("(test post)", text)
        self.assertLessEqual(len(text), social.X_LIMIT)

    def test_summary_text_uses_actual_and_forecast(self) -> None:
        text = social.summary_text(
            replace(self.event, actual="0.4%"), ZoneInfo("UTC")
        )
        self.assertIn("Actual 0.4% vs forecast 0.2%", text)
        self.assertIn("Better than expected", text)
        self.assertLessEqual(len(text), social.BLUESKY_LIMIT)

    def test_clip_never_exceeds_limit(self) -> None:
        clipped = social._clip("x" * 100, 20)
        self.assertEqual(len(clipped), 20)
        self.assertTrue(clipped.endswith("…"))

    def test_hashtag_facets_use_utf8_byte_offsets(self) -> None:
        text = "📊 USD · CPI\n#USD #Forex"
        facets = social._hashtag_facets(text)
        self.assertEqual(len(facets), 2)
        start = len("📊 USD · CPI\n".encode("utf-8"))
        self.assertEqual(facets[0]["index"]["byteStart"], start)
        self.assertEqual(facets[0]["features"][0]["tag"], "USD")
        self.assertEqual(facets[1]["features"][0]["tag"], "Forex")


class ConfigTests(unittest.TestCase):
    def test_defaults_are_conservative(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in (
                "X_POST_MODE",
                "BLUESKY_POST_MODE",
                "SOCIAL_CURRENCIES",
                "SOCIAL_REMINDER_MINUTES",
                "SOCIAL_TIMEZONE",
                "SOCIAL_MIN_IMPACT",
                "BUFFER_API_KEY",
                "BUFFER_CHANNEL_ID",
                "BLUESKY_HANDLE",
                "BLUESKY_APP_PASSWORD",
            ):
                os.environ.pop(name, None)
            cfg = social.load_social_config()
        self.assertEqual(cfg.x_mode, "reminder")
        self.assertEqual(cfg.bluesky_mode, "all")
        self.assertEqual(cfg.reminder_minutes, [15])
        self.assertEqual(cfg.min_impact, "high")
        self.assertFalse(cfg.currencies)
        manager = social.SocialManager(cfg)
        self.assertFalse(manager.active)
        self.assertEqual(manager.describe(), "off")

    def test_only_configured_targets_are_active(self) -> None:
        cfg = social.SocialConfig(
            x_mode="reminder",
            bluesky_mode="all",
            buffer_api_key="key",
            buffer_channel_id="channel",
            bluesky_handle="name.bsky.social",
            bluesky_password="app-password",
        )
        manager = social.SocialManager(cfg)
        self.assertTrue(manager.active)
        self.assertEqual(manager.targets_for("reminder"), ["x", "bluesky"])
        self.assertEqual(manager.targets_for("summary"), ["bluesky"])
        self.assertIn("X via Buffer (reminder)", manager.describe())
        self.assertNotIn("key", manager.describe())
        self.assertNotIn("app-password", manager.describe())


class BufferTests(unittest.TestCase):
    def test_create_post_uses_share_now_graphql(self) -> None:
        publisher = social.BufferPublisher("secret-key", "channel-id")
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "data": {"createPost": {"post": {"id": "post-1"}}}
        }
        with mock.patch.object(
            social.requests, "post", return_value=response
        ) as request:
            post_id = publisher.publish('Hello "quotes"')

        self.assertEqual(post_id, "post-1")
        payload = request.call_args.kwargs["json"]
        self.assertIn("mode: shareNow", payload["query"])
        self.assertIn('channelId: "channel-id"', payload["query"])
        self.assertIn('text: "Hello \\"quotes\\""', payload["query"])
        self.assertEqual(
            request.call_args.kwargs["headers"]["Authorization"],
            "Bearer secret-key",
        )

    def test_graphql_error_is_raised(self) -> None:
        publisher = social.BufferPublisher("key", "channel")
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "errors": [{"message": "rate limited"}]
        }
        with mock.patch.object(social.requests, "post", return_value=response):
            with self.assertRaises(social.SocialError):
                publisher.publish("hello")


class FakeManager:
    active = True

    def __init__(self) -> None:
        self.config = social.SocialConfig(
            timezone="UTC",
            min_impact="high",
            reminder_minutes=[15],
        )
        self.posts: list[tuple[str, str]] = []

    def targets_for(self, kind: str) -> list[str]:
        return {
            "reminder": ["x"],
            "release": ["bluesky"],
            "summary": ["bluesky"],
        }.get(kind, [])

    def publish(self, target: str, text: str) -> str:
        self.posts.append((target, text))
        return "fake-id"


class FakeService:
    def __init__(self, events: list[Event]) -> None:
        self.events = events
        self.details_calls = 0

    async def events_for_range(self, start, end) -> list[Event]:
        return self.events

    async def event_details(self, event: Event) -> Event:
        self.details_calls += 1
        return replace(event, actual="0.4%")


class FakeState:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def seen(self, key: str) -> bool:
        return key in self.keys

    def mark(self, key: str) -> None:
        self.keys.add(key)


class AlertTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_posts_each_kind_once_and_persists_keys(self) -> None:
        now = datetime(2026, 11, 24, 13, 30, tzinfo=timezone.utc)
        events = [
            _event("rem", now + timedelta(minutes=15)),
            _event("rel", now, currency="EUR"),
            _event("sum", now - timedelta(minutes=2), currency="GBP"),
        ]
        manager = FakeManager()
        service = FakeService(events)
        state = FakeState()

        with (
            mock.patch.object(bot_module, "social_manager", manager),
            mock.patch.object(bot_module, "service", service),
            mock.patch.object(bot_module, "state", state),
        ):
            instance = bot_module.ForexBot()
            await instance._social_tick(now)

            self.assertEqual(
                [target for target, _ in manager.posts],
                ["x", "bluesky", "bluesky"],
            )
            self.assertIn("in 15 min", manager.posts[0][1])
            self.assertIn("released", manager.posts[1][1])
            self.assertIn("Actual 0.4%", manager.posts[2][1])
            self.assertEqual(service.details_calls, 1)

            expected_keys = {
                "social:x:rem:rem:15",
                "social:bluesky:release:rel",
                "social:bluesky:summary:sum",
            }
            self.assertEqual(state.keys, expected_keys)

            # A second tick must not repeat anything.
            await instance._social_tick(now)
            self.assertEqual(len(manager.posts), 3)

    async def test_failed_target_is_retried_after_cooldown(self) -> None:
        now = datetime(2026, 11, 24, 13, 30, tzinfo=timezone.utc)
        events = [_event("rel", now)]

        class FailingManager(FakeManager):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            def targets_for(self, kind: str) -> list[str]:
                return ["bluesky"] if kind == "release" else []

            def publish(self, target: str, text: str) -> str:
                self.attempts += 1
                if self.attempts == 1:
                    raise social.SocialError("temporary")
                return super().publish(target, text)

        manager = FailingManager()
        with (
            mock.patch.object(bot_module, "social_manager", manager),
            mock.patch.object(bot_module, "service", FakeService(events)),
            mock.patch.object(bot_module, "state", FakeState()),
        ):
            instance = bot_module.ForexBot()
            await instance._social_tick(now)
            self.assertEqual(manager.attempts, 1)

            await instance._social_tick(now)
            self.assertEqual(manager.attempts, 1)  # still cooling down

            # Expire the cooldown without sleeping.
            for key in list(instance._social_cooldowns):
                instance._social_cooldowns[key] = 0.0
            await instance._social_tick(now)
            self.assertEqual(manager.attempts, 2)
            self.assertEqual(len(manager.posts), 1)


if __name__ == "__main__":
    unittest.main()
