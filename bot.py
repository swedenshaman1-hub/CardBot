import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import wave
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
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
ANALYTICS_SECRET = os.getenv("ANALYTICS_SECRET", BOT_TOKEN)
BOT_LINK = "https://t.me/shamankarty_bot"
MAX_CARDS_PER_SPREAD = 2
REFLECTION_PROMPT_VERSION = "v2"
SPREAD_QUESTION_PROMPT_VERSION = "v1"
SPREAD_QUESTION_MAX_LENGTH = 70
TELEGRAM_CAPTION_MAX_LENGTH = 1024
AUTO_DELETE_SECONDS = 72 * 60 * 60
AUTO_DELETE_SETTING_PREFIX = "spread_auto_delete:"
SPREAD_INTRO_SETTING_PREFIX = "spread_intro:"
SPREAD_VOICE_SCRIPT_PREFIX = "spread_voice_script:"
SPREAD_VOICE_SETTING_PREFIX = "spread_voice:"
SPREAD_CHANNEL_VOICE_PREFIX = "spread_channel_voice:"
SCHEDULED_SPREAD_PREFIX = "scheduled_spread:"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# python-telegram-bot uses httpx internally; its INFO lines contain the complete
# Bot API URL, including the secret token. Never write that to Railway logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_ID


def _actor_hash(user_id: int) -> str:
    """Return a stable, irreversible analytics identifier."""
    return hmac.new(
        ANALYTICS_SECRET.encode("utf-8"),
        str(user_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def record_analytics_event(**event):
    """Queue analytics without delaying card or voice delivery."""
    async def write_event():
        try:
            await asyncio.to_thread(db.record_event, **event)
        except Exception as exc:
            logger.warning("Analytics event was not recorded: %s", exc)

    asyncio.create_task(write_event())


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


def _spread_intro_key(spread_id: int) -> str:
    return f"{SPREAD_INTRO_SETTING_PREFIX}{spread_id:010d}"


def _spread_voice_key(spread_id: int) -> str:
    return f"{SPREAD_VOICE_SETTING_PREFIX}{spread_id:010d}"


def _spread_voice_script_key(spread_id: int) -> str:
    return f"{SPREAD_VOICE_SCRIPT_PREFIX}{spread_id:010d}"


def _spread_channel_voice_key(spread_id: int) -> str:
    return f"{SPREAD_CHANNEL_VOICE_PREFIX}{spread_id:010d}"


def _scheduled_spread_key(spread_id: int) -> str:
    return f"{SCHEDULED_SPREAD_PREFIX}{spread_id:010d}"


def _generate_spread_intro(cards: list[dict], recent_intros: list[str]) -> str:
    """Create a fresh author-style introduction grounded in the chosen cards."""
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=60_000),
    )
    card_context = "\n".join(
        f"- Карта №{card['id']}: {card.get('meaning', '').strip()[:900]}"
        for card in cards
    )
    recent_context = "\n\n---\n\n".join(recent_intros[-7:]) or "Нет предыдущих текстов."
    prompt = f"""
Ты — редактор ежедневного проекта Дмитрия «Карты дня».

Напиши ОДНО уникальное авторское вступление к сегодняшнему раскладу.
Это не трактовка отдельных карт и не предсказание. Дмитрий лично выбрал шесть
метафорических карт, а читатель затем интуитивно откроет две из них.

Голос Дмитрия:
- сердечный, тёплый, живой и простой;
- ощущение личного присутствия, а не безликого бота;
- возвращение внимания человека к себе, телу, чувствам и честному внутреннему вопросу;
- опора и ответы находятся внутри человека;
- никакого осуждения, давления, диагнозов, мистического тумана и обещаний будущего.

Требования:
- 35–45 слов, 2 коротких абзаца, не более 300 символов;
- начать каждый раз по-разному;
- раскрыть единый смысловой нерв шести карт, не называя их номера и содержание;
- добавить один точный вопрос для самонаблюдения;
- закончить мягким приглашением выбрать карту;
- не использовать штампы «Вселенная посылает знак», «неслучайно», «ответ уже рядом»;
- не повторять лексику, структуру и вопрос из недавних вступлений;
- выдать только готовый текст без заголовка и пояснений.

Сегодняшние карты:
{card_context}

Недавние вступления, которые нельзя повторять:
{recent_context}
""".strip()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            temperature=1.0,
            max_output_tokens=900,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    intro = (response.text or "").strip().strip('"')
    intro = intro.replace("*", "").replace("`", "").replace("_", "")
    if not intro:
        raise RuntimeError("Gemini returned an empty spread introduction")
    if len(intro) > 300:
        shortened = intro[:297].rsplit(" ", 1)[0].rstrip(" ,;:-")
        intro = f"{shortened}…"
    return intro


def _generate_spread_voice_script(
    cards: list[dict], recent_scripts: list[str]
) -> str:
    """Create a 30–40 second personal script for Dmitry to record."""
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=60_000),
    )
    card_context = "\n".join(
        f"- Карта №{card['id']}: {card.get('meaning', '').strip()[:1100]}"
        for card in cards
    )
    recent_context = "\n\n---\n\n".join(recent_scripts[-7:]) or "Нет предыдущих текстов."
    prompt = f"""
Ты создаёшь личное голосовое вступление Дмитрия к ежедневному раскладу
«Карты дня». Дмитрий прочитает этот текст своим голосом.

Значения шести карт используй только как скрытый источник темы и глубины.
Готовое послание должно звучать как самостоятельное человеческое размышление.
В нём нельзя упоминать карты, расклад, номера, выбор карты или объяснять,
откуда появилась тема. Человек должен услышать цельную мысль, полезную саму
по себе, даже если слушает аудио отдельно от публикации.

Перед финальным ответом молча проведи текст через редакционную команду:
1. Методолог «Терапии Души» — карта не предсказывает будущее, а возвращает
   внимание к телу, чувствам, мыслям, авторству и внутренней опоре.
2. Редактор голоса Дмитрия — сердечность, живое присутствие, простота,
   уверенность, отсутствие осуждения и разговора сверху.
3. Joanna Wiebe — сильный, ясный хук на языке реального переживания человека.
4. Ann Handley — человеческая интонация, конкретность и ощущение разговора
   с одним человеком, а не с безликой аудиторией.
5. Rory Sutherland — свежий смысловой угол без искажения значений карт.
6. Cialdini и Sandel — этическая проверка: никакого давления, страха,
   искусственного дефицита, манипуляции болью и ложных обещаний.

Формат обязателен:
- первая строка — короткий цепляющий заголовок-хук из 4–8 слов;
- затем пустая строка и связный текст;
- весь текст 75–95 слов: это 30–40 секунд спокойной речи;
- 3 коротких абзаца;
- один точный вопрос, который помогает человеку прислушаться к себе;
- финал — спокойная завершающая мысль или приглашение понаблюдать за собой
  в течение дня, без призыва выбирать карту.

Голос Дмитрия: живой, тёплый, уверенный, сердечный, без пафоса. Должно
чувствоваться, что он лично выбрал карты и сейчас говорит с одним человеком.
Найди общий смысловой нерв шести карт и преврати его в самостоятельное
рассуждение, не выдавая источник темы.

Запрещено: любые слова «карта», «карты», «расклад», «выберите», предсказания,
диагнозы, давление, обещания результата, мистический
туман, «Вселенная посылает знак», «это неслучайно», канцелярит и рекламные штампы.
Не повторяй заголовки, вопросы, начало и образ из недавних текстов.
Выдай только готовый текст. Не ставь кавычки, Markdown и пояснения.

Сегодняшние карты:
{card_context}

Недавние сценарии, которые нельзя повторять:
{recent_context}
""".strip()

    last_text = ""
    for _ in range(3):
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=1.05,
                max_output_tokens=1200,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        last_text = (response.text or "").strip().strip('"')
        last_text = last_text.replace("*", "").replace("`", "").replace("_", "")
        word_count = len(last_text.split())
        if 65 <= word_count <= 110 and "\n" in last_text:
            return last_text
        prompt += (
            f"\n\nПредыдущая попытка содержала {word_count} слов. "
            "Перепиши полностью и строго соблюди объём 75–95 слов."
        )

    if len(last_text.split()) < 50:
        raise RuntimeError("Gemini returned a voice script that is too short")
    return last_text


def _generate_reflection_question(card: dict) -> str:
    """Create one question grounded in the original card meaning."""
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=60_000),
    )
    prompt = f"""
Ты создаёшь один вопрос для пользователя Telegram-бота Дмитрия.

Оригинальный смысл карты №{card["id"]}:
{card.get("meaning", "").strip()[:1800]}

Задача вопроса — помочь человеку связать смысл карты со своим текущим
переживанием. Вопрос должен быть открытым, конкретным и отвечаемым в 1–3
предложениях.

Голос Дмитрия: живой, спокойный, прямой, на равных; внимание к настоящему
моменту, чувствам, телу и личной ответственности.

Запрещено:
- ставить диагноз или называть скрытую причину;
- утверждать наличие травмы, сценария, блока или родовой программы;
- внушать воспоминания;
- спрашивать сразу о нескольких вещах;
- давить, обвинять или обещать исцеление;
- использовать слова «Терапия Души», «метод» и «нейросеть».

Верни только один вопрос, 12–28 слов, без заголовка и пояснений.
""".strip()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            temperature=0.75,
            max_output_tokens=250,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    question = (response.text or "").strip().strip('"').replace("*", "")
    if not question:
        raise RuntimeError("Gemini returned an empty reflection question")
    return question


def _generate_spread_question(
    cards: list[dict], topic: str | None = None
) -> str:
    """Create one open question grounded in all six cards."""
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=60_000),
    )
    cards_context = "\n\n".join(
        f"Карта №{card['id']} — {card.get('name', '').strip()}:\n"
        f"{card.get('meaning', '').strip()[:1800]}"
        for card in cards
    )
    topic_instruction = (
        f"\nТема, которую выбрал автор: {topic.strip()[:200]}\n"
        "Сделай эту тему главным смысловым направлением вопроса."
        if topic and topic.strip()
        else ""
    )
    prompt = f"""
Ты создаёшь один короткий «Вопрос дня» к раскладу из шести метафорических карт.

Карты расклада:
{cards_context}
{topic_instruction}

Сформулируй ровно один короткий открытый вопрос от второго лица — обращайся к человеку на «вы».
Используй от 6 до 10 слов. Вопрос должен легко читаться в Telegram Stories.
Вопрос должен объединять общий смысл расклада, приглашать к самостоятельному размышлению,
не допускать ответа только «да» или «нет» и не обещать результат, исцеление или изменение.
Не ставь диагнозов, не называй скрытых причин и не используй мистические утверждения.

Верни только вопрос без заголовка, пояснения, Markdown и кавычек.
Максимальная длина — {SPREAD_QUESTION_MAX_LENGTH} символов.
""".strip()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            temperature=0.75,
            max_output_tokens=250,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    question = normalize_spread_question((response.text or "").strip().strip('"'))
    if question.count("?") != 1 or not question.endswith("?"):
        raise RuntimeError("Gemini did not return exactly one question")
    word_count = len(question[:-1].split())
    if not 6 <= word_count <= 10:
        raise RuntimeError("Gemini question must contain 6 to 10 words")
    return question


def _generate_safe_reflection(
    card: dict, question: str, user_answer: str
) -> str:
    """Create one bounded reflection for a card dialogue."""
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=60_000),
    )
    prompt = f"""
Ты — цифровой помощник Дмитрия. Подготовь один бережный
разбор ответа человека, опираясь только на три источника ниже.

1. Оригинальный смысл карты №{card["id"]}:
{card.get("meaning", "").strip()[:1800]}

2. Вопрос бота:
{question}

3. Дословный ответ человека:
{user_answer[:1600]}

Смысловая оптика:
- «куда направлено внимание — туда движется переживание»;
- различай внешний триггер и внутреннюю реакцию;
- возвращай человеку авторство без обвинения;
- замечай мысли, чувства и телесные ощущения здесь и сейчас;
- предлагай точку наблюдения, а не готовую истину.

Голос Дмитрия:
- простой, живой, спокойный и прямой;
- разговор на равных, без жалости и позиции спасателя;
- можно использовать «похоже», «в твоём ответе слышится», «возможно»;
- нельзя говорить от первого лица Дмитрия и нельзя выдавать текст за личную
  консультацию Дмитрия.

Строгие границы:
- не ставь диагнозов;
- не определяй травму, возраст события, мотивы других людей, родовые причины,
  блоки, болезни или объективные факты;
- не внушай воспоминаний и не утверждай причинно-следственную связь;
- не пиши, что физический симптом точно вызван эмоцией;
- не давай медицинских, юридических или финансовых советов;
- не обещай результат и не используй мистические объяснения.

Формат:
- 80–120 слов;
- 3 коротких абзаца;
- сначала точно отрази то, что человек действительно написал;
- затем покажи одну возможную связь с темой карты, обязательно как гипотезу;
- закончи одним небольшим наблюдением или безопасным действием на сегодня;
- не задавай второго вопроса;
- не используй Markdown, заголовок и служебные пояснения.

Особенно важно:
- не разворачивай одно слово человека в психологическую теорию;
- не добавляй качества, мотивы и переживания, которых нет в ответе;
- не пересказывай очевидное вроде «в твоём ответе звучит слово...»;
- если данных недостаточно, честно скажи об этом вместо догадки.

Если ответ содержит намерение причинить вред себе/другим, признаки острого
кризиса или просьбу о диагнозе — вместо разбора напиши, что бот не может
безопасно это разбирать и человеку нужна живая профессиональная помощь.
""".strip()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            temperature=0.55,
            max_output_tokens=900,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    reflection = (response.text or "").strip().strip('"')
    reflection = reflection.replace("*", "").replace("`", "")
    if not reflection:
        raise RuntimeError("Gemini returned an empty reflection")
    return reflection


def reflection_answer_is_detailed(answer: str) -> bool:
    words = re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", answer)
    return len(words) >= 5 and len(answer.strip()) >= 20


def reflection_clarification(answer: str) -> str:
    short_answer = " ".join(answer.strip().strip(".?!,;:\"«»").split())[:120]
    return (
        f"«{short_answer}» — важный ответ, но пока он слишком короткий для "
        "точного разбора.\n\n"
        "Раскройте немного подробнее: что это означает лично для вас в "
        "контексте вопроса и где вы замечаете это в своей жизни сейчас?\n\n"
        "Ответьте одним-двумя предложениями — текстом или голосом."
    )


def normalize_spread_question(question: str) -> str:
    question = " ".join((question or "").split())
    if not question:
        raise ValueError("Вопрос не может быть пустым.")
    if len(question) > SPREAD_QUESTION_MAX_LENGTH:
        raise ValueError(
            f"Вопрос должен быть не длиннее {SPREAD_QUESTION_MAX_LENGTH} символов."
        )
    return question


def escape_markdown_question(question: str) -> str:
    """Escape administrator text for Telegram's legacy Markdown mode."""
    escaped = question.replace("\\", "\\\\")
    for character in ("_", "*", "[", "]", "`"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def spread_caption(question: str | None = None) -> str:
    intro = (
        "🔮 *Карты дня*\n\n"
        "Сегодня я выбрал для вас 6 метафорических карт.\n"
        "Посмотрите на них и почувствуйте, какая карта сейчас откликается именно вам."
    )
    remainder = (
        "Чтобы получить своё послание дня, подпишитесь на канал и нажмите номер выбранной карты. Telegram откроет бота автоматически.\n"
        "Описание карты придёт вам в личные сообщения от бота.\n\n"
        "В каждой новой публикации вы можете открывать для себя две карты.\n\n"
        "Если вам откликнулось послание, оставьте реакцию — пусть это будет наш энергообмен."
    )
    if question is None:
        caption = f"{intro}\n\n{remainder}"
    else:
        safe_question = escape_markdown_question(
            normalize_spread_question(question)
        )
        caption = (
            f"{intro}\n\n"
            f"❓ *Вопрос дня*\n{safe_question}\n\n"
            f"{remainder}"
        )
    if len(caption) > TELEGRAM_CAPTION_MAX_LENGTH:
        logger.error(
            "Spread caption is too long: %s characters (limit %s)",
            len(caption),
            TELEGRAM_CAPTION_MAX_LENGTH,
        )
        raise ValueError("Spread caption exceeds Telegram caption limit")
    return caption


def spread_pick_keyboard(spread_id: int, card_ids: list[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                str(position),
                callback_data=f"pick:{spread_id}:{position}",
            )
            for position, card_id in enumerate(card_ids[:3], start=1)
        ],
        [
            InlineKeyboardButton(
                str(position),
                callback_data=f"pick:{spread_id}:{position}",
            )
            for position, card_id in enumerate(card_ids[3:], start=4)
        ],
    ])


def spread_preview_keyboard(
    spread_id: int, has_voice: bool = False
) -> InlineKeyboardMarkup:
    voice_label = (
        "✅ Голос записан — перезаписать"
        if has_voice
        else "🎙 Записать моё послание"
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                voice_label,
                callback_data=f"record-spread:{spread_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "❓ Вопрос дня",
                callback_data=f"question-spread:show:{spread_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "👁 Посмотреть готовую публикацию",
                callback_data=f"show-preview:{spread_id}",
            )
        ],
        [
            InlineKeyboardButton("✅ Опубликовать сейчас", callback_data=f"publish-spread:{spread_id}"),
            InlineKeyboardButton("🕗 Запланировать", callback_data=f"schedule-spread:{spread_id}"),
        ],
        [
            InlineKeyboardButton("✖️ Отменить", callback_data=f"cancel-spread:{spread_id}"),
        ]
    ])


def spread_question_keyboard(
    spread_id: int,
    *,
    allow_generate: bool = True,
    allow_regenerate: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    if allow_generate:
        rows.append([
            InlineKeyboardButton(
                "✨ Предложить через Gemini",
                callback_data=f"question-spread:generate:{spread_id}",
            )
        ])
    elif allow_regenerate:
        rows.append([
            InlineKeyboardButton(
                "🔄 Ещё вариант",
                callback_data=f"question-spread:regenerate:{spread_id}",
            )
        ])
    rows.extend([
        [
            InlineKeyboardButton(
                "🎯 Предложить по моей теме",
                callback_data=f"question-spread:topic:{spread_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "✍️ Ввести готовый вопрос",
                callback_data=f"question-spread:custom:{spread_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 Убрать вопрос",
                callback_data=f"question-spread:remove:{spread_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Вернуться к раскладу",
                callback_data=f"question-spread:back:{spread_id}",
            )
        ],
    ])
    return InlineKeyboardMarkup(rows)


def spread_question_screen_text(question: str | None, notice: str | None = None) -> str:
    current = escape(question) if question else "<i>не задан</i>"
    prefix = f"{escape(notice)}\n\n" if notice else ""
    return (
        f"{prefix}❓ <b>Вопрос дня</b>\n\n"
        f"Текущий вопрос:\n{current}\n\n"
        f"Максимальная длина — {SPREAD_QUESTION_MAX_LENGTH} символов."
    )


async def generate_spread_question_for_topic(
    spread_id: int, topic: str
) -> str:
    """Generate and immediately persist one question for the author's topic."""
    topic = " ".join((topic or "").split())
    if not topic:
        raise ValueError("Тема не может быть пустой.")
    spread = await asyncio.to_thread(db.get_spread, spread_id)
    if spread is None:
        raise ValueError("Расклад не найден.")
    cards, missing = await asyncio.to_thread(db.get_cards, spread["card_ids"])
    if missing:
        raise ValueError("Часть карт расклада не найдена.")
    question = await asyncio.to_thread(
        _generate_spread_question, cards, topic
    )
    await asyncio.to_thread(db.update_spread_question, spread_id, question)
    return question


def recorded_voice_preview_keyboard(spread_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Опубликовать сейчас",
                callback_data=f"publish-spread:{spread_id}",
            ),
            InlineKeyboardButton(
                "🕗 Запланировать",
                callback_data=f"schedule-spread:{spread_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔁 Перезаписать",
                callback_data=f"record-spread:{spread_id}",
            ),
        ]
    ])


def spread_visual_preview_keyboard(spread_id: int) -> InlineKeyboardMarkup:
    """Number buttons that look like the channel post but do not open cards."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                str(position),
                callback_data=f"preview-position:{spread_id}:{position}",
            )
            for position in range(1, 4)
        ],
        [
            InlineKeyboardButton(
                str(position),
                callback_data=f"preview-position:{spread_id}:{position}",
            )
            for position in range(4, 7)
        ],
    ])


async def send_complete_spread_preview(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    spread_id: int,
    voice_file_id: str,
):
    """Send the exact photo+caption+voice sequence without touching the channel."""
    spread = await asyncio.to_thread(db.get_spread, spread_id)
    if spread is None:
        raise ValueError(f"Spread {spread_id} not found")
    back_url = await asyncio.to_thread(db.get_card_back_url)
    collage_path = await asyncio.to_thread(build_collage, back_url, spread_id)
    try:
        with open(collage_path, "rb") as preview_image:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(preview_image),
                caption=spread_caption(spread.get("question")),
                parse_mode="Markdown",
                reply_markup=spread_visual_preview_keyboard(spread_id),
            )
    finally:
        os.remove(collage_path)

    await context.bot.send_voice(
        chat_id=chat_id,
        voice=voice_file_id,
        caption="🎙 Моё личное послание к сегодняшним картам",
        reply_markup=recorded_voice_preview_keyboard(spread_id),
    )


def _auto_delete_setting_key(spread_id: int) -> str:
    return f"{AUTO_DELETE_SETTING_PREFIX}{spread_id}"


async def delete_published_spread_later(
    application: Application,
    spread_id: int,
    message_id: int,
    delete_at: float,
):
    """Delete a channel post at its scheduled time and persist completion."""
    delay = max(0, delete_at - time.time())
    if delay:
        await asyncio.sleep(delay)

    channel_voice_id = await asyncio.to_thread(
        db.get_setting, _spread_channel_voice_key(spread_id)
    )
    if channel_voice_id and channel_voice_id != "deleted":
        try:
            await application.bot.delete_message(
                chat_id=CHANNEL_ID,
                message_id=int(channel_voice_id),
            )
        except (TelegramError, ValueError) as exc:
            logger.warning(
                "Could not automatically delete author voice for spread %s: %s",
                spread_id,
                exc,
            )
        finally:
            await asyncio.to_thread(
                db.set_setting,
                _spread_channel_voice_key(spread_id),
                "deleted",
            )

    try:
        await application.bot.delete_message(chat_id=CHANNEL_ID, message_id=message_id)
        logger.info("Automatically deleted spread %s from channel", spread_id)
    except TelegramError as exc:
        # Treat an already removed message as completed; otherwise keep the
        # error visible in logs without crashing the polling process.
        logger.warning("Could not automatically delete spread %s: %s", spread_id, exc)
    finally:
        await asyncio.to_thread(
            db.set_setting,
            _auto_delete_setting_key(spread_id),
            "deleted",
        )


def schedule_spread_deletion(
    application: Application,
    spread_id: int,
    message_id: int,
    delete_at: float,
):
    tasks = application.bot_data.setdefault("spread_delete_tasks", {})
    old_task = tasks.get(spread_id)
    if old_task and not old_task.done():
        old_task.cancel()
    task = asyncio.create_task(
        delete_published_spread_later(application, spread_id, message_id, delete_at)
    )
    tasks[spread_id] = task


async def restore_scheduled_deletions(application: Application):
    """Restore deletion timers after a Railway restart."""
    records = await asyncio.to_thread(
        db.get_settings_by_prefix,
        AUTO_DELETE_SETTING_PREFIX,
    )
    restored = 0
    for key, raw_value in records.items():
        if raw_value == "deleted":
            continue
        try:
            payload = json.loads(raw_value)
            spread_id = int(key.split(":", 1)[1])
            message_id = int(payload["message_id"])
            delete_at = float(payload["delete_at"])
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            logger.warning("Ignoring invalid auto-delete setting %s", key)
            continue
        schedule_spread_deletion(application, spread_id, message_id, delete_at)
        restored += 1
    if restored:
        logger.info("Restored %s scheduled spread deletion(s)", restored)


def card_reaction_keyboard(
    spread_id: int, card_id: int, position: int
) -> InlineKeyboardMarkup:
    prefix = f"react:{spread_id}:{card_id}:{position}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💫 Мне это близко", callback_data=f"{prefix}:close")],
            [InlineKeyboardButton("🌿 Хочу осмыслить", callback_data=f"{prefix}:reflect")],
            [
                InlineKeyboardButton(
                    "🤍 Сейчас не откликается",
                    callback_data=f"{prefix}:not_now",
                )
            ],
        ]
    )


async def send_card_to_chat(
    bot,
    chat_id: int,
    card_id: int,
    spread_id: int | None = None,
    position: int | None = None,
):
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
            [[
                InlineKeyboardButton(
                    "🎧 Прослушать послание",
                    callback_data=(
                        f"voice:{spread_id}:{card_id}:{position}"
                        if spread_id is not None and position is not None
                        else f"voice:{card_id}"
                    ),
                )
            ]]
        ),
    )
    if spread_id is not None and position is not None:
        await bot.send_message(
            chat_id=chat_id,
            text="Как вам откликнулось это послание?",
            reply_markup=card_reaction_keyboard(spread_id, card_id, position),
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
        context.user_data.pop("pending_card_reflection", None)
        context.user_data.pop("pending_reflection_test", None)
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
    pending_reflection = context.user_data.get("pending_card_reflection")
    if pending_reflection is not None:
        status = await update.message.reply_text("🎙️ Слушаю ваш ответ…")
        try:
            voice = update.message.voice
            file = await voice.get_file()
            ogg_bytes = bytes(await file.download_as_bytearray())
            answer = await asyncio.to_thread(_transcribe_voice, ogg_bytes)
            if not reflection_answer_is_detailed(answer):
                await status.edit_text(reflection_clarification(answer))
                return
            await status.edit_text(f"Ваш ответ: «{answer}»\n\nГотовлю разбор…")
            await complete_card_reflection(update, context, answer)
        except Exception as exc:
            logger.exception("Voice reflection failed", exc_info=exc)
            await status.edit_text(
                "Не удалось обработать голосовой ответ. Попробуйте ещё раз или напишите текстом."
            )
        return

    if not is_admin(update):
        return

    pending_topic_spread_id = context.user_data.get(
        "pending_spread_question_topic_id"
    )
    if (
        context.user_data.get("pending_spread_question_topic")
        and pending_topic_spread_id is not None
    ):
        status = await update.message.reply_text("🎙️ Расшифровываю тему…")
        try:
            voice = update.message.voice
            file = await voice.get_file()
            ogg_bytes = bytes(await file.download_as_bytearray())
            topic = await asyncio.to_thread(_transcribe_voice, ogg_bytes)
            await status.edit_text(
                f"Тема: «{topic}»\n\nГотовлю короткий вопрос…"
            )
            question = await generate_spread_question_for_topic(
                int(pending_topic_spread_id), topic
            )
        except Exception as exc:
            logger.exception("Voice spread topic generation failed", exc_info=exc)
            await status.edit_text(
                "Не удалось подготовить вопрос. Попробуйте ещё раз голосом "
                "или напишите тему текстом."
            )
            return
        context.user_data.pop("pending_spread_question_topic", None)
        context.user_data.pop("pending_spread_question_topic_id", None)
        await status.edit_text(
            spread_question_screen_text(
                question, "✅ Короткий вопрос по вашей теме сохранён."
            ),
            parse_mode="HTML",
            reply_markup=spread_question_keyboard(
                int(pending_topic_spread_id), allow_generate=False
            ),
        )
        return

    pending_spread_id = context.user_data.get("pending_spread_voice_id")
    if pending_spread_id is not None:
        pending_spread_id = int(pending_spread_id)
        voice = update.message.voice
        await asyncio.to_thread(
            db.set_setting,
            _spread_voice_key(pending_spread_id),
            voice.file_id,
        )
        context.user_data.pop("pending_spread_voice_id", None)
        await update.message.reply_text(
            "👁 <b>Полный предпросмотр публикации</b>\n\n"
            "Ниже показано именно то, как публикация будет выглядеть в канале. "
            "Цифры в этом предпросмотре отключены.",
            parse_mode="HTML",
        )
        await send_complete_spread_preview(
            context,
            update.effective_chat.id,
            pending_spread_id,
            voice.file_id,
        )
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
    mapping = "\n".join(
        f"{position} → карта №{card_id}"
        for position, card_id in enumerate(card_ids, start=1)
    )

    with open(collage_path, "rb") as f:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=InputFile(f),
            caption=(
                f"{spread_caption()}\n\n"
                f"*Порядок карт для проверки:*\n{mapping}\n\n"
                "Если всё верно, опубликуй сейчас или запланируй дату и время."
            ),
            parse_mode="Markdown",
            reply_markup=spread_preview_keyboard(spread_id),
        )
    os.remove(collage_path)

    await update.message.reply_text("Расклад готов. В канал ничего не отправлено.")


async def spread_question_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is None or not query.data or not is_admin(update):
        return
    try:
        _, action, spread_id_text = query.data.split(":", 2)
        spread_id = int(spread_id_text)
    except (ValueError, IndexError):
        await query.answer("Не удалось определить расклад.", show_alert=True)
        return

    spread = await asyncio.to_thread(db.get_spread, spread_id)
    if spread is None:
        await query.answer("Расклад не найден.", show_alert=True)
        return

    if action == "show":
        context.user_data.pop("pending_spread_question_input", None)
        context.user_data.pop("pending_spread_question_id", None)
        context.user_data.pop("pending_spread_question_topic", None)
        context.user_data.pop("pending_spread_question_topic_id", None)
        await query.answer()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=spread_question_screen_text(spread.get("question")),
            parse_mode="HTML",
            reply_markup=spread_question_keyboard(spread_id),
        )
        return

    if action == "topic":
        clear_admin_input_states(context)
        context.user_data["pending_spread_question_topic"] = True
        context.user_data["pending_spread_question_topic_id"] = spread_id
        await query.answer()
        await query.edit_message_text(
            spread_question_screen_text(
                spread.get("question"),
                "Напишите или скажите голосом тему. Например: отношения.",
            ),
            parse_mode="HTML",
            reply_markup=spread_question_keyboard(
                spread_id, allow_generate=False
            ),
        )
        return

    if action in {"generate", "regenerate"}:
        await query.answer("Готовлю вариант…")
        cards, missing = await asyncio.to_thread(
            db.get_cards, spread["card_ids"]
        )
        if missing:
            notice = "Не удалось подготовить вопрос: часть карт не найдена."
            question = spread.get("question")
            keyboard = spread_question_keyboard(
                spread_id, allow_generate=False
            )
        else:
            try:
                question = await asyncio.to_thread(
                    _generate_spread_question, cards
                )
                await asyncio.to_thread(
                    db.update_spread_question, spread_id, question
                )
                notice = "✅ Вариант сохранён в раскладе."
                keyboard = spread_question_keyboard(
                    spread_id,
                    allow_generate=False,
                    allow_regenerate=action == "generate",
                )
            except Exception as exc:
                logger.warning(
                    "Could not generate question for spread %s: %s",
                    spread_id,
                    exc,
                )
                question = spread.get("question")
                notice = "Не удалось подготовить вопрос. Можно написать свой или убрать вопрос."
                keyboard = spread_question_keyboard(
                    spread_id, allow_generate=False
                )
        await query.edit_message_text(
            spread_question_screen_text(question, notice),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if action == "custom":
        clear_admin_input_states(context)
        context.user_data["pending_spread_question_input"] = True
        context.user_data["pending_spread_question_id"] = spread_id
        await query.answer()
        await query.edit_message_text(
            spread_question_screen_text(
                spread.get("question"),
                "Отправьте готовый вопрос текстом. Бот сохранит его без изменений.",
            ),
            parse_mode="HTML",
            reply_markup=spread_question_keyboard(
                spread_id, allow_generate=False
            ),
        )
        return

    if action == "remove":
        await asyncio.to_thread(db.update_spread_question, spread_id, None)
        context.user_data.pop("pending_spread_question_input", None)
        context.user_data.pop("pending_spread_question_id", None)
        context.user_data.pop("pending_spread_question_topic", None)
        context.user_data.pop("pending_spread_question_topic_id", None)
        await query.answer("Вопрос убран.")
        await query.edit_message_text(
            spread_question_screen_text(None, "✅ Вопрос удалён из расклада."),
            parse_mode="HTML",
            reply_markup=spread_question_keyboard(spread_id),
        )
        return

    if action == "back":
        clear_admin_input_states(context)
        has_voice = bool(await asyncio.to_thread(
            db.get_setting, _spread_voice_key(spread_id)
        ))
        await query.answer()
        await query.edit_message_text(
            f"Расклад #{spread_id}. Выберите следующее действие.",
            reply_markup=spread_preview_keyboard(spread_id, has_voice),
        )
        return

    await query.answer("Неизвестное действие.", show_alert=True)


async def record_spread_voice_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is None or not query.data or not is_admin(update):
        return

    try:
        spread_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Не удалось определить расклад.", show_alert=True)
        return

    spread = await asyncio.to_thread(db.get_spread, spread_id)
    if spread is None:
        await query.answer("Расклад не найден.", show_alert=True)
        return

    await query.answer("Готовлю текст на 30–40 секунд…")
    script = await asyncio.to_thread(
        db.get_setting, _spread_voice_script_key(spread_id)
    )
    if not script:
        cards, missing = await asyncio.to_thread(
            db.get_cards, spread["card_ids"]
        )
        if missing:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"❌ Не найдены карты: {missing}",
            )
            return
        recent_scripts = await asyncio.to_thread(
            db.get_recent_settings, SPREAD_VOICE_SCRIPT_PREFIX, 7
        )
        try:
            script = await asyncio.to_thread(
                _generate_spread_voice_script,
                cards,
                recent_scripts,
            )
        except Exception as exc:
            logger.exception("Could not generate author voice script", exc_info=exc)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ Не удалось подготовить текст. Нажмите кнопку ещё раз.",
            )
            return
        await asyncio.to_thread(
            db.set_setting,
            _spread_voice_script_key(spread_id),
            script,
        )

    context.user_data.pop("pending_card_reflection", None)
    context.user_data.pop("pending_reflection_test", None)
    context.user_data["pending_spread_voice_id"] = spread_id
    script_parts = script.split("\n", 1)
    hook = escape(script_parts[0].strip())
    body = escape(script_parts[1].strip()) if len(script_parts) > 1 else ""
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "🎙 <b>Текст для вашего голосового послания:</b>\n\n"
            f"<b>{hook}</b>\n\n{body}\n\n"
            "<b>Теперь запишите и отправьте сюда голосовое сообщение.</b>\n"
            "Можно читать не дословно — главное сохранить этот смысл и говорить от себя."
        ),
        parse_mode="HTML",
    )


async def preview_position_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is not None:
        await query.answer(
            "Это предпросмотр. В канале эта кнопка откроет выбранную карту.",
            show_alert=False,
        )


async def show_complete_preview_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is None or not query.data or not is_admin(update):
        return

    try:
        spread_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Не удалось определить расклад.", show_alert=True)
        return

    spread = await asyncio.to_thread(db.get_spread, spread_id)
    if spread is None:
        await query.answer("Расклад не найден.", show_alert=True)
        return
    voice_file_id = await asyncio.to_thread(
        db.get_setting, _spread_voice_key(spread_id)
    )
    if not voice_file_id:
        await query.answer(
            "Сначала запишите голосовое послание.",
            show_alert=True,
        )
        return

    await query.answer("Собираю полный предпросмотр…")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "👁 <b>Так публикация будет выглядеть в канале.</b>\n"
            "Сейчас она отправлена только вам и в канал не попала."
        ),
        parse_mode="HTML",
    )
    await send_complete_spread_preview(
        context,
        query.message.chat_id,
        spread_id,
        voice_file_id,
    )


class AuthorVoicePublishError(RuntimeError):
    """Raised after the photo is rolled back because author voice failed."""


async def publish_spread_to_channel(
    application: Application,
    spread_id: int,
    spread: dict,
    voice_file_id: str,
):
    """Publish the shared photo-and-voice channel sequence for one spread."""
    back_url = await asyncio.to_thread(db.get_card_back_url)
    collage_path = await asyncio.to_thread(build_collage, back_url, spread_id)
    try:
        with open(collage_path, "rb") as image:
            message = await application.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=InputFile(image),
                caption=spread_caption(spread.get("question")),
                parse_mode="Markdown",
                reply_markup=spread_pick_keyboard(spread_id, spread["card_ids"]),
            )
    finally:
        os.remove(collage_path)

    try:
        voice_message = await application.bot.send_voice(
            chat_id=CHANNEL_ID,
            voice=voice_file_id,
            caption="🎙 Моё личное послание к сегодняшним картам",
        )
    except TelegramError as exc:
        try:
            await application.bot.delete_message(
                chat_id=CHANNEL_ID,
                message_id=message.message_id,
            )
        except TelegramError:
            logger.exception(
                "Could not roll back photo post for spread %s", spread_id
            )
        raise AuthorVoicePublishError from exc

    await asyncio.to_thread(
        db.set_setting,
        _spread_channel_voice_key(spread_id),
        str(voice_message.message_id),
    )

    previous_spreads = await asyncio.to_thread(db.get_published_spreads)
    for previous_spread in previous_spreads:
        if previous_spread["id"] == spread_id:
            continue

        previous_voice_id = await asyncio.to_thread(
            db.get_setting,
            _spread_channel_voice_key(previous_spread["id"]),
        )
        if previous_voice_id and previous_voice_id != "deleted":
            try:
                await application.bot.delete_message(
                    chat_id=CHANNEL_ID,
                    message_id=int(previous_voice_id),
                )
            except (TelegramError, ValueError) as exc:
                logger.warning(
                    "Could not delete previous author voice for spread %s: %s",
                    previous_spread["id"],
                    exc,
                )
            finally:
                await asyncio.to_thread(
                    db.set_setting,
                    _spread_channel_voice_key(previous_spread["id"]),
                    "deleted",
                )

        removed = False
        try:
            await application.bot.delete_message(
                chat_id=CHANNEL_ID,
                message_id=previous_spread["channel_message_id"],
            )
            removed = True
            logger.info(
                "Deleted previous spread %s before publishing %s",
                previous_spread["id"],
                spread_id,
            )
        except BadRequest as exc:
            if "message to delete not found" in str(exc).lower():
                removed = True
                logger.info(
                    "Previous spread %s was already absent from the channel",
                    previous_spread["id"],
                )
            else:
                logger.warning(
                    "Could not delete previous spread %s: %s",
                    previous_spread["id"],
                    exc,
                )
        except TelegramError as exc:
            logger.warning(
                "Could not delete previous spread %s: %s",
                previous_spread["id"],
                exc,
            )

        if not removed:
            continue

        await asyncio.to_thread(db.clear_spread_message, previous_spread["id"])
        await asyncio.to_thread(
            db.set_setting,
            _auto_delete_setting_key(previous_spread["id"]),
            "deleted",
        )
        old_task = application.bot_data.get("spread_delete_tasks", {}).pop(
            previous_spread["id"], None
        )
        if old_task and not old_task.done():
            old_task.cancel()

    await asyncio.to_thread(
        db.update_spread_message, spread_id, message.message_id
    )
    await record_analytics_event(
        event_type="spread_published",
        idempotency_key=f"spread:{spread_id}:published",
        spread_id=spread_id,
    )
    delete_at = time.time() + AUTO_DELETE_SECONDS
    await asyncio.to_thread(
        db.set_setting,
        _auto_delete_setting_key(spread_id),
        json.dumps(
            {"message_id": message.message_id, "delete_at": delete_at},
            separators=(",", ":"),
        ),
    )
    schedule_spread_deletion(
        application, spread_id, message.message_id, delete_at
    )


async def publish_scheduled_spread(
    application: Application,
    spread_id: int,
    admin_chat_id: int | None = None,
):
    """Publish one prepared spread without a live callback query."""
    spread = await asyncio.to_thread(db.get_spread, spread_id)
    if spread is None or spread.get("channel_message_id"):
        return

    voice_file_id = await asyncio.to_thread(
        db.get_setting, _spread_voice_key(spread_id)
    )
    if not voice_file_id:
        raise RuntimeError(f"Spread #{spread_id} has no author voice")

    await publish_spread_to_channel(
        application, spread_id, spread, voice_file_id
    )
    await asyncio.to_thread(
        db.set_setting, _scheduled_spread_key(spread_id), "published"
    )
    if admin_chat_id:
        await application.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                f"✅ Запланированный расклад #{spread_id} опубликован "
                "в канале."
            ),
        )


async def scheduled_publish_worker(
    application: Application,
    spread_id: int,
    publish_at: float,
    admin_chat_id: int | None,
):
    delay = max(0, publish_at - time.time())
    if delay:
        await asyncio.sleep(delay)
    try:
        await publish_scheduled_spread(
            application, spread_id, admin_chat_id
        )
    except Exception as exc:
        logger.exception(
            "Scheduled publication failed for spread %s", spread_id,
            exc_info=exc,
        )
        await asyncio.to_thread(
            db.set_setting, _scheduled_spread_key(spread_id), "failed"
        )
        if admin_chat_id:
            await application.bot.send_message(
                chat_id=admin_chat_id,
                text=(
                    f"❌ Не удалось опубликовать запланированный расклад "
                    f"#{spread_id}. Откройте его и попробуйте ещё раз."
                ),
            )


def schedule_spread_publication(
    application: Application,
    spread_id: int,
    publish_at: float,
    admin_chat_id: int | None,
):
    tasks = application.bot_data.setdefault("spread_publish_tasks", {})
    old_task = tasks.get(spread_id)
    if old_task and not old_task.done():
        old_task.cancel()
    tasks[spread_id] = asyncio.create_task(
        scheduled_publish_worker(
            application, spread_id, publish_at, admin_chat_id
        )
    )


def parse_moscow_schedule(text: str) -> datetime:
    """Parse common Russian date/time forms in Moscow time."""
    value = " ".join(text.lower().replace(",", " ").strip().split())
    value = re.sub(r"\s+в\s+", " ", value)
    value = re.sub(r"\s*(?:ч|час(?:а|ов)?)\.?$", "", value)
    now = datetime.now(MOSCOW_TZ)

    time_pattern = r"([01]?\d|2[0-3])[:.]([0-5]\d)"

    relative_match = re.fullmatch(
        rf"(сегодня|завтра)\s+{time_pattern}", value
    )
    if relative_match:
        relative, hour, minute = relative_match.groups()
        day = (now + timedelta(days=1 if relative == "завтра" else 0)).date()
        result = datetime(
            day.year, day.month, day.day, int(hour), int(minute), tzinfo=MOSCOW_TZ
        )
        if relative == "сегодня" and result <= now:
            raise ValueError("Today's time is already past")
        return result

    date_match = re.fullmatch(
        rf"(\d{{1,2}})[./-](\d{{1,2}})(?:[./-](\d{{2}}|\d{{4}}))?\s+{time_pattern}",
        value,
    )
    if date_match:
        day, month, year, hour, minute = date_match.groups()
        year_value = int(year) if year else now.year
        if year and len(year) == 2:
            year_value += 2000
        result = datetime(
            year_value,
            int(month),
            int(day),
            int(hour),
            int(minute),
            tzinfo=MOSCOW_TZ,
        )
        if not year and result <= now:
            result = result.replace(year=now.year + 1)
        return result

    month_names = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
        "мая": 5, "июня": 6, "июля": 7, "августа": 8,
        "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    }
    words_match = re.fullmatch(
        rf"(\d{{1,2}})\s+({'|'.join(month_names)})(?:\s+(\d{{4}}))?\s+{time_pattern}",
        value,
    )
    if words_match:
        day, month_word, year, hour, minute = words_match.groups()
        result = datetime(
            int(year) if year else now.year,
            month_names[month_word],
            int(day),
            int(hour),
            int(minute),
            tzinfo=MOSCOW_TZ,
        )
        if not year and result <= now:
            result = result.replace(year=now.year + 1)
        return result

    time_match = re.fullmatch(time_pattern, value)
    if time_match:
        hour, minute = map(int, time_match.groups())
        result = now.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if result <= now:
            result += timedelta(days=1)
        return result

    raise ValueError("Unsupported schedule format")


async def schedule_spread_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is None or not query.data or not is_admin(update):
        return
    try:
        spread_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Не удалось определить расклад.", show_alert=True)
        return

    spread = await asyncio.to_thread(db.get_spread, spread_id)
    voice_file_id = await asyncio.to_thread(
        db.get_setting, _spread_voice_key(spread_id)
    )
    if spread is None or spread.get("channel_message_id"):
        await query.answer("Расклад недоступен для планирования.", show_alert=True)
        return
    if not voice_file_id:
        await query.answer(
            "Сначала запишите голосовое послание.", show_alert=True
        )
        return

    context.user_data.pop("pending_card_reflection", None)
    context.user_data.pop("pending_reflection_test", None)
    context.user_data["pending_schedule_spread_id"] = spread_id
    await query.answer("Жду дату и время.")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "🕗 <b>Когда опубликовать расклад?</b>\n\n"
            "Отправьте время по Москве в одном из форматов:\n"
            "• <code>завтра 08:00</code>\n"
            "• <code>1.08 09.00</code>\n"
            "• <code>1 августа в 09:00</code>\n"
            "• <code>31.07 08:00</code>\n"
            "• <code>08:00</code> — ближайшее такое время\n\n"
            "После этого бот сохранит расписание и покажет подтверждение."
        ),
        parse_mode="HTML",
    )


async def restore_scheduled_publications(application: Application):
    records = await asyncio.to_thread(
        db.get_settings_by_prefix, SCHEDULED_SPREAD_PREFIX
    )
    restored = 0
    for key, raw_value in records.items():
        if raw_value in {"published", "cancelled", "failed"}:
            continue
        try:
            payload = json.loads(raw_value)
            spread_id = int(key.rsplit(":", 1)[1])
            publish_at = float(payload["publish_at"])
            admin_chat_id = int(payload["admin_chat_id"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        schedule_spread_publication(
            application, spread_id, publish_at, admin_chat_id
        )
        restored += 1
    if restored:
        logger.info("Restored %s scheduled publication(s)", restored)


async def publish_spread_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or not query.data or not is_admin(update):
        return

    action, spread_id_text = query.data.split(":", 1)
    try:
        spread_id = int(spread_id_text)
    except ValueError:
        await query.answer("Не удалось определить расклад.", show_alert=True)
        return

    spread = await asyncio.to_thread(db.get_spread, spread_id)
    if spread is None:
        await query.answer("Расклад не найден.", show_alert=True)
        return

    if action == "cancel-spread":
        clear_admin_input_states(context)
        scheduled_task = context.application.bot_data.get(
            "spread_publish_tasks", {}
        ).pop(spread_id, None)
        if scheduled_task and not scheduled_task.done():
            scheduled_task.cancel()
        await asyncio.to_thread(
            db.set_setting, _scheduled_spread_key(spread_id), "cancelled"
        )
        await query.answer("Публикация отменена.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Расклад #{spread_id} отменён. В канал ничего не отправлено.",
        )
        return

    if spread.get("channel_message_id"):
        await query.answer("Этот расклад уже опубликован.", show_alert=True)
        return

    scheduled_task = context.application.bot_data.get(
        "spread_publish_tasks", {}
    ).pop(spread_id, None)
    if scheduled_task and not scheduled_task.done():
        scheduled_task.cancel()
    await asyncio.to_thread(
        db.set_setting, _scheduled_spread_key(spread_id), "cancelled"
    )

    voice_file_id = await asyncio.to_thread(
        db.get_setting, _spread_voice_key(spread_id)
    )
    if not voice_file_id:
        await query.answer(
            "Сначала нажмите «Записать моё послание» и отправьте голосовое.",
            show_alert=True,
        )
        return

    try:
        await publish_spread_to_channel(
            context.application,
            spread_id,
            spread,
            voice_file_id,
        )
    except AuthorVoicePublishError as exc:
        logger.exception(
            "Could not publish author voice for spread %s: %s",
            spread_id,
            exc,
        )
        await query.answer(
            "Голос не отправился. Публикация отменена, попробуйте ещё раз.",
            show_alert=True,
        )
        return
    except TelegramError:
        logger.exception(
            "Could not publish spread %s to channel %s", spread_id, CHANNEL_ID
        )
        await query.answer(
            "Не удалось опубликовать. Проверьте, что бот — администратор канала.",
            show_alert=True,
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "❌ Не удалось опубликовать расклад в канал. Бот должен быть "
                "добавлен в канал администратором с правом публикации."
            ),
        )
        return
    await query.answer("Расклад опубликован в канале.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"✅ Расклад #{spread_id} опубликован в канале. Бот удалит этот пост через 72 часа.",
    )


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if is_admin(update) and text == "🏠 Главное меню":
        clear_admin_input_states(context)
        await send_admin_main_menu(context.bot, update.effective_chat.id)
        return

    if is_admin(update) and context.user_data.get("pending_spread_question_topic"):
        spread_id = context.user_data.get("pending_spread_question_topic_id")
        status = await update.message.reply_text(
            f"Тема: «{text}»\n\nГотовлю короткий вопрос…"
        )
        try:
            question = await generate_spread_question_for_topic(
                int(spread_id), text
            )
        except Exception as exc:
            logger.exception("Text spread topic generation failed", exc_info=exc)
            await status.edit_text(
                "Не удалось подготовить вопрос. Попробуйте другую формулировку "
                "темы или отправьте её голосом."
            )
            return
        context.user_data.pop("pending_spread_question_topic", None)
        context.user_data.pop("pending_spread_question_topic_id", None)
        await status.edit_text(
            spread_question_screen_text(
                question, "✅ Короткий вопрос по вашей теме сохранён."
            ),
            parse_mode="HTML",
            reply_markup=spread_question_keyboard(
                int(spread_id), allow_generate=False
            ),
        )
        return

    if is_admin(update) and context.user_data.get("pending_spread_question_input"):
        spread_id = context.user_data.get("pending_spread_question_id")
        try:
            question = normalize_spread_question(text)
        except ValueError as exc:
            await update.message.reply_text(
                f"❌ {exc}\n\nНапишите более короткий вопрос — не более "
                f"{SPREAD_QUESTION_MAX_LENGTH} символов."
            )
            return

        spread = await asyncio.to_thread(db.get_spread, spread_id)
        if spread is None:
            clear_admin_input_states(context)
            await update.message.reply_text("Расклад не найден.")
            return

        await asyncio.to_thread(db.update_spread_question, spread_id, question)
        context.user_data.pop("pending_spread_question_input", None)
        context.user_data.pop("pending_spread_question_id", None)
        await update.message.reply_text(
            spread_question_screen_text(question, "✅ Вопрос сохранён."),
            parse_mode="HTML",
            reply_markup=spread_question_keyboard(spread_id),
        )
        return

    if is_admin(update) and context.user_data.get("pending_newspread_menu"):
        card_numbers = text.split()
        if len(card_numbers) != 6 or not all(item.isdigit() for item in card_numbers):
            await update.message.reply_text(
                "Нужно отправить ровно шесть номеров через пробел. Например: "
                "<code>3 25 36 48 71 104</code>",
                parse_mode="HTML",
            )
            return
        context.user_data.pop("pending_newspread_menu", None)
        context.args = card_numbers
        await newspread(update, context)
        return

    if is_admin(update) and context.user_data.get("pending_review_menu"):
        if not text.isdigit() or not 1 <= int(text) <= 120:
            await update.message.reply_text("Отправьте номер карты от 1 до 120.")
            return
        context.user_data.pop("pending_review_menu", None)
        await send_review_card(context.bot, update.effective_chat.id, int(text), context)
        return

    if is_admin(update) and context.user_data.get("pending_test_menu"):
        if not text.isdigit() or not 1 <= int(text) <= 120:
            await update.message.reply_text("Отправьте номер карты от 1 до 120.")
            return
        context.user_data.pop("pending_test_menu", None)
        await send_card_to_chat(
            context.bot,
            update.effective_chat.id,
            int(text),
            spread_id=0,
            position=1,
        )
        return

    pending_card_reflection = context.user_data.get("pending_card_reflection")
    if pending_card_reflection is not None:
        if not reflection_answer_is_detailed(text):
            await update.message.reply_text(reflection_clarification(text))
            return
        await update.message.reply_text("Готовлю короткий разбор…")
        await complete_card_reflection(update, context, text)
        return

    pending_reflection = context.user_data.get("pending_reflection_test")
    if is_admin(update) and pending_reflection is not None:
        if len(text) < 3:
            await update.message.reply_text(
                "Напишите ответ чуть подробнее — хотя бы одним предложением."
            )
            return
        await update.message.reply_text("Собираю тестовый разбор…")
        try:
            card = await asyncio.to_thread(
                db.get_card, int(pending_reflection["card_id"])
            )
            if card is None:
                raise RuntimeError("Card not found")
            reflection = await asyncio.to_thread(
                _generate_safe_reflection,
                card,
                pending_reflection["question"],
                text,
            )
        except Exception as exc:
            logger.exception("Reflection test failed", exc_info=exc)
            await update.message.reply_text(
                "Не удалось подготовить тестовый разбор. Попробуйте ещё раз."
            )
            return
        context.user_data.pop("pending_reflection_test", None)
        await update.message.reply_text(
            "🧭 <b>Тестовый разбор</b>\n\n"
            f"{escape(reflection)}\n\n"
            "<i>Это автоматическое отражение на основе карты и материалов "
            "Дмитрия, а не диагностика и не личная консультация.</i>",
            parse_mode="HTML",
        )
        return

    pending_schedule_id = context.user_data.get(
        "pending_schedule_spread_id"
    )
    if is_admin(update) and pending_schedule_id is not None:
        try:
            scheduled_at = parse_moscow_schedule(text)
        except (ValueError, OverflowError):
            await update.message.reply_text(
                "Не понял дату и время. Отправьте, например: "
                "<code>завтра 08:00</code>, <code>1.08 09.00</code> "
                "или <code>1 августа в 09:00</code>.",
                parse_mode="HTML",
            )
            return
        if scheduled_at.timestamp() <= time.time() + 30:
            await update.message.reply_text(
                "Укажите время минимум на одну минуту позже текущего."
            )
            return

        spread_id = int(pending_schedule_id)
        payload = {
            "publish_at": scheduled_at.timestamp(),
            "admin_chat_id": update.effective_chat.id,
        }
        await asyncio.to_thread(
            db.set_setting,
            _scheduled_spread_key(spread_id),
            json.dumps(payload, separators=(",", ":")),
        )
        schedule_spread_publication(
            context.application,
            spread_id,
            scheduled_at.timestamp(),
            update.effective_chat.id,
        )
        context.user_data.pop("pending_schedule_spread_id", None)
        await update.message.reply_text(
            "✅ <b>Публикация запланирована</b>\n\n"
            f"Расклад: #{spread_id}\n"
            f"Дата и время: <b>{scheduled_at:%d.%m.%Y в %H:%M}</b>\n"
            "Часовой пояс: Москва.\n\n"
            "Бот самостоятельно опубликует карты и ваше голосовое.",
            parse_mode="HTML",
        )
        return

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

    await send_card_to_chat(
        context.bot,
        update.effective_chat.id,
        card_id,
        spread["id"],
        position,
    )
    await record_analytics_event(
        event_type="card_opened",
        idempotency_key=f"telegram:update:{update.update_id}:card_opened",
        spread_id=spread["id"],
        card_id=card_id,
        card_position=position,
        actor_hash=_actor_hash(update.effective_user.id),
    )


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
        spread_id = spread["id"] if spread else None
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

    card_id = spread["card_ids"][position - 1]
    await record_analytics_event(
        event_type="card_button_clicked",
        idempotency_key=f"telegram:update:{update.update_id}:card_button_clicked",
        spread_id=spread["id"],
        card_id=card_id,
        card_position=position,
        actor_hash=_actor_hash(query.from_user.id),
    )

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
            "Откройте бота и нажмите Start — карта придёт автоматически.",
            url=f"{BOT_LINK}?start=spread_{spread_id}_{position}",
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

    selected_count = len(claim["selections"])
    await query.answer(f"Открываю карту {selected_count} из {MAX_CARDS_PER_SPREAD}…")
    try:
        await send_card_to_chat(
            context.bot,
            query.from_user.id,
            card_id,
            spread["id"],
            position,
        )
        await record_analytics_event(
            event_type="card_opened",
            idempotency_key=f"telegram:update:{update.update_id}:card_opened",
            spread_id=spread["id"],
            card_id=card_id,
            card_position=position,
            actor_hash=_actor_hash(query.from_user.id),
        )
    except Exception as exc:
        logger.exception("Could not send selected card", exc_info=exc)


async def voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send voice reading only after a subscriber explicitly asks for it."""
    query = update.callback_query
    if query is None or not query.data:
        return

    parts = query.data.split(":")
    try:
        if len(parts) == 4:
            spread_id = int(parts[1])
            card_id = int(parts[2])
            position = int(parts[3])
        else:
            spread_id = None
            card_id = int(parts[1])
            position = None
    except (IndexError, ValueError):
        await query.answer("Не удалось открыть послание.", show_alert=True)
        return

    allowed, message = await require_channel_subscription(context.bot, query.from_user.id)
    if not allowed:
        await query.answer(message, show_alert=True)
        return


    if spread_id != 0:
        await record_analytics_event(
            event_type="voice_requested",
            idempotency_key=f"telegram:update:{update.update_id}:voice_requested",
            spread_id=spread_id,
            card_id=card_id,
            card_position=position,
            actor_hash=_actor_hash(query.from_user.id),
        )

    await query.answer("Озвучиваю послание...")
    try:
        await send_card_voice(context.bot, query.from_user.id, card_id)
        if spread_id != 0:
            await record_analytics_event(
                event_type="voice_sent",
                idempotency_key=f"telegram:update:{update.update_id}:voice_sent",
                spread_id=spread_id,
                card_id=card_id,
                card_position=position,
                actor_hash=_actor_hash(query.from_user.id),
            )
    except Exception:
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="Не удалось озвучить послание. Попробуй нажать кнопку ещё раз чуть позже.",
        )


async def card_reaction_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is None or not query.data:
        return
    try:
        _, spread_text, card_text, position_text, reaction = query.data.split(":")
        spread_id = int(spread_text)
        card_id = int(card_text)
        position = int(position_text)
    except (ValueError, IndexError):
        await query.answer("Не удалось сохранить отклик.", show_alert=True)
        return

    responses = {
        "close": (
            "Спасибо за отклик. Возьмите из послания только то, что помогает "
            "вам лучше услышать себя."
        ),
        "reflect": (
            "Необязательно понимать всё сразу. Можно оставить послание как "
            "повод для наблюдения и вернуться к нему позже."
        ),
        "not_now": (
            "Это нормально. Карта не обязана описывать вас или вашу ситуацию. "
            "Вы можете просто оставить это послание без дальнейших выводов."
        ),
    }
    if reaction not in responses:
        await query.answer("Не удалось сохранить отклик.", show_alert=True)
        return

    if spread_id != 0:
        try:
            await asyncio.to_thread(
                db.set_card_reaction,
                spread_id=spread_id,
                card_id=card_id,
                card_position=position,
                actor_hash=_actor_hash(query.from_user.id),
                reaction_type=reaction,
                idempotency_key=f"telegram:update:{update.update_id}:reaction",
            )
        except Exception as exc:
            logger.warning("Card reaction was not recorded: %s", exc)

    await query.answer("Отклик сохранён.")
    if reaction == "reflect":
        try:
            for pending_key in (
                "pending_reflection_test",
                "pending_schedule_spread_id",
                "pending_spread_voice_id",
                "pending_card_id",
                "pending_card_image_url",
            ):
                context.user_data.pop(pending_key, None)
            await query.edit_message_text("🌿 Подбираю вопрос для осмысления…")
            card = await asyncio.to_thread(db.get_card, card_id)
            if card is None:
                raise RuntimeError("Card not found")
            question = await asyncio.to_thread(_generate_reflection_question, card)
            await query.edit_message_text(
                "🌿 <b>Вопрос для осмысления</b>\n\n"
                f"{escape(question)}\n\n"
                "Ответьте одним сообщением — текстом или голосом. "
                "Я помогу связать ваш ответ со смыслом карты.",
                parse_mode="HTML",
            )
            context.user_data["pending_card_reflection"] = {
                "spread_id": spread_id,
                "card_id": card_id,
                "position": position,
                "question": question,
            }
            if spread_id != 0:
                await record_analytics_event(
                    event_type="reflection_question_shown",
                    idempotency_key=(
                        f"telegram:update:{update.update_id}:reflection_question_shown"
                    ),
                    spread_id=spread_id,
                    card_id=card_id,
                    card_position=position,
                    actor_hash=_actor_hash(query.from_user.id),
                    metadata={"prompt_version": REFLECTION_PROMPT_VERSION},
                )
        except Exception as exc:
            context.user_data.pop("pending_card_reflection", None)
            logger.exception("Reflection question failed", exc_info=exc)
            await query.edit_message_text(
                "Не удалось подготовить вопрос. Попробуйте нажать кнопку ещё раз чуть позже."
            )
        return

    try:
        await query.edit_message_text(responses[reaction])
    except TelegramError:
        await context.bot.send_message(query.from_user.id, responses[reaction])


async def complete_card_reflection(
    update: Update, context: ContextTypes.DEFAULT_TYPE, answer: str
):
    """Return a bounded reflection grounded in the card and user's own words."""
    pending = context.user_data.get("pending_card_reflection")
    if pending is None:
        return
    try:
        card = await asyncio.to_thread(db.get_card, int(pending["card_id"]))
        if card is None:
            raise RuntimeError("Card not found")
        spread_id = int(pending.get("spread_id", 0))
        card_id = int(pending["card_id"])
        position = int(pending.get("position", 1))
        if spread_id != 0:
            await record_analytics_event(
                event_type="reflection_answered",
                idempotency_key=f"telegram:update:{update.update_id}:reflection_answered",
                spread_id=spread_id,
                card_id=card_id,
                card_position=position,
                actor_hash=_actor_hash(update.effective_user.id),
                metadata={"prompt_version": REFLECTION_PROMPT_VERSION},
            )
        reflection = await asyncio.to_thread(
            _generate_safe_reflection,
            card,
            pending["question"],
            answer,
        )
    except Exception as exc:
        logger.exception("Card reflection failed", exc_info=exc)
        await update.message.reply_text(
            "Не удалось подготовить разбор. Попробуйте отправить ответ ещё раз чуть позже."
        )
        return

    if spread_id != 0:
        await record_analytics_event(
            event_type="reflection_completed",
            idempotency_key=f"telegram:update:{update.update_id}:reflection_completed",
            spread_id=spread_id,
            card_id=card_id,
            card_position=position,
            actor_hash=_actor_hash(update.effective_user.id),
            metadata={"prompt_version": REFLECTION_PROMPT_VERSION},
        )
    context.user_data.pop("pending_card_reflection", None)
    await update.message.reply_text(
        "🧭 <b>Ваш разбор</b>\n\n"
        f"{escape(reflection)}\n\n"
        "<i>Это бережное отражение по смыслу карты и вашим словам, "
        "а не диагностика или личная консультация.</i>\n\n"
        "<b>Насколько этот разбор вам откликается?</b>",
        parse_mode="HTML",
        reply_markup=reflection_feedback_keyboard(
            spread_id, card_id, position
        ),
    )


def reflection_feedback_keyboard(
    spread_id: int, card_id: int, position: int
) -> InlineKeyboardMarkup:
    prefix = f"reflection-feedback:{spread_id}:{card_id}:{position}"
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🎯 Да, точно", callback_data=f"{prefix}:yes"),
            InlineKeyboardButton("🤔 Частично", callback_data=f"{prefix}:partly"),
        ], [
            InlineKeyboardButton(
                "❌ Нет, не про меня", callback_data=f"{prefix}:no"
            )
        ]]
    )


async def reflection_feedback_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is None or not query.data:
        return
    try:
        _, spread_text, card_text, position_text, feedback = query.data.split(":")
        spread_id = int(spread_text)
        card_id = int(card_text)
        position = int(position_text)
    except (ValueError, IndexError):
        await query.answer("Не удалось сохранить ответ.", show_alert=True)
        return
    if feedback not in {"yes", "partly", "no"}:
        await query.answer("Не удалось сохранить ответ.", show_alert=True)
        return

    if spread_id != 0:
        await record_analytics_event(
            event_type="reflection_feedback",
            idempotency_key=f"telegram:update:{update.update_id}:reflection_feedback",
            spread_id=spread_id,
            card_id=card_id,
            card_position=position,
            actor_hash=_actor_hash(query.from_user.id),
            reaction_type=feedback,
            metadata={"prompt_version": REFLECTION_PROMPT_VERSION},
        )
    await query.answer("Спасибо, ваш ответ сохранён.", show_alert=False)
    await query.edit_message_reply_markup(reply_markup=None)


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


async def test_reflection_dialog(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Start an admin-only card → question → answer → reflection test."""
    if not is_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Использование: <code>/testdialog 9</code>",
            parse_mode="HTML",
        )
        return
    card_id = int(context.args[0])
    card = await asyncio.to_thread(db.get_card, card_id)
    if card is None:
        await update.message.reply_text(f"Карта №{card_id} не найдена.")
        return
    await update.message.reply_text("Готовлю вопрос для теста…")
    try:
        question = await asyncio.to_thread(_generate_reflection_question, card)
    except Exception as exc:
        logger.exception("Reflection question generation failed", exc_info=exc)
        await update.message.reply_text(
            "Не удалось подготовить вопрос. Попробуйте ещё раз."
        )
        return
    context.user_data["pending_reflection_test"] = {
        "card_id": card_id,
        "question": question,
    }
    await update.message.reply_photo(photo=card["image_url"])
    await update.message.reply_text(
        f"🧪 <b>Закрытый тест карты №{card_id}</b>\n\n"
        f"{escape(question)}\n\n"
        "Ответьте на вопрос одним–тремя предложениями. Следующее ваше "
        "текстовое сообщение бот использует только для тестового разбора.",
        parse_mode="HTML",
    )


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


def analytics_dashboard_keyboard(active_days: int = 7) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Сегодня", callback_data="admin-stats:1"),
            InlineKeyboardButton("7 дней", callback_data="admin-stats:7"),
            InlineKeyboardButton("30 дней", callback_data="admin-stats:30"),
        ]]
    )


def format_reflection_quality(data: dict) -> str:
    counts = data.get("event_counts", {})
    feedback = data.get("reflection_feedback_counts", {})
    yes = int(feedback.get("yes", 0))
    partly = int(feedback.get("partly", 0))
    no = int(feedback.get("no", 0))
    feedback_total = yes + partly + no
    quality_score = round((yes + partly * 0.5) / feedback_total * 100) if feedback_total else 0
    questions = int(counts.get("reflection_question_shown", 0))
    answered = int(counts.get("reflection_answered", 0))
    completed = int(counts.get("reflection_completed", 0))
    answer_rate = round(answered / questions * 100) if questions else 0

    by_card = data.get("reflection_feedback_by_card", {})
    problem_cards = []
    for raw_card_id, card_feedback in by_card.items():
        negative_weight = int(card_feedback.get("no", 0)) * 2 + int(
            card_feedback.get("partly", 0)
        )
        if negative_weight:
            problem_cards.append((negative_weight, int(raw_card_id), card_feedback))
    problem_cards.sort(reverse=True)
    review_line = "нет данных"
    if problem_cards:
        review_line = ", ".join(
            f"№{card_id} (частично {card_feedback.get('partly', 0)}, нет {card_feedback.get('no', 0)})"
            for _, card_id, card_feedback in problem_cards[:3]
        )

    return (
        "🧠 <b>Качество Gemini</b>\n"
        f"Вопрос показан: <b>{questions}</b>\n"
        f"Ответили: <b>{answered}</b> ({answer_rate}%)\n"
        f"Получили разбор: <b>{completed}</b>\n"
        f"Оценили разбор: <b>{feedback_total}</b>\n"
        f"Индекс точности: <b>{quality_score}%</b>\n"
        f"На проверку: <b>{review_line}</b>\n"
        f"Версия настроек: <b>{REFLECTION_PROMPT_VERSION}</b>"
    )


async def build_dashboard_text(bot, days: int) -> str:
    data = await asyncio.to_thread(db.get_stats, days)
    counts = data.get("event_counts", {})
    reactions = data.get("reaction_counts", {})
    feedback = data.get("reflection_feedback_counts", {})
    try:
        subscribers = await bot.get_chat_member_count(CHANNEL_ID)
        subscriber_text = str(subscribers)
    except TelegramError:
        subscriber_text = "недоступно"
    return (
        f"📊 <b>Панель «Карта дня» — {days} дн.</b>\n\n"
        f"👥 Подписчиков канала сейчас: <b>{subscriber_text}</b>\n"
        f"👤 Получили хотя бы одну карту: <b>{data.get('unique_card_openers', 0)}</b>\n"
        f"🔢 Нажатий на номера: <b>{counts.get('card_button_clicked', 0)}</b>\n"
        f"🃏 Успешно открыто карт: <b>{counts.get('card_opened', 0)}</b>\n"
        f"🎧 Запросов озвучивания: <b>{counts.get('voice_requested', 0)}</b>\n\n"
        "💬 <b>Отклики</b>\n"
        f"💫 Мне это близко: <b>{reactions.get('close', 0)}</b>\n"
        f"🌿 Хочу осмыслить: <b>{reactions.get('reflect', 0)}</b>\n"
        f"🤍 Сейчас не откликается: <b>{reactions.get('not_now', 0)}</b>\n\n"
        "🎯 <b>Точность разбора</b>\n"
        f"Да, точно: <b>{feedback.get('yes', 0)}</b>\n"
        f"Частично: <b>{feedback.get('partly', 0)}</b>\n"
        f"Нет, не про меня: <b>{feedback.get('no', 0)}</b>\n\n"
        f"{format_reflection_quality(data)}\n\n"
        "<i>Telegram показывает текущее число подписчиков. История точных "
        "подписок, отписок и просмотров публикаций пока не подключена.</i>"
    )


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        await build_dashboard_text(context.bot, 7),
        parse_mode="HTML",
        reply_markup=analytics_dashboard_keyboard(7),
    )


async def dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or not query.data or not is_admin(update):
        return
    try:
        days = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Не удалось открыть статистику.", show_alert=True)
        return
    await query.answer("Обновляю статистику…")
    await query.edit_message_text(
        await build_dashboard_text(context.bot, days),
        parse_mode="HTML",
        reply_markup=analytics_dashboard_keyboard(days),
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show anonymous CardBot analytics to the administrator."""
    if not is_admin(update):
        return
    try:
        if context.args and context.args[0].lower() == "spread":
            if len(context.args) < 2 or not context.args[1].isdigit():
                await update.message.reply_text(
                    "Использование: <code>/stats spread 30</code>",
                    parse_mode="HTML",
                )
                return
            spread_id = int(context.args[1])
            data = await asyncio.to_thread(db.get_spread_stats, spread_id)
            title = f"Статистика расклада №{spread_id}"
        else:
            days = 7
            if context.args:
                if not context.args[0].isdigit():
                    await update.message.reply_text(
                        "Использование: <code>/stats</code>, "
                        "<code>/stats 30</code> или "
                        "<code>/stats spread 30</code>",
                        parse_mode="HTML",
                    )
                    return
                days = min(max(int(context.args[0]), 1), 365)
            data = await asyncio.to_thread(db.get_stats, days)
            title = f"Статистика за {days} дн."
    except Exception as exc:
        logger.exception("Could not build analytics report", exc_info=exc)
        await update.message.reply_text(
            "Статистика пока недоступна. Проверьте таблицы аналитики в Supabase."
        )
        return

    counts = data.get("event_counts", {})
    reactions = data.get("reaction_counts", {})
    feedback = data.get("reflection_feedback_counts", {})
    positions = data.get("card_opened_by_position", {})
    position_lines = "\n".join(
        f"• {position} — {positions.get(position, positions.get(str(position), 0))}"
        for position in range(1, 7)
    )
    await update.message.reply_text(
        f"📊 <b>{escape(title)}</b>\n\n"
        f"Уникальных получателей: <b>{data.get('unique_card_openers', 0)}</b>\n"
        f"Нажатий на карты: <b>{counts.get('card_button_clicked', 0)}</b>\n"
        f"Успешно открыто: <b>{counts.get('card_opened', 0)}</b>\n"
        f"Открыли одну карту: <b>{data.get('users_opened_one', 0)}</b>\n"
        f"Открыли две: <b>{data.get('users_opened_two_or_more', 0)}</b>\n\n"
        "🎧 <b>Голос</b>\n"
        f"Запросили: <b>{counts.get('voice_requested', 0)}</b>\n"
        f"Получили: <b>{counts.get('voice_sent', 0)}</b>\n\n"
        "💬 <b>Отклик</b>\n"
        f"Мне это близко: <b>{reactions.get('close', 0)}</b>\n"
        f"Хочу осмыслить: <b>{reactions.get('reflect', 0)}</b>\n"
        f"Сейчас не откликается: <b>{reactions.get('not_now', 0)}</b>\n\n"
        "🎯 <b>Точность разбора</b>\n"
        f"Да, точно: <b>{feedback.get('yes', 0)}</b>\n"
        f"Частично: <b>{feedback.get('partly', 0)}</b>\n"
        f"Нет, не про меня: <b>{feedback.get('no', 0)}</b>\n\n"
        f"{format_reflection_quality(data)}\n\n"
        f"🔢 <b>Открытия по позициям</b>\n{position_lines}",
        parse_mode="HTML",
    )


async def test_engagement_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Show the complete private card interaction without publishing a spread."""
    if not is_admin(update):
        return
    card_id = 1
    if context.args:
        try:
            card_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Использование: <code>/testengagement 9</code>", parse_mode="HTML")
            return
    await send_card_to_chat(
        context.bot,
        update.effective_chat.id,
        card_id,
        spread_id=0,
        position=1,
    )


def admin_persistent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🏠 Главное меню")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Аналитика", callback_data="admin-menu:analytics")],
            [InlineKeyboardButton("🔮 Создать расклад", callback_data="admin-menu:newspread")],
            [InlineKeyboardButton("🕗 Запланированные публикации", callback_data="admin-menu:scheduled")],
            [InlineKeyboardButton("🃏 Проверить карту", callback_data="admin-menu:review")],
            [InlineKeyboardButton("🧪 Проверить диалог", callback_data="admin-menu:test")],
            [InlineKeyboardButton("❓ Помощь", callback_data="admin-menu:help")],
        ]
    )


def clear_admin_input_states(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "pending_newspread_menu",
        "pending_review_menu",
        "pending_test_menu",
        "pending_schedule_spread_id",
        "pending_spread_voice_id",
        "pending_card_reflection",
        "pending_reflection_test",
        "pending_spread_question_input",
        "pending_spread_question_id",
        "pending_spread_question_topic",
        "pending_spread_question_topic_id",
    ):
        context.user_data.pop(key, None)


async def send_admin_main_menu(bot, chat_id: int):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🏠 <b>Главное меню «Карта дня»</b>\n\n"
            "Выберите нужный раздел. Все основные действия доступны по кнопкам."
        ),
        parse_mode="HTML",
        reply_markup=admin_main_menu_keyboard(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    clear_admin_input_states(context)
    await update.message.reply_text(
        "Кнопка «🏠 Главное меню» закреплена под строкой ввода.",
        reply_markup=admin_persistent_keyboard(),
    )
    await send_admin_main_menu(context.bot, update.effective_chat.id)


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or not query.data or not is_admin(update):
        return
    section = query.data.split(":", 1)[1]
    await query.answer()

    if section == "home":
        clear_admin_input_states(context)
        await query.edit_message_text(
            "🏠 <b>Главное меню «Карта дня»</b>\n\nВыберите нужный раздел.",
            parse_mode="HTML",
            reply_markup=admin_main_menu_keyboard(),
        )
        return
    if section == "analytics":
        await query.edit_message_text(
            await build_dashboard_text(context.bot, 7),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                *analytics_dashboard_keyboard(7).inline_keyboard,
                [InlineKeyboardButton("⬅️ Главное меню", callback_data="admin-menu:home")],
            ]),
        )
        return
    if section == "newspread":
        clear_admin_input_states(context)
        context.user_data["pending_newspread_menu"] = True
        text = (
            "🔮 <b>Создание расклада</b>\n\n"
            "Отправьте шесть номеров карт через пробел в нужном порядке.\n"
            "Например: <code>3 25 36 48 71 104</code>"
        )
    elif section == "review":
        clear_admin_input_states(context)
        context.user_data["pending_review_menu"] = True
        text = "🃏 <b>Проверка карты</b>\n\nОтправьте номер карты от 1 до 120."
    elif section == "test":
        clear_admin_input_states(context)
        context.user_data["pending_test_menu"] = True
        text = "🧪 <b>Проверка диалога</b>\n\nОтправьте номер карты от 1 до 120."
    elif section == "scheduled":
        records = await asyncio.to_thread(
            db.get_settings_by_prefix, SCHEDULED_SPREAD_PREFIX
        )
        active = []
        for key, raw_value in records.items():
            try:
                payload = json.loads(raw_value)
                publish_at = datetime.fromtimestamp(
                    float(payload["publish_at"]), MOSCOW_TZ
                )
                if publish_at.timestamp() > time.time():
                    active.append((publish_at, int(key.removeprefix(SCHEDULED_SPREAD_PREFIX))))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        active.sort()
        rows = "\n".join(
            f"• Расклад #{spread_id} — {publish_at:%d.%m.%Y в %H:%M}"
            for publish_at, spread_id in active
        ) or "Запланированных публикаций сейчас нет."
        text = f"🕗 <b>Запланированные публикации</b>\n\n{rows}"
    else:
        text = (
            "❓ <b>Помощь</b>\n\n"
            "Создайте расклад, проверьте предпросмотр, запишите голосовое и "
            "выберите публикацию сразу или по расписанию.\n\n"
            "Если нужно вернуться — нажмите закреплённую кнопку «🏠 Главное меню»."
        )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Главное меню", callback_data="admin-menu:home")]]
        ),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        token = context.args[0]
        parts = token.split("_")
        if len(parts) == 3 and parts[0] == "spread":
            try:
                spread_id = int(parts[1])
                position = int(parts[2])
            except ValueError:
                spread_id = None
                position = None

            if spread_id is not None and position is not None:
                spread = await asyncio.to_thread(db.get_spread, spread_id)
                if spread is None or not 1 <= position <= len(spread["card_ids"]):
                    await update.message.reply_text("Этот расклад уже недоступен.")
                    return

                allowed, message = await require_channel_subscription(
                    context.bot, update.effective_user.id
                )
                if not allowed:
                    await update.message.reply_text(message)
                    return

                claim = await asyncio.to_thread(
                    db.claim_spread_selection,
                    spread_id,
                    update.effective_user.id,
                    position,
                    MAX_CARDS_PER_SPREAD,
                )
                if not claim["allowed"]:
                    await update.message.reply_text(
                        "Вы уже открыли две карты в этом раскладе. Третью открыть нельзя."
                    )
                    return
                if not claim["is_new"]:
                    await update.message.reply_text(
                        "Эту карту вы уже открывали в этом раскладе."
                    )
                    return

                card_id = spread["card_ids"][position - 1]
                await send_card_to_chat(
                    context.bot,
                    update.effective_chat.id,
                    card_id,
                    spread_id,
                    position,
                )
                await record_analytics_event(
                    event_type="card_opened",
                    idempotency_key=f"telegram:update:{update.update_id}:card_opened",
                    spread_id=spread_id,
                    card_id=card_id,
                    card_position=position,
                    actor_hash=_actor_hash(update.effective_user.id),
                )
                return

    if is_admin(update):
        clear_admin_input_states(context)
        await update.message.reply_text(
            "🔮 Панель управления готова. Кнопка «🏠 Главное меню» "
            "закреплена под строкой ввода.",
            reply_markup=admin_persistent_keyboard(),
        )
        await send_admin_main_menu(context.bot, update.effective_chat.id)
        return
    await update.message.reply_text(
        "🔮 Добро пожаловать!\n\n"
        "Каждый день в канале появляются 6 карт.\n"
        "Подпишись на канал и выбери две карты из шести — "
        "получишь их расшифровку и озвучку.",
    )


async def verify_runtime(application: Application):
    """Log whether Telegram can use the configured chat for subscriptions."""
    try:
        me = await application.bot.get_me()
        logger.info("Card bot identity: @%s", me.username)
        await application.bot.set_my_commands(
            [
                BotCommand("menu", "Открыть главное меню"),
                BotCommand("start", "Перезапустить панель управления"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
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
    await restore_scheduled_deletions(application)
    await restore_scheduled_publications(application)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}", exc_info=context.error)


def main():
    db.init_db()
    application = Application.builder().token(BOT_TOKEN).post_init(verify_runtime).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("newspread", newspread))
    application.add_handler(CommandHandler("addcard", addcard))
    application.add_handler(CommandHandler("listcards", listcards))
    application.add_handler(CommandHandler("deletecard", deletecard))
    application.add_handler(CommandHandler("editcard", editcard))
    application.add_handler(CommandHandler("clearcards", clearcards))
    application.add_handler(CommandHandler("review", review_cards))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("testengagement", test_engagement_command))
    application.add_handler(CallbackQueryHandler(voice_callback, pattern=r"^voice:"))
    application.add_handler(
        CallbackQueryHandler(card_reaction_callback, pattern=r"^react:")
    )
    application.add_handler(
        CallbackQueryHandler(
            reflection_feedback_callback,
            pattern=r"^reflection-feedback:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(dashboard_callback, pattern=r"^admin-stats:")
    )
    application.add_handler(
        CallbackQueryHandler(admin_menu_callback, pattern=r"^admin-menu:")
    )
    application.add_handler(
        CallbackQueryHandler(
            record_spread_voice_callback,
            pattern=r"^record-spread:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            preview_position_callback,
            pattern=r"^preview-position:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            show_complete_preview_callback,
            pattern=r"^show-preview:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            schedule_spread_callback,
            pattern=r"^schedule-spread:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            spread_question_callback,
            pattern=r"^question-spread:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(publish_spread_callback, pattern=r"^(publish|cancel)-spread:")
    )
    application.add_handler(
        CallbackQueryHandler(select_card_callback, pattern=r"^(pick|card):")
    )
    application.add_handler(CallbackQueryHandler(review_callback, pattern=r"^review"))
    application.add_handler(MessageHandler(filters.PHOTO, addcard))
    application.add_handler(MessageHandler(filters.Document.IMAGE, addcard_document))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.VOICE, handle_admin_voice)
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message)
    )
    application.add_error_handler(error_handler)

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
