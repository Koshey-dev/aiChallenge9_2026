// Неделя 5, день 25: мини-чат с RAG и памятью задачи. Слева — сценарии и
// чаты, в центре — лента: у ответа видно, что искали, источники [n] и
// проверки хода; реплики, выпавшие из окна истории, приглушены. Справа —
// память задачи (цель, уточнено, ограничения, термины, выводы) с тем, что
// поменял ход; клик по ходу показывает память на тот момент. Внизу —
// проверка сценария. Данные — /api/rag/talk/*. Подвкладку показывает rag.js
// (window.talkShow).

(() => {
  const root = $('.rag-day[data-day="25"]');
  const state = { ready: false, config: null, chat: null, busy: false, stop: false, pinned: null };
  const esc = text => escapeHtml(String(text ?? ""));
  const dec = (n, d = 2) => Number(n).toFixed(d).replace(".", ",");
  const KEYS = ["goal", "clarified", "constraints", "terms", "decisions"];
  const mark = ok => `<span class="ask-mark ${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"}</span>`;

  window.talkShow = () => {
    if (state.ready) return;
    state.ready = true;
    init().catch(e => ($("#tkThread").innerHTML = `<p class="err">Чат не загрузился: ${esc(e.message)}</p>`));
  };

  async function init() {
    state.config = await (await fetch("/api/rag/talk/setup")).json();
    const { models, default: model } = state.config;
    $("#tkModel").innerHTML = models.map(m =>
      `<option value="${esc(m.id)}"${m.id === model ? " selected" : ""}>${esc(m.title)}</option>`).join("");
    drawChats(state.config.chats);
    drawScenarios();
    const last = state.config.chats[0];
    if (last) await openChat(last.id);
    else drawChat({ title: "Новый чат", turns: [], state: null, scenario: "" });
  }

  // ── Слева: сценарии и чаты ───────────────────────────────────────────
  function drawScenarios() {
    $("#tkScen").innerHTML = state.config.scenarios.map(s => `
      <div class="tk-scard" data-id="${s.id}"><b>${esc(s.title)}</b>
        <span class="hint">${s.turns.length} реплик: ${[...new Set(s.turns.map(t => t.kind))].join(", ")}</span>
        <button data-run="${s.id}">Прогнать</button></div>`).join("");
    root.querySelectorAll("[data-run]").forEach(b => b.addEventListener("click", () => runScenario(b.dataset.run)));
  }

  function scoreLine(s) {
    if (!s) return "";
    const judge = s.judge ? ` · итог ${{ ok: "✓", part: "◐", bad: "✗" }[s.judge.verdict]}` : "";
    return `источники ${s.sources}/${s.checked} · цель ${s.goal}/${s.checked}${judge}`;
  }

  function drawChats(chats) {
    $("#tkChats").innerHTML = chats.map(c => `
      <li data-id="${c.id}" class="${state.chat?.id === c.id ? "on" : ""}">
        <b>${esc(c.title)}</b><span class="hint">${c.count} ход. · ${esc(c.updated.slice(5, 16))}</span>
        ${c.score ? `<span class="hint">${scoreLine(c.score)}</span>` : ""}</li>`).join("");
    $("#tkChats").querySelectorAll("li").forEach(li => li.addEventListener("click", () => !state.busy && openChat(li.dataset.id)));
  }

  async function refreshChats() {
    const setup = await (await fetch("/api/rag/talk/setup")).json();
    drawChats(setup.chats);
  }

  async function openChat(id) {
    state.chat = await (await fetch(`/api/rag/talk/chat/${id}`)).json();
    state.pinned = null;
    drawChat(state.chat);
    $("#tkChats").querySelectorAll("li").forEach(li => li.classList.toggle("on", li.dataset.id === id));
  }

  async function newChat(scenario = "") {
    state.chat = await (await fetch("/api/rag/talk/new", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario, model: $("#tkModel").value }) })).json();
    state.chat.score = null;
    state.pinned = null;
    drawChat(state.chat);
    await refreshChats();
  }
  $("#tkNew").addEventListener("click", () => !state.busy && newChat());

  // ── Лента ─────────────────────────────────────────────────────────────
  const where = s => `«${esc(s.title)}» › ${esc(s.section)}` + (s.page_from ? ` · стр. ${s.page_from}` : "");

  function searchLine(t) {
    const s = t.search;
    if (!s) return "ищу…";
    const best = s.best ? ` · лучший cos ${dec(s.best, 3)}` : "";
    if (s.gate) return `искали: ${s.queries.map(q => `«${esc(q)}»`).join(" · ")}${best} — ниже порога, «не знаю» и уточнение`;
    if (!s.queries.length) return `планировщик не дал запросов — искали по самой реплике${best}, плюс источники прошлых ответов · фрагментов: ${s.context.length}`;
    return `искали: ${s.queries.map(q => `«${esc(q)}»`).join(" · ")}${best} · фрагментов в контексте: ${s.context.length}`;
  }

  function checksLine(c) {
    if (!c) return "";
    const out = [`${mark(c.sources)} источники`, `${mark(c.goal)} цель в памяти`];
    if (c.of) out.push(`${mark(c.facts === c.of)} факты ${c.facts} из ${c.of}${c.refused ? " (ответ — отказ)" : ""}`);
    for (const m of c.memory || []) out.push(`${mark(m.ok)} в памяти: ${esc(m.label)}`);
    if ("dropped" in c) out.push(`${mark(c.dropped)} старое значение убрано`);
    return out.join(" · ");
  }

  function changesLine(changes) {
    if (!changes?.length) return "";
    const titles = state.config.titles;
    return changes.map(c => c.add ? `<span class="add">+ ${esc(titles[c.key])}: ${esc(c.add)}</span>`
      : `<span class="drop">− ${esc(titles[c.key])}: ${esc(c.drop)}</span>`).join("");
  }

  function turnHtml(t, i) {
    const answer = markdown(t.answer || "")
      .replace(/\[(\d+)\]/g, (_, n) => `<button class="ask-cite" data-n="${n}">[${n}]</button>`);
    return `<div class="tk-turn" data-i="${i}">
      <div class="tk-user">${t.kind ? `<span class="tk-kind">${esc(t.kind)}</span>` : ""}${esc(t.user)}</div>
      <div class="tk-changes">${changesLine(t.changes)}</div>
      <div class="tk-bot">
        <div class="tk-search">${searchLine(t)}</div>
        <div class="tk-answer">${answer}</div>
        ${t.error ? `<div class="err">${esc(t.error)}</div>` : ""}
        <ol class="tk-sources">${(t.sources || []).map(s => `<li data-n="${s.n}"><b>[${s.n}]</b> ${where(s)}
          <code>${esc(s.chunk_id)}</code></li>`).join("")}</ol>
        ${t.answer && !t.search?.gate && !(t.sources || []).length ? `<div class="err">ответ без ссылок [n] — источников нет</div>` : ""}
        <div class="tk-checks">${checksLine(t.check)}</div>
      </div></div>`;
  }

  // Окно истории — последние WINDOW реплик: следующий ответ видит только их.
  function drawWindow() {
    const turns = root.querySelectorAll(".tk-turn");
    const keep = state.config.window / 2;
    root.querySelector(".tk-cut")?.remove();
    turns.forEach((el, i) => el.classList.toggle("out", i < turns.length - keep));
    if (turns.length > keep) {
      turns[turns.length - keep].insertAdjacentHTML("beforebegin",
        `<div class="tk-cut">↑ вне окна истории: эти реплики модель уже не видит — их держит память задачи</div>`);
    }
  }

  function drawChat(chat) {
    $("#tkTitle").textContent = chat.title;
    $("#tkThread").innerHTML = chat.turns.length ? chat.turns.map(turnHtml).join("")
      : `<p class="hint tk-empty">Напишите сообщение или запустите сценарий слева.</p>`;
    drawWindow();
    drawMemory();
    drawScore(chat);
    $("#tkThread").scrollTop = $("#tkThread").scrollHeight;
  }

  // ── Память задачи ─────────────────────────────────────────────────────
  function drawMemory() {
    const turns = state.chat?.turns || [];
    const i = state.pinned ?? turns.length - 1;
    const t = turns[i];
    const mem = t?.state || state.chat?.state;
    root.querySelectorAll(".tk-turn").forEach(el => el.classList.toggle("pin", state.pinned !== null && Number(el.dataset.i) === i));
    $("#tkMemAt").innerHTML = t ? `после хода ${i + 1}${state.pinned !== null ? ` · <button class="tk-now">сейчас</button>` : ""}` : "пусто";
    root.querySelector(".tk-now")?.addEventListener("click", () => { state.pinned = null; drawMemory(); });
    if (!mem) return ($("#tkMem").innerHTML = `<p class="hint">Память пуста — она заполнится с первым сообщением.</p>`);
    const added = new Set((t?.changes || []).filter(c => c.add).map(c => `${c.key}|${c.add}`));
    const dropped = (t?.changes || []).filter(c => c.drop && c.key !== "goal");
    $("#tkMem").innerHTML = KEYS.map(k => {
      const items = k === "goal" ? (mem.goal ? [mem.goal] : []) : mem[k];
      const gone = dropped.filter(c => c.key === k);
      return `<section class="tk-mk" data-key="${k}"><h4>${esc(state.config.titles[k])}</h4>
        ${items.length || gone.length ? `<ul>${items.map(x => `<li class="${added.has(`${k}|${x}`) ? "new" : ""}">${esc(x)}</li>`).join("")}
          ${gone.map(c => `<li class="gone">${esc(c.drop)}</li>`).join("")}</ul>` : `<p class="hint">—</p>`}</section>`;
    }).join("");
  }

  $("#tkThread").addEventListener("click", e => {
    const turn = e.target.closest(".tk-turn");
    if (!turn || e.target.closest("button, a")) return;
    state.pinned = Number(turn.dataset.i);
    drawMemory();
  });
  root.addEventListener("pointerover", e => {
    const cite = e.target.closest(".ask-cite");
    root.querySelectorAll(".tk-sources li").forEach(li => li.classList.remove("hot"));
    if (cite) $(`.tk-sources li[data-n="${cite.dataset.n}"]`, cite.closest(".tk-bot"))?.classList.add("hot");
  });

  // ── Отправка ──────────────────────────────────────────────────────────
  async function send(text) {
    if (!text.trim() || state.busy) return;
    if (!state.chat?.id) await newChat();
    state.busy = true;
    $("#tkSend").disabled = true;
    $("#tkText").value = "";
    state.pinned = null;
    // Номер хода — до отправки: к концу ответа ход уже в списке, и длина на единицу больше.
    const index = state.chat.turns.length;
    const t = { user: text, kind: "", answer: "", changes: [], sources: [] };
    const scenario = state.config.scenarios.find(s => s.id === state.chat.scenario);
    t.kind = scenario?.turns[index]?.say === text ? scenario.turns[index].kind : "";
    root.querySelector(".tk-empty")?.remove();
    $("#tkThread").insertAdjacentHTML("beforeend", turnHtml(t, index));
    const el = $("#tkThread").lastElementChild;
    drawWindow();
    $("#tkThread").scrollTop = $("#tkThread").scrollHeight;
    const redraw = () => {
      el.outerHTML = turnHtml(t, index);
      $("#tkThread").scrollTop = $("#tkThread").scrollHeight;
    };
    let current = el;
    try {
      await stream("/api/rag/talk/send", { chat: state.chat.id, text }, e => {
        current = $("#tkThread").lastElementChild;
        if (e.t === "plan") {
          Object.assign(t, { changes: e.changes, state: e.state });
          $(".tk-changes", current).innerHTML = changesLine(e.changes);
          state.chat.turns.push({ ...t });
          drawMemory();
          state.chat.turns.pop();
        } else if (e.t === "search") {
          t.search = e;
          $(".tk-search", current).innerHTML = searchLine(t);
        } else if (e.t === "delta") {
          t.answer += e.text;
          $(".tk-answer", current).innerHTML = markdown(t.answer)
            .replace(/\[(\d+)\]/g, (_, n) => `<button class="ask-cite" data-n="${n}">[${n}]</button>`);
          $("#tkThread").scrollTop = $("#tkThread").scrollHeight;
        } else if (e.t === "done") {
          state.chat.turns.push(e.turn);
          Object.assign(t, e.turn);
          state.chat.score = e.score;
          redraw();
          drawWindow();
          drawMemory();
          drawScore(state.chat);
        } else if (e.t === "error") {
          $(".tk-search", current).innerHTML = `<span class="err">${esc(e.message)}</span>`;
        }
      });
    } catch (e) {
      $(".tk-search", current).innerHTML = `<span class="err">запрос оборвался: ${esc(e.message)}</span>`;
    }
    state.busy = false;
    $("#tkSend").disabled = false;
    await refreshChats();
  }
  $("#tkSend").addEventListener("click", () => send($("#tkText").value));
  $("#tkText").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("#tkText").value); }
  });

  // Сценарий: новый чат и реплики по очереди — каждая ждёт ответа на прошлую.
  async function runScenario(id) {
    if (state.busy) return;
    const scenario = state.config.scenarios.find(s => s.id === id);
    await newChat(id);
    root.querySelectorAll("[data-run]").forEach(b => (b.disabled = true));
    for (const turn of scenario.turns) {
      $("#tkText").value = turn.say;
      await new Promise(r => setTimeout(r, 400));
      await send(turn.say);
    }
    root.querySelectorAll("[data-run]").forEach(b => (b.disabled = false));
  }

  // ── Проверка сценария ─────────────────────────────────────────────────
  function tile(label, value, note, ok) {
    return `<div class="ct-tile ${ok ? "ok" : "bad"}"><span>${label}</span><b>${value}</b><i>${note}</i></div>`;
  }

  function drawScore(chat) {
    const scenario = state.config.scenarios.find(s => s.id === chat.scenario);
    $("#tkScore").textContent = scenario ? scoreLine(chat.score) : "";
    $("#tkCheckCard").hidden = !scenario;
    if (!scenario) return;
    const s = chat.score || { checked: 0, sources: 0, goal: 0, facts: 0, of: 0, memory: 0, memory_of: 0, dropped: [], judge: null };
    $("#tkCheckTitle").textContent = `Проверка сценария «${scenario.title}» — ${chat.turns.length} из ${scenario.turns.length} реплик`;
    const judge = s.judge;
    $("#tkTiles").innerHTML = [
      tile("Ответы с источниками", `${s.sources} из ${s.checked}`, "ссылки [n] на фрагменты контекста", s.sources === s.checked),
      tile("Цель в памяти", `${s.goal} из ${s.checked}`, "на каждом ходу, после вопросов в сторону тоже", s.goal === s.checked),
      tile("Факты из базы", `${s.facts} из ${s.of}`, "ключевые строки в ответах на вопросы", s.facts === s.of),
      tile("Договорённости в памяти", `${s.memory} из ${s.memory_of}`, s.dropped.length
        ? `смена ограничения: старое ${s.dropped.every(Boolean) ? "убрано" : "осталось"}` : "уточнения, ограничения, термины", s.memory === s.memory_of && s.dropped.every(Boolean)),
      tile("Итог сверен с целью", judge ? `${judge.yes} из ${judge.yes + judge.partial + judge.no}` : "—",
        judge ? `частично ${judge.partial} · нет ${judge.no} · судья: ${state.config.judge}` : "после итоговой реплики", judge?.verdict === "ok"),
    ].join("");
    $("#tkTable").innerHTML = `<tr><th>№</th><th>Вид</th><th>Реплика</th><th class="num">Лучший cos</th><th>Источники</th>
      <th>Факты</th><th>Цель в памяти</th><th>Память</th></tr>` + scenario.turns.map((st, i) => {
        const t = chat.turns[i], c = t?.check;
        if (!t) return `<tr class="skip"><td class="num">${i + 1}</td><td>${esc(st.kind)}</td><td>${esc(st.say)}</td><td colspan="5" class="hint">ещё не отправлена</td></tr>`;
        return `<tr class="ask-row" data-i="${i}"><td class="num">${i + 1}</td><td>${esc(st.kind)}</td><td>${esc(st.say)}</td>
          <td class="num">${t.search?.best ? dec(t.search.best, 3) : "—"}</td>
          <td>${c ? mark(c.sources) + ` ${t.sources.length}` : "—"}</td>
          <td>${c?.of ? `${mark(c.facts === c.of)} ${c.facts}/${c.of}` : "—"}</td>
          <td>${c ? mark(c.goal) : "—"}</td>
          <td>${c?.memory ? c.memory.map(m => `${mark(m.ok)} ${esc(m.label)}`).join("<br>") + ("dropped" in c ? `<br>${mark(c.dropped)} старое убрано` : "") : "—"}</td></tr>`;
      }).join("");
    $("#tkTable").querySelectorAll(".ask-row").forEach(row => row.addEventListener("click", () => {
      state.pinned = Number(row.dataset.i);
      drawMemory();
      root.querySelector(`.tk-turn[data-i="${row.dataset.i}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    }));
    const last = [...chat.turns].reverse().find(t => t.judge)?.judge;
    const word = { yes: ["ok", "✓ да"], partial: ["part", "◐ частично"], no: ["bad", "✗ нет"] };
    $("#tkJudge").innerHTML = last ? `<h3>Итог против эталона сценария</h3><table class="rag-table">
      <tr><th>Пункт эталона</th><th>В итоге</th><th>Почему</th></tr>${last.items.map(x => `<tr><td>${esc(x.item)}</td>
      <td><span class="ask-mark ${word[x.verdict][0]}">${word[x.verdict][1]}</span></td><td class="hint">${esc(x.why)}</td></tr>`).join("")}</table>` : "";
  }

  if (!root.hidden) window.talkShow();
})();
