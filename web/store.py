"""Диалоги между перезапусками: одна таблица SQLite рядом со стендом.

Коробка агента про этот файл не знает: она умеет отдать своё состояние и принять
его обратно, а где оно лежит — решает стенд. Строка на диалог, история и счётчики
в ней — JSON: читать их построчно некому, а один UPSERT на реплику дешевле, чем
перекладывание сообщений по строкам.
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
    updated TEXT NOT NULL
)
"""


def connect():
    # timeout: две вкладки могут писать одновременно, вторая ждёт свою очередь,
    # а не падает с «database is locked»
    db = sqlite3.connect(FILE, timeout=5)
    db.execute(SCHEMA)
    return db


def load(session):
    """Состояние диалога или None, если такого диалога в базе нет."""
    with closing(connect()) as db:
        row = db.execute("SELECT history, usage FROM dialogs WHERE session = ?",
                         (session,)).fetchone()
    if row is None:
        return None
    return {"history": json.loads(row[0]), "usage": json.loads(row[1])}


def save(session, state):
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO dialogs (session, history, usage, updated) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(session) DO UPDATE SET history = excluded.history, "
            "usage = excluded.usage, updated = excluded.updated",
            (session,
             json.dumps(state["history"], ensure_ascii=False),
             json.dumps(state["usage"])),
        )


def drop(session):
    with closing(connect()) as db, db:
        db.execute("DELETE FROM dialogs WHERE session = ?", (session,))
