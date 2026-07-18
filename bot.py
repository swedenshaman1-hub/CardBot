import asyncio
import base64
import logging
import os
import tempfile
import wave

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from telegram import InputFile, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
from collage import build_collage

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_ID


def _gemini_tts(text: str) -> str:
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=120_000),
    )
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-preview-tts",
                contents=text[:3000],
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
            break
        except Exception as e:
            if any(x in str(e) for x in ("DEADLINE_EXCEEDED", "504", "timeout")) and attempt < 2:
                continue
            raise

    pcm_data = response.candidates[0].content.parts[0].inline_data.data
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_data)
    return path


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
    uid = update.effective_user.id if update.effective_user else 0
    logger.info(f"PHOTO received from uid={uid}")
    # Temporarily open to all to diagnose — re-enable is_admin after confirming ADMIN_ID
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
                "Выбери свою карту — напиши её номер боту в личные сообщения."
            ),
            parse_mode="Markdown",
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

    card_id = spread["card_ids"][position - 1]
    card = await asyncio.to_thread(db.get_card, card_id)
    if card is None:
        await update.message.reply_text("Карта не найдена.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, "upload_photo")
    await update.message.reply_photo(
        photo=card["image_url"],
        caption=card["meaning"],
    )

    await context.bot.send_chat_action(update.effective_chat.id, "record_voice")
    await send_voice(update, card["meaning"])


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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔮 Добро пожаловать!\n\n"
        "Каждый день в канале появляются 6 карт.\n"
        "Напиши цифру от 1 до 6 — получи своё напутствие дня."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}", exc_info=context.error)


def main():
    db.init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("newspread", newspread))
    application.add_handler(CommandHandler("addcard", addcard))
    application.add_handler(CommandHandler("listcards", listcards))
    application.add_handler(CommandHandler("deletecard", deletecard))
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
