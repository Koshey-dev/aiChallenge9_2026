"""День 19: композиция инструментов — конвейер search → summarize → save_to_file.

Три инструмента, с дня 20 — на своём MCP-сервере «журнал» (`SERVER`, до
того они жили на общем с трекером и планировщиком). Первый находит
разделы в журнале стенда (README), второй сжимает найденное моделью в
несколько пунктов, третий кладёт конспект файлом на диск. Цепочку ведёт
модель: на одну реплику она сама зовёт их по очереди.

Данные между шагами идут ссылкой, а не текстом. Каждый результат сервер
кладёт в таблицу `blobs` и отдаёт модели его номер (`ref`) и отпечаток
(`sha`); следующему инструменту модель передаёт только номер. Так текст не
проходит через модель по дороге — она не может его обрезать или
пересказать своими словами, — а передачу видно механически: инструмент
возвращает, что получил (`got`: номер и отпечаток прочитанного), и этот
отпечаток обязан совпасть с тем, что выдал прошлый шаг. У файла отпечаток
снимается с диска, после записи.

Результаты принадлежат чату, как задания планировщика: номер из другого чата
этот чат не видит. Текст конспекта пишет модель чата — функцию для этого
стенд кладёт в `WRITE`, сам модуль про провайдеров не знает.
"""

import hashlib
import json
import math
import re
from contextlib import closing
from pathlib import Path

import tracker

HERE = Path(__file__).parent
SOURCE = HERE.parent / "README.md"
FILES = HERE / "files"

# Функция стенда: (чат, роль, текст) → (ответ модели, цена) или None без ключа.
WRITE = None

TABLE = """
CREATE TABLE IF NOT EXISTS blobs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat    TEXT NOT NULL,
    tool    TEXT NOT NULL,
    source  INTEGER NOT NULL DEFAULT 0,
    got     TEXT NOT NULL DEFAULT '',
    text    TEXT NOT NULL DEFAULT '',
    sha     TEXT NOT NULL,
    meta    TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL
)
"""

FIELDS = ("id", "chat", "tool", "source", "got", "text", "sha", "meta", "created")

# Сколько текста поиск отдаёт дальше: раздел целиком бывает на сотню строк.
SECTION_MAX = 5000
FOUND_MAX = 16000

# Слова, по которым искать бессмысленно: они есть почти в каждом разделе.
STOP = {"что", "как", "это", "про", "для", "все", "всё", "или", "его", "она", "они",
        "так", "там", "где", "когда", "был", "была", "было", "уже", "ещё", "еще",
        "нет", "при", "без", "над", "под", "чем", "том", "тот", "эта", "эти"}

# Имя файла — одно слово: буквы, цифры, дефис, подчёркивание. Ни точек, ни
# разделителей — пути вне папки конвейера не собрать.
NAME = re.compile(r"[\w-]{1,60}")

# Маркер пункта в ответе модели: «- », «• », «* » или «1. ».
BULLET = re.compile(r"(?:[-•*]|\d+[.)])\s+")


def connect():
    db = tracker.connect()
    db.execute(TABLE)
    return db


def digest(text):
    """Отпечаток текста — первые 12 знаков sha256: для глаза хватает."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def blob_row(row):
    item = dict(zip(FIELDS, row))
    return {**item, "meta": json.loads(item["meta"])}


def put(chat, tool, text, *, source=None, sha=None, meta=None):
    """Результат шага — в таблицу. `got` — отпечаток того, что шаг прочитал."""
    with closing(connect()) as db, db:
        cursor = db.execute(
            "INSERT INTO blobs (chat, tool, source, got, text, sha, meta, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (chat, tool, source["id"] if source else 0, digest(source["text"]) if source else "",
             text, sha or digest(text), json.dumps(meta or {}, ensure_ascii=False)))
    return blob(cursor.lastrowid)


def blob(ref):
    with closing(connect()) as db:
        row = db.execute(f"SELECT {', '.join(FIELDS)} FROM blobs WHERE id = ?",
                         (ref,)).fetchone()
    return blob_row(row) if row else None


def taken(ref, chat):
    """Результат прошлого шага по номеру — только своего чата и только с текстом."""
    found = blob(ref)
    if found is None or found["chat"] != chat:
        raise tracker.Failed(f"результата #{ref} в этом чате нет — номер (ref) берите "
                             "из ответа прошлого шага")
    if not found["text"]:
        raise tracker.Failed(f"#{ref} — это запись файла, у неё нет текста; передайте "
                             "ref результата search или summarize")
    return found


def got(source):
    """Что шаг получил на вход — модели и полосе цепочки для сверки стыка."""
    return {"ref": source["id"], "sha": digest(source["text"])}


# ── Поиск по журналу ────────────────────────────────────────────────

def sections():
    """Журнал, порезанный по заголовкам второго и третьего уровня. Заголовок
    подраздела несёт и заголовок раздела: «Проверено» есть у каждого дня."""
    found, top, fenced = [], "", False
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    for number, text in enumerate(lines, 1):
        if text.startswith("```"):
            fenced = not fenced
        heading = not fenced and re.match(r"(#{2,3}) (.+)", text)
        if heading:
            title = heading.group(2).strip()
            if len(heading.group(1)) == 2:
                top = title
            else:
                title = f"{top} › {title}"
            found.append({"title": title, "first": number, "last": number, "body": []})
        elif found:
            found[-1]["body"].append(text)
            found[-1]["last"] = number
    for part in found:
        part["body"] = "\n".join(part["body"]).strip()
    return found


def stems(text):
    """Основы слов: грубо, по первым пяти буквам — «ворота», «воротами» и
    «ворот» сходятся, морфологии для этого не нужно."""
    return [word[:5] for word in re.findall(r"[a-zа-яё0-9]+", text.lower())
            if len(word) >= 3 and word not in STOP]


def score(terms, part):
    words = stems(part["body"])
    title = set(stems(part["title"]))
    total = 0.0
    for term in terms:
        hits = words.count(term)
        total += (1 + math.log(hits) if hits else 0) + (3 if term in title else 0)
    return total


def search(args, chat):
    query = " ".join(args["query"].split())[:200]
    terms = list(dict.fromkeys(stems(query)))
    if not terms:
        raise tracker.Failed("в запросе нет слов, по которым искать")
    limit = min(args.get("limit", 4), 8)
    ranked = sorted(((score(terms, part), part) for part in sections()),
                    key=lambda pair: -pair[0])
    hits = [(points, part) for points, part in ranked if points > 0][:limit]
    if not hits:
        raise tracker.Failed(f"в журнале нет ничего по запросу «{query}» — "
                             "попробуйте другие слова")
    pieces, room = [], FOUND_MAX
    for _, part in hits:
        body = part["body"][:min(SECTION_MAX, room)]
        pieces.append(f"## {part['title']}\n(README.md, строки {part['first']}–{part['last']})"
                      f"\n\n{body}")
        room -= len(body)
        if room <= 0:
            break
    text = "\n\n".join(pieces) + "\n"
    found = [{"title": part["title"], "lines": f"{part['first']}–{part['last']}",
              "score": round(points, 1), "preview": " ".join(part["body"].split())[:160]}
             for points, part in hits[:len(pieces)]]
    saved = put(chat, "search", text, meta={"query": query, "sections": len(found),
                                             "chars": len(text)})
    return {"ref": saved["id"], "sha": saved["sha"], "query": query,
            "found": len(found), "chars": len(text), "sections": found}


# ── Конспект ────────────────────────────────────────────────────────

ROLE = (
    "Тебе дают фрагменты журнала учебного стенда AI Challenge. Сожми их в список "
    "по-русски, число пунктов: {points}. Только то, что есть во фрагментах, без "
    "советов и без выдумок. Пункты распредели по всем фрагментам, а не только по "
    "первому. Каждый пункт — одна-две фразы и в конце в скобках короткое название "
    "раздела, из которого он взят: часть заголовка после «›», если она есть. Верни "
    "только список: каждый пункт с новой строки, начиная с «- »."
)


async def summarize(args, chat):
    source = taken(args["source"], chat)
    points = min(args.get("points", 5), 10)
    # Число пунктов — ещё раз в конце: после длинных фрагментов модель теряет его
    # из роли. И всё равно это заявка — сколько пунктов останется, решает код.
    ask = f"{source['text']}\n---\nЧисло пунктов: {points}."
    written = await WRITE(chat, ROLE.format(points=points), ask) if WRITE else None
    if written is None:
        raise tracker.Failed("у модели чата нет ключа — конспект писать некому")
    said, cost = written
    # Пункт — строка с маркером; строка без него продолжает прошлый пункт, а
    # до первого пункта — это вступление модели, и оно отбрасывается.
    lines = list(filter(None, map(str.strip, said.splitlines())))
    marked = [bool(BULLET.match(line)) for line in lines]
    items = []
    for line, mark in zip(lines, marked):
        if mark or not any(marked):
            items.append(BULLET.sub("", line, count=1))
        elif items:
            items[-1] += " " + line
    items = [f"- {item}" for item in items[:points]]
    if not items:
        raise tracker.Failed("модель вернула пустой конспект")
    # Шапку и источник пишет код: откуда взят текст, модель не пересказывает.
    about = source["meta"].get("query", "")
    head = f"# Конспект: {about}" if about else "# Конспект"
    tail = f"Источник: журнал стенда (README.md), результат #{source['id']}"
    if about:
        tail += f", поиск «{about}»"
    text = f"{head}\n\n" + "\n".join(items) + f"\n\n{tail}.\n"
    saved = put(chat, "summarize", text, source=source,
                meta={"points": len(items), "chars": len(text), "cost": cost})
    return {"ref": saved["id"], "sha": saved["sha"], "got": got(source),
            "points": len(items), "chars": len(text), "cost": cost, "text": text}


# ── Файл ────────────────────────────────────────────────────────────

def save_to_file(args, chat):
    source = taken(args["source"], chat)
    name = args["name"].strip()
    name = re.sub(r"\.(md|txt)$", "", name, flags=re.IGNORECASE)
    if not NAME.fullmatch(name):
        raise tracker.Failed("имя файла — одно слово из букв, цифр, «-» и «_», без точек "
                             "и путей, например vorota")
    FILES.mkdir(exist_ok=True)
    path = FILES / f"{name}.md"
    replaced = path.exists()
    path.write_bytes(source["text"].encode("utf-8"))
    # Отпечаток — с диска, а не с того, что собирались записать.
    on_disk = digest(path.read_bytes().decode("utf-8"))
    saved = put(chat, "save_to_file", "", source=source, sha=on_disk,
                meta={"file": path.name, "bytes": path.stat().st_size, "replaced": replaced})
    return {"ref": saved["id"], "sha": on_disk, "got": got(source), "file": path.name,
            "url": f"/api/files/{path.name}", "bytes": path.stat().st_size,
            "replaced": replaced, "match": on_disk == digest(source["text"])}


def stored(name):
    """Файл конвейера по имени — для ссылки в браузере. Чужое имя — None."""
    stem = name[:-3] if name.endswith(".md") else ""
    path = FILES / name
    return path if NAME.fullmatch(stem) and path.is_file() else None


# ── Цепочки для браузера ────────────────────────────────────────────

def chains(chat, limit=5):
    """Последние цепочки чата: от каждого конечного результата вверх по
    `source` до поиска. Стык цел, если отпечаток прочитанного шагом совпал с
    отпечатком выхода прошлого шага; у файла ещё и с диском."""
    with closing(connect()) as db:
        rows = db.execute(f"SELECT {', '.join(FIELDS)} FROM blobs WHERE chat = ? "
                          "ORDER BY id DESC LIMIT 200", (chat,)).fetchall()
    found = {row["id"]: row for row in map(blob_row, rows)}
    used = {row["source"] for row in found.values()}
    result = []
    for leaf in sorted(found.values(), key=lambda row: -row["id"]):
        if leaf["id"] in used:
            continue
        chain, row = [], leaf
        while row:
            parent = found.get(row["source"])
            ok = parent is None or row["got"] == parent["sha"]
            if row["tool"] == "save_to_file":
                ok = ok and row["sha"] == row["got"]
            chain.append({"ref": row["id"], "tool": row["tool"], "sha": row["sha"],
                          "source": row["source"], "got": row["got"], "ok": ok,
                          "meta": row["meta"], "created": row["created"]})
            row = parent
        result.append(chain[::-1])
        if len(result) == limit:
            break
    return result


def hint():
    return ("Конвейер: search → summarize → save_to_file. Между шагами передавайте "
            "номер результата (ref) из ответа прошлого шага, а не текст.")


REF = {"type": "integer", "minimum": 1,
       "description": "номер результата (ref) из ответа прошлого шага"}

TOOLS = [
    {"name": "search", "title": "Поиск по журналу",
     "description": "Первый шаг конвейера: находит разделы журнала стенда (README — "
                    "что сделано по дням курса) по словам запроса. Возвращает номер "
                    "результата ref, его отпечаток sha и список найденных разделов; "
                    "текст разделов остаётся на сервере — дальше передавайте ref.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "что искать, несколько слов"},
         "limit": {"type": "integer", "minimum": 1,
                   "description": "сколько разделов взять, по умолчанию 4, не больше 8"},
     }, "required": ["query"]},
     "run": search},
    {"name": "summarize", "title": "Конспект",
     "description": "Второй шаг: сжимает результат прошлого шага в несколько пунктов. "
                    "Принимает ref, а не текст. Возвращает новый ref, его sha, текст "
                    "конспекта и got — что было прочитано на входе.",
     "inputSchema": {"type": "object", "properties": {
         "source": REF,
         "points": {"type": "integer", "minimum": 1,
                    "description": "сколько пунктов, по умолчанию 5, не больше 10"},
     }, "required": ["source"]},
     "run": summarize},
    {"name": "save_to_file", "title": "Сохранить в файл",
     "description": "Третий шаг: сохраняет результат прошлого шага файлом .md на "
                    "сервере стенда. Принимает ref и имя файла одним словом. "
                    "Возвращает имя файла, ссылку на него, размер и sha, снятый с диска.",
     "inputSchema": {"type": "object", "properties": {
         "source": REF,
         "name": {"type": "string",
                  "description": "имя файла без расширения: буквы, цифры, - и _"},
     }, "required": ["source", "name"]},
     "run": save_to_file},
]

SERVER = tracker.Server("aichallenge-journal", hint, TOOLS)
