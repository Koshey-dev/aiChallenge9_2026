"""Оркестрация коробки: вход через политику, работа, выход через политику, судья.

Наружу торчит один метод `ask`. Он отдаёт события, а не голый текст: слой HTTP
переводит их в ndjson и ничего не знает ни про роли, ни про политики, ни про то,
сколько агентов работало под капотом.
"""

import asyncio
import time

from . import crew, judge, policy
from .llm import AgentError, new_usage, stream_chat
from .settings import DEFAULTS, coerce


class Agent:
    """Коробка вокруг модели: роль, память, политики, судья, бригада, журнал.

    Живёт между запросами: браузер присылает только новую реплику, историю
    и счётчики агент держит у себя.
    """

    def __init__(self, key, *, url, model, prices=None, settings=None):
        self.key = key
        self.url = url
        self.prices = prices or {}
        self.settings = {**DEFAULTS, "model": model, **coerce(settings)}
        self.history = []
        self.usage = new_usage()
        # Счётчики на начало реплики: разница с ними — расход одного запроса.
        self.before = new_usage()
        self.log = []
        self.results = []
        self.seconds = 0.0

    def configure(self, values):
        """Настройки приходят из браузера с каждой репликой: чужие ключи отсекаются."""
        self.settings.update(coerce(values))

    def forget(self):
        self.history.clear()

    def remembered(self):
        window = max(0, int(self.settings["memory"]))
        return self.history[-window:] if window else []

    def note(self, text):
        self.log.append(text)
        return {"t": "log", "text": text}

    def absorb(self, other):
        """Расход под-агента идёт в общий счёт: считает тот, кто его нанял."""
        for field in self.usage:
            self.usage[field] += other.usage[field]

    def recruit(self, task):
        """Под-агент — та же коробка с настройками по умолчанию, чужой инструкцией
        и без права нанимать своих."""
        return Agent(self.key, url=self.url, model=self.settings["model"],
                     prices=self.prices,
                     settings={"role": task["prompt"], "memory": 0,
                               "crew": False, "judge": False, "log": False})

    def quirks(self):
        """Поля, которых нет в общем OpenAI-протоколе. Уходят только тому провайдеру,
        который их понимает: у DeepSeek рассуждение включено по умолчанию и съедает
        предел токенов раньше, чем начнётся ответ."""
        if not self.settings["model"].startswith("deepseek"):
            return {}
        state = "enabled" if self.settings["thinking"] else "disabled"
        return {"thinking": {"type": state}}

    async def answer(self, client, messages):
        """Один потоковый вызов модели с настройками коробки."""
        async for piece in stream_chat(client, url=self.url, key=self.key,
                                       model=self.settings["model"], messages=messages,
                                       usage=self.usage,
                                       temperature=float(self.settings["temperature"]),
                                       max_tokens=int(self.settings["max_tokens"]),
                                       **self.quirks()):
            yield piece

    async def team_up(self, client, question, tasks):
        """Поднимает агентов, отмечает каждого по мере готовности, ждёт всех."""
        hired = [self.recruit(task) for task in tasks]

        async def work(index, agent):
            await asyncio.sleep(0.4 * index)  # не бьём в лимит провайдера залпом
            text = []
            try:
                async for event in agent.ask(client, question):
                    if event["t"] == "delta":
                        text.append(event["text"])
            except AgentError as error:
                return index, "", str(error)
            return index, "".join(text).strip(), None

        running = [asyncio.create_task(work(index, agent))
                   for index, agent in enumerate(hired)]
        self.results = [(task["title"], "") for task in tasks]

        for done in asyncio.as_completed(running):
            index, text, failure = await done
            self.absorb(hired[index])
            title = tasks[index]["title"]
            self.results[index] = (title, text)
            measured = f"сбой — {failure[:90]}" if failure else f"{len(text)} символов"
            yield self.note(f"агент {index + 1} «{title}»: {measured}")

    async def ask(self, client, text):
        """Полный проход коробки. Отдаёт события: журнал, куски ответа, вердикт."""
        started = time.monotonic()
        self.log = []
        self.results = []
        self.before = dict(self.usage)

        try:
            question, notes = policy.check_input(text, self.settings)
        except policy.Refused as refusal:
            yield self.note(f"политика входа: отказ — {refusal}")
            yield {"t": "blocked", "reason": str(refusal)}
            self.seconds = round(time.monotonic() - started, 1)
            return

        passed = "политика входа: пропущено"
        yield self.note(passed + ("; " + "; ".join(notes) if notes else ""))

        tasks = []
        if self.settings["crew"] and crew.looks_like_crew(question):
            yield self.note("запрос похож на исследование — зову планировщика")
            tasks = await crew.plan(client, url=self.url, key=self.key,
                                    model=self.settings["model"], question=question,
                                    limit=int(self.settings["crew_max"]),
                                    usage=self.usage, **self.quirks())
            yield self.note(f"планировщик: агентов — {len(tasks)}" if tasks
                            else "планировщик: хватит одного агента")

        if tasks:
            async for event in self.team_up(client, question, tasks):
                yield event
            yield self.note("свожу результаты в один ответ")
            messages = [{"role": "system", "content": crew.SUMMARY},
                        {"role": "user", "content": crew.digest(question, self.results)}]
        else:
            messages = [{"role": "system", "content": self.settings["role"]},
                        *self.remembered(),
                        {"role": "user", "content": question}]

        raw = []
        async for piece in self.answer(client, messages):
            raw.append(piece)
            yield {"t": "delta", "text": piece}

        answer = "".join(raw).strip()
        clean, notes = policy.clean_output(answer, self.settings, self.settings["role"])
        if notes:
            yield self.note("политика выхода: " + "; ".join(notes))
            yield {"t": "replace", "text": clean}
        else:
            yield self.note("политика выхода: без правок")

        # История пополняется только после успешного ответа: оборванный запрос
        # не должен оставлять в памяти вопрос без ответа.
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": clean})

        if self.settings["judge"] and clean:
            verdict = await judge.review(client, url=self.url, key=self.key,
                                         model=self.settings["model"],
                                         question=question, answer=clean,
                                         usage=self.usage, **self.quirks())
            hit = "ответ по делу" if verdict["answered"] else "ответ мимо вопроса"
            yield self.note(f"судья: {hit}, риск выдумки — {verdict['risk']}")
            yield {"t": "judge", **verdict}

        self.seconds = round(time.monotonic() - started, 1)

    def price_of(self, prompt, completion):
        price = self.prices.get(self.settings["model"])
        if not price:
            return None
        return round(prompt / 1e6 * price[0] + completion / 1e6 * price[1], 6)

    def report(self):
        """Счётчики за весь диалог и отдельно расход последней реплики.

        Реплика может стоить нескольких запросов — планировщик, агенты бригады,
        сводка, судья, — поэтому расход считается разницей, а не по одному вызову.
        """
        turn = {field: self.usage[field] - self.before[field] for field in self.usage}
        return {
            "seconds": self.seconds,
            "turns": len(self.history) // 2,
            "requests": self.usage["requests"],
            "tokens_in": self.usage["prompt"],
            "tokens_out": self.usage["completion"],
            "cost": self.price_of(self.usage["prompt"], self.usage["completion"]),
            "turn": {
                "requests": turn["requests"],
                "tokens_in": turn["prompt"],
                "tokens_out": turn["completion"],
                "cost": self.price_of(turn["prompt"], turn["completion"]),
            },
            "log": self.log,
        }
