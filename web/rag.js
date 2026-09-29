// Неделя 5, день 21: индекс документов. Экран читает /api/rag/* и рисует
// набор документов, ход сборки, разрез одного документа двумя стратегиями,
// сравнение, контрольные вопросы, пробу поиска и карту эмбеддингов. Графики —
// ручной SVG по ширине экрана, поэтому всё рисуется, только когда вкладка
// видна (ragShow зовёт showPane страницы), и перерисовывается на resize.
// Помощники страницы ($, escapeHtml, stream) — из index.html.

(() => {
  const NS = "http://www.w3.org/2000/svg";
  const COLOR = { fixed: "var(--rag-a)", struct: "var(--rag-b)" };
  const DEFAULT_DOC = "2024-gayd-istochniki-mobi";
  const state = { shown: false, view: null, doc: "", cut: null, chunk: "", map: null, found: null };

  const fmt = n => Number(n).toLocaleString("ru");
  const esc = text => escapeHtml(String(text ?? ""));
  const pages = (a, b) => !a ? "—" : a === b ? `стр. ${a}` : `стр. ${a}–${b}`;
  const kb = bytes => bytes > 1 << 20 ? `${(bytes / (1 << 20)).toFixed(1)} МБ` : `${Math.round(bytes / 1024)} КБ`;

  function plural(n, forms) {
    const m10 = n % 10, m100 = n % 100;
    return forms[m10 === 1 && m100 !== 11 ? 0 : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? 1 : 2];
  }
  const chunksWord = n => `${fmt(n)} ${plural(n, ["чанк", "чанка", "чанков"])}`;

  function el(tag, attrs = {}, parent) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (parent) parent.append(node);
    return node;
  }

  async function getJSON(url, options) {
    const r = await fetch(url, options);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
    return r.json();
  }

  // ── Подсказка ─────────────────────────────────────────────────────────
  const tip = $("#ragTip");
  function hover(node, html) {
    node.addEventListener("pointermove", e => {
      tip.innerHTML = typeof html === "function" ? html() : html;
      tip.hidden = false;
      const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8);
      const y = e.clientY + 16 + tip.offsetHeight > innerHeight ? e.clientY - tip.offsetHeight - 10 : e.clientY + 16;
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    });
    node.addEventListener("pointerleave", () => (tip.hidden = true));
  }

  // ── Загрузка ──────────────────────────────────────────────────────────
  async function load() {
    state.view = await getJSON("/api/rag");
    drawHead();
    drawDocs();
    drawCompare();
    drawChecks();
    const kept = state.view.docs.filter(d => !d.skipped);
    const select = $("#ragDoc");
    select.innerHTML = kept.map(d =>
      `<option value="${d.id}">${esc(d.title)} · ${chunksWord(d.chunks.fixed)} / ${fmt(d.chunks.struct)}</option>`).join("");
    if (!kept.length) return;
    const doc = kept.some(d => d.id === state.doc) ? state.doc
      : kept.some(d => d.id === DEFAULT_DOC) ? DEFAULT_DOC : kept[0].id;
    await openDoc(doc);
    state.map = await getJSON("/api/rag/map");
    drawMaps();
  }

  function redraw() {
    if (!state.view || $("#week5").hidden) return;
    drawStrip();
    drawHists();
    drawMaps();
  }

  window.ragShow = () => {
    if (state.shown) return requestAnimationFrame(redraw);
    state.shown = true;
    load().catch(e => ($("#ragSub").textContent = "Индекс не прочитан: " + e.message));
  };
  let pending = 0;
  addEventListener("resize", () => {
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(redraw);
  });

  // ── Шапка и набор ─────────────────────────────────────────────────────
  function drawHead() {
    const { meta, file, bytes, can_build } = state.view;
    $("#ragSub").innerHTML = meta.model
      ? `Эмбеддинги <code>${esc(meta.model)}</code> через Ollama · ${meta.dim} измерений · индекс
         <code>${esc(file)}</code> (SQLite, ${kb(bytes)}) · собран ${esc(meta.built)}`
      : "Индекс ещё не собран — нажмите «Собрать индекс».";
    $("#ragBuild").disabled = !can_build;
    $("#ragBuildNote").textContent = can_build ? ""
      : "Папки с документами на этой машине нет: индекс собран локально и привезён сюда.";
  }

  function drawDocs() {
    const { docs } = state.view;
    const kept = docs.filter(d => !d.skipped);
    const chars = kept.reduce((s, d) => s + d.chars, 0);
    $("#ragTotals").textContent = docs.length
      ? `В индексе ${kept.length} из ${docs.length} файлов: ${fmt(chars)} символов — примерно ` +
        `${fmt(Math.round(chars / 1800))} машинописных страниц по 1800 знаков. ` +
        `Файл, где текста меньше страницы, не индексируется — причина в строке.`
      : "Документов пока нет.";
    $("#ragDocs").innerHTML = `<tr><th>Документ</th><th>Тип</th><th class="num">Стр.</th>
      <th class="num">Символов</th><th class="num"><span class="swatch fixed"></span>По размеру</th>
      <th class="num"><span class="swatch struct"></span>По структуре</th></tr>` +
      docs.map(d => `<tr class="${d.skipped ? "skip" : ""}">
        <td>${esc(d.title)}<div class="file">${esc(d.source)}</div></td><td>${d.kind}</td>
        <td class="num">${d.pages || "—"}</td><td class="num">${fmt(d.chars)}</td>
        ${d.skipped ? `<td colspan="2">пропущен: ${esc(d.skipped)}</td>`
          : `<td class="num">${fmt(d.chunks.fixed)}</td><td class="num">${fmt(d.chunks.struct)}</td>`}
      </tr>`).join("");
  }

  // ── Сборка ────────────────────────────────────────────────────────────
  function logLine(text, kind = "") {
    const li = document.createElement("li");
    li.textContent = text;
    li.className = kind;
    $("#ragLines").append(li);
    $("#ragLines").scrollTop = 1e9;
  }

  function meter(strategy, done, total, cached) {
    let row = $(`#ragProgress [data-s="${strategy}"]`);
    if (!row) {
      row = document.createElement("div");
      row.className = "rag-meterrow";
      row.dataset.s = strategy;
      row.innerHTML = `<span><span class="swatch ${strategy}"></span>${state.view.strategies[strategy]}</span>
        <div class="rag-meter"><i style="background:${COLOR[strategy]}"></i></div><span></span>`;
      $("#ragProgress").append(row);
    }
    $("i", row).style.width = (100 * done / Math.max(total, 1)) + "%";
    row.lastElementChild.textContent = `${done} / ${total}` + (cached ? ` · из кеша ${cached}` : "");
  }

  $("#ragBuild").addEventListener("click", async () => {
    const button = $("#ragBuild");
    button.disabled = true;
    $("#ragLog").hidden = false;
    $("#ragLines").innerHTML = "";
    $("#ragProgress").innerHTML = "";
    let ok = false;
    try {
      await stream("/api/rag/build", { fresh: $("#ragFresh").checked }, e => {
        if (e.t === "stage") logLine("→ " + e.text, "stage");
        else if (e.t === "doc") logLine(`${e.kind.padEnd(4)} ${e.pages ? e.pages + " стр." : "стр. —"} · ` +
          `${fmt(e.chars)} симв. · ${e.title}` + (e.skipped ? ` — пропущен: ${e.skipped}` : ""),
          e.skipped ? "skip" : "");
        else if (e.t === "chunked") logLine(Object.entries(e.counts)
          .map(([s, n]) => `${state.view.strategies[s]}: ${chunksWord(n)}`).join(", "));
        else if (e.t === "embed") meter(e.strategy, e.done, e.total, e.cached);
        else if (e.t === "error") logLine(e.text, "err");
        else if (e.t === "done") {
          ok = true;
          logLine(`Готово за ${e.seconds} с · индекс ${kb(e.bytes)}`, "done");
        }
      });
    } catch (e) {
      logLine("Сборка оборвалась: " + e.message, "err");
    }
    button.disabled = false;
    if (ok) {
      state.found = null;
      $("#ragResults").innerHTML = "";
      await load();
    }
  });

  // ── Разрез документа ──────────────────────────────────────────────────
  async function openDoc(id) {
    state.doc = id;
    $("#ragDoc").value = id;
    state.cut = await getJSON("/api/rag/doc/" + encodeURIComponent(id));
    drawStrip();
    drawMaps();
  }
  $("#ragDoc").addEventListener("change", e => {
    state.chunk = "";
    $("#ragChunk").hidden = true;
    openDoc(e.target.value);
  });

  function drawStrip() {
    const box = $("#ragStrip");
    const d = state.cut;
    if (!d) return (box.innerHTML = "");
    const W = box.clientWidth, L = 118, R = 6;
    if (!W) return;
    const x = pos => L + pos / d.chars * (W - L - R);
    const lanes = { fixed: 22, struct: 84 };
    const H = d.pages_at.length > 1 ? 150 : 128;
    box.innerHTML = "";
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H });
    el("path", { d: "M0,0 L0,6", stroke: "#181a24", "stroke-width": 2 },
      el("pattern", { id: "ragHatch", width: 5, height: 6, patternUnits: "userSpaceOnUse",
                      patternTransform: "rotate(45)" }, el("defs", {}, svg)));

    for (const s of ["fixed", "struct"]) {
      const top = lanes[s], list = d.chunks[s];
      const name = el("text", { x: 0, y: top + 13, class: "lead" }, svg);
      name.textContent = state.view.strategies[s];
      el("text", { x: 0, y: top + 30 }, svg).textContent = chunksWord(list.length);
      list.forEach((c, i) => {
        // У «по размеру» чанки перекрываются: чётные и нечётные идут в две
        // дорожки, чтобы перекрытие было видно, а не пряталось под соседом.
        const sub = s === "fixed" ? (i % 2) * 20 : 0;
        const h = s === "fixed" ? 18 : 38;
        const x0 = x(c.start), w = Math.max(1, x(c.stop) - x0 - 2);
        const g = el("g", { cursor: "pointer" }, svg);
        el("rect", { x: x0, y: top + sub, width: w, height: h, rx: 2, fill: COLOR[s],
                     opacity: c.id === state.chunk ? 1 : .82 }, g);
        if (c.crosses) el("rect", { x: x0, y: top + sub, width: w, height: h, rx: 2, fill: "url(#ragHatch)" }, g);
        if (c.id === state.chunk) {
          el("rect", { x: x0 - 1, y: top + sub - 1, width: w + 2, height: h + 2, rx: 3,
                       fill: "none", stroke: "var(--text)", "stroke-width": 2 }, g);
        }
        hover(g, `<b>${esc(c.id)}</b><br>${esc(c.section)}<br><span>${pages(c.page_from, c.page_to)} ·
          ${fmt(c.chars)} симв.${c.cut ? " · обрыв фразы" : ""}${c.crosses ? " · захватил 2+ раздела" : ""}</span>`);
        g.addEventListener("click", () => openChunk(c.id));
      });
    }

    d.sections.forEach((sec, i) => {
      if (!i) return;
      const sx = x(sec.start);
      el("line", { x1: sx, x2: sx, y1: 14, y2: 126, stroke: "var(--text)", "stroke-opacity": .55,
                   "stroke-dasharray": "3 3", "pointer-events": "none" }, svg);
      const hit = el("rect", { x: sx - 3, y: 4, width: 6, height: 14, fill: "transparent" }, svg);
      el("path", { d: `M${sx - 3},6 L${sx + 3},6 L${sx},12 Z`, fill: "var(--dim)", "pointer-events": "none" }, svg);
      hover(hit, `<b>${esc(sec.title)}</b><br><span>начало раздела · ${pages(sec.page, sec.page)}</span>`);
    });

    if (d.pages_at.length > 1) {
      const step = Math.ceil(d.pages_at.length / Math.max(1, (W - L) / 34));
      d.pages_at.forEach(([page, at], i) => {
        const px = x(at);
        el("line", { x1: px, x2: px, y1: 130, y2: 134, stroke: "var(--dim)" }, svg);
        if (i % step === 0) el("text", { x: px, y: 146, "text-anchor": "middle" }, svg).textContent = page;
      });
      el("text", { x: 0, y: 146 }, svg).textContent = "страницы";
    }
    box.append(svg);
    const legend = document.createElement("div");
    legend.className = "rag-legend";
    legend.innerHTML = `<span><span class="swatch fixed"></span>по размеру: окно ${state.view.meta.size},
      перекрытие ${state.view.meta.overlap} — дорожки чередуются</span>
      <span><span class="swatch struct"></span>по структуре</span>
      <span><span class="hatch"></span>захватил 2+ раздела</span>
      <span><span class="dash"></span>начало раздела (${d.sections.length})</span>`;
    box.append(legend);
  }

  async function openChunk(id, scroll = false) {
    const doc = id.split(":")[1];
    state.chunk = id;
    if (doc !== state.doc) await openDoc(doc);
    else drawStrip();
    drawMaps();
    const c = await getJSON("/api/rag/chunk/" + encodeURIComponent(id));
    const card = $("#ragChunk");
    card.hidden = false;
    card.innerHTML = `<h3><span class="swatch ${c.strategy}"></span>${esc(c.id)}</h3>
      <dl>
        <dt>chunk_id</dt><dd><code>${esc(c.id)}</code></dd>
        <dt>strategy</dt><dd>${esc(c.strategy)} — ${esc(state.view.strategies[c.strategy])}</dd>
        <dt>source</dt><dd>${esc(c.source)}</dd>
        <dt>file</dt><dd>${esc(c.file)}</dd>
        <dt>title</dt><dd>${esc(c.title)}</dd>
        <dt>section</dt><dd>${esc(c.section)}</dd>
        <dt>page</dt><dd>${pages(c.page_from, c.page_to)}</dd>
        <dt>смещение</dt><dd>${fmt(c.start)}–${fmt(c.stop)} (${fmt(c.chars)} симв., № ${c.ord + 1} в документе)</dd>
        <dt>форма</dt><dd>${c.cut ? "обрыв посреди фразы" : "кончается на границе фразы"} ·
          ${c.crosses ? "захватил 2+ раздела" : "один раздел"}</dd>
        <dt>вектор</dt><dd>${c.vector.dim} измерений, норма ${c.vector.norm}<br>
          <code>[${c.vector.head.join(", ")}, …]</code></dd>
      </dl>
      <pre>${esc(c.text)}</pre>`;
    if (scroll) $("#ragCut").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ── Сравнение ─────────────────────────────────────────────────────────
  const ROWS = [
    ["Чанков", s => s.chunks],
    ["Символов ушло в эмбеддинги", s => s.chars, "low"],
    ["Средний размер / медиана", s => `${fmt(s.avg)} / ${fmt(s.median)}`],
    ["Мин / макс", s => `${fmt(s.min)} / ${fmt(s.max)}`],
    ["Обрыв посреди фразы, %", s => s.cut, "low"],
    ["Захватили 2+ раздела, %", s => s.crosses, "low"],
    ["Крошки короче 200, %", s => s.tiny, "low"],
    ["Эмбеддинги в этой сборке, с", s => s.cached < s.chunks ? s.seconds
      : "из кеша"],
  ];

  function drawCompare() {
    const { meta, strategies } = state.view;
    if (!meta.stats) {
      $("#ragParams").textContent = "Сравнение появится после сборки.";
      $("#ragMetrics").innerHTML = "";
      return;
    }
    $("#ragParams").textContent =
      `По размеру — окно ${meta.size} символов с перекрытием ${meta.overlap}, край сдвигается к пробелу; ` +
      `в эмбеддинг идёт название документа. По структуре — раздел от заголовка до заголовка ` +
      `(в pdf-слайдах — страница с первой строкой), длиннее ${meta.max} — по абзацам и предложениям, ` +
      `короче ${meta.min} — к соседу; в эмбеддинг идёт документ и путь раздела.`;
    const sum = meta.checks.summary;
    const rows = [...ROWS.map(([label, get, better]) => [label, strategies => get(meta.stats[strategies]), better]),
      ["hit@1 контрольных вопросов", s => `${sum[s].hit1} / ${sum[s].total}`, "high", s => sum[s].hit1],
      ["hit@3", s => `${sum[s].hit3} / ${sum[s].total}`, "high", s => sum[s].hit3],
      ["MRR@5", s => sum[s].mrr, "high"]];
    $("#ragMetrics").innerHTML = `<tr><th></th>${Object.keys(strategies).map(s =>
      `<th class="num"><span class="swatch ${s}"></span>${strategies[s]}</th>`).join("")}</tr>` +
      rows.map(([label, get, better, score = get]) => {
        const [a, b] = ["fixed", "struct"].map(score);
        const win = !better || a === b ? "" : (better === "low") === (a < b) ? "fixed" : "struct";
        return `<tr><td>${label}</td>${["fixed", "struct"].map(s => {
          const v = get(s);
          const text = typeof v === "number" ? fmt(v) : v;
          return `<td class="num">${win === s ? `<b>${text}</b>` : text}</td>`;
        }).join("")}</tr>`;
      }).join("") +
      `<tr class="sum"><td colspan="3" class="file">Жирным — лучшее значение в строке, где «лучше» определено.</td></tr>`;
    drawHists();
  }

  function drawHists() {
    const box = $("#ragHists");
    const stats = state.view?.meta?.stats;
    if (!stats) return (box.innerHTML = "");
    box.innerHTML = "";
    const edges = [0, 200, 400, 600, 800, 1000, 1200, 1400, 1600];
    const top = Math.max(...Object.values(stats).flatMap(s => s.hist));
    const W = box.clientWidth, H = 168, L = 30, B = 34, T = 26;
    if (!W) return;
    const band = (W - L - 4) / edges.length;
    const y = v => H - B - v / top * (H - B - T);
    for (const s of ["fixed", "struct"]) {
      const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H }, box);
      el("text", { x: 0, y: 14, class: "lead" }, svg).textContent =
        `${state.view.strategies[s]} — распределение размеров, ${chunksWord(stats[s].chunks)}`;
      for (const v of [Math.round(top / 2), top]) {
        el("line", { x1: L, x2: W, y1: y(v), y2: y(v), stroke: "var(--line)" }, svg);
        el("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = v;
      }
      el("line", { x1: L, x2: W, y1: H - B, y2: H - B, stroke: "var(--dim)", "stroke-opacity": .5 }, svg);
      stats[s].hist.forEach((n, i) => {
        const bx = L + i * band + band * .12, bw = band * .76, by = y(n), bh = H - B - by;
        const g = el("g", {}, svg);
        el("rect", { x: L + i * band, y: T, width: band, height: H - B - T, fill: "transparent" }, g);
        if (n) {
          const r = Math.min(4, bh, bw / 2);
          el("path", { fill: COLOR[s], d: `M${bx},${H - B} V${by + r} Q${bx},${by} ${bx + r},${by}
            H${bx + bw - r} Q${bx + bw},${by} ${bx + bw},${by + r} V${H - B} Z` }, g);
        }
        const range = i < edges.length - 1 ? `${edges[i]}–${edges[i + 1]}` : `${edges[i]}+`;
        // Подписи — на границах корзин: столбик между 800 и 1000 — это 800–1000.
        el("text", { x: L + i * band, y: H - B + 16, "text-anchor": "middle" }, svg).textContent = edges[i];
        hover(g, `<b>${range} символов</b><br>${chunksWord(n)} · ${Math.round(100 * n / stats[s].chunks)}%`);
      });
      el("text", { x: W, y: H - 2, "text-anchor": "end" }, svg).textContent = "размер чанка, символов";
    }
  }

  // ── Контрольные вопросы ───────────────────────────────────────────────
  function drawChecks() {
    const checks = state.view.meta.checks;
    const table = $("#ragQuestions");
    if (!checks) return (table.innerHTML = "");
    const titles = Object.fromEntries(state.view.docs.map(d => [d.id, d.title]));
    const place = p => p ? `${p}` : "—";
    table.innerHTML = `<tr><th>Вопрос</th><th>Где ответ · что обязано быть в чанке</th>
      <th class="num"><span class="swatch fixed"></span>По размеру</th>
      <th class="num"><span class="swatch struct"></span>По структуре</th></tr>` +
      checks.questions.map((q, i) => `<tr class="rag-q" data-i="${i}"><td>${esc(q.q)}</td>
        <td>${esc(titles[q.doc] || q.doc)}<div class="file">${q.markers.map(m => `«${esc(m)}»`).join(" + ")}</div></td>
        <td class="num">${q.fixed === 1 ? "<b>1</b>" : place(q.fixed)}</td>
        <td class="num">${q.struct === 1 ? "<b>1</b>" : place(q.struct)}</td></tr>`).join("") +
      ["hit1", "hit3", "mrr"].map(k => `<tr class="sum"><td colspan="2">${{ hit1: "hit@1", hit3: "hit@3", mrr: "MRR@5" }[k]}</td>
        ${["fixed", "struct"].map(s => `<td class="num">${fmt(checks.summary[s][k])}${k === "mrr" ? "" : " / " + checks.summary[s].total}</td>`).join("")}</tr>`).join("");
    table.querySelectorAll(".rag-q").forEach(row => row.addEventListener("click", () => {
      $("#ragQuery").value = checks.questions[row.dataset.i].q;
      ask();
      $("#ragSearch").scrollIntoView({ behavior: "smooth", block: "start" });
    }));
  }

  // ── Поиск ─────────────────────────────────────────────────────────────
  async function ask() {
    const q = $("#ragQuery").value.trim();
    if (!q) return;
    const box = $("#ragResults");
    box.innerHTML = `<p class="rag-note">Считаю вектор запроса и ищу…</p>`;
    try {
      state.found = await getJSON("/api/rag/search", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q }),
      });
    } catch (e) {
      box.innerHTML = `<p class="err">${esc(e.message)}</p>`;
      return;
    }
    box.innerHTML = ["fixed", "struct"].map(s => `<div><h3><span class="swatch ${s}"></span>${state.view.strategies[s]}</h3>` +
      state.found[s].hits.map((h, i) => `<div class="rag-hit" data-id="${esc(h.id)}">
        <div class="head"><b>${i + 1}</b><b>${h.score.toFixed(3)}</b><span>${esc(h.title)} › ${esc(h.section)} · ${pages(h.page_from, h.page_to)}</span></div>
        <p>${esc(h.text.slice(0, 220))}${h.text.length > 220 ? "…" : ""}</p></div>`).join("") + "</div>").join("");
    box.querySelectorAll(".rag-hit").forEach(n => n.addEventListener("click", () => openChunk(n.dataset.id, true)));
    drawMaps();
  }
  $("#ragAsk").addEventListener("click", ask);
  $("#ragQuery").addEventListener("keydown", e => e.key === "Enter" && ask());

  // ── Карта ─────────────────────────────────────────────────────────────
  function drawMaps() {
    const box = $("#ragMaps");
    if (!state.map || !box.clientWidth) return;
    box.innerHTML = "";
    const titles = Object.fromEntries(state.view.docs.map(d => [d.id, d.title]));
    for (const s of ["fixed", "struct"]) {
      const cell = document.createElement("div");
      box.append(cell);
      const W = cell.clientWidth, H = Math.round(Math.min(420, Math.max(300, W * .66))), P = 16, T = 24;
      const pts = state.map[s];
      const found = state.found?.[s];
      const xs = pts.map(p => p.x).concat(found ? [found.at[0]] : []);
      const ys = pts.map(p => p.y).concat(found ? [found.at[1]] : []);
      const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
      const X = v => P + (v - x0) / (x1 - x0 || 1) * (W - 2 * P);
      const Y = v => T + P + (y1 - v) / (y1 - y0 || 1) * (H - T - 2 * P);
      const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H }, cell);
      el("rect", { x: .5, y: T + .5, width: W - 1, height: H - T - 1, rx: 8, fill: "none", stroke: "var(--line)" }, svg);
      const own = pts.filter(p => p.doc === state.doc).length;
      const share = state.view.meta.maps_var?.[s];
      el("text", { x: 0, y: 15, class: "lead" }, svg).textContent =
        `${state.view.strategies[s]} · ${chunksWord(pts.length)}` + (share ? ` · оси объясняют ${share}% разброса` : "") +
        (own ? ` · цветные — ${own} из «${titles[state.doc]}»` : "");
      const hits = new Map((found?.hits || []).map((h, i) => [h.id, i + 1]));
      const order = [...pts.filter(p => p.doc !== state.doc), ...pts.filter(p => p.doc === state.doc)];
      for (const p of order) {
        const mine = p.doc === state.doc;
        const dot = el("circle", { class: "pt", cx: X(p.x), cy: Y(p.y), r: mine ? 4.5 : 3.2,
          fill: mine ? COLOR[s] : "var(--rag-dot)", stroke: "transparent", "stroke-width": 6 }, svg);
        if (p.id === state.chunk) {
          el("circle", { cx: X(p.x), cy: Y(p.y), r: 8, fill: "none", stroke: "var(--text)", "stroke-width": 2,
                         "pointer-events": "none" }, svg);
        }
        hover(dot, `<b>${esc(titles[p.doc] || p.doc)}</b><br><span>${esc(p.id)}</span>`);
        dot.addEventListener("click", () => openChunk(p.id, true));
      }
      for (const p of pts.filter(q => hits.has(q.id))) {
        el("circle", { cx: X(p.x), cy: Y(p.y), r: 7, fill: "none", stroke: "var(--text)", "stroke-width": 1.5,
                       "pointer-events": "none" }, svg);
        el("text", { x: X(p.x) + 9, y: Y(p.y) - 6, class: "lead", "pointer-events": "none" }, svg).textContent = hits.get(p.id);
      }
      if (found) {
        const qx = X(found.at[0]), qy = Y(found.at[1]);
        el("path", { d: `M${qx},${qy - 8} L${qx + 8},${qy} L${qx},${qy + 8} L${qx - 8},${qy} Z`,
                     fill: "var(--text)", stroke: "var(--card)", "stroke-width": 2 }, svg);
        el("text", { x: qx + 11, y: qy + 4, class: "lead" }, svg).textContent = "запрос";
      }
    }
  }

  if (!$("#week5").hidden) window.ragShow();
})();
