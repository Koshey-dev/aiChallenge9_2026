// Неделя 6, день 28: RAG целиком на этой машине. Вопрос → эмбеддинг
// (embeddinggemma в Ollama) → поиск по индексу недели 5 → ответ локальной
// модели; рядом — облачная модель на тех же фрагментах, для сравнения. Полоса
// показывает, где идёт каждый шаг, плитки и журнал — сеть локальной ветки.
// Ниже — прогон контрольного набора дня 22 по три раза: качество, скорость,
// стабильность. Без модели на машине (VPS) — снимок прогона с ПК, запросы
// выключены. Данные — /api/localrag/*, подвкладку показывает local.js.

(() => {
  const root = $('.rag-day[data-day="28"]');
  const state = { ready: false, live: false, view: null, last: null, busy: false, net: [] };
  const esc = text => escapeHtml(String(text ?? ""));
  const dec = (n, d = 1) => n == null ? "—" : Number(n).toFixed(d).replace(".", ",");
  const fmt = n => n == null ? "—" : Number(n).toLocaleString("ru");
  const LOCAL = /^(127\.0\.0\.1|localhost|::1):/;
  const MARK = { ok: ["✓", "верно"], part: ["◐", "частично"], bad: ["✗", "мимо"] };
  const SIDES = ["local", "cloud"];

  const col = name => $(`.ask-col[data-col="${name}"]`, root);
  const pages = h => !h.page_from ? "" : ` · стр. ${h.page_from}` +
    (h.page_to && h.page_to !== h.page_from ? `–${h.page_to}` : "");

  async function getJSON(url, options) {
    const r = await fetch(url, options);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
    return r.json();
  }

  window.localRagShow = () => {
    if (state.ready) return;
    state.ready = true;
    init().catch(e => ($("#lrEvalNote").textContent = "Стенд не ответил: " + e.message));
  };

  async function init() {
    const v = state.view = await getJSON("/api/localrag");
    state.live = v.live;
    state.last = v.snapshot || null;
    const host = v.status.url.replace(/^https?:\/\//, "");
    $(".lr-who", col("local")).textContent = v.model;
    $(".lr-who", col("cloud")).textContent = v.cloud.title;
    state.where = {
      embed: `${v.embedder} · ${host}`,
      search: `${fmt(v.chunks)} фрагментов по структуре · топ-${v.top}`,
      local: `${v.model} · ${host}`,
      cloud: v.cloud.ready ? `${v.cloud.title} · ${v.cloud.host}` : `ключа ${v.cloud.title} на стенде нет`,
    };
    if (!v.cloud.ready) {
      $("#lrCloud").checked = false;
      $("#lrCloud").disabled = true;
      $("#lrCloud").parentElement.lastChild.textContent = " сравнить с облаком — ключа нет";
    }
    resetFlow();
    $("#lrEvalTitle").textContent = `Сравнение: ${v.questions.length} контрольных вопросов × ${v.repeats}`;
    if (!state.live) {
      $("#lrSnap").hidden = false;
      $("#lrSnap").textContent = (state.last ? `Снимок прогона с ПК от ${state.last.at}. ` : "Снимка прогона нет. ")
        + (v.status.error ? `Ollama здесь не отвечает (${v.status.error}),` : `На этой машине модели ${v.model} нет,`)
        + " поэтому запросы выключены.";
    }
    state.net = state.last?.net || [];
    drawNet();
    setColumns();
    drawTable();
    drawSummary();
    lock(false);
  }

  function lock(busy) {
    state.busy = busy;
    for (const id of ["#lrGo", "#lrEval", "#lrQ"]) $(id).disabled = busy || !state.live;
  }

  function setColumns() {
    const cloud = $("#lrCloud").checked;
    col("cloud").hidden = !cloud;
    $("#lrCols").classList.toggle("one", !cloud);
  }
  $("#lrCloud").addEventListener("change", () => !state.busy && setColumns());

  // ── Полоса: где идёт каждый шаг ──────────────────────────────────────
  function step(name, status, text, where) {
    const li = $(`#lrFlow [data-step="${name}"]`);
    li.className = (name === "cloud" ? "cloud " : "") + status;
    if (text !== undefined) $("span", li).textContent = text;
    if (where !== undefined) $("i", li).textContent = where;
  }

  function resetFlow() {
    const cloud = $("#lrCloud").checked;
    for (const name of ["embed", "search", "local"]) step(name, "", state.where[name], "эта машина");
    step("cloud", cloud ? "" : "skip", state.where.cloud, cloud ? "облако" : "не участвует");
  }

  // ── Сеть локальной ветки ─────────────────────────────────────────────
  function drawNet() {
    const local = state.net.filter(n => LOCAL.test(n.host) && !n.blocked).length;
    const blocked = state.net.filter(n => n.blocked).length;
    const out = state.net.length - local - blocked;
    const tiles = [["", "к этой машине", local], [out ? "bad" : "ok", "ушло наружу", out],
                   [blocked ? "part" : "", "отбито охранником", blocked]];
    $("#lrTiles").innerHTML = tiles.map(([tone, label, value]) =>
      `<div class="ct-tile ${tone}"><span>${label}</span><b>${value}</b></div>`).join("");
    $("#lrLog").innerHTML = state.net.slice().reverse().map(n => `
      <li class="${n.blocked ? "no" : ""}"><b>${esc(n.at)}</b> ${esc(n.method)} ${esc(n.host)}${esc(n.path)} · ${
        n.blocked ? "отбит охранником" : `${n.status ?? "—"}${n.ms != null ? ` · ${n.ms} мс` : ""}`}</li>`).join("")
      || `<li>запросов пока не было</li>`;
  }

  // ── Колонки ответа ───────────────────────────────────────────────────
  function clearColumns() {
    for (const name of SIDES) {
      const c = col(name);
      c.dataset.raw = "";
      $(".ask-answer", c).innerHTML = "";
      $(".ask-grade", c).innerHTML = "";
      $(".ask-meta", c).textContent = "";
    }
    $("#lrSources").innerHTML = "";
  }

  function render(name, raw) {
    const c = col(name);
    c.dataset.raw = raw;
    $(".ask-answer", c).innerHTML = markdown(raw)
      .replace(/\[(\d+)\]/g, (_, n) => `<button class="ask-cite" data-n="${n}">[${n}]</button>`);
  }

  // Сверка: вердикт дня 22 у контрольного вопроса и сбои, видные у любого ответа.
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
    if (g.cut) flags.push("оборван по лимиту токенов");
    if (g.empty) flags.push("пустой ответ или одни ссылки");
    if (!g.verdict && !flags.length) parts.push(`<span class="hint">вопрос не из набора — сверять не с чем, сбоев нет</span>`);
    return parts.join(" · ") + flags.map(f => `<div class="lr-flag">⚠ ${esc(f)}</div>`).join("");
  }

  function meta(m) {
    return `первый токен ${dec(m.ttft, 2)} с · ответ ${dec(m.seconds)} с · ${dec(m.tps)} ток/с · `
      + `${fmt(m.in)} → ${fmt(m.out)} ток. · ${m.cost ? "$" + m.cost.toFixed(5) : "0 ₽"}`;
  }

  function drawAnswer(name, a) {
    if (a.error) {
      $(".ask-grade", col(name)).innerHTML = `<span class="err">${esc(a.error)}</span>`;
      return;
    }
    render(name, a.answer);
    $(".ask-meta", col(name)).textContent = meta(a.metrics);
    $(".ask-grade", col(name)).innerHTML = gradeLine(a.grade);
  }

  function drawSources(hits, cited = []) {
    $("#lrSources").innerHTML = hits.map((h, i) => `
      <li data-n="${i + 1}" class="${cited.includes(i + 1) ? "cited" : ""}">
        <div><b>[${i + 1}]</b> ${esc(h.title)} › ${esc(h.section)}${pages(h)}
          <span class="score">cos ${h.score.toFixed(3)}</span></div>
        ${h.text ? `<details><summary>текст фрагмента</summary><p>${esc(h.text)}</p></details>` : ""}
      </li>`).join("");
  }

  function markCited() {
    const cited = new Set();
    for (const name of SIDES) col(name).querySelectorAll(".ask-cite").forEach(b => cited.add(Number(b.dataset.n)));
    $("#lrSources").querySelectorAll("li").forEach(li => li.classList.toggle("cited", cited.has(Number(li.dataset.n))));
  }

  root.addEventListener("pointerover", e => {
    const cite = e.target.closest(".ask-cite");
    $("#lrSources").querySelectorAll("li")
      .forEach(li => li.classList.toggle("hot", !!cite && li.dataset.n === cite.dataset.n));
  });
  root.addEventListener("click", e => {
    const cite = e.target.closest(".ask-cite");
    const item = cite && $(`#lrSources li[data-n="${cite.dataset.n}"] details`);
    if (item) item.open = true;
  });

  // ── Вопрос ───────────────────────────────────────────────────────────
  async function ask() {
    const q = $("#lrQ").value.trim();
    if (!q || state.busy) return;
    lock(true);
    $("#lrRuns").hidden = true;
    setColumns();
    clearColumns();
    resetFlow();
    const cloud = $("#lrCloud").checked;
    step("embed", "run", "считаю вектор вопроса…");
    state.net = [];
    drawNet();
    try {
      await stream("/api/localrag/ask", { q, cloud }, e => {
        if (e.t === "search") {
          step("embed", "done", `${e.embed_ms ?? "—"} мс · ${state.where.embed}`);
          step("search", "done", `${e.hits.length} фрагментов за ${Math.max(0, e.ms - (e.embed_ms || 0))} мс · `
            + `лучший cos ${e.hits[0].score.toFixed(3)}`);
          drawSources(e.hits);
          step("local", "run", "жду ответ…");
          if (cloud) step("cloud", "run", "жду ответ…");
        } else if (e.t === "net") {
          state.net = e.net;
          drawNet();
        } else if (e.t === "delta") {
          render(e.target, (col(e.target).dataset.raw || "") + e.text);
        } else if (e.t === "error") {
          step(e.target === "search" ? "embed" : e.target, "fail", e.message);
        } else if (e.t === "done") {
          drawAnswer(e.target, e);
          markCited();
          const m = e.metrics;
          step(e.target, e.error ? "fail" : "done", e.error || `${dec(m.seconds)} с · первый токен ${dec(m.ttft, 2)} с`);
        }
      });
    } catch (e) {
      step("local", "fail", "запрос оборвался: " + e.message);
    }
    lock(false);
  }
  $("#lrGo").addEventListener("click", ask);
  $("#lrQ").addEventListener("keydown", e => e.key === "Enter" && ask());

  // ── Прогон ───────────────────────────────────────────────────────────
  const runsOf = i => (state.last?.results || []).filter(r => r.i === i).sort((a, b) => a.r - b.r);

  function cell(i, side, pending) {
    const runs = runsOf(i);
    const repeats = state.last?.repeats || state.view.repeats;
    if (!runs.length) return pending ? `<span class="hint">жду…</span>` : `<span class="hint">—</span>`;
    const marks = runs.map(r => {
      const a = r[side];
      if (!a || a.error || r.error) return `<span class="ask-mark bad" title="ошибка">!</span>`;
      return `<span class="ask-mark ${a.grade.verdict}" title="${MARK[a.grade.verdict][1]}">${MARK[a.grade.verdict][0]}</span>`;
    }).join(" ") + " <span class='hint'>·</span>".repeat(Math.max(0, repeats - runs.length));
    const good = runs.map(r => r[side]).filter(a => a && !a.error);
    const seconds = good.map(a => a.metrics.seconds).sort((a, b) => a - b);
    const flags = good.filter(a => a.grade.bad_refs.length || a.grade.alien.length || a.grade.cut || a.grade.empty).length;
    const same = good.length === repeats && new Set(good.map(a => a.grade.verdict)).size === 1;
    const note = [seconds.length ? `${dec(seconds[Math.floor(seconds.length / 2)])} с` : "",
                  good.length === repeats ? (same ? "вердикт держится" : "разнобой") : "",
                  flags ? `сбоев: ${flags}` : ""].filter(Boolean).join(" · ");
    return `<div class="lr-marks">${marks}</div><div class="hint${same || good.length < repeats ? "" : " lr-shaky"}">${note}</div>`;
  }

  function drawTable(pending = false) {
    const qs = state.view.questions;
    $("#lrTable").innerHTML = `<tr><th>№</th><th>Вопрос · ожидание</th><th>Локально · ${esc(state.view.model)}</th>
      <th>Облако · ${esc(state.view.cloud.title)}</th></tr>` + qs.map((q, i) => `
      <tr class="ask-row" data-i="${i}">
        <td class="num">${i + 1}</td>
        <td><div>${esc(q.q)}</div><div class="file">${q.outside ? "вне базы — верный ответ: отказ" : "ожидание: " + esc(q.expect)}</div></td>
        <td>${cell(i, "local", pending)}</td><td>${cell(i, "cloud", pending)}</td></tr>`).join("");
    $("#lrTable").querySelectorAll(".ask-row").forEach(row =>
      row.addEventListener("click", () => showRow(Number(row.dataset.i), 0)));
  }

  // Строка прогона — наверх, в колонки, с переключателем повторов.
  function showRow(i, r) {
    const q = state.view.questions[i];
    const runs = runsOf(i);
    $("#lrQ").value = q.q;
    if (!runs.length || state.busy) return $("#lrAskCard").scrollIntoView({ behavior: "smooth", block: "start" });
    const run = runs.find(x => x.r === r) || runs[0];
    $("#lrRuns").hidden = false;
    $("#lrRuns").innerHTML = runs.map(x =>
      `<button data-r="${x.r}" class="${x.r === run.r ? "on" : ""}">повтор ${x.r + 1}</button>`).join("");
    $("#lrRuns").querySelectorAll("button").forEach(b => b.addEventListener("click", () => showRow(i, Number(b.dataset.r))));
    $("#lrCloud").checked = !!run.cloud;
    setColumns();
    clearColumns();
    resetFlow();
    step("embed", "done", `${run.embed_ms ?? "—"} мс · из прогона ${state.last.at || "только что"}`);
    step("search", "done", `${run.hits.length} фрагментов за ${Math.max(0, run.search_ms - (run.embed_ms || 0))} мс · `
      + `лучший cos ${run.hits[0]?.score.toFixed(3) ?? "—"}`);
    for (const name of SIDES) {
      if (!run[name]) continue;
      drawAnswer(name, run[name]);
      const m = run[name].metrics;
      step(name, run[name].error ? "fail" : "done", run[name].error || `${dec(m.seconds)} с · первый токен ${dec(m.ttft, 2)} с`);
    }
    drawSources(run.hits);
    markCited();
    $("#lrAskCard").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function drawSummary() {
    const last = state.last, s = last?.summary;
    if (!s?.local || !s?.cloud) {
      $("#lrHead").innerHTML = $("#lrSummary").innerHTML = "";
      $("#lrEvalNote").textContent = state.live ? "Прогона ещё не было." : "Прогона в снимке нет.";
      return;
    }
    const { local: L, cloud: C, search: S } = s;
    const n = s.questions;
    $("#lrEvalNote").textContent = `Прогон ${last.at || "только что"} · ${last.model} против ${state.view.cloud.title}`
      + ` · ${n} вопросов × ${s.repeats} · ${Math.floor(last.seconds / 60)} мин ${last.seconds % 60} с`
      + (last.warm ? ` · прогрев: эмбеддер ${dec(last.warm.embed)} с, модель ${dec(last.warm.model)} с` : "");
    const pair = (label, a, b, note) => `<div class="ct-tile lr-pair"><span>${label}</span>
      <div><b>${a}</b><em>локально</em></div><div><b>${b}</b><em>облако</em></div>${note ? `<i>${note}</i>` : ""}</div>`;
    $("#lrHead").innerHTML =
      pair("Качество · верных ответов", `${L.ok}/${L.total}`, `${C.ok}/${C.total}`,
           `фактов ${L.facts}/${L.of} и ${C.facts}/${C.of}`) +
      pair("Скорость · ответ на новый промпт", `${dec(L.seconds)} с`, `${dec(C.seconds)} с`,
           `первый токен ${dec(L.ttft, 2)} и ${dec(C.ttft, 2)} с; на повторе ${dec(L.ttft_again, 2)} и ${dec(C.ttft_again, 2)} с`) +
      pair("Стабильность · вердикт держится", `${L.stable}/${n}`, `${C.stable}/${n}`,
           `дословно тот же ответ: ${L.same} и ${C.same} из ${n}`) +
      `<div class="ct-tile ok"><span>Поиск · на этой машине</span><b>${fmt(S.ms)} мс</b>
        <i>эмбеддинг ${fmt(S.embed_ms)} мс · те же фрагменты в повторах: ${S.same}/${n}</i></div>`;
    const failures = x => x.errors + x.empty + x.cut;
    const groups = [
      ["Качество", [
        ["Верных ответов", `${L.ok} из ${L.total}`, `${C.ok} из ${C.total}`, L.ok, C.ok, 1],
        ["Частично / мимо", `${L.part} / ${L.bad}`, `${C.part} / ${C.bad}`, L.bad, C.bad, -1],
        ["Фактов из ожиданий", `${L.facts} из ${L.of}`, `${C.facts} из ${C.of}`, L.facts, C.facts, 1],
        ["Вопрос вне базы: отказ", `${L.outside} из ${L.outside_of}`, `${C.outside} из ${C.outside_of}`, L.outside, C.outside, 1],
        ["Ссылка на нужный документ", `${L.cited_ok} из ${L.inside}`, `${C.cited_ok} из ${C.inside}`, L.cited_ok, C.cited_ok, 1],
        ["Ссылка на фрагмент, которого не было", L.bad_refs, C.bad_refs, L.bad_refs, C.bad_refs, -1],
        ["Ответов с чужими буквами", L.alien, C.alien, L.alien, C.alien, -1],
      ]],
      ["Скорость", [
        ["Первый токен · первый круг, промпт новый", `${dec(L.ttft, 2)} с`, `${dec(C.ttft, 2)} с`, L.ttft, C.ttft, -1],
        ["Первый токен · повторы, промпт в кеше", `${dec(L.ttft_again, 2)} с`, `${dec(C.ttft_again, 2)} с`,
         L.ttft_again, C.ttft_again, -1],
        ["Ответ целиком · первый круг", `${dec(L.seconds)} с`, `${dec(C.seconds)} с`, L.seconds, C.seconds, -1],
        ["Ответ целиком · повторы", `${dec(L.seconds_again)} с`, `${dec(C.seconds_again)} с`,
         L.seconds_again, C.seconds_again, -1],
        ["Самый долгий ответ", `${dec(L.worst)} с`, `${dec(C.worst)} с`, L.worst, C.worst, -1],
        ["Скорость выдачи текста, медиана", `${dec(L.tps)} ток/с`, `${dec(C.tps)} ток/с`, L.tps, C.tps, 1],
        ["Токенов на входе / выходе, в среднем", `${fmt(L.tokens_in)} / ${fmt(L.tokens_out)}`,
         `${fmt(C.tokens_in)} / ${fmt(C.tokens_out)}`, 0, 0, 0],
      ]],
      ["Стабильность", [
        ["Вердикт одинаков во всех повторах", `${L.stable} из ${n}`, `${C.stable} из ${n}`, L.stable, C.stable, 1],
        ["Дословно тот же ответ", `${L.same} из ${n}`, `${C.same} из ${n}`, L.same, C.same, 1],
        ["Разброс времени между повторами", `±${dec(L.spread == null ? null : L.spread / 2, 2)} с`, `±${dec(C.spread == null ? null : C.spread / 2, 2)} с`, L.spread, C.spread, -1],
        ["Сбои: ошибка, пустой или одни ссылки, обрыв", failures(L), failures(C), failures(L), failures(C), -1],
      ]],
      ["Цена", [
        ["Весь прогон", "0 ₽ — своя видеокарта", `$${C.cost.toFixed(4)}`, 0, C.cost, -1],
      ]],
    ];
    const bold = (v, mine, other, better) => better && mine !== other && (mine - other) * better > 0 ? `<b>${v}</b>` : v;
    $("#lrSummary").innerHTML = `<tr><th></th><th class="num">Локально · ${esc(last.model)}</th>
      <th class="num">Облако · ${esc(state.view.cloud.title)}</th></tr>` + groups.map(([title, rows]) =>
      `<tr class="lr-group"><td colspan="3">${title}</td></tr>` + rows.map(([label, a, b, x, y, better]) =>
        `<tr><td>${label}</td><td class="num">${bold(a, x, y, better)}</td><td class="num">${bold(b, y, x, better)}</td></tr>`).join("")).join("");
  }

  $("#lrEval").addEventListener("click", async () => {
    if (state.busy) return;
    lock(true);
    const v = state.view;
    const total = v.questions.length * v.repeats;
    state.last = { results: [], model: v.model, repeats: v.repeats, at: "" };
    state.net = [];
    drawNet();
    drawTable(true);
    drawSummary();
    $("#lrEvalNote").textContent = "Поднимаю эмбеддер и модель в память…";
    try {
      await stream("/api/localrag/eval", {}, e => {
        if (e.t === "warm") {
          state.last.warm = e;
          state.net = e.net;
          drawNet();
          $("#lrEvalNote").textContent = `Прогрев: эмбеддер ${dec(e.embed)} с, модель ${dec(e.model)} с. Задаю вопросы…`;
        } else if (e.t === "result") {
          state.last.results.push(e);
          state.net = e.net;
          drawNet();
          drawTable(true);
          $("#lrEvalNote").textContent = `Готово ${state.last.results.length} из ${total}…`;
        } else if (e.t === "error") {
          $("#lrEvalNote").textContent = e.message;
        } else if (e.t === "done") {
          state.last.summary = e.summary;
          state.last.seconds = e.seconds;
          drawTable();
          drawSummary();
          if (!e.saved) $("#lrEvalNote").textContent += " · прогон неполный, в снимок не записан";
        }
      });
    } catch (e) {
      $("#lrEvalNote").textContent = "Прогон оборвался: " + e.message;
    }
    lock(false);
  });

  // Открыли сразу по адресу #week6/28 — local.js показал день до этого файла.
  if (!root.hidden) window.localRagShow();
})();
