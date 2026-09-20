"""Диалоги между перезапусками: SQLite рядом со стендом.

Коробка агента про этот файл не знает: она умеет отдать своё состояние и принять
его обратно, а где оно лежит — решает стенд. Строка на диалог, история и счётчики
в ней — JSON: читать их построчно некому, а один UPSERT на реплику дешевле, чем
перекладывание сообщений по строкам. Всё остальное состояние коробки едет
в колонке `extra` одним объектом: под каждое новое поле заводить колонку значит
дописывать миграцию на ровном месте.

Долговременная память живёт в своей таблице: её смысл в том, чтобы пережить
забытый диалог, а строку диалога кнопка «Забыть диалог» удаляет целиком. Слои
памяти разделены не только в коробке, но и на диске. Профиль — про пользователя,
а не про разговор: новый чат должен знать то, что сказали в старом.

Профилей несколько (день 12): у каждого название, анкета, которую заполняет
пользователь, и долговременная память, которую пополняет ассистент. Строка `*`
— «Основной» профиль: его видит неделя 2 и берёт чат, у которого профиль
удалили. Строки под идентификаторами диалогов — профили самой первой версии,
когда он был у каждого диалога свой; названия у них нет, и в список они
не попадают.

Чат недели 3 — строка в `chats` поверх строки диалога с тем же идентификатором:
в `chats` то, что нужно списку слева, — название, модель, профиль, расход, — и
настройки чата. Настройки у каждого чата свои: снятая для опыта галочка не должна
ехать в соседний разговор. Новый чат начинает с пустых, то есть с умолчаний,
которые знает стенд. Таблица `prefs` осталась от версии, где настройки были
общими: при переходе её значения достались чатам, которые уже были.
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

# Свод инвариантов (день 14) — своя таблица, а не колонка чата и не поле
# состояния диалога. Причина та же, что у профиля: строку диалога «Забыть
# диалог» удаляет целиком, а инварианты забытый разговор переживают — тем они
# и отличаются от рабочей памяти, которую модель переписывает каждую реплику.
RULES = """
CREATE TABLE IF NOT EXISTS rules (
    chat    TEXT PRIMARY KEY,
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

# Строка «Основного» профиля.
PROFILE = "*"

CHAT_FIELDS = ("id", "title", "model", "profile", "prefs", "turns", "context", "cost",
               "created", "updated")


# Поля состояния, у которых в таблице своя колонка. Остальное едет в `extra`.
COLUMNS = ("history", "usage")


def connect():
    # timeout: две вкладки могут писать одновременно, вторая ждёт свою очередь,
    # а не падает с «database is locked»
    db = sqlite3.connect(FILE, timeout=5)
    db.execute(SCHEMA)
    db.execute(PROFILES)
    db.execute(RULES)
    db.execute(CHATS)
    # База могла остаться от версии без журнала расхода — доводим её на месте.
    known = {row[1] for row in db.execute("PRAGMA table_info(dialogs)")}
    if "extra" not in known:
        db.execute("ALTER TABLE dialogs ADD COLUMN extra TEXT NOT NULL DEFAULT '{}'")
    # День 12: у профиля появились название и анкета, у чата — свой профиль.
    known = {row[1] for row in db.execute("PRAGMA table_info(profiles)")}
    if "title" not in known:
        db.execute("ALTER TABLE profiles ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE profiles ADD COLUMN card TEXT NOT NULL DEFAULT '{}'")
    known = {row[1] for row in db.execute("PRAGMA table_info(chats)")}
    if "profile" not in known:
        db.execute(f"ALTER TABLE chats ADD COLUMN profile TEXT NOT NULL DEFAULT '{PROFILE}'")
    # Настройки переехали из общей таблицы в строку чата. Чаты, что уже были,
    # забирают общие значения себе — у них ничего не меняется. Commit явный:
    # ALTER применяется сразу, а UPDATE без него откатился бы при закрытии
    # соединения на чтение, и перенос потерялся бы молча.
    if "prefs" not in known:
        db.execute("ALTER TABLE chats ADD COLUMN prefs TEXT NOT NULL DEFAULT '{}'")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                      "AND name = 'prefs'").fetchone():
            db.execute("UPDATE chats SET prefs = COALESCE("
                       "(SELECT data FROM prefs WHERE name = 'chat'), '{}')")
            db.commit()
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


def load_profile(profile=PROFILE):
    """Долговременная память профиля. Пустой словарь, если её ещё нет."""
    with closing(connect()) as db:
        row = db.execute("SELECT data FROM profiles WHERE session = ?",
                         (profile,)).fetchone()
    return json.loads(row[0]) if row else {}


def save_profile(data, profile=PROFILE):
    """Долговременная память профиля. Название и анкета остаются как были."""
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO profiles (session, data, updated) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(session) DO UPDATE SET data = excluded.data, "
            "updated = excluded.updated",
            (profile, json.dumps(data, ensure_ascii=False)))


def forget_profile(profile=PROFILE):
    """Забыть, что ассистент узнал сам. Анкету заполнял пользователь — она остаётся."""
    save_profile({}, profile)


def load_rules(chat_id):
    """Свод чата. Пустой список, если его ещё не заводили."""
    with closing(connect()) as db:
        row = db.execute("SELECT data FROM rules WHERE chat = ?", (chat_id,)).fetchone()
    return json.loads(row[0]) if row else []


def save_rules(chat_id, items):
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO rules (chat, data, updated) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(chat) DO UPDATE SET data = excluded.data, "
            "updated = excluded.updated",
            (chat_id, json.dumps(items, ensure_ascii=False)))


PROFILE_FIELDS = ("session", "title", "card", "data", "updated")


def unpack(row):
    item = dict(zip(PROFILE_FIELDS, row))
    return {"id": item["session"], "title": item["title"],
            "card": json.loads(item["card"]), "learned": json.loads(item["data"]),
            "updated": item["updated"]}


def profiles():
    """Профили для списков: «Основной» первым, остальные в порядке создания."""
    with closing(connect()) as db:
        rows = db.execute(f"SELECT {', '.join(PROFILE_FIELDS)} FROM profiles "
                          "WHERE title != '' ORDER BY session = ? DESC, rowid",
                          (PROFILE,)).fetchall()
    return [unpack(row) for row in rows]


def profile(profile_id):
    with closing(connect()) as db:
        row = db.execute(f"SELECT {', '.join(PROFILE_FIELDS)} FROM profiles "
                         "WHERE session = ? AND title != ''", (profile_id,)).fetchone()
    return unpack(row) if row else None


def add_profile(profile_id, title, card):
    with closing(connect()) as db, db:
        db.execute("INSERT INTO profiles (session, title, card, data, updated) "
                   "VALUES (?, ?, ?, '{}', datetime('now'))",
                   (profile_id, title, json.dumps(card, ensure_ascii=False)))
    return profile(profile_id)


def edit_profile(profile_id, title, card):
    with closing(connect()) as db, db:
        db.execute("UPDATE profiles SET title = ?, card = ?, updated = datetime('now') "
                   "WHERE session = ?",
                   (title, json.dumps(card, ensure_ascii=False), profile_id))
    return profile(profile_id)


def delete_profile(profile_id):
    """Удалить профиль. Его чаты переходят на «Основной»: без профиля чат не живёт."""
    with closing(connect()) as db, db:
        db.execute("DELETE FROM profiles WHERE session = ?", (profile_id,))
        db.execute("UPDATE chats SET profile = ? WHERE profile = ?", (PROFILE, profile_id))


def seed_profiles(title, presets):
    """Первый запуск дня 12: «Основному» — название, рядом — заготовки.

    Долговременная память, накопленная до дня 12, остаётся в «Основном»: это
    та же строка `*`. Если у неё уже есть название, стенд здесь бывал, и
    удалённые руками заготовки не возвращаются.
    """
    with closing(connect()) as db, db:
        row = db.execute("SELECT title FROM profiles WHERE session = ?",
                         (PROFILE,)).fetchone()
        if row and row[0]:
            return
        db.execute("INSERT INTO profiles (session, title, card, data, updated) "
                   "VALUES (?, ?, '{}', '{}', datetime('now')) "
                   "ON CONFLICT(session) DO UPDATE SET title = excluded.title",
                   (PROFILE, title))
        for preset in presets:
            db.execute("INSERT INTO profiles (session, title, card, data, updated) "
                       "VALUES (?, ?, ?, '{}', datetime('now'))",
                       (preset["id"], preset["title"],
                        json.dumps(preset["card"], ensure_ascii=False)))


def chat_row(row):
    item = dict(zip(CHAT_FIELDS, row))
    return {**item, "prefs": json.loads(item["prefs"])}


def chats():
    """Чаты для списка слева: свежие сверху."""
    with closing(connect()) as db:
        rows = db.execute(f"SELECT {', '.join(CHAT_FIELDS)} FROM chats "
                          "ORDER BY updated DESC").fetchall()
    return [chat_row(row) for row in rows]


def chat(chat_id):
    with closing(connect()) as db:
        row = db.execute(f"SELECT {', '.join(CHAT_FIELDS)} FROM chats WHERE id = ?",
                         (chat_id,)).fetchone()
    return chat_row(row) if row else None


def add_chat(chat_id, model, profile=PROFILE):
    with closing(connect()) as db, db:
        db.execute("INSERT INTO chats (id, model, profile, created, updated) "
                   "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                   (chat_id, model, profile))
    return chat(chat_id)


def edit_chat(chat_id, **fields):
    """Название, модель, профиль или настройки. Порядок в списке не меняется:
    он по последней реплике."""
    names = [name for name in fields if name in ("title", "model", "profile", "prefs")]
    if not names:
        return chat(chat_id)
    values = [json.dumps(fields[name], ensure_ascii=False) if name == "prefs"
              else fields[name] for name in names]
    with closing(connect()) as db, db:
        db.execute(f"UPDATE chats SET {', '.join(f'{name} = ?' for name in names)} "
                   "WHERE id = ?", (*values, chat_id))
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
    """Удалить чат вместе с его диалогом и сводом. Профиль не его и остаётся.

    Свод переживает забытый диалог, но не удалённый чат: он свод этого чата,
    и без чата ему некуда вернуться.
    """
    with closing(connect()) as db, db:
        db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        db.execute("DELETE FROM dialogs WHERE session = ?", (chat_id,))
        db.execute("DELETE FROM rules WHERE chat = ?", (chat_id,))

