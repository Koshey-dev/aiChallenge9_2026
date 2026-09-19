"""Персонализация: анкета профиля как указание модели.

Долговременная память дня 11 — справка: что ассистент узнал о пользователе.
Анкета — другое: её пользователь заполнил сам, и это не сведения, а правила
ответа — обращение, тон, длина, формат, ограничения. Поэтому у неё своя рамка:
указание, которое модель выполняет, а не справка, которую она может учесть.

Поля выбора — не свободный текст: у каждого варианта готовая фраза для модели
и короткая метка для интерфейса. Так видно, что именно ушло в запрос, и два
профиля различаются по одним и тем же осям.
"""

# Пустое значение — «не важно»: в указание такое поле не попадает вовсе.
CHOICES = [
    {"key": "address", "label": "Обращение", "options": [
        {"value": "ты", "label": "на ты",
         "say": "обращайся к пользователю на «ты», даже если он сам пишет на «вы»"},
        {"value": "вы", "label": "на вы",
         "say": "обращайся к пользователю на «вы», даже если он сам пишет на «ты»"},
    ]},
    {"key": "tone", "label": "Тон", "options": [
        {"value": "friendly", "label": "дружелюбно",
         "say": "тон дружелюбный и живой, без канцелярита"},
        {"value": "formal", "label": "по-деловому",
         "say": "тон деловой и сдержанный: без шуток, восклицаний и эмодзи"},
        {"value": "fun", "label": "с юмором", "say": "тон лёгкий, можно пошутить, но по делу"},
    ]},
    {"key": "length", "label": "Длина ответа", "options": [
        {"value": "short", "label": "кратко",
         "say": "отвечай кратко: не больше пяти предложений или пунктов, без вступлений"},
        {"value": "long", "label": "подробно",
         "say": "отвечай подробно: объясняй шаги и причины, приводи пример"},
    ]},
    {"key": "format", "label": "Формат", "options": [
        {"value": "lists", "label": "списки", "say": "оформляй ответ маркированными списками"},
        {"value": "tables", "label": "таблицы",
         "say": "перечни и сравнения давай таблицей в markdown"},
        {"value": "plain", "label": "сплошной текст",
         "say": "пиши сплошным текстом: без списков, заголовков и markdown"},
    ]},
    {"key": "level", "label": "Уровень", "options": [
        {"value": "novice", "label": "новичок",
         "say": "пользователь новичок: объясняй термины простыми словами, без жаргона"},
        {"value": "expert", "label": "эксперт",
         "say": "пользователь эксперт: без азов, термины не расшифровывай"},
    ]},
]

TEXTS = [
    {"key": "name", "label": "Как обращаться", "max": 60, "mark": None,
     "placeholder": "имя или обращение"},
    {"key": "about", "label": "О себе", "max": 600, "mark": "о себе",
     "placeholder": "кто вы, чем заняты, чем пользуетесь"},
    {"key": "limits", "label": "Ограничения", "max": 600, "mark": "ограничения",
     "placeholder": "чего не делать: по строке на ограничение"},
]

OPTIONS = {field["key"]: {option["value"]: option for option in field["options"]}
           for field in CHOICES}

FRAME = ("Персонализация — как этому пользователю отвечать. Это его собственные "
         "настройки: выполняй их в каждом ответе, даже если прошлые ответы в диалоге "
         "звучали иначе. Где они расходятся с общими правилами выше, главнее они; "
         "пожелание, высказанное пользователем позже в разговоре, главнее их.\n")


def clean(card):
    """Анкета из того, что прислал браузер: известные поля и допустимые значения."""
    card = card if isinstance(card, dict) else {}
    tidy = {}
    for field in CHOICES:
        value = str(card.get(field["key"]) or "")
        tidy[field["key"]] = value if value in OPTIONS[field["key"]] else ""
    for field in TEXTS:
        tidy[field["key"]] = str(card.get(field["key"]) or "").strip()[:field["max"]]
    return tidy


def flat(text):
    """Многострочное поле одной строкой: по строке на пункт, через точку с запятой."""
    return "; ".join(line.strip(" -•") for line in text.splitlines() if line.strip(" -•"))


def text(card):
    """Указание без рамки: то, что пользователь увидит в анкете как «уйдёт в запрос»."""
    lines = []
    if card.get("name"):
        lines.append(f"- называй пользователя так: {card['name']}")
    for field in CHOICES:
        option = OPTIONS[field["key"]].get(card.get(field["key"]) or "")
        if option:
            lines.append("- " + option["say"])
    if card.get("about"):
        lines.append("- о пользователе: " + flat(card["about"]))
    if card.get("limits"):
        lines.append("- ограничения: " + flat(card["limits"]))
    return "\n".join(lines)


def sheet(card, limit):
    """Анкета как одно системное сообщение. Пустая места не занимает."""
    body = text(card)
    if not body:
        return []
    return [{"role": "system", "content": FRAME + body[:max(0, int(limit))]}]


def marks(card):
    """Короткие метки для строки «что учтено» под ответом."""
    found = [card["name"]] if card.get("name") else []
    for field in CHOICES:
        option = OPTIONS[field["key"]].get(card.get(field["key"]) or "")
        if option:
            found.append(option["label"])
    found += [field["mark"] for field in TEXTS if field["mark"] and card.get(field["key"])]
    return found
