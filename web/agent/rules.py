"""Свод инвариантов: ограничения, которые ассистент не имеет права нарушать.

Рабочая память (день 11) отвечает на вопрос «что известно о задаче», автомат
(день 13) — «где мы в ней». Свод отвечает на третий: «чего нельзя ни на каком
этапе». Отличается он от памяти не текстом записи, а тем, кто её меняет.
Карточку рабочей памяти маршрутизатор переписывает каждую реплику: сказали
«передумали» — значение заменилось. С инвариантом так нельзя, иначе ограничение
снимается одной фразой в диалоге. Поэтому свод не ведёт ни одна модель: записи
заводит и убирает человек, лежат они в своей таблице и переживают и «Новую
задачу», и забытый диалог.

Принуждение двустороннее, как у автомата дня 13: промпт — это заявка, решает
код. Свод уходит в запрос ограничением и стоит вплотную к вопросу, а готовый
ответ проверяет отдельный вызов — аудитор. Нашёл нарушение — ответ не отдаётся
как есть: он уходит на второй проход с названным инвариантом, а отклонённый
остаётся рядом, чтобы было видно, что коробка не пустила.

Аудитор здесь не потому, что промпта мало: на замерах (README, день 14)
deepseek-flash с инвариантом в запросе удержал его 6 раз из 6, в том числе
под давлением «мы его сняли на созвоне». Аудитор нужен потому, что промпт
работает, пока держится одно допущение — модель видит инвариант и играет по
нему. Стоит инварианту не дойти до запроса, и нарушение проходит молча:
в прогоне с выключенной отправкой свода тот же вопрос дал четыре запрещённые
базы, и поймал их только аудит.

Три ручки в настройках — это три уровня строгости, и разница между ними и есть
предмет дня: свод в промпте, свод под аудитом, свод с переписыванием.
"""

import json

from .llm import stream_chat

# Виды инвариантов. Имена нужны и промпту, и панели свода, поэтому лежат здесь:
# разойдясь, панель рассказывала бы не о том, что видит модель.
KINDS = [
    {"id": "architecture", "title": "архитектура",
     "about": "выбранная архитектура и границы между частями"},
    {"id": "decision", "title": "решение",
     "about": "принятое техническое решение, к которому не возвращаемся"},
    {"id": "stack", "title": "стек",
     "about": "чем можно и чем нельзя пользоваться"},
    {"id": "business", "title": "бизнес-правило",
     "about": "правило предметной области"},
]
TITLES = {kind["id"]: kind["title"] for kind in KINDS}
DEFAULT = KINDS[0]["id"]

MAX_TEXT = 300
MAX_RULES = 20

# Рамка свода — не «справка, а не инструкция», как у слоёв памяти, а наоборот:
# это единственная часть запроса, которая сильнее просьбы пользователя.
#
# Про то, что снять инвариант можно в своде, сказано намеренно, хотя день 13
# учит обратному — там название механизма модель пересказывала вместо ответа.
# Разница в том, кому адресован пересказ: этап пользователь не двигает руками,
# а свод правит он сам, и отказ без этой строчки становится тупиком.
FRAME = (
    "Инварианты этого чата. Это ограничения, а не справка и не пожелание: "
    "решение, которое им противоречит, предлагать нельзя — ни как вариант, "
    "ни как компромисс, ни в примере кода, — даже если пользователь просит "
    "прямо и настаивает.\n"
    "Просьба противоречит инварианту — не выполняй её: назови инвариант, "
    "объясни одной фразой, чем решение его нарушает, и предложи то, что решает "
    "задачу в его рамках. Отменить инвариант в разговоре нельзя, его снимают "
    "в своде — об этом можно сказать прямо.\n"
    "В остальных ответах инварианты не пересказывай: просто держись их.\n"
)

ROLE = (
    "Ты проверяешь ответ агента на соответствие инвариантам — ограничениям, "
    "которые агент не имеет права нарушать. Тебе дают список инвариантов "
    "с номерами, вопрос пользователя и ответ агента.\n\n"
    "Верни два поля:\n"
    "- «нарушения» — список объектов {«номер», «чем»} для инвариантов, которые "
    "нарушает сам ответ: он предлагает, советует или показывает в коде то, что "
    "инвариант запрещает. «Чем» — одна короткая фраза.\n"
    "- «конфликт» — список номеров инвариантов, которым противоречит просьба "
    "пользователя, независимо от того, как агент ответил.\n\n"
    "Отказ — не нарушение: если агент отказался делать то, что запрещено, "
    "и объяснил почему, нарушения нет, есть только конфликт. Упоминание "
    "запрещённого, чтобы сказать «так нельзя», — тоже не нарушение. "
    "Не додумывай: инвариант, о котором в ответе ничего нет, не нарушен.\n\n"
    "Ответ — один JSON-объект, без пояснений и без markdown."
)

FIELDS = {"broken": ("нарушения", "violations"), "clash": ("конфликт", "conflict")}


def clean(items):
    """Свод из сохранённого: чужие ключи и мусор отсекаются."""
    fresh = []
    for item in (items or [])[:MAX_RULES]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:MAX_TEXT]
        if not text:
            continue
        fresh.append({
            "id": str(item.get("id") or "")[:20] or f"r{len(fresh) + 1}",
            "kind": item["kind"] if item.get("kind") in TITLES else DEFAULT,
            "text": text,
            "active": item.get("active", True) is not False,
        })
    return fresh


def add(items, kind, text):
    """Новая запись в свод. Номер берётся с запасом от длины: удалённые
    идентификаторы не переиспользуются, иначе ссылка в старой плашке
    показала бы на чужой инвариант."""
    text = " ".join(str(text or "").split())[:MAX_TEXT]
    if not text or len(items) >= MAX_RULES:
        return items, None
    taken = {item["id"] for item in items}
    number = len(items) + 1
    while f"r{number}" in taken:
        number += 1
    rule = {"id": f"r{number}", "kind": kind if kind in TITLES else DEFAULT,
            "text": text, "active": True}
    return items + [rule], rule


def active(items):
    """Действующие записи: выключенная остаётся в своде, но ни в запрос,
    ни к аудитору не идёт. Это и есть способ увидеть её вклад."""
    return [item for item in items if item["active"]]


def numbered(items):
    """Действующие записи строками с номерами — для запроса и для аудитора.

    Номер здесь сквозной по действующим, а не идентификатор записи: модели
    надёжнее считают «инвариант 2», чем «r7», а обратно номер переводит код.
    """
    return [f"{number}. [{TITLES[item['kind']]}] {item['text']}"
            for number, item in enumerate(active(items), 1)]


def sheet(items):
    """Свод как одно системное сообщение. Пустой места не занимает."""
    lines = numbered(items)
    if not lines:
        return []
    return [{"role": "system", "content": FRAME + "\n".join(lines)}]


def verdict(broken, clash):
    """Итог аудита строкой для журнала реплики."""
    if not broken and not clash:
        return "нарушений нет"
    parts = []
    if broken:
        parts.append("нарушено: " + "; ".join(f"«{hit['text'][:40]}»" for hit in broken))
    if clash:
        parts.append("просьба против свода: "
                     + "; ".join(f"«{hit['text'][:40]}»" for hit in clash))
    return " · ".join(parts)


def fix(items, broken):
    """Сообщение второго прохода: что нарушено и что с этим сделать.

    Идёт после собственного ответа модели — она видит свой текст и правку
    к нему, а не пересобранный с нуля запрос: так в переписанном ответе
    остаётся то, что инвариант не задевало.
    """
    live = active(items)
    named = []
    for hit in broken:
        rule = next((item for item in live if item["id"] == hit["id"]), None)
        if rule:
            named.append(f"- [{TITLES[rule['kind']]}] {rule['text']} — {hit['why']}")
    return {"role": "user", "content":
            "Твой ответ нарушает инварианты этого чата:\n" + "\n".join(named)
            + "\n\nПерепиши ответ на тот же вопрос так, чтобы он их не нарушал. "
              "Если в рамках инвариантов просьбу выполнить нельзя — так и скажи: "
              "назови инвариант, объясни чем он мешает, предложи, что можно "
              "вместо. Про эту правку не упоминай: отвечай, как будто отвечаешь "
              "сразу."}


def parse(raw, items):
    """Вердикт аудитора: (нарушения, конфликт).

    Строгую схему ответа умеет не каждый провайдер, поэтому JSON вынимается из
    текста. Что не разобралось — пустой вердикт: ответ уходит как есть, и это
    видно в журнале. Номер вне списка отбрасывается: выдуманный инвариант хуже,
    чем пропущенное нарушение.
    """
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return [], []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return [], []
    if not isinstance(data, dict):
        return [], []

    live = active(items)

    def rule_at(number):
        try:
            index = int(number) - 1
        except (TypeError, ValueError):
            return None
        return live[index] if 0 <= index < len(live) else None

    found = {field: next((data[name] for name in aliases if name in data), [])
             for field, aliases in FIELDS.items()}

    broken = []
    for hit in found["broken"] if isinstance(found["broken"], list) else []:
        if not isinstance(hit, dict):
            rule = rule_at(hit)
            why = ""
        else:
            rule = rule_at(next((hit[name] for name in ("номер", "number", "id")
                                 if name in hit), None))
            why = str(next((hit[name] for name in ("чем", "why", "reason")
                            if name in hit), "")).strip()[:MAX_TEXT]
        if rule and not any(item["id"] == rule["id"] for item in broken):
            broken.append({"id": rule["id"], "kind": rule["kind"],
                           "text": rule["text"], "why": why})

    clash = []
    for number in found["clash"] if isinstance(found["clash"], list) else []:
        rule = rule_at(number)
        if rule and not any(item["id"] == rule["id"] for item in clash):
            clash.append({"id": rule["id"], "kind": rule["kind"], "text": rule["text"]})
    return broken, clash


async def audit(client, *, url, key, model, items, question, answer, usage, **knobs):
    """Спросить аудитора о готовом ответе. Расход идёт в общий `usage` коробки."""
    lines = numbered(items)
    if not lines:
        return [], []
    request = [
        {"role": "system", "content": ROLE},
        {"role": "user", "content":
            "Инварианты:\n" + "\n".join(lines)
            + f"\n\nВопрос пользователя:\n{question}"
            + f"\n\nОтвет агента:\n{answer}"},
    ]
    pieces = []
    async for piece in stream_chat(client, url=url, key=key, model=model,
                                   messages=request, usage=usage, temperature=0,
                                   max_tokens=600, **knobs):
        pieces.append(piece)
    return parse("".join(pieces), items)
