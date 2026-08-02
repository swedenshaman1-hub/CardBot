import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import TelegramError

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("CHANNEL_ID", "@test_channel")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

import bot
import database as db


class SpreadSelectionTests(unittest.TestCase):
    def setUp(self):
        self.values = {}
        self.get_setting = patch.object(
            db, "get_setting", side_effect=lambda key: self.values.get(key)
        )
        self.set_setting = patch.object(
            db,
            "set_setting",
            side_effect=lambda key, value: self.values.__setitem__(key, value),
        )
        self.get_setting.start()
        self.set_setting.start()

    def tearDown(self):
        self.get_setting.stop()
        self.set_setting.stop()

    def test_only_two_different_cards_are_allowed_per_spread(self):
        first = db.claim_spread_selection(10, 100, 2)
        repeated = db.claim_spread_selection(10, 100, 2)
        second = db.claim_spread_selection(10, 100, 5)
        third = db.claim_spread_selection(10, 100, 6)

        self.assertTrue(first["allowed"] and first["is_new"])
        self.assertTrue(repeated["allowed"] and not repeated["is_new"])
        self.assertTrue(second["allowed"] and second["is_new"])
        self.assertFalse(third["allowed"])
        self.assertEqual(third["selections"], [2, 5])

    def test_a_new_spread_has_a_new_limit(self):
        db.claim_spread_selection(10, 100, 1)
        db.claim_spread_selection(10, 100, 2)
        result = db.claim_spread_selection(11, 100, 3)
        self.assertTrue(result["allowed"] and result["is_new"])


class MembershipTests(unittest.TestCase):
    def test_active_members_are_allowed(self):
        for status in ("creator", "administrator", "member"):
            with self.subTest(status=status):
                self.assertTrue(bot._member_has_channel_access(SimpleNamespace(status=status)))

    def test_non_members_are_denied(self):
        for status in ("left", "kicked"):
            with self.subTest(status=status):
                self.assertFalse(bot._member_has_channel_access(SimpleNamespace(status=status)))

    def test_restricted_member_must_still_belong_to_channel(self):
        self.assertTrue(
            bot._member_has_channel_access(
                SimpleNamespace(status="restricted", is_member=True)
            )
        )
        self.assertFalse(
            bot._member_has_channel_access(
                SimpleNamespace(status="restricted", is_member=False)
            )
        )


class SpreadPublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handle, self.collage_path = tempfile.mkstemp(suffix=".jpg")
        os.close(handle)
        Path(self.collage_path).write_bytes(b"test image")
        self.telegram = SimpleNamespace(
            send_photo=AsyncMock(
                return_value=SimpleNamespace(message_id=501)
            ),
            send_voice=AsyncMock(
                return_value=SimpleNamespace(message_id=502)
            ),
            delete_message=AsyncMock(),
        )
        self.application = SimpleNamespace(bot=self.telegram, bot_data={})
        self.spread = {"id": 42, "card_ids": [3, 25, 36, 48, 71, 104]}

    async def test_shared_publication_keeps_caption_and_pick_keyboard(self):
        with (
            patch.object(bot.db, "get_card_back_url", return_value=None),
            patch.object(bot, "build_collage", return_value=self.collage_path),
            patch.object(bot.db, "set_setting"),
            patch.object(bot.db, "get_published_spreads", return_value=[]),
            patch.object(bot.db, "update_spread_message"),
            patch.object(bot, "record_analytics_event", new=AsyncMock()),
            patch.object(bot, "schedule_spread_deletion", new=MagicMock()),
        ):
            await bot.publish_spread_to_channel(
                self.application, 42, self.spread, "voice-file-id"
            )

        photo_call = self.telegram.send_photo.await_args.kwargs
        self.assertEqual(photo_call["caption"], bot.spread_caption())
        callbacks = [
            button.callback_data
            for row in photo_call["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(
            callbacks,
            [f"pick:42:{position}" for position in range(1, 7)],
        )
        self.telegram.send_voice.assert_awaited_once_with(
            chat_id=bot.CHANNEL_ID,
            voice="voice-file-id",
            caption="🎙 Моё личное послание к сегодняшним картам",
        )

    async def test_voice_failure_rolls_back_photo(self):
        self.telegram.send_voice.side_effect = TelegramError("voice failed")
        with (
            patch.object(bot.db, "get_card_back_url", return_value=None),
            patch.object(bot, "build_collage", return_value=self.collage_path),
        ):
            with self.assertRaises(bot.AuthorVoicePublishError):
                await bot.publish_spread_to_channel(
                    self.application, 42, self.spread, "voice-file-id"
                )

        self.telegram.delete_message.assert_awaited_once_with(
            chat_id=bot.CHANNEL_ID,
            message_id=501,
        )


if __name__ == "__main__":
    unittest.main()
