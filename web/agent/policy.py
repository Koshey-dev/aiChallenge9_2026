"""Политики входа и выхода: что коробка пускает внутрь и что выпускает наружу.

Чистые функции без сети. Их можно проверять отдельно от модели — и нужно:
это единственная часть коробки, которая работает без запроса к провайдеру.
"""

import re

MASK = "[вырезано политикой]"

# Ключи популярных провайдеров: OpenAI-совместимые, Google, Groq.
SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,}"
                    r"|gsk_[A-Za-z0-9]{20,})")

PROBE = re.compile(
    r"систем\w+ (?:промпт|инструкц)|покажи\s+(?:свои\s+)?инструкц"
    r"|повтори\s+(?:свои\s+)?инструкц|system prompt"
    r"|игнорируй\s+(?:все\s+)?(?:предыдущие|прошлые|свои)\s+инструкц"
    r"|ignore (?:all )?previous instructions", re.I)


class Refused(Exception):
    """Вход не прошёл политику: причина уходит пользователю, модель не вызывается."""


def check_input(text, settings):
    """Проверяет запрос до вызова модели.

    Возвращает текст, который можно отправлять, и записи для журнала.
    Нарушение — исключение `Refused`, запрос до провайдера не доходит.
    """
    notes = []
    clean = (text or "").strip()

    if not clean:
        raise Refused("пустой запрос")

    limit = int(settings["input_max"])
    if len(clean) > limit:
        raise Refused(f"запрос длиннее предела: {len(clean)} символов при пределе {limit}")

    banned = [word.strip().lower() for word in str(settings["input_ban"]).split(",")
              if word.strip()]
    hit = next((word for word in banned if word in clean.lower()), None)
    if hit:
        raise Refused(f"стоп-слово «{hit}»")

    if settings["input_guard"] and PROBE.search(clean):
        raise Refused("похоже на попытку вытащить системный промпт")

    if SECRET.search(clean):
        clean = SECRET.sub(MASK, clean)
        notes.append("во входе найден ключ — вырезан до отправки в модель")

    return clean, notes


def clean_output(text, settings, role):
    """Правит готовый ответ перед выдачей. Возвращает текст и записи для журнала."""
    notes = []
    result = text

    if settings["mask_secrets"]:
        result, count = SECRET.subn(MASK, result)
        if count:
            notes.append(f"в ответе замаскировано ключей: {count}")

    if settings["hide_role"] and role:
        head = role[:60]
        if head and head in result:
            result = result.replace(role, MASK) if role in result else result.replace(head, MASK)
            notes.append("из ответа убран пересказ системного промпта")

    limit = int(settings["output_max"])
    if len(result) > limit:
        result = result[:limit].rstrip() + " […]"
        notes.append(f"ответ обрезан до {limit} символов")

    return result, notes
