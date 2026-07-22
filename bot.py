import asyncio
import base64
import logging
import os
import tempfile
import wave

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
from collage import build_collage
from card_reading import build_card_reading

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MAX_CARDS_PER_SPREAD = 2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# python-telegram-bot uses httpx internally; its INFO lines contain the complete
# Bot API URL, including the secret token. Never write that to Railway logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_ID


def _member_has_channel_access(member) -> bool:
    status = getattr(member, "status", "")
    status = getattr(status, "value", status)
    if status in {"creator", "administrator", "member"}:
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))


async def is_channel_subscriber(bot, user_id: int) -> bool | None:
    """True/False for membership; None when Telegram could not verify it."""
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return _member_has_channel_access(member)
    except BadRequest as exc:
        if "member not found" in str(exc).lower() or "user not found" in str(exc).lower():
            return False
        logger.exception(
            "Could not verify channel membership for %s", user_id, exc_info=exc
        )
        return None
    except TelegramError as exc:
        logger.exception(
            "Could not verify channel membership for %s", user_id, exc_info=exc
        )
        return None


async def require_channel_subscription(bot, user_id: int) -> tuple[bool, str | None]:
    subscribed = await is_channel_subscriber(bot, user_id)
    if subscribed is True:
        return True, None
    if subscribed is False:
        return (
            False,
            "Карта дня доступна только подписчикам канала. "
            "Подпишись и попробуй ещё раз.",
        )
    return False, "Не удалось проверить подписку. Попробуй ещё раз через минуту."


def _gemini_tts(text: str) -> str:
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=120_000),
    )
    pcm_data = b""
    for chunk in _split_tts_text(text):
        pcm_data += _gemini_tts_chunk(client, chunk)

    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_data)
    return path


def _split_tts_text(text: str, max_chars: int = 1200) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []

    chunks: list[str] = []
    current = ""
    for sentence in text.replace("!", "!.").replace("?", "?.").split("."):
        sentence = sentence.strip()
        if not sentence:
            continue
        if not sentence.endswith((".", "!", "?")):
            sentence += "."
        if current and len(current) + len(sentence) + 1 > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()

    if current:
        chunks.append(current)
    return chunks


def _gemini_tts_chunk(client: genai.Client, text: str) -> bytes:
    prompt = f"Прочитай спокойным красивым голосом на русском языке:\n\n{text}"
    last_error = None
    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-preview-tts",
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=genai_types.SpeechConfig(
                        voice_config=genai_types.VoiceConfig(
                            prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                                voice_name="Kore"
                            )
                        )
                    ),
                ),
            )
            for candidate in response.candidates or []:
                content = candidate.content
                for part in getattr(content, "parts", None) or []:
                    inline_data = getattr(part, "inline_data", None)
                    if inline_data and inline_data.data:
                        if isinstance(inline_data.data, str):
                            return base64.b64decode(inline_data.data)
                        return inline_data.data
            last_error = "Gemini TTS returned no audio"
        except Exception as e:
            last_error = e

        if attempt < 3:
            continue

    raise RuntimeError(f"TTS failed: {last_error}")


def _transcribe_voice(ogg_bytes: bytes) -> str:
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=60_000),
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=genai_types.Content(
            parts=[
                genai_types.Part(
                    text="Расшифруй точно что сказано в этом голосовом сообщении. Выдай только текст, без каких-либо комментариев и пояснений."
                ),
                genai_types.Part(
                    inline_data=genai_types.Blob(
                        mime_type="audio/ogg",
                        data=base64.b64encode(ogg_bytes).decode(),
                    )
                ),
            ]
        ),
    )
    return response.text.strip()


async def send_voice(update: Update, text: str):
    path = None
    try:
        path = await asyncio.to_thread(_gemini_tts, text)
        with open(path, "rb") as f:
            await update.message.reply_voice(f)
    except Exception as e:
        logger.error(f"Voice error: {e}")
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass


def narrow_card_text(meaning: str, heading: str | None = None) -> str:
    """Return normal Telegram text without artificial line wrapping."""
    return f"{heading}\n\n{meaning.strip()}" if heading else meaning.strip()


async def send_card_to_chat(bot, chat_id: int, card_id: int):
    """Send the original card image and text with an optional voice button."""
    card = await asyncio.to_thread(db.get_card, card_id)
    if card is None:
        raise ValueError(f"Card #{card_id} not found")

    await bot.send_chat_action(chat_id, "upload_photo")
    await bot.send_photo(chat_id=chat_id, photo=card["image_url"])
    await bot.send_message(
        chat_id=chat_id,
        text=narrow_card_text(card["meaning"]),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎧 Прослушать послание", callback_data=f"voice:{card_id}")]]
        ),
    )


async def send_card_voice(bot, chat_id: int, card_id: int):
    """Generate and send a voice reading for a card."""
    card = await asyncio.to_thread(db.get_card, card_id)
    if card is None:
        raise ValueError(f"Card #{card_id} not found")

    voice_path = None
    try:
        await bot.send_chat_action(chat_id, "record_voice")
        voice_path = await asyncio.to_thread(_gemini_tts, card["meaning"])
        with open(voice_path, "rb") as audio:
            await bot.send_voice(chat_id=chat_id, voice=audio)
    except Exception as e:
        logger.exception("Card voice error for card %s: %s", card_id, e)
        raise
    finally:
        if voice_path:
            try:
                os.unlink(voice_path)
            except OSError:
                pass


async def _save_card_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_bytes: bytes):
    """Common logic after getting file_bytes from photo or document."""
    caption = (update.message.caption or "").strip()

    if caption.lower() == "back":
        await asyncio.to_thread(db.upload_back_image, file_bytes)
        await update.message.reply_text("✅ Рубашка карты установлена.")
        return

    parts = caption.split(":", 1)
    try:
        card_id = int(parts[0].strip())
    except ValueError:
        await update.message.reply_text(
            "Подпись должна начинаться с номера карты.\n"
            "Пример: <code>5</code> или <code>5: текст описания</code>",
            parse_mode="HTML",
        )
        return

    image_url = await asyncio.to_thread(db.upload_card_image, card_id, file_bytes)

    if len(parts) == 2 and parts[1].strip():
        meaning = parts[1].strip()
        await asyncio.to_thread(db.add_card, card_id, f"Карта {card_id}", meaning, image_url)
        await update.message.reply_text(f"✅ Карта #{card_id} сохранена.")
    else:
        context.user_data["pending_card_id"] = card_id
        context.user_data["pending_card_image_url"] = image_url
        await update.message.reply_text(
            f"📸 Фото карты #{card_id} сохранено.\n\n"
            f"Теперь пришли голосовое с описанием этой карты."
        )


async def addcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends card as photo (Telegram-compressed)."""
    if not is_admin(update):
        return
    if not update.message.photo:
        await update.message.reply_text(
            "Пришли фото карты с номером в подписи, например: <code>5</code>",
            parse_mode="HTML",
        )
        return
    photo = update.message.photo[-1]
    file = await photo.get_file()
    file_bytes = bytes(await file.download_as_bytearray())
    await _save_card_image(update, context, file_bytes)


async def addcard_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends card as file/document — original quality, no Telegram compression."""
    if not is_admin(update):
        return
    doc = update.message.document
    if not doc or not doc.mime_type or not doc.mime_type.startswith("image/"):
        return
    file = await doc.get_file()
    file_bytes = bytes(await file.download_as_bytearray())
    await _save_card_image(update, context, file_bytes)


async def handle_admin_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin dictates card description as voice message."""
    if not is_admin(update):
        return

    pending_id = context.user_data.get("pending_card_id")
    pending_url = context.user_data.get("pending_card_image_url")

    if pending_id is None:
        await update.message.reply_text(
            "Сначала отправь фото карты с номером в подписи, например: <code>5</code>",
            parse_mode="HTML",
        )
        return

    msg = await update.message.reply_text("🎙️ Расшифровываю...")

    voice = update.message.voice
    file = await voice.get_file()
    ogg_bytes = bytes(await file.download_as_bytearray())

    try:
        meaning = await asyncio.to_thread(_transcribe_voice, ogg_bytes)
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        await msg.edit_text("❌ Не удалось расшифровать. Попробуй ещё раз.")
        return

    await asyncio.to_thread(
        db.add_card, pending_id, f"Карта {pending_id}", meaning, pending_url
    )
    context.user_data.pop("pending_card_id", None)
    context.user_data.pop("pending_card_image_url", None)

    await msg.edit_text(
        f"✅ Карта #{pending_id} сохранена.\n\n"
        f"<b>Описание:</b>\n{meaning}",
        parse_mode="HTML",
    )


async def newspread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if len(context.args) != 6:
        await update.message.reply_text("Использование: /newspread id1 id2 id3 id4 id5 id6")
        return
    try:
        card_ids = [int(a) for a in context.args]
    except ValueError:
        await update.message.reply_text("Все ID должны быть числами.")
        return

    _, missing = await asyncio.to_thread(db.get_cards, card_ids)
    if missing:
        await update.message.reply_text(f"Не найдены карты с ID: {missing}")
        return

    back_url = await asyncio.to_thread(db.get_card_back_url)
    spread_id = await asyncio.to_thread(db.save_spread, card_ids)
    collage_path = await asyncio.to_thread(build_collage, back_url, spread_id)

    with open(collage_path, "rb") as f:
        message = await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=InputFile(f),
            caption=(
                "🔮 *Карты дня*\n\n"
                "Чтобы получить карту дня, подпишись на канал и выбери *до двух карт из шести*.\n"
                "Нажми номер карты ниже — расшифровка придёт тебе в личные сообщения от бота.\n"
                "После выбора под текстом можно нажать кнопку *«Прослушать послание»*.\n\n"
                "Если вам откликнулось послание, оставьте реакцию — пусть это будет наш энергообмен."
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(str(position), callback_data=f"pick:{spread_id}:{position}")
                    for position, card_id in enumerate(card_ids[:3], start=1)
                ],
                [
                    InlineKeyboardButton(str(position), callback_data=f"pick:{spread_id}:{position}")
                    for position, card_id in enumerate(card_ids[3:], start=4)
                ],
            ]),
        )
    os.remove(collage_path)

    await asyncio.to_thread(db.update_spread_message, spread_id, message.message_id)
    await update.message.reply_text("✅ Расклад опубликован в канале.")


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text(
            "🔮 Напиши цифру от 1 до 6, чтобы открыть свою карту дня."
        )
        return
    position = int(text)
    if not 1 <= position <= 6:
        await update.message.reply_text("Выбери цифру от 1 до 6 🔮")
        return

    spread = await asyncio.to_thread(db.get_latest_spread)
    if spread is None:
        await update.message.reply_text("✨ Сегодня ещё не было расклада — загляни позже.")
        return

    allowed, message = await require_channel_subscription(
        context.bot, update.effective_user.id
    )
    if not allowed:
        await update.message.reply_text(message)
        return

    claim = await asyncio.to_thread(
        db.claim_spread_selection,
        spread["id"],
        update.effective_user.id,
        position,
        MAX_CARDS_PER_SPREAD,
    )
    if not claim["allowed"]:
        await update.message.reply_text(
            "Ты уже выбрал две карты в этом раскладе. Третью открыть нельзя."
        )
        return
    if not claim["is_new"]:
        await update.message.reply_text(
            "Эту карту ты уже выбрал. Она входит в твои две карты дня."
        )
        return

    card_id = spread["card_ids"][position - 1]
    card = await asyncio.to_thread(db.get_card, card_id)
    if card is None:
        await update.message.reply_text("Карта не найдена.")
        return

    await send_card_to_chat(context.bot, update.effective_chat.id, card_id)


async def select_card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check subscription and enforce two card choices per published spread."""
    query = update.callback_query
    if query is None or not query.data:
        return

    spread = None
    position = None
    if query.data.startswith("pick:"):
        _, spread_id_text, position_text = query.data.split(":", 2)
        try:
            spread_id = int(spread_id_text)
            position = int(position_text)
        except ValueError:
            await query.answer("Не удалось определить карту.", show_alert=True)
            return
        spread = await asyncio.to_thread(db.get_spread, spread_id)
    elif query.data.startswith("card:"):
        # Compatibility with buttons in posts published before this update.
        _, legacy_card_id_text, position_text = query.data.split(":", 2)
        try:
            legacy_card_id = int(legacy_card_id_text)
            position = int(position_text)
        except ValueError:
            await query.answer("Не удалось определить карту.", show_alert=True)
            return
        spread = await asyncio.to_thread(db.get_latest_spread)
        if (
            spread is None
            or not 1 <= position <= len(spread["card_ids"])
            or spread["card_ids"][position - 1] != legacy_card_id
        ):
            await query.answer(
                "Этот расклад уже завершён. Выбери карту в новой публикации.",
                show_alert=True,
            )
            return
    else:
        return

    if spread is None or position is None or not 1 <= position <= len(spread["card_ids"]):
        await query.answer("Этот расклад не найден.", show_alert=True)
        return

    allowed, message = await require_channel_subscription(context.bot, query.from_user.id)
    if not allowed:
        await query.answer(message, show_alert=True)
        return

    # A chat action is a quick, silent way to verify that the user started the
    # bot before consuming one of the two choices.
    try:
        await context.bot.send_chat_action(query.from_user.id, "typing")
    except TelegramError:
        await query.answer(
            "Сначала открой бота в личных сообщениях и нажми Start, "
            "затем выбери карту снова.",
            show_alert=True,
        )
        return

    claim = await asyncio.to_thread(
        db.claim_spread_selection,
        spread["id"],
        query.from_user.id,
        position,
        MAX_CARDS_PER_SPREAD,
    )
    if not claim["allowed"]:
        await query.answer(
            "Ты уже выбрал две карты в этом раскладе. Третью открыть нельзя.",
            show_alert=True,
        )
        return
    if not claim["is_new"]:
        await query.answer(
            "Эту карту ты уже выбрал. Она входит в твои две карты дня.",
            show_alert=True,
        )
        return

    card_id = spread["card_ids"][position - 1]
    selected_count = len(claim["selections"])
    await query.answer(f"Открываю карту {selected_count} из {MAX_CARDS_PER_SPREAD}…")
    try:
        await send_card_to_chat(context.bot, query.from_user.id, card_id)
    except Exception as exc:
        logger.exception("Could not send selected card", exc_info=exc)


async def voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send voice reading only after a subscriber explicitly asks for it."""
    query = update.callback_query
    if query is None or not query.data:
        return

    try:
        card_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Не удалось открыть послание.", show_alert=True)
        return

    allowed, message = await require_channel_subscription(context.bot, query.from_user.id)
    if not allowed:
        await query.answer(message, show_alert=True)
        return

    await query.answer("Озвучиваю послание...")
    try:
        await send_card_voice(context.bot, query.from_user.id, card_id)
    except Exception:
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="Не удалось озвучить послание. Попробуй нажать кнопку ещё раз чуть позже.",
        )


def review_keyboard(card_id: int) -> InlineKeyboardMarkup:
    previous_id = max(1, card_id - 1)
    next_id = min(120, card_id + 1)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️", callback_data=f"review:{previous_id}"),
            InlineKeyboardButton("✓ Верно", callback_data=f"review-ok:{card_id}"),
            InlineKeyboardButton("▶️", callback_data=f"review:{next_id}"),
        ]
    ])


async def send_review_card(bot, chat_id: int, card_id: int, context: ContextTypes.DEFAULT_TYPE):
    card = await asyncio.to_thread(db.get_card, card_id)
    if card is None:
        raise ValueError(f"Card #{card_id} not found")

    # Review is deliberately three separate Telegram messages:
    # original card, narrow text beneath it, then a voice reading.
    await bot.send_chat_action(chat_id, "upload_photo")
    photo = await bot.send_photo(chat_id=chat_id, photo=card["image_url"])
    text = await bot.send_message(
        chat_id=chat_id,
        text=narrow_card_text(card["meaning"], f"🔎 Карта №{card_id}"),
        reply_markup=review_keyboard(card_id),
    )

    voice_path = None
    voice = None
    try:
        await bot.send_chat_action(chat_id, "record_voice")
        voice_path = await asyncio.to_thread(_gemini_tts, card["meaning"])
        with open(voice_path, "rb") as audio:
            voice = await bot.send_voice(chat_id=chat_id, voice=audio)
    except Exception as e:
        logger.exception("Review voice error for card %s: %s", card_id, e)
    finally:
        if voice_path:
            try:
                os.unlink(voice_path)
            except OSError:
                pass
    context.user_data["review_photo_message_id"] = photo.message_id
    context.user_data["review_text_message_id"] = text.message_id
    if voice:
        context.user_data["review_voice_message_id"] = voice.message_id
    return

    # Keep the card and its text inside one image, so Telegram cannot make
    # a description bubble wider than the card itself.
    reading_path = None
    voice_path = None
    try:
        await bot.send_chat_action(chat_id, "upload_photo")
        reading_path = await asyncio.to_thread(
            build_card_reading,
            card["image_url"],
            card["meaning"],
            f"Проверка карты №{card_id}",
        )
        with open(reading_path, "rb") as image:
            photo = await bot.send_photo(
                chat_id=chat_id,
                photo=image,
                reply_markup=review_keyboard(card_id),
            )

        await bot.send_chat_action(chat_id, "record_voice")
        voice_path = await asyncio.to_thread(_gemini_tts, card["meaning"])
        with open(voice_path, "rb") as audio:
            voice = await bot.send_voice(chat_id=chat_id, voice=audio)
        context.user_data["review_voice_message_id"] = voice.message_id
        context.user_data["review_photo_message_id"] = photo.message_id
    finally:
        for path in (reading_path, voice_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    return

    photo = await bot.send_photo(chat_id=chat_id, photo=card["image_url"])
    text = await bot.send_message(
        chat_id=chat_id,
        text=f"🔎 Проверка карты №{card_id}\n\n{card['meaning']}",
        reply_markup=review_keyboard(card_id),
    )
    context.user_data["review_photo_message_id"] = photo.message_id
    context.user_data["review_text_message_id"] = text.message_id


async def review_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only sequential review of all original cards and their texts."""
    if not is_admin(update):
        return
    try:
        card_id = int(context.args[0]) if context.args else 1
    except ValueError:
        await update.message.reply_text("Использование: /review 1")
        return
    if not 1 <= card_id <= 120:
        await update.message.reply_text("Номер карты — от 1 до 120.")
        return
    await send_review_card(context.bot, update.effective_chat.id, card_id, context)


async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or not is_admin(update):
        return

    if query.data.startswith("review-ok:"):
        card_id = query.data.split(":", 1)[1]
        await query.answer(f"Карта №{card_id} отмечена как проверенная.")
        return

    try:
        card_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Не удалось открыть карту.", show_alert=True)
        return

    await query.answer()
    # Remove the previous pair, so the review chat stays clean.
    old_voice_id = context.user_data.get("review_voice_message_id")
    if old_voice_id:
        try:
            await context.bot.delete_message(query.message.chat_id, old_voice_id)
        except Exception:
            pass
    old_photo_id = context.user_data.get("review_photo_message_id")
    if old_photo_id:
        try:
            await context.bot.delete_message(query.message.chat_id, old_photo_id)
        except Exception:
            pass
    try:
        await query.message.delete()
    except Exception:
        pass
    await send_review_card(context.bot, query.message.chat_id, card_id, context)


async def listcards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cards = await asyncio.to_thread(db.list_all_cards)
    if not cards:
        await update.message.reply_text("Карт пока нет.")
        return
    lines = [f"#{c['id']} — {c['meaning'][:60]}{'…' if len(c['meaning']) > 60 else ''}" for c in cards]
    text = f"Загружено карт: {len(cards)}\n\n" + "\n".join(lines)
    await update.message.reply_text(text)


async def deletecard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /deletecard 5")
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    await asyncio.to_thread(db.delete_card, card_id)
    await update.message.reply_text(f"✅ Карта #{card_id} удалена.")


async def editcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /editcard 5 новый текст описания")
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Первым должен быть номер карты.")
        return
    meaning = " ".join(context.args[1:])
    await asyncio.to_thread(db.update_card_meaning, card_id, meaning)
    await update.message.reply_text(f"✅ Текст карты #{card_id} обновлён.\n\n{meaning}")


async def clearcards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await asyncio.to_thread(db.delete_all_cards)
    await update.message.reply_text("✅ Все карты удалены. База чистая.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔮 Добро пожаловать!\n\n"
        "Каждый день в канале появляются 6 карт.\n"
        "Подпишись на канал и выбери две карты из шести — "
        "получишь их расшифровку и озвучку."
    )


async def verify_runtime(application: Application):
    """Log whether Telegram can use the configured chat for subscriptions."""
    try:
        me = await application.bot.get_me()
        logger.info("Card bot identity: @%s", me.username)
        chat = await application.bot.get_chat(CHANNEL_ID)
        logger.info("Card access chat: title=%s type=%s", chat.title, chat.type)
        member = await application.bot.get_chat_member(CHANNEL_ID, me.id)
        logger.info(
            "Card access target: bot=@%s chat_type=%s bot_status=%s",
            me.username,
            chat.type,
            member.status,
        )
        if chat.type not in {"channel", "supergroup"}:
            logger.error("CHANNEL_ID must point to a channel or supergroup")
        if member.status not in {"creator", "administrator"}:
            logger.warning(
                "Bot should be an administrator for reliable membership checks"
            )
    except BadRequest as exc:
        if "member list is inaccessible" in str(exc).lower():
            logger.error(
                "Card access is blocked: add the bot as a channel administrator"
            )
        else:
            logger.exception("Card access startup check failed", exc_info=exc)
    except TelegramError as exc:
        logger.exception("Card access startup check failed", exc_info=exc)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}", exc_info=context.error)


def main():
    db.init_db()
    application = Application.builder().token(BOT_TOKEN).post_init(verify_runtime).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("newspread", newspread))
    application.add_handler(CommandHandler("addcard", addcard))
    application.add_handler(CommandHandler("listcards", listcards))
    application.add_handler(CommandHandler("deletecard", deletecard))
    application.add_handler(CommandHandler("editcard", editcard))
    application.add_handler(CommandHandler("clearcards", clearcards))
    application.add_handler(CommandHandler("review", review_cards))
    application.add_handler(CallbackQueryHandler(voice_callback, pattern=r"^voice:"))
    application.add_handler(
        CallbackQueryHandler(select_card_callback, pattern=r"^(pick|card):")
    )
    application.add_handler(CallbackQueryHandler(review_callback, pattern=r"^review"))
    application.add_handler(MessageHandler(filters.PHOTO, addcard))
    application.add_handler(MessageHandler(filters.Document.IMAGE, addcard_document))
    application.add_handler(MessageHandler(filters.VOICE, handle_admin_voice))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message)
    )
    application.add_error_handler(error_handler)

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
