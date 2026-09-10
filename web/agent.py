"""Агент: отдельная сущность со своей ролью, памятью и счётчиками.

Наружу торчит только `ask`. Как собирается запрос, как разбирается поток
и что делать с лимитом провайдера — дело агента, интерфейс этого не знает.
"""

import asyncio
import json
import time

import httpx

ROLE = (
    "Ты — ассистент учебного стенда AI Challenge. Отвечай по делу: без вступлений, "
    "без пересказа вопроса, два-три абзаца максимум. "
    "Если вопрос читается по-разному — назови прочтения и спроси, какое имелось в виду. "
    "Если чего-то не знаешь — скажи прямо, не придумывай."
)

# Сколько последних реплик уходит в следующий запрос. Модель помнит только это:
# всё, что вышло за окно, для неё не существует.
MEMORY = 20


class AgentError(Exception):
    """Ответа не будет: провайдер вернул ошибку или сеть отвалилась."""


class Agent:
    """Собеседник с ролью и памятью диалога.

    Живёт между запросами: браузер присылает только новую реплику, историю
    и счётчики агент держит у себя.
    """

    def __init__(self, key, *, url, model, role=ROLE, temperature=0.3, price=None):
        self.key = key
        self.url = url
        self.model = model
        self.role = role
        self.temperature = temperature
        self.price = price
        self.history = []
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost = 0.0
        self.seconds = 0.0

    def forget(self):
        self.history.clear()

    def payload(self, question):
        messages = [
            {"role": "system", "content": self.role},
            *self.history[-MEMORY:],
            {"role": "user", "content": question},
        ]
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    async def ask(self, client, question):
        """Спрашивает модель и отдаёт ответ кусками по мере генерации.

        История пополняется только после успешного ответа: оборванный запрос
        не должен оставлять в памяти вопрос без ответа.
        """
        started = time.monotonic()
        headers = {"Authorization": f"Bearer {self.key}"}
        payload = self.payload(question)
        answer = []
        # usage приходит нарастающим итогом в каждом чанке — берём последнее значение
        spent = {"prompt": 0, "completion": 0}

        for attempt in range(3):
            try:
                async with client.stream("POST", self.url,
                                         headers=headers, json=payload) as response:
                    if response.status_code in (429, 503) and attempt < 2:
                        await response.aread()
                        await asyncio.sleep(2 * (attempt + 1))
                        continue

                    if response.status_code != 200:
                        body = (await response.aread()).decode("utf-8", "replace")[:300]
                        raise AgentError(f"{response.status_code}: {body}")

                    async for raw in response.aiter_lines():
                        if not raw.startswith("data: "):
                            continue
                        chunk = raw.removeprefix("data: ")
                        if chunk == "[DONE]":
                            break

                        data = json.loads(chunk)
                        usage = data.get("usage")
                        if usage:
                            spent["prompt"] = usage.get("prompt_tokens", 0)
                            spent["completion"] = usage.get("completion_tokens", 0)

                        choices = data.get("choices")
                        if not choices:
                            continue
                        delta = choices[0]["delta"].get("content") or ""
                        if delta:
                            answer.append(delta)
                            yield delta
                    break

            except httpx.HTTPError as error:
                if attempt == 2:
                    raise AgentError(f"сеть: {error}") from error
                await asyncio.sleep(2 * (attempt + 1))
        else:
            raise AgentError("провайдер не отвечает: лимит запросов")

        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": "".join(answer).strip()})
        self.requests += 1
        self.tokens_in += spent["prompt"]
        self.tokens_out += spent["completion"]
        if self.price:
            self.cost += (spent["prompt"] / 1e6 * self.price[0]
                          + spent["completion"] / 1e6 * self.price[1])
        self.seconds = round(time.monotonic() - started, 1)

    def report(self):
        """Счётчики за весь диалог, время — за последнюю реплику."""
        return {
            "seconds": self.seconds,
            "turns": len(self.history) // 2,
            "requests": self.requests,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost": round(self.cost, 6) if self.price else None,
        }
