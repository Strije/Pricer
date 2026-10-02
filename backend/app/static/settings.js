// Настройки: сервисы (ЮKassa, Laximo) — свои карточки с понятными полями и проверкой подключения;
// прайс-листы — сводка источников (полная настройка — следующий шаг).
(() => {
  const TAX = [[1, "ОСН — общая"], [2, "УСН «доходы»"], [3, "УСН «доходы минус расходы»"], [4, "ЕНВД"], [5, "ЕСХН"], [6, "Патент"]];
  // Коды НДС ЮKassa (vat_code). Код для 22% появился с повышением НДС — сверьте с документацией ЮKassa.
  const VAT = [[1, "Без НДС"], [2, "НДС 0%"], [7, "НДС 5%"], [8, "НДС 7%"], [3, "НДС 10%"], [4, "НДС 20%"], [11, "НДС 22% (сверьте код с ЮKassa)"]];
  const SUBJECT = [["commodity", "Товар"], ["service", "Услуга"]];
  const MODE = [["full_prepayment", "Полная предоплата"], ["full_payment", "Полный расчёт"], ["prepayment", "Частичная предоплата"], ["advance", "Аванс"]];
  const opts = (list, value) => list.map(([v, t]) => `<option value="${v}"${String(v) === String(value ?? "") ? " selected" : ""}>${esc(t)}</option>`).join("");
  let accounts = [];

  const status = (acc) => {
    const st = (acc && acc.status) || {};
    if (!acc) return '<span class="st muted">не подключён</span>';
    if (acc.config.enabled === false) return '<span class="st muted">выключен</span>';
    if (st.ok === true) return `<span class="st ok" title="${esc(st.at || "")}">работает</span>`;
    if (st.ok === false) return `<span class="st bad">ошибка</span>`;
    return '<span class="st warn">не проверен</span>';
  };

  window.loadServices = async function () {
    try { accounts = await api("/api/suppliers"); } catch (e) { accounts = []; }
    const yk = accounts.find((a) => a.section === "yookassa"), lx = accounts.find((a) => a.section === "laximo");
    $("services-box").innerHTML = yooCard(yk) + laximoCard(lx);
    bind("yookassa", yk, yooForm);
    bind("laximo", lx, () => ({config: {enabled: $("lx-enabled").checked}, secrets: secretValues({login: "lx-login", password: "lx-password"})}));
    if (yk) {
      const sync = () => {
        $("yk-test-box").classList.toggle("hidden", $("yk-mode").value !== "test");
        $("yk-receipt-box").classList.toggle("hidden", !$("yk-receipts").checked);
      };
      $("yk-mode").onchange = sync; $("yk-receipts").onchange = sync; sync();
    }
  };

  function secretValues(map) {
    const out = {};
    for (const [name, id] of Object.entries(map)) { const v = $(id).value.trim(); if (v) out[name] = v; }
    return out;
  }
  const secretInput = (acc, name, id, label) => `<label>${label}<input type="password" id="${id}" autocomplete="new-password"
      placeholder="${acc && acc.secrets_set.includes(name) ? "сохранено — оставьте пустым, чтобы не менять" : ""}"></label>`;

  function yooCard(acc) {
    const c = (acc && acc.config) || {}, st = (acc && acc.status) || {};
    if (!acc) return `<div class="card svc"><h3>Оплата ЮKassa</h3><div class="note">Оплата подборов по ссылке: карта, СБП, SberPay — способ выбирает клиент на странице ЮKassa.</div>
      <div class="row" style="margin-top:10px"><button data-connect="yookassa">Подключить</button></div></div>`;
    return `<form class="card svc" id="svc-yookassa"><div class="row" style="justify-content:space-between"><h3>Оплата ЮKassa</h3>${status(acc)}</div>
      <label class="check"><input type="checkbox" id="yk-enabled" ${c.enabled === false ? "" : "checked"}> Принимать оплату подборов</label>
      <label>Режим<select id="yk-mode">${opts([["live", "Боевой магазин"], ["test", "Тестовый (деньги не списываются)"]], c.test_mode ? "test" : "live")}</select></label>
      <label>Идентификатор магазина (shopId)<input id="yk-shop" value="${esc(c.shop_id || "")}" inputmode="numeric"></label>
      ${secretInput(acc, "secret_key", "yk-secret", "Секретный ключ (secretKey)")}
      <div id="yk-test-box"><label>Тестовый shopId<input id="yk-test-shop" value="${esc(c.test_shop_id || "")}" inputmode="numeric"></label>
        ${secretInput(acc, "test_secret_key", "yk-test-secret", "Тестовый секретный ключ")}</div>
      <label class="check" style="margin-top:10px"><input type="checkbox" id="yk-receipts" ${c.receipts ? "checked" : ""}> Отправлять чеки (54-ФЗ)</label>
      <div id="yk-receipt-box">
        <label>Система налогообложения<select id="yk-tax">${opts(TAX, c.tax_system_code ?? 3)}</select></label>
        <label>НДС<select id="yk-vat">${opts(VAT, c.vat_code ?? 1)}</select></label>
        <label>Предмет расчёта<select id="yk-subject">${opts(SUBJECT, c.payment_subject || "commodity")}</select></label>
        <label>Способ расчёта<select id="yk-paymode">${opts(MODE, c.payment_mode || "full_prepayment")}</select></label>
      </div>
      <div class="note" style="margin-top:8px">Адрес HTTP-уведомлений в кабинете ЮKassa менять не нужно: статус оплаты Pricer спрашивает у ЮKassa сам.</div>
      ${st.message ? `<div class="${st.ok ? "note" : "acc-msg"}" style="margin-top:6px">${esc(st.message)}</div>` : ""}
      <div class="row" style="margin-top:10px"><button>Сохранить</button><button type="button" class="secondary" data-check>Проверить подключение</button><span class="msg"></span></div></form>`;
  }
  function yooForm() {
    return {
      config: {enabled: $("yk-enabled").checked, test_mode: $("yk-mode").value === "test", shop_id: $("yk-shop").value.trim(),
        test_shop_id: $("yk-test-shop").value.trim(), receipts: $("yk-receipts").checked, tax_system_code: Number($("yk-tax").value),
        vat_code: Number($("yk-vat").value), payment_subject: $("yk-subject").value, payment_mode: $("yk-paymode").value},
      secrets: secretValues({secret_key: "yk-secret", test_secret_key: "yk-test-secret"}),
    };
  }
  function laximoCard(acc) {
    const st = (acc && acc.status) || {};
    if (!acc) return `<div class="card svc"><h3>Каталог Laximo</h3><div class="note">Подбор по VIN, номеру кузова и госномеру, схемы узлов, «ТО по машине».</div>
      <div class="row" style="margin-top:10px"><button data-connect="laximo">Подключить</button></div></div>`;
    return `<form class="card svc" id="svc-laximo"><div class="row" style="justify-content:space-between"><h3>Каталог Laximo</h3>${status(acc)}</div>
      <label class="check"><input type="checkbox" id="lx-enabled" ${acc.config.enabled === false ? "" : "checked"}> Включён</label>
      ${secretInput(acc, "login", "lx-login", "Логин")}${secretInput(acc, "password", "lx-password", "Пароль")}
      ${st.message ? `<div class="${st.ok ? "note" : "acc-msg"}" style="margin-top:6px">${esc(st.message)}</div>` : ""}
      <div class="row" style="margin-top:10px"><button>Сохранить</button><button type="button" class="secondary" data-check>Проверить подключение</button><span class="msg"></span></div></form>`;
  }
  function bind(section, acc, collect) {
    const connect = document.querySelector(`[data-connect="${section}"]`);
    if (connect) connect.onclick = async () => { await api("/api/suppliers", {method: "POST", body: {section}}); loadServices(); };
    const form = $("svc-" + section);
    if (!form) return;
    const msg = form.querySelector(".msg");
    form.onsubmit = async (e) => {
      e.preventDefault();
      try { await api(`/api/suppliers/${acc.id}`, {method: "PUT", body: collect()}); msg.className = "msg ok"; msg.textContent = "Сохранено"; setTimeout(loadServices, 800); }
      catch (err) { msg.className = "msg"; msg.textContent = err.message; }
    };
    form.querySelector("[data-check]").onclick = async (e) => {
      e.target.disabled = true; e.target.textContent = "Проверяю…";
      try { await api(`/api/services/${section}/check`, {method: "POST"}); } catch (err) { msg.className = "msg"; msg.textContent = err.message; }
      loadServices();
    };
  }

  // ---------- прайс-листы ----------
  const P = {data: null, q: "", filter: ""};
  const STATE = {ok: ["загружен", "ok"], warn: ["давно не обновлялся", "warn"], stale: ["устарел — не в выдаче", "bad"],
    bad: ["ошибка", "bad"], new: ["ещё не загружался", "muted"], off: ["выключен", "muted"]};
  const HOURS = (h) => h % 24 === 0 ? (h === 24 ? "раз в сутки" : `раз в ${h / 24} дн.`) : (h === 1 ? "каждый час" : `каждые ${h} ч`);

  window.loadPrices = async function () {
    try { P.data = await api("/api/prices"); } catch (err) { $("prices-box").innerHTML = `<p class="msg">${esc(err.message)}</p>`; return; }
    renderPrices();
  };
  function renderPrices() {
    const d = P.data, open = new Set([...document.querySelectorAll("#prices-list details[open]")].map((x) => x.dataset.id));
    const q = P.q.toLowerCase();
    const list = d.sources.filter((src) => (!q || `${src.name} ${src.location_hint}`.toLowerCase().includes(q)) && (!P.filter || src.state === P.filter
      || (P.filter === "bad" && ["bad", "stale"].includes(src.state))));
    const rows = d.sources.reduce((n, src) => n + ((src.status || {}).rows || 0), 0);
    $("prices-box").innerHTML = `<div class="row acc-tools">
        <input id="pr-q" placeholder="Поиск прайса" value="${esc(P.q)}" style="flex:1 1 220px">
        <span class="seg" id="pr-filter">${[["", "Все"], ["ok", "Загружены"], ["bad", "Ошибки"], ["off", "Выключены"]].map(([v, t]) => `<button data-pf="${v}" class="${P.filter === v ? "on" : ""}">${t}</button>`).join("")}</span>
        <button class="secondary small" id="pr-add">+ Прайс по ссылке или FTP</button>
        <span class="note">${d.sources.length} прайсов · ${rows.toLocaleString("ru-RU")} строк в поиске</span>
      </div>
      <p class="note">Прайс не обновлялся ${d.warn_days} дня — предупреждение; дольше ${d.stale_days} дней — в выдачу не попадает.
        Сервер загружает прайсы сам по расписанию каждого.</p>
      <div id="prices-list" class="acc-list"></div>`;
    for (const src of list) $("prices-list").appendChild(priceRow(src, open.has(String(src.id))));
    if (!d.sources.length) $("prices-list").innerHTML = '<p class="note">Прайсов нет. Добавьте по ссылке или FTP — или импортируйте settings.json десктопа во вкладке «Поставщики».</p>';
    $("pr-q").oninput = (e) => { P.q = e.target.value; renderPrices(); const el = $("pr-q"); el.focus(); el.setSelectionRange(P.q.length, P.q.length); };
    document.querySelectorAll("#pr-filter [data-pf]").forEach((b) => b.onclick = () => { P.filter = b.dataset.pf; renderPrices(); });
    $("pr-add").onclick = async () => {
      const src = await api("/api/prices", {method: "POST", body: {name: "Новый прайс"}});
      P.data.sources.unshift(src); renderPrices();
      const el = document.querySelector(`#prices-list details[data-id="${src.id}"]`); if (el) el.open = true;
    };
  }
  function priceRow(src, open) {
    const st = src.status || {}, [label, cls] = STATE[src.state] || ["", "muted"], set = src.settings || {};
    const det = document.createElement("details"); det.className = "acc"; det.dataset.id = src.id; det.open = open;
    const when = src.loaded_at ? dt(src.loaded_at) : "";
    det.innerHTML = `<summary>
        <label class="switch" title="Включить или выключить"><input type="checkbox" data-en ${src.enabled ? "checked" : ""}><span></span></label>
        <span class="acc-name"><strong>${esc(src.name)}</strong> <span class="note">${esc(src.location_hint || "адрес не указан")} · ${HOURS(src.schedule_hours)}</span>
          ${st.ok === false && st.message ? `<span class="acc-msg">${esc(st.message)}</span>` : ""}</span>
        <span class="st ${cls}" title="${esc(st.message || "")}">${esc(label)}${src.state === "ok" || src.state === "warn" ? ` · ${(st.rows || 0).toLocaleString("ru-RU")} строк · ${when}` : ""}</span>
        <button type="button" class="secondary small" data-load>Загрузить</button>
      </summary>
      <form class="card acc-body">
        <label>Название в выдаче<input data-f="name" value="${esc(src.name)}"></label>
        <label>Адрес (https://… или ftp://логин:пароль@сервер/путь/файл)
          <input data-f="location" placeholder="${src.has_location ? "сохранён — оставьте пустым, чтобы не менять" : "https://…/price.csv"}" autocomplete="off"></label>
        <label>Обновлять<select data-f="schedule_hours">${P.data.schedules.map((h) => `<option value="${h}"${h === src.schedule_hours ? " selected" : ""}>${HOURS(h)}</option>`).join("")}</select></label>
        <label>Срок поставки, если в прайсе нет (дней)<input data-s="default_days" value="${esc(set.default_days ?? "")}" placeholder="0"></label>
        <label>Склад, если в прайсе нет<input data-s="warehouse" value="${esc(set.warehouse || "")}"></label>
        <label>Первая строка<select data-s="has_header">${[["auto", "определить само"], ["true", "заголовки колонок"], ["false", "сразу данные"]].map(([v, t]) =>
          `<option value="${v}"${String(set.has_header ?? "auto") === v ? " selected" : ""}>${t}</option>`).join("")}</select></label>
        <label>Поиск на сайте поставщика (ссылка с {article})<input data-s="site_search_url" value="${esc(set.site_search_url || "")}" placeholder="https://site.ru/search?q={article}"></label>
        ${st.reasons && Object.keys(st.reasons).length ? `<div class="note">Последняя загрузка (${esc(dt(st.at))}): принято ${st.rows}, пропущено ${st.skipped}: ${Object.entries(st.reasons).map(([k, v]) => `${esc(k)} — ${v}`).join(", ")}</div>` : ""}
        <div class="row"><button>Сохранить</button><button type="button" class="secondary" data-preview>Предпросмотр и колонки</button>
          <button type="button" class="danger" data-del>Удалить</button><span class="msg"></span></div>
        <div data-preview-box></div>
      </form>`;
    const form = det.querySelector("form"), msg = det.querySelector(".msg");
    det.querySelector("[data-en]").onclick = (e) => e.stopPropagation();
    det.querySelector("[data-en]").onchange = (e) => save(src, {enabled: e.target.checked});
    det.querySelector("[data-load]").onclick = (e) => { e.preventDefault(); loadNow(src, e.target); };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const body = {name: form.querySelector('[data-f="name"]').value, schedule_hours: Number(form.querySelector('[data-f="schedule_hours"]').value), settings: {}};
      const loc = form.querySelector('[data-f="location"]').value.trim(); if (loc) body.location = loc;
      form.querySelectorAll("[data-s]").forEach((el) => { body.settings[el.dataset.s] = el.dataset.s === "has_header" ? ({auto: "auto", true: true, false: false})[el.value] : el.value.trim(); });
      try { await save(src, body); msg.className = "msg ok"; msg.textContent = "Сохранено"; } catch (err) { msg.className = "msg"; msg.textContent = err.message; }
    };
    det.querySelector("[data-del]").onclick = async () => {
      if (!confirm(`Удалить прайс «${src.name}» и все его строки?`)) return;
      await api(`/api/prices/${src.id}`, {method: "DELETE"}); P.data.sources = P.data.sources.filter((x) => x.id !== src.id); renderPrices();
    };
    det.querySelector("[data-preview]").onclick = () => showPreview(src, det.querySelector("[data-preview-box]"), msg);
    return det;
  }
  async function save(src, body) {
    const fresh = await api(`/api/prices/${src.id}`, {method: "PUT", body});
    P.data.sources = P.data.sources.map((x) => x.id === src.id ? fresh : x); renderPrices();
    return fresh;
  }
  async function loadNow(src, button) {
    button.disabled = true; button.textContent = "Загружаю…";
    try {
      const r = await api(`/api/prices/${src.id}/load`, {method: "POST"});
      if (r.queued) button.textContent = "В очереди…";
      await listenJob(r.job_id, {});
    } catch (err) {}
    loadPrices();
  }
  async function showPreview(src, box, msg) {
    box.innerHTML = '<p class="note">Скачиваю файл…</p>';
    let pv;
    try { pv = await api(`/api/prices/${src.id}/preview`, {method: "POST"}); } catch (err) { box.innerHTML = `<p class="msg">${esc(err.message)}</p>`; return; }
    const mapping = {...pv.guess, ...pv.mapping}, byHeader = {};
    for (const [field, header] of Object.entries(mapping)) byHeader[header] = field;
    const fields = P.data.fields;
    box.innerHTML = `<p class="note">Строк в файле: ${pv.total.toLocaleString("ru-RU")}. Укажите, где какое поле (артикул и цена обязательны) — подобрано по заголовкам.</p>
      <div class="table-wrap"><table><thead><tr>${pv.headers.map((h) => `<th><select data-col="${esc(h)}"><option value="">—</option>${fields.map((f) =>
        `<option value="${f.code}"${byHeader[h] === f.code ? " selected" : ""}>${esc(f.label)}${f.required ? " *" : ""}</option>`).join("")}</select><div class="note">${esc(h)}</div></th>`).join("")}</tr></thead>
        <tbody>${pv.rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
      <div class="row" style="margin-top:8px"><button type="button" data-save-map>Сохранить колонки и загрузить</button></div>`;
    box.querySelector("[data-save-map]").onclick = async () => {
      const column_map = {};
      box.querySelectorAll("[data-col]").forEach((sel) => { if (sel.value) column_map[sel.value] = sel.dataset.col; });
      if (!column_map.article || !column_map.price) { msg.className = "msg"; msg.textContent = "Нужны хотя бы «Артикул» и «Цена»"; return; }
      try { await save(src, {settings: {column_map}}); const btn = document.querySelector(`#prices-list details[data-id="${src.id}"] [data-load]`); if (btn) loadNow(src, btn); }
      catch (err) { msg.className = "msg"; msg.textContent = err.message; }
    };
  }
  const dt = (v) => v ? String(v).replace("T", " ").slice(0, 16) : "";
})();
