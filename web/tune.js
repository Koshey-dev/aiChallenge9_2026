// Неделя 6, день 29: оптимизация локальной модели под RAG дня 28. Наверху —
// конфигурации «до» и «после» (модель aichallenge-rag в Ollama и её Modelfile)
// и итог прогона; ниже — один вопрос обеим, лестница ступеней с качеством,
// стабильностью, скоростью и памятью и сравнение квантов. Без модели на машине
// (VPS) — снимок прогонов с ПК, запросы выключены. Данные — /api/tune/*,
// подвкладку показывает local.js.

(() => {
  const root = $('.rag-day[data-day="29"]');
  const state = { ready: false, live: false, view: null, steps: {}, busy: false, run: null, open: null, pick: null };
  const esc = text => escapeHtml(String(text ?? ""));
  const dec = (n, d = 1) => n == null ? "—" : Number(n).toFixed(d).replace(".", ",");
  const fmt = n => n == null ? "—" : Math.round(n).toLocaleString("ru");
  const gb = bytes => bytes == null ? "—" : `${dec(bytes / 2 ** 30)} ГБ`;
  const MARK = { ok: ["✓", "верно"], part: ["◐", "частично"], bad: ["✗", "мимо"] };
  const SETS = { d22: "набор дня 22", talk: "разговорные" };
  const col = name => $(`.ask-col[data-col="${name}"]`, root);
  const stepOf = id => state.view.steps.find(s => s.id === id);

  async function getJSON(url, options) {
    const r = await fetch(url, options);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
    return r.json();
  }

  window.tuneShow = () => {
    if (state.ready) return;
    state.ready = true;
    init().catch(e => ($("#tuEvalNote").textContent = "Стенд не ответил: " + e.message));
  };

  async function init() {
    const v = state.view = await getJSON("/api/tune");
    state.live = v.live;
    state.steps = v.snapshot?.steps || {};
    if (!state.live) {
      $("#tuSnap").hidden = false;
      $("#tuSnap").textContent = (v.snapshot ? `Снимок прогонов с ПК от ${v.snapshot.at}. ` : "Снимка прогонов нет. ")
        + (v.status.error ? `Ollama здесь не отвечает (${v.status.error}),` : `На этой машине модели ${v.status.model} нет,`)
        + " поэтому запросы выключены.";
    }
    $("#tuList").innerHTML = v.questions.flatMap(x => [x.q, x.talk]).map(q => `<option value="${esc(q)}">`).join("");
    $(".tu-who", col("before")).textContent = stepOf("before").model;
    $(".tu-who", col("after")).textContent = v.tuned;
    drawAll();
    lock(false);
  }

  function drawAll() {
    drawConfigs();
    drawResult();
    drawLadder();
    drawQuant();
    if (state.open) drawDetail(state.open);
  }

  function lock(busy) {
    state.busy = busy;
    for (const id of ["#tuGo", "#tuEval", "#tuQ", "#tuCreate"]) if ($(id)) $(id).disabled = busy || !state.live;
    root.querySelectorAll(".tu-run").forEach(b => (b.disabled = busy || !state.live));
  }

  // ── Память: сколько модели в видеокарте и сколько на процессоре ──────
  const modelIn = (memory, model) => memory?.models.find(m => m.name === model || m.name === model + ":latest");

  function vramBar(memory, model) {
    const m = modelIn(memory, model), g = memory?.gpu;
    if (!m || !g) return `<span class="hint">нет данных о памяти</span>`;
    const pct = n => `${Math.max(0, n / g.total * 100).toFixed(1)}%`;
    const other = Math.max(0, g.used - m.vram);
    const cpu = m.size - m.vram;
    return `<div class="lc-bar tu-bar"><i class="m" style="width:${pct(m.vram)}"></i><i class="o" style="width:${pct(other)}"></i></div>
      <div class="hint">в видеокарте ${gb(m.vram)} из ${gb(g.total)}${cpu > 2 ** 20
        ? ` · <span class="tu-cpu">на процессоре ${gb(cpu)}</span>` : " · вся модель"}</div>`;
  }

  // ── До и после ───────────────────────────────────────────────────────
  function config(id, options, extra) {
    const s = stepOf(id), rec = state.steps[id];
    const disk = rec?.disk ?? state.view.disk?.[s.model] ?? state.view.disk?.[s.model + ":latest"];
    const rows = [
      ["temperature", dec(options.temperature, options.temperature ? 1 : 0)],
      ["seed", options.seed ?? "случайный"],
      ["предел ответа", `${options.num_predict} ток.`],
      ["окно", `${fmt(options.num_ctx)} ток.`],
      ["слои в видеокарте", options.num_gpu ? `все (num_gpu ${options.num_gpu})` : "сколько решит Ollama"],
      ["эмбеддер", s.embed ? "на процессоре" : "в видеокарте"],
      ["промпт", s.prompt === "d22" ? "дня 22" : "под задачу"],
    ];
    const prompt = state.view.prompts[s.prompt === "d22" ? "d22" : "tune"];
    return `<h3>${esc(s.title)}</h3>
      <div class="tu-model"><b>${esc(s.model)}</b> · ${gb(disk)} на диске</div>
      <dl class="tu-params">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      ${rec ? vramBar(rec.memory, s.model) : `<span class="hint">ступень ещё не прогоняли</span>`}
      <details class="ask-prompt"><summary>Промпт: ${s.prompt === "d22" ? "правила дня 22" : "правила, два примера, вопрос до и после фрагментов"}</summary>
        <pre class="tu-pre">${prompt.map(m => `<b>${m.role}</b>\n${esc(m.content)}`).join("\n\n")}</pre></details>
      ${extra || ""}`;
  }

  function drawConfigs() {
    const v = state.view, c = v.created;
    $("#tuBefore").innerHTML = config("before", stepOf("before").options);
    $("#tuAfter").innerHTML = config("after", v.tuned_options, `
      <details class="ask-prompt" open><summary>Modelfile модели ${esc(v.tuned)}</summary>
        <pre class="tu-pre">${esc(v.modelfile)}</pre></details>
      <div class="rag-bar tu-create"><button id="tuCreate">Собрать модель в Ollama</button>
        <span class="hint" id="tuCreated">${c?.exists
          ? `есть в Ollama: ${esc(v.tuned)} · ${esc(c.quant)} · ${gb(c.disk)} на диске — слои общие с q8_0 · ${esc(c.digest)}`
          : state.live ? "в Ollama её ещё нет" : "на этой машине Ollama нет"}</span></div>`);
    $("#tuCreate").addEventListener("click", create);
    $("#tuCreate").disabled = state.busy || !state.live;
  }

  async function create() {
    if (state.busy) return;
    lock(true);
    $("#tuCreated").textContent = "собираю…";
    try {
      const c = await getJSON("/api/tune/create", { method: "POST" });
      state.view.created = c;
      $("#tuCreated").textContent = `собрана за ${dec(c.seconds, 2)} с: ${state.view.tuned} · ${c.quant} · `
        + `${gb(c.disk)} на диске — слои общие с q8_0 · ${c.digest}`;
    } catch (e) {
      $("#tuCreated").textContent = "не собралась: " + e.message;
    }
    lock(false);
  }

  function drawResult() {
    const a = state.steps.before?.summary, b = state.steps.after?.summary;
    if (!a || !b) {
      $("#tuResult").innerHTML = `<p class="hint">Итог появится, когда пройдут ступени «До» и «После».</p>`;
      return;
    }
    const ma = modelIn(state.steps.before.memory, state.steps.before.model);
    const mb = modelIn(state.steps.after.memory, state.steps.after.model);
    const pair = (label, x, y, note) => `<div class="ct-tile lr-pair"><span>${label}</span>
      <div><b>${x}</b><em>до</em></div><div><b>${y}</b><em>после</em></div>${note ? `<i>${note}</i>` : ""}</div>`;
    $("#tuResult").innerHTML =
      pair("Качество · набор дня 22", `${a.d22.ok}/${a.d22.total}`, `${b.d22.ok}/${b.d22.total}`,
           `фактов ${a.d22.facts} → ${b.d22.facts} из ${b.d22.of}, ссылка на нужный документ ${a.d22.cited_ok} → ${b.d22.cited_ok} из ${b.d22.inside}`) +
      pair("Проверка · разговорные слова", `${a.talk.ok}/${a.talk.total}`, `${b.talk.ok}/${b.talk.total}`,
           "их при подборе промпта не смотрели") +
      pair("Стабильность · вердикт держится", `${a.d22.stable}/${a.d22.repeated}`, `${b.d22.stable}/${b.d22.repeated}`,
           `дословно тот же ответ: ${a.d22.same} → ${b.d22.same}; пустых и «одни ссылки» ${a.d22.empty} → ${b.d22.empty}`) +
      pair("Скорость · ответ целиком", `${dec(a.d22.seconds)} с`, `${dec(b.d22.seconds)} с`,
           `первый токен ${dec(a.d22.ttft, 2)} → ${dec(b.d22.ttft, 2)} с, генерация ${dec(a.d22.tps, 0)} → ${dec(b.d22.tps, 0)} ток/с, `
           + `ответ ${fmt(a.d22.tokens_out)} → ${fmt(b.d22.tokens_out)} ток.`) +
      pair("Память · видеокарта", gb(ma?.vram), gb(mb?.vram),
           `на диске ${gb(state.steps.before.disk)} → ${gb(state.steps.after.disk)}; эмбеддер ушёл на процессор`);
  }

  // ── Лестница ─────────────────────────────────────────────────────────
  const share = s => s && s.total ? s.ok / s.total : null;

  function quality(s, base, live) {
    if (!s) return live || `<span class="hint">—</span>`;
    const d = base && share(base) != null ? share(s) - share(base) : 0;
    const tone = Math.abs(d) < 0.001 ? "" : d > 0 ? "tu-up" : "tu-down";
    const flags = s.empty + s.cut + s.alien + s.bad_refs;
    return `<b class="${tone}">${s.ok}/${s.total}</b>
      <div class="hint">факты ${s.facts}/${s.of} · ссылки ${s.cited_ok}/${s.inside} · отказ ${s.outside}/${s.outside_of}${
        flags ? ` · <span class="tu-down">сбоев ${flags}</span>` : ""}</div>`;
  }

  // Окно 2048: Ollama молча режет начало промпта — видно, если сравнить токены на входе со ступенью «Параметры».
  function trimmed() {
    const w = state.steps.window?.results, p = state.steps.params?.results;
    if (!w || !p) return [];
    return w.filter(r => r.r === 0 && r.metrics).map(r => {
      const full = p.find(x => x.set === r.set && x.i === r.i && x.r === 0)?.metrics?.in;
      return full && r.metrics.in < full ? { ...r, full } : null;
    }).filter(Boolean);
  }

  function speed(s) {
    if (!s) return `<span class="hint">—</span>`;
    return `<div>первый токен ${dec(s.ttft, 2)} с · ответ ${dec(s.seconds)} с</div>
      <div class="hint">генерация ${dec(s.tps, 0)} ток/с · чтение промпта ${fmt(s.read)} ток/с</div>${s.reloads
        ? `<div class="lr-flag">⚠ модель грузилась заново на ${s.reloads} ответах из ${s.total}, эмбеддинг ${fmt(s.embed_ms)} мс</div>` : ""}`;
  }

  function drawLadder() {
    const base = state.steps.before?.summary;
    const cut = trimmed();
    $("#tuTable").innerHTML = `<tr><th>Ступень · что меняем</th><th>${SETS.d22} · по нему подбирали</th>
      <th>${SETS.talk} · проверка</th><th>Стабильность</th><th>Скорость · первый круг</th><th>Память</th><th></th></tr>`
      + state.view.steps.map(s => {
        const rec = state.steps[s.id], sum = rec?.summary;
        const running = state.run?.id === s.id;
        const live = running ? `<span class="hint">${state.run.note}</span>` : null;
        const d = sum?.d22;
        const stable = !d ? "—" : d.repeated ? `вердикт держится ${d.stable}/${d.repeated}<div class="hint">дословно ${d.same}/${d.repeated}</div>`
          : `<span class="hint">один круг</span>`;
        const note = s.id === "window" && cut.length
          ? `<div class="lr-flag">⚠ промпт обрезан у ${cut.length} из ${rec.results.filter(r => r.r === 0).length}: ${
              cut.map(r => `${fmt(r.full)} → ${fmt(r.metrics.in)}`).join(", ")} ток.</div>` : "";
        return `<tr class="ask-row${s.probe ? " tu-probe" : ""}${state.open === s.id ? " on" : ""}${running ? " tu-running" : ""}" data-id="${s.id}">
          <td><b>${esc(s.title)}</b>${s.probe ? ` <span class="tu-tag">проба</span>` : ""}<div class="hint">${esc(s.change)}</div>${note}${
            rec ? `<div class="hint">прогон ${esc(rec.at)} · ${Math.floor(rec.seconds / 60)} мин ${rec.seconds % 60} с</div>` : ""}</td>
          <td>${quality(d, base?.d22, live)}</td>
          <td>${quality(sum?.talk, base?.talk, running ? " " : null)}</td>
          <td>${stable}</td>
          <td>${speed(d)}</td>
          <td class="tu-mem">${rec ? vramBar(rec.memory, s.model) + `<div class="hint">диск ${gb(rec.disk)} · загрузка ${dec(rec.warm.model)} с</div>` : "—"}</td>
          <td><button class="tu-run" data-id="${s.id}" title="Прогнать эту ступень">▶</button></td></tr>`;
      }).join("");
    $("#tuTable").querySelectorAll(".ask-row").forEach(row => row.addEventListener("click", e => {
      if (e.target.closest(".tu-run")) return;
      state.open = state.open === row.dataset.id ? null : row.dataset.id;
      state.pick = null;
      drawLadder();
      drawDetail(state.open);
    }));
    $("#tuTable").querySelectorAll(".tu-run").forEach(b => b.addEventListener("click", () => evaluate([b.dataset.id])));
    lock(state.busy);
  }

  // ── Ответы ступени: вопрос × круги, клик по отметке — ответ ──────────
  function mark(r) {
    if (!r) return `<span class="hint">·</span>`;
    if (r.error) return `<button class="tu-mark ask-mark bad" data-k="${r.set}.${r.i}.${r.r}" title="${esc(r.error)}">!</button>`;
    const [sign, word] = MARK[r.grade.verdict];
    const pick = state.pick === `${r.set}.${r.i}.${r.r}`;
    return `<button class="tu-mark ask-mark ${r.grade.verdict}${pick ? " on" : ""}" data-k="${r.set}.${r.i}.${r.r}" title="${word}">${sign}</button>`;
  }

  function drawDetail(id) {
    const box = $("#tuDetail");
    const rec = state.steps[id];
    box.hidden = !id;
    if (!id) return;
    if (!rec) {
      box.innerHTML = `<p class="hint">Ступень «${esc(stepOf(id).title)}» ещё не прогоняли.</p>`;
      return;
    }
    const rounds = Math.max(...rec.results.filter(r => r.set === "d22").map(r => r.r)) + 1;
    const find = (set, i, r) => rec.results.find(x => x.set === set && x.i === i && x.r === r);
    const picked = state.pick && find(...state.pick.split(".").map((v, n) => n ? Number(v) : v));
    box.innerHTML = `<h3>${esc(stepOf(id).title)} · ответы</h3>
      <table class="rag-table tu-answers"><tr><th>№</th><th>Вопрос</th><th>${SETS.d22}${rounds > 1 ? ` · ${rounds} круга` : ""}</th><th>${SETS.talk}</th></tr>`
      + state.view.questions.map((x, i) => `<tr><td class="num">${i + 1}</td>
        <td><div>${esc(x.q)}</div><div class="hint">«${esc(x.talk)}»</div></td>
        <td class="lr-marks">${Array.from({ length: rounds }, (_, r) => mark(find("d22", i, r))).join(" ")}</td>
        <td class="lr-marks">${mark(find("talk", i, 0))}</td></tr>`).join("") + `</table>`
      + (picked ? answerView(picked) : `<p class="hint">Клик по отметке — ответ модели.</p>`);
    box.querySelectorAll(".tu-mark").forEach(b => b.addEventListener("click", () => {
      state.pick = b.dataset.k;
      drawDetail(id);
    }));
  }

  function answerView(r) {
    const x = state.view.questions[r.i];
    if (r.error) return `<div class="tu-answer"><span class="err">${esc(r.error)}</span></div>`;
    return `<div class="tu-answer">
      <div class="hint">${r.set === "d22" ? `вопрос: ${esc(x.q)} · круг ${r.r + 1}` : `разговорными словами: «${esc(x.talk)}»`}
        · ожидание: ${esc(x.outside ? "отказ — в базе этого нет" : x.expect)}</div>
      <div class="ask-answer">${markdown(r.answer)}</div>
      <div class="ask-grade">${gradeLine(r.grade)}</div>
      <div class="ask-meta">${meta(r.metrics)} · эмбеддинг ${fmt(r.embed_ms)} мс</div></div>`;
  }

  // ── Сверка и метрики ответа ──────────────────────────────────────────
  function gradeLine(g) {
    const parts = [];
    if (g.verdict) {
      const [sign, word] = MARK[g.verdict];
      parts.push(`<span class="ask-mark ${g.verdict}">${sign} ${word}</span>`);
      if (g.of) {
        parts.push(`факты ${g.facts} из ${g.of}`);
        parts.push(g.cited_ok ? "сослался на нужный документ" : g.retrieved
          ? "нужный документ был в топе, но ссылки на него нет" : "нужного документа нет в топе");
      } else parts.push(g.refused ? "отказался — ответа в базе нет" : "назвал ответ, которого в базе нет");
    }
    const flags = [];
    if (g.bad_refs.length) flags.push(`ссылка на фрагмент, которого не было: ${g.bad_refs.map(n => `[${n}]`).join(" ")}`);
    if (g.alien.length) flags.push(`чужие буквы: ${g.alien.join(" ")}`);
    if (g.cut) flags.push("оборван по пределу токенов");
    if (g.empty) flags.push("пустой ответ или одни ссылки");
    if (!g.verdict && !flags.length) parts.push(`<span class="hint">вопрос не из набора — сверять не с чем, сбоев нет</span>`);
    return parts.join(" · ") + flags.map(f => `<div class="lr-flag">⚠ ${esc(f)}</div>`).join("");
  }

  const meta = m => `загрузка ${dec(m.load, 2)} с · первый токен ${dec(m.ttft, 2)} с · ответ ${dec(m.seconds)} с · `
    + `чтение ${fmt(m.read)} ток/с · генерация ${dec(m.tps, 0)} ток/с · ${fmt(m.in)} → ${fmt(m.out)} ток.`;

  // ── Квант ────────────────────────────────────────────────────────────
  function drawQuant() {
    const rows = [["prompt", "q4_K_M"], ["q3", "q3_K_M"], ["q5", "q5_K_M"], ["q8", "q8_0 · слои раскладывает Ollama"],
                  ["after", "q8_0 · все слои в видеокарте"]]
      .map(([id, label]) => ({ id, label, rec: state.steps[id] })).filter(x => x.rec);
    if (!rows.length) {
      $("#tuQuant").innerHTML = `<tr><td class="hint">Ступени с квантами ещё не прогоняли.</td></tr>`;
      return;
    }
    const good = rec => {
      const s = rec.summary, d = s.d22, t = s.talk;
      return { ok: d.ok + t.ok, total: d.total + t.total };
    };
    const top = Math.max(...rows.map(x => x.rec.summary.d22.tps || 0));
    const meter = (part, tone = "") => `<div class="tu-meter ${tone}"><i style="width:${(part * 100).toFixed(1)}%"></i></div>`;
    $("#tuQuant").innerHTML = `<tr><th>Квант</th><th>На диске</th><th>Память</th><th>Верных · оба набора</th>
      <th>Генерация</th><th>Ответ целиком</th></tr>` + rows.map(({ id, label, rec }) => {
        const g = good(rec), d = rec.summary.d22;
        return `<tr><td><b>${esc(label)}</b><div class="hint">ступень «${esc(stepOf(id).title)}»</div></td>
          <td class="num">${gb(rec.disk)}</td>
          <td class="tu-mem">${vramBar(rec.memory, rec.model)}</td>
          <td>${meter(g.ok / g.total, g.ok / g.total < 0.5 ? "bad" : "")}<span class="num">${g.ok} из ${g.total}</span></td>
          <td>${meter((d.tps || 0) / top)}<span class="num">${dec(d.tps, 0)} ток/с</span></td>
          <td class="num">${dec(d.seconds)} с</td></tr>`;
      }).join("");
  }

  // ── Один вопрос ──────────────────────────────────────────────────────
  function clearColumns() {
    for (const name of ["before", "after"]) {
      const c = col(name);
      c.dataset.raw = "";
      c.classList.remove("tu-wait");
      $(".ask-answer", c).innerHTML = "";
      $(".ask-grade", c).innerHTML = "";
      $(".ask-meta", c).innerHTML = "";
    }
    $("#tuSources").innerHTML = "";
  }

  function render(name, raw) {
    const c = col(name);
    c.dataset.raw = raw;
    $(".ask-answer", c).innerHTML = markdown(raw)
      .replace(/\[(\d+)\]/g, (_, n) => `<button class="ask-cite" data-n="${n}">[${n}]</button>`);
  }

  function drawSources(hits) {
    $("#tuSources").innerHTML = hits.map((h, i) => `
      <li data-n="${i + 1}">
        <div><b>[${i + 1}]</b> ${esc(h.title)} › ${esc(h.section)} <span class="score">cos ${h.score.toFixed(3)}</span></div>
        <details><summary>текст фрагмента</summary><p>${esc(h.text)}</p></details>
      </li>`).join("");
  }

  root.addEventListener("pointerover", e => {
    const cite = e.target.closest("#tuCols .ask-cite");
    $("#tuSources").querySelectorAll("li")
      .forEach(li => li.classList.toggle("hot", !!cite && li.dataset.n === cite.dataset.n));
  });

  async function ask() {
    const q = $("#tuQ").value.trim();
    if (!q || state.busy) return;
    lock(true);
    clearColumns();
    for (const name of ["before", "after"]) {
      col(name).classList.add("tu-wait");
      $(".ask-meta", col(name)).textContent = "жду поиск…";
    }
    try {
      await stream("/api/tune/ask", { q, steps: ["before", "after"] }, e => {
        if (e.t === "search") {
          drawSources(e.hits);
          for (const name of ["before", "after"]) $(".ask-meta", col(name)).textContent = `поиск готов, эмбеддинг ${fmt(e.embed_ms)} мс · жду очереди…`;
        } else if (e.t === "start") {
          col(e.id).classList.remove("tu-wait");
          $(".ask-meta", col(e.id)).textContent = "модель думает…";
        } else if (e.t === "delta") {
          render(e.id, (col(e.id).dataset.raw || "") + e.text);
        } else if (e.t === "done") {
          render(e.id, e.answer);
          $(".ask-grade", col(e.id)).innerHTML = gradeLine(e.grade);
          $(".ask-meta", col(e.id)).innerHTML = esc(meta(e.metrics)) + vramBar(e.memory, stepOf(e.id).model);
        } else if (e.t === "error") {
          $(".ask-grade", col(e.id === "search" ? "before" : e.id)).innerHTML = `<span class="err">${esc(e.message)}</span>`;
        }
      });
    } catch (e) {
      $(".ask-grade", col("before")).innerHTML = `<span class="err">Запрос оборвался: ${esc(e.message)}</span>`;
    }
    lock(false);
  }
  $("#tuGo").addEventListener("click", ask);
  $("#tuQ").addEventListener("keydown", e => e.key === "Enter" && ask());

  // ── Прогон ступеней ──────────────────────────────────────────────────
  async function evaluate(ids) {
    if (state.busy) return;
    lock(true);
    const started = Date.now();
    const total = id => (stepOf(id).probe ? 1 : state.view.repeats) * 10 + 10;
    let done = 0;
    try {
      await stream("/api/tune/eval", { steps: ids }, e => {
        if (e.t === "warm") {
          state.run = { id: e.id, n: 0, note: `прогрев: эмбеддер ${dec(e.embed)} с, модель ${dec(e.model)} с…` };
          drawLadder();
        } else if (e.t === "result") {
          state.run.n += 1;
          state.run.note = `ответ ${state.run.n} из ${total(e.id)}…`;
          const cell = $(`#tuTable tr[data-id="${e.id}"] td:nth-child(2)`);
          if (cell) cell.innerHTML = `<span class="hint">${state.run.note}</span>`;
        } else if (e.t === "step") {
          done += 1;
          state.steps[e.id] = { ...e, results: null };
          state.run = null;
          getJSON("/api/tune").then(v => {
            state.steps = v.snapshot?.steps || state.steps;
            drawAll();
          });
          $("#tuEvalNote").textContent = `готово ступеней: ${done} из ${ids.length}${e.saved ? "" : " · с ошибками, в снимок не записана"}`;
        } else if (e.t === "error") {
          state.run = null;
          $("#tuEvalNote").textContent = `${stepOf(e.id).title}: ${e.message}`;
          drawLadder();
        } else if (e.t === "end") {
          const s = Math.round((Date.now() - started) / 1000);
          $("#tuEvalNote").textContent = `Прогон: ${ids.length} ступ. за ${Math.floor(s / 60)} мин ${s % 60} с · `
            + `запросов к этой машине ${e.net.local}, ушло наружу ${e.net.out}`;
        }
      });
    } catch (e) {
      $("#tuEvalNote").textContent = "Прогон оборвался: " + e.message;
    }
    state.run = null;
    lock(false);
    drawLadder();
  }
  $("#tuEval").addEventListener("click", () => evaluate(state.view.steps.map(s => s.id)));

  // Открыли сразу по адресу #week6/29 — local.js показал день до этого файла.
  if (!root.hidden) window.tuneShow();
})();
