// Неделя 6, день 26: локальная LLM. Наверху — где живёт модель (Ollama,
// версия, квантование, сколько её в видеопамяти и когда выгрузится), ниже —
// один вопрос через три двери (CLI, родной API, OpenAI-совместимый) и
// лестница из четырёх вопросов растущей сложности с проверкой кодом.
// Если модели на этой машине нет (VPS), всё рисуется из снимка local.json,
// а кнопки выключены. Данные — /api/local/*. Подвкладки недели 6 — здесь же.

(() => {
  const state = { ready: false, view: null, live: false, busy: false, left: 0, results: [] };
  const esc = text => escapeHtml(String(text ?? ""));
  const dec = (n, d = 1) => Number(n).toFixed(d).replace(".", ",");
  const gb = bytes => `${dec(bytes / 2 ** 30)} ГБ`;
  const clock = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const DOORS = {
    cli: ["CLI", "ollama run"],
    api: ["HTTP API", "/api/chat"],
    openai: ["OpenAI API", "/v1/chat/completions"],
  };
  const MARK = { ok: "✓", part: "◐", bad: "✗" };

  async function getJSON(url, options) {
    const r = await fetch(url, options);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
    return r.json();
  }

  // Подвкладки недели 6 — как у недели 5: адрес #week6/<день>, по умолчанию последний.
  // День 27 живёт в assist.js (window.assistShow).
  const DAYS = [...document.querySelectorAll("#w6Days .rag-daytab")].map(b => b.dataset.day);
  function showDay(day) {
    day = DAYS.includes(day) ? day : DAYS.at(-1);
    document.querySelectorAll("#week6 .rag-day").forEach(d => (d.hidden = d.dataset.day !== day));
    document.querySelectorAll("#w6Days .rag-daytab")
      .forEach(b => b.classList.toggle("on", b.dataset.day === day));
    history.replaceState(null, "", "#week6/" + day);
    if (day === "27") window.assistShow?.();
    else if (!state.ready) {
      state.ready = true;
      init().catch(e => ($("#lcServerNote").textContent = "Стенд не ответил: " + e.message));
    }
  }
  document.querySelectorAll("#w6Days .rag-daytab")
    .forEach(b => b.addEventListener("click", () => showDay(b.dataset.day)));

  window.localShow = () => showDay(location.hash.split("/")[1]);

  async function init() {
    state.view = await getJSON("/api/local");
    const { status, snapshot, prompt } = state.view;
    state.live = status.live;
    $("#lcPrompt").value = snapshot.doors?.api?.prompt || prompt;
    if (!state.live) {
      $("#lcSnap").hidden = false;
      $("#lcSnap").textContent = (snapshot.status
        ? `Снимок прогона с ПК от ${snapshot.status.at}. `
        : "Снимка прогона нет. ") + (status.error
        ? `Ollama здесь не отвечает (${status.error}),`
        : `На этой машине модели ${status.model} нет,`) + " поэтому запросы выключены.";
    }
    drawServer(state.live || !snapshot.status ? status : snapshot.status);
    drawDoors(snapshot.doors || {});
    state.results = snapshot.ladder?.results || [];
    drawLadder(snapshot.ladder?.at);
    lock(false);
    setInterval(tick, 1000);
  }

  function lock(busy) {
    state.busy = busy;
    for (const id of ["#lcLoad", "#lcUnload", "#lcAsk", "#lcRun"]) $(id).disabled = busy || !state.live;
  }

  // ── Машина ────────────────────────────────────────────────────────────
  function drawServer(st) {
    const host = st.url.replace(/^https?:\/\//, "");
    const own = /^(127\.|localhost|\[::1\])/.test(host);
    const run = st.loaded;
    const share = run && run.size ? run.vram / run.size : 0;
    const where = !run ? "—" : share >= 0.995 ? "100% GPU" : share <= 0.005 ? "CPU" : `${Math.round(share * 100)}% GPU`;
    const whereNote = !run ? (st.gpu ? `${esc(st.gpu.name)} ждёт` : "модель не в памяти")
      : run.vram ? `${gb(run.vram)} в видеопамяти · ${esc(st.gpu?.name || "GPU")}`
      : `${gb(run.size)} в оперативной памяти`;
    state.left = state.live && run ? run.left : 0;
    const d = st.details;
    const tiles = [
      ["", "Сервер", `Ollama ${esc(st.version || "?")}`, `${esc(host)}${own ? " — только эта машина" : ""}`],
      ["", "Модель", esc(st.model), d ? `${esc(d.parameter_size)} параметров · ${esc(d.quantization_level)} ·
        ${gb(st.disk)} на диске` : "не скачана"],
      [run ? "ok" : "", "Где считает", where, whereNote],
      [run ? "ok" : "", "Состояние", run ? "в памяти" : "выгружена", !run ? "первый запрос поднимет её сам"
        : state.live ? `выгрузится через <span id="lcLeft">${clock(state.left)}</span> простоя`
        : `контекст ${run.context} токенов`],
    ];
    $("#lcTiles").innerHTML = tiles.map(([tone, label, value, note]) =>
      `<div class="ct-tile ${tone}"><span>${label}</span><b>${value}</b><i>${note}</i></div>`).join("");
    $("#lcServerNote").textContent = st.error
      ? st.error
      : `Опрошено ${st.at}: /api/version, /api/tags, /api/show и /api/ps — что Ollama держит в памяти и где.`;
    drawVram(st);
  }

  function drawVram(st) {
    const box = $("#lcVram");
    box.hidden = !st.gpu;
    if (!st.gpu) return;
    const { total, used, name } = st.gpu;
    const model = Math.min(st.loaded?.vram || 0, used), other = used - model;
    const pct = n => `${(n / total * 100).toFixed(1)}%`;
    box.innerHTML = `<div>Видеопамять · ${esc(name)}</div>
      <div class="lc-bar"><i class="m" style="width:${pct(model)}"></i><i class="o" style="width:${pct(other)}"></i></div>
      <div class="lc-legend"><span><b class="m"></b>модель ${gb(model)}</span>
        <span><b class="o"></b>прочее ${gb(other)}</span><span>свободно ${gb(total - used)} из ${gb(total)}</span></div>`;
  }

  async function refresh() {
    const status = await getJSON("/api/local").then(v => v.status);
    drawServer(status);
  }

  function tick() {
    if (!state.left) return;
    state.left -= 1;
    const node = $("#lcLeft");
    if (node) node.textContent = clock(state.left);
    if (!state.left && !state.busy) refresh().catch(() => {});
  }

  // Загрузка идёт секунды — счётчик на экране, чтобы было видно, что ждём.
  async function timed(note, run) {
    const start = performance.now();
    const timer = setInterval(() => ($("#lcLoadNote").textContent =
      `${note}… ${dec((performance.now() - start) / 1000)} с`), 100);
    try {
      return await run();
    } finally {
      clearInterval(timer);
    }
  }

  $("#lcLoad").addEventListener("click", async () => {
    lock(true);
    try {
      const res = await timed("модель грузится в память", () => getJSON("/api/local/load", { method: "POST" }));
      $("#lcLoadNote").textContent = `загрузка заняла ${dec(res.seconds)} с`;
      drawServer(res.status);
    } catch (e) {
      $("#lcLoadNote").textContent = "не загрузилась: " + e.message;
    }
    lock(false);
  });

  $("#lcUnload").addEventListener("click", async () => {
    lock(true);
    try {
      const res = await getJSON("/api/local/unload", { method: "POST" });
      $("#lcLoadNote").textContent = "выгружена — следующий запрос начнётся с загрузки";
      drawServer(res.status);
    } catch (e) {
      $("#lcLoadNote").textContent = "не выгрузилась: " + e.message;
    }
    lock(false);
  });

  // ── Метрики ответа ────────────────────────────────────────────────────
  // OpenAI-совместимый путь длительностей не сообщает — его скорость стенд
  // меряет своими часами, отсюда «≈».
  function meta(m, door) {
    const parts = [];
    if (m.load >= 0.1) parts.push(`загрузка ${dec(m.load)} с`);
    if (m.ttft != null) parts.push(`первый токен ${dec(m.ttft, 2)} с`);
    parts.push(`всего ${dec(m.seconds, 2)} с`);
    if (m.out != null) parts.push(`${m.in} → ${m.out} ток`);
    if (m.tps) parts.push(`${door === "openai" ? "≈" : ""}${Math.round(m.tps)} ток/с`);
    parts.push("0 ₽");
    return parts.join(" · ");
  }

  // ── Три двери ─────────────────────────────────────────────────────────
  function drawDoors(saved) {
    $("#lcDoors").innerHTML = Object.entries(DOORS).map(([door, [title, path]]) => {
      const s = saved[door];
      return `<div class="lc-door${s ? " done" : ""}" data-door="${door}">
        <h3>${title} <span>${esc(path)}</span><em>${s ? `снято ${esc(s.at.slice(11, 16))}` : "ждёт"}</em></h3>
        <pre class="lc-cmd">${s ? "$ " + esc(s.command) : ""}</pre>
        <div class="ask-answer">${esc(s?.answer)}</div>
        <div class="ask-meta">${s ? meta(s.metrics, door) : ""}</div>
        <details class="ask-prompt"><summary>Сырой вывод</summary><pre>${esc(s?.raw?.join("\n"))}</pre></details>
      </div>`;
    }).join("");
  }

  async function askDoor(door, prompt) {
    const box = $(`.lc-door[data-door="${door}"]`);
    box.className = "lc-door run";
    $("h3 em", box).textContent = "идёт";
    for (const sel of [".lc-cmd", ".ask-answer", ".ask-meta", "pre:not(.lc-cmd)"]) $(sel, box).textContent = "";
    const answer = $(".ask-answer", box);
    await stream("/api/local/ask", { door, prompt }, e => {
      if (e.t === "start") $(".lc-cmd", box).textContent = "$ " + e.command;
      else if (e.t === "delta") answer.textContent += e.text;
      else if (e.t === "done") {
        box.className = "lc-door done";
        $("h3 em", box).textContent = "✓ ответила";
        answer.textContent = e.answer;
        $(".ask-meta", box).textContent = meta(e.metrics, door);
        $("pre:not(.lc-cmd)", box).textContent = e.raw.join("\n");
      } else if (e.t === "error") {
        box.className = "lc-door fail";
        $("h3 em", box).textContent = "✗ ошибка";
        answer.textContent = e.message;
      }
    });
  }

  $("#lcAsk").addEventListener("click", async () => {
    const prompt = $("#lcPrompt").value.trim();
    if (!prompt) return;
    lock(true);
    for (const door of Object.keys(DOORS)) {
      $(`.lc-door[data-door="${door}"] h3 em`).textContent = "в очереди";
    }
    try {
      for (const door of Object.keys(DOORS)) await askDoor(door, prompt);
      await refresh();
    } catch (e) {
      $("#lcServerNote").textContent = "Запрос не прошёл: " + e.message;
    }
    lock(false);
  });

  // ── Лестница ──────────────────────────────────────────────────────────
  function drawLadder(at) {
    const items = state.view.ladder;
    $("#lcSteps").innerHTML = items.map((item, i) => {
      const r = state.results[i];
      const g = r?.grade;
      return `<div class="lc-step ${g ? g.mark : ""}" data-i="${i}">
        <div class="lc-side"><b>${i + 1} · ${esc(item.level)}</b><span>${esc(item.what)}</span>
          <span class="ask-mark ${g ? g.mark : ""}">${g ? `${MARK[g.mark]} ${esc(g.note)}` : r?.error ? "✗ ошибка" : ""}</span></div>
        <div class="lc-body"><div class="lc-q">${esc(item.q)}</div>
          <div class="ask-answer">${esc(r?.answer || r?.error)}</div>
          <div class="ask-meta">${r?.metrics ? meta(r.metrics, "api") : ""}</div></div>
      </div>`;
    }).join("");
    drawSum();
    $("#lcRunNote").textContent = at ? `прогон ${at}` : "";
  }

  function drawSum() {
    const done = state.results.filter(r => r?.grade);
    if (!done.length) {
      $("#lcSum").innerHTML = "";
      return;
    }
    const count = mark => done.filter(r => r.grade.mark === mark).length;
    const ok = count("ok"), n = state.view.ladder.length;
    const speeds = done.map(r => r.metrics.tps).filter(Boolean);
    const seconds = done.reduce((s, r) => s + r.metrics.seconds, 0);
    const out = done.reduce((s, r) => s + (r.metrics.out || 0), 0);
    const tiles = [
      [ok === n ? "ok" : ok ? "part" : "bad", "Верно", `${ok} из ${n}`, `◐ частично ${count("part")} · ✗ мимо ${count("bad")}`],
      ["", "Скорость", `${Math.round(speeds.reduce((a, b) => a + b, 0) / speeds.length)} ток/с`, "в среднем по ответам"],
      ["", "Время", `${dec(seconds)} с`, `на ${done.length} ${done.length > 4 ? "вопросов" : "вопроса"}, ${out} ток на выходе`],
      ["ok", "Цена", "0 ₽", "ни ключа, ни внешней сети"],
    ];
    $("#lcSum").innerHTML = tiles.map(([tone, label, value, note]) =>
      `<div class="ct-tile ${tone}"><span>${label}</span><b>${value}</b><i>${note}</i></div>`).join("");
  }

  $("#lcRun").addEventListener("click", async () => {
    lock(true);
    state.results = [];
    drawLadder();
    $("#lcRunNote").textContent = "идёт…";
    const step = i => $(`.lc-step[data-i="${i}"]`);
    try {
      await stream("/api/local/ladder", {}, e => {
        if (e.t === "item") step(e.i).className = "lc-step run";
        else if (e.t === "delta") $(".ask-answer", step(e.i)).textContent += e.text;
        else if (e.t === "done" || e.t === "error") {
          state.results[e.i] = e.t === "done"
            ? { answer: e.answer, metrics: e.metrics, grade: e.grade } : { error: e.message };
          const box = step(e.i), g = e.grade;
          box.className = `lc-step ${g ? g.mark : "bad"}`;
          $(".lc-side .ask-mark", box).className = `ask-mark ${g ? g.mark : "bad"}`;
          $(".lc-side .ask-mark", box).textContent = g ? `${MARK[g.mark]} ${g.note}` : "✗ ошибка";
          if (g) $(".ask-meta", box).textContent = meta(e.metrics, "api");
          else $(".ask-answer", box).textContent = e.message;
          drawSum();
        } else if (e.t === "end") {
          $("#lcRunNote").textContent = e.saved ? "прогон записан в снимок local.json" : "прогон не полный — снимок не тронут";
        }
      });
      await refresh();
    } catch (e) {
      $("#lcRunNote").textContent = "прогон оборвался: " + e.message;
    }
    lock(false);
  });

  // Открыли сразу по адресу #week6 — showPane страницы сработал до этого файла.
  if (!$("#week6").hidden) window.localShow();
})();
