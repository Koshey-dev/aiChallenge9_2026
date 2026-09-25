"""Оркестрация коробки: вход через политику, работа, выход через политику, судья.

Наружу торчит один метод `ask`. Он отдаёт события, а не голый текст: слой HTTP
переводит их в ndjson и ничего не знает ни про роли, ни про политики, ни про то,
сколько агентов работало под капотом.
"""

import asyncio
import json
import time

from . import crew, facts, judge, layers, mcp, persona, policy, recap, rules, task, tokens
from .llm import AgentError, new_usage, stream_chat
from .settings import DEFAULTS, coerce

# Имя линии, в которой диалог живёт, пока от него не отвели ветку.
MAIN = "основная"

# Сколько раз за реплику модель может сходить к инструментам. Потом — ответ
# без них: зациклившаяся модель не должна жечь запросы без конца. Четырёх
# хватало конвейеру дня 19; длинному флоу дня 20 через четыре сервера нужно
# шесть-семь ходов, если модель зовёт по одному инструменту за ход.
TOOL_ROUNDS = 8


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
        # Карточка липких фактов: ключ — имя факта, значение — строка. Живёт
        # отдельно от истории и переживает любое окно памяти.
        self.facts = {}
        # Модель памяти: рабочая память живёт до новой задачи и едет вместе
        # с веткой, долговременная (профиль) — до кнопки «Забыть профиль».
        # Профиль держится отдельно от состояния диалога: он и не должен
        # исчезать вместе с забытым диалогом, поэтому лежит в своей таблице.
        self.work = {}
        self.profile = {}
        # Где мы в задаче: этап, шаг, ожидаемое действие. Рабочая память
        # говорит, что о задаче известно, автомат — на каком она ходу.
        # Живёт столько же, сколько рабочая память: до «Новой задачи».
        self.task = task.blank()
        # Свод инвариантов: ограничения, которые ответ нарушать не имеет права.
        # Как и профиль, лежит в своей таблице и в состояние диалога не идёт —
        # забытый диалог свод не отменяет.
        self.rules = []
        # Что аудитор нашёл в последнем ответе и каким он был до переписывания.
        self.broken = []
        self.clash = []
        self.rejected = ""
        # Анкета профиля и его название: их заполняет пользователь, в запрос
        # они уходят указанием. Лежат вместе с профилем, а не в диалоге.
        self.persona = {}
        # Что маршрутизатор дописал в профиль на этой реплике и что из профиля
        # ушло в запрос — для плашки и меток под ответом.
        self.fresh = {}
        self.told = {}
        # Куда автомат сдвинулся на этой реплике: строка для плашки над ответом.
        self.moved = None
        # Чем сторож счёл ответ перепрыгнувшим этап и каким ответ был до правки.
        self.jumped = ""
        self.ahead = ""
        # Инструменты MCP (дни 17–18). Ящик с ними даёт стенд: коробка не знает,
        # где сервер и как до него дойти, — только что он умеет и как позвать.
        # Список инструментов берётся у сервера на каждую реплику, вызовы
        # реплики — для карточек в пузыре. С дня 20 серверов несколько, и
        # `servers` — как прошло знакомство с каждым: для полосы маршрута.
        self.toolbox = None
        self.tools = []
        self.hint = ""
        self.calls = []
        self.servers = []
        # Ответы для других профилей: номер реплики — список вариантов.
        # В историю они не идут: разговор продолжается от исходного ответа.
        self.variants = {}
        # Ветки диалога: имя активной линии, снимки остальных и контрольная
        # точка — снимок, от которого отводится новая ветка.
        self.branch = MAIN
        self.branches = {}
        self.point = None

    def configure(self, values):
        """Настройки приходят из браузера с каждой репликой: чужие ключи отсекаются."""
        self.settings.update(coerce(values))

    def forget(self):
        # Профиль здесь не трогаем: забытый диалог — не забытый пользователь.
        self.history.clear()
        self.summary = ""
        self.folded = 0
        self.facts = {}
        self.work = {}
        self.task = task.blank()
        self.variants = {}
        self.branch = MAIN
        self.branches = {}
        self.point = None

    def remembered(self, history=None):
        """Реплики, которые уходят в запрос дословно.

        Без сжатия это окно памяти, всё за ним теряется. Со сжатием окно — нижняя
        граница: что свёрнуто в конспект, второй раз не отправляется, а что за
        окном, но ещё не свёрнуто, едет как есть — иначе реплика пропадала бы
        в промежутке между двумя пересборками конспекта.
        """
        history = self.history if history is None else history
        if self.settings["compress"]:
            return history[self.folded:]
        window = max(0, int(self.settings["memory"]))
        return history[-window:] if window else []

    def briefing(self):
        """Конспект как одно системное сообщение. Пустой места не занимает.

        Решение, уходит ли он в запрос, принимается на месте сборки: в шапке
        колонки конспект показан и после того, как сжатие выключили.
        """
        if not self.summary:
            return []
        return [{"role": "system", "content": recap.FRAME + self.summary}]

    def factsheet(self):
        """Карточка фактов как одно системное сообщение.

        Как и конспект, в запрос уходит по решению места сборки: в шапке колонки
        карточка видна и после того, как стратегию переключили на другую.
        """
        return facts.sheet(self.facts)

    def worksheet(self):
        """Рабочая память как одно системное сообщение."""
        return layers.sheet(layers.WORK_FRAME, self.work)

    def profilesheet(self):
        """Долговременная память как одно системное сообщение."""
        return layers.sheet(layers.PROFILE_FRAME, self.profile)

    def tasksheet(self):
        """Состояние задачи как одно системное сообщение — как в режиме
        планирования. Счётчики показывают вес состояния, а не режим."""
        return task.sheet(self.task, gates=bool(self.settings["task_gates"]))

    def rulesheet(self):
        """Свод как одно системное сообщение. Пустой места не занимает."""
        return rules.sheet(self.rules)

    def newtask(self):
        """Новая задача: рабочая память стирается, профиль и диалог остаются.

        Это и есть граница между слоями: у рабочей памяти срок жизни — одна
        задача, и без способа её закончить она ничем не отличалась бы от
        карточки фактов. Автомат обнуляется вместе с ней: этап и шаги — тоже
        про ту задачу, которая только что закончилась.
        """
        gone = len(self.work)
        self.work = {}
        self.task = task.blank()
        return gone

    def steer(self, act):
        """Ручной переход автомата: ключи ворот, шаг назад и закрытие."""
        self.task, said = task.switch(self.task, act,
                                      gates=bool(self.settings["task_gates"]))
        return said

    def move(self, layer, name, to):
        """Перенос записи между слоями руками; пустой `to` — удаление.

        Маршрутизатор решает, что куда положить, но решает моделью — значит
        ошибается. Разложить руками должно быть можно, иначе неверно понятый
        факт останется в слое навсегда.

        Отдельный получатель — свод: запись рабочей памяти поднимается в
        инвариант. Маршрутизатор уже вытащил «принятые решения» из диалога,
        и повысить решение до ограничения остаётся человеку — модель этого
        сделать не может по устройству свода.
        """
        source = self.work if layer == "work" else self.profile
        if name not in source:
            return False
        value = source.pop(name)
        if to == "work":
            self.work[name] = value
        elif to == "profile":
            self.profile[name] = value
        elif to == "rules":
            self.rules, rule = rules.add(self.rules, "decision", f"{name}: {value}")
            if rule is None:
                # Свод полон: запись возвращается на место, иначе она пропала бы
                # молча — и из памяти, и мимо свода.
                source[name] = value
                return False
        return True

    def bind(self, kind, text):
        """Новый инвариант. Заводит его человек: модель в свод не пишет, иначе
        ограничение снимал бы тот же разговор, который его нарушает."""
        self.rules, rule = rules.add(self.rules, kind, text)
        return rule

    def unbind(self, rule_id):
        """Убрать инвариант из свода. Это тоже делает только человек."""
        before = len(self.rules)
        self.rules = [item for item in self.rules if item["id"] != rule_id]
        return len(self.rules) != before

    def toggle(self, rule_id, on):
        """Выключить инвариант, не убирая: выключенный остаётся в панели, но
        ни в запрос, ни к аудитору не идёт. Так виден вклад одной записи."""
        for item in self.rules:
            if item["id"] == rule_id:
                item["active"] = bool(on)
                return True
        return False

    def snapshot(self):
        """Линия диалога целиком: история и всё, что из неё выведено.

        Ветка — это не только реплики: конспект, карточка фактов и рабочая
        память собраны из них же, и оставить их общими значило бы протащить
        в одну ветку то, что сказали в другой. Профиль сюда не идёт: он про
        пользователя, а не про линию разговора, и общий у всех веток.
        """
        return {"history": list(self.history), "facts": dict(self.facts),
                "summary": self.summary, "folded": self.folded,
                "work": dict(self.work)}

    def mark(self):
        """Контрольная точка: снимок места, от которого разойдутся ветки."""
        self.point = self.snapshot()
        return len(self.point["history"])

    def switch(self, name):
        """Перейти на ветку: текущую линию — в хранилище, запрошенную — в работу.

        Неизвестное имя значит новую ветку: она начинается с контрольной точки,
        то есть видит ровно ту историю, что была на момент её постановки. Точки
        нет — ветка отходит от текущего места, иначе кнопка просто не работала бы.
        """
        self.branches[self.branch] = self.snapshot()
        fresh = name not in self.branches
        line = (self.point or self.snapshot()) if fresh else self.branches[name]
        self.branch = name
        self.history = list(line["history"])
        self.facts = dict(line["facts"])
        self.summary = line["summary"]
        self.folded = line["folded"]
        self.work = dict(line.get("work") or {})
        return fresh

    def lines(self):
        """Имена ветвей: сохранённые плюс текущая — её в хранилище ещё нет."""
        names = list(self.branches)
        if self.branch not in names:
            names.append(self.branch)
        return names

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
        Долговременная память тоже не идёт: у неё своя таблица, и второй
        экземпляр в строке диалога исчезал бы вместе с забытым диалогом.
        """
        return {"history": self.history, "usage": self.usage,
                "ballast": self.ballast, "ledger": self.ledger, "scale": self.scale,
                "summary": self.summary, "folded": self.folded,
                "facts": self.facts, "work": self.work, "task": self.task,
                "variants": self.variants,
                "branch": self.branch, "branches": self.branches, "point": self.point}

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
        self.facts = dict(state.get("facts") or {})
        self.work = dict(state.get("work") or {})
        self.task = task.clean(state.get("task"))
        self.variants = dict(state.get("variants") or {})
        self.branch = str(state.get("branch") or MAIN)
        self.branches = dict(state.get("branches") or {})
        self.point = state.get("point") or None

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
            "persona": self.told,
            "learned": self.fresh,
            "moved": self.moved,
            # Отклонённый ответ едет в журнал расхода вместе со строкой реплики:
            # иначе после перезагрузки страницы от нарушения осталась бы только
            # плашка, а сравнить было бы не с чем.
            "broken": self.broken,
            "clash": self.clash,
            "rejected": self.rejected,
            "jumped": self.jumped,
            "ahead": self.ahead,
            "calls": self.calls,
            "servers": self.servers,
        })

    async def answer(self, client, messages, wants=None):
        """Один потоковый вызов модели с настройками коробки.

        Инструменты, если они есть, модель видит всегда, а звать может только
        тот вызов, который собирает её просьбы (`wants`). Переписывание и
        вариант идут по разговору, где вызовы уже были, — там список нужен,
        чтобы разговор читался, а новые вызовы запрещены.
        """
        knobs = {}
        if self.tools:
            knobs["tools"] = [mcp.function(tool) for tool in self.tools]
            if wants is None:
                knobs["tool_choice"] = "none"
        async for piece in stream_chat(client, url=self.url, key=self.key,
                                       model=self.settings["model"], messages=messages,
                                       usage=self.usage, wants=wants,
                                       temperature=float(self.settings["temperature"]),
                                       max_tokens=int(self.settings["max_tokens"]),
                                       **knobs, **self.quirks()):
            yield piece

    async def equip(self):
        """Спросить у сервера инструменты на эту реплику. Не вышло — без них.

        Вместе со списком приходит `instructions` сервера: она уходит модели
        системным сообщением рядом с ролью (`layout`), поэтому снаряжение идёт
        до раскладки запроса.

        С дня 20 серверов несколько, и не ответивший не отменяет остальных:
        журнал называет его, а список собирается из тех, кто ответил.
        """
        self.tools = []
        self.hint = ""
        self.servers = []
        if not (self.settings["mcp_tools"] and self.toolbox):
            return
        self.tools, self.hint = await self.toolbox.open()
        self.servers = self.toolbox.servers
        for seen in self.servers:
            if seen["state"] == "down":
                yield self.note(f"сервер {seen['id']} не ответил — {seen['error']}; "
                                "реплика идёт без его инструментов")
        yield {"t": "servers", "servers": self.servers}
        if not self.tools:
            return
        cost = tokens.estimate(json.dumps([mcp.function(tool) for tool in self.tools],
                                          ensure_ascii=False))
        live = ", ".join(f"{seen['id']} {len(seen['tools'])}"
                         for seen in self.servers if seen["tools"])
        off = [seen["id"] for seen in self.servers if seen["state"] == "off"]
        yield self.note(f"инструменты MCP: {len(self.tools)} ({live}), ~{cost} ток. к запросу"
                        + (f"; сняты: {', '.join(off)}" if off else ""))

    async def converse(self, client, sent, raw):
        """Ответ, по ходу которого модель может звать инструменты.

        Модель просит вызов — коробка идёт на сервер, результат уходит модели
        сообщением `tool`, и она отвечает дальше. `sent` пополняется обменом на
        месте: переписывания ниже идут по тому же разговору, иначе модель
        переписывала бы ответ, не видя данных, на которых он построен.
        """
        for turn in range(TOOL_ROUNDS + 1):
            wants = {} if self.tools and turn < TOOL_ROUNDS else None
            said = []
            async for piece in self.answer(client, sent, wants):
                # Текст до вызова и текст после — разные абзацы, а не одна
                # строка: модель начинает новый ход без пробела.
                if not said and "".join(raw).strip():
                    piece = "\n\n" + piece.lstrip()
                said.append(piece)
                raw.append(piece)
                yield {"t": "delta", "text": piece}
            calls = [call for call in (wants or {}).get("calls", [])
                     if call["function"]["name"]]
            if not calls:
                return
            asked = {"role": "assistant", "content": "".join(said) or None,
                     "tool_calls": calls}
            if wants.get("reasoning"):
                asked["reasoning_content"] = wants["reasoning"]
            sent.append(asked)
            for call in calls:
                card, text = await self.run_tool(call, turn + 1)
                yield self.note(f"вызов {card['server'] or '—'} · {card['name']}: "
                                + (f"ошибка — {card['error']}" if card["error"]
                                   else f"{card['status']} за {card['ms']} мс"))
                yield {"t": "tool", **card}
                sent.append({"role": "tool", "tool_call_id": call["id"], "content": text})

    async def run_tool(self, call, round_no):
        """Один вызов по просьбе модели: карточка для пузыря и текст для модели.

        В карточке — сервер, на который вызов ушёл, и ход модели, в котором она
        его попросила: вызовы одного хода модель просила, не видя ответов друг
        друга, и по ходам видно, какой шаг мог опираться на какой (день 20).
        """
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = None
        if isinstance(args, dict):
            step, text = await self.toolbox.call(name, args)
        else:
            step = {"status": 0, "ms": 0, "got": None,
                    "error": "аргументы пришли не объектом JSON"}
            text = "ошибка: " + step["error"]
            args = call["function"]["arguments"]
        result = (step["got"] or {}).get("result") or {}
        card = {"name": name, "args": args,
                "result": result.get("structuredContent", text),
                "error": step["error"], "status": step["status"], "ms": step["ms"],
                "server": step.get("server", ""), "round": round_no}
        self.calls.append(card)
        return card, text

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

    async def pin(self, client, question):
        """Обновить карточку фактов по новой реплике пользователя.

        Вызов на каждую реплику, а не пачкой, как конспект: карточка должна
        помочь ответу на ту самую реплику, из которой её обновили. Это цена
        стратегии — второй запрос к модели на каждый вопрос.
        """
        before = len(self.facts)
        fresh = await facts.update(client, url=self.url, key=self.key,
                                   model=self.settings["model"], facts=self.facts,
                                   question=question, usage=self.usage,
                                   limit=int(self.settings["facts_max"]),
                                   **self.quirks())
        if not fresh:
            # Пустая карточка вместо прошлой — потеря фактов без всякой выгоды.
            yield self.note("факты: карточка не разобралась — оставляю прошлую")
            return

        self.facts = fresh
        yield self.note(f"факты: записей {len(self.facts)} (было {before}), "
                        f"карточка ~{tokens.of(self.factsheet())} ток.")

    async def sort(self, client, question):
        """Разложить новое из реплики по слоям памяти.

        Вызов на каждую реплику, как и карточка фактов: слой должен помочь
        ответу на ту самую реплику, из которой его пополнили. Правила записи
        у слоёв разные — профиль дополняется, рабочая память приходит
        карточкой целиком, — поэтому ответ модели тут не один объект, а два.
        """
        before = dict(self.work)
        added, card = await layers.route(
            client, url=self.url, key=self.key, model=self.settings["model"],
            work=self.work, profile=self.profile, question=question,
            usage=self.usage, work_limit=int(self.settings["work_max"]),
            profile_limit=int(self.settings["profile_max"]),
            persona=persona.text(self.persona), **self.quirks())

        # Пустой слой значит «менять нечего», а не «сотри»: модель отдаёт
        # пустое и когда реплика ничего не добавила, и когда ответ не
        # разобрался. Стирает слой только кнопка.
        if card:
            self.work = card
        fresh = {name: value for name, value in added.items()
                 if self.profile.get(name) != value}
        # Учёба выключена — профиль меняется только руками. Маршрутизатор
        # всё равно зовётся: рабочую память кроме него никто не ведёт.
        asked = len(fresh)
        if not self.settings["learn"]:
            fresh = {}
        fresh = {name: value for name, value in fresh.items()
                 if name in self.profile or len(self.profile) < layers.MAX_KEYS}
        self.profile.update(fresh)
        self.fresh = fresh

        changed = [name for name, value in self.work.items()
                   if before.get(name) != value]
        into = (f"в профиль {len(fresh)} ({layers.names(fresh)})" if self.settings["learn"]
                else f"в профиль не пишу — учёба выключена (просилось {asked})")
        yield self.note(f"маршрут: в рабочую {len(changed)} ({layers.names(changed)}), "
                        + into)

    async def stage(self, client, question):
        """Сдвинуть автомат задачи под новую реплику.

        Отдельный вызов модели до ответа, как у карточки фактов и слоёв: этап
        должен быть верен для того самого ответа, который сейчас уйдёт.
        Зовётся только в режиме планирования (`task`): в режиме общения автомат
        замирает, и реплика стоит на один запрос дешевле.
        """
        claim = await task.track(client, url=self.url, key=self.key,
                                 model=self.settings["model"], state=self.task,
                                 question=question, history=self.history,
                                 usage=self.usage,
                                 steps_max=int(self.settings["task_steps"]),
                                 gates=bool(self.settings["task_gates"]),
                                 **self.quirks())
        self.task, said, moved = task.apply(
            self.task, claim, strict=bool(self.settings["task_strict"]),
            gates=bool(self.settings["task_gates"]))
        self.moved = moved
        yield self.note(said)

    def tell(self, card, profile):
        """Что из профиля ушло в запрос: метки анкеты и число замеченных записей.

        Строится из тех же настроек, что и сам запрос, — метки под ответом
        показывают не профиль вообще, а то, что модель на самом деле получила.
        """
        on = bool(self.settings["send_persona"])
        noticed = (len(profile) if self.settings["strategy"] == "layers"
                   and self.settings["send_profile"] else 0)
        return {"title": str(card.get("title") or ""),
                "marks": persona.marks(card) if on else [],
                "off": not on, "noticed": noticed}

    def layout(self, question, memory, card, profile):
        """Части запроса без бригады: чей профиль и какая память — решает вызывающий.

        Слой заполняется маршрутизатором всегда, а уходит в запрос по галочке:
        заполненный, но отключённый слой — это и есть проверка, на что он
        влияет в ответе. С анкетой так же.
        """
        layered = self.settings["strategy"] == "layers"
        return {
            "role": [{"role": "system", "content": self.settings["role"]}],
            # Подсказка MCP-сервера — что это за инструменты и который час.
            "tools": ([{"role": "system", "content": "Инструменты MCP. " + self.hint}]
                      if self.hint else []),
            "persona": (persona.sheet(card, self.settings["persona_max"])
                        if self.settings["send_persona"] else []),
            "summary": self.briefing() if self.settings["compress"] else [],
            "facts": (self.factsheet()
                      if self.settings["strategy"] == "facts" else []),
            "profile": (layers.sheet(layers.PROFILE_FRAME, profile)
                        if layered and self.settings["send_profile"] else []),
            "work": (self.worksheet()
                     if layered and self.settings["send_work"] else []),
            "task": (task.sheet(self.task, frozen=not self.settings["task"],
                              gates=bool(self.settings["task_gates"]))
                     if self.settings["send_task"] else []),
            "rules": self.rulesheet() if self.settings["send_rules"] else [],
            "note": [{"role": "system", "content": layers.NOTE}] if layered else [],
            "ballast": self.padding(),
            "memory": list(memory),
            "question": [{"role": "user", "content": question}],
        }

    @staticmethod
    def arrange(pieces):
        """Части запроса в том порядке, в каком они уходят модели.

        Слои памяти стоят после окна, прямо перед вопросом. Модель сильнее
        опирается на то, что ближе к вопросу, и справка из слоёв не должна
        проигрывать старой реплике окна, которая ей противоречит: со слоями
        перед окном deepseek-flash повторял свой прошлый ответ «имени нет»
        в 11 прогонах из 12, после окна — ни разу. Цена — кэш провайдера:
        слои идут после растущей истории и каждый раз считаются заново вместе
        с прошлым вопросом. На 14 репликах из кэша 37% входа против 45% при
        слоях перед окном.

        Анкета — там же, перед слоями, по той же причине: после смены профиля
        посреди чата окно полно ответов в чужом стиле. Анкета после окна
        у deepseek-flash дала «списками» в 14 прогонах из 14, сразу после
        роли — в 11; у GPT-OSS и Gemini разницы нет. Кэшу это стоит самой
        анкеты — около 200 токенов на запрос.

        Правило «память ведёшь не ты» (`layers.NOTE`) — сразу после окна:
        ниже прошлых ответов, которым оно противоречит, но выше анкеты и слоёв,
        чтобы не отодвигать их от вопроса. Замер — в `layers.NOTE`.

        Состояние задачи — предпоследним, у самого вопроса: слои отвечают на
        вопрос «что известно», а оно — «что делать этим ходом», и проигрывать
        справке из слоёв это указание не должно.

        Свод — последним, ниже всего остального. Он единственная часть запроса,
        которая сильнее просьбы пользователя, а значит не должен проигрывать
        ни справке слоёв, ни указанию этапа, ни пожеланиям анкеты: «покороче»
        и «только PostgreSQL» — требования разного веса.
        """
        return [*pieces["role"], *pieces["tools"], *pieces["summary"], *pieces["facts"],
                *pieces["ballast"], *pieces["memory"], *pieces["note"],
                *pieces["persona"], *pieces["profile"], *pieces["work"],
                *pieces["task"], *pieces["rules"], *pieces["question"]]

    async def ask(self, client, text):
        """Полный проход коробки. Отдаёт события: журнал, куски ответа, вердикт."""
        started = time.monotonic()
        self.log = []
        self.results = []
        self.fresh = {}
        self.told = {}
        self.moved = None
        self.broken = []
        self.clash = []
        self.rejected = ""
        self.jumped = ""
        self.ahead = ""
        self.calls = []
        self.servers = []
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

        if self.settings["strategy"] == "facts" and not tasks:
            async for event in self.pin(client, question):
                yield event

        if self.settings["strategy"] == "layers" and not tasks:
            async for event in self.sort(client, question):
                yield event

        # Диспетчер после маршрутизатора: этап считается по той же реплике,
        # но с уже пополненной рабочей памятью — она и есть данные задачи.
        if self.settings["task"] and not tasks:
            async for event in self.stage(client, question):
                yield event

        # Запрос собирается по частям, а не одним списком: каждую часть коробка
        # взвешивает отдельно и показывает, из чего сложился расход токенов.
        if tasks:
            async for event in self.team_up(client, question, tasks):
                yield event
            yield self.note("свожу результаты в один ответ")
            pieces = {
                "role": [{"role": "system", "content": crew.SUMMARY}],
                "tools": [],
                "persona": [],
                "summary": [],
                "facts": [],
                "profile": [],
                "work": [],
                "task": [],
                "rules": [],
                "note": [],
                "ballast": [],
                "memory": [],
                "question": [{"role": "user",
                              "content": crew.digest(question, self.results)}],
            }
        else:
            async for event in self.equip():
                yield event
            pieces = self.layout(question, self.remembered(), self.persona, self.profile)
            self.told = self.tell(self.persona, self.profile)

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
        sent = self.arrange(pieces)
        raw = []
        async for event in self.converse(client, sent, raw):
            yield event

        answer = "".join(raw).strip()
        clean, notes = policy.clean_output(answer, self.settings, self.settings["role"])
        if notes:
            yield self.note("политика выхода: " + "; ".join(notes))
            yield {"t": "replace", "text": clean}
        else:
            yield self.note("политика выхода: без правок")

        # Свод инвариантов. Аудитор судит о готовом ответе, а решает код —
        # ровно как с заявкой диспетчера в дне 13. Нашёл нарушение и
        # переписывание разрешено — ответ уходит на второй проход, а
        # отклонённый остаётся рядом: по нему и видно, что коробка не пустила.
        # Проверяется уже очищенный политикой текст: судить надо о том, что
        # увидит пользователь, а не о том, что пришло от провайдера.
        if self.settings["rules_guard"] and clean and rules.active(self.rules):
            self.broken, self.clash = await rules.audit(
                client, url=self.url, key=self.key, model=self.settings["model"],
                items=self.rules, question=question, answer=clean,
                usage=self.usage, **self.quirks())
            yield self.note("аудит свода: " + rules.verdict(self.broken, self.clash))
            if self.broken and self.settings["rules_retry"]:
                self.rejected = clean
                again = []
                async for piece in self.answer(
                        client, [*sent, {"role": "assistant", "content": clean},
                                 rules.fix(self.rules, self.broken)]):
                    again.append(piece)
                fixed, _ = policy.clean_output("".join(again).strip(), self.settings,
                                               self.settings["role"])
                if fixed:
                    clean = fixed
                    yield self.note("свод: ответ переписан")
                    yield {"t": "replace", "text": clean}
                else:
                    # Пустой ответ вместо нарушившего — это не соблюдение
                    # инварианта, а потеря реплики.
                    self.rejected = ""
                    yield self.note("свод: переписанный ответ пришёл пустым — "
                                    "оставляю прежний")

        # Сторож этапа. Ворота держат переходы, но не текст: автомат остаётся
        # на планировании, а ответ уже выдал готовую реализацию. Поэтому
        # проверка идёт после ответа и по тому же очищенному тексту, что
        # увидит пользователь.
        if (self.settings["task"] and self.settings["task_watch"] and clean
                and self.task["stage"] != task.IDLE):
            over, why = await task.watch(
                client, url=self.url, key=self.key, model=self.settings["model"],
                state=self.task, answer=clean, usage=self.usage, **self.quirks())
            self.jumped = (why or "работа следующего этапа") if over else ""
            yield self.note("сторож этапа: "
                            + (f"ответ перепрыгнул этап — {self.jumped}"
                               if self.jumped else "ответ в рамках этапа"))
            if self.jumped:
                self.ahead = clean
                again = []
                async for piece in self.answer(
                        client, [*sent, {"role": "assistant", "content": clean},
                                 task.mend(self.task, self.jumped)]):
                    again.append(piece)
                fixed, _ = policy.clean_output("".join(again).strip(), self.settings,
                                               self.settings["role"])
                if fixed:
                    clean = fixed
                    yield self.note("сторож этапа: ответ переписан под этап")
                    yield {"t": "replace", "text": clean}
                else:
                    self.ahead = ""
                    yield self.note("сторож этапа: переписанный ответ пришёл пустым — "
                                    "оставляю прежний")

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

    async def retell(self, client, card, profile):
        """Последний ответ заново — для другого профиля.

        Всё остальное как у исходного ответа: тот же вопрос, то же окно памяти,
        та же рабочая память. Меняются только анкета и долговременная память,
        поэтому разница между ответами — это и есть вклад профиля. В историю
        вариант не идёт, маршрутизатор не зовётся: профиль чужой, и учить его
        на чужом разговоре незачем.
        """
        if len(self.history) < 2:
            raise AgentError("в чате ещё нет ответа")
        question = self.history[-2]["content"]
        pieces = self.layout(question, self.remembered(self.history[:-2]), card, profile)
        raw = []
        async for piece in self.answer(client, self.arrange(pieces)):
            raw.append(piece)
            yield {"t": "delta", "text": piece}
        clean, _ = policy.clean_output("".join(raw).strip(), self.settings,
                                       self.settings["role"])
        variant = {"persona": self.tell(card, profile), "text": clean}
        self.variants.setdefault(str(len(self.history) // 2), []).append(variant)
        yield {"t": "variant", **variant}

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
            "facts": self.facts,
            "facts_tokens": tokens.of(self.factsheet()),
            "work": self.work,
            "work_tokens": tokens.of(self.worksheet()),
            "task": self.task,
            "task_tokens": tokens.of(self.tasksheet()),
            "task_line": task.summary(self.task),
            # Закрытые ворота считает коробка, а не браузер: условие перехода
            # должно быть одно и то же и в запросе, и на схеме автомата.
            "task_shut": task.shut(self.task, bool(self.settings["task_gates"])),
            "rules": self.rules,
            "rules_tokens": tokens.of(self.rulesheet()),
            "profile": self.profile,
            "profile_tokens": tokens.of(self.profilesheet()),
            "persona": self.persona,
            "variants": self.variants,
            # Короткая память: сколько сообщений уходит дословно и из скольких.
            # Хвост — первые слова последних сообщений истории, а не только
            # окна: окно всегда её конец, и что из хвоста уходит в запрос, видно
            # по `window`. При нулевом окне хвост не пустеет, и в интерфейсе
            # видно, какие именно реплики выключены.
            "window": len(self.remembered()),
            "messages": len(self.history),
            "tail": [{"role": message["role"], "text": message["content"][:60]}
                     for message in self.history[-8:]],
            "branch": self.branch,
            "branches": self.lines(),
            "point": len(self.point["history"]) if self.point else None,
            "ballast": self.ballast,
            "context": int(self.settings["context_limit"]),
            "scale": self.scale,
            "ledger": self.ledger,
            "log": self.log,
        }
