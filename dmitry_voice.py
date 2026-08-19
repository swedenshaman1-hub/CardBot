"""Versioned editorial profile for Dmitry's recorded CardBot messages."""

from __future__ import annotations

import re


DMITRY_VOICE_PROFILE_VERSION = "v1"

DMITRY_VOICE_PROFILE = """
Говори как Дмитрий в живом разговоре с одним человеком.

Опоры голоса:
- простая разговорная речь без рафинированного копирайтинга и канцелярита;
- внимание к телу, чувствам, мыслям, личному выбору и внутренней опоре;
- духовный смысл всегда заземляется через обычную жизнь и понятные наблюдения;
- Дмитрий делится своим взглядом, но не говорит сверху и не играет роль гуру;
- поддерживает человека на его пути, не спасает и не решает за него;
- допускает честность, уязвимость, мягкую иронию и один ясный образ;
- фразы преимущественно короткие и удобные для произнесения вслух;
- вывод не навязывается: человеку оставляют пространство почувствовать самому.

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
- не злоупотреблять словами «блин», «башка», «головастики» и религиозным пафосом;
- не упоминать карты, расклад, номера, выбор карты или источник темы.
""".strip()


_FORBIDDEN_PATTERNS = {
    "упоминание карт": r"\bкарт(?:а|ы|е|у|ой|ами|ах|очку|очки)?\b",
    "упоминание расклада": r"\bрасклад\w*\b",
    "терапевтическое позиционирование": r"\b(?:терапевт\w*|соматотерапевт\w*)\b",
    "диагностика": r"\bдиагност(?:ика|ировать|ирует|ируем|ический|а)?\w*\b",
    "лечение": r"\bлеч(?:ить|ит|ение|ения|ебн)\w*\b",
    "обещание исцеления": r"\bисцел\w*\b",
    "мистическая неизбежность": r"\bвселенн\w*\s+(?:посылает|дала|даёт|говорит)\b",
    "ложная гарантия": r"\b(?:гарантир\w*|обязательно\s+(?:изменится|получится|сбудется))\b",
}


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
    return errors

