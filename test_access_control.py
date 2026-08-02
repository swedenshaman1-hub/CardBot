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
        self.spread = {
            "id": 42,
            "card_ids": [3, 25, 36, 48, 71, 104],
            "question": None,
        }

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

    async def test_shared_publication_includes_question(self):
        self.spread["question"] = "Что вы готовы увидеть иначе?"
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
        self.assertIn(
            "❓ *Вопрос дня*\nЧто вы готовы увидеть иначе?",
            self.telegram.send_photo.await_args.kwargs["caption"],
        )


class SpreadQuestionCaptionTests(unittest.TestCase):
    def test_spread_without_question_keeps_exact_caption(self):
        expected = (
            "🔮 *Карты дня*\n\n"
            "Сегодня я выбрал для вас 6 метафорических карт.\n"
            "Посмотрите на них и почувствуйте, какая карта сейчас откликается именно вам.\n\n"
            "Чтобы получить своё послание дня, подпишитесь на канал и нажмите номер выбранной карты. Telegram откроет бота автоматически.\n"
            "Описание карты придёт вам в личные сообщения от бота.\n\n"
            "В каждой новой публикации вы можете открывать для себя две карты.\n\n"
            "Если вам откликнулось послание, оставьте реакцию — пусть это будет наш энергообмен."
        )
        self.assertEqual(bot.spread_caption(), expected)

    def test_question_is_between_intro_and_card_call_to_action(self):
        caption = bot.spread_caption("Что для тебя сейчас действительно важно?")
        self.assertLess(caption.index("Посмотрите на них"), caption.index("❓ *Вопрос дня*"))
        self.assertLess(caption.index("❓ *Вопрос дня*"), caption.index("Чтобы получить"))

    def test_question_at_limit_keeps_caption_within_telegram_limit(self):
        question = "В" * (bot.SPREAD_QUESTION_MAX_LENGTH - 1) + "?"
        self.assertLessEqual(
            len(bot.spread_caption(question)),
            bot.TELEGRAM_CAPTION_MAX_LENGTH,
        )

    def test_question_over_limit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "70"):
            bot.normalize_spread_question("В" * 71)

    def test_markdown_special_characters_are_escaped(self):
        caption = bot.spread_caption("Что *важно* в [этом]_дне_ и `сейчас`?")
        self.assertIn(r"\*важно\*", caption)
        self.assertIn(r"\[этом\]\_дне\_", caption)
        self.assertIn(r"\`сейчас\`", caption)


class SpreadQuestionDatabaseTests(unittest.TestCase):
    def test_save_spread_without_question_omits_nullable_field(self):
        client = MagicMock()
        table = client.table.return_value
        table.insert.return_value.execute.return_value = SimpleNamespace(data=[{"id": 7}])
        with patch.object(db, "get_client", return_value=client):
            spread_id = db.save_spread([1, 2, 3, 4, 5, 6])
        self.assertEqual(spread_id, 7)
        payload = table.insert.call_args.args[0]
        self.assertNotIn("question", payload)

    def test_get_spread_supports_old_row_without_question(self):
        client = MagicMock()
        query = client.table.return_value.select.return_value.eq.return_value.limit.return_value
        query.execute.return_value = SimpleNamespace(data=[{
            "id": 7,
            "created_at": "2026-08-02T00:00:00+00:00",
            "card_ids": [1, 2, 3, 4, 5, 6],
            "channel_message_id": None,
        }])
        with patch.object(db, "get_client", return_value=client):
            spread = db.get_spread(7)
        self.assertIsNone(spread["question"])

    def test_update_spread_question_sets_text_and_none(self):
        client = MagicMock()
        table = client.table.return_value
        with patch.object(db, "get_client", return_value=client):
            db.update_spread_question(7, "Что сейчас важно?")
            db.update_spread_question(7, None)
        self.assertEqual(
            [call.args[0] for call in table.update.call_args_list],
            [{"question": "Что сейчас важно?"}, {"question": None}],
        )


class SpreadQuestionInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_over_limit_question_is_rejected_during_input(self):
        message = SimpleNamespace(
            text="В" * 71,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=bot.ADMIN_ID),
            effective_chat=SimpleNamespace(id=bot.ADMIN_ID),
        )
        context = SimpleNamespace(user_data={
            "pending_spread_question_input": True,
            "pending_spread_question_id": 7,
        })
        with patch.object(db, "update_spread_question") as update_question:
            await bot.handle_private_message(update, context)
        update_question.assert_not_called()
        self.assertTrue(context.user_data["pending_spread_question_input"])
        self.assertIn("70", message.reply_text.await_args.args[0])

    async def test_text_topic_generates_and_saves_question(self):
        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            text="отношения",
            reply_text=AsyncMock(return_value=status),
        )
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=bot.ADMIN_ID),
            effective_chat=SimpleNamespace(id=bot.ADMIN_ID),
        )
        context = SimpleNamespace(user_data={
            "pending_spread_question_topic": True,
            "pending_spread_question_topic_id": 7,
        })
        with patch.object(
            bot,
            "generate_spread_question_for_topic",
            new=AsyncMock(return_value="Что сейчас важно увидеть в ваших отношениях?"),
        ) as generate:
            await bot.handle_private_message(update, context)
        generate.assert_awaited_once_with(7, "отношения")
        self.assertNotIn("pending_spread_question_topic", context.user_data)
        self.assertIn(
            "Короткий вопрос по вашей теме сохранён",
            status.edit_text.await_args.args[0],
        )

    def test_question_keyboard_separates_topic_and_ready_question(self):
        labels = [
            button.text
            for row in bot.spread_question_keyboard(7).inline_keyboard
            for button in row
        ]
        self.assertIn("🎯 Предложить по моей теме", labels)
        self.assertIn("✍️ Ввести готовый вопрос", labels)


if __name__ == "__main__":
    unittest.main()
