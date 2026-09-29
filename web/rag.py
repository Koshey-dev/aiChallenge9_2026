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
"""

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
