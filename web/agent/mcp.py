"""День 16: клиент MCP на голом HTTP — рукопожатие и список инструментов.

SDK здесь нет намеренно: весь стенд ходит в чужие сервисы простым POST, и MCP
не исключение. Под транспортом Streamable HTTP лежит JSON-RPC 2.0: запрос —
это `{"jsonrpc": "2.0", "id": …, "method": …}`, ответ приходит либо телом JSON,
либо потоком SSE — сервер выбирает сам.

Порядок обязателен. Сначала `initialize`: клиент называет версию протокола и
свои возможности, сервер — свои. Затем уведомление `notifications/initialized`:
у него нет `id`, и ответа на него не бывает — только код 202. И лишь потом
`tools/list`. Серверу разрешено выдать на `initialize` идентификатор сессии
заголовком `Mcp-Session-Id` — тогда его нужно повторять в каждом следующем
запросе, иначе разговор не продолжится.

День 17 добавил вызов: `tools/call` с именем инструмента и аргументами.
Ответ — список кусков `content` (текст для модели) и флаг `isError`: инструмент
отработал, но дело не вышло. Это не ошибка протокола, а результат — его читает
модель и может поправиться сама.

День 18 забирает из ответа на `initialize` ещё и `instructions` — подсказку,
которую протокол велит клиенту отдать модели системным сообщением. Свой сервер
стенда пишет в неё «сейчас» планировщика.
"""

import asyncio
import ipaddress
import json
import socket
import time
from urllib.parse import urlparse

import httpx

from . import tokens

# Версия протокола, на которой договариваемся. Сервер вправе ответить своей —
# тогда на экране видно расхождение, а не молчаливое «как-то работает».
VERSION = "2025-06-18"

# Ответ бывает и потоком, поэтому оба типа в Accept: без text/event-stream
# сервер вправе отказать ещё на рукопожатии.
HEAD = {"Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"}

TIMEOUT = 25

# Публичные серверы без ключа и регистрации. Третий отличается от первых двух:
# он требует сессию — на нём видно, зачем нужно рукопожатие.
SERVERS = [
    {"id": "deepwiki", "title": "DeepWiki",
     "url": "https://mcp.deepwiki.com/mcp",
     "note": "документация по репозиториям GitHub, три инструмента"},
    {"id": "context7", "title": "Context7",
     "url": "https://mcp.context7.com/mcp",
     "note": "документация библиотек; два инструмента, но описания длинные"},
    {"id": "gitmcp", "title": "GitMCP",
     "url": "https://gitmcp.io/docs",
     "note": "пять инструментов; без рукопожатия отвечает отказом"},
]

# Что просит показывать шаг: метод, подпись на экране и надо ли ждать ответ.
STEPS = {
    "initialize": "клиент называет версию протокола, сервер — себя и свои возможности",
    "notifications/initialized": "уведомление без id: ответа не ждём, только код",
    "tools/list": "список инструментов, ради которого всё и затевалось",
    "tools/call": "вызов инструмента: имя и аргументы по его схеме",
}

HELLO = {"protocolVersion": VERSION, "capabilities": {},
         "clientInfo": {"name": "aichallenge-stand", "version": "1.0"}}


class McpError(Exception):
    """Запрос даже не ушёл: адрес не тот. Отказ сервера — не он, а шаг с причиной."""


def _unwrap(text):
    """Полезная часть ответа: телом JSON или событием потока SSE с ответом.

    В потоке до ответа сервер вправе прислать свои уведомления: DeepWiki на
    долгом вопросе шлёт `notifications/message` о ходе работы. У уведомления
    нет ни `result`, ни `error` — ответ на запрос тот, у кого они есть. Первое
    событие потока — не обязательно ответ (день 20: так терялся текст).
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        return json.loads(text)
    events = [json.loads(line[5:].strip()) for line in text.splitlines()
              if line.startswith("data:")]
    for event in events:
        if isinstance(event, dict) and ("result" in event or "error" in event):
            return event
    return events[0] if events else None


def _public(host):
    """Адрес ведёт наружу, а не внутрь машины и не в локальную сеть."""
    try:
        found = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for *_, sockaddr in found:
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False
    return bool(found)


async def check(url):
    """Адрес пригоден для запроса. Стенд открыт наружу — внутрь ходить нельзя."""
    where = urlparse(url)
    if where.scheme not in ("http", "https") or not where.hostname:
        raise McpError("адрес должен начинаться с http:// или https://")
    if not await asyncio.to_thread(_public, where.hostname):
        raise McpError("адрес ведёт внутрь сети — стенд ходит только наружу")


async def _call(client, url, method, params, session, *, notify=False):
    """Один вызов JSON-RPC. Возвращает шаг так, как он ляжет на экран."""
    sent = {"jsonrpc": "2.0", "method": method}
    if not notify:
        sent["id"] = 1
    sent["params"] = params
    head = dict(HEAD, **{"MCP-Protocol-Version": VERSION})
    if session:
        head["Mcp-Session-Id"] = session
    step = {"method": method, "note": STEPS.get(method, ""), "sent": sent,
            "session": session, "status": 0, "ms": 0, "got": None, "error": ""}
    began = time.monotonic()
    try:
        res = await client.post(url, json=sent, headers=head)
    except httpx.HTTPError as bad:
        step["ms"] = round((time.monotonic() - began) * 1000)
        step["error"] = f"{type(bad).__name__}: {bad}"
        return step, ""
    step["ms"] = round((time.monotonic() - began) * 1000)
    step["status"] = res.status_code
    try:
        step["got"] = _unwrap(res.text)
    except json.JSONDecodeError:
        step["error"] = "ответ не разобрать как JSON"
        step["got"] = {"raw": res.text[:400]}
    if isinstance(step["got"], dict) and step["got"].get("error"):
        step["error"] = step["got"]["error"].get("message", "ошибка сервера")
    elif res.status_code >= 400 and not step["error"]:
        step["error"] = f"сервер ответил {res.status_code}"
    return step, res.headers.get("mcp-session-id", "")


def _args(schema):
    """Аргументы инструмента из его JSON-схемы — плоским списком для таблицы."""
    props = (schema or {}).get("properties") or {}
    must = set((schema or {}).get("required") or [])
    rows = []
    for name, about in props.items():
        kind = about.get("type") or ("|".join(
            one.get("type", "?") for one in about.get("anyOf", [])) or "?")
        rows.append({"name": name, "type": kind, "required": name in must,
                     "note": (about.get("description") or "").strip()})
    return rows


def _tool(raw):
    """Инструмент для экрана: описание, аргументы и чего он стоит в запросе."""
    body = json.dumps(raw, ensure_ascii=False)
    return {
        "name": raw.get("name", "—"),
        "title": (raw.get("title") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "args": _args(raw.get("inputSchema")),
        "chars": len(body),
        "tokens": tokens.estimate(body),
        "raw": raw,
    }


async def probe(url, *, shake=True, client=None):
    """Соединиться и получить список инструментов. Отдаёт весь разговор целиком.

    `shake=False` пропускает рукопожатие — на серверах с сессией видно, что
    список так не получить: порядок в протоколе не формальность.

    Сорвавшийся шаг ответ не отменяет: разговор возвращается вместе с причиной,
    иначе самое интересное — на чём именно оборвалось — осталось бы за кадром.

    `client` приходит только для своего сервера стенда (день 17): он живёт
    в этом же процессе, и запрос к нему идёт в приложение напрямую, минуя
    сеть, — поэтому и проверка адреса ему не нужна.
    """
    if client is None:
        await check(url)
    steps = []
    session = ""
    server = {}
    trouble = ""
    async with (client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)) as client:
        if shake:
            step, given = await _call(client, url, "initialize", HELLO, "")
            steps.append(step)
            if step["error"]:
                return {"url": url, "server": {}, "steps": steps, "tools": [],
                        "total": {"tools": 0, "chars": 0, "tokens": 0},
                        "error": "рукопожатие не прошло: " + step["error"]}
            session = given
            result = (step["got"] or {}).get("result") or {}
            info = result.get("serverInfo") or {}
            server = {
                "name": info.get("name", "—"),
                "version": info.get("version", ""),
                "about": (info.get("description") or "").strip(),
                "protocol": result.get("protocolVersion", ""),
                "abilities": sorted(result.get("capabilities") or {}),
                "session": session,
                "hint": (result.get("instructions") or "").strip(),
            }
            step, _ = await _call(client, url, "notifications/initialized", {},
                                  session, notify=True)
            steps.append(step)
        step, _ = await _call(client, url, "tools/list", {}, session)
        steps.append(step)
    if step["error"]:
        trouble = "список инструментов не пришёл: " + step["error"]
    found = [_tool(one) for one in ((step["got"] or {}).get("result") or {}).get("tools", [])]
    return {
        "url": url,
        "server": server,
        "steps": steps,
        "tools": found,
        "total": {"tools": len(found),
                  "chars": sum(one["chars"] for one in found),
                  "tokens": sum(one["tokens"] for one in found)},
        "error": trouble,
    }


async def connect(client, url):
    """Рукопожатие перед работой: `initialize` и уведомление. Отдаёт сессию
    и `instructions` сервера — подсказку, которую протокол велит клиенту
    отдать модели системным сообщением (день 18: в ней «сейчас» планировщика).

    Сорвалось — `McpError`: без знакомства вызывать инструменты не у кого.
    """
    step, session = await _call(client, url, "initialize", HELLO, "")
    if step["error"]:
        raise McpError("рукопожатие не прошло: " + step["error"])
    await _call(client, url, "notifications/initialized", {}, session, notify=True)
    result = (step["got"] or {}).get("result") or {}
    return session, (result.get("instructions") or "").strip()


async def tools(client, url, session):
    """Список инструментов сервера — как есть, со схемами."""
    step, _ = await _call(client, url, "tools/list", {}, session)
    if step["error"]:
        raise McpError("список инструментов не пришёл: " + step["error"])
    return ((step["got"] or {}).get("result") or {}).get("tools", [])


async def use(client, url, session, name, args):
    """Вызвать инструмент. Отдаёт шаг разговора и текст результата для модели.

    Отказ протокола (нет инструмента, кривые аргументы) тоже уходит модели
    текстом: так она видит свою ошибку и может позвать инструмент правильно.
    """
    step, _ = await _call(client, url, "tools/call",
                          {"name": name, "arguments": args}, session)
    result = (step["got"] or {}).get("result") or {}
    if step["error"]:
        text = "ошибка: " + step["error"]
    else:
        text = "\n".join(part.get("text", "") for part in result.get("content", [])
                         if part.get("type") == "text")
        if result.get("isError"):
            step["error"] = text or "инструмент сообщил об ошибке"
            text = "ошибка: " + text
    return step, text


def function(tool):
    """Инструмент MCP в том виде, в каком его ждёт OpenAI-совместимый чат.

    Схема аргументов переносится как есть: `inputSchema` у MCP — та же
    JSON Schema, что `parameters` у функции. Пустую схему провайдеры не
    принимают, поэтому у инструмента без аргументов — пустой объект.
    """
    return {"type": "function", "function": {
        "name": tool["name"],
        "description": (tool.get("description") or "").strip(),
        "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
    }}
