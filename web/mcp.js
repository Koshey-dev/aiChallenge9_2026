// День 16: стенд MCP. Живёт блоком дня в окне «Настройки» чата
// (#mcpBlock в index.html), оформление — в chat.css. Клиент на сервере
// (web/agent/mcp.py): браузеру чужой домен отдавать не обязан, да и задание
// про серверный код. Здесь — только показ разговора.
// Помощники страницы общие: $, post, escapeHtml.
(() => {
  const ui = {
    picks: $("#mcpPicks"), url: $("#mcpUrl"), go: $("#mcpGo"),
    shake: $("#mcpShake"), bad: $("#mcpBad"), out: $("#mcpOut"),
    facts: $("#mcpWho"), talk: $("#mcpTalk"), tools: $("#mcpTools"),
    toolsPart: $("#mcpToolsPart"), spin: $("#mcpSpin"),
  };
  let version = "";   // версия протокола, на которой здоровается стенд

  const pretty = value => escapeHtml(JSON.stringify(value, null, 2));

  // Число со словом в нужном падеже: «1 инструмент», «3 инструмента»,
  // «5 инструментов», и одиннадцать-девятнадцать особняком.
  const spell = (count, one, few, many) => {
    const last = count % 10, two = count % 100;
    const tail = two > 10 && two < 20 ? many
      : last === 1 ? one : last > 1 && last < 5 ? few : many;
    return `${count} ${tail}`;
  };

  function showServer(server, url) {
    if (!server.name) {
      ui.facts.innerHTML = "<p class='none'>Рукопожатия не было: стенд спросил список "
        + "сразу, поэтому сервер о себе ничего не рассказал.</p>";
      return;
    }
    const tiles = [
      ["Сервер", escapeHtml(server.name + (server.version ? " " + server.version : ""))],
      ["Протокол", server.protocol === version
        ? escapeHtml(server.protocol)
        : `${escapeHtml(server.protocol || "—")}<span class="warn"> ·
           просили ${escapeHtml(version)}</span>`],
      ["Сессия", server.session
        ? `<code>${escapeHtml(server.session.slice(0, 12))}…</code>`
        : "<span class='none'>не выдал</span>"],
      ["Умеет", (server.abilities || []).map(one =>
        `<code>${escapeHtml(one)}</code>`).join(" ") || "<span class='none'>—</span>"],
      ["Адрес", `<code>${escapeHtml(url)}</code>`],
    ];
    ui.facts.innerHTML = tiles.map(([title, value]) =>
      `<div class="tile"><b>${title}</b><span>${value}</span></div>`).join("")
      + (server.about ? `<p class="says">${escapeHtml(server.about)}</p>` : "")
      + (server.hint ? `<details class="says"><summary>Сервер передал инструкцию для
          модели — ${spell(server.hint.length, "символ", "символа", "символов")}</summary>
          <pre>${escapeHtml(server.hint)}</pre></details>` : "");
  }

  function showTalk(steps) {
    ui.talk.innerHTML = steps.map((step, number) => {
      const state = step.error ? "bad" : "ok";
      const code = step.status ? step.status : "нет ответа";
      return `<details class="step ${state}">
        <summary>
          <span class="no">${number + 1}</span>
          <code class="method">${escapeHtml(step.method)}</code>
          <span class="fine">${escapeHtml(step.note)}</span>
          <span class="code">${escapeHtml(String(code))}</span>
          <span class="ms">${step.ms} мс</span>
        </summary>
        ${step.error ? `<p class="why">${escapeHtml(step.error)}</p>` : ""}
        <div class="pair">
          <div><b>Ушло</b><pre>${pretty(step.sent)}</pre></div>
          <div><b>Пришло</b><pre>${step.got === null
            ? "<span class='none'>тела нет</span>" : pretty(step.got)}</pre></div>
        </div>
      </details>`;
    }).join("");
  }

  function showTools(tools, total) {
    ui.tools.innerHTML = `<p class="sum">
        ${spell(total.tools, "инструмент", "инструмента", "инструментов")} ·
        ${spell(total.chars, "символ", "символа", "символов")} ·
        <b>≈${spell(total.tokens, "токен", "токена", "токенов")}</b>
        <span class="fine">столько добавится к каждому запросу, если положить
          этот список в промпт модели</span></p>`
      + tools.map(tool => `<div class="tool">
          <div class="line">
            <code class="name">${escapeHtml(tool.name)}</code>
            ${tool.title ? `<span class="what">${escapeHtml(tool.title)}</span>` : ""}
            <span class="price">≈${tool.tokens} т.</span>
          </div>
          <p class="about">${escapeHtml(tool.description) || "<span class='none'>без описания</span>"}</p>
          ${tool.args.length ? `<ul class="args">${tool.args.map(arg =>
            `<li><code>${escapeHtml(arg.name)}</code>
              <span class="type">(${escapeHtml(arg.type)})</span>
              ${arg.required ? "<span class='must'>обязателен</span>" : ""}
              ${arg.note ? `<span class="fine">${escapeHtml(arg.note)}</span>` : ""}</li>`)
            .join("")}</ul>` : "<p class='none'>аргументов нет</p>"}
        </div>`).join("");
  }

  async function connect() {
    const url = ui.url.value.trim();
    if (!url) return;
    ui.go.disabled = true;
    ui.bad.hidden = true;
    // Ожидание показывает значок рядом, а не текст на кнопке: другой текст —
    // другая ширина, и строка с адресом дёргалась бы на каждом нажатии.
    ui.spin.classList.add("on");
    try {
      const res = await post("/api/mcp/tools", { url, shake: ui.shake.checked });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || "сервер стенда отказал");
      showServer(body.server, body.url);
      showTalk(body.steps);
      // Отказ сервера — тоже результат дня: разговор остаётся на экране, и
      // видно, на каком шаге он оборвался и что ответила та сторона.
      showTools(body.tools, body.total);
      ui.toolsPart.hidden = !body.tools.length;
      ui.bad.textContent = body.error;
      ui.bad.hidden = !body.error;
      ui.out.hidden = false;
    } catch (bad) {
      // Сюда доходит только негодный адрес: запрос не ушёл, показывать нечего.
      ui.bad.textContent = bad.message;
      ui.bad.hidden = false;
      ui.out.hidden = true;
    } finally {
      ui.go.disabled = false;
      ui.spin.classList.remove("on");
    }
  }

  fetch("/api/mcp/servers").then(res => res.json()).then(config => {
    version = config.version;
    ui.picks.innerHTML = config.servers.map((server, number) =>
      `<button class="pick${number ? "" : " on"}" data-url="${escapeHtml(server.url)}"
         title="${escapeHtml(server.note)}">${escapeHtml(server.title)}</button>`).join("")
      + `<span class="fine">публичные серверы без ключа · стенд здоровается
         версией <code>${escapeHtml(version)}</code></span>`;
    ui.url.value = config.servers[0].url;
    ui.picks.querySelectorAll(".pick").forEach(pick => {
      pick.addEventListener("click", () => {
        ui.url.value = pick.dataset.url;
        ui.picks.querySelectorAll(".pick")
          .forEach(one => one.classList.toggle("on", one === pick));
      });
    });
  });

  ui.go.addEventListener("click", connect);
  ui.url.addEventListener("keydown", event => {
    if (event.key === "Enter") connect();
  });
  // Выбранный адрес перестаёт совпадать с кнопкой, как только его правят руками.
  ui.url.addEventListener("input", () => ui.picks.querySelectorAll(".pick")
    .forEach(pick => pick.classList.toggle("on", pick.dataset.url === ui.url.value.trim())));
})();
