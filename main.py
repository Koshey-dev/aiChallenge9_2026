import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["GEMINI_API_KEY"]
URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemini-3.5-flash-lite"

print(f"Чат с моделью {MODEL}")
print(f"Запросы идут по REST API: POST {URL}")
print("Пустая строка или exit — выход.\n")

messages = []

while True:
    prompt = input("> ").strip()
    if prompt in ("", "exit"):
        break

    messages.append({"role": "user", "content": prompt})

    response = requests.post(
        URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": MODEL, "messages": messages, "stream": True},
        stream=True,
        timeout=60,
    )
    response.raise_for_status()

    answer = ""
    for raw_line in response.iter_lines():
        line = raw_line.decode("utf-8")
        if not line.startswith("data: "):
            continue

        chunk = line.removeprefix("data: ")
        if chunk == "[DONE]":
            break

        delta = json.loads(chunk)["choices"][0]["delta"].get("content") or ""
        print(delta, end="", flush=True)
        answer += delta

    messages.append({"role": "assistant", "content": answer})
    print("\n")
