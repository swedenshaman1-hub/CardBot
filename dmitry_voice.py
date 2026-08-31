"""Versioned editorial profile for Dmitry's recorded CardBot messages."""

from __future__ import annotations

import re
from difflib import SequenceMatcher


DMITRY_VOICE_PROFILE_VERSION = "v3"

DMITRY_VOICE_PROFILE = """
Говори как Дмитрий в живом разговоре с одним человеком.

Опоры голоса:
- простая разговорная речь без рафинированного копирайтинга и канцелярита;
- одна запись раскрывает только одну ясную мысль;
- начинать с узнаваемой жизненной ситуации или честного наблюдения, чтобы человек
  мог подумать: «это про меня»;
- не украшать простую мысль сложными словами: глубина должна появляться из
  точности наблюдения, а не из абстракций;
- внимание к телу, чувствам, мыслям, личному выбору и внутренней опоре;
- духовный смысл всегда заземляется через обычную жизнь и понятные наблюдения;
- Дмитрий делится своим взглядом, но не говорит сверху и не играет роль гуру;
- поддерживает человека на его пути, не спасает и не решает за него;
- допускает честность, уязвимость, мягкую иронию и один ясный образ;
- фразы преимущественно короткие и удобные для произнесения вслух;
- вывод не навязывается: человеку оставляют пространство почувствовать самому.

Внутренняя конструкция текста:
1. Простая ситуация или наблюдение, которое быстро включает человека.
2. Привычное объяснение, которое часто оказывается неполным.
3. Один новый, но понятный угол зрения.
4. Небольшая польза: что сегодня можно заметить, почувствовать или проверить.
5. Спокойный финал или один естественный вопрос.

Это не рекламный ролик. Не добавляй призывы подписаться, купить, сохранить,
поставить реакцию или написать комментарий. Не перечисляй пункты конструкции в
готовом тексте и не делай каждую фразу коротким лозунгом.

Характерные приёмы, которые можно использовать только уместно и не повторять
механически: противопоставить контроль головы живому ощущению; показать смысл
через бытовой пример; сказать прямо и немного непричёсанно; завершить спокойным
наблюдением вместо лозунга.

Жёсткие границы:
- не называть Дмитрия терапевтом, диагностом, врачом или целителем;
- не обещать лечение, диагностику, исцеление, гарантированный результат;
- не выдумывать научные доказательства, биографические факты и духовные регалии;
- не нагнетать боль, не ставить диагнозы и не приписывать человеку скрытые причины;
- не использовать эзотерический туман, знаки Вселенной и мистическую неизбежность;
- не использовать без конкретного смысла слова «глубина», «пространство»,
  «истинный», «трансформация», «ресурс», «путь» и «проявленность»;
- не начинать словами «Привет, замечал», «Привет, знаешь» или «Иногда мы»;
- не строить финал по шаблону «Что сегодня ты выбираешь»;
- не заканчивать фразами «Позволь этому дню» и «Прислушайся к себе»;
- не злоупотреблять словами «блин», «башка», «головастики» и религиозным пафосом;
- не упоминать карты, расклад, номера, выбор карты или источник темы.
""".strip()


_FORBIDDEN_PATTERNS = {
    "упоминание карт": r"\bкарт(?:а|ы|е|у|ой|ами|ах|очку|очки)?\b",
    "упоминание расклада": r"\bрасклад\w*\b",
    "терапевтическое позиционирование": r"\b(?:терапевт\w*|соматотерапевт\w*)\b",
    "медицинское позиционирование": r"\b(?:врач\w*|целител\w*)\b",
    "диагностика": r"\bдиагност(?:ика|ировать|ирует|ируем|ический|а)?\w*\b",
    "лечение": r"\bлеч(?:ить|ит|ение|ения|ебн)\w*\b",
    "обещание исцеления": r"\bисцел\w*\b",
    "мистическая неизбежность": r"\bвселенн\w*\s+(?:посылает|дала|даёт|говорит)\b",
    "приписывание скрытой причины": (
        r"\b(?:(?:твоя|ваша)\s+(?:травм\w*|блок\w*|родов\w+\s+программ\w*)|"
        r"у\s+(?:тебя|вас)\s+(?:травм\w*|блок\w*|родов\w+\s+программ\w*))\b"
    ),
    "выдуманное научное подтверждение": (
        r"\b(?:научно\s+доказано|уч[её]ные\s+(?:доказали|установили)|"
        r"исследовани\w*\s+(?:доказал\w*|подтвержда\w*))\b"
    ),
    "ложная гарантия": r"\b(?:гарантир\w*|обязательно\s+(?:изменится|получится|сбудется))\b",
}

_REPETITIVE_PATTERNS = {
    "шаблонное начало": r"\b(?:привет[,!]?[ ]*)?(?:замечал|знаешь)[,!]?[ ]+как\b|\bиногда\s+мы\b",
    "шаблонный выбор": r"\bчто\s+сегодня\s+ты\s+выбираешь\b",
    "шаблонный финал": r"\b(?:позволь\s+этому\s+дню|прислушайся\s+к\s+себе)\b",
    "шаблонная глубина": r"\bистинн(?:ая|ую)\s+(?:сила|свобода|глубина)\b",
}

# These violations must never reach the administrator as a suggested script.
# Layout and length issues can be reviewed manually, but unsafe positioning or
# mystical promises must keep blocking the result.
VOICE_SCRIPT_BLOCKING_ERRORS = frozenset(_FORBIDDEN_PATTERNS)

VOICE_SCRIPT_RECOVERABLE_ERRORS = frozenset(
    {
        "нужно 2–4 абзаца",
        "первая строка должна быть хуком из 3–10 слов",
        "хук должен быть короткой строкой без конечного знака",
        "допустим не более одного вопроса",
    }
)


def normalize_voice_script(text: str) -> str:
    """Remove model formatting while preserving natural paragraph breaks."""
    cleaned = (text or "").strip().strip('"“”')
    cleaned = cleaned.replace("*", "").replace("`", "").replace("_", "")
    lines = [line.strip() for line in cleaned.splitlines()]
    compact: list[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    return "\n".join(compact).strip()


def voice_script_word_count(text: str) -> int:
    return len(re.findall(r"[0-9A-Za-zА-Яа-яЁё]+(?:[-–—][0-9A-Za-zА-Яа-яЁё]+)*", text))


def validate_voice_script(text: str) -> list[str]:
    """Return deterministic editorial violations; an empty list means valid."""
    errors: list[str] = []
    normalized = normalize_voice_script(text)
    word_count = voice_script_word_count(normalized)
    if not 65 <= word_count <= 105:
        errors.append(f"объём {word_count} слов, допустимо 65–105")

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    if not 2 <= len(paragraphs) <= 4:
        errors.append("нужно 2–4 абзаца")

    first_line = normalized.splitlines()[0] if normalized else ""
    if not 3 <= voice_script_word_count(first_line) <= 10:
        errors.append("первая строка должна быть хуком из 3–10 слов")
    if first_line.endswith((".", "?", "!", ":", ";")):
        errors.append("хук должен быть короткой строкой без конечного знака")

    if normalized.count("?") > 1:
        errors.append("допустим не более одного вопроса")

    lowered = normalized.lower()
    for label, pattern in _FORBIDDEN_PATTERNS.items():
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            errors.append(label)
    for label, pattern in _REPETITIVE_PATTERNS.items():
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            errors.append(label)
    return errors


def _comparison_text(text: str) -> str:
    return " ".join(
        re.findall(r"[0-9a-zа-яё]+", normalize_voice_script(text).lower())
    )


def validate_voice_script_novelty(text: str, recent_scripts: list[str]) -> list[str]:
    """Reject literal and almost literal repeats; semantic review is done by Gemini."""
    candidate = _comparison_text(text)
    if not candidate:
        return ["пустой текст для проверки новизны"]

    for recent in recent_scripts:
        previous = _comparison_text(recent)
        if not previous:
            continue
        if candidate == previous:
            return ["дословно повторяет недавний сценарий"]

        sequence_ratio = SequenceMatcher(None, candidate, previous).ratio()
        if sequence_ratio >= 0.90:
            return ["слишком похож на недавний сценарий"]
    return []
