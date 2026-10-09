// Неделя 6, день 27: локальный ассистент — чат-приложение на модели с этого
// ПК. Слева чаты и модель, в центре лента с ответом потоком и кнопкой «Стоп»,
// справа «Без облака»: состояние модели, счётчики запросов, охранник клиента
// и журнал сети. Без модели на машине (VPS) — снимок последнего чата с ПК,
// отправка выключена. Данные — /api/assist/*, подвкладку показывает local.js.

(() => {
  const root = $('.rag-day[data-day="27"]');
  const state = { ready: false, live: false, chats: [], chat: null, busy: false, abort: null, net: [], window: 12 };
  const esc = text => escapeHtml(String(text ?? ""));
  const dec = (n, d = 1) => Number(n).toFixed(d).replace(".", ",");
  const kb = n => n >= 1024 ? `${dec(n / 1024)} КБ` : `${n} Б`;
  const LOCAL = /^(127\.0\.0\.1|localhost|::1):/;
  function replies(n) {
    const m10 = n % 10, m100 = n % 100;
    return n + (m10 === 1 && m100 !== 11 ? " реплика" : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? " реплики" : " реплик");
  }

  async function getJSON(url, options) {
    const r = await fetch(url, options);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
    return r.json();
  }

  // Поток ndjson, который можно оборвать: «Стоп» закрывает соединение, стенд
  // закрывает своё к Ollama, и модель бросает генерацию.
  async function ndjson(url, payload, signal, handle) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(payload), signal });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
    const reader = r.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n")) >= 0) {
        const raw = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 1);
        if (raw.trim()) handle(JSON.parse(raw));
      }
    }
  }

  window.assistShow = () => {
    if (state.ready) return;
    state.ready = true;
    init().catch(e => ($("#laWindow").textContent = "Стенд не ответил: " + e.message));
  };

  const summary = chat => ({ id: chat.id, title: chat.title, model: chat.model, updated: chat.updated,
                             count: chat.messages.length });

  async function init() {
    const v = await getJSON("/api/assist");
    state.live = v.live;
    state.window = v.window;
    const snap = v.snapshot?.chat;
    const models = v.live ? v.models : [snap?.model || v.default];
    $("#laModel").innerHTML = models.map(m =>
      `<option${m === v.default ? " selected" : ""}>${esc(m)}</option>`).join("");
    drawModel(v.status);
    drawGuard(v);
    if (v.live) {
      state.net = v.net;
      state.chats = v.chats;
      if (v.chats.length) openChat(await getJSON(`/api/assist/chat/${v.chats[0].id}`));
      else openChat(null);
    } else {
      $("#laSnap").hidden = false;
      $("#laSnap").textContent = (snap ? `Снимок чата с ПК от ${snap.updated}. ` : "Снимка чата нет. ")
        + `На этой машине модели ${v.status.model} нет, поэтому отправка выключена.`;
      state.chats = snap ? [summary(snap)] : [];
      state.net = snap ? snap.messages.filter(m => m.net).map(m => m.net) : [];
      openChat(snap || null);
    }
    drawNet();
    lock(false);
  }

  function lock(busy) {
    state.busy = busy;
    $("#laSend").textContent = busy ? "Стоп" : "Отправить";
    $("#laSend").disabled = !state.live;
    $("#laText").disabled = !state.live;
    for (const id of ["#laNew", "#laModel", "#laProbe"]) $(id).disabled = busy || !state.live;
  }

  // ── Чаты и лента ──────────────────────────────────────────────────────
  function drawChats() {
    $("#laChats").innerHTML = state.chats.map(c => `
      <li data-id="${esc(c.id)}" class="${c.id === state.chat?.id ? "on" : ""}">${esc(c.title)}
        <span class="hint">${replies(c.count)} · ${esc(c.updated.slice(5, 16))}</span></li>`).join("");
  }

  $("#laChats").addEventListener("click", async e => {
    const li = e.target.closest("li");
    if (!li || state.busy || li.dataset.id === state.chat?.id || !state.live) return;
    openChat(await getJSON(`/api/assist/chat/${li.dataset.id}`));
  });

  function openChat(chat) {
    state.chat = chat;
    $("#laTitle").textContent = chat ? chat.title : "Новый чат";
    windowHint();
    drawChats();
    drawThread();
  }

  function windowHint(sent, of) {
    const model = state.chat?.model || $("#laModel").value;
    $("#laWindow").textContent = `${model} · в запрос уходят роль и последние ${state.window} реплик`
      + (sent ? ` — сейчас ${sent} из ${of}` : "");
  }

  function meta(m) {
    if (!m.metrics) return "";
    const x = m.metrics, parts = [esc(m.model)];
    if (x.ttft != null) parts.push(`первый токен ${dec(x.ttft, 2)} с`);
    if (x.out != null) parts.push(`${x.out} ток за ${dec(x.seconds, 2)} с`);
    if (x.tps) parts.push(`≈${Math.round(x.tps)} ток/с`);
    parts.push(`в запросе ${replies(m.window)}`, "0 ₽");
    const n = m.net;
    const net = n ? `<div class="net">→ ${esc(n.method)} ${esc(n.host)}${esc(n.path)} · ${n.status ?? "—"}
      · ↑ ${kb(n.sent)}${n.got ? ` ↓ ${kb(n.got)}` : ""}</div>` : "";
    return parts.join(" · ") + net;
  }

  function bubble(m) {
    if (m.role === "user") return `<div class="tk-user">${esc(m.content)}</div>`;
    const body = m.error ? `<span class="err">${esc(m.error)}</span>`
      : markdown(m.content) + (m.stopped ? `<span class="la-stop"> ■ остановлено</span>` : "");
    return `<div class="tk-bot la-bot"><div class="tk-answer">${body}</div><div class="la-meta">${meta(m)}</div></div>`;
  }

  const scrollDown = () => ($("#laThread").scrollTop = $("#laThread").scrollHeight);

  function drawThread() {
    const messages = state.chat?.messages || [];
    $("#laThread").innerHTML = messages.length ? messages.map(bubble).join("")
      : `<div class="la-empty">Спросите что-нибудь — ответит модель с этого компьютера.</div>`;
    scrollDown();
  }

  async function newChat() {
    const chat = await getJSON("/api/assist/new", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: $("#laModel").value }) });
    state.chats.unshift(summary(chat));
    openChat(chat);
  }

  $("#laNew").addEventListener("click", () => newChat().catch(e => ($("#laWindow").textContent = e.message)));

  function touch(chat) {
    const i = state.chats.findIndex(c => c.id === chat.id);
    if (i >= 0) state.chats.splice(i, 1);
    state.chats.unshift(summary(chat));
    drawChats();
    $("#laTitle").textContent = chat.title;
  }

  async function send() {
    if (state.busy) {
      state.abort?.abort();
      return;
    }
    const text = $("#laText").value.trim();
    if (!text || !state.live) return;
    lock(true);
    let box;
    try {
      if (!state.chat) await newChat();
      const chat = state.chat;
      $("#laText").value = "";
      chat.messages.push({ role: "user", content: text });
      if (chat.title === "Новый чат") chat.title = text.slice(0, 48);
      drawThread();
      box = document.createElement("div");
      box.className = "tk-bot la-bot run";
      box.innerHTML = `<div class="tk-answer"></div><div class="la-meta">запрос уходит в локальную модель…</div>`;
      $("#laThread").append(box);
      scrollDown();
      const answer = $(".tk-answer", box);
      state.abort = new AbortController();
      await ndjson("/api/assist/send", { chat: chat.id, text }, state.abort.signal, e => {
        if (e.t === "start") windowHint(e.window, e.of);
        else if (e.t === "net") {
          state.net.push(e.net);
          drawNet(true);
          $(".la-meta", box).textContent = `→ ${e.net.method} ${e.net.host}${e.net.path} · ${e.net.status ?? "—"} · модель пишет…`;
        } else if (e.t === "delta") {
          answer.textContent += e.text;
          scrollDown();
        } else if (e.t === "done") {
          if (e.message.net) state.net[state.net.length - 1] = e.message.net;
          chat.messages.push(e.message);
          drawThread();
          drawNet();
        }
      });
      touch(chat);
    } catch (e) {
      if (e.name === "AbortError") {
        // Начало ответа стенд сохраняет сам, когда заметит закрытое соединение, —
        // это доли секунды или секунда; ждём, пока оно появится в базе.
        // Реплику и ответ стенд пишет разом, так что ждём, пока их станет на две больше.
        const expected = state.chat.messages.length + 1;
        let fresh;
        for (let i = 0; i < 10; i++) {
          await new Promise(done => setTimeout(done, 500));
          fresh = await getJSON(`/api/assist/chat/${state.chat.id}`);
          if (fresh.messages.length >= expected) break;
        }
        const last = fresh.messages.at(-1);
        if (last?.net) state.net[state.net.length - 1] = last.net;
        openChat(fresh);
        touch(fresh);
        drawNet();
      } else if (box) {
        box.innerHTML = `<div class="tk-answer"><span class="err">${esc(e.message)}</span></div>`;
      } else {
        $("#laWindow").textContent = e.message;
      }
    }
    state.abort = null;
    lock(false);
    refreshModel();
  }

  $("#laSend").addEventListener("click", send);
  $("#laText").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  // ── Без облака ────────────────────────────────────────────────────────
  // Состояние модели опрашивает код дня 26 (/api/local), в журнал оно не идёт:
  // журнал — только запросы клиента приложения.
  function drawModel(st) {
    const run = st.loaded;
    const where = !run ? "" : run.vram >= run.size ? " · 100% GPU" : run.vram ? ` · ${Math.round(run.vram / run.size * 100)}% GPU` : " · CPU";
    $("#laModelState").innerHTML = st.error ? `<span class="err">${esc(st.error)}</span>`
      : `<b>${esc(st.model)}</b> в Ollama ${esc(st.version || "")} · ${esc(st.url.replace(/^https?:\/\//, ""))}<br>`
        + (run ? `в памяти${where}` : "выгружена — первый ответ начнётся с загрузки");
  }

  async function refreshModel() {
    if (!state.live) return;
    drawModel(await getJSON("/api/local").then(v => v.status).catch(() => ({ error: "Ollama не ответила" })));
  }

  function drawGuard(v) {
    $("#laGuard").textContent = `Клиент приложения пускает только на ${v.guard.join(", ")}: запрос к другому хосту `
      + `отбивается в транспорте, до отправки. Ключей облака в окружении стенда: ${v.cloud_keys}`
      + (v.cloud_keys ? " — приложению они не нужны." : " — облачные модели здесь недоступны вовсе.");
  }

  function drawNet(fresh = false) {
    const local = state.net.filter(n => LOCAL.test(n.host) && !n.blocked).length;
    const blocked = state.net.filter(n => n.blocked).length;
    const out = state.net.length - local - blocked;
    const tiles = [[out ? "bad" : "ok", "ушло наружу", out], [blocked ? "part" : "", "отбито", blocked],
                   ["", "к этой машине", local]];
    $("#laTiles").innerHTML = tiles.map(([tone, label, value]) =>
      `<div class="ct-tile ${tone}"><span>${label}</span><b>${value}</b></div>`).join("");
    $("#laLog").innerHTML = state.net.slice().reverse().map((n, i) => `
      <li class="${n.blocked ? "no" : ""}${fresh && !i ? " new" : ""}"><b>${esc(n.at)}</b> ${esc(n.method)}
        ${esc(n.host)}${esc(n.path)} · ${n.blocked ? "отбит охранником"
          : `${n.status ?? "—"}${n.ms != null ? ` · ${n.ms} мс` : ""}${n.got ? ` · ↓ ${kb(n.got)}` : ""}`}</li>`).join("")
      || `<li>запросов пока не было</li>`;
  }

  $("#laProbe").addEventListener("click", async () => {
    $("#laProbe").disabled = true;
    try {
      const res = await getJSON("/api/assist/probe", { method: "POST" });
      state.net.push(res.net);
      drawNet(true);
      $("#laProbeNote").innerHTML = res.blocked ? `<span class="err">✗ ${esc(res.message)}</span>`
        : `<span class="err">запрос ушёл наружу: ${res.status}</span>`;
    } catch (e) {
      $("#laProbeNote").textContent = e.message;
    }
    $("#laProbe").disabled = !state.live;
  });

  // Открыли сразу по адресу #week6/27 — local.js показал день до этого файла.
  if (!root.hidden) window.assistShow();
})();
