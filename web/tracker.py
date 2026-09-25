"""День 17: свой MCP-сервер вокруг мини-трекера задач.

Трекер — «чужое API», вокруг которого сервер построен: таблица задач в той же
базе, что и стенд, и три операции над ней. Коробка агента про трекер не знает
ничего: она видит только то, что сервер рассказал о себе по протоколу, —
имена, описания и схемы аргументов, — и зовёт инструменты через `tools/call`.

Сервер — это JSON-RPC 2.0 поверх одного POST, как и клиент дня 16, без SDK.
Инструмент регистрируется записью в `TOOLS`: имя, описание для модели, схема
входных параметров (JSON Schema) и обработчик. Из этой же записи собираются
и ответ на `tools/list`, и проверка аргументов перед вызовом — описанное
и проверяемое не могут разойтись.

Ошибки двух родов, и протокол их разводит. Неизвестный метод или инструмент,
кривые аргументы — ошибка JSON-RPC (`error` с кодом): вызов не состоялся.
Инструмент отработал, но дело не вышло (нет такой задачи) — это результат
с `isError: true`: его читает модель и может поправиться сама.

С дня 18 сервер один на несколько модулей: другой модуль отдаёт свои записи
в `register`, и они встают в тот же список. Обработчику приходит и чат, из
которого стенд позвал инструмент (заголовок `X-Chat` своего клиента): трекеру
он не нужен, а заданиям планировщика — нужен, они принадлежат чату.

С дня 19 обработчик может быть и корутиной: конспект конвейера ждёт модель,
а держать цикл событий стенда на время её ответа нельзя.

С дня 20 серверов три, и `register` больше нет: протокол — класс `Server`,
у каждого экземпляра свои инструменты, своя подсказка и свои сессии. Трекер,
планировщик и конвейер — три таких сервера на трёх адресах; сессия, выданная
одним, другому ничего не значит.
"""

import inspect
import json
import sqlite3
import uuid
from contextlib import closing

import store

# Версии протокола, которые сервер понимает. Клиент просит свою — если она
# в списке, сервер отвечает ей же, иначе — последней своей.
VERSIONS = ["2025-06-18", "2025-03-26"]

INSTRUCTIONS = ("Мини-трекер задач стенда. Задачи живут в статусах todo → doing → done, "
                "приоритет low, normal или high. Номер задачи берите из list_tasks.")

STATUSES = ["todo", "doing", "done"]
PRIORITIES = ["low", "normal", "high"]

TABLE = """
CREATE TABLE IF NOT EXISTS tracker (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title    TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    status   TEXT NOT NULL DEFAULT 'todo',
    created  TEXT NOT NULL,
    updated  TEXT NOT NULL
)
"""

# Пустой трекер на первом открытии: list_tasks сразу есть что показать.
SEED = [("Собрать демо дня 16", "normal", "done"),
        ("Разобрать отзывы после занятия", "low", "todo"),
        ("Выкатить стенд на VPS", "high", "doing")]

FIELDS = ("id", "title", "priority", "status", "created", "updated")


def connect():
    db = sqlite3.connect(store.FILE, timeout=5)
    db.execute(TABLE)
    return db


def seed():
    with closing(connect()) as db, db:
        if db.execute("SELECT 1 FROM tracker LIMIT 1").fetchone():
            return
        db.executemany("INSERT INTO tracker (title, priority, status, created, updated) "
                       "VALUES (?, ?, ?, datetime('now'), datetime('now'))", SEED)


def tasks(status=""):
    """Задачи трекера, свежие сверху. Пустой статус — все."""
    query = f"SELECT {', '.join(FIELDS)} FROM tracker"
    args = ()
    if status:
        query += " WHERE status = ?"
        args = (status,)
    with closing(connect()) as db:
        rows = db.execute(query + " ORDER BY id DESC", args).fetchall()
    return [dict(zip(FIELDS, row)) for row in rows]


def task(task_id):
    with closing(connect()) as db:
        row = db.execute(f"SELECT {', '.join(FIELDS)} FROM tracker WHERE id = ?",
                         (task_id,)).fetchone()
    return dict(zip(FIELDS, row)) if row else None


# ── Инструменты ─────────────────────────────────────────────────────
# Обработчик получает аргументы, уже сверенные со схемой, и возвращает
# данные — или бросает `Failed`: инструмент отработал, но дело не вышло.

class Failed(Exception):
    pass


def list_tasks(args, _chat):
    found = tasks(args.get("status", ""))
    return {"count": len(found), "tasks": found}


def create_task(args, _chat):
    title = " ".join(args["title"].split())[:120]
    if not title:
        raise Failed("название задачи пустое")
    with closing(connect()) as db, db:
        cursor = db.execute(
            "INSERT INTO tracker (title, priority, status, created, updated) "
            "VALUES (?, ?, 'todo', datetime('now'), datetime('now'))",
            (title, args.get("priority", "normal")))
    return task(cursor.lastrowid)


def update_status(args, _chat):
    before = task(args["id"])
    if before is None:
        raise Failed(f"задачи #{args['id']} нет — номер берите из list_tasks")
    with closing(connect()) as db, db:
        db.execute("UPDATE tracker SET status = ?, updated = datetime('now') WHERE id = ?",
                   (args["status"], args["id"]))
    return {**task(args["id"]), "was": before["status"]}


TOOLS = [
    {"name": "list_tasks", "title": "Список задач",
     "description": "Задачи трекера, свежие сверху: номер, название, приоритет, статус. "
                    "Без аргументов — все задачи.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": STATUSES,
                    "description": "показать только задачи в этом статусе"},
     }},
     "run": list_tasks},
    {"name": "create_task", "title": "Завести задачу",
     "description": "Заводит новую задачу в статусе todo и возвращает её с номером.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "название задачи, коротко"},
         "priority": {"type": "string", "enum": PRIORITIES,
                      "description": "приоритет; по умолчанию normal"},
     }, "required": ["title"]},
     "run": create_task},
    {"name": "update_status", "title": "Сменить статус",
     "description": "Переводит задачу в другой статус. Номер задачи — из list_tasks.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "номер задачи"},
         "status": {"type": "string", "enum": STATUSES, "description": "новый статус"},
     }, "required": ["id", "status"]},
     "run": update_status},
]
KINDS = {"string": str, "integer": int, "object": dict}


def checked(tool, args):
    """Аргументы против схемы инструмента. Лишнее и не того типа — отказ,
    а не молчаливая правка: модель должна увидеть, что ошиблась."""
    if not isinstance(args, dict):
        raise ValueError("arguments должен быть объектом")
    schema = tool["inputSchema"]
    props = schema["properties"]
    for name in schema.get("required", []):
        if name not in args:
            raise ValueError(f"не хватает аргумента {name}")
    for name, value in args.items():
        about = props.get(name)
        if about is None:
            raise ValueError(f"у инструмента нет аргумента {name}")
        kind = KINDS[about["type"]]
        # bool в Python — тоже int, а номер задачи True — это не номер.
        if not isinstance(value, kind) or isinstance(value, bool):
            raise ValueError(f"{name} должен быть {about['type']}")
        if "enum" in about and value not in about["enum"]:
            raise ValueError(f"{name}: одно из {', '.join(about['enum'])}")
        if "minimum" in about and value < about["minimum"]:
            raise ValueError(f"{name} должен быть не меньше {about['minimum']}")
    return args


# ── Протокол ────────────────────────────────────────────────────────

class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Server:
    """Один MCP-сервер: имя, подсказка для модели и свои инструменты.

    `about` — функция, а не строка: подсказка собирается на каждое рукопожатие,
    и «сейчас» планировщика в ней свежее. Сессии, выданные на `initialize`, —
    в памяти: сервер живёт в процессе стенда, и перезапуск честно обрывает
    старые разговоры — клиент поздоровается заново.
    """

    def __init__(self, name, about, tools):
        self.info = {"name": name, "version": "1.0"}
        self.about = about
        self.tools = tools
        self.by_name = {tool["name"]: tool for tool in tools}
        self.sessions = set()

    def listed(self):
        """Инструменты так, как их видит клиент: без обработчика."""
        return [{key: value for key, value in tool.items() if key != "run"}
                for tool in self.tools]

    async def call(self, params, chat):
        tool = self.by_name.get(params.get("name"))
        if tool is None:
            raise RpcError(-32602, f"инструмента {params.get('name')!r} нет")
        try:
            args = checked(tool, params.get("arguments") or {})
        except ValueError as bad:
            raise RpcError(-32602, f"{tool['name']}: {bad}") from bad
        try:
            data = tool["run"](args, chat)
            if inspect.isawaitable(data):
                data = await data
        except Failed as failed:
            return {"content": [{"type": "text", "text": str(failed)}], "isError": True}
        # Текстом — для модели и клиентов, которые знают только `content`;
        # структурой — для тех, кто умеет `structuredContent` (протокол 2025-06-18).
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data, "isError": False}

    async def handle(self, message, session, chat=""):
        """Одно сообщение JSON-RPC. Отдаёт код ответа, тело и выданную сессию.

        Уведомление (сообщение без `id`) ответа не получает — только 202.
        Всё, кроме `initialize`, требует сессию, выданную на рукопожатии: без
        знакомства разговор не начинается, как у GitMCP из дня 16.
        `chat` — из какого чата стенд зовёт инструмент; у внешнего клиента пусто.
        """
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0"                 or not isinstance(message.get("method"), str):
            return 400, fault(None, -32600, "это не запрос JSON-RPC 2.0"), ""
        method = message["method"]
        if "id" not in message:
            return 202, None, ""
        ident = message["id"]
        params = message.get("params") or {}
        given = ""
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                given = uuid.uuid4().hex
                self.sessions.add(given)
                result = {"protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": self.info, "instructions": self.about()}
            elif session not in self.sessions:
                return 400, fault(ident, -32000,
                                  "Mcp-Session-Id не выдан: сначала initialize"), ""
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.listed()}
            elif method == "tools/call":
                result = await self.call(params, chat)
            else:
                raise RpcError(-32601, f"метода {method} нет")
        except RpcError as bad:
            return 200, fault(ident, bad.code, str(bad)), given
        return 200, {"jsonrpc": "2.0", "id": ident, "result": result}, given


def fault(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


SERVER = Server("aichallenge-tracker", lambda: INSTRUCTIONS, TOOLS)
