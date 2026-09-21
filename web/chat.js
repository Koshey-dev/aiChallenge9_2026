// Чат-ассистент, которого курс собирает по шагам: одна панель на вкладки
// недель 3 и 4. Разметка — в index.html (#chatpane), оформление — в chat.css.
// Элементы дней после недели 3 помечаются data-week, чтобы в неделе 3 их не
// было видно. Общие помощники страницы
// берутся оттуда же: $, post, stream, markdown, escapeHtml, money.
(() => {
  const KEY = "chat-current";   // открытый чат
  const DRAWER = "chat-drawer"; // панель памяти открыта
  const VAULT = "chat-vault";   // панель свода открыта
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
    profileButton: $("#profileButton"), profileMenu: $("#profileMenu"),
    profileName: $("#profileName"), people: $("#profiles"),
    profileList: $("#profileList"), profileCard: $("#profileCard"),
    stageline: $("#stageline"), stageTrack: $("#stageTrack"),
    stageStep: $("#stageStep"), stagebox: $("#stagebox"),
    modeButton: $("#modeButton"), modeName: $("#modeName"),
    modeMenu: $("#modeMenu"),
    vaultButton: $("#vaultButton"), vaultCount: $("#vaultCount"), vault: $("#vault"),
    vaultAdd: $("#vaultAdd"), ruleKind: $("#ruleKind"), ruleText: $("#ruleText"),
    kindAbout: $("#kindAbout"),
    ruleList: $("#ruleList"), vaultCost: $("#vaultCost"),
  };

  let order = [];        // модели в порядке меню, сгруппированы по провайдеру
  let models = {};       // id → модель
  let fallback = "";     // модель, если у чата она неизвестна
  let blocks = [];       // окно настроек: описание полей с сервера
  let chats = [];        // список слева, свежие сверху
  let currentId = "";    // открытый чат
  let metrics = null;    // счётчики агента открытого чата
  let persona = { choices: [], texts: [] };  // поля анкеты с сервера
  let stages = [];       // этапы автомата с сервера, по порядку
  let modes = [];        // режимы чата с сервера: планирование и общение
  let kinds = [];        // виды инвариантов с сервера
  let profiles = [];     // профили, «Основной» первым
  let editing = "";      // профиль, открытый в окне «Профили»
  let dirty = false;     // в анкете есть несохранённые правки
  let turns = [];        // реплики открытого чата на экране
  let last = null;       // последняя из них: только у неё кнопка варианта
  const busy = new Set();   // чаты, в которых сейчас идёт ответ
  // Узлы реплики, на которую ещё идёт ответ: уйдёшь в другой чат и вернёшься —
  // лента перерисуется из базы, а эта реплика в базе появится только в конце.
  const live = new Map();

  const here = () => chats.find(item => item.id === currentId);
  // Настройки открытого чата: у каждого чата свои, сервер отдаёт их целиком.
  const prefs = () => (here() || {}).prefs || {};
  const modelOf = item => models[item.model] || models[fallback];
  const profileOf = item => profiles.find(one => one.id === item.profile) || profiles[0];
  const CHECK = "<svg class='i small check' viewBox='0 0 24 24'><path d='m5 12 5 5 9-11'/></svg>";
  const CHEVRON = "<svg class='i small' viewBox='0 0 24 24'><path d='m6 9 6 6 6-6'/></svg>";
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
      where.textContent = `${model.title} · ${profileOf(item).title} · ${when(item.updated)}`;
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
  // Модель и профиль новый чат берёт у открытого — их обычно и выбирают заново.
  // Настройки — нет: новый чат начинает с умолчаний, где всё включено. Пустой
  // чат, который берётся вместо нового, к ним и возвращается.
  async function newChat() {
    const empty = chats.find(item => !item.turns && !busy.has(item.id));
    if (empty) {
      const fresh = defaults();
      if (Object.keys(fresh).some(key => empty.prefs[key] !== fresh[key])) {
        patch(empty.id, await api("POST", `/api/chats/${empty.id}/prefs`, { values: fresh }));
      }
      open(empty);
    } else {
      const item = await api("POST", "/api/chats",
        { model: here()?.model || fallback, profile: here()?.profile || profiles[0].id });
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
    showVault();
    renderTask();
    ui.thread.textContent = "";

    const data = await post("/api/agent/history", { session: item.id }).then(r => r.json());
    if (currentId !== item.id) return;  // пока ждали, открыли другой чат
    metrics = data.metrics;
    draw(data.messages, data.metrics.ledger || [], data.metrics.variants || {});
    if (live.has(item.id)) {
      clearEmpty();
      ui.thread.append(...live.get(item.id));
    }
    showMemory();
    showVault();
    renderTask();
    renderHeader();
    toBottom();
  }

  // ── Лента ─────────────────────────────────────────────────────────
  function empty() {
    ui.thread.innerHTML = "<p class='empty'>Чат пустой. Спроси что-нибудь — вопрос "
      + "и ответ лягут на диск, и после перезапуска разговор продолжится с этого "
      + "места. Профиль чата — в шапке: его анкета и то, что ассистент о тебе "
      + "заметил в других чатах с этим профилем, уходят в каждый запрос.</p>";
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
      + "<div class='text'></div><div class='foot' hidden></div>";
    ui.thread.append(me, bot);
    // Варианты ответа: первый — тот, что в истории, остальные — для других
    // профилей. У каждого своя строка меток: что из профиля ушло в запрос.
    const turn = { me, bot, raw: "", trace: $(".trace", bot), gist: $(".gist", bot),
                   lines: $(".lines", bot), text: $(".text", bot), foot: $(".foot", bot),
                   variants: [], index: 0 };
    turns.push(turn);
    return turn;
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
  function draw(messages, ledger, variants) {
    ui.thread.textContent = "";
    turns = [];
    last = null;
    if (!messages.length && !live.has(currentId)) { empty(); return; }
    for (let i = 0; i < messages.length; i += 2) {
      const turn = addTurn(messages[i].content);
      const answer = (messages[i + 1] || {}).content || "";
      const row = ledger.find(item => item.turn === i / 2 + 1);
      turn.variants = [{ persona: told(row), text: answer },
                       ...rejected(row), ...ahead(row),
                       ...(variants[i / 2 + 1] || [])];
      render(turn, answer);
      last = turn;
      if (!row) { turn.trace.hidden = true; continue; }
      learned(turn, row);
      stepped(turn, row);
      broke(turn, row);
      overran(turn, row);
      turn.gist.textContent = `${number(row.tokens_in)} ток. · ${money(row.cost)}`;
      line(turn, `вход ${row.tokens_in} ток. · выход ${row.tokens_out} ток. · `
               + `история ~${row.history} ток. · оценка была ~${row.estimated}`, "spend");
    }
    turns.forEach(foot);
  }

  // Что из профиля ушло в запрос — из строки расхода. У реплик до дня 12
  // этого нет, и меток у них нет.
  const told = row => (row && row.persona && row.persona.title !== undefined ? row.persona : null);

  // Плашка «профиль пополнен» — над ответом, как «память обновлена» у ChatGPT:
  // маршрутизатор решил это до ответа, и ответ уже шёл с новой записью.
  function learned(turn, row) {
    const fresh = Object.entries(row.learned || {});
    if (!fresh.length) return;
    const plaque = document.createElement("button");
    plaque.className = "learned";
    plaque.title = "Открыть профиль";
    plaque.innerHTML = "<svg class='i small' viewBox='0 0 24 24'><path d='M12 20h9'/>"
      + "<path d='M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z'/></svg><span></span>";
    $("span", plaque).textContent = `Профиль «${(told(row) || {}).title || "чата"}» пополнен: `
      + fresh.map(([name, value]) => `${name} — ${value}`).join("; ");
    plaque.addEventListener("click", () => openProfiles(here()?.profile));
    turn.text.before(plaque);
  }

  // Плашка перехода — там же, над ответом: этап сменился до ответа, и ответ
  // уже шёл с новым. Отклонённый переход показывается наравне с прошедшим —
  // по нему и видно, что автомат держит порядок этапов.
  function stepped(turn, row) {
    const move = row.moved;
    if (!move) return;
    const plaque = document.createElement("button");
    plaque.className = "moved" + (move.ok ? "" : " no");
    plaque.title = "Показать план и журнал переходов";
    plaque.innerHTML = "<svg class='i small' viewBox='0 0 24 24'>"
      + "<path d='M5 12h14M13 6l6 6-6 6'/></svg><span></span>";
    $("span", plaque).textContent = move.ok
      ? `Этап: ${stageTitle(move.from)} → ${stageTitle(move.to)}`
      : `Переход отклонён: ${stageTitle(move.from)} → ${stageTitle(move.to)} — `
        + `${move.why}`;
    plaque.addEventListener("click", () => showStagebox(true));
    turn.text.before(plaque);
  }

  // Ответ, который свод не пропустил, — вторым вариантом рядом с переписанным.
  // Разница между ними и есть день 14: видно, что модель хотела ответить.
  const rejected = row => (row && row.rejected
    ? [{ persona: told(row), text: row.rejected, rejected: true }] : []);

  // То же для сторожа этапа: ответ, который делал работу следующего этапа,
  // остаётся рядом с переписанным. Свод судит о содержании, сторож — о том,
  // чей это этап, поэтому вариантов может оказаться и два сразу.
  const ahead = row => (row && row.ahead
    ? [{ persona: told(row), text: row.ahead, ahead: true }] : []);

  // Плашка свода — над ответом, рядом с плашкой перехода. Нарушил сам ответ —
  // красная; спорит с инвариантом сама просьба, а ответ отказал — обычная:
  // это не сбой, а ровно та работа, ради которой свод заводится.
  function broke(turn, row) {
    const broken = row.broken || [];
    const clash = row.clash || [];
    if (!broken.length && !clash.length) return;
    const plaque = document.createElement("button");
    plaque.className = "guarded" + (broken.length ? " no" : "");
    plaque.title = "Показать свод";
    plaque.innerHTML = "<svg class='i small' viewBox='0 0 24 24'>"
      + "<rect x='4' y='10' width='16' height='11' rx='2'/>"
      + "<path d='M8 10V7a4 4 0 0 1 8 0v3'/></svg><span></span>";
    $("span", plaque).textContent = broken.length
      ? `Инвариант нарушен: ${broken.map(hit => hit.text).join("; ")}`
        + (row.rejected ? " — ответ переписан" : "")
      : `Запрос против свода: ${clash.map(hit => hit.text).join("; ")}`;
    plaque.addEventListener("click", () => showVaultPanel(true));
    turn.text.before(plaque);
  }

  // Плашка сторожа этапа. Ворота держат переходы, но не текст: автомат стоит
  // на планировании, а ответ уже выдал готовую реализацию. Плашка говорит,
  // чем именно ответ забежал вперёд, и переписанный лежит рядом с прежним.
  function overran(turn, row) {
    if (!row.jumped) return;
    const plaque = document.createElement("button");
    plaque.className = "guarded no";
    plaque.title = "Показать план и журнал переходов";
    plaque.innerHTML = "<svg class='i small' viewBox='0 0 24 24'>"
      + "<path d='M4 18h6M14 18h6M7 18V9l5 4 5-4v9'/></svg><span></span>";
    $("span", plaque).textContent = `Ответ перепрыгнул этап: ${row.jumped}`
      + (row.ahead ? " — переписан под этап" : "");
    plaque.addEventListener("click", () => showStagebox(true));
    turn.text.before(plaque);
  }

  // Метки под ответом: чей профиль и что из него ушло в запрос.
  function cues(persona) {
    const chips = persona.off ? ["анкета не в запросе"]
      : persona.marks.length ? [...persona.marks] : ["анкета пустая"];
    if (persona.noticed) chips.push(`замечено: ${persona.noticed}`);
    return chips;
  }

  // Строка под ответом: листалка вариантов, метки профиля и — только у
  // последней реплики — «Ответить для профиля». Вариант строится на той же
  // памяти, поэтому для старых реплик его уже не собрать честно.
  function foot(turn) {
    turn.foot.textContent = "";
    const many = turn.variants.length > 1;
    const persona = (turn.variants[turn.index] || {}).persona;
    if (many) {
      const pager = document.createElement("span");
      pager.className = "pager";
      pager.innerHTML = "<button aria-label='Предыдущий вариант'>‹</button><span></span>"
                      + "<button aria-label='Следующий вариант'>›</button>";
      const [back, next] = pager.querySelectorAll("button");
      $("span", pager).textContent = `${turn.index + 1}/${turn.variants.length}`;
      back.disabled = turn.index === 0;
      next.disabled = turn.index === turn.variants.length - 1;
      back.addEventListener("click", () => show(turn, turn.index - 1));
      next.addEventListener("click", () => show(turn, turn.index + 1));
      turn.foot.append(pager);
    }
    const shown = turn.variants[turn.index] || {};
    if (shown.rejected || shown.ahead) {
      const chip = document.createElement("span");
      chip.className = "chip no";
      chip.textContent = shown.rejected ? "отклонён сводом" : "перепрыгнул этап";
      turn.foot.append(chip);
    }
    if (persona && (prefs().show_marks || many)) {
      const marks = document.createElement("span");
      marks.className = "marks";
      marks.title = "Что из профиля ушло в запрос";
      [persona.title, ...(prefs().show_marks ? cues(persona) : [])].forEach((text, index) => {
        const chip = document.createElement("span");
        chip.className = index ? "chip" : "chip who";
        chip.textContent = text;
        marks.append(chip);
      });
      turn.foot.append(marks);
    }
    if (turn === last && !busy.has(currentId) && profiles.length > 1 && turn.variants.length) {
      const box = document.createElement("span");
      box.className = "retell";
      box.innerHTML = "<button class='again' aria-haspopup='menu' aria-expanded='false'>"
        + "<svg class='i small' viewBox='0 0 24 24'><path d='M3 12a9 9 0 1 0 3-6.7L3 8'/>"
        + "<path d='M3 3v5h5'/></svg>Ответить для профиля" + CHEVRON + "</button>";
      $(".again", box).addEventListener("click", () => retellMenu(turn, box));
      turn.foot.append(box);
    }
    turn.foot.hidden = !turn.foot.childElementCount;
  }

  function show(turn, index) {
    turn.index = index;
    render(turn, turn.variants[index].text);
    foot(turn);
  }

  function closeRetell() {
    ui.thread.querySelectorAll(".retell .menu").forEach(menu => menu.remove());
    ui.thread.querySelectorAll(".retell .again")
      .forEach(button => button.setAttribute("aria-expanded", "false"));
  }

  function retellMenu(turn, box) {
    const opened = box.querySelector(".menu");
    closeRetell();
    if (opened) return;
    const item = here();
    const menu = document.createElement("div");
    menu.className = "menu";
    menu.setAttribute("role", "menu");
    profiles.filter(profile => profile.id !== item.profile).forEach(profile => {
      const option = document.createElement("button");
      option.className = "opt";
      option.setAttribute("role", "menuitem");
      option.innerHTML = "<span class='name'></span><span class='note'></span>";
      $(".name", option).textContent = profile.title;
      $(".note", option).textContent = profile.marks.slice(0, 3).join(" · ") || "анкета пустая";
      option.addEventListener("click", () => { closeRetell(); retell(turn, profile); });
      menu.append(option);
    });
    $(".again", box).setAttribute("aria-expanded", "true");
    box.append(menu);
  }

  // Тот же вопрос, та же память, другой профиль. Вариант ложится рядом
  // с ответом, а разговор продолжается от исходного.
  async function retell(turn, profile) {
    const item = here();
    if (!item || busy.has(item.id)) return;
    busy.add(item.id);
    lock();
    const before = turn.index;
    turn.variants.push({ persona: { title: profile.title, marks: profile.marks,
                                    off: !prefs().send_persona, noticed: 0 }, text: "" });
    turn.index = turn.variants.length - 1;
    render(turn, "");
    foot(turn);
    let done = false;
    try {
      await stream(`/api/chats/${item.id}/variant`, { profile: profile.id }, event => {
        const variant = turn.variants[turn.variants.length - 1];
        if (event.t === "delta") {
          variant.text += event.text;
          if (turn.index === turn.variants.length - 1) render(turn, variant.text);
        } else if (event.t === "variant") {
          Object.assign(variant, { persona: event.persona, text: event.text });
          done = true;
        } else if (event.t === "error") {
          fail(turn, event.message);
        } else if (event.t === "done") {
          patch(item.id, event.chat);
        }
      });
    } catch (error) {
      fail(turn, "сбой: " + error.message);
    } finally {
      busy.delete(item.id);
      if (!done) {
        turn.variants.pop();
        turn.index = before;
      }
      show(turn, turn.index);
      lock();
    }
  }

  // Из чего сложился запрос — до того, как он ушёл к провайдеру. Части
  // в том порядке, в каком стоят в запросе: слои памяти — после окна.
  function budgetText(b) {
    const parts = [`роль ${b.role}`, `память ${b.memory}`];
    if (b.note) parts.push(`правило памяти ${b.note}`);
    if (b.persona) parts.push(`анкета ${b.persona}`);
    if (b.profile) parts.push(`профиль ${b.profile}`);
    if (b.work) parts.push(`задача ${b.work}`);
    if (b.task) parts.push(`состояние ${b.task}`);
    if (b.rules) parts.push(`свод ${b.rules}`);
    parts.push(`вопрос ${b.question}`);
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
    const before = item.turns;
    const previous = last;
    if (previous) foot(previous);
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
          const row = (event.ledger || []).at(-1);
          // Отказ политики строку расхода не пишет: последняя тогда — от
          // прошлой реплики, и чужие метки сюда не нужны.
          if (row && event.turns === before + 1 && row.turn === event.turns) {
            turn.variants = [{ persona: told(row), text: turn.raw },
                             ...rejected(row), ...ahead(row)];
            learned(turn, row);
            stepped(turn, row);
            broke(turn, row);
            overran(turn, row);
          }
          if (currentId === item.id) {
            metrics = event;
            last = turn;
          }
          patch(item.id, event.chat);
          if (currentId === item.id) {
            showMemory();
            showVault();
            renderTask();
            const closed = row && row.moved && row.moved.ok && row.moved.to === "done";
            settleMode(closed).catch(() => {});
          }
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
      foot(turn);
      if (previous && previous !== last) foot(previous);
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

    const owner = profileOf(item);
    ui.profileName.textContent = owner.title;
    ui.profileButton.title = owner.prompt
      ? "Профиль чата. Уходит в запрос:\n" + owner.prompt : "Профиль чата. Анкета пустая";
    renderProfileMenu();
    renderMode();
  }

  function renderProfileMenu() {
    const item = here();
    ui.profileMenu.textContent = "";
    profiles.forEach(profile => {
      const option = document.createElement("button");
      option.className = "opt" + (item && item.profile === profile.id ? " on" : "");
      option.setAttribute("role", "menuitemradio");
      option.dataset.profile = profile.id;
      option.innerHTML = "<span class='name'></span><span class='note'></span>" + CHECK;
      $(".name", option).textContent = profile.title;
      $(".note", option).textContent = profile.marks.slice(0, 3).join(" · ") || "анкета пустая";
      ui.profileMenu.append(option);
    });
    const edit = document.createElement("button");
    edit.className = "opt edit";
    edit.dataset.act = "edit";
    edit.textContent = "Править профили…";
    ui.profileMenu.append(edit);
  }

  function showPeople(on) {
    ui.profileMenu.hidden = !on;
    ui.profileButton.setAttribute("aria-expanded", String(on));
  }

  // Профиль, как и модель, меняется со следующей реплики. Панель памяти
  // перечитывается сразу: долговременная память у нового профиля своя.
  async function pickProfile(id) {
    const item = here();
    if (!item || id === item.profile) return;
    patch(item.id, await api("PATCH", `/api/chats/${item.id}`, { profile: id }));
    notice(`профиль: ${profileOf(item).title} — со следующей реплики`);
    await reload();
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
      option.innerHTML = "<span class='name'></span><span class='note'></span>" + CHECK;
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
    const item = here();
    $(".layer.profile .kind", ui.drawer).textContent =
      item && profiles.length ? `профиль «${profileOf(item).title}»` : "профиль";
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
      // Запись рабочей памяти поднимается в свод отсюда: маршрутизатор уже
      // вытащил из диалога «принятые решения», а повысить решение до
      // ограничения может только человек — в свод модель не пишет.
      const ops = kind === "work"
        ? [["→", other, hint], ["⇧", "rules", "Сделать инвариантом"],
           ["×", "", "Забыть запись"]]
        : [["→", other, hint], ["×", "", "Забыть запись"]];
      ops.forEach(([sign, to, title]) => {
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
      .forEach(box => (box.checked = Boolean(prefs()[box.dataset.pref])));
  }

  function showDrawer(on) {
    ui.drawer.hidden = !on;
    ui.memoryButton.setAttribute("aria-expanded", String(on));
    localStorage.setItem(DRAWER, on ? "1" : "");
    if (on) showVaultPanel(false);
  }

  // Ответы сервера на ручки памяти одинаковые: свежие счётчики агента.
  async function memoryCall(url, payload) {
    const data = await post(url, { session: currentId, ...payload }).then(r => r.json());
    metrics = data.metrics;
    showMemory();
    showVault();
    renderTask();
    return data;
  }

  async function newTask() {
    const data = await memoryCall("/api/agent/newtask", {});
    notice(`Новая задача: рабочая память стёрта (записей было ${data.gone}), `
         + "этап и шаги сброшены. Профиль и история на месте.");
  }

  async function forgetProfile(button) {
    if (!arm(button, "Точно забыть?")) return;
    const data = await memoryCall("/api/agent/forget-profile", {});
    notice(`Профиль «${profileOf(here()).title}»: забыто записей ${data.gone} — во всех `
         + "его чатах. Анкета на месте.");
  }

  // ── Свод инвариантов ──────────────────────────────────────────────
  const kindTitle = id => (kinds.find(kind => kind.id === id) || {}).title || id;
  const ruleList = () => (metrics || {}).rules || [];

  // Свод рисуется из счётчиков агента, как и слои памяти: сервер отдаёт его
  // целиком после каждой правки, и второй копии в браузере нет.
  function showVault() {
    const items = ruleList();
    const live = items.filter(item => item.active);
    ui.vaultCount.textContent = live.length;
    ui.vaultCost.textContent = items.length
      ? `инвариантов ${items.length}` + (live.length < items.length
          ? ` · в запрос уходит ${live.length}` : "")
        + ` · ~${(metrics || {}).rules_tokens || 0} ток.`
      : "свод пуст";

    ui.ruleList.textContent = "";
    items.forEach(item => {
      const row = document.createElement("div");
      row.className = "rule" + (item.active ? "" : " off");
      row.dataset.rule = item.id;
      row.innerHTML = "<div class='rhead'><span class='kind'></span>"
        + "<label class='toggle'><input type='checkbox' data-rule-on>в запрос</label>"
        + "<button data-rule-drop title='Убрать инвариант' "
        + "aria-label='Убрать инвариант'>×</button></div><div class='text'></div>";
      $(".kind", row).textContent = kindTitle(item.kind);
      $(".text", row).textContent = item.text;
      $("[data-rule-on]", row).checked = item.active;
      ui.ruleList.append(row);
    });
  }

  function showVaultPanel(on) {
    ui.vault.hidden = !on;
    ui.vaultButton.setAttribute("aria-expanded", String(on));
    localStorage.setItem(VAULT, on ? "1" : "");
    // Две панели по 340 пикселей не влезают рядом с лентой на узком экране,
    // и держать их обе открытыми незачем: свод и память читают по очереди.
    if (on) showDrawer(false);
  }

  async function ruleCall(payload) {
    const data = await post("/api/agent/rules", { session: currentId, ...payload })
      .then(r => r.json());
    metrics = data.metrics;
    showVault();
    return data;
  }

  // Правка свода действует со следующей реплики — как смена модели и профиля.
  // Это и есть то, чего по самой панели не видно: прошлое не перепроверяется,
  // плашки и отклонённые варианты остаются как были.
  const RULE_SAID = {
    add: "добавлен — действует со следующей реплики; прошлые ответы "
       + "не перепроверяются",
    drop: "убран — со следующей реплики ассистент им не ограничен",
    on: "включён — со следующей реплики снова в запросе и под аудитом",
    off: "выключен — со следующей реплики не уходит ни в запрос, ни к аудитору",
  };

  // Пример к выбранному виду. Вид на проверку не влияет, поэтому и подпись
  // здесь — про то, что в этот вид кладут, а не про то, что он делает.
  function showKind() {
    const kind = kinds.find(item => item.id === ui.ruleKind.value) || kinds[0];
    ui.kindAbout.textContent = kind ? kind.about : "";
  }

  function ruleNotice(act, text) {
    const short = text.length > 40 ? text.slice(0, 40).trimEnd() + "…" : text;
    notice(`Свод: «${short}» ${RULE_SAID[act]}.`);
  }

  function addRule(event) {
    event.preventDefault();
    const text = ui.ruleText.value.trim();
    if (!text || !currentId) return;
    ruleCall({ act: "add", kind: ui.ruleKind.value, text })
      .then(data => {
        if (!data.done) {
          notice("инвариант не добавлен: в своде уже максимум записей");
          return;
        }
        ui.ruleText.value = "";
        ruleNotice("add", text);
      })
      .catch(error => notice("инвариант не добавлен: " + error.message));
  }

  // ── Состояние задачи ──────────────────────────────────────────────
  const stageTitle = id => (stages.find(stage => stage.id === id) || {}).title || id;

  // Полоса этапов появляется вместе с задачей: пока её нет, автомату нечего
  // показывать, и места под шапкой он не занимает.
  function renderTask() {
    const state = (metrics || {}).task;
    const started = state && (state.stage !== "idle" || (state.log || []).length);
    const frozen = prefs().mode === "talk";
    // В общении полоса не прячется, а гаснет: задача жива, и видно, куда
    // вернёмся. Прячется она, только пока задачи нет вовсе.
    ui.stageline.hidden = !started;
    if (ui.stageline.hidden) {
      showStagebox(false);
      return;
    }

    const path = stages.map(stage => stage.id).filter(id => id !== "idle");
    const at = path.indexOf(state.stage);
    ui.stageline.classList.toggle("paused", frozen);
    ui.stageTrack.querySelectorAll(".seg").forEach(seg => {
      const index = path.indexOf(seg.dataset.stage);
      seg.classList.toggle("past", index < at);
      seg.classList.toggle("now", index === at);
    });

    const steps = state.steps || [];
    // Номер шага справа от полосы нужен только на выполнении: на остальных
    // этапах полоса и так подписана, а шаг там ещё или уже ни при чём.
    const where = state.stage === "execution" && steps.length && state.step
      ? `шаг ${state.step} из ${steps.length}` : stageTitle(state.stage);
    ui.stageStep.textContent = frozen ? `замерло · ${where}` : where;

    const plan = $(".steps", ui.stagebox);
    plan.textContent = "";
    steps.forEach((text, index) => {
      const row = document.createElement("li");
      row.textContent = text;
      row.className = index + 1 < state.step ? "past"
        : index + 1 === state.step ? "now" : "";
      plan.append(row);
    });
    plan.hidden = !steps.length;
    $(".expect", ui.stagebox).textContent = state.expect
      ? `Ожидается: ${state.expect} — ${state.side === "agent" ? "ход ассистента" : "ваш ход"}`
      : steps.length ? "Ожидаемое действие не названо" : "Плана ещё нет";

    // Схема: горит текущий этап и стрелки, которые из него разрешены. Куда
    // просились и не пустили, на схеме не нарисовано — нарисовано только
    // разрешённое, — поэтому отказ помечает сам этап, с которого просились.
    const last = (state.log || []).at(-1);
    const refused = Boolean(last) && !last.ok && last.from === state.stage;
    ui.stagebox.querySelectorAll(".node").forEach(node => {
      const here = node.dataset.stage === state.stage;
      node.classList.toggle("now", here);
      node.classList.toggle("no", here && refused);
    });
    // Ворота: замок на переходе, пока его условие не выполнено. Условие
    // считает сервер — тем же кодом, которым он отклоняет переход, — иначе
    // схема рассказывала бы о своём наборе правил.
    const shut = (metrics || {}).task_shut || [];
    const locked = new Set(shut.map(barrier => `${state.stage}-${barrier.to}`));
    const barrier = $(".barrier", ui.stagebox);
    barrier.hidden = !shut.length;
    barrier.textContent = shut
      .map(item => `Дальше закрыто: ${item.why}`
                 + (item.key ? ` — откроет только кнопка «${item.key}»` : ""))
      .join(" · ");
    ui.stagebox.querySelectorAll(".edge").forEach(edge => {
      edge.classList.toggle("can", edge.dataset.edge.split("-")[0] === state.stage);
      edge.classList.toggle("shut", locked.has(edge.dataset.edge));
    });
    ui.stagebox.querySelectorAll(".latch").forEach(latch =>
      latch.classList.toggle("shut", locked.has(latch.dataset.edge)));

    // Ключи ворот. Кнопка неактивна там, где ключ ещё ни на что не влияет:
    // утверждать нечего, пока плана нет, и отмечать проверку — пока до неё
    // не дошли. Нажатая кнопка гасит замок на схеме.
    ui.stagebox.querySelectorAll(".key").forEach(button => {
      const key = button.dataset.act;
      button.classList.toggle("on", Boolean(state[key]));
      button.disabled = key === "approved"
        ? !steps.length || state.stage !== "planning"
        : state.stage !== "validation";
      $(".name", button).textContent = state[key]
        ? (key === "approved" ? "План утверждён" : "Проверка отмечена")
        : (key === "approved" ? "Утвердить план" : "Проверка пройдена");
    });

    const moves = $(".moves ul", ui.stagebox);
    moves.textContent = "";
    (state.log || []).slice().reverse().forEach(entry => {
      const row = document.createElement("li");
      row.className = entry.ok ? "" : "no";
      const moved = entry.from !== entry.to;
      const where = moved ? `${stageTitle(entry.from)} → ${stageTitle(entry.to)}`
        : entry.why;
      // У прошедшего перехода причина есть, только когда его пропустили со
      // снятой строгостью или снятыми воротами. Без неё в журнале не отличить
      // переход, который заслужили, от того, который просто некому было держать.
      row.textContent = where
        + (entry.ok ? (moved && entry.why ? ` (${entry.why})` : "")
                    : " — отклонён: " + entry.why)
        + ` · ${entry.by}`;
      moves.append(row);
    });
    $(".moves", ui.stagebox).hidden = !(state.log || []).length;
  }

  function showStagebox(on) {
    ui.stagebox.hidden = !on;
    ui.stageTrack.setAttribute("aria-expanded", String(on));
  }

  // Ручной переход. Кнопка сильнее заявки модели, но не сильнее таблицы:
  // что нельзя и кнопкой, сервер говорит строкой, и она идёт в ленту.
  async function taskCall(act) {
    const data = await post("/api/agent/task", { session: currentId, act })
      .then(r => r.json());
    metrics = data.metrics;
    renderTask();
    notice(`Состояние задачи: ${data.said}.`);
    await settleMode(act === "finish" && (data.metrics.task || {}).stage === "done");
  }

  // ── Режим чата ────────────────────────────────────────────────────
  const modeTitle = id => (modes.find(mode => mode.id === id) || {}).title || id;
  const mode = () => prefs().mode || "plan";

  function renderMode() {
    const now = mode();
    ui.modeName.textContent = modeTitle(now);
    ui.modeButton.classList.toggle("on", now === "plan");
    ui.modeButton.title = `Режим чата: ${(modes.find(item => item.id === now) || {}).about || ""}`;

    ui.modeMenu.textContent = "";
    modes.forEach(item => {
      const option = document.createElement("button");
      option.className = "opt" + (item.id === now ? " on" : "");
      option.setAttribute("role", "menuitemradio");
      option.dataset.mode = item.id;
      option.innerHTML = "<span class='name'></span><span class='note'></span>" + CHECK;
      $(".name", option).textContent = item.title;
      $(".note", option).textContent = item.about;
      ui.modeMenu.append(option);
    });
  }

  function showModes(on) {
    ui.modeMenu.hidden = !on;
    ui.modeButton.setAttribute("aria-expanded", String(on));
  }

  // Смена режима автомат не двигает и не стирает: он замирает на своём этапе
  // и ждёт возврата. Строка в ленте — про то, куда вернёмся, чтобы не пришлось
  // объяснять задачу заново.
  async function setMode(next) {
    if (!here() || next === mode()) return;
    const line = (metrics || {}).task_line || "";
    const live = ((metrics || {}).task || {}).stage !== "idle";
    await savePrefs({ mode: next });
    if (next === "talk") {
      notice(live ? `Режим общения: задача замерла — ${line}. Вернёшься в `
                  + "планирование — продолжим с этого места."
                  : "Режим общения: задача не ведётся, этап не появится.");
    } else {
      notice(live ? `Режим планирования: продолжаем с того же места — ${line}. `
                  + "Объяснять заново нечего."
                  : "Режим планирования: задача заведётся с первой же постановки.");
    }
  }

  // Задача закрыта — держать чат в планировании незачем: новых шагов у неё
  // не будет, а следующая задача начинается с «Новой задачи». Переключаем
  // только на самом закрытии, а не всякий раз, когда этап «готово»: иначе
  // возврат в планирование на закрытой задаче тут же отменялся бы сам.
  async function settleMode(closed) {
    if (!closed || mode() !== "plan") return;
    await savePrefs({ mode: "talk" });
    notice("Задача закрыта — чат перешёл в режим общения. "
         + "Новая задача начинается кнопкой «Новая задача».");
  }

  // ── Настройки ─────────────────────────────────────────────────────
  // Настройки — у открытого чата. Короткая память считается агентом по ним,
  // поэтому после смены счётчики чата перечитываются.
  async function savePrefs(values) {
    const item = here();
    if (!item) return;
    patch(item.id, await api("POST", `/api/chats/${item.id}/prefs`, { values }));
    syncToggles();
    turns.forEach(foot);
    await reload();
  }

  async function reload() {
    const item = here();
    if (!item || busy.has(item.id)) return;
    const data = await post("/api/agent/history", { session: item.id }).then(r => r.json());
    if (currentId !== item.id) return;
    metrics = data.metrics;
    showMemory();
    showVault();
    renderTask();
  }

  // Блок прошлого дня свёрнут: его ручки остаются, но место занимает текущий.
  function renderPrefs() {
    ui.prefFields.textContent = "";
    // Во вкладке недели 4 все дни недели 3 — прошлые: текущий день стоит над
    // ними своим блоком, а они свёрнуты. В неделе 3 окно остаётся как было.
    const past = $("#chatpane").dataset.week === "4";
    blocks.forEach(block => {
      const folded = block.folded || past;
      const part = document.createElement(folded ? "details" : "fieldset");
      part.className = "block" + (folded ? " fold" : "");
      part.innerHTML = folded ? "<summary></summary><p class='fine'></p>"
                              : "<legend></legend><p class='fine'></p>";
      $(folded ? "summary" : "legend", part).textContent = block.title;
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
          input.checked = Boolean(prefs()[field.key]);
        } else {
          input.type = "number";
          input.min = 0;
          input.step = 1;
          input.value = prefs()[field.key];
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

  function defaults() {
    const values = {};
    blocks.forEach(block => block.fields.forEach(field => (values[field.key] = field.default)));
    return values;
  }

  function readPrefs() {
    const values = {};
    ui.prefFields.querySelectorAll("[data-key]").forEach(input => {
      values[input.dataset.key] = input.type === "checkbox" ? input.checked
                                                            : Number(input.value);
    });
    return values;
  }

  // ── Профили ───────────────────────────────────────────────────────
  // Список перечитывается при каждом открытии: маршрутизатор мог дописать
  // в профиль новое, пока окно было закрыто.
  async function openProfiles(id) {
    profiles = await api("GET", "/api/profiles");
    editing = profiles.some(one => one.id === id) ? id : profileOf(here() || {}).id;
    dirty = false;
    renderPeople();
    renderCard();
    ui.people.hidden = false;
  }

  function renderPeople() {
    ui.profileList.textContent = "";
    profiles.forEach(profile => {
      const row = document.createElement("button");
      row.className = "person" + (profile.id === editing ? " on" : "");
      row.dataset.id = profile.id;
      row.innerHTML = "<span class='title'></span><span class='meta'></span>";
      $(".title", row).textContent = profile.title;
      const used = chats.filter(item => item.profile === profile.id).length;
      $(".meta", row).textContent = [profile.marks.slice(0, 2).join(" · ") || "анкета пустая",
                                     used ? `чатов ${used}` : ""].filter(Boolean).join(" · ");
      ui.profileList.append(row);
    });
    const add = document.createElement("button");
    add.className = "person add";
    add.dataset.act = "add";
    add.textContent = "+ Новый профиль";
    ui.profileList.append(add);
  }

  function labelled(label, control, wide) {
    const box = document.createElement("label");
    box.className = "row" + (wide ? " wide" : "");
    const name = document.createElement("span");
    name.textContent = label;
    box.append(name, control);
    return box;
  }

  // Анкета строится по описанию полей с сервера: там же, где из неё
  // собирается указание модели, так что поле не разойдётся с фразой.
  function renderCard() {
    const profile = profiles.find(one => one.id === editing);
    ui.profileCard.textContent = "";
    if (!profile) return;

    const title = document.createElement("input");
    title.dataset.card = "title";
    title.maxLength = 40;
    title.value = profile.title;
    const grid = document.createElement("div");
    grid.className = "grid";
    persona.texts.filter(field => field.max <= 100).forEach(field => {
      const input = document.createElement("input");
      input.dataset.card = field.key;
      input.maxLength = field.max;
      input.placeholder = field.placeholder || "";
      input.value = profile.card[field.key] || "";
      grid.append(labelled(field.label, input));
    });
    persona.choices.forEach(field => {
      const select = document.createElement("select");
      select.dataset.card = field.key;
      [{ value: "", label: "не важно" }, ...field.options].forEach(option => {
        select.append(new Option(option.label, option.value));
      });
      select.value = profile.card[field.key] || "";
      grid.append(labelled(field.label, select));
    });
    ui.profileCard.append(labelled("Название профиля", title), grid);

    persona.texts.filter(field => field.max > 100).forEach(field => {
      const area = document.createElement("textarea");
      area.dataset.card = field.key;
      area.maxLength = field.max;
      area.rows = 3;
      area.placeholder = field.placeholder || "";
      area.value = profile.card[field.key] || "";
      const left = document.createElement("small");
      left.className = "left";
      const box = labelled(field.label, area, true);
      box.append(left);
      ui.profileCard.append(box);
    });
    counters();

    const prompt = document.createElement("details");
    prompt.className = "stash prompt";
    prompt.innerHTML = "<summary>как анкета уходит в запрос</summary><pre></pre>";
    $("pre", prompt).textContent = profile.prompt || "Анкета пустая — в запрос из неё ничего не уходит.";
    ui.profileCard.append(prompt);

    const noticed = document.createElement("section");
    noticed.className = "noticed";
    noticed.innerHTML = "<h3>Замечено ассистентом</h3><p class='fine'></p><div class='records'></div>";
    const records = Object.entries(profile.learned || {});
    $(".fine", noticed).textContent = records.length
      ? "Ассистент записал это сам по вашим репликам и шлёт в запрос долговременной "
        + "памятью. ✓ — перенести в «О себе», × — забыть."
      : "Пока ничего. Скажите в чате с этим профилем что-то о себе или попросите "
        + "«дальше без эмодзи» — запись появится здесь.";
    records.forEach(([name, value]) => {
      const record = document.createElement("div");
      record.className = "record";
      record.innerHTML = "<div class='rhead'><span class='name'></span><span class='ops'>"
        + "<button data-keep='1' title='Перенести в «О себе»' aria-label='Перенести в «О себе»'>✓</button>"
        + "<button data-keep='' title='Забыть' aria-label='Забыть запись'>×</button>"
        + "</span></div><div class='value'></div>";
      $(".name", record).textContent = name;
      $(".value", record).textContent = value;
      record.querySelectorAll("button").forEach(button => (button.dataset.key = name));
      $(".records", noticed).append(record);
    });
    ui.profileCard.append(noticed);

    const actions = document.createElement("div");
    actions.className = "actions";
    actions.innerHTML = "<span class='unsaved' hidden>есть несохранённые правки</span>"
      + "<button class='plain danger' data-act='delete'>Удалить профиль</button>"
      + "<button class='primary' data-act='save'>Сохранить</button>";
    // «Основной» не удаляется: его берёт неделя 2 и чаты удалённых профилей.
    $("[data-act=delete]", actions).hidden = profile.id === profiles[0].id;
    ui.profileCard.append(actions);
  }

  function counters() {
    ui.profileCard.querySelectorAll(".row.wide").forEach(box => {
      const area = $("textarea", box);
      $(".left", box).textContent = `${area.value.length} / ${area.maxLength}`;
    });
  }

  function readCard() {
    const values = {};
    ui.profileCard.querySelectorAll("[data-card]")
      .forEach(input => (values[input.dataset.card] = input.value));
    const { title, ...card } = values;
    return { title, card };
  }

  function replaceProfile(saved) {
    profiles = profiles.map(one => (one.id === saved.id ? saved : one));
    dirty = false;
    renderPeople();
    renderCard();
    renderHeader();
  }

  async function saveCard() {
    replaceProfile(await api("PATCH", `/api/profiles/${editing}`, readCard()));
  }

  // Правки не теряются молча: перед переходом к другому профилю и перед
  // действием с записью анкета сохраняется. Закрытие окна — отмена.
  async function settle() {
    if (dirty) await saveCard();
  }

  async function addProfile() {
    await settle();
    const made = await api("POST", "/api/profiles", { title: "Новый профиль" });
    profiles.push(made);
    editing = made.id;
    renderPeople();
    renderCard();
    const title = $("[data-card=title]", ui.profileCard);
    title.focus();
    title.select();
  }

  async function deleteProfile(button) {
    if (!arm(button, "Точно удалить?")) return;
    const id = editing;
    await api("DELETE", `/api/profiles/${id}`);
    profiles = profiles.filter(one => one.id !== id);
    chats.forEach(item => { if (item.profile === id) item.profile = profiles[0].id; });
    editing = profiles[0].id;
    dirty = false;
    renderPeople();
    renderCard();
    renderList();
    renderHeader();
    await reload();
  }

  async function keepRecord(key, keep) {
    await settle();
    try {
      replaceProfile(await api("POST", `/api/profiles/${editing}/record`, { key, keep }));
    } catch (error) {
      notice("запись не перенесена: " + error.message);
      return;
    }
    if (here()?.profile === editing) await reload();
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
    ui.profileButton.disabled = on;
    ui.modeButton.disabled = on;
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

  ui.profileButton.addEventListener("click", () => showPeople(ui.profileMenu.hidden));
  ui.profileMenu.addEventListener("click", event => {
    const option = event.target.closest(".opt");
    if (!option) return;
    showPeople(false);
    if (option.dataset.act === "edit") {
      openProfiles(here()?.profile);
      return;
    }
    pickProfile(option.dataset.profile).catch(error =>
      notice("профиль не сменился: " + error.message));
  });

  $("#openProfiles").addEventListener("click", () => {
    openProfiles();
    if (narrow.matches) showSide(false);
  });
  $("#closeProfiles").addEventListener("click", () => (ui.people.hidden = true));
  ui.people.addEventListener("click", event => {
    if (event.target === ui.people) ui.people.hidden = true;
  });
  ui.profileList.addEventListener("click", async event => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.act === "add") {
      addProfile();
    } else if (button.dataset.id !== editing) {
      await settle();
      editing = button.dataset.id;
      renderPeople();
      renderCard();
    }
  });
  ui.profileCard.addEventListener("input", () => {
    dirty = true;
    counters();
    $(".unsaved", ui.profileCard).hidden = false;
  });
  ui.profileCard.addEventListener("click", event => {
    const button = event.target.closest("button");
    if (!button) return;
    const failed = error => notice("профиль не сохранён: " + error.message);
    if (button.dataset.act === "save") saveCard().catch(failed);
    else if (button.dataset.act === "delete") deleteProfile(button).catch(failed);
    else if (button.dataset.key !== undefined) keepRecord(button.dataset.key, Boolean(button.dataset.keep));
  });

  ui.newTask.addEventListener("click", newTask);
  ui.stageTrack.addEventListener("click", () => showStagebox(ui.stagebox.hidden));
  const steer = act => taskCall(act).catch(error =>
    notice("состояние задачи не сменилось: " + error.message));
  ui.modeButton.addEventListener("click", () => showModes(ui.modeMenu.hidden));
  ui.modeMenu.addEventListener("click", event => {
    const option = event.target.closest(".opt");
    if (!option) return;
    showModes(false);
    setMode(option.dataset.mode)
      .catch(error => notice("режим не сменился: " + error.message));
  });
  ui.stagebox.addEventListener("click", event => {
    const act = (event.target.closest("button") || {}).dataset?.act;
    if (!act) return;
    if (act === "newtask") newTask();
    else steer(act);
  });
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

  ui.vaultButton.addEventListener("click", () => showVaultPanel(ui.vault.hidden));
  ui.vaultAdd.addEventListener("submit", addRule);
  ui.vault.addEventListener("click", event => {
    const button = event.target.closest("button");
    if (button && button.id === "closeVault") showVaultPanel(false);
    else if (button && button.dataset.ruleDrop !== undefined) {
      // Текст берётся до вызова: после него записи в своде уже нет.
      const id = button.closest(".rule").dataset.rule;
      const gone = ruleList().find(item => item.id === id) || { text: "" };
      ruleCall({ act: "drop", rule: id })
        .then(() => ruleNotice("drop", gone.text))
        .catch(error => notice("инвариант не убран: " + error.message));
    }
  });
  ui.vault.addEventListener("change", event => {
    const box = event.target;
    if (box === ui.ruleKind) { showKind(); return; }
    if (box.dataset.ruleOn === undefined) return;
    const id = box.closest(".rule").dataset.rule;
    const rule = ruleList().find(item => item.id === id) || { text: "" };
    ruleCall({ act: "toggle", rule: id, active: box.checked })
      .then(() => ruleNotice(box.checked ? "on" : "off", rule.text))
      .catch(error => notice("инвариант не переключился: " + error.message));
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
    await savePrefs(defaults());
    renderPrefs();
  });

  document.addEventListener("click", event => {
    if (!event.target.closest("#modePicker")) showModes(false);
    if (!event.target.closest("#modelPicker")) showMenu(false);
    if (!event.target.closest("#profilePicker")) showPeople(false);
    if (!event.target.closest("#chatpane .retell")) closeRetell();
    if (!event.target.closest("#chatpane .pop, #chatpane .more")) closePops();
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    showMenu(false);
    showPeople(false);
    showModes(false);
    closeRetell();
    closePops();
    showStagebox(false);
    showVaultPanel(false);
    ui.prefs.hidden = true;
    ui.people.hidden = true;
  });

  async function boot() {
    const config = await api("GET", "/api/chat/config");
    order = config.models;
    models = Object.fromEntries(order.map(model => [model.id, model]));
    fallback = config.default;
    blocks = config.blocks;
    persona = config.persona;
    stages = config.stages;
    modes = config.modes;
    kinds = config.kinds;
    profiles = config.profiles;
    chats = await api("GET", "/api/chats");

    // В списке — только название вида: пример к нему стоит строкой ниже.
    // Вместе они не влезают в закрытый `select` и обрываются на полуслове.
    ui.ruleKind.innerHTML = kinds.map(kind =>
      `<option value="${kind.id}">${escapeHtml(kind.title)}</option>`).join("");
    showKind();
    ui.app.classList.toggle("folded", localStorage.getItem(FOLD) === "1");
    showDrawer(localStorage.getItem(DRAWER) === "1" && !narrow.matches);
    showVaultPanel(localStorage.getItem(VAULT) === "1" && !narrow.matches
                   && localStorage.getItem(DRAWER) !== "1");
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
