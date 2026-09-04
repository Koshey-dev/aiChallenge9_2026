import asyncio
import difflib
import itertools
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from models import MAX_TOKENS, MODELS, MODEL_TASKS, RUNS_PER_MODEL, SCALES, URLS, cost_of

load_dotenv()

URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemini-3.5-flash-lite"
SERVER_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")

# Google отдаёт 400 «User location is not supported», если запрос пришёл из закрытой
# страны. На сервере запросы к модели идут через локальный SOCKS-прокси, локально
# переменной нет и всё ходит напрямую.
PROXY = os.environ.get("LLM_PROXY") or None

# Прайс Google за 1M токенов, тариф Standard, сентябрь 2026.
# Поменяли MODEL — проверьте, что она есть здесь, иначе стоимость не посчитается.
PRICES = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}

PRESETS = [
    {
        "type": "логическая",
        "task": "У Алисы четыре брата и одна сестра. Сколько сестёр у брата Алисы?",
    },
    {
        "type": "алгоритмическая",
        "task": "Как найти дубликат среди миллиарда 32-битных чисел, "
                "если в оперативную память помещается только сто тысяч?",
    },
    {
        "type": "аналитическая",
        "task": "Мобильное приложение теряет 40% пользователей на втором экране онбординга. "
                "С чего начать разбор и какие гипотезы проверить первыми?",
    },
]

STEPWISE = (
    "Решай задачу пошагово. Разбей рассуждение на пронумерованные шаги, "
    "каждый шаг — одна мысль. В конце отдельной строкой выведи итоговый вывод."
)

PROMPT_WRITER = (
    "Ты составляешь промпты для языковой модели. Тебе дают задачу — решать её не надо. "
    "Напиши промпт, который поможет другой модели решить эту задачу максимально точно: "
    "что учесть, какие шаги пройти, где обычно ошибаются, в каком виде дать ответ. "
    "Верни только текст промпта, без пояснений и без решения задачи."
)

ROLE_WRITER = (
    "Ты собираешь команду экспертов под конкретную задачу. Роли фиксированы, "
    "верни их ровно в этом порядке и с этими именами: Аналитик, Инженер, Критик.\n"
    "Для каждой роли напиши инструкцию — как именно ей подойти к этой задаче.\n"
    "Аналитик разбирает суть: что вообще спрашивают и из чего задача состоит.\n"
    "Инженер даёт практическое решение и оценивает его цену.\n"
    "Критик подходит с максимальным подозрением: ищет двусмысленности, крайние случаи "
    "и скрытые допущения, из-за которых решение развалится.\n"
    "Каждая роль работает самостоятельно и ответов остальных не видит — "
    "не пиши инструкций вида «оцени решение коллеги»."
)

ROLE_SCHEMA = {
    "type": "object",
    "properties": {
        "roles": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "instruction": {"type": "string"},
                },
                "required": ["name", "instruction"],
            },
        }
    },
    "required": ["roles"],
}

SOLVER = (
    "Ты — решала. Тебе дают задачу и три независимых решения от экспертов, "
    "которые не видели ответов друг друга. Твоя работа — дать окончательный ответ. "
    "Можешь выбрать одно из решений, можешь собрать своё. "
    "Сначала короткий разбор: в чём эксперты сходятся и где расходятся. "
    "Затем отдельным абзацем окончательный ответ."
)

TEMPERATURES = [0, 0.7, 1.2]
RUNS = 5
# Бесплатный тариф даёт 15 запросов в минуту на модель, поэтому запросы
# разводятся по времени: старт не чаще одного раза в PACE секунд.
PACE = 4.2

TASKS = {
    "factual": "В каком году человек впервые вышел в открытый космос? "
               "Ответь одним предложением.",
    "creative": "Придумай название для AI Advent Challenge #9 для продвижения "
                "на билбордах города. Одна строка, только название.",
}

TEMP_JUDGE = (
    "Тебе дают один и тот же запрос, выполненный на трёх температурах по пять раз. "
    "Скажи, чем группы отличаются между собой, где ответы разнообразнее, а где "
    "однообразнее, и какая температура уместнее для задач такого типа. "
    "Содержание ответов не пересказывай. Уложись в 5-7 предложений."
)

JUDGE = (
    "Сравни четыре ответа на одну задачу, полученные разными способами. "
    "Скажи, чем они отличаются по полноте, структуре и уверенности "
    "и какой выглядит самым надёжным. Содержание ответов не пересказывай. "
    "Уложись в 5-7 предложений."
)

app = FastAPI()
HERE = Path(__file__).parent


class RunIn(BaseModel):
    task: str
    key: str | None = None


class SummaryIn(BaseModel):
    task: str
    answers: dict[str, str]
    key: str | None = None


class TemperatureIn(BaseModel):
    task: str
    key: str | None = None


class VerdictIn(BaseModel):
    task: str
    groups: dict[str, list[str]]
    key: str | None = None


class ModelsIn(BaseModel):
    task: str
    key: str | None = None
    groq_key: str | None = None


def new_metrics():
    return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}


def finish(metrics, started, chars):
    tokens_in = metrics["prompt_tokens"]
    tokens_out = metrics["completion_tokens"]
    # Страница сравнения моделей считает стоимость сама: в одном прогоне там шесть
    # разных прайсов, и общий MODEL к ним отношения не имеет.
    cost = metrics.get("cost")
    price = PRICES.get(MODEL)
    if cost is None and price:
        cost = tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1]
    return {
        "seconds": round(time.monotonic() - started, 1),
        "chars": chars,
        "tokens": tokens_in + tokens_out,
        "requests": metrics["requests"],
        "cost": cost,
    }


def line(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"


def chat(task, system=None):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": task})
    return messages


async def call(client, key, messages, target, metrics, collect, url=URL, **knobs):
    """Один потоковый запрос. Отдаёт события браузеру, копит метрики и текст.

    `url` и `model` в `knobs` меняются только на странице сравнения моделей:
    там каждый запрос уходит своей модели, а половина — вообще другому провайдеру.
    """
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
        "stream_options": {"include_usage": True},
        **knobs,
    }
    headers = {"Authorization": f"Bearer {key}"}

    for attempt in range(3):
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code in (429, 503) and attempt < 2:
                    await response.aread()
                    pause = 2 * (attempt + 1)
                    yield {"t": "retry", "target": target,
                           "code": response.status_code, "after": pause}
                    await asyncio.sleep(pause)
                    continue

                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    yield {"t": "error", "target": target,
                           "message": f"{response.status_code}: {body}"}
                    return

                metrics["requests"] += 1
                # usage приходит в каждом чанке нарастающим итогом, а не только в последнем:
                # суммировать нельзя, берём последнее значение за запрос
                spent = {"prompt": 0, "completion": 0}

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
                        collect.append(delta)
                        yield {"t": "delta", "target": target, "text": delta}

                metrics["prompt_tokens"] += spent["prompt"]
                metrics["completion_tokens"] += spent["completion"]
                return

        except httpx.HTTPError as error:
            if attempt == 2:
                yield {"t": "error", "target": target, "message": f"сеть: {error}"}
                return
            await asyncio.sleep(2 * (attempt + 1))


async def call_json(client, key, messages, schema, metrics):
    """Непотоковый запрос со схемой ответа: результат нужен целиком до следующего шага."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.2,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "roles", "schema": schema},
        },
    }
    response = await client.post(URL, headers={"Authorization": f"Bearer {key}"}, json=payload)
    response.raise_for_status()
    data = response.json()

    metrics["requests"] += 1
    usage = data.get("usage") or {}
    metrics["prompt_tokens"] += usage.get("prompt_tokens", 0)
    metrics["completion_tokens"] += usage.get("completion_tokens", 0)
    return json.loads(data["choices"][0]["message"]["content"])


async def method_1(client, key, task, metrics, collect):
    async for event in call(client, key, chat(task), "main", metrics, collect):
        yield event


async def method_2(client, key, task, metrics, collect):
    async for event in call(client, key, chat(task, STEPWISE), "main", metrics, collect):
        yield event


async def method_3(client, key, task, metrics, collect):
    yield {"t": "stage", "text": "модель пишет промпт себе"}
    written = []
    async for event in call(client, key, chat(task, PROMPT_WRITER), "prompt", metrics, written):
        yield event

    prompt = "".join(written).strip()
    if not prompt:
        yield {"t": "error", "target": "main", "message": "промпт не сгенерировался"}
        return

    yield {"t": "stage", "text": "решает по своему промпту"}
    async for event in call(client, key, chat(task, prompt), "main", metrics, collect):
        yield event


async def drain(source, queue):
    async for event in source:
        await queue.put(event)
    await queue.put(None)


async def method_4(client, key, task, metrics, collect):
    yield {"t": "stage", "text": "подбираю экспертов"}
    try:
        roles = (await call_json(client, key, chat(task, ROLE_WRITER),
                                 ROLE_SCHEMA, metrics))["roles"]
    except Exception as error:
        yield {"t": "error", "target": "solver", "message": f"роли не собрались: {error}"}
        return

    for index, role in enumerate(roles):
        yield {"t": "role", "i": index, "name": role["name"],
               "instruction": role["instruction"]}

    yield {"t": "stage", "text": "эксперты работают параллельно"}
    answers = [[] for _ in roles]
    queue = asyncio.Queue()

    async def one(index, role):
        await asyncio.sleep(0.3 * index)  # не бьём в лимит залпом
        async for event in call(client, key, chat(task, role["instruction"]),
                                f"role{index}", metrics, answers[index]):
            yield event

    workers = [asyncio.create_task(drain(one(i, role), queue))
               for i, role in enumerate(roles)]
    left = len(roles)
    while left:
        event = await queue.get()
        if event is None:
            left -= 1
            continue
        yield event
    await asyncio.gather(*workers)

    yield {"t": "stage", "text": "решала сводит ответы"}
    digest = "\n\n".join(
        f"{role['name']}:\n{''.join(answers[index]).strip()}"
        for index, role in enumerate(roles)
    )
    async for event in call(client, key,
                            chat(f"Задача:\n{task}\n\nРешения экспертов:\n{digest}", SOLVER),
                            "solver", metrics, collect):
        yield event


METHODS = {1: method_1, 2: method_2, 3: method_3, 4: method_4}


def diversity(answers):
    """Разнообразие группы ответов: сколько разных и насколько похожи друг на друга."""
    pairs = list(itertools.combinations(answers, 2))
    similarity = 1.0
    if pairs:
        similarity = sum(
            difflib.SequenceMatcher(None, a, b).ratio() for a, b in pairs
        ) / len(pairs)
    return {"unique": len(set(answers)), "similarity": round(similarity, 2)}


# Момент последнего запроса со страницы температур. Общий, а не локальный для задачи:
# иначе на стыке двух задач отсчёт начинается заново и минутный лимит трещит.
last_paced = 0.0


async def temperature_run(client, key, task, metrics, collect):
    global last_paced

    for temperature in TEMPERATURES:
        answers = []
        for run in range(RUNS):
            wait = PACE - (time.monotonic() - last_paced)
            if wait > 0:
                yield {"t": "pause", "left": round(wait, 1)}
                await asyncio.sleep(wait)
            last_paced = time.monotonic()

            target = f"t{temperature}r{run}"
            yield {"t": "card", "temp": temperature, "run": run, "target": target}

            text = []
            broken = False
            async for event in call(client, key, chat(task), target,
                                    metrics, text, temperature=temperature):
                if event["t"] == "error":
                    broken = True
                yield event

            answer = "".join(text).strip()
            collect.extend(text)
            if answer and not broken:
                answers.append(answer)

        yield {"t": "stats", "temp": temperature, "ok": len(answers),
               "runs": RUNS, **diversity(answers)}


def average(values):
    return round(sum(values) / len(values), 2) if values else None


async def model_runs(client, keys, model, task, total, collect):
    """Прогоны одной модели подряд. Счётчики у каждого прогона свои: две шкалы
    идут параллельно, и на общем словаре разницы «до и после» перемешались бы."""
    provider = model["provider"]
    every = []
    clean = []

    for run in range(RUNS_PER_MODEL):
        target = f"{model['key']}r{run}"
        yield {"t": "card", "model": model["key"], "run": run, "target": target}

        spent = new_metrics()
        text = []
        started = time.monotonic()
        first = None
        retries = 0
        broken = False

        async for event in call(client, keys[provider], chat(task), target,
                                spent, text, url=URLS[provider],
                                model=model["id"], max_tokens=MAX_TOKENS):
            if event["t"] == "delta" and first is None:
                first = time.monotonic() - started
            elif event["t"] == "retry":
                retries += 1
            elif event["t"] == "error":
                broken = True
            yield event

        seconds = time.monotonic() - started
        tokens_in = spent["prompt_tokens"]
        tokens_out = spent["completion_tokens"]
        cost = cost_of(model, tokens_in, tokens_out) if tokens_out else None

        total["requests"] += spent["requests"]
        total["prompt_tokens"] += tokens_in
        total["completion_tokens"] += tokens_out
        total["cost"] += cost or 0.0
        collect.extend(text)

        # Скорость сквозная: токены выхода на всё время запроса. Отделить генерацию
        # от ожидания по клиенту нельзя — ответ приходит двумя-тремя крупными кусками,
        # и «время после первого токена» вырождается в доли секунды. Деление на них
        # давало 7795 токенов/с, чего не бывает.
        tps = round(tokens_out / seconds, 1) if tokens_out and seconds > 0 else None

        measured = {
            "seconds": round(seconds, 1),
            "ttft": round(first, 2) if first is not None else None,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tps": tps,
            "cost": cost,
        }
        yield {"t": "run", "model": model["key"], "run": run,
               "retries": retries, **measured}

        every.append(measured)
        # Повтор по лимиту растягивает время в разы, поэтому в средние такой прогон
        # не идёт. На стоимость повтор не влияет — она считается по всем прогонам.
        if not broken and not retries:
            clean.append(measured)

    yield {
        "t": "model_stats",
        "model": model["key"],
        "ok": len(clean),
        "runs": RUNS_PER_MODEL,
        "ttft": average([m["ttft"] for m in clean if m["ttft"] is not None]),
        "seconds": average([m["seconds"] for m in clean]),
        "tps": average([m["tps"] for m in clean if m["tps"] is not None]),
        "tokens_out": average([m["tokens_out"] for m in clean]),
        "cost": cost_of(model, sum(m["tokens_in"] for m in every),
                        sum(m["tokens_out"] for m in every)),
    }


async def models_run(client, keys, task, total, collect):
    """Две шкалы идут параллельно, внутри шкалы модели — по очереди.

    Параллельно только между провайдерами: внутри одной шкалы одновременные запросы
    делят очередь провайдера, и замер времени перестал бы что-либо значить.
    """
    total["cost"] = 0.0
    groups = {}
    for model in MODELS:
        groups.setdefault(model["scale"], []).append(model)

    async def scale(models):
        for model in models:
            async for event in model_runs(client, keys, model, task, total, collect):
                yield event

    queue = asyncio.Queue()
    workers = [asyncio.create_task(drain(scale(models), queue))
               for models in groups.values()]
    left = len(workers)
    while left:
        event = await queue.get()
        if event is None:
            left -= 1
            continue
        yield event
    await asyncio.gather(*workers)


def streamer(builder):
    async def run():
        metrics = new_metrics()
        collect = []
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=180, default_encoding="utf-8",
                                     proxy=PROXY) as client:
            async for event in builder(client, metrics, collect):
                yield line(event)
        answer = "".join(collect).strip()
        yield line({"t": "done", "answer": answer,
                    "metrics": finish(metrics, started, len(answer))})

    return StreamingResponse(run(), media_type="application/x-ndjson")


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/temperature")
def temperature_page():
    return FileResponse(HERE / "temperature.html")


@app.get("/models")
def models_page():
    return FileResponse(HERE / "models.html")


@app.get("/api/models-config")
def models_config():
    return {
        "models": MODELS,
        "scales": SCALES,
        "tasks": MODEL_TASKS,
        "runs": RUNS_PER_MODEL,
        "max_tokens": MAX_TOKENS,
        "server_key": bool(SERVER_KEY),
        "server_groq_key": bool(GROQ_KEY),
    }


@app.post("/api/models-run")
def run_models(body: ModelsIn):
    keys = {
        "gemini": (body.key or "").strip() or SERVER_KEY,
        "groq": (body.groq_key or "").strip() or GROQ_KEY,
    }
    return streamer(
        lambda client, metrics, collect: models_run(client, keys, body.task,
                                                    metrics, collect)
    )


@app.get("/api/config")
def config():
    price = PRICES.get(MODEL)
    return {
        "model": MODEL,
        "price": {"input": price[0], "output": price[1]} if price else None,
        "presets": PRESETS,
        "server_key": bool(SERVER_KEY),
        "tasks": TASKS,
        "temperatures": TEMPERATURES,
        "runs": RUNS,
    }


@app.post("/api/run/{number}")
def run(number: int, body: RunIn):
    key = (body.key or "").strip() or SERVER_KEY
    method = METHODS[number]
    return streamer(
        lambda client, metrics, collect: method(client, key, body.task, metrics, collect)
    )


@app.post("/api/summary")
def summary(body: SummaryIn):
    key = (body.key or "").strip() or SERVER_KEY
    parts = "\n\n".join(
        f"Способ {number}:\n{text.strip()}"
        for number, text in sorted(body.answers.items())
        if text.strip()
    )
    task = f"Задача:\n{body.task}\n\nОтветы:\n{parts}"

    def builder(client, metrics, collect):
        return call(client, key, chat(task, JUDGE), "main", metrics, collect)

    return streamer(builder)


@app.post("/api/temperature")
def run_temperature(body: TemperatureIn):
    key = (body.key or "").strip() or SERVER_KEY
    return streamer(
        lambda client, metrics, collect: temperature_run(client, key, body.task,
                                                         metrics, collect)
    )


@app.post("/api/verdict")
def verdict(body: VerdictIn):
    key = (body.key or "").strip() or SERVER_KEY
    groups = "\n\n".join(
        f"temperature = {temperature}:\n" + "\n".join(f"- {a}" for a in answers)
        for temperature, answers in body.groups.items()
    )
    task = f"Запрос:\n{body.task}\n\nОтветы по группам:\n{groups}"

    async def builder(client, metrics, collect):
        global last_paced
        wait = PACE - (time.monotonic() - last_paced)
        if wait > 0:
            yield {"t": "pause", "left": round(wait, 1)}
            await asyncio.sleep(wait)
        last_paced = time.monotonic()
        async for event in call(client, key, chat(task, TEMP_JUDGE), "main",
                                metrics, collect):
            yield event

    return streamer(builder)
