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

  // ---------- прайс-листы: пока сводка ----------
  window.loadPrices = async function () {
    try { accounts = await api("/api/suppliers"); } catch (e) { accounts = []; }
    const acc = accounts.find((a) => a.section === "url_csv");
    $("prices-box").innerHTML = `<div class="panel"><strong>Прайс-листы по ссылкам, FTP и почте</strong>
      <p class="note">${acc ? "Источники из десктопа перенесены импортом settings.json и хранятся зашифрованными." : "Источников пока нет — импортируйте settings.json десктопа или добавьте источник."}
      Список источников с расписанием, предпросмотром и разметкой колонок, загрузка на сервере и поиск по прайсам — следующий шаг, он уже в работе.</p></div>`;
  };
})();
