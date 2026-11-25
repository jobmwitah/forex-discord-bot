"""Tests for the free social publishers and their alert-loop hook."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
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
        self.assertIn("#USD #Forex #Pipwire", text)
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
                "SOCIAL_IMAGES",
                "SOCIAL_BRAND_NAME",
                "SOCIAL_BRAND_HANDLE",
                "CLOUDINARY_CLOUD_NAME",
                "CLOUDINARY_API_KEY",
                "CLOUDINARY_API_SECRET",
            ):
                os.environ.pop(name, None)
            cfg = social.load_social_config()
        self.assertEqual(cfg.x_mode, "reminder")
        self.assertEqual(cfg.bluesky_mode, "all")
        self.assertEqual(cfg.reminder_minutes, [15])
        self.assertEqual(cfg.min_impact, "high")
        self.assertTrue(cfg.images_enabled)
        self.assertEqual(cfg.brand_name, "Pipwire")
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

    def test_image_url_is_sent_as_a_buffer_asset(self) -> None:
        publisher = social.BufferPublisher("key", "channel")
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "data": {"createPost": {"post": {"id": "post-2"}}}
        }
        url = "https://res.cloudinary.com/demo/image/upload/card.png"
        with mock.patch.object(
            social.requests, "post", return_value=response
        ) as request:
            publisher.publish("With card", image_url=url)

        query = request.call_args.kwargs["json"]["query"]
        self.assertIn(f'assets: [{{ image: {{ url: "{url}" }} }}]', query)

    def test_text_only_post_has_no_assets_field(self) -> None:
        publisher = social.BufferPublisher("key", "channel")
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "data": {"createPost": {"post": {"id": "post-3"}}}
        }
        with mock.patch.object(
            social.requests, "post", return_value=response
        ) as request:
            publisher.publish("No card")

        self.assertNotIn("assets:", request.call_args.kwargs["json"]["query"])


def _card(data: bytes = b"png-bytes") -> "social.CardImage":
    from graphics import CardImage

    return CardImage(
        data=data,
        mime="image/png",
        width=1200,
        height=675,
        alt="USD CPI forecast",
    )


class BlueskyImageTests(unittest.TestCase):
    def _publisher(self) -> social.BlueskyPublisher:
        publisher = social.BlueskyPublisher(
            "name.bsky.social", "app-password", "https://bsky.social"
        )
        publisher._token = "access-token"
        publisher._did = "did:plc:test"
        publisher._expires = 4102444800.0
        return publisher

    def test_uploads_blob_and_adds_image_embed(self) -> None:
        publisher = self._publisher()
        blob = {"$type": "blob", "ref": {"$link": "cid"}, "size": 9}
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "uri": "at://did:plc:test/app.bsky.feed.post/1"
        }
        with (
            mock.patch.object(
                publisher, "_upload_blob", return_value=blob
            ) as upload,
            mock.patch.object(
                social.requests, "post", return_value=response
            ) as request,
        ):
            publisher.publish("CPI is out", image=_card())

        upload.assert_called_once_with(b"png-bytes", "image/png")
        record = request.call_args.kwargs["json"]["record"]
        self.assertEqual(record["embed"]["$type"], "app.bsky.embed.images")
        self.assertEqual(record["embed"]["images"][0]["image"], blob)
        self.assertEqual(
            record["embed"]["images"][0]["aspectRatio"],
            {"width": 1200, "height": 675},
        )

    def test_oversized_image_falls_back_to_text(self) -> None:
        publisher = self._publisher()
        response = mock.Mock(status_code=200)
        response.json.return_value = {}
        with (
            mock.patch.object(publisher, "_upload_blob") as upload,
            mock.patch.object(
                social.requests, "post", return_value=response
            ) as request,
        ):
            publisher.publish(
                "Too big", image=_card(b"x" * 1_000_001)
            )

        upload.assert_not_called()
        record = request.call_args.kwargs["json"]["record"]
        self.assertNotIn("embed", record)

    def test_upload_failure_falls_back_to_text(self) -> None:
        publisher = self._publisher()
        response = mock.Mock(status_code=200)
        response.json.return_value = {}
        with (
            mock.patch.object(
                publisher,
                "_upload_blob",
                side_effect=social.SocialError("blob rejected"),
            ),
            mock.patch.object(
                social.requests, "post", return_value=response
            ) as request,
        ):
            publisher.publish("Fallback", image=_card())

        record = request.call_args.kwargs["json"]["record"]
        self.assertNotIn("embed", record)


class CloudinaryTests(unittest.TestCase):
    def test_upload_signs_parameters_and_returns_secure_url(self) -> None:
        host = social.CloudinaryHost("demo", "api-key", "api-secret")
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "secure_url": "https://res.cloudinary.com/demo/image/upload/card.png"
        }
        with mock.patch.object(
            social.requests, "post", return_value=response
        ) as request:
            url = host.upload(_card(), public_id="rem:USD:1")

        self.assertEqual(
            url, "https://res.cloudinary.com/demo/image/upload/card.png"
        )
        payload = request.call_args.kwargs["data"]
        self.assertEqual(payload["folder"], "pipwire")
        self.assertEqual(payload["public_id"], "rem-USD-1")
        self.assertEqual(payload["api_key"], "api-key")
        expected = "&".join(
            f"{key}={payload[key]}"
            for key in sorted(
                ("folder", "overwrite", "public_id", "timestamp")
            )
        )
        expected_signature = social.hashlib.sha1(
            (expected + "api-secret").encode("utf-8")
        ).hexdigest()
        self.assertEqual(payload["signature"], expected_signature)
        self.assertIn("/v1_1/demo/image/upload", request.call_args.args[0])
        filename, data, mime = request.call_args.kwargs["files"]["file"]
        self.assertEqual(filename, "rem-USD-1.png")
        self.assertEqual(data, b"png-bytes")
        self.assertEqual(mime, "image/png")


class SocialManagerImageTests(unittest.TestCase):
    def _manager(self) -> social.SocialManager:
        cfg = social.SocialConfig(
            x_mode="reminder",
            bluesky_mode="all",
            images_enabled=True,
            buffer_api_key="key",
            buffer_channel_id="channel",
            bluesky_handle="name.bsky.social",
            bluesky_password="app-password",
            cloudinary_cloud_name="demo",
            cloudinary_api_key="api-key",
            cloudinary_api_secret="api-secret",
        )
        manager = social.SocialManager(cfg)
        manager.cloudinary = mock.Mock()
        manager.cloudinary.upload.return_value = "https://img.example/card.png"
        return manager

    def test_routes_urls_to_x_and_blobs_to_bluesky(self) -> None:
        manager = self._manager()
        card = _card()
        with (
            mock.patch.object(
                manager.publishers["x"], "publish", return_value="x-1"
            ) as x_publish,
            mock.patch.object(
                manager.publishers["bluesky"],
                "publish",
                return_value="b-1",
            ) as b_publish,
        ):
            manager.publish("x", "text", image=card, image_id="rem:1")
            manager.publish("bluesky", "text", image=card, image_id="rem:1")

        manager.cloudinary.upload.assert_called_once()
        upload_args, upload_kwargs = manager.cloudinary.upload.call_args
        self.assertIs(upload_args[0], card)
        self.assertTrue(upload_kwargs["public_id"].startswith("rem:1-"))
        x_publish.assert_called_once_with(
            "text", image_url="https://img.example/card.png"
        )
        b_publish.assert_called_once_with("text", image=card)

    def test_cloudinary_failure_still_posts_to_x(self) -> None:
        manager = self._manager()
        manager.cloudinary.upload.side_effect = social.SocialError("down")
        with mock.patch.object(
            manager.publishers["x"], "publish", return_value="x-1"
        ) as x_publish:
            manager.publish("x", "text", image=_card(), image_id="rem:1")

        x_publish.assert_called_once_with("text", image_url=None)

    def test_describe_reports_image_capability_without_secrets(self) -> None:
        manager = self._manager()
        status = manager.describe()
        self.assertIn("images", status)
        self.assertNotIn("api-secret", status)


class GraphicsTests(unittest.TestCase):
    def test_reminder_card_is_1200x675_and_within_bluesky_limit(self) -> None:
        from PIL import Image

        from graphics import HEIGHT, WIDTH, render_card

        event = _event(
            "g1",
            datetime(2026, 11, 25, 0, 30, tzinfo=timezone.utc),
            currency="AUD",
        )
        card = render_card(event, "reminder", ZoneInfo("UTC"), minutes=15)

        self.assertEqual((card.width, card.height), (WIDTH, HEIGHT))
        self.assertEqual((WIDTH, HEIGHT), (1200, 675))
        self.assertEqual(card.mime, "image/png")
        self.assertLessEqual(len(card.data), 1_000_000)
        image = Image.open(BytesIO(card.data))
        self.assertEqual(image.size, (1200, 675))
        self.assertIn("AUD", card.alt)
        self.assertIn("15 minutes", card.alt)

    def test_long_title_card_still_renders(self) -> None:
        from graphics import render_card

        event = replace(
            _event(
                "g2",
                datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc),
                currency="USD",
            ),
            title=(
                "A very long economic indicator name that has to wrap onto "
                "several lines without leaving the card"
            ),
        )
        card = render_card(event, "summary", ZoneInfo("UTC"))
        self.assertGreater(len(card.data), 1000)


class FakeManager:
    active = True
    images_enabled = False

    def __init__(self) -> None:
        self.config = social.SocialConfig(
            timezone="UTC",
            min_impact="high",
            reminder_minutes=[15],
        )
        self.posts: list[tuple[str, str]] = []
        self.images: list[object] = []

    def targets_for(self, kind: str) -> list[str]:
        return {
            "reminder": ["x"],
            "release": ["bluesky"],
            "summary": ["bluesky"],
        }.get(kind, [])

    def publish(
        self,
        target: str,
        text: str,
        image=None,
        image_id: str = "",
    ) -> str:
        self.posts.append((target, text))
        self.images.append(image)
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

    async def test_tick_renders_cards_when_images_are_enabled(self) -> None:
        now = datetime(2026, 11, 24, 13, 30, tzinfo=timezone.utc)
        events = [
            _event("rem", now + timedelta(minutes=15)),
            _event("rel", now, currency="EUR"),
            _event("sum", now - timedelta(minutes=2), currency="GBP"),
        ]
        manager = FakeManager()
        manager.images_enabled = True
        sentinel = object()

        with (
            mock.patch.object(bot_module, "social_manager", manager),
            mock.patch.object(bot_module, "service", FakeService(events)),
            mock.patch.object(bot_module, "state", FakeState()),
            mock.patch.object(
                bot_module, "render_card", return_value=sentinel
            ) as render,
        ):
            instance = bot_module.ForexBot()
            await instance._social_tick(now)

        self.assertEqual(render.call_count, 3)
        self.assertEqual(manager.images, [sentinel, sentinel, sentinel])

    async def test_failed_target_is_retried_after_cooldown(self) -> None:
        now = datetime(2026, 11, 24, 13, 30, tzinfo=timezone.utc)
        events = [_event("rel", now)]

        class FailingManager(FakeManager):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            def targets_for(self, kind: str) -> list[str]:
                return ["bluesky"] if kind == "release" else []

            def publish(self, target: str, text: str, image=None, image_id: str = "") -> str:
                self.attempts += 1
                if self.attempts == 1:
                    raise social.SocialError("temporary")
                return super().publish(target, text, image, image_id)

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
