// Неделя 5, день 24: цитаты, источники и «не знаю». Ответ модели — JSON с
// текстом, источниками по chunk_id и цитатами; на экране он разложен на три
// части, каждая цитата показана на своём месте в тексте чанка, а над ними —
// строка проверок: источники из контекста, цитаты дословно, числа, смысл по
// судье. Ниже порога релевантности — карточка «не знаю» с уточняющим
// вопросом. Внизу — прогон десяти контрольных вопросов (/api/rag/cite/*).
// Подвкладку показывает rag.js (window.citeShow).

(() => {
  const root = $('.rag-day[data-day="24"]');
  const state = { ready: false, config: null, last: null, busy: false, ctx: [], check: null };
  const esc = text => escapeHtml(String(text ?? ""));
  const fmt = n => Number(n).toLocaleString("ru");
  const dec = (n, d = 2) => Number(n).toFixed(d).replace(".", ",");
  const MARK = { ok: ["✓", "верно"], part: ["◐", "частично"], bad: ["✗", "мимо"] };
  const KIND = { exact: ["ok", "✓ дословно"], close: ["part", "◐ почти дословно"],
                 missing: ["bad", "✗ нет в чанке"], foreign: ["bad", "✗ chunk_id не из контекста"] };

  window.citeShow = () => {
    if (state.ready) return;
    state.ready = true;
    init().catch(e => ($("#ctEvalNote").textContent = "Набор не прочитан: " + e.message));
  };

  async function init() {
    state.config = await (await fetch("/api/rag/cite/setup")).json();
    const { models, default: model, judge, threshold } = state.config;
    $("#ctModel").innerHTML = models.map(m =>
      `<option value="${esc(m.id)}"${m.id === model ? " selected" : ""}>ответ: ${esc(m.title)}</option>`).join("");
    $("#ctJudge").textContent = `судья смысла: ${judge.title}`;
    $("#ctTau").value = threshold;
    $("#ctTauOut").textContent = dec(threshold);
    state.last = state.config.last;
    drawTable();
    drawTiles();
  }
  $("#ctTau").addEventListener("input", () => ($("#ctTauOut").textContent = dec($("#ctTau").value)));

  // ── Полоса шагов ──────────────────────────────────────────────────────
  function step(name, status, text) {
    const li = $(`#ctFlow [data-step="${name}"]`);
    li.className = status;
    if (text !== undefined) $("span", li).textContent = text;
  }

  function reset() {
    for (const s of ["search", "gate", "answer", "check", "judge"]) step(s, "", "—");
    $("#ctOut").hidden = true;
    for (const id of ["#ctChecks", "#ctAnswer", "#ctSources", "#ctQuotes", "#ctClaims", "#ctUnknown"]) $(id).innerHTML = "";
    $("#ctRaw").textContent = $("#ctPrompt").textContent = "";
    $("#ctClaimsBox").hidden = true;
  }

  // ── Ответ: текст, источники, цитаты ───────────────────────────────────
  const where = s => `«${esc(s.title)}» › ${esc(s.section)}` + (s.page_from ? ` · стр. ${s.page_from}` +
    (s.page_to && s.page_to !== s.page_from ? `–${s.page_to}` : "") : "");

  // Цитата на своём месте в тексте чанка: кусок до и после, сама — маркером.
  function inChunk(q, chunk) {
    if (!chunk || q.start === undefined) {
      return `<div class="ct-inchunk miss">${q.kind === "foreign" ? "такого chunk_id не было в контексте"
        : `в тексте чанка такой фразы нет — совпало ${Math.round(q.ratio * 100)}% букв`}</div>`;
    }
    const t = chunk.text, from = Math.max(0, q.start - 160), to = Math.min(t.length, q.stop + 160);
    return `<div class="ct-inchunk">${from ? "…" : ""}${esc(t.slice(from, q.start))}<mark class="${KIND[q.kind][0]}">${
      esc(t.slice(q.start, q.stop))}</mark>${esc(t.slice(q.stop, to))}${to < t.length ? "…" : ""}</div>`;
  }

  function drawAnswer(check, ctx) {
    $("#ctOut").hidden = false;
    $("#ctDoc").hidden = check.status !== "answer";
    $("#ctUnknown").hidden = check.status !== "unknown";
    if (check.status !== "answer") return;
    $("#ctAnswer").innerHTML = markdown(check.answer)
      .replace(/\[(\d+)\]/g, (_, n) => `<button class="ask-cite" data-n="${n}">[${n}]</button>`);
    $("#ctSources").innerHTML = check.sources.map(s => `
      <li data-n="${s.n ?? ""}" class="${s.known ? "" : "bad"}"><b>[${s.n ?? "?"}]</b>
        ${s.known ? where(s) : "<span class='err'>нет в контексте</span>"}<div><code>${esc(s.chunk_id)}</code></div></li>`).join("");
    const byId = Object.fromEntries(ctx.map(h => [h.id, h]));
    $("#ctQuotes").innerHTML = check.quotes.map(q => {
      const [tone, label] = KIND[q.kind];
      return `<div class="ct-quote ${tone}" data-n="${q.n ?? ""}">
        <div class="ct-qhead"><b>[${q.n ?? "?"}]</b> <code>${esc(q.chunk_id)}</code>
          <span class="ct-kind ${tone}">${label}${q.kind !== "close" ? "" : q.ratio >= 0.995
            ? " · склеена из частей чанка" : ` · ${Math.round(q.ratio * 100)}% букв`}</span></div>
        <blockquote>${esc(q.text)}</blockquote>${inChunk(q, byId[q.chunk_id])}</div>`;
    }).join("");
  }

  function drawUnknown(check, gate, best, threshold, near) {
    $("#ctUnknown").innerHTML = `<div class="ct-no">Не знаю</div>
      <p>${gate ? `Лучший кандидат — cos ${dec(best, 3)}, ниже порога ${dec(threshold)}: модель не отвечала по существу.`
        : "Чанки прошли порог, но модель не нашла в них ответа и вернула status «unknown»."}</p>
      ${check.clarify ? `<div class="ct-ask"><span>Ассистент спрашивает:</span> ${esc(check.clarify)}</div>`
        : `<div class="ct-ask err">Уточняющего вопроса нет</div>`}
      ${near?.length ? `<div class="ct-near"><span class="hint">Ближайшее в базе, ниже порога:</span> ${near.slice(0, 5).map(h =>
        `<span class="ct-chip">${esc(h.section)} <i>cos ${dec(h.score, 3)}</i></span>`).join("")}</div>` : ""}`;
  }

  // Строка проверок. Цвет повторяет знак, а не заменяет его.
  function chip(tone, text) { return `<li class="${tone}">${text}</li>`; }

  function drawChecks(check, judge, expected, gate) {
    const out = [];
    if (!check.format) out.push(chip("bad", "✗ ответ не разобран как JSON"));
    else if (check.status === "unknown") {
      out.push(chip("ok", `✓ «не знаю»: ${gate ? "ниже порога релевантности" : "решила модель"}`));
      out.push(check.clarify ? chip("ok", "✓ уточняющий вопрос") : chip("bad", "✗ без уточняющего вопроса"));
    } else if (check.status === "answer") {
      const n = check.sources.length, foreign = check.sources.filter(s => !s.known);
      out.push(check.has_sources ? chip("ok", `✓ источники: ${n}, все из контекста`)
        : chip("bad", n ? `✗ не из контекста: ${foreign.map(s => esc(s.chunk_id)).join(", ")}` : "✗ источников нет"));
      out.push(check.cites_ok ? chip("ok", "✓ каждая [n] ведёт на источник") : chip("bad", "✗ ссылка [n] без источника"));
      out.push(check.has_quotes ? chip(check.covered ? "ok" : "part",
        `${check.covered ? "✓" : "◐"} цитаты: ${check.quotes.length}${check.covered ? ", у каждого источника" : ", не у всех источников"}`)
        : chip("bad", "✗ цитат нет"));
      if (check.quotes.length) {
        const tone = check.missing ? "bad" : check.close ? "part" : "ok";
        out.push(chip(tone, `${{ ok: "✓", part: "◐", bad: "✗" }[tone]} дословно ${check.exact} из ${check.quotes.length}` +
          (check.close ? ` · почти ${check.close}` : "") + (check.missing ? ` · нет в чанке ${check.missing}` : "")));
      }
      out.push(check.numbers_missing.length ? chip("bad", `✗ числа без цитаты: ${check.numbers_missing.join(", ")}`)
        : chip("ok", "✓ числа ответа есть в цитатах"));
      if (judge === undefined) out.push(chip("run", "… судья читает цитаты"));
      else if (judge) {
        const total = judge.claims.length, sign = { ok: "✓", part: "◐", bad: "✗" }[judge.verdict];
        out.push(chip(judge.verdict, `${sign} смысл: подтверждено ${judge.yes} из ${total} утверждений` +
          (judge.partial ? ` · частично ${judge.partial}` : "") + (judge.no ? ` · нет в цитатах ${judge.no}` : "")));
      }
    }
    if (expected) {
      const [sign, word] = MARK[expected.verdict];
      out.push(chip(expected.verdict, `${sign} по ожиданию: ${word} · ${esc(expected.note)}`));
    }
    $("#ctChecks").innerHTML = out.join("");
  }

  function drawClaims(judge, quotes) {
    $("#ctClaimsBox").hidden = !judge;
    if (!judge) return;
    const word = { yes: ["ok", "✓ да"], partial: ["part", "◐ частично"], no: ["bad", "✗ нет"] };
    $("#ctClaims").innerHTML = `<tr><th>Утверждение ответа</th><th>Подтверждает</th><th>Цитата</th><th>Почему</th></tr>` +
      judge.claims.map(c => `<tr><td>${esc(c.claim)}</td><td><span class="ask-mark ${word[c.verdict][0]}">${word[c.verdict][1]}</span></td>
        <td class="num">${c.quote && quotes[c.quote - 1] ? `[${quotes[c.quote - 1].n ?? "?"}]` : "—"}</td><td class="hint">${esc(c.why)}</td></tr>`).join("");
    $("#ctClaimsBox").open = judge.verdict !== "ok";
  }

  // Наведение на [n] подсвечивает источник и его цитаты.
  root.addEventListener("pointerover", e => {
    const cite = e.target.closest(".ask-cite");
    root.querySelectorAll(".ct-sources li, .ct-quote").forEach(el =>
      el.classList.toggle("hot", !!cite && el.dataset.n === cite.dataset.n));
  });

  // ── Вопрос ────────────────────────────────────────────────────────────
  async function ask() {
    const q = $("#ctQ").value.trim();
    if (!q || state.busy) return;
    state.busy = true;
    $("#ctGo").disabled = true;
    reset();
    step("q", "done", `«${q.length > 70 ? q.slice(0, 68) + "…" : q}»`);
    step("search", "run", "ищу в индексе…");
    let search = null, check = null, raw = "";
    try {
      await stream("/api/rag/cite/ask", { q, model: $("#ctModel").value, threshold: Number($("#ctTau").value) }, e => {
        if (e.t === "search") {
          search = e;
          const kept = e.candidates.filter(h => h.kept);
          state.ctx = kept;
          step("search", "done", `${e.candidates.length} кандидатов за ${fmt(e.ms)} мс · лучший cos ${dec(e.best, 3)}`);
          step("gate", kept.length ? "done" : "stop", kept.length
            ? `cos ${dec(e.best, 3)} ≥ ${dec(e.threshold)} → ${kept.length} чанка в контекст`
            : `cos ${dec(e.best, 3)} < ${dec(e.threshold)} → «не знаю», модель пишет уточнение`);
          step("answer", "run", kept.length ? "модель пишет JSON…" : "уточняющий вопрос…");
        } else if (e.t === "prompt") {
          $("#ctPrompt").textContent = `system:\n${e.system}\n\nuser:\n${e.user}`;
        } else if (e.t === "delta") {
          raw += e.text;
          $("#ctOut").hidden = false;
          $("#ctRaw").textContent = raw;
        } else if (e.t === "checked") {
          check = e.check;
          $("#ctRaw").textContent = e.raw;
          step("answer", e.error ? "fail" : "done", e.error || (check.status === "unknown" ? "status: unknown"
            : `status: answer · источников ${check.sources.length} · цитат ${check.quotes.length}`));
          step("check", check.format ? "done" : "fail", !check.format ? "не JSON" : check.status === "unknown"
            ? (check.clarify ? "уточняющий вопрос есть" : "без уточнения")
            : `дословно ${check.exact} из ${check.quotes.length}${check.missing ? ` · нет в чанке ${check.missing}` : ""}`);
          step("judge", check.status === "answer" && check.quotes.length ? "run" : "skip",
            check.status === "answer" && check.quotes.length ? "судья читает цитаты…" : "сверять нечего");
          drawAnswer(check, state.ctx);
          if (check.status === "unknown") drawUnknown(check, e.gate, search.best, search.threshold, search.candidates);
          drawChecks(check, check.status === "answer" && check.quotes.length ? undefined : null, null, e.gate);
        } else if (e.t === "judge") {
          const j = e.judge;
          step("judge", j ? "done" : "fail", j ? `${j.yes} из ${j.claims.length} утверждений подтверждены цитатами`
            : e.error || "судья вернул не тот JSON");
          drawClaims(j, check.quotes);
          drawChecks(check, j, null, false);
        } else if (e.t === "done") {
          drawChecks(e.check, e.judge, e.expected, e.gate);
        } else if (e.t === "error") {
          step("search", "fail", e.message);
        }
      });
    } catch (e) {
      step("answer", "fail", "запрос оборвался: " + e.message);
    }
    state.busy = false;
    $("#ctGo").disabled = false;
  }
  $("#ctGo").addEventListener("click", ask);
  $("#ctQ").addEventListener("keydown", e => e.key === "Enter" && ask());

  // Строка прогона — наверх, без нового запроса.
  function showRecord(r) {
    const q = state.config.questions[r.i];
    $("#ctQ").value = q.q;
    reset();
    const kept = r.context.length, threshold = state.last?.settings?.threshold ?? state.config.threshold;
    step("q", "done", `«${q.q}» · из прогона ${state.last?.created || "только что"}`);
    step("search", "done", `${r.candidates.length} кандидатов · лучший cos ${dec(r.best, 3)}`);
    step("gate", kept ? "done" : "stop", kept ? `cos ${dec(r.best, 3)} ≥ ${dec(threshold)} → ${kept} чанка в контекст`
      : `cos ${dec(r.best, 3)} < ${dec(threshold)} → «не знаю»`);
    step("answer", "done", `status: ${r.check.status || "?"}`);
    step("check", "done", r.check.status === "answer" ? `дословно ${r.check.exact} из ${r.check.quotes.length}` : "—");
    step("judge", r.judge ? "done" : "skip", r.judge ? `${r.judge.yes} из ${r.judge.claims.length} утверждений подтверждены` : "сверять нечего");
    $("#ctRaw").textContent = r.raw || "";
    $("#ctPrompt").textContent = "из прогона — промпт не сохраняется, «Спросить» покажет его";
    drawAnswer(r.check, r.context);
    if (r.check.status === "unknown") drawUnknown(r.check, r.gate, r.best, threshold, r.candidates);
    drawClaims(r.judge, r.check.quotes || []);
    drawChecks(r.check, r.judge, r.expected, r.gate);
    $("#ctCard").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ── Прогон десяти вопросов ────────────────────────────────────────────
  const yes = (v, tone = v ? "ok" : "bad") => `<span class="ask-mark ${tone}">${v ? "✓" : "✗"}</span>`;

  function drawTable(pending = new Set()) {
    const results = new Map((state.last?.results || []).filter(Boolean).map(r => [r.i, r]));
    $("#ctTable").innerHTML = `<tr><th>№</th><th>Вопрос · ожидание</th><th class="num">Лучший cos</th><th>Ответ</th>
      <th>Источники</th><th>Цитаты</th><th>Дословно</th><th>Смысл</th><th>По ожиданию</th></tr>` +
      state.config.questions.map((q, i) => {
        const r = results.get(i), c = r?.check;
        const cells = pending.has(i) ? Array(6).fill(`<span class="hint">жду…</span>`) : !c ? Array(6).fill("—")
          : c.status !== "answer" ? [
              c.status === "unknown" ? `«не знаю»${c.clarify ? " + уточнение" : ""}` : `<span class="err">не JSON</span>`,
              "—", "—", "—", "—"]
          : ["ответ", yes(c.has_sources) + ` ${c.sources.length}`, yes(c.has_quotes) + ` ${c.quotes.length}`,
              `${c.exact} из ${c.quotes.length}${c.close ? ` · почти ${c.close}` : ""}${c.missing ? ` · <span class="err">нет ${c.missing}</span>` : ""}`,
              r.judge ? `<span class="ask-mark ${r.judge.verdict}">${{ ok: "✓", part: "◐", bad: "✗" }[r.judge.verdict]}</span> ${r.judge.yes} из ${r.judge.claims.length}` : "—"];
        if (c && !pending.has(i)) cells.push(`<span class="ask-mark ${r.expected.verdict}">${MARK[r.expected.verdict].join(" ")}</span><div class="hint">${esc(r.expected.note)}</div>`);
        return `<tr class="ask-row" data-i="${i}"><td class="num">${i + 1}</td>
          <td><div>${esc(q.q)}</div><div class="file">ожидание: ${esc(q.expect)}</div></td>
          <td class="num">${r?.best !== undefined && !pending.has(i) ? dec(r.best, 3) : "—"}</td>${cells.map(x => `<td>${x}</td>`).join("")}</tr>`;
      }).join("");
    $("#ctTable").querySelectorAll(".ask-row").forEach(row => row.addEventListener("click", () => {
      const r = results.get(Number(row.dataset.i));
      if (r?.check) showRecord(r);
    }));
  }

  function tile(label, value, note, tone) {
    return `<div class="ct-tile ${tone}"><span>${label}</span><b>${value}</b><i>${note}</i></div>`;
  }

  function drawTiles() {
    const s = state.last?.summary;
    if (!s) {
      $("#ctTiles").innerHTML = "";
      $("#ctEvalNote").textContent = "Прогона ещё не было.";
      return;
    }
    const full = (a, b) => (a === b ? "ok" : "bad");
    $("#ctTiles").innerHTML = [
      tile("Источники в ответах", `${s.has_sources} из ${s.answers}`, "все chunk_id — из контекста", full(s.has_sources, s.answers)),
      tile("Цитаты в ответах", `${s.has_quotes} из ${s.answers}`, `у каждого источника — в ${s.covered}`, full(s.has_quotes, s.answers)),
      tile("Цитаты дословно", `${s.exact} из ${s.quotes}`, `почти ${s.close} · нет в чанке ${s.missing}`, s.missing ? "bad" : s.close ? "part" : "ok"),
      tile("Смысл совпал с цитатами", `${s.meaning_ok} из ${s.judged}`, `утверждений подтверждено ${s.claims_yes} из ${s.claims}`, full(s.meaning_ok, s.judged)),
      tile("«Не знаю» с уточнением", `${s.weak_clarify} из ${s.weak}`, "на вопросах со слабым контекстом", full(s.weak_clarify, s.weak)),
      tile("По ожиданию", `${s.ok} из ${s.total}`, `частично ${s.part} · мимо ${s.bad}`, full(s.ok, s.total)),
    ].join("");
    const title = Object.fromEntries(state.config.models.map(m => [m.id, m.title]));
    $("#ctEvalNote").textContent = `Прогон ${state.last.created || "только что"} · ответ ${title[state.last.model] || state.last.model}` +
      ` · судья ${title[state.last.judge] || state.last.judge} · порог ${dec(state.last.settings?.threshold ?? state.config.threshold)}` +
      ` · ${fmt(s.tokens)} ток. и ${dec(s.seconds, 1)} с на вопрос · $${s.cost.toFixed(4)} за прогон`;
  }

  $("#ctEvalGo").addEventListener("click", async () => {
    const button = $("#ctEvalGo");
    button.disabled = true;
    const total = state.config.questions.length;
    const pending = new Set(state.config.questions.map((_, i) => i));
    const threshold = Number($("#ctTau").value);
    state.last = { results: [], model: $("#ctModel").value, judge: state.config.judge.id, settings: { threshold } };
    drawTable(pending);
    $("#ctTiles").innerHTML = "";
    $("#ctEvalNote").textContent = `Задаю ${total} вопросов…`;
    try {
      await stream("/api/rag/cite/eval", { model: state.last.model, threshold }, e => {
        if (e.t === "result") {
          pending.delete(e.i);
          state.last.results.push(e);
          drawTable(pending);
          $("#ctEvalNote").textContent = `Готово ${total - pending.size} из ${total}…`;
        } else if (e.t === "done") {
          state.last.summary = e.summary;
          state.last.created = "";
          drawTiles();
          if (!e.saved) $("#ctEvalNote").textContent += " · прогон неполный, не сохранён";
        }
      });
    } catch (e) {
      $("#ctEvalNote").textContent = "Прогон оборвался: " + e.message;
    }
    button.disabled = false;
  });

  if (!root.hidden) window.citeShow();
})();
