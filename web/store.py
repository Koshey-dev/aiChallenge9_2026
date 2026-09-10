"""Диалоги между перезапусками: одна таблица SQLite рядом со стендом.

Коробка агента про этот файл не знает: она умеет отдать своё состояние и принять
его обратно, а где оно лежит — решает стенд. Строка на диалог, история и счётчики
в ней — JSON: читать их построчно некому, а один UPSERT на реплику дешевле, чем
перекладывание сообщений по строкам. Всё остальное состояние коробки едет
в колонке `extra` одним объектом: под каждое новое поле заводить колонку значит
дописывать миграцию на ровном месте.
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


# Поля состояния, у которых в таблице своя колонка. Остальное едет в `extra`.
COLUMNS = ("history", "usage")


def connect():
    # timeout: две вкладки могут писать одновременно, вторая ждёт свою очередь,
    # а не падает с «database is locked»
    db = sqlite3.connect(FILE, timeout=5)
    db.execute(SCHEMA)
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
    with closing(connect()) as db, db:
        db.execute("DELETE FROM dialogs WHERE session = ?", (session,))
