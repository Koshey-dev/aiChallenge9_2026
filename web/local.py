"""День 26: локальная LLM — Ollama на этой же машине.

Модель `LOCAL_MODEL` (по умолчанию `qwen2.5:3b`) крутит Ollama на `OLLAMA_URL` —
тот же сервер, что считает эмбеддинги индекса недели 5. Модуль спрашивает у
него состояние (версия, модель, сколько её в видеопамяти), будит и выгружает
модель и задаёт вопрос тремя путями:

- `cli` — `ollama run <модель> --verbose --nowordwrap "<вопрос>"` подпроцессом:
  ответ из stdout, счётчики — из сводки, которую `--verbose` пишет в stderr
  (без `--nowordwrap` CLI переносит слова escape-кодами и без терминала);
- `api` — родной REST Ollama, `POST /api/chat` потоком;
- `openai` — OpenAI-совместимый `POST /v1/chat/completions`, тот же протокол,
  по которому стенд ходит к DeepSeek, Gemini и Groq.

Лестница `LADDER` — четыре вопроса растущей сложности, ответ проверяет код.
Последний прогон дверей и лестницы лежит в `local.json` рядом с модулем и
едет в git: на VPS этой модели нет, и вкладка там показывает снимок с ПК.

День 27 — приложение на той же модели: чат-ассистент с историей (таблица
`local_chats` в agent.db), ответ потоком через `/v1`. Все запросы приложения
идут через свой клиент, транспорт которого пускает только на эту машину и
пишет каждый запрос в журнал сети. Последний чат тоже едет в `local.json`.

День 28 — RAG целиком на этой машине: индекс недели 5 (эмбеддинги в той же
Ollama), ответ — этой моделью. Здесь сверка ответа и итог прогона «локально
против облака»; запросы к моделям делает app.py, локальная ветка — через
клиентов-охранников. Последний прогон едет в `local.json`.

Запуск из консоли: `py web/local.py` — прогнать лестницу и напечатать итог.
"""

import codecs
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

import rag
import store

load_dotenv()
HERE = Path(__file__).parent
SNAPSHOT = HERE / "local.json"
OLLAMA = os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434"
MODEL = os.environ.get("LOCAL_MODEL") or "qwen2.5:3b"
# Температура 0 — чтобы повторный прогон давал тот же ответ. У CLI такой ручки
# нет: `ollama run` берёт параметры модели по умолчанию.
OPTIONS = {"temperature": 0}
PROMPT = "Кто ты? Ответь одним предложением."
DOORS = ("cli", "api", "openai")

LADDER = [
    {"level": "простой", "what": "один факт, одно действие",
     "q": "Сколько будет 17 × 23?", "check": "has", "expect": "391"},
    {"level": "средний", "what": "знание и строгий формат ответа",
     "q": "Назови три планеты Солнечной системы, ближайшие к Солнцу, по порядку. "
          "Ответь только JSON-массивом строк на русском, без пояснений.",
     "check": "json", "expect": ["Меркурий", "Венера", "Земля"]},
    {"level": "сложный", "what": "четыре шага арифметики подряд",
     "q": "В корзине 12 яблок. Треть яблок отдали соседу, потом съели половину "
          "оставшихся, потом купили ещё 5. Сколько яблок в корзине? Реши по шагам, "
          "а последней строкой напиши «Ответ: <число>».",
     "check": "final", "expect": "9"},
    {"level": "с подвохом", "what": "логика: сама Алиса — тоже сестра своим братьям",
     "q": "У Алисы 3 брата и 2 сестры. Сколько сестёр у брата Алисы? "
          "Последней строкой напиши «Ответ: <число>».",
     "check": "final", "expect": "3"},
]


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── Состояние ────────────────────────────────────────────────────────

def gpu():
    """Видеокарта по nvidia-smi: имя и память, байты. Нет карты — None."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        row = subprocess.run([exe, "--query-gpu=name,memory.total,memory.used",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=5).stdout.splitlines()[0]
        name, total, used = (part.strip() for part in row.split(","))
        return {"name": name, "total": int(total) << 20, "used": int(used) << 20}
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def seconds_left(stamp):
    """Сколько секунд модель ещё держится в памяти. Ollama пишет время с
    семью знаками дробной части — fromisoformat столько не берёт."""
    stamp = re.sub(r"(\.\d{6})\d+", r"\1", stamp)
    return max(0, round(datetime.fromisoformat(stamp).timestamp() - time.time()))


def status():
    out = {"url": OLLAMA, "model": MODEL, "live": False, "at": now()}
    try:
        with httpx.Client(base_url=OLLAMA, timeout=5) as client:
            out["version"] = client.get("/api/version").json()["version"]
            tags = {m["name"]: m for m in client.get("/api/tags").json()["models"]}
            if MODEL in tags:
                show = client.post("/api/show", json={"model": MODEL}).json()
                info = show.get("model_info", {})
                arch = info.get("general.architecture", "")
                out.update(live=True, disk=tags[MODEL]["size"], details=show["details"],
                           context=info.get(f"{arch}.context_length"))
            loaded = next((m for m in client.get("/api/ps").json()["models"]
                           if m["name"] == MODEL), None)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        out["error"] = f"Ollama не ответила: {e}"
        return out
    if loaded:
        out["loaded"] = {"size": loaded["size"], "vram": loaded["size_vram"],
                         "context": loaded.get("context_length"),
                         "left": seconds_left(loaded["expires_at"])}
    out["gpu"] = gpu()
    return out


def load():
    """Поднять модель в память без вопроса: пустой запрос к /api/generate."""
    start = time.perf_counter()
    r = httpx.post(f"{OLLAMA}/api/generate", json={"model": MODEL}, timeout=300)
    r.raise_for_status()
    return {"seconds": round(time.perf_counter() - start, 2), "status": status()}


def unload():
    r = httpx.post(f"{OLLAMA}/api/generate", json={"model": MODEL, "keep_alive": 0}, timeout=60)
    r.raise_for_status()
    return {"status": status()}


# ── Три двери ────────────────────────────────────────────────────────
# Каждая отдаёт события: start (точная команда), delta (кусок ответа), done
# (ответ, метрики, сырой ответ) или error. Метрики одной формы: загрузка
# модели, время до первого токена, всё время, токены на входе и выходе,
# скорость генерации. Чего путь не сообщает, то None.

def curl(path, body, extra=""):
    data = json.dumps(body, ensure_ascii=False).replace("'", "'\\''")
    return f"curl {OLLAMA}{path}{extra} -d '{data}'"


def metrics(start, first, **known):
    end = time.perf_counter()
    return {"load": None, "in": None, "out": None, "tps": None,
            "ttft": round(first - start, 2) if first else None,
            "seconds": round(end - start, 2), **known}


def via_api(prompt):
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "options": OPTIONS}
    yield {"t": "start", "door": "api", "command": curl("/api/chat", body)}
    start, first, parts, lines, last = time.perf_counter(), None, [], [], {}
    with httpx.stream("POST", f"{OLLAMA}/api/chat", json=body, timeout=300) as r:
        r.raise_for_status()
        for raw in r.iter_lines():
            if not raw.strip():
                continue
            lines.append(raw)
            chunk = json.loads(raw)
            if "error" in chunk:
                raise RuntimeError(chunk["error"])
            text = chunk.get("message", {}).get("content", "")
            if text:
                first = first or time.perf_counter()
                parts.append(text)
                yield {"t": "delta", "text": text}
            if chunk.get("done"):
                last = chunk
    tps = last["eval_count"] / (last["eval_duration"] / 1e9) if last.get("eval_duration") else None
    yield {"t": "done", "answer": "".join(parts),
           "raw": [lines[0], f"… ещё {len(lines) - 2} строк потока …", lines[-1]] if len(lines) > 2 else lines,
           "metrics": metrics(start, first, load=round(last.get("load_duration", 0) / 1e9, 2),
                              out=last.get("eval_count"), tps=tps and round(tps, 1),
                              **{"in": last.get("prompt_eval_count")})}


def via_openai(prompt):
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
            "temperature": OPTIONS["temperature"], "stream": True,
            "stream_options": {"include_usage": True}}
    yield {"t": "start", "door": "openai",
           "command": curl("/v1/chat/completions", body, ' -H "Content-Type: application/json"')}
    start, first, end, parts, lines, usage = time.perf_counter(), None, None, [], [], {}
    with httpx.stream("POST", f"{OLLAMA}/v1/chat/completions", json=body, timeout=300) as r:
        r.raise_for_status()
        for raw in r.iter_lines():
            if not raw.startswith("data: "):
                continue
            lines.append(raw)
            if raw == "data: [DONE]":
                break
            chunk = json.loads(raw[6:])
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices", []):
                text = choice.get("delta", {}).get("content") or ""
                if text:
                    first = first or time.perf_counter()
                    end = time.perf_counter()
                    parts.append(text)
                    yield {"t": "delta", "text": text}
    out = usage.get("completion_tokens")
    # Длительностей этот протокол не сообщает: скорость — по часам стенда,
    # от первого куска ответа до последнего.
    tps = (out - 1) / (end - first) if out and first and end > first else None
    yield {"t": "done", "answer": "".join(parts),
           "raw": [lines[0], f"… ещё {len(lines) - 3} строк потока …", *lines[-2:]] if len(lines) > 3 else lines,
           "metrics": metrics(start, first, out=out, tps=tps and round(tps, 1),
                              **{"in": usage.get("prompt_tokens")})}


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[⠀-⣿]")
GO_PART = re.compile(r"([\d.]+)(h|ms|µs|us|ns|m|s)")
GO_UNIT = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "µs": 1e-6, "us": 1e-6, "ns": 1e-9}


def go_seconds(text):
    """Длительность в записи Go: 3.29s, 78.8ms, 1m2.5s."""
    return sum(float(n) * GO_UNIT[u] for n, u in GO_PART.findall(text))


def via_cli(prompt):
    exe = shutil.which("ollama")
    if not exe:
        raise RuntimeError("программы ollama нет в PATH")
    args = [exe, "run", MODEL, "--verbose", "--nowordwrap", prompt]
    yield {"t": "start", "door": "cli",
           "command": f'ollama run {MODEL} --verbose --nowordwrap "{prompt}"'}
    # stdin закрыт: без терминала `ollama run` дочитывает вопрос из stdin и ждёт.
    env = {**os.environ, "OLLAMA_HOST": OLLAMA}
    start, first, parts, errors = time.perf_counter(), None, [], []
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    # stderr читает отдельный поток: пока модель грузится, туда льётся спиннер,
    # и полная труба остановила бы процесс.
    reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
    reader.start()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while chunk := proc.stdout.read1(4096):
            text = decoder.decode(chunk)
            if text:
                first = first or time.perf_counter()
                parts.append(text)
                yield {"t": "delta", "text": text}
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    reader.join(timeout=5)
    log = ANSI.sub("", b"".join(errors).decode("utf-8", "replace"))
    stats = dict(re.findall(r"^([a-z ]+):\s+(.+)$", log, re.M))
    if proc.returncode:
        raise RuntimeError(log.strip() or f"ollama вышла с кодом {proc.returncode}")
    count = lambda key: int(stats[key].split()[0]) if key in stats else None
    rate = stats.get("eval rate", "").split()
    yield {"t": "done", "answer": "".join(parts).strip(),
           "raw": [row.strip() for row in log.splitlines() if row.strip()],
           "metrics": metrics(start, first, load=round(go_seconds(stats.get("load duration", "")), 2),
                              out=count("eval count"), tps=float(rate[0]) if rate else None,
                              **{"in": count("prompt eval count")})}


VIA = {"cli": via_cli, "api": via_api, "openai": via_openai}


def ask(door, prompt):
    """Вопрос через одну дверь. Ошибка становится событием, а не обрывом потока."""
    try:
        yield from VIA[door](prompt)
    except (httpx.HTTPError, OSError, RuntimeError, subprocess.SubprocessError,
            json.JSONDecodeError) as e:
        yield {"t": "error", "message": f"{type(e).__name__}: {e}"}


# ── Лестница ─────────────────────────────────────────────────────────

def final_number(answer):
    found = re.findall(r"Ответ\W*?(-?\d+)", answer)
    return found[-1] if found else None


def grade(item, answer):
    """Вердикт: ok, part или bad, и почему."""
    if item["check"] == "has":
        return ({"mark": "ok", "note": f"есть {item['expect']}"} if item["expect"] in answer
                else {"mark": "bad", "note": f"нет {item['expect']}"})
    if item["check"] == "final":
        n = final_number(answer)
        if n is None:
            return {"mark": "bad", "note": "нет строки «Ответ: …»"}
        return ({"mark": "ok", "note": f"ответ {n}"} if n == item["expect"]
                else {"mark": "bad", "note": f"ответ {n}, верно {item['expect']}"})
    text = answer.strip()
    bare = re.sub(r"^```\w*\s*|\s*```$", "", text)
    try:
        got = json.loads(bare)
    except ValueError:
        return {"mark": "bad", "note": "не JSON"}
    if not isinstance(got, list) or not all(isinstance(x, str) for x in got):
        return {"mark": "bad", "note": "JSON, но не массив строк"}
    fenced = " (в обёртке ```)" if bare != text else ""
    if got == item["expect"]:
        return {"mark": "part" if fenced else "ok", "note": "JSON, порядок верный" + fenced}
    if sorted(got) == sorted(item["expect"]):
        return {"mark": "part", "note": "JSON, планеты те, но порядок не тот" + fenced}
    return {"mark": "bad", "note": "JSON, но планеты не те" + fenced}


def ladder():
    """Вопросы лестницы по очереди через родной API: так видны загрузка и
    скорость генерации, которые сообщает сама Ollama."""
    yield {"t": "start", "total": len(LADDER)}
    results = []
    for i, item in enumerate(LADDER):
        yield {"t": "item", "i": i}
        record = None
        for event in ask("api", item["q"]):
            if event["t"] == "done":
                event["grade"] = grade(item, event["answer"])
                record = {f: event[f] for f in ("answer", "metrics", "grade")}
            elif event["t"] == "error":
                record = {"error": event["message"]}
            yield {**event, "i": i}
        results.append(record)
    saved = all(r and "grade" in r for r in results)
    if saved:
        keep("ladder", {"at": now(), "results": results}, status=status())
    yield {"t": "end", "saved": saved}


def door(name, prompt):
    """Одна дверь с записью в снимок."""
    command = ""
    for event in ask(name, prompt):
        if event["t"] == "start":
            command = event["command"]
        if event["t"] == "done":
            keep("doors", {name: {"at": now(), "prompt": prompt, "command": command,
                                  **{f: event[f] for f in ("answer", "metrics", "raw")}}}, status=status())
        yield event


# ── Снимок ───────────────────────────────────────────────────────────

def snapshot():
    try:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def keep(part, value, **more):
    """Записать часть снимка. Состояние машины (status) передают прогоны дня 26:
    плашка снимка датирует им прогон, и чат дня 27 его не трогает."""
    data = snapshot()
    data[part] = {**data.get(part, {}), **value} if part == "doors" else value
    data.update(more)
    tmp = SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SNAPSHOT)


def overview():
    return {"status": status(), "snapshot": snapshot(), "prompt": PROMPT,
            "ladder": [{f: item[f] for f in ("level", "what", "q")} for item in LADDER]}


# ── День 27: локальный ассистент ─────────────────────────────────────
# Чат поверх той же модели. Ответ идёт через OpenAI-совместимый /v1 — тот же
# протокол, что у облачных провайдеров стенда: для приложения сменился только
# адрес. Контекст модели в Ollama — 4096 токенов, поэтому в запрос уходит
# роль и последние WINDOW реплик, а в базе лежит весь чат.

ROLE = ("Ты — локальный ассистент: работаешь на компьютере пользователя, без интернета. "
        "Отвечай по-русски, по делу и коротко, если не просят подробнее. "
        "Если чего-то не знаешь — скажи прямо, не придумывай.")
WINDOW = 12
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
CLOUD_KEYS = ("DEEPSEEK_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY")
NET = []  # журнал сети приложения за жизнь процесса, последние 200 запросов


class Blocked(httpx.TransportError):
    """Запрос к чужой машине: охранник не выпустил его из транспорта."""


def watch(request, log):
    """Запрос — в журнал; к чужой машине — отбить, пока он не ушёл."""
    url = request.url
    port = url.port or (443 if url.scheme == "https" else 80)
    entry = {"at": time.strftime("%H:%M:%S"), "method": request.method,
             "host": f"{url.host}:{port}", "path": url.path, "sent": len(request.content)}
    log.append(entry)
    if log is NET:
        del NET[:-200]
    request.extensions["net"] = entry
    if url.host not in LOCAL_HOSTS:
        entry["blocked"] = True
        raise Blocked(f"{url.host} — не эта машина: приложение ходит только на {', '.join(LOCAL_HOSTS)}")
    return entry


class LocalOnly(httpx.HTTPTransport):
    """Транспорт приложения: пускает только на эту машину, каждый запрос — в журнал."""

    def __init__(self, log=NET):
        super().__init__()
        self.log = log

    def handle_request(self, request):
        entry, start = watch(request, self.log), time.perf_counter()
        response = super().handle_request(request)
        entry.update(status=response.status_code, ms=round((time.perf_counter() - start) * 1000))
        return response


class LocalOnlyAsync(httpx.AsyncHTTPTransport):
    """То же для асинхронного клиента — им стенд ходит к моделям потоком."""

    def __init__(self, log=NET):
        super().__init__()
        self.log = log

    async def handle_async_request(self, request):
        entry, start = watch(request, self.log), time.perf_counter()
        response = await super().handle_async_request(request)
        entry.update(status=response.status_code, ms=round((time.perf_counter() - start) * 1000))
        return response


def client(log=NET):
    return httpx.Client(transport=LocalOnly(log), timeout=300)


def async_client(log=NET):
    return httpx.AsyncClient(transport=LocalOnlyAsync(log), timeout=300, default_encoding="utf-8")


def chat_models():
    """Чат-модели, которые стоят в Ollama, — без эмбеддеров."""
    with client() as c:
        tags = c.get(f"{OLLAMA}/api/tags").json()["models"]
    return [m["name"] for m in tags if "embed" not in m["name"]]


CHATS = """
CREATE TABLE IF NOT EXISTS local_chats (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    model    TEXT NOT NULL,
    created  TEXT NOT NULL,
    updated  TEXT NOT NULL,
    messages TEXT NOT NULL
)
"""


def chats_db():
    db = sqlite3.connect(store.FILE, timeout=5)
    db.execute(CHATS)
    return db


def new_chat(model):
    chat = {"id": uuid.uuid4().hex[:10], "title": "Новый чат", "model": model,
            "created": now(), "updated": now(), "messages": []}
    save_chat(chat)
    return chat


def save_chat(chat):
    with closing(chats_db()) as db, db:
        db.execute("INSERT OR REPLACE INTO local_chats VALUES (?, ?, ?, ?, ?, ?)",
                   (chat["id"], chat["title"], chat["model"], chat["created"], chat["updated"],
                    json.dumps(chat["messages"], ensure_ascii=False)))


def load_chat(chat_id):
    with closing(chats_db()) as db:
        row = db.execute("SELECT id, title, model, created, updated, messages FROM local_chats "
                         "WHERE id = ?", (chat_id,)).fetchone()
    if not row:
        return None
    return {**dict(zip(("id", "title", "model", "created", "updated"), row[:5])),
            "messages": json.loads(row[5])}


def list_chats():
    with closing(chats_db()) as db:
        rows = db.execute("SELECT id, title, model, updated, messages FROM local_chats "
                          "ORDER BY updated DESC LIMIT 30").fetchall()
    return [{"id": r[0], "title": r[1], "model": r[2], "updated": r[3], "count": len(json.loads(r[4]))}
            for r in rows]


def send(chat, text):
    """Реплика и ответ потоком. Ответ сохраняется и тогда, когда его оборвали
    («Стоп» закрывает соединение): в чате остаётся начало с пометкой."""
    chat["messages"].append({"role": "user", "content": text, "at": now()})
    if chat["title"] == "Новый чат":
        chat["title"] = text[:48]
    window = chat["messages"][-WINDOW:]
    body = {"model": chat["model"], "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": ROLE},
                         *({"role": m["role"], "content": m["content"]} for m in window)]}
    yield {"t": "start", "window": len(window), "of": len(chat["messages"])}
    message = {"role": "assistant", "model": chat["model"], "window": len(window)}
    parts, usage, net, got = [], {}, None, 0
    start, first, end = time.perf_counter(), None, None
    stopped = True
    try:
        with client() as c, c.stream("POST", f"{OLLAMA}/v1/chat/completions", json=body) as r:
            net = r.request.extensions["net"]
            yield {"t": "net", "net": net}
            r.raise_for_status()
            for raw in r.iter_lines():
                got += len(raw.encode()) + 1
                if not raw.startswith("data: ") or raw == "data: [DONE]":
                    continue
                chunk = json.loads(raw[6:])
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices", []):
                    piece = choice.get("delta", {}).get("content") or ""
                    if piece:
                        first = first or time.perf_counter()
                        end = time.perf_counter()
                        parts.append(piece)
                        yield {"t": "delta", "text": piece}
        stopped = False
    except (httpx.HTTPError, ValueError) as e:
        stopped = False
        message["error"] = f"{type(e).__name__}: {e}"
    finally:
        out = usage.get("completion_tokens")
        tps = (out - 1) / (end - first) if out and first and end > first else None
        if net:
            net["got"] = got
        message.update(content="".join(parts), at=now(), net=net,
                       metrics={"ttft": round(first - start, 2) if first else None,
                                "seconds": round(time.perf_counter() - start, 2),
                                "in": usage.get("prompt_tokens"), "out": out, "tps": tps and round(tps, 1)})
        if stopped:
            message["stopped"] = True
        chat["messages"].append(message)
        chat["updated"] = now()
        save_chat(chat)
        keep("assist", {"chat": chat})
    yield {"t": "done", "message": message}


def probe():
    """Проверка охранника: клиент приложения пробует сходить к облачному провайдеру."""
    try:
        with client() as c:
            r = c.post("https://api.deepseek.com/chat/completions", json={"model": "deepseek-chat"})
    except Blocked as e:
        return {"blocked": True, "message": str(e), "net": NET[-1]}
    return {"blocked": False, "status": r.status_code, "net": NET[-1]}


def assist_overview():
    st = status()
    try:
        models = chat_models() if st["live"] else []
    except httpx.HTTPError:
        models = []
    return {"status": st, "live": st["live"], "models": models, "default": MODEL, "window": WINDOW,
            "chats": list_chats() if st["live"] else [], "snapshot": snapshot().get("assist"),
            "guard": LOCAL_HOSTS, "cloud_keys": sum(bool(os.environ.get(k)) for k in CLOUD_KEYS),
            "net": NET[-40:]}


# ── День 28: локальный RAG ───────────────────────────────────────────
# Поиск — индекс недели 5: эмбеддинг вопроса считает embeddinggemma в этой же
# Ollama, косинус — numpy. Ответ — эта модель, промпт и правила дня 22. Облачная
# модель отвечает на тех же фрагментах — для сравнения. Каждый контрольный
# вопрос задаётся REPEATS раз: так видно, держит ли модель ответ. Повторы идут
# кругами по всему набору, а не подряд. Ollama держит в памяти кеш уже
# обработанных промптов (в её логе — «cache state: 9 prompts, 530 MiB»), и
# повтор берёт промпт оттуда: первый токен через 0,07 с вместо 0,6. Поэтому
# время считается отдельно для первого круга (промпты новые) и для повторов.

REPEATS = 3


def warm(c):
    """Эмбеддер и модель — в память до замеров: загрузка не должна попасть во
    время первого вопроса. Модель сначала выгружается — это сбрасывает кеш
    промпта Ollama: иначе вопрос, который уже задавали до прогона, пришёл бы
    из кеша, и замер «новый вопрос» соврал бы."""
    c.post(f"{OLLAMA}/api/generate", json={"model": MODEL, "keep_alive": 0}).raise_for_status()
    start = time.perf_counter()
    rag.embed(["прогрев"], c)
    middle = time.perf_counter()
    c.post(f"{OLLAMA}/api/generate", json={"model": MODEL}).raise_for_status()
    return {"embed": round(middle - start, 2), "model": round(time.perf_counter() - middle, 2)}


def alien(text):
    """Буквы не из кириллицы и латиницы: у 3B-модели проскакивают китайские и грузинские."""
    return sorted({ch for ch in text
                   if ch.isalpha() and not unicodedata.name(ch, "").startswith(("CYRILLIC", "LATIN"))})


def check(q, answer, hits, metrics):
    """Сверка дня 22 (факты, отказ, ссылка на нужный документ) у контрольного
    вопроса и сбои, которые видны у любого: ссылка на фрагмент, которого не
    было, чужие буквы, обрыв по лимиту, пустой ответ. Пустым считается и
    ответ из одних ссылок — 3B-модель отвечает «[2]» вместо текста."""
    refs = {int(n) for n in rag.CITE.findall(answer)}
    out = {"bad_refs": sorted(n for n in refs if not 0 < n <= len(hits)), "alien": alien(answer),
           "cut": (metrics.get("out") or 0) >= rag.ANSWER_TOKENS,
           "empty": not re.sub(r"\[\d+\]|[\W_]", "", answer)}
    question = rag.question_of(q)
    if question:
        out.update(rag.grade(question, answer, hits))
    return out


def median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def summary28(results):
    """Итог прогона по колонкам: качество, скорость, стабильность. Время —
    отдельно для первого круга и для повторов, где промпт уже в кеше Ollama."""
    runs = {}
    for r in results:
        runs.setdefault(r["i"], []).append(r)
    out = {"questions": len(runs), "repeats": max((len(v) for v in runs.values()), default=0)}
    for side in ("local", "cloud"):
        rows = [(rag.QUESTIONS[r["i"]], r[side]) for r in results if r.get(side)]
        good = [(x, a) for x, a in rows if not a.get("error")]
        grades = [a["grade"] for _, a in good]
        inside = [a["grade"] for x, a in good if not x.get("outside")]
        times = [a["metrics"] for _, a in good]
        new, again = ([r[side]["metrics"] for r in results if r.get(side) and not r[side].get("error")
                       and (r["r"] == 0) == first] for first in (True, False))
        stable = same = 0
        spread = []
        for group in runs.values():
            answers = [r.get(side) or {"error": "нет"} for r in group]
            if any(a.get("error") for a in answers):
                continue
            stable += len({a["grade"]["verdict"] for a in answers}) == 1
            same += len({a["answer"].strip() for a in answers}) == 1
            seconds = [a["metrics"]["seconds"] for a in answers]
            spread.append(max(seconds) - min(seconds))
        out[side] = {
            "total": len(rows), "errors": len(rows) - len(good),
            **{v: sum(g["verdict"] == v for g in grades) for v in ("ok", "part", "bad")},
            "facts": sum(g["facts"] for g in inside), "of": sum(g["of"] for g in inside),
            "outside": sum(a["grade"]["refused"] for x, a in good if x.get("outside")),
            "outside_of": sum(1 for x, _ in rows if x.get("outside")),
            "cited_ok": sum(g["cited_ok"] for g in inside), "inside": len(inside),
            **{flag: sum(bool(g[flag]) for g in grades) for flag in ("bad_refs", "alien", "cut", "empty")},
            "ttft": median(m["ttft"] for m in new), "ttft_again": median(m["ttft"] for m in again),
            "seconds": median(m["seconds"] for m in new), "seconds_again": median(m["seconds"] for m in again),
            "worst": max((m["seconds"] for m in times), default=None), "tps": median(m["tps"] for m in times),
            "tokens_in": round(statistics.mean(m["in"] for m in times)) if times else None,
            "tokens_out": round(statistics.mean(m["out"] for m in times)) if times else None,
            "cost": round(sum(m["cost"] for m in times), 5),
            "stable": stable, "same": same, "spread": round(statistics.mean(spread), 2) if spread else None}
    out["search"] = {"ms": median(r.get("search_ms") for r in results),
                     "embed_ms": median(r.get("embed_ms") for r in results),
                     "same": sum(len({tuple(h["id"] for h in r.get("hits", [])) for r in group}) == 1
                                 for group in runs.values())}
    return out


def rag_overview():
    st = status()
    return {"status": st, "live": st["live"], "model": MODEL, "embedder": rag.MODEL, "top": rag.TOP,
            "repeats": REPEATS, "questions": rag.question_set(), "snapshot": snapshot().get("rag"),
            "chunks": len(rag.loaded()["index"]["struct"][0]), "guard": LOCAL_HOSTS}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for event in ladder():
        if event["t"] == "done":
            m, g = event["metrics"], event["grade"]
            print(f"{LADDER[event['i']]['level']:>11}: {g['mark']:4} {g['note']:40} "
                  f"{m['seconds']} с · {m['out']} ток · {m['tps']} ток/с")
        elif event["t"] == "error":
            print("ошибка:", event["message"])
