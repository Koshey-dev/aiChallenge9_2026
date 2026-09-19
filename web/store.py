"""Диалоги между перезапусками: SQLite рядом со стендом.

Коробка агента про этот файл не знает: она умеет отдать своё состояние и принять
его обратно, а где оно лежит — решает стенд. Строка на диалог, история и счётчики
в ней — JSON: читать их построчно некому, а один UPSERT на реплику дешевле, чем
перекладывание сообщений по строкам. Всё остальное состояние коробки едет
в колонке `extra` одним объектом: под каждое новое поле заводить колонку значит
дописывать миграцию на ровном месте.

Долговременная память живёт в своей таблице: её смысл в том, чтобы пережить
забытый диалог, а строку диалога кнопка «Забыть диалог» удаляет целиком. Слои
памяти разделены не только в коробке, но и на диске. Профиль один на весь стенд:
он про пользователя, а не про разговор, и новый чат должен знать то, что сказали
в старом.

Чат недели 3 — строка в `chats` поверх строки диалога с тем же идентификатором:
в `chats` то, что нужно списку слева, — название, модель, расход. Настройки чата
общие на все чаты и лежат в `prefs`.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

FILE = Path(__file__).parent / "agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogs (
    session TEXT PRIMARY KEY,
    history TEXT NOT NULL,
    usage   TEXT NOT NULL,
    extra   TEXT NOT NULL DEFAULT '{}',
    updated TEXT NOT NULL
)
"""

PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    session TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    updated TEXT NOT NULL
)
"""

CHATS = """
CREATE TABLE IF NOT EXISTS chats (
    id      TEXT PRIMARY KEY,
    title   TEXT NOT NULL DEFAULT '',
    model   TEXT NOT NULL,
    turns   INTEGER NOT NULL DEFAULT 0,
    context INTEGER NOT NULL DEFAULT 0,
    cost    REAL NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
)
"""

PREFS = """
CREATE TABLE IF NOT EXISTS prefs (
    name    TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    updated TEXT NOT NULL
)
"""

# Строка общего профиля. Строки, которые лежат под идентификаторами диалогов, —
# профили прежней версии, когда он был у каждого диалога свой; их никто не читает.
PROFILE = "*"

CHAT_FIELDS = ("id", "title", "model", "turns", "context", "cost", "created", "updated")


# Поля состояния, у которых в таблице своя колонка. Остальное едет в `extra`.
COLUMNS = ("history", "usage")


def connect():
    # timeout: две вкладки могут писать одновременно, вторая ждёт свою очередь,
    # а не падает с «database is locked»
    db = sqlite3.connect(FILE, timeout=5)
    db.execute(SCHEMA)
    db.execute(PROFILES)
    db.execute(CHATS)
    db.execute(PREFS)
    # База могла остаться от версии без журнала расхода — доводим её на месте.
    known = {row[1] for row in db.execute("PRAGMA table_info(dialogs)")}
    if "extra" not in known:
        db.execute("ALTER TABLE dialogs ADD COLUMN extra TEXT NOT NULL DEFAULT '{}'")
    return db


def load(session):
    """Состояние диалога или None, если такого диалога в базе нет."""
    with closing(connect()) as db:
        row = db.execute("SELECT history, usage, extra FROM dialogs WHERE session = ?",
                         (session,)).fetchone()
    if row is None:
        return None
    return {**json.loads(row[2]),
            "history": json.loads(row[0]), "usage": json.loads(row[1])}


def save(session, state):
    with closing(connect()) as db, db:
        extra = {key: value for key, value in state.items() if key not in COLUMNS}
        db.execute(
            "INSERT INTO dialogs (session, history, usage, extra, updated) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(session) DO UPDATE SET history = excluded.history, "
            "usage = excluded.usage, extra = excluded.extra, updated = excluded.updated",
            (session,
             json.dumps(state["history"], ensure_ascii=False),
             json.dumps(state["usage"]),
             json.dumps(extra, ensure_ascii=False)),
        )


def drop(session):
    """Забыть диалог. Профиль остаётся: он лежит в другой таблице."""
    with closing(connect()) as db, db:
        db.execute("DELETE FROM dialogs WHERE session = ?", (session,))


def load_profile():
    """Долговременная память. Пустой словарь, если её ещё нет."""
    with closing(connect()) as db:
        row = db.execute("SELECT data FROM profiles WHERE session = ?",
                         (PROFILE,)).fetchone()
    return json.loads(row[0]) if row else {}


def save_profile(profile):
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO profiles (session, data, updated) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(session) DO UPDATE SET data = excluded.data, "
            "updated = excluded.updated",
            (PROFILE, json.dumps(profile, ensure_ascii=False)))


def drop_profile():
    with closing(connect()) as db, db:
        db.execute("DELETE FROM profiles WHERE session = ?", (PROFILE,))


def chats():
    """Чаты для списка слева: свежие сверху."""
    with closing(connect()) as db:
        rows = db.execute(f"SELECT {', '.join(CHAT_FIELDS)} FROM chats "
                          "ORDER BY updated DESC").fetchall()
    return [dict(zip(CHAT_FIELDS, row)) for row in rows]


def chat(chat_id):
    with closing(connect()) as db:
        row = db.execute(f"SELECT {', '.join(CHAT_FIELDS)} FROM chats WHERE id = ?",
                         (chat_id,)).fetchone()
    return dict(zip(CHAT_FIELDS, row)) if row else None


def add_chat(chat_id, model):
    with closing(connect()) as db, db:
        db.execute("INSERT INTO chats (id, model, created, updated) "
                   "VALUES (?, ?, datetime('now'), datetime('now'))", (chat_id, model))
    return chat(chat_id)


def edit_chat(chat_id, **fields):
    """Название или модель. Порядок в списке не меняется: он по последней реплике."""
    names = [name for name in fields if name in ("title", "model")]
    if not names:
        return chat(chat_id)
    with closing(connect()) as db, db:
        db.execute(f"UPDATE chats SET {', '.join(f'{name} = ?' for name in names)} "
                   "WHERE id = ?", (*(fields[name] for name in names), chat_id))
    return chat(chat_id)


def after_turn(chat_id, *, title, turns, context, cost):
    """Реплика дошла до конца: счётчики чата и место в списке.

    Цена копится, а не пересчитывается по всему расходу: модель у чата меняют
    по ходу разговора, и прошлые реплики стоили по прайсу своей модели.
    """
    with closing(connect()) as db, db:
        db.execute("UPDATE chats SET title = ?, turns = ?, context = ?, "
                   "cost = cost + ?, updated = datetime('now') WHERE id = ?",
                   (title, turns, context, cost, chat_id))
    return chat(chat_id)


def drop_chat(chat_id):
    """Удалить чат вместе с его диалогом. Профиль общий и остаётся."""
    with closing(connect()) as db, db:
        db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        db.execute("DELETE FROM dialogs WHERE session = ?", (chat_id,))


def load_prefs(name):
    with closing(connect()) as db:
        row = db.execute("SELECT data FROM prefs WHERE name = ?", (name,)).fetchone()
    return json.loads(row[0]) if row else {}


def save_prefs(name, data):
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO prefs (name, data, updated) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET data = excluded.data, "
            "updated = excluded.updated",
            (name, json.dumps(data, ensure_ascii=False)))
