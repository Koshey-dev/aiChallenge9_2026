"""Реестр моделей для страницы сравнения: две шкалы по три модели.

Спецификации проверены 4 сентября 2026:
- Groq отдаёт прайс, контекст и `hugging_face_id` прямо в GET /openai/v1/models;
- у `allam-2-7b` поля `pricing` нет вообще — модель не продаётся, только бесплатный тариф;
- активные параметры gpt-oss взяты из карточки модели на HuggingFace;
- квантизация — из `quantization_config` в config.json репозитория;
- у Google из этого публикуется только контекст, и не в OpenAI-совместимом
  эндпоинте, а в родном GET /v1beta/models полем `inputTokenLimit`; параметров и
  квантизации нет нигде, поэтому в карточках стоит None — это не пропуск в данных,
  а само наблюдение.
"""

URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}

# Прайс за 1M токенов: (вход, выход). None — модель на бесплатном тарифе, цены нет.
MODELS = [
    {
        "key": "allam",
        "id": "allam-2-7b",
        "provider": "groq",
        "scale": "groq",
        "tier": "слабая",
        "title": "ALLaM 2 7B",
        "params": "7B",
        "active": None,
        "context": 4096,
        "quant": None,
        "price": None,
        "hf": "https://huggingface.co/ALLaM-AI/ALLaM-2.0-7B-Instruct",
    },
    {
        "key": "oss20",
        "id": "openai/gpt-oss-20b",
        "provider": "groq",
        "scale": "groq",
        "tier": "средняя",
        "title": "GPT-OSS 20B",
        "params": "21B",
        "active": "3.6B",
        "context": 131072,
        "quant": "MXFP4",
        "price": (0.075, 0.30),
        "hf": "https://huggingface.co/openai/gpt-oss-20b",
    },
    {
        "key": "oss120",
        "id": "openai/gpt-oss-120b",
        "provider": "groq",
        "scale": "groq",
        "tier": "сильная",
        "title": "GPT-OSS 120B",
        "params": "117B",
        "active": "5.1B",
        "context": 131072,
        "quant": "MXFP4",
        "price": (0.15, 0.60),
        "hf": "https://huggingface.co/openai/gpt-oss-120b",
    },
    {
        "key": "flite31",
        "id": "gemini-3.1-flash-lite",
        "provider": "gemini",
        "scale": "gemini",
        "tier": "слабая",
        "title": "Gemini 3.1 Flash Lite",
        "params": None,
        "active": None,
        "context": 1048576,
        "quant": None,
        "price": (0.25, 1.50),
        "hf": None,
    },
    {
        "key": "flite35",
        "id": "gemini-3.5-flash-lite",
        "provider": "gemini",
        "scale": "gemini",
        "tier": "средняя",
        "title": "Gemini 3.5 Flash Lite",
        "params": None,
        "active": None,
        "context": 1048576,
        "quant": None,
        "price": (0.30, 2.50),
        "hf": None,
    },
    {
        "key": "flash35",
        "id": "gemini-3.5-flash",
        "provider": "gemini",
        "scale": "gemini",
        "tier": "сильная",
        "title": "Gemini 3.5 Flash",
        "params": None,
        "active": None,
        "context": 1048576,
        "quant": None,
        "price": (1.50, 9.00),
        "hf": None,
    },
]

SCALES = [
    {
        "key": "groq",
        "title": "Groq: открытые веса",
        "note": "Параметры, контекст и квантизация опубликованы — «вес» модели проверяется, "
                "а не берётся из прайса. Одно железо на все три, поэтому время сравнимо.",
    },
    {
        "key": "gemini",
        "title": "Google: лесенка по прайсу",
        "note": "Ни параметров, ни квантизации Google не публикует, а контекст у всех "
                "трёх одинаковый. «Слабая» и «сильная» здесь определяются только "
                "ценником и названием — проверить это по спецификациям нечем.",
    },
]

# У обеих задач есть один правильный ответ, и обе с ловушкой: в первой брата легко
# забыть посчитать саму Алису, во второй +25% и −25% не компенсируют друг друга —
# цена падает на 6,25%. Так качество проверяется глазом, а не на вкус.
MODEL_TASKS = [
    {
        "type": "проверяемая",
        "task": "У Алисы четыре брата и одна сестра. Сколько сестёр у брата Алисы? "
                "Дай ответ числом и обоснуй в двух-трёх предложениях.",
    },
    {
        "type": "с ловушкой",
        "task": "Магазин поднял цену на 25%, а потом объявил скидку 25%. "
                "Цена вернулась к исходной? Ответь и покажи расчёт.",
    },
]

RUNS_PER_MODEL = 3
MAX_TOKENS = 1500


def cost_of(model, tokens_in, tokens_out):
    price = model["price"]
    if not price:
        return None
    return tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1]
