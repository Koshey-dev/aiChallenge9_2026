// Неделя 6, день 30: приватный AI-сервис на локальной LLM. Сервис — шлюз
// gateway.py перед Ollama на VPS, путь /llm, вход по ключу. Вкладка — его
// клиент: браузер ходит в публичный адрес сервиса по сети. Наверху — схема и
// живое состояние машины, ниже — чат, журнал запросов со всех устройств и
// проверки: доступ по сети, залп параллельных запросов, лимит ключа, предел
// контекста. Ключи отдаёт /api/service, порты проверяет /api/service/ports.

(() => {
  const root = $('.rag-day[data-day="30"]');
  const state = { ready: false, view: null, history: [], busy: false, seen: new Set(), timer: null };
  const esc = text => escapeHtml(String(text ?? ""));
  const dec = (n, d = 1) => n == null ? "—" : Number(n).toFixed(d).replace(".", ",");
  const SYSTEM = "Ты — приватный ассистент команды, которая делает игры. Модель работает на её сервере. "
    + "Отвечай по-русски, коротко и по делу.";
  const STATUS = { 200: "ok", 401: "bad", 413: "part", 429: "part", 499: "", 502: "bad", 503: "bad" };

  async function getJSON(url, options) {
    const r = await fetch(url, options);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
    return r.json();
  }

  const api = (path, key, options = {}) => fetch(state.view.url + path, {
    ...options, headers: { "Content-Type": "application/json", ...(key ? { Authorization: "Bearer " + key } : {}),
                           ...(options.headers || {}) } });
  const key = name => state.view.keys[name];

  window.serviceShow = () => {
    if (state.ready) return;
    state.ready = true;
    init().catch(e => ($("#svNote").textContent = "Стенд не ответил: " + e.message));
  };

  async function init() {
    state.view = await getJSON("/api/service");
    $("#svUrl").textContent = state.view.url;
    $("#svPage").href = state.view.url + "/";
    $("#svPage").textContent = state.view.url + "/";
    for (const el of root.querySelectorAll(".sv-base")) el.textContent = state.view.url;
    if (!state.view.ready) {
      $("#svNote").textContent = "Ключей сервиса на этом стенде нет (LLM_KEY_STAND, LLM_KEY_TEST, LLM_KEY_LOAD в .env) — запросы выключены.";
      root.querySelectorAll("button, textarea").forEach(b => (b.disabled = true));
      return;
    }
    await refresh();
    state.timer = setInterval(() => !document.hidden && !root.hidden && refresh().catch(() => {}), 3000);
  }

  // ── Состояние и журнал ───────────────────────────────────────────────
  async function refresh() {
    const r = await api("/stats", key("stand"));
    if (!r.ok) throw new Error("HTTP " + r.status);
    const s = state.stats = await r.json();
    const m = s.memory, c = s.counts, sum = codes => codes.reduce((a, k) => a + (c[k] || 0), 0);
    const tiles = [
      ["", "Модель", s.loaded ? `в памяти · ${dec(s.loaded.size / 2 ** 30)} ГБ` : (s.ollama ? "выгружена" : "Ollama молчит"),
       `${s.model} · окно ${s.context} · ответ до ${s.max_tokens} ток.`],
      [m && m.available < 200 ? "part" : "", "Память сервера", m ? `${m.available} МБ свободно` : "—",
       m ? `из ${m.total} МБ · swap занят ${m.swap_used} МБ · ядер ${s.cpus} · загрузка ${dec(s.load, 2)}` : ""],
      [s.waiting ? "part" : "", "Очередь", `${s.busy ? 1 : 0} в работе · ${s.waiting} ждут`, `не больше 1 + ${s.queue_max}`],
      ["", "Запросы", `${sum(["200"])} ответов`,
       `отказы: 401 — ${sum(["401"])}, 429 — ${sum(["429"])}, 413 — ${sum(["413"])}, 503 — ${sum(["503"])}, ушли — ${sum(["499"])}`],
      ["", "Работает", uptime(s.uptime), "процесс шлюза"],
    ];
    $("#svTiles").innerHTML = tiles.map(([tone, label, value, note]) =>
      `<div class="ct-tile ${tone}"><span>${label}</span><b>${esc(value)}</b><i>${esc(note)}</i></div>`).join("");
    drawJournal(s.journal.slice(0, 25));
  }

  const uptime = s => s < 3600 ? `${Math.floor(s / 60)} мин` : `${Math.floor(s / 3600)} ч ${Math.floor(s % 3600 / 60)} мин`;

  function drawJournal(rows) {
    const id = e => e.id || `${e.at}${e.key}${e.status}${e.device}`;
    $("#svJournal").innerHTML = `<tr><th>Время</th><th>Устройство</th><th>Сеть</th><th>Ключ</th><th>Статус</th>
      <th class="num">Очередь</th><th class="num">Первый токен</th><th class="num">Всего</th><th class="num">Токены</th><th>Заметка</th></tr>`
      + rows.map(e => `<tr class="${state.seen.size && !state.seen.has(id(e)) ? "sv-new" : ""}">
        <td>${esc(e.at)}</td><td><b>${esc(e.device)}</b></td><td class="hint">${esc(e.ip)}</td><td>${esc(e.key ?? "—")}</td>
        <td><span class="sv-code ${STATUS[e.status] ?? ""}">${e.status}</span></td>
        <td class="num">${e.queue != null ? dec(e.queue) + " с" : "—"}</td>
        <td class="num">${e.ttft != null ? dec(e.ttft) + " с" : "—"}</td>
        <td class="num">${e.seconds != null ? dec(e.seconds) + " с" : "—"}</td>
        <td class="num">${e.in != null ? `${e.in} → ${e.out ?? "—"}` : "—"}</td>
        <td class="hint">${esc(e.reason ?? "")}</td></tr>`).join("");
    rows.forEach(e => state.seen.add(id(e)));
  }

  // ── Поток ответа: SSE в формате OpenAI плюс комментарии очереди ──────
  async function stream(body, keyName, on, signal) {
    const r = await api("/v1/chat/completions", key(keyName), { method: "POST", body: JSON.stringify({ ...body, stream: true }), signal });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || "";
      return { status: r.status, detail, retry: r.headers.get("Retry-After") };
    }
    on({ t: "open", limit: r.headers.get("X-Max-Tokens") });
    const reader = r.body.getReader(), dec2 = new TextDecoder();
    let buf = "", usage = null, text = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec2.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const ev = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        if (ev.startsWith(": queue")) on({ t: "queue", position: Number(ev.split(" ").pop()) });
        else if (ev.startsWith("data: {")) {
          const c = JSON.parse(ev.slice(6));
          if (c.error) return { status: 502, detail: c.error.message };
          if (c.usage) usage = c.usage;
          const piece = c.choices?.[0]?.delta?.content;
          if (piece) { text += piece; on({ t: "delta", text }); }
        }
      }
    }
    return { status: 200, text, usage };
  }

  // ── Чат ──────────────────────────────────────────────────────────────
  function bubble(cls, html) {
    $("#svLog").querySelector(".hint")?.remove();
    const div = document.createElement("div");
    div.className = "sv-msg " + cls;
    div.innerHTML = html;
    $("#svLog").append(div);
    $("#svLog").scrollTop = $("#svLog").scrollHeight;
    return div;
  }

  function context(usage) {
    const used = usage ? usage.prompt_tokens + usage.completion_tokens : 0;
    $("#svCtxText").textContent = usage ? `контекст: ${used} из ${state.stats?.context ?? 2048} токенов` : "контекст: —";
    $("#svCtxBar").style.width = `${Math.min(100, used / (state.stats?.context ?? 2048) * 100)}%`;
  }

  async function send() {
    const text = $("#svText").value.trim();
    if (!text || state.busy) return;
    state.busy = true;
    $("#svSend").disabled = true;
    $("#svText").value = "";
    bubble("user", esc(text));
    state.history.push({ role: "user", content: text });
    const bot = bubble("bot", `<div class="sv-body"></div><div class="sv-meta sv-wait">отправляю…</div>`);
    const meta = $(".sv-meta", bot);
    try {
      const res = await stream({ messages: [{ role: "system", content: SYSTEM }, ...state.history] }, "stand", e => {
        if (e.t === "queue") meta.textContent = `в очереди: ${e.position}`;
        if (e.t === "delta") { $(".sv-body", bot).innerHTML = markdown(e.text); meta.textContent = "пишет…"; }
      });
      if (res.status !== 200) throw new Error(`${res.status}: ${res.detail}`);
      state.history.push({ role: "assistant", content: res.text });
      const u = res.usage;
      meta.classList.remove("sv-wait");
      meta.textContent = `очередь ${dec(u.queue_seconds)} с · первый токен ${dec(u.ttft_seconds)} с · ответ ${dec(u.seconds)} с · `
        + `${dec(u.tokens_per_second)} ток/с · ${u.prompt_tokens} → ${u.completion_tokens} ток.`;
      context(u);
    } catch (e) {
      state.history.pop();
      bot.classList.add("err");
      bot.innerHTML = esc(e.message);
    }
    state.busy = false;
    $("#svSend").disabled = false;
    refresh().catch(() => {});
  }
  $("#svSend").addEventListener("click", send);
  $("#svText").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  $("#svReset").addEventListener("click", () => {
    state.history = [];
    $("#svLog").innerHTML = `<p class="hint">Новый чат.</p>`;
    context(null);
  });

  // ── Проверки ─────────────────────────────────────────────────────────
  const line = (ok, text) => `<li class="${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"} ${text}</li>`;

  $("#svNet").addEventListener("click", async () => {
    const box = $("#svNetOut");
    box.innerHTML = `<li class="hint">проверяю…</li>`;
    const out = [];
    const models = await api("/v1/models", key("stand"));
    const list = models.ok ? (await models.json()).data.map(m => m.id).join(", ") : "";
    out.push(line(models.status === 200, `с ключом: <b>${models.status}</b>${list ? ` — модель ${esc(list)}` : ""}`));
    const none = await api("/v1/models");
    out.push(line(none.status === 401, `без ключа: <b>${none.status}</b> — ${esc((await none.json()).detail)}`));
    const wrong = await fetch(state.view.url + "/v1/models", { headers: { Authorization: "Bearer not-a-real-key" } });
    out.push(line(wrong.status === 401, `чужой ключ: <b>${wrong.status}</b>`));
    box.innerHTML = out.join("") + `<li class="hint">порты сервера с машины стенда…</li>`;
    const p = await getJSON("/api/service/ports");
    box.innerHTML = out.join("") + p.ports.map(x => line(x.port === 443 ? x.state === "открыт" : x.state !== "открыт",
      `${esc(p.host)}:${x.port} (${esc(x.what)}) — ${esc(x.state)}, ${x.ms} мс`)).join("");
    refresh().catch(() => {});
  });

  $("#svBurst").addEventListener("click", async () => {
    const n = 6;
    const before = state.stats?.memory?.available;
    const box = $("#svGantt");
    const runs = Array.from({ length: n }, (_, i) => ({ i, start: performance.now(), queue: 0 }));
    const draw = () => {
      const now = performance.now(), t0 = runs[0].start;
      const end = Math.max(...runs.map(r => (r.end ?? now) - t0), 1000);
      box.innerHTML = runs.map(r => {
        const pct = ms => `${(ms / end * 100).toFixed(2)}%`;
        const stop = (r.end ?? now) - t0, wait = r.slot != null ? r.slot - t0 : (r.status ? stop : now - t0);
        const first = r.first != null ? r.first - t0 : null;
        const bar = r.status && r.status !== 200
          ? `<i class="no" style="left:0;width:${pct(Math.max(stop, end * 0.02))}"></i>`
          : `<i class="wait" style="left:0;width:${pct(wait)}"></i>` + (r.slot != null
            ? `<i class="read" style="left:${pct(wait)};width:${pct((first ?? stop) - wait)}"></i>` : "")
            + (first != null ? `<i class="gen" style="left:${pct(first)};width:${pct(stop - first)}"></i>` : "");
        const label = r.status === 200 ? `200 · в очереди было ${r.maxQueue || 0}-м, ждал ${dec((r.slot - t0) / 1000)} с, всего ${dec(stop / 1000)} с`
          : r.status ? `${r.status} · ${esc(r.detail)}` : r.queue ? `в очереди: ${r.queue}` : r.first ? "пишет…" : "жду…";
        return `<div class="sv-lane"><span>запрос ${r.i + 1}</span><div class="sv-track">${bar}</div><em>${label}</em></div>`;
      }).join("");
    };
    const tick = setInterval(draw, 200);
    $("#svBurst").disabled = true;
    await Promise.all(runs.map(async r => {
      const res = await stream({ messages: [{ role: "user", content: `Назови число ${r.i + 1} словом.` }], max_tokens: 12 }, "load", e => {
        if (e.t === "queue") { r.queue = e.position; r.maxQueue = Math.max(r.maxQueue || 0, e.position); }
        if (e.t === "delta" && r.first == null) r.first = performance.now();
      }).catch(e => ({ status: 0, detail: e.message }));
      r.end = performance.now();
      r.status = res.status;
      r.detail = res.detail;
      if (res.usage) {
        r.slot = r.start + res.usage.queue_seconds * 1000;
        r.first = r.start + res.usage.ttft_seconds * 1000;
      }
    }));
    clearInterval(tick);
    draw();
    await refresh().catch(() => {});
    const ok = runs.filter(r => r.status === 200).length;
    $("#svBurstNote").textContent = `${ok} из ${n} ответили по очереди, ${n - ok} получили отказ сразу · свободная память `
      + `${before ?? "—"} → ${state.stats?.memory?.available ?? "—"} МБ`;
    $("#svBurst").disabled = false;
  });

  $("#svRate").addEventListener("click", async () => {
    const box = $("#svRateOut");
    box.innerHTML = "";
    $("#svRate").disabled = true;
    for (let i = 1; i <= 7; i++) {
      const r = await api("/v1/chat/completions", key("test"), { method: "POST",
        body: JSON.stringify({ messages: [{ role: "user", content: "Скажи «да»." }], max_tokens: 1 }) });
      const detail = r.ok ? "" : (await r.json().catch(() => ({}))).detail;
      if (r.ok) await r.json();
      box.insertAdjacentHTML("beforeend", `<li class="${r.ok ? "ok" : "part"}"><b>${i}</b> · ${r.status}${
        r.ok ? " — ответ" : ` — Retry-After ${r.headers.get("Retry-After")} с · ${esc(detail)}`}</li>`);
    }
    $("#svRate").disabled = false;
    refresh().catch(() => {});
  });

  $("#svCtx").addEventListener("click", async () => {
    const box = $("#svCtxOut");
    box.innerHTML = `<li class="hint">проверяю…</li>`;
    const long = "Игра — это система правил, целей и обратной связи с игроком. ".repeat(150);
    const r = await api("/v1/chat/completions", key("stand"), { method: "POST",
      body: JSON.stringify({ messages: [{ role: "user", content: long + "Перескажи коротко." }] }) });
    const detail = (await r.json().catch(() => ({}))).detail;
    const out = [line(r.status === 413, `сообщение на ${long.length.toLocaleString("ru")} символов: <b>${r.status}</b> — ${esc(detail)}`)];
    // max_tokens сверх предела: сервис урежет и скажет об этом в заголовке — ответ дожидаться не нужно.
    const stop = new AbortController();
    const asked = 2000;
    const res = await stream({ messages: [{ role: "user", content: "Скажи «ок»." }], max_tokens: asked }, "stand", e => {
      if (e.t === "open") {
        out.push(line(Number(e.limit) < asked, `max_tokens ${asked}: принят, урезан до <b>${e.limit}</b> (заголовок X-Max-Tokens)`));
        stop.abort();
      }
    }, stop.signal).catch(() => null);
    if (res && res.status !== 200) out.push(line(false, `max_tokens ${asked}: ${res.status} ${esc(res.detail)}`));
    box.innerHTML = out.join("");
    setTimeout(() => refresh().catch(() => {}), 800);
  });

  // Открыли сразу по адресу #week6/30 — local.js показал день до этого файла.
  if (!root.hidden) window.serviceShow();
})();
