"""День 18: планировщик — инструменты с отложенным и периодическим выполнением.

Задание — строка в таблице `jobs`: чей чат, вид, срок и период. Выполняет его
не запрос из браузера, а цикл внутри процесса стенда: раз в несколько секунд
он смотрит, у кого срок вышел, срабатывает и пишет результат в `runs`. Процесс
uvicorn держит systemd, поэтому цикл живёт и без открытой вкладки — это и
есть «агент 24/7». Задания лежат в SQLite и переживают перезапуск: одноразовое
просроченное срабатывает при старте, периодическое перевзводится от момента
срабатывания, а не от пропущенного срока, — иначе после долгого простоя оно
отстреляло бы всё пропущенное подряд.

Два вида заданий. Напоминание — текст, который в срок уходит в чат. Сводка —
периодический сбор данных по стенду: код снимает счётчики (задачи трекера по
статусам, реплики и расход по чатам), считает разницу с прошлым снимком, а
текст по этим числам пишет модель — если у чата стоит галочка. Модель ничего
не собирает и не считает: она пересказывает.

Инструменты регистрируются в сервере дня 17 (`tracker.register`): те же записи
с именем, описанием, схемой и обработчиком. Обработчику приходит и чат, из
которого его позвали: задания принадлежат чату, и вызов из другого чата их
не видит. Сам цикл про модель не знает — текст сводки он просит у функции,
которую ему дал стенд.
"""

import asyncio
import json
import logging
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import store
import tracker

log = logging.getLogger("scheduler")

# Сколько секунд между проверками срока. Меньше — точнее, но чаще запрос к
# базе; минута у заданий всё равно минимальный шаг.
TICK = 5

# Часовой пояс пользователя стенда: в нём модель видит «сейчас» и в нём же
# отдаёт сроки заданий. Сервер живёт в UTC, база — тоже.
ZONE_NAME = os.environ.get("STAND_TZ", "Europe/Moscow")
try:
    ZONE = ZoneInfo(ZONE_NAME)
except ZoneInfoNotFoundError:
    log.warning("часовой пояс %s не найден — время будет в UTC", ZONE_NAME)
    ZONE_NAME, ZONE = "UTC", timezone.utc

STAMP = "%Y-%m-%d %H:%M:%S"

TABLES = """
CREATE TABLE IF NOT EXISTS jobs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    text    TEXT NOT NULL DEFAULT '',
    every   INTEGER NOT NULL DEFAULT 0,
    due     TEXT NOT NULL,
    base    TEXT NOT NULL DEFAULT '{}',
    fired   INTEGER NOT NULL DEFAULT 0,
    active  INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    job   INTEGER NOT NULL,
    chat  TEXT NOT NULL,
    kind  TEXT NOT NULL,
    at    TEXT NOT NULL,
    after INTEGER NOT NULL DEFAULT 0,
    data  TEXT NOT NULL DEFAULT '{}'
);
"""

JOB_FIELDS = ("id", "chat", "kind", "text", "every", "due", "base", "fired", "active",
              "created")
RUN_FIELDS = ("id", "job", "chat", "kind", "at", "after", "data")

# Когда цикл последний раз проверял сроки — браузеру, чтобы было видно,
# что он жив. Пусто, пока цикл не поднялся.
TICKED = ""


def now():
    return datetime.now(timezone.utc)


def stamp(moment):
    """Время так, как его пишет SQLite: UTC без пояса, до секунды."""
    return moment.strftime(STAMP)


def parse(text):
    return datetime.strptime(text, STAMP).replace(tzinfo=timezone.utc)


def local(text):
    """Время из базы — в поясе пользователя, для модели и для человека."""
    return parse(text).astimezone(ZONE).strftime("%Y-%m-%d %H:%M")


def clock():
    """«Сейчас» для модели: она не знает времени, пока ей не скажут."""
    moment = now().astimezone(ZONE)
    offset = moment.strftime("%z")
    return f"{moment.strftime('%Y-%m-%d %H:%M')} ({ZONE_NAME}, UTC{offset[:3]}:{offset[3:]})"


def hint():
    """Строка в instructions сервера — свежая на каждое рукопожатие."""
    return (f"Планировщик: сейчас {clock()}; сроки напоминаний считайте от этого "
            "времени. Задания этого чата — в list_jobs, снять — cancel_job.")


def connect():
    db = tracker.connect()
    db.executescript(TABLES)
    return db


def job_row(row):
    item = dict(zip(JOB_FIELDS, row))
    return {**item, "base": json.loads(item["base"]), "active": bool(item["active"])}


def run_row(row):
    item = dict(zip(RUN_FIELDS, row))
    return {**item, "data": json.loads(item["data"])}


def jobs(chat):
    """Активные задания чата, ближайшее сверху."""
    with closing(connect()) as db:
        rows = db.execute(f"SELECT {', '.join(JOB_FIELDS)} FROM jobs "
                          "WHERE chat = ? AND active = 1 ORDER BY due, id", (chat,)).fetchall()
    return [job_row(row) for row in rows]


def job(job_id):
    with closing(connect()) as db:
        row = db.execute(f"SELECT {', '.join(JOB_FIELDS)} FROM jobs WHERE id = ?",
                         (job_id,)).fetchone()
    return job_row(row) if row else None


def due_jobs():
    with closing(connect()) as db:
        rows = db.execute(f"SELECT {', '.join(JOB_FIELDS)} FROM jobs "
                          "WHERE active = 1 AND due <= ? ORDER BY due, id",
                          (stamp(now()),)).fetchall()
    return [job_row(row) for row in rows]


def add_job(chat, kind, text, every, due):
    # У сводки снимок берётся при постановке: первое срабатывание уже знает,
    # с чем сравнивать.
    base = snapshot() if kind == "digest" else {}
    with closing(connect()) as db, db:
        cursor = db.execute(
            "INSERT INTO jobs (chat, kind, text, every, due, base, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat, kind, text, every, stamp(due), json.dumps(base), stamp(now())))
    return job(cursor.lastrowid)


def cancel(job_id):
    with closing(connect()) as db, db:
        db.execute("UPDATE jobs SET active = 0 WHERE id = ?", (job_id,))


def runs(chat, *, after_id=0, since="", limit=200):
    """Срабатывания чата по порядку. `after_id` — только новее известного
    браузеру; `since` — только за период, для агрегата."""
    query = f"SELECT {', '.join(RUN_FIELDS)} FROM runs WHERE chat = ? AND id > ?"
    args = [chat, after_id]
    if since:
        query += " AND at >= ?"
        args.append(since)
    with closing(connect()) as db:
        rows = db.execute(query + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [run_row(row) for row in reversed(rows)]


# ── Сбор данных ─────────────────────────────────────────────────────

def snapshot():
    """Счётчики стенда на этот момент: задачи по статусам, реплики и расход."""
    with closing(connect()) as db:
        tasks = dict(db.execute("SELECT status, COUNT(*) FROM tracker GROUP BY status")
                     .fetchall())
        chats, turns, cost = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(turns), 0), COALESCE(SUM(cost), 0) FROM chats"
        ).fetchone()
    return {"tasks": {status: tasks.get(status, 0) for status in tracker.STATUSES},
            "total": sum(tasks.values()), "chats": chats, "turns": turns,
            "cost": round(cost, 6)}


def delta(before, after):
    """Что изменилось между двумя снимками. Считает код, не модель."""
    return {"created": after["total"] - before["total"],
            "closed": after["tasks"]["done"] - before["tasks"]["done"],
            "turns": after["turns"] - before["turns"],
            "cost": round(after["cost"] - before["cost"], 6)}


def changed(since):
    """Задачи трекера, которых касались за период, — модели есть что назвать."""
    with closing(connect()) as db:
        rows = db.execute("SELECT id, title, status FROM tracker WHERE updated >= ? "
                          "ORDER BY updated DESC LIMIT 10", (since,)).fetchall()
    return [dict(zip(("id", "title", "status"), row)) for row in rows]


def last_fired(job_id):
    with closing(connect()) as db:
        row = db.execute("SELECT at FROM runs WHERE job = ? ORDER BY id DESC LIMIT 1",
                         (job_id,)).fetchone()
    return row[0] if row else ""


async def collect(entry, item, write):
    """Сводка: снимок, разница с прошлым и — если чат просил — текст модели."""
    since = last_fired(item["id"]) or item["created"]
    current = snapshot()
    data = {"since": since,
            "minutes": max(1, round((now() - parse(since)).total_seconds() / 60)),
            "delta": delta(item["base"], current), "changed": changed(since),
            "now": current, "summary": "", "cost": 0.0}
    try:
        data["summary"], data["cost"] = await write(entry, data)
    except Exception as bad:  # noqa: BLE001 — сводка без текста лучше, чем мёртвый цикл
        data["error"] = f"{type(bad).__name__}: {bad}"[:200]
        log.warning("текст сводки не получен: %s", data["error"])
    return data


async def fire(item, write):
    """Одно срабатывание: результат в `runs`, задание перевзведено или закрыто."""
    entry = store.chat(item["chat"]) or {}
    if item["kind"] == "remind":
        data = {"text": item["text"]}
    else:
        data = await collect(entry, item, write)
    moment = now()
    with closing(connect()) as db, db:
        # `after` — на какой реплике чат стоял в момент срабатывания: по нему
        # браузер ставит пузырь в ленту на своё место.
        db.execute("INSERT INTO runs (job, chat, kind, at, after, data) VALUES (?, ?, ?, ?, ?, ?)",
                   (item["id"], item["chat"], item["kind"], stamp(moment),
                    entry.get("turns", 0), json.dumps(data, ensure_ascii=False)))
        if item["every"]:
            db.execute("UPDATE jobs SET fired = fired + 1, due = ?, base = ? WHERE id = ?",
                       (stamp(moment + timedelta(minutes=item["every"])),
                        json.dumps(data.get("now", item["base"])), item["id"]))
        else:
            db.execute("UPDATE jobs SET fired = fired + 1, active = 0 WHERE id = ?",
                       (item["id"],))


async def loop(write):
    """Фоновый цикл стенда: раз в TICK секунд срабатывают задания с вышедшим сроком."""
    global TICKED
    while True:
        for item in due_jobs():
            try:
                await fire(item, write)
            except Exception:  # noqa: BLE001 — одно сломанное задание не роняет остальные
                log.exception("задание #%s не выполнилось", item["id"])
        TICKED = stamp(now())
        await asyncio.sleep(TICK)


def idle():
    """Сколько секунд назад цикл проверял сроки. Пусто — не поднимался."""
    return round((now() - parse(TICKED)).total_seconds()) if TICKED else None


# ── Инструменты ─────────────────────────────────────────────────────

def shown(item):
    """Задание так, как его видит модель: сроки в поясе пользователя."""
    left = max(0, round((parse(item["due"]) - now()).total_seconds() / 60))
    return {"id": item["id"], "kind": item["kind"], "text": item["text"],
            "every_minutes": item["every"], "due": local(item["due"]), "in_minutes": left,
            "fired": item["fired"], "active": item["active"]}


def remind(args, chat):
    text = " ".join(args["text"].split())[:200]
    if not text:
        raise tracker.Failed("текст напоминания пустой")
    due = now() + timedelta(minutes=args["in_minutes"])
    return shown(add_job(chat, "remind", text, args.get("every_minutes", 0), due))


def digest(args, chat):
    # Сводка у чата одна: повторный вызов меняет период, а не плодит задания.
    for old in jobs(chat):
        if old["kind"] == "digest":
            cancel(old["id"])
    due = now() + timedelta(minutes=args["every_minutes"])
    return shown(add_job(chat, "digest", "", args["every_minutes"], due))


def list_jobs(args, chat):
    found = [shown(item) for item in jobs(chat)]
    return {"count": len(found), "jobs": found, "now": clock()}


def cancel_job(args, chat):
    found = job(args["id"])
    if found is None or found["chat"] != chat or not found["active"]:
        raise tracker.Failed(f"активного задания #{args['id']} нет — номер берите из list_jobs")
    cancel(args["id"])
    return {**shown(found), "active": False}


def summary(args, chat):
    """Агрегат по сохранённым срабатываниям чата за период. Разницы сводок
    складываются: сводка у чата одна, и её периоды не перекрываются."""
    hours = args.get("hours", 24)
    since = stamp(now() - timedelta(hours=hours))
    fired = runs(chat, since=since, limit=1000)
    digests = [run for run in fired if run["kind"] == "digest"]
    reminders = [run for run in fired if run["kind"] == "remind"]
    result = {"hours": hours, "digests": len(digests),
              "reminders": [{"at": local(run["at"]), "text": run["data"]["text"]}
                            for run in reminders],
              "now": snapshot(), "jobs": [shown(item) for item in jobs(chat)]}
    if digests:
        result["change"] = {key: round(sum(run["data"]["delta"][key] for run in digests), 6)
                            for key in ("created", "closed", "turns", "cost")}
        result["since"] = local(digests[0]["data"]["since"])
        result["last"] = {"at": local(digests[-1]["at"]),
                          "summary": digests[-1]["data"].get("summary", "")}
    return result


MINUTES = {"type": "integer", "minimum": 1, "description": "период в минутах"}

TOOLS = [
    {"name": "remind", "title": "Напоминание",
     "description": "Напомнить в этот чат через заданное число минут. С every_minutes "
                    "напоминание повторяется с этим периодом, пока его не снимут через "
                    "cancel_job. Срок считается от текущего времени — оно названо "
                    "в инструкциях сервера.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "о чём напомнить"},
         "in_minutes": {"type": "integer", "minimum": 1,
                        "description": "через сколько минут напомнить"},
         "every_minutes": {"type": "integer", "minimum": 0,
                           "description": "период повтора в минутах; 0 или без него — один раз"},
     }, "required": ["text", "in_minutes"]},
     "run": remind},
    {"name": "digest", "title": "Периодическая сводка",
     "description": "Раз в every_minutes присылать в этот чат сводку по стенду: задачи "
                    "трекера по статусам, сколько заведено и закрыто, реплики и расход "
                    "за период. Сводка у чата одна: повторный вызов меняет период. "
                    "Снять — cancel_job.",
     "inputSchema": {"type": "object", "properties": {
         "every_minutes": {**MINUTES, "description": "период сводки в минутах"},
     }, "required": ["every_minutes"]},
     "run": digest},
    {"name": "list_jobs", "title": "Задания планировщика",
     "description": "Активные задания этого чата: напоминания и сводка, сроки, "
                    "периоды и сколько раз сработали.",
     "inputSchema": {"type": "object", "properties": {}},
     "run": list_jobs},
    {"name": "cancel_job", "title": "Снять задание",
     "description": "Снять задание планировщика. Номер — из list_jobs.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "номер задания"},
     }, "required": ["id"]},
     "run": cancel_job},
    {"name": "summary", "title": "Сводка за период",
     "description": "Агрегат по сохранённым срабатываниям этого чата за последние hours "
                    "часов: сколько было сводок и напоминаний, что изменилось на стенде "
                    "за это время, текущее состояние и активные задания. Отвечает сразу, "
                    "ничего не ждёт.",
     "inputSchema": {"type": "object", "properties": {
         "hours": {"type": "integer", "minimum": 1,
                   "description": "за сколько последних часов; по умолчанию 24"},
     }},
     "run": summary},
]
