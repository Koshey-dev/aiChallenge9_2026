"""День 21: индексация документов — текст, чанки двумя способами, эмбеддинги, SQLite.

Источник — папка с гайдами по геймдеву (pdf и docx, путь в `RAG_DOCS`). Текст
pdf достаёт `pdftotext`, docx разбирается как zip со стилями абзацев. Из
каждого файла получается документ: сплошной текст и блоки в нём — абзацы и
заголовки со смещениями и страницами. Файл, где текста меньше страницы
(скан, картинки, пустой шаблон), в индекс не идёт, причина остаётся в списке.

Стратегии разбиения:

- `fixed` — по размеру: окно `SIZE` символов с перекрытием `OVERLAP`, край
  сдвигается к ближайшему пробелу, про разделы окно не знает;
- `struct` — по структуре: раздел от заголовка до заголовка (в pdf-слайдах —
  страница с её первой строкой), длинный раздел режется по абзацам и
  предложениям, короткий приклеивается к соседу.

Эмбеддинги считает `embeddinggemma` через REST Ollama. Модели нужен префикс:
чанк уходит как `title: … | text: …`, запрос — как `task: search result |
query: …`. В `title` у `fixed` — только документ, у `struct` — документ и путь
раздела: знание о структуре и есть то, что вторая стратегия добавляет.

Индекс — `rag.db` рядом с модулем: документы, чанки с метаданными, векторы
float32, итоги сборки и кеш эмбеддингов по хешу входа (пересборка без правок
не зовёт модель). Поиск — косинус по всем векторам numpy, их здесь сотни.
Сборка пишет всё одной транзакцией в конце, так что прерванная сборка
оставляет старый индекс целым.

Запуск из консоли: `py web/rag.py` — собрать и напечатать сравнение,
`--fresh` — мимо кеша.

С дня 22 здесь же всё, что вокруг первого RAG-запроса: какие чанки взять,
как сложить их с вопросом в промпт, десять контрольных вопросов с ожиданием
и сверка ответа с ним. Сам запрос к модели делает app.py.

День 23 добавляет второй этап после поиска: из топ-K₁ кандидатов остаются
прошедшие порог (косинус или оценка LLM-реранкера), не больше K₂, а до поиска
модель может переписать вопрос. Тут же разговорный двойник контрольного
набора и данные для подбора порога.

День 24 требует от ответа три части — текст, источники по chunk_id и
дословные цитаты — и проверяет их: источники из контекста, цитаты в своих
чанках, смысл ответа против цитат (судья — другая модель). Ниже порога
релевантности ответа нет: «не знаю» и уточняющий вопрос.

День 25 — мини-чат: память задачи (цель, уточнения, ограничения, термины,
выводы), которую ведёт планировщик, поисковые запросы на каждое сообщение,
окно истории, источники по [n] и два длинных сценария с проверкой хода.
Сами запросы к модели и хранение хода делает app.py, чаты — таблица
`rag_chats` в agent.db.
"""

import difflib
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import closing
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv

import store

# Модуль читает настройки при импорте, а app.py грузит .env позже, — и из
# консоли (`py web/rag.py`) его тоже никто не грузит. Пустая строка из
# шаблона .env — «не задано», а не текущая папка.
load_dotenv()
HERE = Path(__file__).parent
FILE = HERE / "rag.db"
DOCS = Path(os.environ.get("RAG_DOCS") or r"E:\Learning\Гемдев\Гайды(изучить отобрать нужные)")
OLLAMA = os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434"
MODEL = "embeddinggemma:300m-qat-q8_0"
# Git для Windows кладёт pdftotext к себе в mingw64, в PATH он там не попадает.
PDFTOTEXT = (os.environ.get("PDFTOTEXT") or shutil.which("pdftotext")
             or r"C:\Program Files\Git\mingw64\bin\pdftotext.exe")

MIN_TEXT = 1000          # меньше страницы текста — в индекс не идёт
SIZE, OVERLAP = 1000, 200  # стратегия fixed
MAX, MIN = 1500, 200     # стратегия struct: потолок чанка и порог «крошки»
BATCH = 32               # столько чанков за один запрос к Ollama
TOP = 5

STRATEGIES = {"fixed": "По размеру", "struct": "По структуре"}

# Контрольные вопросы: что спросить, в каком документе ответ и какие строки
# обязаны быть в найденном чанке все вместе. Попадание — чанк из этого
# документа с ответом внутри, а не просто «тот же файл».
CHECKS = [
    ("Какой бюджет советуют на тест в Snapchat Ads?", "2024-gayd-istochniki-mobi",
     ("площадка — snapchat", "1000 установок")),
    ("Удержание первого дня у казуальных игр в Европе", "gameanalytics-q1-2024-mob",
     ("in europe, casual games", "31.79")),
    ("Кому переходят исключительные права на результат работ?", "obrazets-dogovor-podryada",
     ("исключительн",)),
    ("Как проверить, кто из игроков сейчас онлайн в чате игры?", "gayd-vstraivaem-chat-v-ig",
     ("isonline:true",)),
    ("Как показать цветом разные команды или противников?", "rekomendatsii-po-podboru",
     ("контрастные или околоконстрастные",)),
    ("Какие браузерные площадки подходят для релиза игры?", "unity-conf-2024-bonus-den",
     ("браузерные платформы",)),
    ("Какие приложения заработали больше всех в 2023 году?", "2023-otchet-po-rynku-mobi",
     ("youtube", "$")),
    ("Как собрать ключевые фразы для ASO через ChatGPT?", "aso-s-pomoschyu-chat-gpt",
     ("ключевые фразы",)),
    ("Почему в гиперказуальных играх нельзя делать чёрные тени?", "voodoo-art-manualv3",
     ("black shadows",)),
    ("Что входит в финансовый план игры?", "sozdanie-biznes-plana-igr",
     ("финансового плана",)),
    ("Что такое RuStore и чем он полезен разработчику?", "spisok-ploschadok-dlya-re",
     ("rustore это",)),
    ("Какой плагин Unity добавляет историю выделения объектов?", "assety-po-kategoriyam",
     ("selection history",)),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id       TEXT PRIMARY KEY,
    ord      INTEGER NOT NULL,
    source   TEXT NOT NULL,
    file     TEXT NOT NULL,
    title    TEXT NOT NULL,
    kind     TEXT NOT NULL,
    pages    INTEGER NOT NULL,
    chars    INTEGER NOT NULL,
    skipped  TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL DEFAULT '',
    sections TEXT NOT NULL DEFAULT '[]',
    pages_at TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS chunks (
    id        TEXT PRIMARY KEY,
    strategy  TEXT NOT NULL,
    doc       TEXT NOT NULL,
    ord       INTEGER NOT NULL,
    section   TEXT NOT NULL,
    page_from INTEGER NOT NULL,
    page_to   INTEGER NOT NULL,
    start     INTEGER NOT NULL,
    stop      INTEGER NOT NULL,
    chars     INTEGER NOT NULL,
    cut       INTEGER NOT NULL,
    crosses   INTEGER NOT NULL,
    text      TEXT NOT NULL,
    x         REAL NOT NULL DEFAULT 0,
    y         REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vectors (chunk TEXT PRIMARY KEY, vec BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, vec BLOB NOT NULL);
"""

BUILDING = threading.Lock()


def connect():
    db = sqlite3.connect(FILE, timeout=5)
    db.executescript(SCHEMA)
    return db


# ── Текст ────────────────────────────────────────────────────────────────

LATIN = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                 ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n",
                  "o", "p", "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y",
                  "", "e", "yu", "ya"]))


def slug(name):
    """Короткий латинский id документа — он же часть chunk_id."""
    s = "".join(LATIN.get(c, c) for c in name.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:25].strip("-")


def clean(s):
    s = s.replace("\ufffd", "").replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b-\x1f]", "", s)).strip()


def is_heading(line):
    """Первая строка слайда годится в заголовок: короткая, с буквами, не обрывок фразы."""
    return (3 <= len(line) <= 70 and re.search(r"[A-Za-zА-Яа-яЁё]", line)
            and not line.endswith((",", ";", "-", "—")))


def blocks_pdf(path):
    """Блоки pdf: (текст, страница, уровень заголовка). Заголовок — первая строка страницы."""
    raw = subprocess.run([PDFTOTEXT, "-enc", "UTF-8", str(path), "-"],
                         capture_output=True, check=True).stdout.decode("utf-8", errors="replace")
    out = []
    for page, body in enumerate(raw.split("\f")[:-1] or [raw], 1):
        paras = [[clean(line) for line in p.splitlines()] for p in re.split(r"\n\s*\n", body)]
        paras = [[line for line in p if line] for p in paras]
        paras = [p for p in paras if p]
        if paras and is_heading(paras[0][0]):
            out.append((paras[0][0], page, 1))
            paras[0] = paras[0][1:]
        out += [(" ".join(p), page, 0) for p in paras if p]
    return out, max(1, raw.count("\f"))


BOLD = re.compile(r'<w:b(?: w:val="(?:1|true|on)")?\s*/>')


def blocks_docx(path):
    """Блоки docx. Заголовок — стиль Heading/Title или короткий абзац целиком жирным.

    Страницу подсказывает сам Word: при сохранении он оставляет метки
    `lastRenderedPageBreak` там, где в последний раз разрывал страницу.
    Файлы не из Word (Google Docs) меток не несут — тогда страница 0: не знаем.
    """
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    marked = "<w:lastRenderedPageBreak/>" in xml or '<w:br w:type="page"/>' in xml
    out, page = [], 1 if marked else 0
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        if marked:
            page += p.count("<w:lastRenderedPageBreak/>") + p.count('<w:br w:type="page"/>')
        runs = [(bool(BOLD.search(r)), "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", r)))
                for r in re.findall(r"<w:r[ >].*?</w:r>", p, re.S)]
        text = clean(html.unescape("".join(t for _, t in runs)))
        if not text:
            continue
        style = re.search(r'<w:pStyle w:val="([^"]+)"', p)
        style = style.group(1) if style else ""
        if style in ("Title", "Heading1"):
            level = 1
        elif style.startswith("Heading") or (len(text) < 100 and all(b for b, t in runs if t.strip())):
            level = 2
        else:
            level = 0
        out.append((text, page, level))
    return out, page


def extract(path):
    """Документ: сплошной текст, блоки со смещениями, разделы и начала страниц."""
    kind = path.suffix.lower()[1:]
    raw, pages = blocks_pdf(path) if kind == "pdf" else blocks_docx(path)
    text, blocks, sections, pages_at, h1 = "", [], [], [], ""
    for body, page, level in raw:
        if text:
            text += "\n\n"
        if level or not sections:
            if level == 1:
                h1 = title = body
            elif level:
                title = f"{h1} › {body}" if h1 else body
            else:
                title = "Начало документа"
            sections.append({"title": title, "start": len(text), "page": page})
        if not pages_at or pages_at[-1][0] != page:
            pages_at.append([page, len(text)])
        blocks.append({"start": len(text), "stop": len(text) + len(body),
                       "page": page, "section": len(sections) - 1})
        text += body
    title = re.sub(r"[_\s]+", " ", path.stem).strip()
    return {"id": slug(path.stem), "source": path.relative_to(DOCS).as_posix(),
            "file": path.name, "title": title, "kind": kind, "pages": pages,
            "chars": len(text), "text": text, "blocks": blocks, "sections": sections,
            "pages_at": pages_at,
            "skipped": "" if len(text) >= MIN_TEXT else
                       f"{len(text)} символов — картинки или пустой шаблон"}


# ── Разбиение ────────────────────────────────────────────────────────────

def space_before(text, lo, hi):
    """Последний пробел или перевод строки в [lo, hi), иначе -1."""
    return max(text.rfind(" ", lo, hi), text.rfind("\n", lo, hi))


def fixed(doc):
    """Окна по SIZE символов с перекрытием OVERLAP, края — по пробелам."""
    text, out, start = doc["text"], [], 0
    while start < len(text):
        stop = min(start + SIZE, len(text))
        if stop < len(text):
            space = space_before(text, stop - 100, stop)
            stop = space if space > start else stop
        out.append((start, stop))
        if stop >= len(text):
            break
        nxt = stop - OVERLAP
        space = space_before(text, nxt - 60, nxt)
        start = space + 1 if space > start else nxt
    return out


def split_long(text, start, stop):
    """Кусок длиннее MAX — по предложениям, предложение длиннее MAX — по пробелу."""
    if stop - start <= MAX:
        return [(start, stop)]
    ends = [start + m.end() for m in re.finditer(r"[.!?…](?=\s)", text[start:stop])] + [stop]
    out, cur = [], start
    while cur < stop:
        fit = [e for e in ends if cur < e <= cur + MAX]
        if fit:
            nxt = fit[-1]
        else:
            space = space_before(text, cur + MAX - 150, cur + MAX)
            nxt = space if space > cur else cur + MAX
        out.append((cur, nxt))
        cur = nxt
        while cur < stop and text[cur].isspace():
            cur += 1
    return out


def struct(doc):
    """Разделы целиком; длинный — по абзацам до MAX, крошка — к соседу."""
    pieces, by_section = [], {}
    for b in doc["blocks"]:
        by_section.setdefault(b["section"], []).append(b)
    for blocks in by_section.values():
        if blocks[-1]["stop"] - blocks[0]["start"] <= MAX:
            pieces.append([blocks[0]["start"], blocks[-1]["stop"]])
            continue
        cur = None
        for b in blocks:
            for s, e in split_long(doc["text"], b["start"], b["stop"]):
                if cur and e - cur[0] <= MAX:
                    cur[1] = e
                else:
                    if cur:
                        pieces.append(cur)
                    cur = [s, e]
        pieces.append(cur)
    merged = []
    for p in pieces:
        last = merged[-1] if merged else None
        if last and min(last[1] - last[0], p[1] - p[0]) < MIN and p[1] - last[0] <= MAX:
            last[1] = p[1]
        else:
            merged.append(p)
    return [tuple(p) for p in merged]


def describe(doc, strategy, ordinal, start, stop):
    """Чанк с метаданными и двумя отметками формы: обрыв фразы и захват разделов."""
    text = doc["text"]
    while start < stop and text[start].isspace():
        start += 1
    while stop > start and text[stop - 1].isspace():
        stop -= 1
    touched = [b for b in doc["blocks"] if b["start"] < stop and b["stop"] > start]
    secs = sorted({b["section"] for b in touched})
    names = [doc["sections"][s]["title"] for s in secs]
    body = text[start:stop]
    at_edge = any(b["stop"] == stop for b in touched)
    return {"id": f"{strategy}:{doc['id']}:{ordinal:04d}", "strategy": strategy,
            "doc": doc["id"], "ord": ordinal,
            "section": names[0] if len(names) == 1 else f"{names[0]} → {names[-1]}",
            "page_from": touched[0]["page"], "page_to": touched[-1]["page"],
            "start": start, "stop": stop, "chars": len(body),
            "cut": int(not at_edge and not body.endswith((".", "!", "?", "…", ":", ";", "»", ")"))),
            "crosses": int(len(secs) > 1), "text": body}


def embed_input(doc, chunk):
    title = doc["title"] if chunk["strategy"] == "fixed" else f"{doc['title']} › {chunk['section']}"
    return f"title: {title} | text: {chunk['text']}"


def query_input(q):
    return f"task: search result | query: {q}"


# ── Эмбеддинги ───────────────────────────────────────────────────────────

def embed(texts):
    r = httpx.post(f"{OLLAMA}/api/embed", json={"model": MODEL, "input": texts}, timeout=600)
    r.raise_for_status()
    vecs = np.array(r.json()["embeddings"], dtype=np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def embed_cached(db, inputs, fresh):
    """Векторы по входам; готовые берутся из кеша. Отдаёт ход по пачкам, в конце — массив."""
    keys = [hashlib.sha256(f"{MODEL}\n{x}".encode()).hexdigest() for x in inputs]
    have = {}
    if not fresh:
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            have.update(db.execute(f"SELECT key, vec FROM cache WHERE key IN ({','.join('?' * len(part))})",
                                   part))
    todo = [i for i, k in enumerate(keys) if k not in have]
    yield {"total": len(inputs), "cached": len(inputs) - len(todo), "done": len(inputs) - len(todo)}
    for n in range(0, len(todo), BATCH):
        part = todo[n:n + BATCH]
        vecs = embed([inputs[i] for i in part])
        with db:
            db.executemany("INSERT OR REPLACE INTO cache VALUES (?, ?)",
                           [(keys[i], v.tobytes()) for i, v in zip(part, vecs)])
        have.update((keys[i], v.tobytes()) for i, v in zip(part, vecs))
        yield {"total": len(inputs), "cached": len(inputs) - len(todo),
               "done": len(inputs) - len(todo) + n + len(part)}
    yield np.stack([np.frombuffer(have[k], dtype=np.float32) for k in keys])


def pca(vecs):
    """Две главные компоненты: точки на карту, чем проецировать запрос и какую
    долю разброса (в %) эти две оси вообще удерживают."""
    mean = vecs.mean(axis=0)
    _, s, vt = np.linalg.svd(vecs - mean, full_matrices=False)
    return mean, vt[:2], round(float(100 * (s[:2] ** 2).sum() / (s ** 2).sum()), 1)


# ── Сравнение ────────────────────────────────────────────────────────────

BUCKETS = [0, 200, 400, 600, 800, 1000, 1200, 1400, 1600]


def stats(chunks, seconds, cached):
    sizes = np.array([c["chars"] for c in chunks])
    n = len(chunks)
    hist = [int(((sizes >= lo) & (sizes < hi)).sum()) for lo, hi in zip(BUCKETS, BUCKETS[1:])]
    return {"chunks": n, "chars": int(sizes.sum()), "avg": round(float(sizes.mean())),
            "median": int(np.median(sizes)), "min": int(sizes.min()), "max": int(sizes.max()),
            "cut": round(100 * sum(c["cut"] for c in chunks) / n, 1),
            "crosses": round(100 * sum(c["crosses"] for c in chunks) / n, 1),
            "tiny": round(100 * int((sizes < MIN).sum()) / n, 1),
            "hist": hist + [int((sizes >= BUCKETS[-1]).sum())], "seconds": round(seconds, 1),
            "cached": cached}


def rank(chunks, scores, doc, markers):
    """Место первого чанка с ответом в топе (1…TOP) или 0."""
    for place, i in enumerate(np.argsort(-scores)[:TOP], 1):
        if chunks[i]["doc"] == doc and all(m in chunks[i]["text"].lower() for m in markers):
            return place
    return 0


def evaluate(index, qvecs):
    out = {"questions": [], "summary": {}}
    for s, (chunks, vecs) in index.items():
        places = [rank(chunks, vecs @ q, doc, markers) for q, (_, doc, markers) in zip(qvecs, CHECKS)]
        out["summary"][s] = {"hit1": sum(p == 1 for p in places), "hit3": sum(0 < p <= 3 for p in places),
                             "mrr": round(sum(1 / p for p in places if p) / len(places), 3),
                             "total": len(places)}
        for i, p in enumerate(places):
            if len(out["questions"]) <= i:
                q, doc, markers = CHECKS[i]
                out["questions"].append({"q": q, "doc": doc, "markers": markers})
            out["questions"][i][s] = p
    return out


# ── Сборка ───────────────────────────────────────────────────────────────

def build(fresh=False):
    """Пайплайн целиком; отдаёт события хода сборки, последнее — `done`."""
    if not BUILDING.acquire(blocking=False):
        yield {"t": "error", "text": "Индекс уже собирается."}
        return
    try:
        yield from _build(fresh)
    except (httpx.HTTPError, OSError, subprocess.CalledProcessError) as e:
        yield {"t": "error", "text": f"{type(e).__name__}: {e}"}
    finally:
        BUILDING.release()


def _build(fresh):
    if not DOCS.is_dir():
        yield {"t": "error", "text": f"Нет папки с документами: {DOCS}"}
        return
    began = time.monotonic()
    yield {"t": "stage", "stage": "extract", "text": f"Извлекаю текст из {DOCS.name}"}
    files = sorted(p for p in DOCS.rglob("*") if p.suffix.lower() in (".pdf", ".docx"))
    docs = []
    for path in files:
        doc = extract(path)
        docs.append(doc)
        yield {"t": "doc", **{k: doc[k] for k in ("id", "title", "kind", "pages", "chars", "skipped")}}
    kept = [d for d in docs if not d["skipped"]]
    times = {"extract": round(time.monotonic() - began, 1)}

    yield {"t": "stage", "stage": "chunk", "text": "Режу на чанки двумя способами"}
    split = {"fixed": fixed, "struct": struct}
    chunks = {s: [describe(d, s, i, a, b) for d in kept for i, (a, b) in enumerate(split[s](d))]
              for s in STRATEGIES}
    yield {"t": "chunked", "counts": {s: len(c) for s, c in chunks.items()}}

    by_id = {d["id"]: d for d in kept}
    index, took, cached = {}, {}, {}
    with closing(connect()) as db:
        for s in STRATEGIES:
            yield {"t": "stage", "stage": "embed", "strategy": s,
                   "text": f"Эмбеддинги: {STRATEGIES[s].lower()}, {MODEL}"}
            t0 = time.monotonic()
            for step in embed_cached(db, [embed_input(by_id[c["doc"]], c) for c in chunks[s]], fresh):
                if isinstance(step, dict):
                    cached[s] = step["cached"]
                    yield {"t": "embed", "strategy": s, **step}
                else:
                    index[s] = (chunks[s], step)
            took[s] = time.monotonic() - t0

        yield {"t": "stage", "stage": "check", "text": f"Контрольные вопросы: {len(CHECKS)}"}
        qvecs = embed([query_input(q) for q, _, _ in CHECKS])
        checks = evaluate(index, qvecs)

        yield {"t": "stage", "stage": "save", "text": f"Пишу индекс в {FILE.name}"}
        maps, share = {}, {}
        for s, (rows, vecs) in index.items():
            mean, comp, share[s] = pca(vecs)
            xy = (vecs - mean) @ comp.T
            for c, (x, y) in zip(rows, xy):
                c["x"], c["y"] = float(x), float(y)
            maps[s] = {"mean": mean.round(6).tolist(), "comp": comp.round(6).tolist()}
        meta = {"model": MODEL, "dim": int(qvecs.shape[1]), "built": time.strftime("%Y-%m-%d %H:%M"),
                "source": str(DOCS), "size": SIZE, "overlap": OVERLAP, "max": MAX, "min": MIN,
                "stats": {s: stats(chunks[s], took[s], cached[s]) for s in STRATEGIES},
                "checks": checks, "maps": maps, "maps_var": share,
                "times": {**times, **{s: round(took[s], 1) for s in STRATEGIES}}}
        with db:
            for table in ("docs", "chunks", "vectors", "meta"):
                db.execute(f"DELETE FROM {table}")
            db.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                (d["id"], i, d["source"], d["file"], d["title"], d["kind"], d["pages"], d["chars"],
                 d["skipped"], d["text"], json.dumps(d["sections"], ensure_ascii=False),
                 json.dumps(d["pages_at"])) for i, d in enumerate(docs)])
            fields = ("id", "strategy", "doc", "ord", "section", "page_from", "page_to", "start",
                      "stop", "chars", "cut", "crosses", "text", "x", "y")
            for s, (rows, vecs) in index.items():
                db.executemany(f"INSERT INTO chunks VALUES ({','.join('?' * len(fields))})",
                               [tuple(c[f] for f in fields) for c in rows])
                db.executemany("INSERT INTO vectors VALUES (?, ?)",
                               [(c["id"], v.tobytes()) for c, v in zip(rows, vecs)])
            db.executemany("INSERT INTO meta VALUES (?, ?)",
                           [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
    yield {"t": "done", "seconds": round(time.monotonic() - began, 1), "bytes": FILE.stat().st_size,
           "stats": meta["stats"], "checks": checks["summary"]}


# ── Чтение индекса ───────────────────────────────────────────────────────

_loaded = {"mtime": None}


def loaded():
    """Чанки и векторы в памяти; перечитываются, когда файл индекса сменился."""
    mtime = FILE.stat().st_mtime if FILE.exists() else None
    if _loaded["mtime"] != mtime:
        with closing(connect()) as db:
            meta = {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}
            cols = ("id", "strategy", "doc", "section", "page_from", "page_to", "chars", "text", "x", "y")
            rows = [dict(zip(cols, r)) for r in db.execute(
                f"SELECT {', '.join('c.' + c for c in cols)} FROM chunks c ORDER BY c.strategy, c.doc, c.ord")]
            vecs = dict(db.execute("SELECT chunk, vec FROM vectors"))
            titles = dict(db.execute("SELECT id, title FROM docs"))
        index = {}
        for s in STRATEGIES:
            part = [r for r in rows if r["strategy"] == s]
            for r in part:
                r["title"] = titles.get(r["doc"], r["doc"])
            index[s] = (part, np.stack([np.frombuffer(vecs[r["id"]], dtype=np.float32) for r in part])
                        if part else np.zeros((0, 1), dtype=np.float32))
        _loaded.update(mtime=mtime, meta=meta, index=index)
    return _loaded


def overview():
    """Всё для шапки вкладки: документы, итоги сборки, сравнение, вопросы."""
    state = loaded()
    with closing(connect()) as db:
        docs = [dict(zip(("id", "source", "file", "title", "kind", "pages", "chars", "skipped"), r))
                for r in db.execute("SELECT id, source, file, title, kind, pages, chars, skipped "
                                    "FROM docs ORDER BY ord")]
        counts = dict(((d, s), n) for d, s, n in
                      db.execute("SELECT doc, strategy, COUNT(*) FROM chunks GROUP BY doc, strategy"))
    for d in docs:
        d["chunks"] = {s: counts.get((d["id"], s), 0) for s in STRATEGIES}
    meta = {k: v for k, v in state["meta"].items() if k != "maps"}
    return {"docs": docs, "meta": meta, "strategies": STRATEGIES, "can_build": DOCS.is_dir(),
            "bytes": FILE.stat().st_size if FILE.exists() else 0, "file": FILE.name}


def document(doc_id):
    """Разрез одного документа: разделы, начала страниц и чанки обеих стратегий."""
    with closing(connect()) as db:
        row = db.execute("SELECT title, chars, sections, pages_at FROM docs WHERE id = ?",
                         (doc_id,)).fetchone()
        if not row:
            return None
        cols = ("id", "strategy", "section", "page_from", "page_to", "start", "stop", "chars", "cut", "crosses")
        chunks = [dict(zip(cols, r)) for r in db.execute(
            f"SELECT {', '.join(cols)} FROM chunks WHERE doc = ? ORDER BY ord", (doc_id,))]
    return {"id": doc_id, "title": row[0], "chars": row[1], "sections": json.loads(row[2]),
            "pages_at": json.loads(row[3]),
            "chunks": {s: [c for c in chunks if c["strategy"] == s] for s in STRATEGIES}}


def chunk(chunk_id):
    """Карточка чанка: метаданные, текст и начало вектора."""
    with closing(connect()) as db:
        cols = ("id", "strategy", "doc", "ord", "section", "page_from", "page_to", "start", "stop",
                "chars", "cut", "crosses", "text")
        row = db.execute(f"SELECT {', '.join(cols)} FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            return None
        out = dict(zip(cols, row))
        out["source"], out["file"], out["title"] = db.execute(
            "SELECT source, file, title FROM docs WHERE id = ?", (out["doc"],)).fetchone()
        vec = np.frombuffer(db.execute("SELECT vec FROM vectors WHERE chunk = ?", (chunk_id,)).fetchone()[0],
                            dtype=np.float32)
    out["vector"] = {"dim": len(vec), "head": [round(float(v), 4) for v in vec[:8]],
                     "norm": round(float(np.linalg.norm(vec)), 4)}
    return out


def points():
    """Карта: каждый чанк — точка в плоскости двух главных компонент."""
    state = loaded()
    return {s: [{"id": c["id"], "doc": c["doc"], "x": round(c["x"], 4), "y": round(c["y"], 4)}
                for c in chunks] for s, (chunks, _) in state["index"].items()}


def search(q, k=TOP):
    """Топ-k каждой стратегии по косинусу и место запроса на карте."""
    state = loaded()
    qv = embed([query_input(q)])[0]
    out = {}
    for s, (chunks, vecs) in state["index"].items():
        scores = vecs @ qv
        m = state["meta"]["maps"][s]
        x, y = (qv - np.array(m["mean"])) @ np.array(m["comp"]).T
        out[s] = {"at": [round(float(x), 4), round(float(y), 4)],
                  "hits": [{**{f: chunks[i][f] for f in ("id", "doc", "title", "section", "page_from",
                                                          "page_to", "chars", "text")},
                            "score": round(float(scores[i]), 4)}
                           for i in np.argsort(-scores)[:k]]}
    return out


# ── День 22: первый RAG-запрос ───────────────────────────────────────────
#
# Вопрос → поиск чанков → объединение с вопросом → запрос к LLM. Сам запрос
# к модели делает стенд (app.py знает провайдеров и ключи), здесь — всё, что
# вокруг: какие чанки взять, как сложить промпт и как проверить ответ.

ROLE = "Ты ассистент команды, которая делает мобильные и браузерные игры. Отвечай по-русски, кратко и по делу."
RAG_RULES = (
    " Отвечай только по фрагментам документов из сообщения пользователя. После каждого "
    "утверждения ставь номер фрагмента в квадратных скобках: [1], [2]. Если во фрагментах "
    "ответа нет, так и скажи: «В документах этого нет» — и ничего не добавляй от себя.")
ANSWER_TOKENS = 500

# Контрольный набор дня 22: девять вопросов с ответом в базе и один мимо неё.
# `facts` — что обязано быть в ответе: список групп, группа засчитана, если в
# ответе есть любой её вариант. `sources` — документы, где лежит ответ.
# У вопроса вне базы фактов нет: правильный ответ — отказ.
QUESTIONS = [
    {"q": "Какой бюджет советуют закладывать на тест рекламы в Snapchat Ads?",
     "expect": "От 1000 установок на тест, бюджет не лимитируется",
     "facts": [("1000 установок", "1 000 установок", "1000 инсталл")],
     "sources": ["2024-gayd-istochniki-mobi"], "section": "Snapchat Ads"},
    {"q": "Какое удержание первого дня у казуальных игр в Европе по данным GameAnalytics за Q1 2024?",
     "expect": "31,79% у топ-25% игр — самое высокое среди жанров в Европе",
     "facts": [("31,79", "31.79")],
     "sources": ["gameanalytics-q1-2024-mob"], "section": "Europe | Retention"},
    {"q": "Сколько в среднем стоит реклама в Telegram, если заходить через посредника?",
     "expect": "В среднем €1500 без учёта комиссий и НДС (напрямую — депозит от €2 млн)",
     # Сумма — вместе с евро: без RAG модель пишет «1 500–10 000 ₽», и голое
     # число засчитало бы чужой ответ.
     "facts": [("€1500", "€ 1500", "€1 500", "1500 €", "1 500 €", "1500 евро", "1 500 евро")],
     "sources": ["2024-gayd-istochniki-mobi"], "section": "Telegram"},
    {"q": "Как в чате игры получить список только тех участников, кто сейчас онлайн?",
     "expect": "Передать isOnline: true дополнительным аргументом в метод fetchMembers",
     "facts": [("isonline",), ("fetchmembers",)],
     "sources": ["gayd-vstraivaem-chat-v-ig"], "section": "Проверка онлайна игроков"},
    {"q": "Что исполнитель по договору подряда обязан сделать до передачи результатов заказчику?",
     "expect": "Согласовать с заказчиком окончательный вид и работу продукта (п. 2.1.3)",
     "facts": [("согласова",), ("окончательный вид",)],
     "sources": ["obrazets-dogovor-podryada"], "section": "2. Права и обязанности сторон"},
    {"q": "Что входит в финансовый план игры?",
     "expect": "Бюджет и расходы, источники финансирования, риски, расчёт даты окупаемости и возврата инвестиций",
     "facts": [("бюджет",), ("источник",), ("риск",), ("окупаемост",), ("возврат",)],
     "sources": ["sozdanie-biznes-plana-igr"], "section": "Основные пункты финансового плана"},
    {"q": "Какой плагин для Unity добавляет окно с историей выделения объектов?",
     "expect": "Selection History",
     "facts": [("selection history",)],
     "sources": ["assety-po-kategoriyam"], "section": "Список"},
    {"q": "Что такое RuStore?",
     "expect": "Российский аналог Google Play для Android: скачать приложение в РФ без ограничений",
     "facts": [("google play",), ("android", "андроид")],
     "sources": ["spisok-ploschadok-dlya-re"], "section": "RuStore"},
    {"q": "На каких площадках можно выпустить браузерную игру?",
     "expect": "Яндекс Игры, CrazyGames, Одноклассники; ещё Game Distribution, Facebook Instant Games, Y8",
     "facts": [("яндекс игр", "яндекс.игр", "yandex games"), ("crazygames", "crazy games"),
               ("одноклассник", "game distribution", "gamedistribution", "instant games", "y8")],
     "sources": ["unity-conf-2024-bonus-den", "spisok-ploschadok-dlya-re"], "section": "Браузерные платформы"},
    {"q": "Сколько стоит подписка Unity Pro в 2026 году?",
     "expect": "Цен Unity в базе нет — честное «в документах этого нет», без выдуманной суммы",
     "facts": [], "sources": [], "section": "", "outside": True},
]

# Отказ ответить — по этим словам. Без RAG модель тоже может честно сказать,
# что не знает, и это засчитывается так же.
REFUSAL = ("в документах этого нет", "в документах нет", "нет в документах", "в документах не",
           "во фрагментах нет", "во фрагментах не", "нет информации", "нет данных", "не знаю",
           "не располагаю", "не могу сказать", "не могу точно", "не указан", "не содерж")

CITE = re.compile(r"\[(\d+)\]")

RUNS = """
CREATE TABLE IF NOT EXISTS rag_runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    created  TEXT NOT NULL,
    model    TEXT NOT NULL,
    strategy TEXT NOT NULL,
    k        INTEGER NOT NULL,
    results  TEXT NOT NULL
)
"""


def retrieve(q, strategy="struct", k=TOP):
    """Топ-k одной стратегии — это и есть контекст RAG-запроса."""
    return search(q, k)[strategy]["hits"]


def rag_prompt(q, hits):
    """Объединение с вопросом: пронумерованные фрагменты с метаданными, потом вопрос."""
    blocks = []
    for n, h in enumerate(hits, 1):
        page = "" if not h["page_from"] else f", стр. {h['page_from']}" + (
            f"–{h['page_to']}" if h["page_to"] != h["page_from"] else "")
        blocks.append(f"[{n}] «{h['title']}» › {h['section']}{page}\n{h['text']}")
    return "Фрагменты документов:\n\n" + "\n\n".join(blocks) + f"\n\nВопрос: {q}"


def messages(q, hits=None):
    """Сообщения модели: без RAG — только вопрос, с RAG — правила, фрагменты и вопрос."""
    if hits is None:
        return [{"role": "system", "content": ROLE}, {"role": "user", "content": q}]
    return [{"role": "system", "content": ROLE + RAG_RULES},
            {"role": "user", "content": rag_prompt(q, hits)}]


def norm(text):
    return text.lower().replace("ё", "е").replace("\xa0", " ").replace(" ", " ")


def grade(question, answer, hits=None):
    """Сверка ответа с ожиданием: факты, отказ, источники (только у RAG)."""
    low = norm(answer)
    found = [any(norm(alt) in low for alt in group) for group in question["facts"]]
    refused = any(m in low for m in REFUSAL)
    out = {"facts": sum(found), "of": len(found), "found": found, "refused": refused}
    if hits is not None:
        cited = sorted({int(n) for n in CITE.findall(answer) if 0 < int(n) <= len(hits)})
        out["cited"] = cited
        out["retrieved"] = any(h["doc"] in question["sources"] for h in hits)
        out["cited_ok"] = any(hits[n - 1]["doc"] in question["sources"] for n in cited)
    if question.get("outside"):
        ok = refused
        out["verdict"] = "ok" if ok else "bad"
    else:
        share = out["facts"] / out["of"]
        out["verdict"] = "ok" if share == 1 else "part" if share else "bad"
    return out


def question_of(q):
    """Контрольный вопрос, если реплика совпала с ним дословно, — чтобы сверить и её."""
    return next((x for x in QUESTIONS if x["q"].strip() == q.strip()), None)


def summary(results):
    """Итог прогона по режимам: факты, вердикты, отказ вне базы, источники, расход."""
    out = {}
    for mode in ("plain", "rag"):
        rows = [(QUESTIONS[r["i"]], r[mode]) for r in results if r and r.get(mode)]
        if not rows:
            continue
        inside = [a["grade"] for x, a in rows if not x.get("outside")]
        outside = [a["grade"]["refused"] for x, a in rows if x.get("outside")]
        n = len(rows)
        out[mode] = {
            "facts": sum(g["facts"] for g in inside), "of": sum(g["of"] for g in inside),
            **{v: sum(a["grade"]["verdict"] == v for _, a in rows) for v in ("ok", "part", "bad")},
            "total": n, "outside_refused": all(outside) if outside else None,
            "tokens_in": round(sum(a["metrics"]["in"] for _, a in rows) / n),
            "tokens_out": round(sum(a["metrics"]["out"] for _, a in rows) / n),
            "seconds": round(sum(a["metrics"]["seconds"] for _, a in rows) / n, 1),
            "cost": round(sum(a["metrics"]["cost"] for _, a in rows), 5)}
        if mode == "rag":
            out[mode]["retrieved"] = sum(g["retrieved"] for g in inside)
            out[mode]["cited_ok"] = sum(g["cited_ok"] for g in inside)
            out[mode]["inside"] = len(inside)
    return out


def runs_db():
    db = sqlite3.connect(store.FILE, timeout=5)
    db.execute(RUNS)
    return db


def save_run(model, strategy, k, results):
    with closing(runs_db()) as db, db:
        db.execute("INSERT INTO rag_runs (created, model, strategy, k, results) VALUES (?, ?, ?, ?, ?)",
                   (time.strftime("%Y-%m-%d %H:%M"), model, strategy, k,
                    json.dumps(results, ensure_ascii=False)))


def last_run():
    with closing(runs_db()) as db:
        row = db.execute("SELECT created, model, strategy, k, results FROM rag_runs "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return dict(zip(("created", "model", "strategy", "k"), row[:4]), results=json.loads(row[4]))


def question_set():
    """Набор для экрана: вопрос, ожидание, какие документы — с названиями."""
    with closing(connect()) as db:
        titles = dict(db.execute("SELECT id, title FROM docs"))
    return [{"q": x["q"], "expect": x["expect"], "outside": bool(x.get("outside")),
             "facts": [" / ".join(g) for g in x["facts"]], "section": x["section"],
             "sources": [{"id": s, "title": titles.get(s, s)} for s in x["sources"]]}
            for x in QUESTIONS]


# ── День 23: реранкинг, фильтр и переписывание запроса ──────────────────
#
# Поиск отдаёт топ-K₁ кандидатов, второй этап оставляет прошедших порог — не
# больше K₂. Порог — по косинусу или по оценке LLM-реранкера (0–10). Пустой
# контекст — отказ без запроса к модели. Перед поиском модель может
# переписать вопрос: разговорный («что писать в финплане?») по косинусу далёк
# от текста гайдов, и порог, верный для точных вопросов, отрезает у него всё.

CANDIDATES, KEEP = 10, 3  # K₁ — до фильтра, K₂ — после
THRESHOLD = 0.45          # порог косинуса
MIN_SCORE = 5             # порог оценки реранкера: «есть часть ответа»

# Базовый режим — день 22 как есть: исходный вопрос, топ-5, без фильтра.
MODES23 = {
    "base": {"title": "Базовый", "rewrite": False, "stage": None},
    "filter": {"title": "Фильтр по cos", "rewrite": False, "stage": "cos"},
    "rewrite": {"title": "Rewrite + фильтр", "rewrite": True, "stage": "cos"},
    "rerank": {"title": "Rewrite + реранкер", "rewrite": True, "stage": "llm"},
}

# Те же десять вопросов разговорным языком, в порядке QUESTIONS: факты и
# источники у них общие. Сленг и сокращения уводят косинус с 0,5–0,7 до
# 0,2–0,5 — ниже, чем у вопроса вне базы в точной формулировке.
TALK = [
    "сколько денег кидать на снэпчат, чтобы потестить?",
    "какой ретеншн у казуалок в европе?",
    "почём реклама в телеге, если идти через посредника?",
    "как узнать, кто сейчас онлайн в чате?",
    "что подрядчик должен сделать, прежде чем отдать работу?",
    "что писать в финплане?",
    "какой плагин в юньке показывает историю выделения?",
    "что за русский гугл плей?",
    "куда залить html5-игру?",
    # Год обязателен: в базе есть «$2000 за Pro-версию Unity» для выпуска на
    # Xbox, и без года этот ответ по документам был бы верным.
    "почём юнити про в 2026-м?",
]

REWRITE_ROLE = (
    "Ты переписываешь вопрос пользователя в поисковый запрос по базе гайдов для разработчиков "
    "мобильных и браузерных игр: реклама и трафик, аналитика рынка, договоры, бизнес-план, "
    "площадки для релиза, ассеты Unity, арт. Раскрой сленг и сокращения, назови предмет полными "
    "терминами, как их пишут в документах, добавь два-три ключевых слова. Не отвечай на вопрос и "
    "не добавляй фактов: чисел, цен, названий, которых нет в вопросе. Общих слов про игры и их "
    "разработку не добавляй — они есть в каждом документе. Верни одну строку — запрос, без "
    "кавычек и пояснений.")
RERANK_ROLE = (
    "Ты оцениваешь, насколько каждый фрагмент документа помогает ответить на вопрос. Оценка от 0 "
    "до 10: 10 — во фрагменте прямой ответ, 5–7 — есть часть ответа, 1–4 — та же тема, но ответа "
    "нет, 0 — не о том. Верни только JSON: {\"scores\": [оценка фрагмента 1, оценка фрагмента 2, "
    "…]} — столько чисел, сколько фрагментов, по порядку.")

RUNS23 = """
CREATE TABLE IF NOT EXISTS rerank_runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    created  TEXT NOT NULL,
    model    TEXT NOT NULL,
    settings TEXT NOT NULL,
    results  TEXT NOT NULL
)
"""


def question23(q):
    """Контрольный вопрос по точной или разговорной формулировке."""
    q = q.strip()
    return next((x for x, talk in zip(QUESTIONS, TALK) if q in (x["q"].strip(), talk)), None)


def rewrite_messages(q):
    return [{"role": "system", "content": REWRITE_ROLE}, {"role": "user", "content": q}]


def parse_rewrite(text):
    """Первая непустая строка ответа без кавычек и подписи «Запрос:»."""
    first = next((s for s in text.splitlines() if s.strip()), "")
    return re.sub(r"^(запрос|поисковый запрос)\s*:\s*", "", first.strip(), flags=re.I).strip(" «»\"'")


def rerank_messages(q, hits):
    """Реранкеру — исходный вопрос (что спросил человек) и кандидаты целиком."""
    blocks = [f"[{n}] «{h['title']}» › {h['section']}\n{h['text']}" for n, h in enumerate(hits, 1)]
    return [{"role": "system", "content": RERANK_ROLE},
            {"role": "user", "content": f"Вопрос: {q}\n\nФрагменты:\n\n" + "\n\n".join(blocks)}]


def parse_scores(text, n):
    """Оценки 0–10 по порядку кандидатов; не тот JSON или не то число — None."""
    found = re.search(r"\{.*\}", text, re.S)
    try:
        scores = json.loads(found.group(0))["scores"] if found else None
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(scores, list) or len(scores) != n:
        return None
    try:
        return [max(0.0, min(10.0, float(s))) for s in scores]
    except (TypeError, ValueError):
        return None


def relevant(question, hit):
    """Чанк из нужного документа, где есть хотя бы один факт ожидания."""
    if not question or question.get("outside") or hit["doc"] not in question["sources"]:
        return False
    low = norm(hit["text"])
    return any(norm(alt) in low for group in question["facts"] for alt in group)


def second_stage(hits, stage, threshold, min_score, keep):
    """Кандидаты в итоговом порядке с отметкой, кто взят в контекст и почему нет.

    `stage` None — без фильтра (взять первые `keep`), `cos` — порог косинуса,
    `llm` — сортировка по оценке реранкера (при равной — по косинусу) и её порог.
    """
    order = list(range(len(hits)))
    if stage == "llm":
        order.sort(key=lambda i: (-hits[i]["rerank"], -hits[i]["score"]))
    out, taken = [], 0
    for place, i in enumerate(order, 1):
        h = {**hits[i], "place": i + 1, "final": place}
        passed = (stage is None or (h["rerank"] >= min_score if stage == "llm"
                                    else h["score"] >= threshold))
        h["kept"] = passed and taken < keep
        h["why"] = "" if h["kept"] else "порог" if not passed else "K₂"
        taken += h["kept"]
        out.append(h)
    return out


def context_grade(question, listed):
    """Что попало в контекст: сколько чанков, сколько из них с фактом ожидания."""
    kept = [h for h in listed if h["kept"]]
    good = sum(h.get("relevant", False) for h in kept)
    return {"kept": len(kept), "relevant": good,
            "found": bool(good) if question and not question.get("outside") else None}


def summary23(results):
    """Итог прогона: на каждый набор и режим — ответы, контекст и расход."""
    out = {}
    for name in ("exact", "talk"):
        rows = [r for r in results if r and r["set"] == name]
        out[name] = {}
        for mode in MODES23:
            got = [(QUESTIONS[r["i"]], r["modes"][mode]) for r in rows if r["modes"].get(mode)]
            if not got:
                continue
            inside = [(x, a) for x, a in got if not x.get("outside")]
            outside = [a["grade"]["refused"] for x, a in got if x.get("outside")]
            kept = [a["context"]["kept"] for _, a in got]
            n = len(got)
            total = lambda a, f: a["metrics"][f] + a["extra"][f]
            out[name][mode] = {
                **{v: sum(a["grade"]["verdict"] == v for _, a in got) for v in ("ok", "part", "bad")},
                "total": n, "outside_refused": all(outside) if outside else None,
                "found": sum(bool(a["context"]["found"]) for _, a in inside), "inside": len(inside),
                "kept": round(sum(kept) / n, 1),
                "precision": round(100 * sum(a["context"]["relevant"] for _, a in inside)
                                   / max(1, sum(a["context"]["kept"] for _, a in inside))),
                "tokens_in": round(sum(a["metrics"]["in"] for _, a in got) / n),
                "tokens_all": round(sum(total(a, "in") + total(a, "out") for _, a in got) / n),
                "seconds": round(sum(total(a, "seconds") for _, a in got) / n, 1),
                "cost": round(sum(total(a, "cost") for _, a in got), 5)}
    return out


def runs23_db():
    db = sqlite3.connect(store.FILE, timeout=5)
    db.execute(RUNS23)
    return db


def save_run23(model, settings, results):
    with closing(runs23_db()) as db, db:
        db.execute("INSERT INTO rerank_runs (created, model, settings, results) VALUES (?, ?, ?, ?)",
                   (time.strftime("%Y-%m-%d %H:%M"), model, json.dumps(settings),
                    json.dumps(results, ensure_ascii=False)))


def last_run23():
    with closing(runs23_db()) as db:
        row = db.execute("SELECT created, model, settings, results FROM rerank_runs "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"created": row[0], "model": row[1], "settings": json.loads(row[2]),
            "results": json.loads(row[3])}


def sweep(strategy, k, rewrites):
    """Данные для подбора порога: у каждого вопроса — топ-k косинусов с
    отметкой «есть факт». Наборы — точный и разговорный, и, если был прогон,
    они же после rewrite (запросы из прогона). Кривые считает браузер."""
    sets = {"exact": [x["q"] for x in QUESTIONS], "talk": TALK}
    for name in ("exact", "talk"):
        got = [rewrites.get(f"{name}:{i}") for i in range(len(QUESTIONS))]
        if all(got):
            sets[name + "_rw"] = got
    flat = [(name, i, q) for name, qs in sets.items() for i, q in enumerate(qs)]
    qvecs = embed([query_input(q) for _, _, q in flat])
    chunks, vecs = loaded()["index"][strategy]
    out = {name: [] for name in sets}
    for (name, i, q), qv in zip(flat, qvecs):
        scores = vecs @ qv
        top = np.argsort(-scores)[:k]
        out[name].append({"i": i, "q": q, "outside": bool(QUESTIONS[i].get("outside")),
                          "hits": [[round(float(scores[j]), 4), relevant(QUESTIONS[i], chunks[j])]
                                   for j in top]})
    return out


def question_set23():
    """Набор дня 23: точная и разговорная формулировки рядом."""
    return [{**x, "talk": talk} for x, talk in zip(question_set(), TALK)]


# ── День 24: цитаты, источники и «не знаю» ──────────────────────────────
#
# Ответ модели — строгий JSON: статус, текст со ссылками [n], источники по
# chunk_id и дословные цитаты. Документ и раздел источника подставляет код по
# chunk_id: модель называет только id, и id не из контекста сразу виден.
# Цитата сверяется с текстом своего чанка по словам — регистр и пунктуация не
# в счёт. Смысл ответа против цитат проверяет судья — другая модель. Если
# лучший кандидат ниже порога, отвечает правило, а не модель: «не знаю», а
# уточняющий вопрос модель пишет по названиям ближайших разделов.

CITE_TOKENS, JUDGE_TOKENS = 900, 800
CLOSE = 0.85  # доля совпавших слов подряд, с которой цитата «почти дословная»

CITE_RULES = (
    " Отвечай только по фрагментам документов из сообщения пользователя. Верни только JSON:"
    " {\"status\": \"answer\" или \"unknown\", \"answer\": \"ответ\","
    " \"sources\": [{\"n\": номер фрагмента, \"chunk_id\": \"id фрагмента\"}],"
    " \"quotes\": [{\"n\": номер фрагмента, \"chunk_id\": \"id фрагмента\", \"text\": \"цитата\"}],"
    " \"clarify\": \"\"}. Правила: после каждого утверждения в answer ставь номер фрагмента [n];"
    " в sources — каждый фрагмент, на который ссылается ответ, chunk_id копируй из заголовка"
    " фрагмента; в quotes — для каждого источника хотя бы одна цитата: дословная выдержка из"
    " текста фрагмента, одно-два предложения, без пересказа, перевода, многоточий и пропусков."
    " Каждое утверждение ответа должно подтверждаться одной из цитат: чего нет в quotes, того не"
    " пиши и в answer. Если во фрагментах нет ответа или вопрос слишком общий и фрагменты отвечают на разные его"
    " варианты — status \"unknown\", answer начни с «Не знаю», sources и quotes оставь пустыми,"
    " а в clarify задай один уточняющий вопрос.")
CLARIFY_ROLE = (
    "Ты ассистент по базе гайдов для разработчиков мобильных и браузерных игр. На вопрос "
    "пользователя в базе не нашлось достаточно близких фрагментов, поэтому отвечать по существу "
    "нельзя: не называй фактов, цен и советов. Задай один короткий уточняющий вопрос, чтобы "
    "пользователь переформулировал запрос под то, что есть в базе. Ближайшие разделы базы даны "
    "только как подсказка, о чём база. Если они на ту же тему, что вопрос, — вопрос слишком общий: "
    "не говори, что данных нет, а спроси, какой из вариантов нужен, и назови два-три из них. Если "
    "вопрос не про них, скажи, что такой темы в базе нет, и предложи, о чём можно спросить. Верни "
    "только JSON: {\"clarify\": \"вопрос пользователю\"}")
JUDGE_ROLE = (
    "Ты проверяешь ответ ассистента по цитатам из документов. Разбей ответ на отдельные "
    "утверждения — проверяемые сообщения о фактах. Для каждого реши, подтверждают ли его цитаты: "
    "«да» — цитата говорит то же самое (перевод и пересказ допустимы), «частично» — подтверждена "
    "только часть, «нет» — в цитатах этого нет или сказано другое. Своими знаниями не пользуйся, "
    "только цитатами. Номера в квадратных скобках в ответе — ссылки на источники, не утверждения. "
    "Верни только JSON: {\"claims\": [{\"claim\": \"утверждение\", \"verdict\": \"да\", "
    "\"quote\": номер подтверждающей цитаты или 0, \"why\": \"коротко почему\"}]}")

# Семь вопросов дня 22 с ответом в базе и три со слабым контекстом: два вне
# базы и один слишком общий — у всех трёх лучший кандидат ниже порога, и
# правильное поведение — «не знаю» с уточняющим вопросом.
QUESTIONS24 = [QUESTIONS[i] for i in (0, 1, 2, 3, 4, 5, 8, 9)] + [
    {"q": "Какие налоги платит инди-разработчик в России?",
     "expect": "Налогов в базе нет — «не знаю» и уточняющий вопрос",
     "facts": [], "sources": [], "section": "", "outside": True},
    {"q": "Сколько стоит реклама?",
     "expect": "Вопрос слишком общий: цены есть по разным площадкам — «не знаю» и вопрос, какая площадка",
     "facts": [], "sources": [], "section": "", "outside": True},
]

RUNS24 = """
CREATE TABLE IF NOT EXISTS cite_runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    created  TEXT NOT NULL,
    model    TEXT NOT NULL,
    judge    TEXT NOT NULL,
    settings TEXT NOT NULL,
    results  TEXT NOT NULL
)
"""

NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def question24(q):
    return next((x for x in QUESTIONS24 if x["q"].strip() == q.strip()), None)


def cite_prompt(q, hits):
    """Фрагменты с chunk_id в заголовке — модель копирует его в источники и цитаты."""
    blocks = []
    for n, h in enumerate(hits, 1):
        page = "" if not h["page_from"] else f", стр. {h['page_from']}" + (
            f"–{h['page_to']}" if h["page_to"] != h["page_from"] else "")
        blocks.append(f"[{n}] chunk_id: {h['id']}\n«{h['title']}» › {h['section']}{page}\n{h['text']}")
    return "Фрагменты документов:\n\n" + "\n\n".join(blocks) + f"\n\nВопрос: {q}"


def messages24(q, hits):
    return [{"role": "system", "content": ROLE + CITE_RULES},
            {"role": "user", "content": cite_prompt(q, hits)}]


def clarify_messages(q, near):
    topics = "\n".join(f"- «{h['title']}» › {h['section']} (cos {h['score']:.2f})" for h in near)
    return [{"role": "system", "content": CLARIFY_ROLE},
            {"role": "user", "content": f"Вопрос: {q}\n\nБлижайшие разделы базы (ниже порога):\n{topics}"}]


def judge_messages(answer, quotes):
    body = "\n".join(f"[{i}] {q['text']}" for i, q in enumerate(quotes, 1))
    return [{"role": "system", "content": JUDGE_ROLE},
            {"role": "user", "content": f"Ответ:\n{answer}\n\nЦитаты:\n{body}"}]


def parse_json(text):
    """JSON ответа модели; вокруг него бывает текст или ограда ```json."""
    try:
        return json.loads(text)
    except ValueError:
        found = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(found.group(0)) if found else None
        except ValueError:
            return None


def unknown_text(best, threshold):
    best, threshold = f"{best:.3f}".replace(".", ","), f"{threshold:.2f}".replace(".", ",")
    return f"Не знаю: в базе нет фрагментов, достаточно близких к вопросу (лучший cos {best} ниже порога {threshold})."


def letters(s):
    """Только буквы и цифры в нижнем регистре, ё = е, и где каждая стояла в
    исходной строке: пробелы, дефисы и кавычки на сверку цитаты не влияют
    («flash-играми» и «flashиграми» — одно и то же)."""
    kept = [(i, c) for i, c in enumerate(s.lower().replace("ё", "е")) if c.isalnum()]
    return "".join(c for _, c in kept), [i for i, _ in kept]


def locate(quote, text):
    """Где цитата в тексте чанка. `exact` — те же буквы подряд, `close` —
    совпало не меньше CLOSE букв цитаты на отрезке не длиннее полутора цитат,
    `missing` — такого в чанке нет. start/stop — для подсветки на экране."""
    q, _ = letters(quote)
    t, at = letters(text)
    if not q or not t:
        return {"kind": "missing", "ratio": 0}
    pos = t.find(q)
    if pos >= 0:
        return {"kind": "exact", "ratio": 1, "start": at[pos], "stop": at[pos + len(q) - 1] + 1}
    blocks = [b for b in difflib.SequenceMatcher(None, q, t, autojunk=False).get_matching_blocks() if b.size >= 3]
    ratio = sum(b.size for b in blocks) / len(q)
    if blocks and ratio >= CLOSE and blocks[-1].b + blocks[-1].size - blocks[0].b <= 1.5 * len(q):
        return {"kind": "close", "ratio": round(ratio, 2), "start": at[blocks[0].b],
                "stop": at[blocks[-1].b + blocks[-1].size - 1] + 1}
    return {"kind": "missing", "ratio": round(ratio, 2)}


def numbers(text):
    """Числа от двух цифр без ссылок [n]: «1 500» и «31,79» приводятся к «1500»
    и «31.79». Одиночные цифры — обычно номера и названия («Day 1», «Y8»)."""
    text = re.sub(r"\[\d+\]", " ", text)
    text = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", text)
    return {m.replace(",", ".") for m in NUMBER.findall(text) if len(re.sub(r"\D", "", m)) >= 2}


def check24(hits, data):
    """Обязательные части ответа и их сверка с контекстом (hits — чанки, ушедшие в модель).

    Номер источника берётся по chunk_id — это номер фрагмента в промпте; на
    него и должны указывать ссылки [n] ответа."""
    if not isinstance(data, dict):
        return {"format": False}
    by_id = {h["id"]: (n, h) for n, h in enumerate(hits, 1)}
    meta = ("title", "section", "page_from", "page_to")

    def entry(item):
        cid = str(item.get("chunk_id") or "") if isinstance(item, dict) else ""
        n, h = by_id.get(cid, (None, None))
        return cid, n, h

    sources = []
    for item in data.get("sources") or []:
        cid, n, h = entry(item)
        sources.append({"n": n, "chunk_id": cid, "known": h is not None, **({k: h[k] for k in meta} if h else {})})
    quotes = []
    for item in data.get("quotes") or []:
        cid, n, h = entry(item)
        text = str(item.get("text") or "") if isinstance(item, dict) else ""
        quotes.append({"n": n, "chunk_id": cid, "text": text, "known": h is not None,
                       **(locate(text, h["text"]) if h else {"kind": "foreign", "ratio": 0})})
    answer = str(data.get("answer") or "")
    cited = sorted({int(n) for n in CITE.findall(answer)})
    quoted = {q["chunk_id"] for q in quotes}
    in_quotes = set().union(*(numbers(q["text"]) for q in quotes)) if quotes else set()
    return {"format": True, "status": data.get("status") if data.get("status") in ("answer", "unknown") else "",
            "answer": answer, "clarify": str(data.get("clarify") or "").strip(),
            "sources": sources, "quotes": quotes, "cited": cited,
            "has_sources": bool(sources) and all(s["known"] for s in sources),
            "has_quotes": bool(quotes),
            "cites_ok": bool(cited) and set(cited) <= {s["n"] for s in sources},
            "covered": bool(sources) and all(s["chunk_id"] in quoted for s in sources),
            "exact": sum(q["kind"] == "exact" for q in quotes),
            "close": sum(q["kind"] == "close" for q in quotes),
            "missing": sum(q["kind"] in ("missing", "foreign") for q in quotes),
            "numbers_missing": sorted(numbers(answer) - in_quotes)}


def judge_summary(data):
    """Вердикты судьи по утверждениям и итог: все «да» — совпадает, есть «нет» — нет."""
    claims = [c for c in (data or {}).get("claims") or [] if isinstance(c, dict)]
    if not claims:
        return None
    norm_verdict = {"да": "yes", "частично": "partial", "нет": "no"}
    out = [{"claim": str(c.get("claim") or ""), "verdict": norm_verdict.get(str(c.get("verdict")).strip().lower(), "no"),
            "quote": c.get("quote") if isinstance(c.get("quote"), int) else 0, "why": str(c.get("why") or "")}
           for c in claims]
    count = {v: sum(c["verdict"] == v for c in out) for v in ("yes", "partial", "no")}
    return {"claims": out, **count, "verdict": "bad" if count["no"] else "part" if count["partial"] else "ok"}


def verdict24(question, check, hits):
    """То ли сделал ассистент, чего ждали: ответ с фактами из ожидания или
    «не знаю» с уточнением на вопросе со слабым контекстом."""
    if not check.get("format"):
        return {"verdict": "bad", "note": "ответ не разобран как JSON"}
    if question.get("outside"):
        if check.get("status") != "unknown":
            return {"verdict": "bad", "note": "ответил, хотя должен был сказать «не знаю»"}
        return {"verdict": "ok" if check.get("clarify") else "part",
                "note": "не знаю и уточнение" if check.get("clarify") else "не знаю без уточнения"}
    if check.get("status") != "answer":
        return {"verdict": "bad", "note": "сказал «не знаю», хотя ответ в базе есть"}
    g = grade(question, check["answer"], hits)
    return {"verdict": g["verdict"], "note": f"факты {g['facts']} из {g['of']}"}


def summary24(results):
    """Итог прогона: обязательные части, дословность цитат, смысл, поведение."""
    rows = [r for r in results if r and r.get("check")]
    answers = [r for r in rows if r["check"].get("status") == "answer"]
    weak = [r for r in rows if QUESTIONS24[r["i"]].get("outside")]
    judged = [r["judge"] for r in answers if r.get("judge")]
    quotes = [q for r in answers for q in r["check"]["quotes"]]
    n = len(rows) or 1
    return {
        "total": len(rows), "answers": len(answers),
        "has_sources": sum(r["check"]["has_sources"] for r in answers),
        "has_quotes": sum(r["check"]["has_quotes"] for r in answers),
        "covered": sum(r["check"]["covered"] for r in answers),
        "cites_ok": sum(r["check"]["cites_ok"] for r in answers),
        "quotes": len(quotes), "exact": sum(q["kind"] == "exact" for q in quotes),
        "close": sum(q["kind"] == "close" for q in quotes),
        "missing": sum(q["kind"] in ("missing", "foreign") for q in quotes),
        "numbers_ok": sum(not r["check"]["numbers_missing"] for r in answers),
        "judged": len(judged), **{f"meaning_{v}": sum(j["verdict"] == v for j in judged) for v in ("ok", "part", "bad")},
        "claims": sum(len(j["claims"]) for j in judged), "claims_yes": sum(j["yes"] for j in judged),
        "weak": len(weak), "weak_unknown": sum(r["check"].get("status") == "unknown" for r in weak),
        "weak_clarify": sum(r["check"].get("status") == "unknown" and bool(r["check"].get("clarify")) for r in weak),
        **{v: sum(r["expected"]["verdict"] == v for r in rows) for v in ("ok", "part", "bad")},
        "tokens": round(sum(sum(m["in"] + m["out"] for m in r["metrics"].values()) for r in rows) / n),
        "seconds": round(sum(sum(m["seconds"] for m in r["metrics"].values()) for r in rows) / n, 1),
        "cost": round(sum(sum(m["cost"] for m in r["metrics"].values()) for r in rows), 5)}


def runs24_db():
    db = sqlite3.connect(store.FILE, timeout=5)
    db.execute(RUNS24)
    return db


def save_run24(model, judge, settings, results):
    with closing(runs24_db()) as db, db:
        db.execute("INSERT INTO cite_runs (created, model, judge, settings, results) VALUES (?, ?, ?, ?, ?)",
                   (time.strftime("%Y-%m-%d %H:%M"), model, judge, json.dumps(settings),
                    json.dumps(results, ensure_ascii=False)))


def last_run24():
    with closing(runs24_db()) as db:
        row = db.execute("SELECT created, model, judge, settings, results FROM cite_runs "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"created": row[0], "model": row[1], "judge": row[2], "settings": json.loads(row[3]),
            "results": json.loads(row[4])}


def question_set24():
    with closing(connect()) as db:
        titles = dict(db.execute("SELECT id, title FROM docs"))
    return [{"q": x["q"], "expect": x["expect"], "outside": bool(x.get("outside")), "section": x["section"],
             "sources": [{"id": s, "title": titles.get(s, s)} for s in x["sources"]]} for x in QUESTIONS24]


# ── День 25: мини-чат с RAG и памятью задачи ────────────────────────────
#
# Ход чата: планировщик одним JSON-запросом обновляет память задачи и пишет
# самостоятельные поисковые запросы, по одному на тему сообщения → поиск с
# порогом → ответ потоком со ссылками [n], источники к ним подставляет код.
# История в запросе — только окно последних WINDOW реплик; цель, уточнения,
# ограничения, термины и выводы едут в каждый запрос карточкой памяти, так что
# из окна выпадают реплики, а не задача. Сообщению без вопроса (итог,
# договорённость) в контекст добавляются фрагменты, на которые уже ссылались
# прошлые ответы диалога.

WINDOW, PLAN_WINDOW = 6, 4  # реплик дословно: в запросе ответа и планировщика
CHAT_CONTEXT, DIALOG_CONTEXT = 5, 8  # чанков в контексте: по запросам и вместе с источниками диалога
PLAN_TOKENS = 700
# «Решено и выяснено» — сверх трёх полей задания: без него итог теряет выводы,
# которые выпали из окна истории (первый прогон сценария 2 потерял площадку и
# всё про договор). Его пишет планировщик по прошлому ответу ассистента.
STATE_KEYS = ("goal", "clarified", "constraints", "terms", "decisions")
STATE_TITLES = {"goal": "Цель", "clarified": "Уточнено", "constraints": "Ограничения", "terms": "Термины",
                "decisions": "Решено и выяснено"}
MAX_ITEMS, MAX_ITEM = 10, 200

PLAN_ROLE = (
    "Ты ведёшь память задачи в диалоге пользователя с ассистентом по базе гайдов для разработчиков "
    "мобильных и браузерных игр. На входе — память задачи, последние реплики и новое сообщение "
    "пользователя. Верни только JSON: {\"state\": {\"goal\": \"цель диалога одной фразой\", "
    "\"clarified\": [\"что пользователь уточнил о себе, продукте и задаче\"], "
    "\"constraints\": [\"ограничения: бюджет, платформа, рынок, сроки, состав команды\"], "
    "\"terms\": [\"термин — что он значит в этом диалоге\"], "
    "\"decisions\": [\"что выяснили и решили по ходу диалога\"]}, \"queries\": [\"поисковый запрос\"]}. "
    "Память: возвращай карточку целиком; чего сообщение не отменило — перенеси дословно; что "
    "изменило — замени, старое значение не оставляй. Цель меняй, только если пользователь явно "
    "сменил задачу: уточнения и вопросы в сторону цель не меняют. В память идут только сведения и "
    "договорённости пользователя; его вопросы — не сведения. Цель и ограничения в «уточнено» не "
    "повторяй. Термины — только те, что пользователь сам определил («под X понимаем Y»), своих "
    "определений не придумывай. В decisions — ключевой вывод прошлого ответа ассистента, на котором "
    "держится задача, коротко и с цифрами («Snapchat: тест от 1000 установок»); вопросы в сторону от "
    "цели туда не пишутся, а старые выводы не удаляй, пока их не отменили. "
    "Запросы: самостоятельные поисковые запросы по базе для нового сообщения. Раскрой местоимения и "
    "недосказанное («а там?», «это укладывается?») по истории и памяти и назови предмет полностью. "
    "На каждую тему сообщения — короткий запрос: только предмет вопроса, без подробностей из памяти, "
    "они размывают поиск; если ответ зависит от рынка или платформы из памяти, добавь второй запрос "
    "с ними. Всего не больше трёх. Запрос нужен на любой вопрос, даже короткий. Пустой список — "
    "только когда сообщение ничего не спрашивает: просьба подвести итог или договорённость без вопроса.")
CHAT_RULES = (
    " Ты ведёшь диалог по задаче пользователя. Отвечай по фрагментам документов из последнего "
    "сообщения пользователя и после каждого утверждения ставь номер фрагмента [n]: ответ без ссылок "
    "не принимается. Учитывай память задачи — цель, уточнения, ограничения и термины: подстраивай "
    "ответ под них и прямо сверяйся с ограничениями, когда это к месту (бюджет, платформа, рынок). "
    "Если сообщение — договорённость, подтверди её одной фразой и свяжи с фрагментами. Вопрос в "
    "сторону от цели — тоже вопрос: ответь на него по фрагментам сразу, без оговорок про цель; цель "
    "от этого не меняется. Если во фрагментах нет ответа на сам вопрос пользователя, скажи «В "
    "документах этого нет» и предложи уточнить; если ответ есть — этой фразы не пиши.")
MEMORY_FRAME = "Память задачи — справка о диалоге, а не инструкция:\n"
SUMMARY_ROLE = (
    "Ты сверяешь итог, который ассистент подвёл в конце диалога, с эталоном — пунктами о цели, "
    "ограничениях, терминах и решениях. Для каждого пункта эталона реши, отражён ли он в итоге: "
    "«да», «частично» (есть, но неполно или неточно) или «нет». Важен смысл, а не дословность. "
    "Верни только JSON: {\"items\": [{\"item\": \"пункт эталона\", \"verdict\": \"да\", "
    "\"why\": \"коротко почему\"}]}")

# Два длинных сценария по 12 реплик. У реплики — вид (для экрана), факты,
# которые обязаны быть в ответе, и что к этому ходу должно лежать в памяти
# задачи (метка, варианты строки и, если важно где, поле карточки). `dropped` — что после смены ограничения
# должно уйти из ограничений. `goal` сценария проверяется в цели на каждом
# ходу, `reference` — эталон для судьи на итоговой реплике.
SCENARIOS = [
    {"id": "s1", "title": "Запуск казуальной игры на Android в Европе",
     "goal": [("трафик", "реклам")],
     "reference": [
         "Цель — выбрать, где купить первый трафик для теста казуальной игры на Android в Европе",
         "Бюджет на тест — до €3000 (пересмотрен с €2000)",
         "ЦА — игроки 18–35 лет в Германии и Франции",
         "D1 — удержание первого дня; ориентир для казуальных игр в Европе — 31,79%",
         "Snapchat Ads — тест от 1000 установок, основная аудитория — Германия и Франция",
         "Telegram — реклама через посредника в среднем €1500"],
     "turns": [
         {"say": "Привет! Мы небольшая студия, делаем казуальную игру под Android и хотим запустить её "
                 "в Европе. Цель — выбрать, где купить первый трафик для теста. Бюджет на тест — до €2000. "
                 "С каких источников обычно начинают такой тест?",
          "kind": "цель", "memory": [("Android", ("android", "андроид")), ("Европа", ("европ",)),
                                     ("€2000", ("2000", "2 000"), "constraints")]},
         {"say": "Что советуют по Snapchat Ads — с какого объёма начинать тест?",
          "kind": "вопрос", "facts": [("1000", "1 000")]},
         {"say": "А какая там основная аудитория?",
          "kind": "эллипсис", "facts": [("герман",), ("франц",)]},
         {"say": "Договоримся: под ЦА я имею в виду игроков 18–35 лет в Германии и Франции.",
          "kind": "термин", "memory": [("ЦА", ("ца ", "ца:", "ца —", "ца -", "(ца)", "целев")),
                                       ("18–35", ("18–35", "18-35", "18 – 35", "18 - 35", "от 18 до 35"))]},
         {"say": "А Telegram нам подойдёт? Сколько стоит реклама там через посредника?",
          "kind": "вопрос", "facts": [("1500", "1 500")]},
         {"say": "Это укладывается в наш бюджет?",
          "kind": "эллипсис", "facts": [("2000", "2 000")]},
         {"say": "Кстати, не по теме: мы встраиваем в игру свой внутриигровой чат — как показать в нём "
                 "только тех игроков, кто сейчас онлайн?",
          "kind": "в сторону", "facts": [("isonline",)]},
         {"say": "Окей, вернёмся к запуску. Какое удержание первого дня у казуальных игр в Европе "
                 "считается хорошим?",
          "kind": "возврат", "facts": [("31,79", "31.79")]},
         {"say": "Дальше под D1 понимаем удержание первого дня. И бюджет пересмотрели: теперь до €3000.",
          "kind": "смена", "memory": [("D1", ("d1",)), ("€3000", ("3000", "3 000"), "constraints")],
          "dropped": ["2000", "2 000"]},
         {"say": "Как в игре выделить цветом разные команды или противников?",
          "kind": "вопрос", "facts": [("контраст",)]},
         {"say": "С новым бюджетом мы потянем тест и в Snapchat, и в Telegram?",
          "kind": "эллипсис", "facts": [("3000", "3 000")]},
         {"say": "Подведи итог: какая у нас цель, что мы уточнили и что решили по трафику?",
          "kind": "итог"},
     ]},
    {"id": "s2", "title": "Браузерная игра и художник по договору",
     "goal": [("браузер",), ("договор", "подряд", "художник")],
     "reference": [
         "Цель — выпустить браузерную игру на площадке и нанять художника по договору подряда",
         "Команда — два человека, игроки — из России",
         "Только браузерная версия: мобильные сторы и RuStore не нужны",
         "Площадка — Яндекс Игры как самая популярная в России; есть и CrazyGames",
         "По договору подряда исключительные права на результат переходят заказчику",
         "До передачи исполнитель согласует с заказчиком окончательный вид и работу продукта",
         "«Результат» — готовые спрайты и анимации",
         "Бюджет — 300 тыс. ₽ на полгода, на художника — не больше 100 тыс. ₽"],
     "turns": [
         {"say": "Привет! Нас двое, делаем браузерную игру для игроков из России. Цель — выпустить её "
                 "на браузерной площадке и нанять художника по договору подряда.",
          "kind": "цель", "memory": [("двое", ("двое", "2 человек", "два человек", "двух")), ("Россия", ("росси",))]},
         {"say": "На каких площадках можно выпустить браузерную игру?",
          "kind": "вопрос", "facts": [("яндекс",), ("crazygames", "crazy games")]},
         {"say": "А какая из них самая популярная у нас?",
          "kind": "эллипсис", "facts": [("яндекс",)]},
         {"say": "Важно: делаем только браузерную версию, мобильные сторы не рассматриваем. "
                 "Нужен ли нам тогда RuStore?",
          "kind": "ограничение", "facts": [("android", "андроид")],
          "memory": [("только браузер", ("только браузер", "мобильн"), "constraints")]},
         {"say": "Теперь про художника. Кому по договору подряда переходят исключительные права на результат?",
          "kind": "вопрос", "facts": [("заказчик",)]},
         {"say": "А что исполнитель обязан сделать до передачи результата?",
          "kind": "эллипсис", "facts": [("согласова",)]},
         {"say": "Зафиксируем термин: «результат» — это готовые спрайты и анимации для игры.",
          "kind": "термин", "memory": [("результат", ("спрайт",))]},
         {"say": "Кстати, не по теме: как собрать ключевые фразы для ASO через ChatGPT?",
          "kind": "в сторону"},
         {"say": "Вернёмся к делу. Нам нужен финплан для инвестора — что в него входит?",
          "kind": "возврат", "facts": [("бюджет",), ("риск",), ("окупаем",)]},
         {"say": "Бюджет у нас 300 тысяч рублей на полгода, на художника — не больше 100 тысяч.",
          "kind": "ограничение", "memory": [("300 тыс.", ("300",), "constraints"),
                                            ("100 тыс.", ("100",), "constraints")]},
         {"say": "Мы хотим добавить в игру чат. Как это сделать на Construct 3?",
          "kind": "вопрос", "facts": [("gamepush",)]},
         {"say": "Подведи итог: какая у нас цель, какие ограничения и термины, что решили по площадке "
                 "и по договору?",
          "kind": "итог"},
     ]},
]
SCENARIO_BY_ID = {s["id"]: s for s in SCENARIOS}

CHATS = """
CREATE TABLE IF NOT EXISTS rag_chats (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    scenario TEXT NOT NULL DEFAULT '',
    model    TEXT NOT NULL,
    created  TEXT NOT NULL,
    updated  TEXT NOT NULL,
    state    TEXT NOT NULL,
    turns    TEXT NOT NULL
)
"""


def empty_state():
    return {"goal": "", **{key: [] for key in STATE_KEYS[1:]}}


def clean_state(data, old):
    """Карточка планировщика по схеме; поля, которых он не вернул, — из старой."""
    if not isinstance(data, dict):
        return old
    out = {"goal": str(data.get("goal") or old["goal"]).strip()[:MAX_ITEM]}
    for key in STATE_KEYS[1:]:
        items = data.get(key) if isinstance(data.get(key), list) else old[key]
        out[key] = [str(x).strip()[:MAX_ITEM] for x in items if str(x).strip()][:MAX_ITEMS]
    return out


def state_text(state):
    lines = [f"{STATE_TITLES['goal']}: {state['goal'] or '—'}"]
    lines += [f"{STATE_TITLES[k]}: " + ("; ".join(state[k]) if state[k] else "—") for k in STATE_KEYS[1:]]
    return "\n".join(lines)


def state_changes(old, new):
    """Что ход поменял в памяти: цель и пункты списков — добавленные и убранные."""
    out = []
    if old["goal"] != new["goal"]:
        out.append({"key": "goal", "add": new["goal"], "drop": old["goal"]})
    for key in STATE_KEYS[1:]:
        out += [{"key": key, "add": x} for x in new[key] if x not in old[key]]
        out += [{"key": key, "drop": x} for x in old[key] if x not in new[key]]
    return out


def history(turns, size):
    """Последние `size` реплик дословно — окно короткой памяти."""
    talk = []
    for t in turns:
        talk += [{"role": "user", "content": t["user"]}, {"role": "assistant", "content": t["answer"]}]
    return talk[-size:]


def plan_messages(state, turns, text):
    talk = "\n".join(f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['content'][:600]}"
                     for m in history(turns, PLAN_WINDOW))
    return [{"role": "system", "content": PLAN_ROLE},
            {"role": "user", "content": f"Память задачи:\n{json.dumps(state, ensure_ascii=False)}\n\n"
                                        f"Последние реплики:\n{talk or '—'}\n\nНовое сообщение: {text}"}]


def chat_messages(state, turns, text, context):
    """Запрос ответа: правила и память задачи, окно истории, сообщение и фрагменты.

    Сообщение — перед фрагментами, а не после, как в дне 22: в конце, за
    длинными фрагментами, модель читала вопрос в сторону как продолжение
    прошлой темы (на ходу про чат после Telegram — отказ 3 раза из 3, с
    сообщением впереди — ответ 3 из 3)."""
    user = text
    if context:
        fragments = rag_prompt(text, context).rsplit("\n\nВопрос: ", 1)[0]
        user = f"Сообщение: {text}\n\n{fragments}\n\nОтветь на сообщение выше по этим фрагментам."
    return ([{"role": "system", "content": ROLE + CHAT_RULES + "\n\n" + MEMORY_FRAME + state_text(state)}]
            + history(turns, WINDOW) + [{"role": "user", "content": user}])


def chat_context(queries, text, turns):
    """Контекст хода. По каждому запросу планировщика — топ-K₁ и порог, в
    контекст идут лучшие прошедшие без повторов; не прошёл никто — «не знаю».
    Без запросов поиск всё равно идёт — по самой реплике (планировщик бывает
    уверен, что вопроса нет, и ошибается), а к найденному добавляются
    фрагменты, на которые ссылался диалог; «не знаю» в этой ветке нет."""
    kept, best, near = {}, 0.0, []
    for q in queries or [text]:
        hits = retrieve(q, "struct", CANDIDATES)
        best, near = max(best, hits[0]["score"] if hits else 0.0), near or hits[:5]
        for h in second_stage(hits, "cos", THRESHOLD, 0, KEEP):
            if h["kept"] and h["score"] > kept.get(h["id"], {"score": -1})["score"]:
                kept[h["id"]] = h
    context = sorted(kept.values(), key=lambda h: -h["score"])[:CHAT_CONTEXT]
    if queries:
        return context, best, near, not context
    return (context + [c for c in dialog_sources(turns) if c["id"] not in kept])[:DIALOG_CONTEXT], best, near, False


def chunks_by_id(ids):
    chunks, _ = loaded()["index"]["struct"]
    by_id = {c["id"]: c for c in chunks}
    return [{k: by_id[i][k] for k in ("id", "doc", "title", "section", "page_from", "page_to", "chars", "text")}
            for i in ids if i in by_id]


def dialog_sources(turns, limit=8):
    """Чанки, на которые ссылались прошлые ответы, — свежие первыми, без повторов."""
    seen = []
    for t in reversed(turns):
        seen += [s["chunk_id"] for s in t.get("sources", []) if s["chunk_id"] not in seen]
    return chunks_by_id(seen[:limit])


def cited_sources(answer, context):
    """Источники ответа — фрагменты, на которые он сослался [n]; номера мимо контекста — отдельно."""
    nums = sorted({int(n) for n in CITE.findall(answer)})
    good = [n for n in nums if 0 < n <= len(context)]
    return ([{"n": n, "chunk_id": context[n - 1]["id"],
              **{k: context[n - 1][k] for k in ("title", "section", "page_from", "page_to")}} for n in good],
            [n for n in nums if n not in good])


def found(text, groups):
    low = norm(text)
    return [any(norm(alt) in low for alt in group) for group in groups]


def turn_check(scenario, spec, state, answer, sources, bad_refs, gate):
    """Проверки хода сценария: источники, цель в памяти, факты ответа, что записано и что убрано."""
    out = {"sources": bool(sources) and not bad_refs, "gate": gate,
           "goal": all(found(state["goal"], scenario["goal"]))}
    if spec.get("facts"):
        # Факт внутри отказа («в документах этого нет, там только isOnline») не засчитывается.
        hits = found(answer, spec["facts"])
        out["refused"] = any(m in norm(answer) for m in REFUSAL)
        out["facts"], out["of"] = 0 if out["refused"] else sum(hits), len(hits)
    if spec.get("memory"):
        card = state_text(state)
        out["memory"] = [{"label": label, "ok": found("; ".join(state[field[0]]) if field else card, [alts])[0]}
                         for label, alts, *field in spec["memory"]]
    if spec.get("dropped"):
        out["dropped"] = not any(found("; ".join(state["constraints"]), [tuple(spec["dropped"])]))
    return out


def summary_messages(answer, reference):
    items = "\n".join(f"- {x}" for x in reference)
    return [{"role": "system", "content": SUMMARY_ROLE},
            {"role": "user", "content": f"Итог ассистента:\n{answer}\n\nЭталон:\n{items}"}]


def summary_verdict(data):
    """Вердикты по пунктам эталона: без «нет» — цель не потеряна, одно-два «нет» — частично."""
    items = [x for x in (data or {}).get("items") or [] if isinstance(x, dict)]
    if not items:
        return None
    word = {"да": "yes", "частично": "partial", "нет": "no"}
    out = [{"item": str(x.get("item") or ""), "verdict": word.get(str(x.get("verdict")).strip().lower(), "no"),
            "why": str(x.get("why") or "")} for x in items]
    count = {v: sum(x["verdict"] == v for x in out) for v in ("yes", "partial", "no")}
    return {"items": out, **count, "verdict": "ok" if not count["no"] else "part" if count["no"] <= 2 else "bad"}


def chat_score(chat):
    """Счёт сценарного чата по ходам: источники, цель в памяти, факты, память, итог."""
    checks = [t["check"] for t in chat["turns"] if t.get("check")]
    memory = [m for c in checks for m in c.get("memory", [])]
    judged = next((t["judge"] for t in reversed(chat["turns"]) if t.get("judge")), None)
    return {"turns": len(chat["turns"]), "checked": len(checks),
            "sources": sum(c["sources"] for c in checks), "goal": sum(c["goal"] for c in checks),
            "facts": sum(c.get("facts", 0) for c in checks), "of": sum(c.get("of", 0) for c in checks),
            "memory": sum(m["ok"] for m in memory), "memory_of": len(memory),
            "dropped": [c["dropped"] for c in checks if "dropped" in c],
            "judge": {k: judged[k] for k in ("verdict", "yes", "partial", "no")} if judged else None}


def chats_db():
    db = sqlite3.connect(store.FILE, timeout=5)
    db.execute(CHATS)
    return db


def new_chat(scenario, model):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    title = SCENARIO_BY_ID[scenario]["title"] if scenario in SCENARIO_BY_ID else "Новый чат"
    chat = {"id": hashlib.sha1(f"{now}{time.monotonic()}".encode()).hexdigest()[:10], "title": title,
            "scenario": scenario if scenario in SCENARIO_BY_ID else "", "model": model,
            "created": now, "updated": now, "state": empty_state(), "turns": []}
    save_chat(chat)
    return chat


def save_chat(chat):
    with closing(chats_db()) as db, db:
        db.execute("INSERT OR REPLACE INTO rag_chats VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (chat["id"], chat["title"], chat["scenario"], chat["model"], chat["created"], chat["updated"],
                    json.dumps(chat["state"], ensure_ascii=False), json.dumps(chat["turns"], ensure_ascii=False)))


def load_chat(chat_id):
    with closing(chats_db()) as db:
        row = db.execute("SELECT id, title, scenario, model, created, updated, state, turns FROM rag_chats "
                         "WHERE id = ?", (chat_id,)).fetchone()
    if not row:
        return None
    chat = dict(zip(("id", "title", "scenario", "model", "created", "updated"), row[:6]))
    chat.update(state={**empty_state(), **json.loads(row[6])}, turns=json.loads(row[7]))
    return chat


def list_chats():
    with closing(chats_db()) as db:
        ids = [r[0] for r in db.execute("SELECT id FROM rag_chats ORDER BY updated DESC LIMIT 30")]
    out = []
    for chat in map(load_chat, ids):
        out.append({k: chat[k] for k in ("id", "title", "scenario", "updated")}
                   | {"count": len(chat["turns"]), "score": chat_score(chat) if chat["scenario"] else None})
    return out


def scenario_set():
    return [{"id": s["id"], "title": s["title"], "reference": s["reference"],
             "turns": [{"say": t["say"], "kind": t["kind"]} for t in s["turns"]]} for s in SCENARIOS]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for e in build(fresh="--fresh" in sys.argv):
        if e["t"] == "stage":
            print("→", e["text"])
        elif e["t"] == "doc":
            print(f"   {e['kind']:4} {e['pages']:>3} стр. {e['chars']:>7} симв.  {e['title'][:60]}"
                  + (f"  — пропущен: {e['skipped']}" if e["skipped"] else ""))
        elif e["t"] == "chunked":
            print("  ", ", ".join(f"{STRATEGIES[s]}: {n}" for s, n in e["counts"].items()))
        elif e["t"] == "embed" and e["done"] == e["total"]:
            print(f"   {STRATEGIES[e['strategy']]}: {e['total']} векторов, из кеша {e['cached']}")
        elif e["t"] == "error":
            sys.exit(e["text"])
        elif e["t"] == "done":
            print(f"\nГотово за {e['seconds']} с, {FILE.name} — {e['bytes'] // 1024} КБ\n")
            rows = [("чанков", "chunks"), ("символов в эмбеддинги", "chars"), ("средний размер", "avg"),
                    ("медиана", "median"), ("мин / макс", None), ("обрыв фразы, %", "cut"),
                    ("захват 2+ разделов, %", "crosses"), ("крошки < 200, %", "tiny"),
                    ("эмбеддинги, с", "seconds"), ("из них из кеша", "cached")]
            print(f"{'':24}" + "".join(f"{STRATEGIES[s]:>16}" for s in STRATEGIES))
            for label, key in rows:
                vals = [f"{e['stats'][s]['min']} / {e['stats'][s]['max']}" if key is None
                        else str(e["stats"][s][key]) for s in STRATEGIES]
                print(f"{label:24}" + "".join(f"{v:>16}" for v in vals))
            for label, key in (("hit@1", "hit1"), ("hit@3", "hit3"), ("MRR@5", "mrr")):
                print(f"{label:24}" + "".join(
                    f"{str(e['checks'][s][key]) + ('/' + str(e['checks'][s]['total']) if key != 'mrr' else ''):>16}"
                    for s in STRATEGIES))
