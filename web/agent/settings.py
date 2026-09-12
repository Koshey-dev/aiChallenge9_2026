"""Параметры коробки: значения по умолчанию и описание полей для интерфейса.

Один список `BLOCKS` — единственный источник правды: из него берутся и значения
по умолчанию, и типы для приведения, и форма настроек в браузере.
"""

import copy

ROLE = (
    "Ты — ассистент учебного стенда AI Challenge. Отвечай по делу: без вступлений, "
    "без пересказа вопроса, два-три абзаца максимум. "
    "Если вопрос читается по-разному — назови прочтения и спроси, какое имелось в виду. "
    "Если чего-то не знаешь — скажи прямо, не придумывай."
)

BLOCKS = [
    {
        "title": "Модель",
        "note": "Куда и как уходит запрос.",
        "fields": [
            {"key": "model", "label": "Модель", "type": "text", "default": ""},
            {"key": "temperature", "label": "Температура", "type": "number",
             "default": 0.3, "hint": "0 — предсказуемо, выше 1 — разнообразнее"},
            {"key": "max_tokens", "label": "Предел токенов ответа", "type": "number",
             "default": 1500},
            {"key": "thinking", "label": "Рассуждение модели (DeepSeek)", "type": "bool",
             "default": False,
             "hint": "включено — рассуждение съедает предел токенов раньше ответа, "
                     "поднимайте предел до 6000 и выше"},
        ],
    },
    {
        "title": "Роль и память",
        "note": "Что уходит первым сообщением и сколько прошлого агент берёт с собой.",
        "fields": [
            {"key": "role", "label": "Системный промпт", "type": "textarea",
             "default": ROLE},
            {"key": "memory", "label": "Сколько последних реплик помнить",
             "type": "number", "default": 20},
        ],
    },
    {
        "title": "Сжатие истории",
        "note": "Что делать с репликами, которые вышли за окно памяти: потерять "
                "или свернуть в конспект и подставлять его вместо них.",
        "fields": [
            {"key": "compress", "label": "Сжимать историю в конспект", "type": "bool",
             "default": False,
             "hint": "выключено — всё, что вышло за окно памяти, просто теряется"},
            {"key": "compress_every", "label": "Через сколько сообщений пересобирать",
             "type": "number", "default": 10,
             "hint": "конспект стоит отдельного запроса, поэтому собирается пачками"},
            {"key": "summary_max", "label": "Предел конспекта, символов",
             "type": "number", "default": 1200},
        ],
    },
    {
        "title": "Токены и контекст",
        "note": "Сколько токенов уходит в запросе и что делать, когда они перестают "
                "влезать в контекст модели.",
        "fields": [
            {"key": "context_limit", "label": "Предел контекста, токенов",
             "type": "number", "default": 0,
             "hint": "0 — не проверять; предел модели подставляется стендом"},
            {"key": "context_guard", "label": "Проверять предел до запроса",
             "type": "bool", "default": True,
             "hint": "выключено — запрос уходит как есть, и предел ловит провайдер"},
            {"key": "trim_history", "label": "Резать память, когда не влезает",
             "type": "bool", "default": True,
             "hint": "выключено — коробка отказывает, а не теряет реплики молча"},
        ],
    },
    {
        "title": "Политика входа",
        "note": "Отрабатывает до вызова модели. Отказ виден в чате.",
        "fields": [
            {"key": "input_max", "label": "Максимум символов в запросе",
             "type": "number", "default": 2000},
            {"key": "input_ban", "label": "Стоп-слова через запятую", "type": "text",
             "default": "пароль, номер карты, cvv"},
            {"key": "input_guard", "label": "Ловить попытки вытащить системный промпт",
             "type": "bool", "default": True},
        ],
    },
    {
        "title": "Политика выхода",
        "note": "Отрабатывает на готовом ответе. Если политика вмешалась, текст в чате заменяется очищенным.",
        "fields": [
            {"key": "output_max", "label": "Максимум символов в ответе",
             "type": "number", "default": 4000},
            {"key": "mask_secrets", "label": "Маскировать ключи и токены",
             "type": "bool", "default": True},
            {"key": "hide_role", "label": "Вырезать пересказ системного промпта",
             "type": "bool", "default": True},
        ],
    },
    {
        "title": "Судья",
        "note": "Отдельный вызов модели проверяет готовый ответ. Удваивает число запросов.",
        "fields": [
            {"key": "judge", "label": "Проверять ответ судьёй", "type": "bool",
             "default": False},
        ],
    },
    {
        "title": "Бригада",
        "note": "Если запрос похож на исследование, коробка поднимает под себя "
                "несколько таких же агентов с настройками по умолчанию.",
        "fields": [
            {"key": "crew", "label": "Разрешить бригаду", "type": "bool", "default": True},
            {"key": "crew_max", "label": "Сколько агентов максимум", "type": "number",
             "default": 3},
        ],
    },
    {
        "title": "Журнал",
        "note": "Что коробка сделала с репликой: политики, план, агенты, судья.",
        "fields": [
            {"key": "log", "label": "Показывать журнал решений в чате", "type": "bool",
             "default": True},
        ],
    },
]

DEFAULTS = {field["key"]: field["default"] for block in BLOCKS for field in block["fields"]}
TYPES = {field["key"]: field["type"] for block in BLOCKS for field in block["fields"]}


def blocks(model, context=0, limit=0):
    """Описание настроек для интерфейса. Модель, её предел контекста и предел,
    с которым стартует стенд, приходят снаружи: коробка не решает, какую модель
    подняли и с каким запасом её гоняют, — она только знает, что контекст конечный.
    """
    described = copy.deepcopy(BLOCKS)
    for block in described:
        for field in block["fields"]:
            if field["key"] == "model":
                field["default"] = model
            elif field["key"] == "context_limit":
                field["default"] = limit or context
                if context:
                    field["hint"] = (f"предел модели — {context}; стенд стартует "
                                     "с меньшего, чтобы переполнение было видно "
                                     "за пару реплик и стоило копейки")
    return described


def coerce(values):
    """Приводит присланное браузером к типам полей. Чужие ключи выбрасываются:
    настройки приходят снаружи, и подкладывать в них что попало нельзя."""
    clean = {}
    for key, value in (values or {}).items():
        kind = TYPES.get(key)
        if kind is None:
            continue
        try:
            if kind == "number":
                number = float(value)
                clean[key] = int(number) if isinstance(DEFAULTS[key], int) else number
            elif kind == "bool":
                clean[key] = (value.strip().lower() in ("1", "true", "on", "да")
                              if isinstance(value, str) else bool(value))
            else:
                clean[key] = str(value)
        except (TypeError, ValueError):
            continue
    return clean
