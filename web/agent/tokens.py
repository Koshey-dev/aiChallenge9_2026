"""Счёт токенов без токенизатора: оценка по символам и синтетический балласт.

Точное число знает только провайдер — оно приходит в `usage` вместе с ответом.
Но решать, влезет ли запрос в контекст, коробке нужно до отправки, поэтому здесь
живёт оценка по классам символов. Она заведомо неточная: у каждого провайдера
свой словарь. Расхождение с фактом коробка меряет сама и держит коэффициентом.
"""

import math
import re

# Сколько символов приходится на один токен. У BPE-словарей кириллица режется
# мельче латиницы, цифры — мельче всего. Числа подобраны на глаз, поправку
# на реальный словарь провайдера даёт коэффициент `scale` в коробке.
CHARS_PER_TOKEN = {"cyrillic": 2.5, "latin": 4.0, "digit": 2.0, "other": 3.0}

RUNS = {
    "cyrillic": re.compile(r"[А-Яа-яЁё]+"),
    "latin": re.compile(r"[A-Za-z]+"),
    "digit": re.compile(r"[0-9]+"),
}

# Каркас сообщения: роль, разделители, служебные метки. На длинном диалоге
# мелочь, на коротком — заметная доля запроса.
FRAME = 4

FILLER = ("Реплика-балласт: смысла не несёт, а место в контексте занимает "
          "ровно так же, как настоящая. ")


def estimate(text):
    """Сколько токенов примерно займёт текст."""
    if not text:
        return 0
    known = 0
    total = 0.0
    for kind, pattern in RUNS.items():
        chars = sum(len(run) for run in pattern.findall(text))
        known += chars
        total += chars / CHARS_PER_TOKEN[kind]
    total += (len(text) - known) / CHARS_PER_TOKEN["other"]
    return round(total)


def of(messages):
    """Оценка для списка сообщений: тексты плюс каркас каждого."""
    return sum(estimate(message["content"]) + FRAME for message in messages)


def filler(target):
    """Текст примерно на `target` токенов: балласт для опытов с переполнением."""
    if target <= 0:
        return ""
    times = max(1, math.ceil(target / estimate(FILLER)))
    return (FILLER * times).strip()
