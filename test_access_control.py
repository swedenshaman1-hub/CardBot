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
import dmitry_voice


VALID_DMITRY_SCRIPT = """Сначала почувствуй, потом решай

Иногда голова торопится всё объяснить и заранее разложить по местам. А тело уже знает, где вам спокойно, а где внутри появляется напряжение. Не обязательно спорить с собой или немедленно искать правильный ответ. Можно просто заметить эту разницу и немного побыть рядом с ней.

Что меняется, когда вы не подгоняете себя? Сегодня попробуйте оставить немного пространства между первым импульсом и решением. Иногда именно в этой короткой паузе становится слышно то, что действительно важно."""


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

    def test_failed_delivery_releases_reserved_choice(self):
        db.claim_spread_selection(10, 100, 2)
        self.assertTrue(db.release_spread_selection(10, 100, 2))
        self.assertEqual(db.get_spread_selections(10, 100), [])
        retry = db.claim_spread_selection(10, 100, 2)
        self.assertTrue(retry["allowed"] and retry["is_new"])


class CardDeliveryAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_delivery_failure_releases_choice_and_records_reason(self):
        query = SimpleNamespace(
            data="pick:42:1",
            from_user=SimpleNamespace(id=55),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, update_id=9001)
        context = SimpleNamespace(
            bot=SimpleNamespace(send_chat_action=AsyncMock()),
        )
        spread = {"id": 42, "card_ids": [3, 4, 5, 6, 7, 8]}

        with (
            patch.object(bot.db, "get_spread", return_value=spread),
            patch.object(bot, "require_channel_subscription", new=AsyncMock(return_value=(True, None))),
            patch.object(
                bot.db,
                "claim_spread_selection",
                return_value={"allowed": True, "is_new": True, "selections": [1]},
            ),
            patch.object(bot.db, "release_spread_selection", return_value=True) as release,
            patch.object(bot, "send_card_to_chat", new=AsyncMock(side_effect=RuntimeError("send failed"))),
            patch.object(bot, "record_analytics_event", new=AsyncMock()) as record,
        ):
            await bot.select_card_callback(update, context)

        release.assert_called_once_with(42, 55, 1)
        event_types = [call.kwargs["event_type"] for call in record.await_args_list]
        self.assertIn("card_button_clicked", event_types)
        self.assertIn("card_delivery_failed", event_types)
        self.assertNotIn("card_delivery_succeeded", event_types)

    def test_attempt_summary_prefers_eventual_delivery_after_start_redirect(self):
        rows = [
            {
                "event_type": "card_button_clicked",
                "actor_hash": "actor",
                "metadata": {"attempt_id": "100"},
            },
            {
                "event_type": "card_rejected_bot_not_started",
                "actor_hash": "actor",
                "metadata": {"attempt_id": "100"},
            },
            {
                "event_type": "card_delivery_succeeded",
                "actor_hash": "actor",
                "metadata": {"attempt_id": "100"},
            },
        ]
        summary = db._summarise_events(rows)
        self.assertEqual(summary["tracked_button_attempts"], 1)
        self.assertEqual(summary["button_outcome_counts"], {"delivered": 1})


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


class DmitryVoiceProfileTests(unittest.TestCase):
    def test_profile_is_versioned_and_contains_brand_boundaries(self):
        self.assertEqual(dmitry_voice.DMITRY_VOICE_PROFILE_VERSION, "v2")
        profile = dmitry_voice.DMITRY_VOICE_PROFILE.lower()
        self.assertIn("простая разговорная речь", profile)
        self.assertIn("одну ясную мысль", profile)
        self.assertIn("это про меня", profile)
        self.assertIn("не рекламный ролик", profile)
        self.assertIn("не называть дмитрия терапевтом", profile)
        self.assertIn("не упоминать карты", profile)

    def test_validator_accepts_natural_spoken_structure(self):
        self.assertEqual(dmitry_voice.validate_voice_script(VALID_DMITRY_SCRIPT), [])

    def test_validator_rejects_cards_and_healing_claims(self):
        invalid = VALID_DMITRY_SCRIPT.replace(
            "Иногда голова",
            "Эта карта принесёт исцеление, и жизнь обязательно изменится. Иногда голова",
        )
        errors = dmitry_voice.validate_voice_script(invalid)
        self.assertIn("упоминание карт", errors)
        self.assertIn("обещание исцеления", errors)
        self.assertIn("ложная гарантия", errors)

    def test_generator_uses_draft_and_editor_with_lower_variance(self):
        draft = VALID_DMITRY_SCRIPT.replace("важно", "по-настоящему важно")
        responses = [SimpleNamespace(text=draft), SimpleNamespace(text=VALID_DMITRY_SCRIPT)]
        generate = MagicMock(side_effect=responses)
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        cards = [{"id": index, "meaning": f"Смысл {index}"} for index in range(1, 7)]

        with patch.object(bot.genai, "Client", return_value=client):
            result = bot._generate_spread_voice_script(cards, [])

        self.assertEqual(result, VALID_DMITRY_SCRIPT)
        self.assertEqual(generate.call_count, 2)
        first, second = generate.call_args_list
        self.assertIn(dmitry_voice.DMITRY_VOICE_PROFILE, first.kwargs["contents"])
        self.assertIn("узнаваемая жизненная ситуация", first.kwargs["contents"])
        self.assertIn("простой новый угол", first.kwargs["contents"])
        self.assertIn("проверить сегодня", first.kwargs["contents"])
        self.assertIn(dmitry_voice.DMITRY_VOICE_PROFILE, second.kwargs["contents"])
        self.assertIn("перепиши её проще", second.kwargs["contents"])
        self.assertEqual(first.kwargs["config"].temperature, 0.72)
        self.assertEqual(second.kwargs["config"].temperature, 0.35)

    def test_generator_falls_back_to_valid_draft_when_editor_fails(self):
        generate = MagicMock(
            side_effect=[SimpleNamespace(text=VALID_DMITRY_SCRIPT), RuntimeError("editor down")]
        )
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        cards = [{"id": index, "meaning": f"Смысл {index}"} for index in range(1, 7)]

        with patch.object(bot.genai, "Client", return_value=client):
            result = bot._generate_spread_voice_script(cards, [])

        self.assertEqual(result, VALID_DMITRY_SCRIPT)

    def test_generator_repairs_non_blocking_format_errors(self):
        malformed = VALID_DMITRY_SCRIPT.replace("\n\n", "\n").replace(
            "себя?", "себя? А что ты выберешь?"
        )
        repaired = VALID_DMITRY_SCRIPT
        generate = MagicMock(
            side_effect=[
                SimpleNamespace(text=malformed),
                SimpleNamespace(text=malformed),
                SimpleNamespace(text=repaired),
            ]
        )
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        cards = [{"id": index, "meaning": f"Смысл {index}"} for index in range(1, 7)]

        with patch.object(bot.genai, "Client", return_value=client):
            result = bot._generate_spread_voice_script(cards, [])

        self.assertEqual(result, repaired)
        self.assertEqual(generate.call_count, 3)
        self.assertIn("нарушения формата", generate.call_args_list[2].kwargs["contents"])

    def test_generator_keeps_safe_text_when_only_layout_remains_invalid(self):
        malformed = VALID_DMITRY_SCRIPT.replace("\n\n", "\n").replace(
            "себя?", "себя? А что ты выберешь?"
        )
        generate = MagicMock(
            side_effect=[SimpleNamespace(text=malformed)] * 3
        )
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        cards = [{"id": index, "meaning": f"Смысл {index}"} for index in range(1, 7)]

        with patch.object(bot.genai, "Client", return_value=client):
            result = bot._generate_spread_voice_script(cards, [])

        self.assertEqual(result, malformed)

    def test_generator_never_falls_back_to_unsafe_positioning(self):
        unsafe = VALID_DMITRY_SCRIPT.replace(
            "Иногда голова", "Как целитель я вижу: у тебя родовая программа. Иногда голова"
        ).replace("\n\n", "\n")
        generate = MagicMock(side_effect=[SimpleNamespace(text=unsafe)] * 3)
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        cards = [{"id": index, "meaning": f"Смысл {index}"} for index in range(1, 7)]

        with patch.object(bot.genai, "Client", return_value=client):
            with self.assertRaises(RuntimeError):
                bot._generate_spread_voice_script(cards, [])

    def test_generator_does_not_accept_arbitrary_length_as_cosmetic(self):
        too_short = "Слишком короткий текст без нужного смысла"
        generate = MagicMock(side_effect=[SimpleNamespace(text=too_short)] * 3)
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        cards = [{"id": index, "meaning": f"Смысл {index}"} for index in range(1, 7)]

        with patch.object(bot.genai, "Client", return_value=client):
            with self.assertRaises(RuntimeError):
                bot._generate_spread_voice_script(cards, [])


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
            patch.object(bot.db, "get_spread_engagement", return_value={}),
            patch.object(bot, "build_collage", return_value=self.collage_path),
            patch.object(bot.db, "set_setting"),
            patch.object(bot.db, "get_published_spreads", return_value=[]),
            patch.object(bot.db, "update_spread_message"),
            patch.object(bot, "record_analytics_event", new=AsyncMock()),
            patch.object(bot, "schedule_spread_deletion", new=MagicMock()),
            patch.object(bot, "notify_reminder_subscribers", new=AsyncMock()),
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
            patch.object(bot.db, "get_spread_engagement", return_value={}),
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
            patch.object(bot.db, "get_spread_engagement", return_value={}),
            patch.object(bot, "build_collage", return_value=self.collage_path),
            patch.object(bot.db, "set_setting"),
            patch.object(bot.db, "get_published_spreads", return_value=[]),
            patch.object(bot.db, "update_spread_message"),
            patch.object(bot, "record_analytics_event", new=AsyncMock()),
            patch.object(bot, "schedule_spread_deletion", new=MagicMock()),
            patch.object(bot, "notify_reminder_subscribers", new=AsyncMock()),
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


class AudienceGrowthTests(unittest.TestCase):
    def test_theme_is_added_without_changing_unthemed_caption(self):
        plain = bot.spread_caption()
        themed = bot.spread_caption(
            engagement={"theme": "Отношения и близость", "day": 3}
        )
        self.assertTrue(themed.startswith("📖 *Тема недели · День 3*"))
        self.assertIn("Отношения и близость", themed)
        self.assertIn(plain, themed)

    def test_reminder_subscription_can_be_enabled_and_disabled(self):
        values = {}
        with (
            patch.object(db, "get_setting", side_effect=lambda key: values.get(key)),
            patch.object(db, "set_setting", side_effect=lambda key, value: values.__setitem__(key, value)),
            patch.object(db, "delete_setting", side_effect=lambda key: values.pop(key, None)),
        ):
            db.set_reminder_subscription(55, True)
            self.assertTrue(db.is_reminder_subscriber(55))
            db.set_reminder_subscription(55, False)
            self.assertFalse(db.is_reminder_subscriber(55))

    def test_active_theme_advances_and_cycles_after_seven(self):
        values = {}
        with (
            patch.object(db, "get_setting", side_effect=lambda key: values.get(key)),
            patch.object(db, "set_setting", side_effect=lambda key, value: values.__setitem__(key, value)),
            patch.object(db, "delete_setting", side_effect=lambda key: values.pop(key, None)),
        ):
            db.set_active_weekly_theme("Отношения")
            attached = [db.attach_active_theme_to_spread(i) for i in range(1, 9)]
        self.assertEqual([item.get("day") for item in attached], [1, 2, 3, 4, 5, 6, 7, None])

    def test_weekly_summary_is_anonymous_and_uses_aggregates(self):
        text = bot.weekly_summary_text(42, {
            "event_counts": {"card_opened": 20},
            "reaction_counts": {"close": 6, "reflect": 2, "not_now": 2},
        })
        self.assertIn("20", text)
        self.assertIn("80%", text)
        self.assertNotIn("42", text)


if __name__ == "__main__":
    unittest.main()
