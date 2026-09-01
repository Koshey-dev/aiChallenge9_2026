import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

API_KEY = os.environ["GEMINI_API_KEY"]
URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemini-3.5-flash-lite"

BOLD = "\033[1m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"

QUESTION = "Объясни, что такое REST API."

FORMAT_RULES = (
    "Отвечай строго списком из трёх пунктов, каждый с новой строки в виде «- текст». "
    "Каждый пункт — одно предложение не длиннее 12 слов. "
    "После третьего пункта выведи отдельной строкой ###КОНЕЦ и больше ничего."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "points": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 3,
        }
    },
    "required": ["points"],
}


def body(system=None, **knobs):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": QUESTION})
    return {"model": MODEL, "messages": messages, "temperature": 0.2, **knobs}


LEVELS = [
    (
        "L0 — без ограничений",
        "Только вопрос. Формат и длину модель выбирает сама.",
        body(),
    ),
    (
        "L1 — жёсткий лимит длины",
        "max_tokens, и ни слова в промпте. Сервер режет по-живому.",
        body(max_tokens=60),
    ),
    (
        "L2 — ограничения в промпте",
        "Формат, длина и стоп-слово описаны словами. Просьба, а не приказ.",
        body(FORMAT_RULES),
    ),
    (
        "L3 — промпт плюс параметры API",
        "То же самое, но подкреплено max_tokens и stop.",
        body(FORMAT_RULES, max_tokens=200, stop=["###КОНЕЦ"]),
    ),
    (
        "L4 — жёсткая схема ответа",
        "response_format: сервер обязан вернуть JSON по схеме.",
        body(
            "Ответь на вопрос тремя короткими пунктами.",
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": SCHEMA},
            },
            max_tokens=300,
        ),
    ),
]


def ask(payload):
    for attempt in range(3):
        response = requests.post(
            URL,
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={**payload, "stream": True},
            stream=True,
            timeout=60,
        )
        if response.status_code in (429, 503) and attempt < 2:
            print(f"{DIM}{response.status_code} от сервера, повтор…{RESET}")
            time.sleep(2 * (attempt + 1))
            continue
        response.raise_for_status()
        return read_stream(response)


def read_stream(response):
    answer = ""
    finish = None

    for raw_line in response.iter_lines():
        line = raw_line.decode("utf-8")
        if not line.startswith("data: "):
            continue

        chunk = line.removeprefix("data: ")
        if chunk == "[DONE]":
            break

        choices = json.loads(chunk).get("choices")
        if not choices:
            continue

        delta = choices[0]["delta"].get("content") or ""
        print(delta, end="", flush=True)
        answer += delta
        finish = choices[0].get("finish_reason") or finish

    return answer, finish


def show_knobs(payload):
    knobs = {
        key: value
        for key, value in payload.items()
        if key in ("max_tokens", "stop", "response_format")
    }
    system = payload["messages"][0]["role"] == "system"
    print(f"{DIM}system-промпт: {'да' if system else 'нет'}")
    print(f"параметры API: {json.dumps(knobs, ensure_ascii=False) or 'нет'}{RESET}\n")


print(f"\n{BOLD}{CYAN}Один вопрос, четыре уровня контроля ответа{RESET}")
print(f"{DIM}POST {URL}")
print(f'Вопрос: «{QUESTION}»{RESET}\n')
input(f"{DIM}Enter — начать…{RESET}")

rows = []

for number, (title, note, payload) in enumerate(LEVELS, start=1):
    print(f"\n{BOLD}{CYAN}{title}{RESET}")
    print(f"{DIM}{note}{RESET}\n")
    show_knobs(payload)

    answer, finish = ask(payload)
    print()
    print(
        f"\n{YELLOW}символов: {len(answer)}   строк: {len(answer.splitlines())}"
        f"   finish_reason: {finish}{RESET}"
    )
    rows.append((title, len(answer), len(answer.splitlines()), finish))

    following = "сводка" if number == len(LEVELS) else LEVELS[number][0]
    input(f"\n{DIM}Enter — {following}…{RESET}")

print(f"\n{BOLD}{CYAN}Сводка{RESET}\n")
print(f"{'уровень':<34}{'символов':>10}{'строк':>8}   finish_reason")
for title, chars, lines, finish in rows:
    print(f"{title:<34}{chars:>10}{lines:>8}   {finish}")
print()
