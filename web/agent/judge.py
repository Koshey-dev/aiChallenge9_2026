"""Судья: отдельный вызов модели, который оценивает уже готовый ответ."""

from .llm import json_chat

ROLE = (
    "Ты проверяешь ответ другого агента. Тебе дают вопрос и ответ. "
    "Скажи, отвечает ли ответ на вопрос, есть ли в нём выдумка или непроверяемое "
    "утверждение и что стоит поправить. Никаких похвал, одно-два предложения."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "answered": {"type": "boolean"},
        "risk": {"type": "string", "enum": ["нет", "низкий", "высокий"]},
        "comment": {"type": "string"},
    },
    "required": ["answered", "risk", "comment"],
}


async def review(client, *, url, key, model, question, answer, usage, **knobs):
    """Вердикт по схеме: ответил ли, есть ли риск выдумки, что поправить."""
    messages = [
        {"role": "system", "content": ROLE},
        {"role": "user", "content": f"Вопрос:\n{question}\n\nОтвет:\n{answer}"},
    ]
    return await json_chat(client, url=url, key=key, model=model, messages=messages,
                           schema=SCHEMA, name="verdict", usage=usage, temperature=0,
                           **knobs)
