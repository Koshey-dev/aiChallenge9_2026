import os

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["GEMINI_API_KEY"]
URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemini-2.5-flash"

prompt = input("> ")

response = requests.post(
    URL,
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={"model": MODEL, "messages": [{"role": "user", "content": prompt}]},
    timeout=60,
)
response.raise_for_status()
print(response.json()["choices"][0]["message"]["content"])
