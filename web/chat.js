// Чат недели 3: ассистент, которого курс собирает по шагам. Разметка — в
// index.html (#week3), оформление — в chat.css. Общие помощники страницы
// берутся оттуда же: $, post, stream, markdown, escapeHtml, money.
(() => {
  const KEY = "chat-current";   // открытый чат
  const DRAWER = "chat-drawer"; // панель памяти открыта
  const FOLD = "chat-fold";     // панель чатов свёрнута
  const narrow = matchMedia("(max-width: 900px)");

  const ui = {
    app: $("#app"), list: $("#chatList"), feed: $("#feed"), thread: $("#thread"),
    prompt: $("#prompt"), send: $("#sendChat"), estimate: $("#estimate"),
    modelButton: $("#modelButton"), modelMenu: $("#modelMenu"),
    badge: $("#modelBadge"), modelName: $("#modelName"),
    ring: $("#ring"), ringText: $("#ringText"), cost: $("#cost"),
    newTask: $("#newTask"), memoryButton: $("#memoryButton"),
    memoryCount: $("#memoryCount"), drawer: $("#drawer"),
    prefs: $("#prefs"), prefFields: $("#prefFields"),
  };

  let order = [];        // модели в порядке меню, сгруппированы по провайдеру
  let models = {};       // id → модель
  let fallback = "";     // модель, если у чата она неизвестна
  let blocks = [];       // окно настроек: описание полей с сервера
  let prefs = {};        // значения настроек, общие для всех чатов
  let chats = [];        // список слева, свежие сверху
  let currentId = "";    // открытый чат
  let metrics = null;    // счётчики агента открытого чата
  const busy = new Set();   // чаты, в которых сейчас идёт ответ
  // Узлы реплики, на которую ещё идёт ответ: уйдёшь в другой чат и вернёшься —
  // лента перерисуется из базы, а эта реплика в базе появится только в конце.
  const live = new Map();

  const here = () => chats.find(item => item.id === currentId);
  const modelOf = item => models[item.model] || models[fallback];
  const number = value => Number(value || 0).toLocaleString("ru");

  async function api(method, url, payload) {
    const response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json();
  }

  // Та же оценка по классам символов, что у коробки (agent/tokens.py): точное
  // число знает только провайдер, а здесь нужно «примерно сколько».
  const RATES = [[/[А-Яа-яЁё]/g, 2.5], [/[A-Za-z]/g, 4], [/[0-9]/g, 2]];

  function estimate(text) {
    let known = 0;
    let total = 0;
    RATES.forEach(([pattern, rate]) => {
      const count = (text.match(pattern) || []).length;
      known += count;
      total += count / rate;
    });
    return Math.round(total + (text.length - known) / 3);
  }

  // Время из SQLite — UTC без пояса.
  function when(stamp) {
    return new Date(stamp.replace(" ", "T") + "Z").toLocaleString("ru",
      { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  function short(context) {
    return context >= 1e6 ? Math.round(context / 1e6) + "M"
                          : Math.round(context / 1024) + "K";
  }

  // ── Список чатов ──────────────────────────────────────────────────
  function renderList() {
    ui.list.textContent = "";
    chats.forEach(item => {
      const row = document.createElement("div");
      row.className = "item" + (item.id === currentId ? " on" : "");
      row.dataset.id = item.id;
      row.innerHTML = "<button class='pick'><span class='title'></span>"
        + "<span class='meta'></span><span class='meta'></span></button>"
        + "<button class='more' title='Действия' aria-label='Действия с чатом' "
        + "aria-expanded='false'>⋯</button>";
      const [title, where, spend] = row.querySelectorAll(".title, .meta");
      const model = modelOf(item);
      title.textContent = item.title || "Новый чат";
      title.classList.toggle("untitled", !item.title);
      where.textContent = `${model.vendor} · ${model.title} · ${when(item.updated)}`;
      spend.textContent = item.turns
        ? `контекст ${number(item.context)} · ${money(item.cost)}` : "ответов пока нет";
      ui.list.append(row);
    });
  }

  // Строка списка поменялась на сервере: переименовали, пришло название,
  // закончилась реплика. Список держится по последней реплике, свежие сверху.
  function patch(id, fields) {
    const item = chats.find(chat => chat.id === id);
    if (!item || !fields) return;
    Object.assign(item, fields);
    chats.sort((a, b) => b.updated.localeCompare(a.updated));
    renderList();
    if (id === currentId) renderHeader();
  }

  function closePops() {
    ui.list.querySelectorAll(".pop").forEach(pop => pop.remove());
    ui.list.querySelectorAll(".more").forEach(more => more.setAttribute("aria-expanded", "false"));
  }

  // Две ступени вместо окна подтверждения: первый клик взводит кнопку, второй
  // выполняет. Модальное окно браузера останавливало бы и страницу, и прогон.
  function arm(button, text) {
    if (button.classList.contains("armed")) {
      button.classList.remove("armed");
      button.textContent = button.dataset.label;
      return true;
    }
    button.dataset.label = button.textContent;
    button.textContent = text;
    button.classList.add("armed");
    setTimeout(() => {
      if (!button.classList.contains("armed")) return;
      button.classList.remove("armed");
      button.textContent = button.dataset.label;
    }, 3000);
    return false;
  }

  function toggleActions(row, item) {
    const more = $(".more", row);
    const opened = more.getAttribute("aria-expanded") === "true";
    closePops();
    if (opened) return;
    more.setAttribute("aria-expanded", "true");
    const pop = document.createElement("div");
    pop.className = "pop";
    pop.innerHTML = "<button data-act='rename'>Переименовать</button>"
                  + "<button class='danger' data-act='drop'>Удалить</button>";
    $("[data-act=drop]", pop).disabled = busy.has(item.id);
    pop.addEventListener("click", event => {
      event.stopPropagation();
      const button = event.target.closest("button");
      if (!button) return;
      if (button.dataset.act === "rename") {
        closePops();
        rename(row, item);
      } else if (arm(button, "Точно удалить?")) {
        closePops();
        drop(item);
      }
    });
    row.append(pop);
  }

  function rename(row, item) {
    const input = document.createElement("input");
    input.className = "rename";
    input.value = item.title;
    input.maxLength = 80;
    input.setAttribute("aria-label", "Название чата");
    $(".pick", row).replaceWith(input);
    input.focus();
    input.select();

    let done = false;
    const finish = async keep => {
      if (done) return;
      done = true;
      const title = input.value.trim();
      if (keep && title && title !== item.title) {
        try {
          patch(item.id, await api("PATCH", `/api/chats/${item.id}`, { title }));
          return;
        } catch (error) {
          notice("чат не переименован: " + error.message);
        }
      }
      renderList();
    };
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      else if (event.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
  }

  async function drop(item) {
    await api("DELETE", `/api/chats/${item.id}`);
    chats = chats.filter(chat => chat.id !== item.id);
    if (item.id !== currentId) { renderList(); return; }
    if (chats.length) open(chats[0]);
    else newChat();
  }

  // Пустых чатов не плодим: если чат без ответов уже есть, новым будет он.
  // Модель новый чат берёт у открытого — её обычно и выбирают заново.
  async function newChat() {
    const empty = chats.find(item => !item.turns && !busy.has(item.id));
    if (empty) {
      open(empty);
    } else {
      const item = await api("POST", "/api/chats", { model: here()?.model || fallback });
      chats.unshift(item);
      open(item);
    }
    ui.prompt.focus();
  }

  async function open(item) {
    currentId = item.id;
    localStorage.setItem(KEY, item.id);
    closePops();
    metrics = null;
    renderList();
    renderHeader();
    showMemory();
    ui.thread.textContent = "";

    const data = await post("/api/agent/history", { session: item.id }).then(r => r.json());
    if (currentId !== item.id) return;  // пока ждали, открыли другой чат
    metrics = data.metrics;
    draw(data.messages, data.metrics.ledger || []);
    if (live.has(item.id)) {
      clearEmpty();
      ui.thread.append(...live.get(item.id));
    }
    showMemory();
    renderHeader();
    toBottom();
  }

  // ── Лента ─────────────────────────────────────────────────────────
  function empty() {
    ui.thread.innerHTML = "<p class='empty'>Чат пустой. Спроси что-нибудь — вопрос "
      + "и ответ лягут на диск, и после перезапуска разговор продолжится с этого "
      + "места. Профиль общий: что агент узнал о тебе в других чатах, он знает "
      + "и здесь.</p>";
  }

  function clearEmpty() {
    ui.thread.querySelectorAll(".empty").forEach(node => node.remove());
  }

  const stuck = () =>
    ui.feed.scrollHeight - ui.feed.scrollTop - ui.feed.clientHeight < 120;
  const toBottom = () => (ui.feed.scrollTop = ui.feed.scrollHeight);

  function addTurn(question) {
    clearEmpty();
    const me = document.createElement("div");
    me.className = "me";
    me.textContent = question;
    const bot = document.createElement("div");
    bot.className = "bot";
    bot.innerHTML = "<details class='trace'><summary>"
      + "<svg class='i small' viewBox='0 0 24 24'><path d='m9 6 6 6-6 6'/></svg>"
      + "<span class='gist'></span></summary><div class='lines'></div></details>"
      + "<div class='text'></div>";
    ui.thread.append(me, bot);
    return { me, bot, raw: "", trace: $(".trace", bot), gist: $(".gist", bot),
             lines: $(".lines", bot), text: $(".text", bot) };
  }

  function render(turn, raw) {
    turn.raw = raw;
    turn.text.innerHTML = markdown(raw);
  }

  function line(turn, text, kind = "") {
    const row = document.createElement("div");
    row.className = kind;
    row.textContent = text;
    turn.lines.append(row);
  }

  function fail(turn, text) {
    const row = document.createElement("div");
    row.className = "err";
    row.textContent = text;
    turn.bot.append(row);
  }

  function notice(text) {
    clearEmpty();
    const row = document.createElement("div");
    row.className = "notice";
    row.textContent = text;
    ui.thread.append(row);
    toBottom();
  }

  // Журнал живёт только у реплик этой сессии страницы. У восстановленных из
  // базы есть строка расхода — по ней и собрана сводка.
  function draw(messages, ledger) {
    ui.thread.textContent = "";
    if (!messages.length && !live.has(currentId)) { empty(); return; }
    for (let i = 0; i < messages.length; i += 2) {
      const turn = addTurn(messages[i].content);
      render(turn, (messages[i + 1] || {}).content || "");
      const row = ledger.find(item => item.turn === i / 2 + 1);
      if (!row) { turn.trace.hidden = true; continue; }
      turn.gist.textContent = `${number(row.tokens_in)} ток. · ${money(row.cost)}`;
      line(turn, `вход ${row.tokens_in} ток. · выход ${row.tokens_out} ток. · `
               + `история ~${row.history} ток. · оценка была ~${row.estimated}`, "spend");
    }
  }

  // Из чего сложился запрос — до того, как он ушёл к провайдеру.
  function budgetText(b) {
    const parts = [`роль ${b.role}`];
    if (b.profile) parts.push(`профиль ${b.profile}`);
    if (b.work) parts.push(`задача ${b.work}`);
    parts.push(`память ${b.memory}`, `вопрос ${b.question}`);
    return `запрос ~${b.predicted} ток. (${parts.join(" · ")}) + ${b.reply_max} на ответ`
      + (b.limit ? ` при пределе ${number(b.limit)}` : "");
  }

  async function send() {
    const item = here();
    const text = ui.prompt.value.trim();
    if (!item || !text || busy.has(item.id)) return;
    ui.prompt.value = "";
    fit();
    count();

    busy.add(item.id);
    const turn = addTurn(text);
    live.set(item.id, [turn.me, turn.bot]);
    turn.trace.classList.add("live");
    turn.gist.textContent = "агент работает";
    lock();
    toBottom();

    let route = "";
    try {
      await stream(`/api/chats/${item.id}/send`, { text }, event => {
        const follow = stuck();
        if (event.t === "log") {
          line(turn, event.text);
          // Строка маршрута — главное, что коробка делает до ответа в день 11:
          // она и идёт в сводку, без имён записей, чтобы сводка влезала в строку.
          if (event.text.startsWith("маршрут:")) {
            route = event.text.replace(/\s*\([^)]*\)/g, "");
            turn.gist.textContent = route;
          }
        } else if (event.t === "budget") {
          line(turn, budgetText(event), "budget");
        } else if (event.t === "delta") {
          render(turn, turn.raw + event.text);
        } else if (event.t === "replace") {
          render(turn, event.text);
        } else if (event.t === "blocked") {
          fail(turn, "отказ: " + event.reason);
        } else if (event.t === "error") {
          fail(turn, event.message);
        } else if (event.t === "title") {
          patch(item.id, { title: event.title });
        } else if (event.t === "done") {
          const spent = event.turn;
          line(turn, `потрачено: ${spent.tokens_in} ток. вход · ${spent.tokens_out} ток. `
                   + `выход · ${spent.requests} запр. · ${money(spent.cost)}`, "spend");
          turn.gist.textContent = [route, `${number(spent.tokens_in)} ток.`, money(spent.cost)]
            .filter(Boolean).join(" · ");
          if (currentId === item.id) metrics = event;
          patch(item.id, event.chat);
          if (currentId === item.id) showMemory();
        }
        if (follow && currentId === item.id) toBottom();
      });
    } catch (error) {
      fail(turn, "сбой: " + error.message);
    } finally {
      busy.delete(item.id);
      live.delete(item.id);
      turn.trace.classList.remove("live");
      if (turn.gist.textContent === "агент работает") turn.gist.textContent = "журнал";
      lock();
    }
  }

  // ── Шапка ─────────────────────────────────────────────────────────
  function renderHeader() {
    const item = here();
    if (!item) return;
    const model = modelOf(item);
    ui.badge.dataset.vendor = model.provider;
    ui.badge.textContent = model.vendor[0];
    ui.modelName.textContent = model.title;
    ui.modelButton.title = `${model.vendor} · ${model.id} · контекст `
                         + `${number(model.context)} токенов`;

    // Заполнение контекста — вход последнего запроса против предела модели,
    // которая стоит у чата сейчас.
    const share = model.context ? item.context / model.context * 100 : 0;
    ui.ringText.textContent = share.toLocaleString("ru",
      { maximumFractionDigits: share < 1 ? 1 : 0 }) + "%";
    $(".fill", ui.ring).style.strokeDashoffset = 50.27 * (1 - Math.min(share, 100) / 100);
    ui.ring.title = `Контекст последнего запроса: ${number(item.context)} из `
                  + `${number(model.context)} токенов`;
    ui.cost.textContent = money(item.cost);
    renderMenu();
  }

  function renderMenu() {
    const item = here();
    ui.modelMenu.textContent = "";
    let vendor = "";
    order.forEach(model => {
      if (model.vendor !== vendor) {
        vendor = model.vendor;
        const group = document.createElement("div");
        group.className = "group";
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.dataset.vendor = model.provider;
        badge.textContent = vendor[0];
        group.append(badge, vendor);
        ui.modelMenu.append(group);
      }
      const option = document.createElement("button");
      option.className = "opt" + (item && item.model === model.id ? " on" : "");
      option.setAttribute("role", "menuitemradio");
      option.dataset.model = model.id;
      option.disabled = !model.ready;
      option.innerHTML = "<span class='name'></span><span class='note'></span>"
        + "<svg class='i small check' viewBox='0 0 24 24'><path d='m5 12 5 5 9-11'/></svg>";
      $(".name", option).textContent = model.title;
      $(".note", option).textContent = model.ready
        ? `контекст ${short(model.context)}` : "нет ключа на сервере";
      ui.modelMenu.append(option);
    });
  }

  function showMenu(on) {
    ui.modelMenu.hidden = !on;
    ui.modelButton.setAttribute("aria-expanded", String(on));
  }

  async function pickModel(id) {
    const item = here();
    if (!item || id === item.model) return;
    patch(item.id, await api("PATCH", `/api/chats/${item.id}`, { model: id }));
    const model = modelOf(item);
    notice(`модель: ${model.vendor} · ${model.title} — со следующей реплики`);
  }

  // ── Память ────────────────────────────────────────────────────────
  function showMemory() {
    const m = metrics || {};
    const short = $(".layer.short", ui.drawer);
    const tail = m.tail || [];
    // Окно — конец истории: в запрос уходят последние `window` сообщений.
    // Хвост показан и при выключенной короткой памяти, только бледным: так
    // карточка не меняет высоту, и видно, какие именно реплики не уходят.
    const out = tail.length - Math.min(m.window || 0, tail.length);
    $(".count", short).textContent = m.messages
      ? `в запросе ${m.window} из ${m.messages} сообщений · `
        + `вся история ~${number(m.history_tokens)} ток.`
      : "диалог пуст";
    $(".peek", short).innerHTML = tail.map((item, index) =>
      `<li${index < out ? " class='out' title='в запрос не уходит'" : ""}>`
      + `<b>${item.role === "user" ? "вы" : "агент"}:</b> ${escapeHtml(item.text)}`
      + `${item.text.length >= 60 ? "…" : ""}</li>`).join("");
    stash(short, tail.length, `последние реплики (${tail.length})`);
    fillLayer("work", m.work, m.work_tokens);
    fillLayer("profile", m.profile, m.profile_tokens);
    ui.memoryCount.textContent =
      Object.keys(m.work || {}).length + Object.keys(m.profile || {}).length;
    syncToggles();
    lock();
  }

  // Запись слоя: значение и две кнопки — перенести в соседний слой и забыть.
  // Раскладывает модель, а значит ошибается; поправить должно быть можно.
  // Имена и значения пишет модель, поэтому в разметку они идут только текстом.
  function fillLayer(kind, map, cost) {
    const layer = $(`.layer.${kind}`, ui.drawer);
    const rows = Object.entries(map || {});
    const other = kind === "work" ? "profile" : "work";
    const hint = other === "profile" ? "Перенести в профиль" : "Перенести в рабочую память";
    $(".count", layer).textContent = rows.length
      ? `записей ${rows.length} · ~${cost} ток.` : "пусто";

    const host = $(".records", layer);
    host.textContent = "";
    rows.forEach(([name, value]) => {
      const record = document.createElement("div");
      record.className = "record";
      record.innerHTML = "<div class='rhead'><span class='name'></span>"
                       + "<span class='ops'></span></div><div class='value'></div>";
      $(".name", record).textContent = name;
      $(".value", record).textContent = value;
      [["→", other, hint], ["×", "", "Забыть запись"]].forEach(([sign, to, title]) => {
        const button = document.createElement("button");
        button.textContent = sign;
        button.title = title;
        button.setAttribute("aria-label", title);
        button.dataset.move = to;
        button.dataset.key = name;
        $(".ops", record).append(button);
      });
      host.append(record);
    });
    stash(layer, rows.length, `записи (${rows.length})`);
  }

  // Накопленное в слое лежит под раскрывашкой: всё время на виду оно не нужно,
  // а счётчик над ней и так говорит, сколько там. Пустой слой её не показывает.
  function stash(layer, count, label) {
    const box = $(".stash", layer);
    box.hidden = !count;
    $("summary", box).textContent = label;
  }

  function syncToggles() {
    ui.drawer.querySelectorAll("[data-pref]")
      .forEach(box => (box.checked = Boolean(prefs[box.dataset.pref])));
  }

  function showDrawer(on) {
    ui.drawer.hidden = !on;
    ui.memoryButton.setAttribute("aria-expanded", String(on));
    localStorage.setItem(DRAWER, on ? "1" : "");
  }

  // Ответы сервера на ручки памяти одинаковые: свежие счётчики агента.
  async function memoryCall(url, payload) {
    const data = await post(url, { session: currentId, ...payload }).then(r => r.json());
    metrics = data.metrics;
    showMemory();
    return data;
  }

  async function newTask() {
    const data = await memoryCall("/api/agent/newtask", {});
    notice(`Новая задача: рабочая память стёрта (записей было ${data.gone}). `
         + "Профиль и история на месте.");
  }

  async function forgetProfile(button) {
    if (!arm(button, "Точно забыть?")) return;
    const data = await memoryCall("/api/agent/forget-profile", {});
    notice(`Профиль забыт во всех чатах: записей было ${data.gone}.`);
  }

  // ── Настройки ─────────────────────────────────────────────────────
  // Короткая память считается агентом по настройкам, поэтому после их смены
  // счётчики открытого чата перечитываются.
  async function savePrefs(values) {
    prefs = await api("POST", "/api/chat/prefs", { values });
    syncToggles();
    const item = here();
    if (!item || busy.has(item.id)) return;
    const data = await post("/api/agent/history", { session: item.id }).then(r => r.json());
    if (currentId !== item.id) return;
    metrics = data.metrics;
    showMemory();
  }

  function renderPrefs() {
    ui.prefFields.textContent = "";
    blocks.forEach(block => {
      const part = document.createElement("fieldset");
      part.className = "block";
      part.innerHTML = "<legend></legend><p class='fine'></p>";
      $("legend", part).textContent = block.title;
      $(".fine", part).textContent = block.note || "";

      block.fields.forEach(field => {
        const row = document.createElement("div");
        row.className = "field";
        const label = document.createElement("label");
        label.textContent = field.label;
        label.htmlFor = "pref-" + field.key;
        const input = document.createElement("input");
        input.id = "pref-" + field.key;
        input.dataset.key = field.key;
        if (field.type === "bool") {
          input.type = "checkbox";
          input.checked = Boolean(prefs[field.key]);
        } else {
          input.type = "number";
          input.min = 0;
          input.step = 1;
          input.value = prefs[field.key];
        }
        row.append(label, input);
        if (field.hint) {
          const hint = document.createElement("span");
          hint.className = "hint";
          hint.textContent = field.hint;
          row.append(hint);
        }
        part.append(row);
      });
      ui.prefFields.append(part);
    });
  }

  function readPrefs() {
    const values = {};
    ui.prefFields.querySelectorAll("[data-key]").forEach(input => {
      values[input.dataset.key] = input.type === "checkbox" ? input.checked
                                                            : Number(input.value);
    });
    return values;
  }

  // ── Поле ввода ────────────────────────────────────────────────────
  function fit() {
    ui.prompt.style.height = "auto";
    ui.prompt.style.height = Math.min(ui.prompt.scrollHeight, 220) + "px";
  }

  function count() {
    ui.estimate.textContent = `≈ ${number(estimate(ui.prompt.value))} ток. · оценка`;
    lock();
  }

  // Пока в чате идёт ответ, второй вопрос туда не уходит, а ручки памяти стоят:
  // коробка ещё раскладывает эту реплику, и правка слоя разошлась бы с ней.
  function lock() {
    const on = busy.has(currentId);
    ui.send.disabled = on || !ui.prompt.value.trim();
    ui.newTask.disabled = on;
    ui.drawer.querySelectorAll(".layer button").forEach(button => (button.disabled = on));
  }

  function showSide(on) {
    if (narrow.matches) {
      ui.app.classList.toggle("open", on);
      return;
    }
    ui.app.classList.toggle("folded", !on);
    localStorage.setItem(FOLD, on ? "" : "1");
  }

  // ── События ───────────────────────────────────────────────────────
  ui.list.addEventListener("click", event => {
    const row = event.target.closest(".item");
    if (!row) return;
    const item = chats.find(chat => chat.id === row.dataset.id);
    if (event.target.closest(".more")) {
      toggleActions(row, item);
    } else if (event.target.closest(".pick")) {
      if (item.id !== currentId) open(item);
      if (narrow.matches) showSide(false);
    }
  });

  $("#newChat").addEventListener("click", () => {
    newChat();
    if (narrow.matches) showSide(false);
  });
  $("#fold").addEventListener("click", () => showSide(false));
  $("#unfold").addEventListener("click", () => showSide(true));
  $("#scrim").addEventListener("click", () => showSide(false));

  ui.modelButton.addEventListener("click", () => showMenu(ui.modelMenu.hidden));
  ui.modelMenu.addEventListener("click", event => {
    const option = event.target.closest(".opt");
    if (!option || option.disabled) return;
    showMenu(false);
    pickModel(option.dataset.model).catch(error =>
      notice("модель не сменилась: " + error.message));
  });

  ui.newTask.addEventListener("click", newTask);
  ui.memoryButton.addEventListener("click", () => showDrawer(ui.drawer.hidden));
  ui.drawer.addEventListener("click", event => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.id === "closeDrawer") showDrawer(false);
    else if (button.dataset.act === "newtask") newTask();
    else if (button.dataset.act === "forget") forgetProfile(button);
    else if (button.dataset.key !== undefined) {
      const layer = button.closest(".layer").classList.contains("work") ? "work" : "profile";
      memoryCall("/api/agent/move",
        { layer, key: button.dataset.key, to: button.dataset.move });
    }
  });
  ui.drawer.addEventListener("change", event => {
    const key = event.target.dataset.pref;
    if (key) savePrefs({ [key]: event.target.checked });
  });

  ui.prompt.addEventListener("input", () => { fit(); count(); });
  ui.prompt.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  ui.send.addEventListener("click", send);

  $("#openPrefs").addEventListener("click", () => {
    renderPrefs();
    ui.prefs.hidden = false;
  });
  $("#closePrefs").addEventListener("click", () => (ui.prefs.hidden = true));
  ui.prefs.addEventListener("click", event => {
    if (event.target === ui.prefs) ui.prefs.hidden = true;
  });
  $("#savePrefs").addEventListener("click", async () => {
    await savePrefs(readPrefs());
    ui.prefs.hidden = true;
  });
  $("#resetPrefs").addEventListener("click", async () => {
    const values = {};
    blocks.forEach(block => block.fields.forEach(field => (values[field.key] = field.default)));
    await savePrefs(values);
    renderPrefs();
  });

  document.addEventListener("click", event => {
    if (!event.target.closest("#week3 .picker")) showMenu(false);
    if (!event.target.closest("#week3 .pop, #week3 .more")) closePops();
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    showMenu(false);
    closePops();
    ui.prefs.hidden = true;
  });

  async function boot() {
    const config = await api("GET", "/api/chat/config");
    order = config.models;
    models = Object.fromEntries(order.map(model => [model.id, model]));
    fallback = config.default;
    blocks = config.blocks;
    prefs = config.prefs;
    chats = await api("GET", "/api/chats");

    ui.app.classList.toggle("folded", localStorage.getItem(FOLD) === "1");
    showDrawer(localStorage.getItem(DRAWER) === "1" && !narrow.matches);
    count();
    const saved = chats.find(item => item.id === localStorage.getItem(KEY));
    if (saved || chats.length) await open(saved || chats[0]);
    else await newChat();
  }

  boot().catch(error => {
    ui.thread.innerHTML = "<p class='empty err'></p>";
    $(".empty", ui.thread).textContent = "Чат не поднялся: " + error.message;
  });
})();
