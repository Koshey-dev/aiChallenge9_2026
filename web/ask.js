// Неделя 5, день 22: первый RAG-запрос. Агент с двумя режимами — без RAG и
// с RAG (или оба рядом): вопрос → поиск чанков → объединение с вопросом →
// запрос к LLM, каждый шаг загорается на полосе по мере событий потока
// /api/rag/ask. Ниже — десять контрольных вопросов с ожиданием и источниками
// и прогон всех в обоих режимах (/api/rag/eval). Подвкладку показывает rag.js
// (window.askShow). Помощники страницы ($, escapeHtml, markdown, stream) —
// из index.html.

(() => {
  const root = $('.rag-day[data-day="22"]');
  const state = { ready: false, mode: "both", config: null, last: null, busy: false };
  const esc = text => escapeHtml(String(text ?? ""));
  const fmt = n => Number(n).toLocaleString("ru");
  const MODES = { plain: ["plain"], rag: ["rag"], both: ["plain", "rag"] };
  const MARK = { ok: ["✓", "верно"], part: ["◐", "частично"], bad: ["✗", "мимо"] };

  const col = name => $(`.ask-col[data-col="${name}"]`, root);
  const pages = h => !h.page_from ? "" : ` · стр. ${h.page_from}` +
    (h.page_to && h.page_to !== h.page_from ? `–${h.page_to}` : "");

  window.askShow = () => {
    if (state.ready) return;
    state.ready = true;
    init().catch(e => ($("#evalNote").textContent = "Набор не прочитан: " + e.message));
  };

  async function init() {
    const r = await fetch("/api/rag/questions");
    state.config = await r.json();
    const { models, strategies, k, default: model } = state.config;
    $("#askModel").innerHTML = models.map(m =>
      `<option value="${esc(m.id)}"${m.id === model ? " selected" : ""}>${esc(m.title)}</option>`).join("");
    $("#askStrategy").innerHTML = Object.entries(strategies).map(([id, title]) =>
      `<option value="${id}"${id === "struct" ? " selected" : ""}>чанки: ${esc(title.toLowerCase())}</option>`).join("");
    $("#askK").innerHTML = [3, 5, 8].map(n => `<option${n === k ? " selected" : ""}>${n}</option>`).join("");
    state.last = state.config.last;
    setMode("both");
    drawTable();
    drawSummary();
  }

  // ── Режим агента ──────────────────────────────────────────────────────
  function setMode(mode) {
    state.mode = mode;
    root.querySelectorAll("#askModes button").forEach(b => b.classList.toggle("on", b.dataset.mode === mode));
    for (const name of ["plain", "rag"]) col(name).hidden = !MODES[mode].includes(name);
    $("#askCols").classList.toggle("one", mode !== "both");
  }
  root.querySelectorAll("#askModes button").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));

  // ── Полоса шагов ──────────────────────────────────────────────────────
  function step(name, status, text) {
    const li = $(`#askFlow [data-step="${name}"]`);
    li.className = status;
    if (text !== undefined) $("span", li).textContent = text;
  }

  function clearColumns() {
    for (const name of ["plain", "rag"]) {
      const c = col(name);
      c.dataset.raw = "";
      $(".ask-answer", c).innerHTML = "";
      $(".ask-grade", c).innerHTML = "";
      $(".ask-meta", c).textContent = "";
    }
    $(".ask-sources", col("rag")).innerHTML = "";
    $(".ask-prompt pre", col("rag")).textContent = "";
  }

  // Ответ — разметкой страницы, ссылки [n] на фрагменты — кнопками.
  function render(name, raw) {
    const c = col(name);
    c.dataset.raw = raw;
    $(".ask-answer", c).innerHTML = markdown(raw)
      .replace(/\[(\d+)\]/g, (_, n) => `<button class="ask-cite" data-n="${n}">[${n}]</button>`);
  }

  function gradeLine(grade, name) {
    if (!grade) return "";
    const [sign, word] = MARK[grade.verdict];
    const parts = [];
    if (grade.of) parts.push(`факты ${grade.facts} из ${grade.of}`);
    else parts.push(grade.refused ? "отказался ответить — ответа в базе нет" : "назвал ответ, которого в базе нет");
    if (name === "rag" && grade.of) {
      parts.push(grade.cited_ok ? "сослался на нужный документ" : grade.retrieved
        ? "нужный документ был в топе, но ссылки на него нет" : "нужного документа нет в топе");
    }
    return `<span class="ask-mark ${grade.verdict}">${sign} ${word}</span> ${parts.join(" · ")}`;
  }

  function meta(m) {
    return `${fmt(m.in)} ток. на входе · ${fmt(m.out)} на выходе · ${String(m.seconds).replace(".", ",")} с · $${m.cost.toFixed(5)}`;
  }

  function drawSources(hits, cited = [], full = true) {
    $(".ask-sources", col("rag")).innerHTML = hits.map((h, i) => `
      <li data-n="${i + 1}" class="${cited.includes(i + 1) ? "cited" : ""}">
        <div><b>[${i + 1}]</b> ${esc(h.title)} › ${esc(h.section)}${pages(h)}
          <span class="score">cos ${h.score.toFixed(3)}</span></div>
        ${full && h.text ? `<details><summary>текст фрагмента</summary><p>${esc(h.text)}</p></details>` : ""}
      </li>`).join("");
  }

  function markCited(cited) {
    col("rag").querySelectorAll(".ask-sources li")
      .forEach(li => li.classList.toggle("cited", cited.includes(Number(li.dataset.n))));
  }

  // Наведение на [n] в ответе подсвечивает фрагмент, клик раскрывает его текст.
  root.addEventListener("pointerover", e => {
    const cite = e.target.closest(".ask-cite");
    col("rag").querySelectorAll(".ask-sources li")
      .forEach(li => li.classList.toggle("hot", !!cite && li.dataset.n === cite.dataset.n));
  });
  root.addEventListener("click", e => {
    const cite = e.target.closest(".ask-cite");
    if (!cite) return;
    const item = $(`.ask-sources li[data-n="${cite.dataset.n}"] details`, col("rag"));
    if (item) item.open = true;
  });

  // ── Вопрос ────────────────────────────────────────────────────────────
  async function ask() {
    const q = $("#askQ").value.trim();
    if (!q || state.busy) return;
    state.busy = true;
    $("#askGo").disabled = true;
    const mode = state.mode, wanted = MODES[mode];
    clearColumns();
    const short = q.length > 70 ? q.slice(0, 68) + "…" : q;
    step("q", "done", `«${short}»`);
    const rag = wanted.includes("rag");
    step("search", rag ? "run" : "skip", rag ? "ищу в индексе…" : "без RAG — шаг пропущен");
    step("prompt", rag ? "" : "skip", rag ? "—" : "без RAG — вопрос уходит как есть");
    step("llm", rag ? "" : "run", rag ? "—" : "жду ответ…");
    const spent = {};
    try {
      await stream("/api/rag/ask", {
        q, mode, model: $("#askModel").value, strategy: $("#askStrategy").value, k: Number($("#askK").value),
      }, e => {
        if (e.t === "search") {
          step("search", "done", `${e.hits.length} чанков за ${fmt(e.ms)} мс · лучший cos ${e.hits[0].score.toFixed(3)}`);
          step("prompt", "run", "складываю…");
          drawSources(e.hits);
        } else if (e.t === "prompt") {
          step("prompt", "done", `${fmt(e.user.length)} симв.: фрагменты с метаданными + вопрос`);
          $(".ask-prompt pre", col("rag")).textContent = `system:\n${e.system}\n\nuser:\n${e.user}`;
          step("llm", "run", "жду ответ…");
        } else if (e.t === "delta") {
          render(e.target, (col(e.target).dataset.raw || "") + e.text);
        } else if (e.t === "error") {
          const where = e.target === "search" ? "search" : "llm";
          step(where, "fail", e.message);
          if (e.target !== "search") $(".ask-grade", col(e.target)).innerHTML = `<span class="err">${esc(e.message)}</span>`;
        } else if (e.t === "done") {
          render(e.target, e.answer);
          $(".ask-meta", col(e.target)).textContent = meta(e.metrics);
          if (e.grade) $(".ask-grade", col(e.target)).innerHTML = gradeLine(e.grade, e.target);
          if (e.cited) markCited(e.cited);
          spent[e.target] = e.metrics.in;
          if (wanted.every(w => w in spent)) {
            step("llm", "done", wanted.map(w => `${w === "rag" ? "с RAG" : "без RAG"} — ${fmt(spent[w])} ток. на входе`).join(" · "));
          }
        }
      });
    } catch (e) {
      step("llm", "fail", "запрос оборвался: " + e.message);
    }
    state.busy = false;
    $("#askGo").disabled = false;
  }
  $("#askGo").addEventListener("click", ask);
  $("#askQ").addEventListener("keydown", e => e.key === "Enter" && ask());

  // ── Контрольные вопросы ───────────────────────────────────────────────
  function cell(answer, name) {
    if (!answer) return `<span class="hint">—</span>`;
    if (answer.error) return `<span class="err">ошибка</span>`;
    const g = answer.grade;
    const [sign, word] = MARK[g.verdict];
    const detail = g.of ? `факты ${g.facts}/${g.of}` : g.refused ? "отказ" : "назвал ответ";
    const source = name === "rag" && g.of ? `<div class="hint">${g.cited_ok ? "источник [" +
      g.cited.join(", ") + "] ✓" : g.retrieved ? "в топе, без ссылки" : "не найден"}</div>` : "";
    return `<span class="ask-mark ${g.verdict}">${sign} ${word}</span><div class="hint">${detail}</div>${source}`;
  }

  function drawTable(pending = new Set()) {
    const results = new Map((state.last?.results || []).filter(Boolean).map(r => [r.i, r]));
    $("#evalTable").innerHTML = `<tr><th>№</th><th>Вопрос · ожидание</th><th>Где ответ</th>
      <th>Без RAG</th><th>С RAG</th></tr>` +
      state.config.questions.map((q, i) => {
        const r = results.get(i);
        const wait = pending.has(i) ? `<span class="hint">жду…</span>` : null;
        return `<tr class="ask-row" data-i="${i}">
          <td class="num">${i + 1}</td>
          <td><div>${esc(q.q)}</div><div class="file">ожидание: ${esc(q.expect)}</div></td>
          <td>${q.outside ? `<span class="hint">вне базы</span>` : q.sources.map(s => esc(s.title)).join("<br>") +
            (q.section ? `<div class="file">› ${esc(q.section)}</div>` : "")}</td>
          <td>${wait ?? cell(r?.plain, "plain")}</td><td>${wait ?? cell(r?.rag, "rag")}</td></tr>`;
      }).join("");
    $("#evalTable").querySelectorAll(".ask-row").forEach(row => row.addEventListener("click", () => showRow(Number(row.dataset.i))));
  }

  // Строка прогона — наверх, в колонки: оба ответа, сверка и источники без нового запроса.
  function showRow(i) {
    const q = state.config.questions[i];
    const r = (state.last?.results || []).find(x => x && x.i === i);
    $("#askQ").value = q.q;
    if (!r) return $("#askCard").scrollIntoView({ behavior: "smooth", block: "start" });
    setMode("both");
    clearColumns();
    step("q", "done", `«${q.q}» · из прогона ${state.last.created || "только что"}`);
    step("search", "done", `${r.hits.length} чанков · лучший cos ${r.hits[0]?.score.toFixed(3) ?? "—"}`);
    step("prompt", "done", "фрагменты с метаданными + вопрос");
    step("llm", "done", `без RAG — ${fmt(r.plain.metrics.in)} ток. на входе · с RAG — ${fmt(r.rag.metrics.in)} ток.`);
    for (const name of ["plain", "rag"]) {
      render(name, r[name].answer);
      $(".ask-meta", col(name)).textContent = meta(r[name].metrics);
      $(".ask-grade", col(name)).innerHTML = gradeLine(r[name].grade, name);
    }
    drawSources(r.hits, r.rag.cited || [], false);
    $("#askCard").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function drawSummary() {
    const last = state.last, s = last?.summary;
    if (!s?.plain || !s?.rag) {
      $("#evalSummary").innerHTML = "";
      $("#evalNote").textContent = "Прогона ещё не было.";
      return;
    }
    const models = Object.fromEntries(state.config.models.map(m => [m.id, m.title]));
    $("#evalNote").textContent = `Прогон ${last.created || "только что"} · ${models[last.model] || last.model} · ` +
      `${state.config.strategies[last.strategy].toLowerCase()} · топ-${last.k}`;
    const { plain: p, rag: r } = s;
    const outside = v => v === null ? "—" : v ? "✓ отказался" : "✗ назвал ответ";
    const rows = [
      ["Верных ответов", `${p.ok} из ${p.total}`, `${r.ok} из ${r.total}`, p.ok < r.ok ? "rag" : p.ok > r.ok ? "plain" : ""],
      ["Частично / мимо", `${p.part} / ${p.bad}`, `${r.part} / ${r.bad}`, ""],
      ["Фактов из ожиданий", `${p.facts} из ${p.of}`, `${r.facts} из ${r.of}`, p.facts < r.facts ? "rag" : p.facts > r.facts ? "plain" : ""],
      ["Вопрос вне базы", outside(p.outside_refused), outside(r.outside_refused), ""],
      ["Нужный документ в топе", "—", `${r.retrieved} из ${r.inside}`, ""],
      ["Ответ сослался на него", "—", `${r.cited_ok} из ${r.inside}`, ""],
      ["Токенов на входе, в среднем", fmt(p.tokens_in), fmt(r.tokens_in), p.tokens_in < r.tokens_in ? "plain" : "rag"],
      ["Токенов на выходе, в среднем", fmt(p.tokens_out), fmt(r.tokens_out), p.tokens_out < r.tokens_out ? "plain" : "rag"],
      ["Время ответа, с", String(p.seconds).replace(".", ","), String(r.seconds).replace(".", ","), p.seconds < r.seconds ? "plain" : "rag"],
      ["Цена прогона", `$${p.cost.toFixed(5)}`, `$${r.cost.toFixed(5)}`, p.cost < r.cost ? "plain" : "rag"],
    ];
    $("#evalSummary").innerHTML = `<tr><th></th><th class="num">Без RAG</th><th class="num">С RAG</th></tr>` +
      rows.map(([label, a, b, win]) => `<tr><td>${label}</td>
        <td class="num">${win === "plain" ? `<b>${a}</b>` : a}</td><td class="num">${win === "rag" ? `<b>${b}</b>` : b}</td></tr>`).join("");
  }

  $("#evalGo").addEventListener("click", async () => {
    const button = $("#evalGo");
    button.disabled = true;
    const total = state.config.questions.length;
    const pending = new Set(state.config.questions.map((_, i) => i));
    state.last = { results: [], model: $("#askModel").value, strategy: $("#askStrategy").value, k: Number($("#askK").value) };
    drawTable(pending);
    $("#evalSummary").innerHTML = "";
    $("#evalNote").textContent = `Задаю ${total} вопросов в обоих режимах…`;
    try {
      await stream("/api/rag/eval", { model: state.last.model, strategy: state.last.strategy, k: state.last.k }, e => {
        if (e.t === "result") {
          pending.delete(e.i);
          state.last.results.push(e);
          drawTable(pending);
          $("#evalNote").textContent = `Готово ${total - pending.size} из ${total}…`;
        } else if (e.t === "done") {
          state.last.summary = e.summary;
          state.last.created = "";
          drawSummary();
          if (!e.saved) $("#evalNote").textContent += " · прогон неполный, не сохранён";
        }
      });
    } catch (e) {
      $("#evalNote").textContent = "Прогон оборвался: " + e.message;
    }
    button.disabled = false;
  });

  if (!root.hidden) window.askShow();
})();
