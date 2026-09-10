"""Транспорт коробки: потоковый запрос и запрос со схемой ответа.

Единственное место внутри агента, которое знает про HTTP. Ни ролей, ни политик
здесь нет — только запрос, разбор потока, повтор при лимите и счёт токенов.
"""

import asyncio
import json

import httpx


class AgentError(Exception):
    """Ответа не будет: провайдер вернул ошибку или сеть отвалилась."""


def new_usage():
    return {"requests": 0, "prompt": 0, "completion": 0}


def _headers(key):
    return {"Authorization": f"Bearer {key}"}


def _add(usage, prompt, completion):
    usage["requests"] += 1
    usage["prompt"] += prompt
    usage["completion"] += completion


async def stream_chat(client, *, url, key, model, messages, usage, **knobs):
    """Отдаёт ответ кусками по мере генерации, расход токенов кладёт в `usage`."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        **knobs,
    }
    # usage приходит нарастающим итогом в каждом чанке — берём последнее значение
    spent = {"prompt": 0, "completion": 0}

    for attempt in range(3):
        try:
            async with client.stream("POST", url,
                                     headers=_headers(key), json=payload) as response:
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
                    block = data.get("usage")
                    if block:
                        spent["prompt"] = block.get("prompt_tokens", 0)
                        spent["completion"] = block.get("completion_tokens", 0)

                    choices = data.get("choices")
                    if not choices:
                        continue
                    delta = choices[0]["delta"].get("content") or ""
                    if delta:
                        yield delta

                _add(usage, spent["prompt"], spent["completion"])
                return

        except httpx.HTTPError as error:
            if attempt == 2:
                raise AgentError(f"сеть: {str(error) or type(error).__name__}") from error
            await asyncio.sleep(2 * (attempt + 1))

    raise AgentError("провайдер не отвечает: лимит запросов")


async def json_chat(client, *, url, key, model, messages, schema, name, usage, **knobs):
    """Непотоковый запрос со схемой ответа: результат нужен целиком до следующего шага."""
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": schema},
        },
        **knobs,
    }
    try:
        response = await client.post(url, headers=_headers(key), json=payload)
    except httpx.HTTPError as error:
        raise AgentError(f"сеть: {str(error) or type(error).__name__}") from error

    if response.status_code != 200:
        raise AgentError(f"{response.status_code}: {response.text[:300]}")

    data = response.json()
    spent = data.get("usage") or {}
    _add(usage, spent.get("prompt_tokens", 0), spent.get("completion_tokens", 0))
    return json.loads(data["choices"][0]["message"]["content"])
