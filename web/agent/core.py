"""Оркестрация коробки: вход через политику, работа, выход через политику, судья.

Наружу торчит один метод `ask`. Он отдаёт события, а не голый текст: слой HTTP
переводит их в ndjson и ничего не знает ни про роли, ни про политики, ни про то,
сколько агентов работало под капотом.
"""

import asyncio
import time

from . import crew, judge, policy, recap, tokens
from .llm import AgentError, new_usage, stream_chat
from .settings import DEFAULTS, coerce


class Agent:
    """Коробка вокруг модели: роль, память, политики, судья, бригада, журнал.

    Живёт между запросами: браузер присылает только новую реплику, историю
    и счётчики агент держит у себя. Где они лежат между перезапусками, коробка
    не знает: она отдаёт состояние в `state()` и принимает обратно в `restore()`.
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
        # Балласт — синтетическая история на столько токенов. Нужен, чтобы
        # дойти до предела контекста, не оплачивая сотню настоящих реплик.
        self.ballast = 0
        # Расход по репликам: из него видно, как растут токены и цена.
        self.ledger = []
        # Во сколько раз факт от провайдера разошёлся с оценкой по символам.
        self.scale = 1.0
        # Конспект истории и сколько сообщений с начала диалога в него свёрнуто.
        # Сам конспект живёт отдельно от истории: история — что было сказано,
        # конспект — что от неё уходит в запрос вместо этого.
        self.summary = ""
        self.folded = 0

    def configure(self, values):
        """Настройки приходят из браузера с каждой репликой: чужие ключи отсекаются."""
        self.settings.update(coerce(values))

    def forget(self):
        self.history.clear()
        self.summary = ""
        self.folded = 0

    def remembered(self):
        """Реплики, которые уходят в запрос дословно.

        Без сжатия это окно памяти, всё за ним теряется. Со сжатием окно — нижняя
        граница: что свёрнуто в конспект, второй раз не отправляется, а что за
        окном, но ещё не свёрнуто, едет как есть — иначе реплика пропадала бы
        в промежутке между двумя пересборками конспекта.
        """
        if self.settings["compress"]:
            return self.history[self.folded:]
        window = max(0, int(self.settings["memory"]))
        return self.history[-window:] if window else []

    def briefing(self):
        """Конспект как одно системное сообщение. Пустой места не занимает.

        Решение, уходит ли он в запрос, принимается на месте сборки: в шапке
        колонки конспект показан и после того, как сжатие выключили.
        """
        if not self.summary:
            return []
        return [{"role": "system", "content": recap.FRAME + self.summary}]

    def padding(self):
        """Балласт как пара реплик: занимает контекст, не тратя запросов к модели."""
        if self.ballast <= 0:
            return []
        half = self.ballast // 2
        return [{"role": "user", "content": tokens.filler(half)},
                {"role": "assistant", "content": tokens.filler(self.ballast - half)}]

    def state(self):
        """Всё, что стоит пережить перезапуск: диалог, счётчики, журнал расхода.

        Настройки сюда не идут — они приходят из браузера с каждой репликой,
        и хранить их вторым экземпляром значит рано или поздно разойтись с ним.
        """
        return {"history": self.history, "usage": self.usage,
                "ballast": self.ballast, "ledger": self.ledger, "scale": self.scale,
                "summary": self.summary, "folded": self.folded}

    def restore(self, state):
        """Поднять диалог из сохранённого состояния — как будто не выключались."""
        saved = state.get("usage") or {}
        self.history = list(state.get("history") or [])
        self.usage = {field: int(saved.get(field, 0)) for field in new_usage()}
        self.before = dict(self.usage)
        self.ballast = int(state.get("ballast") or 0)
        self.ledger = list(state.get("ledger") or [])
        self.scale = float(state.get("scale") or 1.0)
        self.summary = str(state.get("summary") or "")
        self.folded = int(state.get("folded") or 0)

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

    def weigh(self, pieces):
        """Оценка запроса до отправки: во что обходится каждая его часть.

        Точное число придёт от провайдера в `usage` вместе с ответом. Здесь —
        оценка по символам, поправленная коэффициентом, который коробка сняла
        с прошлых ответов: по ней решается, влезет ли запрос в контекст.
        """
        parts = {name: tokens.of(messages) for name, messages in pieces.items()}
        total = sum(parts.values())
        return {
            **parts,
            "total": total,
            "predicted": round(total * self.scale),
            "scale": self.scale,
            "history": tokens.of(self.history),
            "reply_max": int(self.settings["max_tokens"]),
            "limit": int(self.settings["context_limit"]),
        }

    def over(self, budget):
        """Не влезает ли запрос вместе с местом, зарезервированным под ответ."""
        limit = budget["limit"]
        return bool(limit) and budget["predicted"] + budget["reply_max"] > limit

    def savings(self):
        """Сколько токенов снимает конспект: свёрнутые реплики против него самого.

        Точка отсчёта — вся история: без сжатия те же реплики либо ушли бы
        в запрос целиком и платно, либо потерялись бы вместе со своими фактами.
        """
        if not (self.settings["compress"] and self.folded):
            return 0
        return max(0, tokens.of(self.history[:self.folded])
                   - tokens.of(self.briefing()))

    def record(self, budget):
        """Строка в журнал расхода: что стоила реплика и во что обошёлся диалог.

        Заодно калибровка: оценка сверяется с фактом только там, где запрос был
        один — у бригады и судьи в факт попадает расход чужих вызовов.
        """
        spent = {field: self.usage[field] - self.before[field] for field in self.usage}
        if spent["requests"] == 1 and budget["total"]:
            self.scale = round(spent["prompt"] / budget["total"], 2)
        self.ledger.append({
            "turn": len(self.history) // 2,
            "estimated": budget["predicted"],
            "tokens_in": spent["prompt"],
            "tokens_out": spent["completion"],
            "history": tokens.of(self.history),
            "cost": self.price_of(spent["prompt"], spent["completion"]),
            "total_cost": self.price_of(self.usage["prompt"], self.usage["completion"]),
            "saved": self.savings(),
        })

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

    async def compact(self, client):
        """Свернуть в конспект реплики, вышедшие за окно памяти.

        Сворачивается только то, что уже за окном: реплики внутри окна модель
        должна видеть дословно. Пачка меньше порога — конспект не пересобираем,
        иначе отдельный вызов на каждую реплику съел бы всю экономию.
        """
        window = max(0, int(self.settings["memory"]))
        edge = max(0, len(self.history) - window)
        batch = self.history[self.folded:edge]
        if len(batch) < max(1, int(self.settings["compress_every"])):
            return

        before = tokens.of(self.history[:edge])
        text = await recap.fold(client, url=self.url, key=self.key,
                                model=self.settings["model"], summary=self.summary,
                                messages=batch, usage=self.usage,
                                limit=int(self.settings["summary_max"]),
                                **self.quirks())
        if not text:
            # Пустой конспект вместо реплик — потеря фактов без всякой экономии.
            yield self.note("сжатие: конспект пришёл пустым — история не свёрнута")
            return

        self.summary = text
        self.folded = edge
        yield self.note(f"сжатие: в конспекте ~{tokens.of(self.briefing())} ток. "
                        f"вместо ~{before} — свёрнуто сообщений: {self.folded}")

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

        if self.settings["compress"] and not tasks:
            async for event in self.compact(client):
                yield event

        # Запрос собирается по частям, а не одним списком: каждую часть коробка
        # взвешивает отдельно и показывает, из чего сложился расход токенов.
        if tasks:
            async for event in self.team_up(client, question, tasks):
                yield event
            yield self.note("свожу результаты в один ответ")
            pieces = {
                "role": [{"role": "system", "content": crew.SUMMARY}],
                "summary": [],
                "ballast": [],
                "memory": [],
                "question": [{"role": "user",
                              "content": crew.digest(question, self.results)}],
            }
        else:
            pieces = {
                "role": [{"role": "system", "content": self.settings["role"]}],
                "summary": self.briefing() if self.settings["compress"] else [],
                "ballast": self.padding(),
                "memory": list(self.remembered()),
                "question": [{"role": "user", "content": question}],
            }

        budget = self.weigh(pieces)
        if self.settings["context_guard"]:
            dropped = 0
            if self.settings["trim_history"]:
                # Режем парами: реплика пользователя без ответа модели в памяти
                # только путает и модель, и того, кто читает журнал.
                while self.over(budget) and pieces["memory"]:
                    del pieces["memory"][:2]
                    dropped += 2
                    budget = self.weigh(pieces)
                if dropped:
                    yield self.note("страж контекста: память обрезана до "
                                    f"{len(pieces['memory'])} сообщений — иначе "
                                    "запрос не влезал в предел")
            if self.over(budget):
                reason = (f"контекст переполнен: запрос ~{budget['predicted']} токенов "
                          f"плюс {budget['reply_max']} на ответ при пределе "
                          f"{budget['limit']}")
                yield self.note("страж контекста: отказ — " + reason)
                yield {"t": "budget", **budget}
                yield {"t": "blocked", "reason": reason}
                self.seconds = round(time.monotonic() - started, 1)
                return

        yield {"t": "budget", **budget}
        messages = [*pieces["role"], *pieces["summary"], *pieces["ballast"],
                    *pieces["memory"], *pieces["question"]]

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

        self.record(budget)
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
            # Токены всей истории — не то же самое, что сумма `tokens_in`: та
            # растёт с каждой репликой, потому что история уходит заново.
            "history_tokens": tokens.of(self.history),
            "summary": self.summary,
            "summary_tokens": tokens.of(self.briefing()),
            "folded": self.folded,
            "saved": self.savings(),
            "ballast": self.ballast,
            "context": int(self.settings["context_limit"]),
            "scale": self.scale,
            "ledger": self.ledger,
            "log": self.log,
        }
