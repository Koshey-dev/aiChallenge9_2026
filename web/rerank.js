// Неделя 5, день 23: реранкинг и фильтр. Слева — базовый RAG дня 22 (топ-5
// без фильтра), справа — режим с выбранным вторым этапом: фильтр по косинусу,
// rewrite + фильтр или rewrite + LLM-реранкер. Воронка показывает топ-K₁
// кандидатов, порог и кто прошёл в контекст; ползунки пересчитывают её без
// нового запроса. Ниже — точки порога по контрольным вопросам и прогон 20
// вопросов в четырёх режимах (/api/rag/rerank/*). Подвкладку показывает
// rag.js (window.rerankShow). Помощники страницы — из index.html.

(() => {
  const root = $('.rag-day[data-day="23"]');
  const state = { ready: false, mode: "rerank", config: null, last: null, busy: false,
                  live: null, shown: null, sweep: null };
  const esc = text => escapeHtml(String(text ?? ""));
  const fmt = n => Number(n).toLocaleString("ru");
  const dec = (n, d = 2) => Number(n).toFixed(d).replace(".", ",");
  const MARK = { ok: ["✓", "верно"], part: ["◐", "частично"], bad: ["✗", "мимо"] };
  const STAGE = { filter: "cos", rewrite: "cos", rerank: "llm" };
  const REWRITE = { filter: false, rewrite: true, rerank: true };
  const COS_MAX = 0.8;  // шкала полос косинуса: 0…0,8

  const col = name => $(`.ask-col[data-col="${name}"]`, root);
  const knobs = () => ({ k1: Number($("#rrK1").value), k2: Number($("#rrK2").value),
                         threshold: Number($("#rrTau").value), min_score: Number($("#rrMin").value) });

  window.rerankShow = () => {
    if (state.ready) return drawStrip();
    state.ready = true;
    init().catch(e => ($("#rrEvalNote").textContent = "Набор не прочитан: " + e.message));
  };

  async function init() {
    state.config = await (await fetch("/api/rag/rerank/setup")).json();
    const { models, strategies, defaults: d, default: model } = state.config;
    $("#rrModel").innerHTML = models.map(m =>
      `<option value="${esc(m.id)}"${m.id === model ? " selected" : ""}>${esc(m.title)}</option>`).join("");
    $("#rrStrategy").innerHTML = Object.entries(strategies).map(([id, title]) =>
      `<option value="${id}"${id === "struct" ? " selected" : ""}>чанки: ${esc(title.toLowerCase())}</option>`).join("");
    $("#rrK1").innerHTML = [5, 10, 15, 20].map(n => `<option${n === d.k1 ? " selected" : ""}>${n}</option>`).join("");
    $("#rrK2").innerHTML = [1, 2, 3, 4, 5].map(n => `<option${n === d.k2 ? " selected" : ""}>${n}</option>`).join("");
    $("#rrTau").value = d.threshold;
    $("#rrMin").value = d.min_score;
    showKnobs();
    state.last = state.config.last;
    setMode("rerank");
    drawTable();
    drawSums();
    loadSweep();
  }

  // ── Ручки ─────────────────────────────────────────────────────────────
  function showKnobs() {
    $("#rrTauOut").textContent = dec($("#rrTau").value);
    $("#rrMinOut").textContent = $("#rrMin").value;
    root.querySelectorAll(".rr-range").forEach(l => l.classList.toggle("off", l.dataset.stage !== STAGE[state.mode]));
  }

  // Порог и K₂ пересчитывают воронку на месте: косинусы и оценки уже есть.
  for (const id of ["#rrTau", "#rrMin", "#rrK2"]) {
    $(id).addEventListener("input", () => {
      showKnobs();
      if (state.live) drawFunnel(state.live.list, state.live.stage, state.live.at);
      drawStrip();
    });
  }
  $("#rrStrategy").addEventListener("change", loadSweep);

  function setMode(mode) {
    state.mode = mode;
    root.querySelectorAll("#rrModes button").forEach(b => b.classList.toggle("on", b.dataset.mode === mode));
    $("#rrBestTitle").textContent = state.config.modes[mode];
    showKnobs();
    if (state.shown) showRecord(state.shown);
    else resetBest();
  }
  root.querySelectorAll("#rrModes button").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));

  // ── Второй этап на стороне браузера: то же, что rag.second_stage ──────
  function cut(list, stage, k) {
    const items = [...list].sort((a, b) => a.place - b.place);
    if (stage === "llm") items.sort((a, b) => b.rerank - a.rerank || b.score - a.score);
    let taken = 0;
    return items.map((h, i) => {
      const passed = stage === "llm" ? h.rerank >= k.min_score : h.score >= k.threshold;
      const kept = passed && taken < k.k2;
      taken += kept;
      return { ...h, final: i + 1, kept, why: kept ? "" : passed ? "K₂" : "порог" };
    });
  }

  // ── Полоса шагов ──────────────────────────────────────────────────────
  function step(name, status, text) {
    const li = $(`#rrFlow [data-step="${name}"]`);
    li.className = status;
    if (text !== undefined) $("span", li).textContent = text;
  }

  function clearCol(name) {
    const c = col(name);
    c.dataset.raw = "";
    $(".ask-answer", c).innerHTML = "";
    $(".ask-grade", c).innerHTML = "";
    $(".ask-meta", c).textContent = "";
    if (name === "base") $(".ask-sources", c).innerHTML = "";
    else $(".ask-prompt pre", c).textContent = "";
  }

  function resetBest() {
    state.live = null;
    clearCol("best");
    $("#rrFunnel").hidden = true;
    for (const s of ["rewrite", "search", "rerank", "filter", "llm"]) step(s, "", "—");
  }

  function render(name, raw) {
    const c = col(name);
    c.dataset.raw = raw;
    $(".ask-answer", c).innerHTML = markdown(raw)
      .replace(/\[(\d+)\]/g, (_, n) => `<button class="ask-cite" data-col="${name}" data-n="${n}">[${n}]</button>`);
  }

  function gradeLine(g) {
    if (!g) return "";
    const [sign, word] = MARK[g.verdict];
    const parts = [g.of ? `факты ${g.facts} из ${g.of}`
      : g.refused ? "отказался ответить — ответа в базе нет" : "назвал ответ, которого в базе нет"];
    if (g.of) parts.push(g.cited_ok ? "сослался на нужный документ" : g.retrieved
      ? "нужный документ в контексте, но ссылки на него нет" : "нужного документа нет в контексте");
    return `<span class="ask-mark ${g.verdict}">${sign} ${word}</span> ${parts.join(" · ")}`;
  }

  function meta(a) {
    if (a.empty) return "контекст пуст — модель не вызывалась, отказ без запроса";
    const m = a.metrics, x = a.extra;
    let text = `ответ: ${fmt(m.in)} ток. на входе · ${fmt(m.out)} на выходе · ${dec(m.seconds, 1)} с · $${m.cost.toFixed(5)}`;
    if (x && x.in) text += ` · rewrite и реранкер: ${fmt(x.in + x.out)} ток. · ${dec(x.seconds, 1)} с · $${x.cost.toFixed(5)}`;
    return text;
  }

  function answer(name, a) {
    render(name, a.answer);
    $(".ask-meta", col(name)).textContent = meta(a);
    $(".ask-grade", col(name)).innerHTML = a.error ? `<span class="err">${esc(a.error)}</span>` : gradeLine(a.grade);
  }

  const where = h => `${esc(h.title)} › ${esc(h.section)}`;

  function drawBase(list) {
    $(".ask-sources", col("base")).innerHTML = list.map((h, i) => `
      <li data-n="${i + 1}"><div><b>[${i + 1}]</b> ${where(h)}${h.relevant ? ` <span class="rr-yes">✓</span>` : ""}
        <span class="score">cos ${h.score.toFixed(3)}</span></div></li>`).join("");
  }

  // ── Воронка: топ-K₁ → порог → топ-K₂ ──────────────────────────────────
  // `at` — ручки, при которых получен ответ: разошлись с текущими — пишем.
  function drawFunnel(list, stage, at) {
    const k = knobs();
    const rows = cut(list, stage, k);
    const kept = rows.filter(r => r.kept).length;
    const rule = stage === "llm" ? `оценка реранкера ≥ ${k.min_score}` : `cos ≥ ${dec(k.threshold)}`;
    $("#rrFunnel").hidden = false;
    $("#rrFunnelTitle").textContent = `Кандидаты: ${list.length} → ${kept} в контексте · ${rule} · K₂ ≤ ${k.k2}`;
    const moved = at && (at.k2 !== k.k2 || (stage === "llm" ? at.min_score !== k.min_score : at.threshold !== k.threshold));
    $("#rrFunnelNote").textContent = moved ? `ответ ниже получен при ${stage === "llm" ? "оценке ≥ " + at.min_score
      : "cos ≥ " + dec(at.threshold)}, K₂ ≤ ${at.k2} — «Спросить», чтобы ответить по новому контексту` : "";
    let n = 0;
    $("#rrRows").innerHTML = rows.map(h => {
      const move = stage === "llm" && h.place !== h.final
        ? (h.place > h.final ? `<span class="up">↑${h.place - h.final}</span>` : `<span class="down">↓${h.final - h.place}</span>`) : "";
      const status = h.kept ? `в контексте [${++n}]` : h.why === "порог" ? "ниже порога" : "сверх K₂";
      const llm = h.rerank === undefined || h.rerank === null ? "" : `<span class="rr-bar small" title="оценка реранкера">
        <i style="width:${h.rerank * 10}%"></i>${stage === "llm" ? `<s style="left:${k.min_score * 10}%"></s>` : ""}</span>
        <span class="rr-num">${fmt(h.rerank)}/10</span>`;
      return `<details class="rr-row ${h.kept ? "kept" : "cut"}" data-n="${h.kept ? n : ""}">
        <summary>
          <span class="rr-pos">${h.final}</span><span class="rr-move">${move}</span>
          <span class="rr-doc" title="${where(h)}">${where(h)}</span>
          <span class="rr-cos"><span class="rr-bar"><i style="width:${Math.min(100, h.score / COS_MAX * 100)}%"></i>${
            stage === "cos" ? `<s style="left:${k.threshold / COS_MAX * 100}%"></s>` : ""}</span>
            <span class="rr-num">${h.score.toFixed(3)}</span></span>
          <span class="rr-llm">${llm}</span>
          <span class="rr-yes">${h.relevant ? "✓" : ""}</span>
          <span class="rr-state">${status}</span>
        </summary>
        ${h.text ? `<p>${esc(h.text)}</p>` : ""}
      </details>`;
    }).join("");
  }

  // Наведение на [n] подсвечивает фрагмент: слева — список базового, справа — строку воронки.
  root.addEventListener("pointerover", e => {
    const cite = e.target.closest(".ask-cite");
    root.querySelectorAll(".ask-sources li, .rr-row").forEach(el => el.classList.remove("hot"));
    if (!cite) return;
    const target = cite.dataset.col === "base" ? $(`.ask-sources li[data-n="${cite.dataset.n}"]`, col("base"))
      : $(`.rr-row[data-n="${cite.dataset.n}"]`, root);
    target?.classList.add("hot");
  });
  root.addEventListener("click", e => {
    const cite = e.target.closest(".ask-cite");
    if (cite && cite.dataset.col === "best") {
      const row = $(`.rr-row[data-n="${cite.dataset.n}"]`, root);
      if (row && $("p", row)) row.open = true;
    }
  });

  // ── Вопрос ────────────────────────────────────────────────────────────
  async function ask() {
    const q = $("#rrQ").value.trim();
    if (!q || state.busy) return;
    state.busy = true;
    state.shown = null;
    $("#rrGo").disabled = true;
    const mode = state.mode, at = knobs();
    clearCol("base");
    resetBest();
    step("q", "done", `«${q.length > 70 ? q.slice(0, 68) + "…" : q}»`);
    step("rewrite", REWRITE[mode] ? "run" : "skip", REWRITE[mode] ? "переписываю вопрос…" : "без rewrite — ищем исходный вопрос");
    step("search", REWRITE[mode] ? "" : "run", REWRITE[mode] ? "—" : "ищу в индексе…");
    step("rerank", mode === "rerank" ? "" : "skip", mode === "rerank" ? "—" : "порог по косинусу, без реранкера");
    let rawBest = null;
    const spent = {};
    try {
      await stream("/api/rag/rerank/ask", { q, mode, model: $("#rrModel").value,
                                            strategy: $("#rrStrategy").value, ...at }, e => {
        if (e.t === "search") {
          const best = e.hits[0]?.score ?? 0;
          if (e.target === "raw") rawBest = best;
          if (e.target === "rewritten" || !REWRITE[mode]) {
            step("search", "done", `${Math.min(at.k1, e.hits.length)} кандидатов за ${fmt(e.ms)} мс · лучший cos ${best.toFixed(3)}`);
            step(mode === "rerank" ? "rerank" : "filter", "run", mode === "rerank" ? "модель оценивает кандидатов…" : "отсекаю…");
          }
          if (e.target === "rewritten") {
            $("#rrFlow [data-step='rewrite'] span").textContent += ` · лучший cos ${rawBest.toFixed(3)} → ${best.toFixed(3)}`;
          }
        } else if (e.t === "rewrite") {
          step("rewrite", e.error ? "fail" : "done", e.error || `«${e.query}»`);
          step("search", "run", "ищу переписанный запрос…");
        } else if (e.t === "rerank") {
          step("rerank", e.scores ? "done" : "fail", e.scores
            ? `оценки: ${e.scores.map(s => fmt(s)).join(", ")} · ${fmt(e.metrics.in)} ток. · ${dec(e.metrics.seconds, 1)} с`
            : e.error);
          step("filter", "run", "отсекаю…");
        } else if (e.t === "filter") {
          if (e.target === "base") return drawBase(e.candidates);
          state.live = { list: e.candidates, stage: e.stage, at };
          drawFunnel(e.candidates, e.stage, at);
          const kept = e.candidates.filter(h => h.kept).length;
          step("filter", "done", kept ? `${e.candidates.length} → ${kept} в контексте`
            : `${e.candidates.length} → 0: контекст пуст, ответ — отказ без модели`);
          step("llm", "run", "жду ответ…");
        } else if (e.t === "prompt") {
          if (e.target !== "base") $(".ask-prompt pre", col("best")).textContent = `system:\n${e.system}\n\nuser:\n${e.user}`;
        } else if (e.t === "delta") {
          const name = e.target === "base" ? "base" : "best";
          render(name, (col(name).dataset.raw || "") + e.text);
        } else if (e.t === "error") {
          step(e.target === "search" ? "search" : "llm", "fail", e.message);
        } else if (e.t === "done") {
          answer(e.target === "base" ? "base" : "best", e);
          spent[e.target === "base" ? "base" : "best"] = e;
          if (spent.base && spent.best) {
            const tokens = a => a.empty ? "модель не вызывалась" : `${fmt(a.metrics.in)} ток. на входе`;
            step("llm", "done", `базовый — ${tokens(spent.base)} · справа — ${tokens(spent.best)}`);
          }
        }
      });
    } catch (e) {
      step("llm", "fail", "запрос оборвался: " + e.message);
    }
    state.busy = false;
    $("#rrGo").disabled = false;
  }
  $("#rrGo").addEventListener("click", ask);
  $("#rrQ").addEventListener("keydown", e => e.key === "Enter" && ask());

  // ── Строка прогона — наверх, без нового запроса ───────────────────────
  function showRecord(r) {
    state.shown = r;
    const mode = state.mode, q = state.config.questions[r.i];
    const text = r.set === "talk" ? q.talk : q.q;
    $("#rrQ").value = text;
    const at = state.last?.settings || knobs();
    const list = r.lists[mode], base = r.lists.base, stage = list.some(h => h.rerank !== undefined && h.rerank !== null)
      && mode === "rerank" ? "llm" : "cos";
    step("q", "done", `«${text}» · из прогона ${state.last?.created || "только что"}`);
    step("rewrite", REWRITE[mode] ? "done" : "skip", REWRITE[mode] ? `«${r.rewritten}»` : "без rewrite — ищем исходный вопрос");
    step("search", "done", `${list.length} кандидатов · лучший cos ${Math.max(...list.map(h => h.score)).toFixed(3)}`);
    step("rerank", mode === "rerank" ? "done" : "skip", mode === "rerank"
      ? `оценки: ${[...list].sort((a, b) => a.place - b.place).map(h => fmt(h.rerank ?? 0)).join(", ")}` : "порог по косинусу, без реранкера");
    const kept = list.filter(h => h.kept).length;
    step("filter", "done", kept ? `${list.length} → ${kept} в контексте` : `${list.length} → 0: контекст пуст`);
    const a = r.modes.base, b = r.modes[mode];
    const tokens = x => x.empty ? "модель не вызывалась" : `${fmt(x.metrics.in)} ток. на входе`;
    step("llm", "done", `базовый — ${tokens(a)} · справа — ${tokens(b)}`);
    state.live = { list, stage, at };
    drawFunnel(list, stage, at);
    drawBase(base.filter(h => h.kept));
    answer("base", a);
    answer("best", b);
    $(".ask-prompt pre", col("best")).textContent = "из прогона — промпт не сохраняется, «Спросить» покажет его";
    $("#rrCard").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ── Прогон: 20 вопросов × 4 режима ────────────────────────────────────
  function cell(a) {
    if (!a) return `<span class="hint">—</span>`;
    if (a.error) return `<span class="err">ошибка</span>`;
    const [sign, word] = MARK[a.grade.verdict];
    const c = a.context;
    const ctx = c.kept ? `контекст ${c.kept}${c.found === null ? "" : ` · ✓${c.relevant}`}` : "контекст пуст";
    return `<span class="ask-mark ${a.grade.verdict}">${sign} ${word}</span><div class="hint">${ctx}</div>`;
  }

  function drawTable(pending = new Set()) {
    const { questions, modes } = state.config;
    const results = new Map((state.last?.results || []).filter(Boolean).map(r => [`${r.set}:${r.i}`, r]));
    const head = `<tr><th>№</th><th>Вопрос · ожидание</th>${Object.values(modes).map(t => `<th>${esc(t)}</th>`).join("")}</tr>`;
    const group = (name, title) => `<tr class="rr-group"><td colspan="${2 + Object.keys(modes).length}">${title}</td></tr>` +
      questions.map((q, i) => {
        const key = `${name}:${i}`, r = results.get(key);
        const wait = pending.has(key) ? `<span class="hint">жду…</span>` : null;
        return `<tr class="ask-row" data-key="${key}">
          <td class="num">${i + 1}</td>
          <td><div>${esc(name === "talk" ? q.talk : q.q)}</div>
            ${r?.rewritten ? `<div class="file">rewrite: ${esc(r.rewritten)}</div>` : ""}
            <div class="file">ожидание: ${esc(q.expect)}</div></td>
          ${Object.keys(modes).map(m => `<td>${wait ?? cell(r?.modes[m])}</td>`).join("")}</tr>`;
      }).join("");
    $("#rrTable").innerHTML = head + group("exact", "Точные вопросы") + group("talk", "Разговорные вопросы");
    $("#rrTable").querySelectorAll(".ask-row").forEach(row => row.addEventListener("click", () => {
      const r = results.get(row.dataset.key);
      if (r && Object.keys(r.modes).length === Object.keys(modes).length) showRecord(r);
    }));
  }

  // Лучшее в строке — жирным (все равные). `better`: 1 — больше лучше, -1 — меньше, 0 — не сравниваем.
  const ROWS = [
    ["Верных ответов", s => `${s.ok} из ${s.total}`, s => s.ok, 1],
    ["Частично / мимо", s => `${s.part} / ${s.bad}`, null, 0],
    ["Вопрос вне базы", s => s.outside_refused === null ? "—" : s.outside_refused ? "✓ отказ" : "✗ ответил", null, 0],
    ["Нужный чанк в контексте", s => `${s.found} из ${s.inside}`, s => s.found, 1],
    ["Чанков в контексте, в среднем", s => dec(s.kept, 1), null, 0],
    ["Точность контекста", s => `${s.precision}%`, s => s.precision, 1],
    ["Токенов на входе ответа", s => fmt(s.tokens_in), s => s.tokens_in, -1],
    ["Токенов всего, с rewrite и реранкером", s => fmt(s.tokens_all), s => s.tokens_all, -1],
    ["Время на вопрос, с", s => dec(s.seconds, 1), s => s.seconds, -1],
    ["Цена прогона", s => `$${s.cost.toFixed(4)}`, s => s.cost, -1],
  ];

  function drawSums() {
    const last = state.last;
    const { modes, models } = state.config;
    for (const [name, id] of [["exact", "#rrSumExact"], ["talk", "#rrSumTalk"]]) {
      const s = last?.summary?.[name];
      if (!s || Object.keys(s).length < Object.keys(modes).length) { $(id).innerHTML = ""; continue; }
      $(id).innerHTML = `<tr><th></th>${Object.values(modes).map(t => `<th class="num">${esc(t)}</th>`).join("")}</tr>` +
        ROWS.map(([label, show, value, better]) => {
          const vals = Object.keys(modes).map(m => value ? value(s[m]) : 0);
          const best = better > 0 ? Math.max(...vals) : Math.min(...vals);
          return `<tr><td>${label}</td>${Object.keys(modes).map((m, i) =>
            `<td class="num">${better && vals[i] === best ? `<b>${show(s[m])}</b>` : show(s[m])}</td>`).join("")}</tr>`;
        }).join("");
    }
    if (!last?.summary) return ($("#rrEvalNote").textContent = "Прогона ещё не было.");
    const title = Object.fromEntries(models.map(m => [m.id, m.title]));
    const st = last.settings || {};
    $("#rrEvalNote").textContent = `Прогон ${last.created || "только что"} · ${title[last.model] || last.model} · ` +
      `K₁ ${st.k1}, K₂ ≤ ${st.k2}, cos ≥ ${dec(st.threshold)}, оценка ≥ ${st.min_score}`;
  }

  $("#rrEvalGo").addEventListener("click", async () => {
    const button = $("#rrEvalGo");
    button.disabled = true;
    const settings = { strategy: $("#rrStrategy").value, ...knobs() };
    const pending = new Set(["exact", "talk"].flatMap(s => state.config.questions.map((_, i) => `${s}:${i}`)));
    const total = pending.size;
    state.last = { results: [], model: $("#rrModel").value, settings };
    drawTable(pending);
    drawSums();
    $("#rrEvalNote").textContent = `Задаю ${total} вопросов в четырёх режимах…`;
    try {
      await stream("/api/rag/rerank/eval", { model: state.last.model, ...settings }, e => {
        if (e.t === "result") {
          pending.delete(`${e.set}:${e.i}`);
          state.last.results.push(e);
          drawTable(pending);
          $("#rrEvalNote").textContent = `Готово ${total - pending.size} из ${total}…`;
        } else if (e.t === "done") {
          state.last.summary = e.summary;
          state.last.created = "";
          drawSums();
          if (!e.saved) $("#rrEvalNote").textContent += " · прогон неполный, не сохранён";
          else loadSweep();
        }
      });
    } catch (e) {
      $("#rrEvalNote").textContent = "Прогон оборвался: " + e.message;
    }
    button.disabled = false;
  });

  // ── Точки порога ──────────────────────────────────────────────────────
  // Ряд — набор запросов, точка — вопрос из базы на лучшем чанке с фактом
  // среди первых K₂ (по косинусу), ✕ — вопрос вне базы на лучшем кандидате.
  const SETS = [["exact", "Точные", "a", false], ["exact_rw", "Точные после rewrite", "a", true],
                ["talk", "Разговорные", "b", false], ["talk_rw", "Разговорные после rewrite", "b", true]];
  const X0 = 0.15, X1 = 0.8;

  async function loadSweep() {
    $("#rrStrip").innerHTML = `<p class="hint">Считаю косинусы контрольных вопросов…</p>`;
    try {
      const r = await fetch("/api/rag/rerank/sweep?strategy=" + $("#rrStrategy").value);
      if (!r.ok) throw new Error((await r.json()).detail || r.status);
      state.sweep = await r.json();
      drawStrip();
    } catch (e) {
      $("#rrStrip").innerHTML = `<p class="err">Не посчитано: ${esc(e.message)}</p>`;
    }
  }

  function drawStrip() {
    const data = state.sweep, box = $("#rrStrip");
    if (!data || root.hidden || !box.clientWidth) return;
    const { threshold: tau, k2 } = knobs();
    const W = box.clientWidth, L = 8, R = 8, ROW = 64, TOP = 22;
    const sets = SETS.filter(([key]) => data[key]);
    const H = TOP + sets.length * ROW + 26;
    const x = v => L + (Math.max(X0, Math.min(X1, v)) - X0) / (X1 - X0) * (W - L - R);
    const ticks = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8];
    let svg = `<rect x="${L}" y="${TOP - 6}" width="${x(tau) - L}" height="${sets.length * ROW}" class="rr-cutzone"/>` +
      ticks.map(t => `<line x1="${x(t)}" x2="${x(t)}" y1="${TOP - 6}" y2="${TOP + sets.length * ROW - 6}" class="rr-grid"/>
        <text x="${x(t)}" y="${H - 6}" text-anchor="middle">${dec(t, 1)}</text>`).join("");
    sets.forEach(([key, title, tone, filled], row) => {
      const y = TOP + row * ROW + 36;
      const items = data[key];
      const inside = items.filter(q => !q.outside);
      const best = q => Math.max(-1, ...q.hits.slice(0, k2).filter(([, good]) => good).map(([s]) => s));
      const passed = inside.filter(q => best(q) >= tau).length;
      const out = items.find(q => q.outside);
      const outCut = out && out.hits[0][0] < tau;
      svg += `<text x="${L}" y="${y - 20}" class="lead">${title}</text>
        <text x="${W - R}" y="${y - 20}" text-anchor="end">нужный чанк прошёл: ${passed} из ${inside.length}${
          out ? ` · вне базы: ${outCut ? "отсечён" : "прошёл"}` : ""}</text>`;
      inside.forEach((q, j) => {
        const s = best(q), cy = y + ((j % 3) - 1) * 6;
        const label = `${q.i + 1}. ${q.q} — ${s < 0 ? `нет чанка с фактом в первых ${k2}` : "cos " + s.toFixed(3)}`;
        svg += `<g><title>${esc(label)}</title><circle cx="${x(s < 0 ? X0 : s)}" cy="${cy}" r="10" class="rr-hit"/>
          <circle cx="${x(s < 0 ? X0 : s)}" cy="${cy}" r="4.5" class="rr-dot ${tone}${filled ? " fill" : ""}${s < 0 ? " miss" : ""}"/></g>`;
      });
      if (out) {
        const cx = x(out.hits[0][0]);
        svg += `<g><title>${esc(`${out.i + 1}. ${out.q} — вне базы, лучший кандидат cos ${out.hits[0][0].toFixed(3)}`)}</title>
          <circle cx="${cx}" cy="${y}" r="10" class="rr-hit"/>
          <path d="M${cx - 5} ${y - 5}L${cx + 5} ${y + 5}M${cx + 5} ${y - 5}L${cx - 5} ${y + 5}" class="rr-out"/></g>`;
      }
    });
    svg += `<line x1="${x(tau)}" x2="${x(tau)}" y1="${TOP - 10}" y2="${TOP + sets.length * ROW - 6}" class="rr-tau"/>
      <text x="${x(tau)}" y="${TOP - 12}" text-anchor="middle" class="lead">порог ${dec(tau)}</text>`;
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img"
        aria-label="Косинус нужного чанка у контрольных вопросов и линия порога">${svg}</svg>` +
      `<div class="rag-legend"><span><i class="rr-key a"></i>точные</span><span><i class="rr-key b"></i>разговорные</span>
        <span><i class="rr-key a fill"></i>заливка — после rewrite</span><span>✕ — вопрос вне базы</span>
        <span>серый фон — отрезано порогом</span>${data.exact_rw ? "" : "<span>рядов после rewrite нет: прогона ещё не было</span>"}</div>`;
  }
  let pendingResize = 0;
  addEventListener("resize", () => {
    cancelAnimationFrame(pendingResize);
    pendingResize = requestAnimationFrame(drawStrip);
  });

  if (!root.hidden) window.rerankShow();
})();
