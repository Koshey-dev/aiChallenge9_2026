"""День 30: приватный AI-сервис на локальной LLM.

Шлюз перед Ollama — отдельный процесс на 127.0.0.1:8100. Наружу его выпускает
Caddy по пути `/llm` того же домена, что и стенд, но без пароля стенда: вход
сюда — по ключу API (`Authorization: Bearer …`). Ollama слушает только
127.0.0.1, снаружи до неё не достать.

Протокол — OpenAI-совместимый: `POST /llm/v1/chat/completions` (потоком и
целиком) и `GET /llm/v1/models`, так что клиентом может быть curl, OpenAI SDK
или браузер. `GET /llm/` — страница чата для любого устройства, `GET
/llm/stats` — журнал запросов и состояние машины.

Ограничения проверяются до модели, в таком порядке:
- ключ — `401`;
- запросы в минуту на ключ — `429` с `Retry-After`;
- контекст: токены промпта плюс предел ответа больше окна — `413`. Токены
  считает токенизатор Qwen2.5 по тому же шаблону чата, что у Ollama: сама она
  длинный промпт режет молча (день 29);
- очередь: ядро одно, модель отвечает по одному, ждут не больше QUEUE — `503`.
`max_tokens` больше MAX_TOKENS урезается, а не отклоняется.

Запуск: `uvicorn gateway:app --host 127.0.0.1 --port 8100` из `web/`. Ключи —
`LLM_KEY_<ИМЯ>` в `.env`; токенизатор — `qwen2.5-tokenizer.json` рядом
(tokenizer.json модели Qwen/Qwen2.5-1.5B-Instruct с Hugging Face).
"""

import asyncio
import collections
import hmac
import json
import math
import os
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from tokenizers import Tokenizer

load_dotenv()

HERE = Path(__file__).parent
OLLAMA = os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434"
MODEL = os.environ.get("LLM_SERVICE_MODEL") or "qwen2.5:1.5b"
CONTEXT = 2048     # окно модели, num_ctx
MAX_TOKENS = 256   # предел ответа: на одном ядре это ~30 с генерации
QUEUE = 4          # сколько запросов ждёт, пока модель занята
KEEP_ALIVE = "30m"
# Ключ → запросов в минуту. stand, phone, cli — клиенты; test — для показа
# 429, load — для залпа, где упираться надо в очередь, а не в лимит ключа.
LIMITS = {"stand": 20, "phone": 20, "cli": 20, "test": 5, "load": 120}
KEYS = {os.environ[f"LLM_KEY_{name.upper()}"]: name for name in LIMITS if os.environ.get(f"LLM_KEY_{name.upper()}")}

TOKENIZER = Tokenizer.from_file(str(HERE / "qwen2.5-tokenizer.json"))
# Шаблон чата qwen2.5 в Ollama: без своего system подставляет этот.
DEFAULT_SYSTEM = "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."

app = FastAPI(title="Приватный LLM-сервис")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type"], expose_headers=["Retry-After", "X-Max-Tokens"])

STARTED = time.time()
JOURNAL = collections.deque(maxlen=200)
COUNTS = collections.Counter()
CALLS = collections.defaultdict(collections.deque)  # ключ → время принятых запросов за минуту
BUSY = asyncio.Lock()
WAITING = []  # id запросов в очереди, по порядку


# ── Кто пришёл ───────────────────────────────────────────────────────

def device(agent):
    """Устройство и программа по User-Agent — для журнала, без точной версии."""
    agent = agent or ""
    for mark, name in (("curl/", "curl"), ("OpenAI/Python", "OpenAI SDK"), ("python-httpx", "Python httpx"),
                       ("python-requests", "Python requests")):
        if mark in agent:
            return name
    system = next((n for m, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                  ("Windows", "Windows"), ("Mac OS", "macOS"), ("Linux", "Linux")) if m in agent), "")
    browser = next((n for m, n in (("Edg/", "Edge"), ("YaBrowser", "Яндекс"), ("Firefox/", "Firefox"),
                                   ("Chrome/", "Chrome"), ("Safari/", "Safari")) if m in agent), "")
    return " · ".join(x for x in (system, browser) if x) or (agent.split("/")[0][:24] or "неизвестно")


def masked(ip):
    """Адрес клиента без двух последних частей: видно, что сети разные, но не чьи."""
    if ":" in ip:
        return ":".join(ip.split(":")[:2]) + ":…"
    parts = ip.split(".")
    return ".".join(parts[:2] + ["•", "•"]) if len(parts) == 4 else ip


def client_of(request):
    # Шлюз слушает только 127.0.0.1, к нему приходит лишь Caddy — его заголовку можно верить.
    ip = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
    return {"device": device(request.headers.get("user-agent")), "ip": masked(ip)}


def note(entry, status, **more):
    entry.update(status=status, **more)
    COUNTS[status] += 1
    JOURNAL.appendleft(entry)


def key_of(request, entry):
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    name = next((n for k, n in KEYS.items() if hmac.compare_digest(k, token)), None)
    if not name:
        note(entry, 401, reason="нет ключа" if not token else "чужой ключ")
        raise HTTPException(status_code=401, detail="нужен ключ API: Authorization: Bearer <ключ>")
    entry["key"] = name
    return name


# ── Лимиты ───────────────────────────────────────────────────────────

def rate(name, entry):
    """Скользящее окно в минуту: считаются только принятые запросы."""
    calls, now = CALLS[name], time.time()
    while calls and now - calls[0] >= 60:
        calls.popleft()
    if len(calls) >= LIMITS[name]:
        wait = math.ceil(60 - (now - calls[0]))
        note(entry, 429, reason=f"{LIMITS[name]} запросов в минуту на ключ «{name}»")
        raise HTTPException(status_code=429, detail=f"лимит ключа «{name}»: {LIMITS[name]} запросов в минуту, "
                            f"повторите через {wait} с", headers={"Retry-After": str(wait)})
    return calls


def prompt_tokens(messages):
    """Токены промпта так, как его соберёт шаблон qwen2.5 в Ollama."""
    if not any(m["role"] == "system" for m in messages):
        messages = [{"role": "system", "content": DEFAULT_SYSTEM}, *messages]
    text = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages) + "<|im_start|>assistant\n"
    return len(TOKENIZER.encode(text, add_special_tokens=False).ids)


def parse(body, entry):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not all(
            isinstance(m, dict) and m.get("role") in ("system", "user", "assistant") and isinstance(m.get("content"), str)
            for m in messages):
        note(entry, 400, reason="messages — список {role, content}")
        raise HTTPException(status_code=400, detail="messages — непустой список {role: system|user|assistant, content: строка}")
    asked = body.get("max_tokens") or MAX_TOKENS
    if not isinstance(asked, int) or asked < 1:
        note(entry, 400, reason="max_tokens — целое больше нуля")
        raise HTTPException(status_code=400, detail="max_tokens — целое больше нуля")
    temperature = body.get("temperature", 0.7)
    if not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
        note(entry, 400, reason="temperature вне 0…2")
        raise HTTPException(status_code=400, detail="temperature — число от 0 до 2")
    return [{"role": m["role"], "content": m["content"]} for m in messages], asked, min(asked, MAX_TOKENS), temperature


def fits(messages, limit, entry):
    tokens = prompt_tokens(messages)
    entry["in"] = tokens
    if tokens + limit > CONTEXT:
        note(entry, 413, reason=f"{tokens} токенов промпта + {limit} на ответ > {CONTEXT}")
        raise HTTPException(status_code=413, detail=f"контекст не помещается: промпт — {tokens} токенов, на ответ — "
                            f"{limit}, окно модели — {CONTEXT}. Сократите историю или max_tokens")
    return tokens


def admit(entry):
    """Место в очереди. Занято всё — 503 сразу, а не ожидание без конца."""
    if BUSY.locked() and len(WAITING) >= QUEUE:
        note(entry, 503, reason=f"очередь полна: 1 в работе, {QUEUE} ждут")
        raise HTTPException(status_code=503, detail=f"сервис занят: 1 запрос в работе, {QUEUE} в очереди. Повторите позже",
                            headers={"Retry-After": "10"})
    WAITING.append(entry["id"])


# ── Модель ───────────────────────────────────────────────────────────

async def generate(messages, limit, temperature):
    """Поток Ollama: куски текста, в конце — её счётчики."""
    body = {"model": MODEL, "messages": messages, "stream": True, "keep_alive": KEEP_ALIVE,
            "options": {"num_ctx": CONTEXT, "num_predict": limit, "temperature": temperature}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
        async with client.stream("POST", f"{OLLAMA}/api/chat", json=body) as r:
            r.raise_for_status()
            async for raw in r.aiter_lines():
                if not raw.strip():
                    continue
                chunk = json.loads(raw)
                if "error" in chunk:
                    raise RuntimeError(chunk["error"])
                yield chunk


def chunk(cid, created, delta=None, finish=None, usage=None):
    out = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL,
           "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        out["usage"] = usage
    return f"data: {json.dumps(out, ensure_ascii=False)}\n\n"


async def answer(request, entry, messages, limit, temperature, tokens):
    """Ждать очередь, потом отвечать. События: queue (место в очереди),
    delta (кусок текста), done (счётчики). Слот освобождается и при обрыве."""
    start = time.perf_counter()
    try:
        while True:
            if not BUSY.locked() and WAITING and WAITING[0] == entry["id"]:
                await BUSY.acquire()
                break
            yield {"t": "queue", "position": WAITING.index(entry["id"]) + 1}
            await asyncio.sleep(0.25)
    except (asyncio.CancelledError, GeneratorExit):
        note(entry, 499, queue=round(time.perf_counter() - start, 2), reason="клиент ушёл из очереди")
        raise
    finally:
        WAITING.remove(entry["id"])
    queued, first, parts, last = time.perf_counter() - start, None, [], {}
    status = 499
    try:
        async for c in generate(messages, limit, temperature):
            text = c.get("message", {}).get("content", "")
            if text:
                first = first or time.perf_counter()
                parts.append(text)
                yield {"t": "delta", "text": text}
            if c.get("done"):
                last = c
            if await request.is_disconnected():
                return
        status = 200
        out = last.get("eval_count") or 0
        usage = {"prompt_tokens": tokens, "completion_tokens": out, "total_tokens": tokens + out,
                 "prompt_tokens_read": last.get("prompt_eval_count"), "queue_seconds": round(queued, 2),
                 "ttft_seconds": round(first - start, 2) if first else None,
                 "seconds": round(time.perf_counter() - start, 2),
                 "tokens_per_second": round(out / (last["eval_duration"] / 1e9), 1) if last.get("eval_duration") else None,
                 "load_seconds": round(last.get("load_duration", 0) / 1e9, 2)}
        yield {"t": "done", "text": "".join(parts), "finish": "length" if out >= limit else "stop", "usage": usage}
    except (httpx.HTTPError, RuntimeError) as e:
        status = 502
        yield {"t": "error", "message": f"модель не ответила: {e}"}
    finally:
        BUSY.release()
        elapsed = time.perf_counter() - start
        note(entry, status, queue=round(queued, 2), ttft=round(first - start, 2) if first else None,
             seconds=round(elapsed, 2), out=last.get("eval_count"), read=last.get("prompt_eval_count"),
             reason="клиент ушёл" if status == 499 else entry.get("reason"))


# ── Маршруты ─────────────────────────────────────────────────────────

@app.post("/llm/v1/chat/completions")
async def completions(request: Request):
    entry = {"id": uuid.uuid4().hex[:8], "at": time.strftime("%H:%M:%S"), **client_of(request), "key": None}
    name = key_of(request, entry)
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except (ValueError, AssertionError):
        note(entry, 400, reason="тело — не JSON")
        raise HTTPException(status_code=400, detail="тело запроса — JSON")
    messages, asked, limit, temperature = parse(body, entry)
    calls = rate(name, entry)
    tokens = fits(messages, limit, entry)
    admit(entry)
    calls.append(time.time())
    entry.update(limit=limit, asked=asked, stream=bool(body.get("stream")))
    headers = {"X-Max-Tokens": str(limit)}
    events = answer(request, entry, messages, limit, temperature, tokens)
    cid, created = "chatcmpl-" + entry["id"], int(time.time())

    if body.get("stream"):
        async def sse():
            async for e in events:
                if e["t"] == "queue":
                    yield f": queue {e['position']}\n\n"
                elif e["t"] == "delta":
                    yield chunk(cid, created, {"content": e["text"]})
                elif e["t"] == "done":
                    yield chunk(cid, created, finish=e["finish"])
                    yield chunk(cid, created, usage=e["usage"])
                    yield "data: [DONE]\n\n"
                elif e["t"] == "error":
                    yield f"data: {json.dumps({'error': {'message': e['message']}}, ensure_ascii=False)}\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream", headers=headers)

    try:
        async for e in events:
            if e["t"] == "error":
                return JSONResponse({"error": {"message": e["message"]}}, status_code=502, headers=headers)
            if e["t"] == "done":
                return JSONResponse({"id": cid, "object": "chat.completion", "created": created, "model": MODEL,
                                     "choices": [{"index": 0, "message": {"role": "assistant", "content": e["text"]},
                                                  "finish_reason": e["finish"]}], "usage": e["usage"]}, headers=headers)
        return JSONResponse({"error": {"message": "клиент ушёл"}}, status_code=499)
    finally:
        await events.aclose()  # слот модели — свободен сразу, а не когда соберут мусор


@app.get("/llm/v1/models")
def models(request: Request):
    entry = {"at": time.strftime("%H:%M:%S"), **client_of(request)}
    key_of(request, entry)
    note(entry, 200, reason="список моделей")
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "created": int(STARTED), "owned_by": "local"}]}


def memory():
    """Память машины по /proc/meminfo, МБ. Не Linux — None."""
    try:
        info = dict(line.split(":") for line in Path("/proc/meminfo").read_text().splitlines())
    except OSError:
        return None
    mb = lambda name: int(info[name].split()[0]) // 1024
    return {"total": mb("MemTotal"), "available": mb("MemAvailable"), "swap_used": mb("SwapTotal") - mb("SwapFree")}


@app.get("/llm/stats")
async def stats(request: Request):
    key_of(request, {"at": time.strftime("%H:%M:%S"), **client_of(request)})
    loaded = None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            loaded = next(({"size": m["size"], "context": m.get("context_length"), "until": m.get("expires_at")}
                           for m in (await client.get(f"{OLLAMA}/api/ps")).json()["models"] if m["name"] == MODEL), None)
            alive = True
    except httpx.HTTPError:
        alive = False
    load = None
    try:
        load = float(Path("/proc/loadavg").read_text().split()[0])
    except OSError:
        pass
    return {"model": MODEL, "context": CONTEXT, "max_tokens": MAX_TOKENS, "queue_max": QUEUE, "limits": LIMITS,
            "ollama": alive, "loaded": loaded, "busy": BUSY.locked(), "waiting": len(WAITING),
            "uptime": round(time.time() - STARTED), "counts": dict(COUNTS), "memory": memory(), "load": load,
            "cpus": os.cpu_count(), "journal": list(JOURNAL)[:60]}


@app.get("/llm/health")
def health():
    return {"status": "ok", "model": MODEL}


@app.get("/llm/")
def chat_page():
    return FileResponse(HERE / "service.html")


@app.get("/llm")
def chat_page_bare():
    return FileResponse(HERE / "service.html")
