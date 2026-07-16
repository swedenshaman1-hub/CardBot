import asyncio
import logging
import os
import tempfile

from dotenv import load_dotenv
from gtts import gTTS
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_ID


async def send_voice(update: Update, text: str):
    fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    fd2, fast_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd2)
    try:
        await asyncio.to_thread(lambda: gTTS(text=text, lang="ru").save(mp3_path))
        await asyncio.to_thread(lambda: os.system(
            f'ffmpeg -y -i "{mp3_path}" -filter:a "atempo=1.25" "{fast_path}" -loglevel quiet'
        ))
        send_path = fast_path if os.path.exists(fast_path) and os.path.getsize(fast_path) > 0 else mp3_path
        with open(send_path, "rb") as f:
            await update.message.reply_voice(f)
    except Exception as e:
        logger.error(f"Voice error: {e}")
    finally:
        for p in (mp3_path, fast_path):
            try:
                os.unlink(p)
            except Exception:
                pass


async def addcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends card photo with caption: ID: Название: Расшифровка"""
    if not is_admin(update):
        return
    if not update.message.photo:
        await update.message.reply_text(
            "Пришли фото карты с подписью в формате:\nID: Название: Расшифровка"
        )
        return
    caption = update.message.caption or ""
    parts = caption.split(":", 2)
    if len(parts) != 3:
        await update.message.reply_text("Подпись должна быть: ID: Название: Расшифровка")
        return
    try:
        card_id = int(parts[0].strip())
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    name, meaning = parts[1].strip(), parts[2].strip()

    photo = update.message.photo[-1]
    file = await photo.get_file()
    file_bytes = bytes(await file.download_as_bytearray())

    image_url = await asyncio.to_thread(db.upload_card_image, card_id, file_bytes)
    await asyncio.to_thread(db.add_card, card_id, name, meaning, image_url)
    await update.message.reply_text(f"Карта #{card_id} «{name}» сохранена.")


async def setback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends card back photo with caption 'back' to set the rубашка image."""
    if not is_admin(update):
        return
    if not update.message.photo:
        await update.message.reply_text(
            "Пришли фото рубашки карты с подписью «back»."
        )
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    file_bytes = bytes(await file.download_as_bytearray())

    url = await asyncio.to_thread(db.upload_back_image, file_bytes)
    await update.message.reply_text(f"✅ Рубашка карты установлена.")


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

    caption = f"🃏 *{card['name']}*\n\n{card['meaning']}"
    await context.bot.send_chat_action(update.effective_chat.id, "upload_photo")
    await update.message.reply_photo(
        photo=card["image_url"],
        caption=caption,
        parse_mode="Markdown",
    )

    await context.bot.send_chat_action(update.effective_chat.id, "record_voice")
    await send_voice(update, card["meaning"])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔮 Добро пожаловать!\n\n"
        "Каждый день в канале появляются 6 карт.\n"
        "Напиши цифру от 1 до 6 — получи своё напутствие дня."
    )


def main():
    db.init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("newspread", newspread))
    application.add_handler(CommandHandler("addcard", addcard))
    # Photo with "back" caption → set card back image
    application.add_handler(
        MessageHandler(filters.PHOTO & filters.CaptionRegex(r"(?i)^back$"), setback)
    )
    # Photo with numeric caption → add card
    application.add_handler(
        MessageHandler(filters.PHOTO & filters.CaptionRegex(r"^\d"), addcard)
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message)
    )

    application.run_polling()


if __name__ == "__main__":
    main()
