"""Бригада: разбор запроса на независимые части и сведение результатов.

Сами агенты поднимаются в `core` — здесь только планировщик, признак «похоже на
исследование» и промпт сводки.
"""

import re

from .llm import json_chat

PLANNER = (
    "Ты планировщик. Тебе дают запрос пользователя. Реши, хватит ли одного агента "
    "или задачу стоит разложить на независимые части и раздать нескольким. "
    "Разбивай только тогда, когда пользователь просит несколько направлений, "
    "сравнение или прямо просит нескольких агентов. "
    "Для каждой части дай короткое название и инструкцию агенту — что именно ему "
    "делать. Части не пересекаются и не ссылаются друг на друга: агенты работают "
    "одновременно и ответов друг друга не видят. "
    "Если хватает одного агента — верни mode=solo и пустой список."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["solo", "crew"]},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["title", "prompt"],
            },
        },
    },
    "required": ["mode", "tasks"],
}

SUMMARY = (
    "Тебе дают исходный запрос и результаты нескольких агентов, которые работали "
    "параллельно и не видели ответов друг друга. Сведи их в один ответ: сначала "
    "сводка по каждому направлению, в конце отдельным абзацем общий вывод. "
    "Не пересказывай инструкции агентов и не пиши, что ты что-то сводишь."
)

# Планировщик — лишний запрос, поэтому его зовут не на каждую реплику, а только
# когда запрос вообще похож на исследование.
HINTS = re.compile(r"агент\w*|исследован\w+|сравн\w+|параллельн\w+|сводк\w+"
                   r"|направлени\w+|разными\s+\w+ами", re.I)


def looks_like_crew(text):
    return bool(HINTS.search(text))


async def plan(client, *, url, key, model, question, limit, usage):
    """Решает, звать ли бригаду, и режет задачу на части. Список обрезан до `limit`."""
    messages = [
        {"role": "system", "content": f"{PLANNER}\nБольше {limit} частей не предлагай."},
        {"role": "user", "content": question},
    ]
    answer = await json_chat(client, url=url, key=key, model=model, messages=messages,
                             schema=SCHEMA, name="plan", usage=usage, temperature=0)
    tasks = answer.get("tasks") or []
    if answer.get("mode") != "crew" or not tasks:
        return []
    return tasks[:limit]


def digest(question, results):
    """Материал для сводки: исходный запрос и что нашёл каждый агент."""
    parts = "\n\n".join(f"{title}:\n{text}" for title, text in results if text)
    return f"Запрос:\n{question}\n\nРезультаты агентов:\n{parts}"
