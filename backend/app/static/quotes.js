// Подбор для клиента (сторона менеджера): варианты из выдачи кнопкой 📋 попадают в активный подбор,
// ссылка уходит клиенту (WhatsApp, Telegram, копирование), выбор клиента — уведомлением, затем заказ.
(() => {
  const Q = {active: null, list: [], current: null};
  const ACTIVE_KEY = "pricer.quote.active";
  try { Q.active = Number(localStorage.getItem(ACTIVE_KEY)) || null; } catch (e) {}
  const dt = (v) => v ? v.replace("T", " ").slice(0, 16) : "";
  const absUrl = (q) => location.origin + q.url;
  const STATUS_CLS = {draft: "muted", sent: "warn", viewed: "warn", chosen: "ok", ordered: "ok", cancelled: "muted"};

  function setActive(quote) {
    Q.active = quote ? quote.id : null;
    try { Q.active ? localStorage.setItem(ACTIVE_KEY, String(Q.active)) : localStorage.removeItem(ACTIVE_KEY); } catch (e) {}
    $("quote-badge").classList.toggle("hidden", !quote);
    if (quote) { $("quote-count").textContent = quote.variants; $("quote-badge").title = `Активный подбор «${quote.title}»: ${quote.positions} поз., ${quote.variants} вар.`; }
  }
  async function refreshBadge() {
    if (!Q.active) return setActive(null);
    try { setActive(await api(`/api/quotes/${Q.active}`)); } catch (e) { setActive(null); }
  }
  $("quote-badge").onclick = () => { document.querySelector('[data-tab="quotes"]').click(); if (Q.active) openQuote(Q.active); };

  // ---------- из выдачи: «📋 В подбор» ----------
  window.addToQuote = async function (jobId, offerId, qty, button) {
    try {
      if (!Q.active) setActive(await api("/api/quotes", {method: "POST", body: {}}));
      const quote = await api(`/api/quotes/${Q.active}/variants`, {method: "POST", body: {job_id: jobId, internal_offer_id: offerId, qty}});
      setActive(quote);
      if (button) { button.textContent = "✓"; setTimeout(() => button.textContent = "📋", 1200); }
      const msg = $("find-msg"); msg.className = "msg ok";
      msg.innerHTML = `Добавлено в подбор «${esc(quote.title)}» (${quote.variants} вар.) · <button class="link" id="q-open-active">открыть подбор</button>`;
      $("q-open-active").onclick = () => $("quote-badge").click();
    } catch (err) {
      if (/не найден/.test(err.message) && Q.active) { setActive(null); return addToQuote(jobId, offerId, qty, button); }
      $("find-msg").className = "msg"; $("find-msg").textContent = "Подбор: " + err.message;
    }
  };

  // ---------- вкладка «Подборы» ----------
  window.loadQuotes = async function () {
    try { Q.list = await api("/api/quotes"); } catch (e) { Q.list = []; }
    renderList();
    if (Q.current) openQuote(Q.current.id); else if (Q.active) openQuote(Q.active);
  };
  function renderList() {
    const q = $("q-search").value.trim().toLowerCase();
    const rows = Q.list.filter((x) => !q || `${x.title} ${x.client}`.toLowerCase().includes(q));
    $("q-list").innerHTML = rows.map((x) => `<div class="q-item${Q.current && Q.current.id === x.id ? " on" : ""}" data-q="${x.id}">
        <div class="row" style="justify-content:space-between"><strong>${esc(x.title)}</strong>${x.id === Q.active ? '<span class="note">📋 активный</span>' : ""}</div>
        <div class="note">${esc(x.client || "без клиента")} · ${x.positions} поз. · ${dt(x.created_at)}</div>
        <span class="st ${STATUS_CLS[x.status] || "muted"}">${esc(x.status_label)}</span></div>`).join("")
      || '<p class="note">Подборов пока нет.</p>';
    document.querySelectorAll("[data-q]").forEach((el) => el.onclick = () => openQuote(Number(el.dataset.q)));
  }
  $("q-search").addEventListener("input", renderList);
  $("q-new").onclick = async () => {
    const quote = await api("/api/quotes", {method: "POST", body: {}});
    setActive(quote); Q.current = quote; await loadQuotes();
  };

  async function openQuote(id) {
    try { Q.current = await api(`/api/quotes/${id}`); } catch (err) { $("q-editor").innerHTML = `<p class="msg">${esc(err.message)}</p>`; return; }
    renderList(); renderEditor();
  }
  function renderEditor() {
    const q = Q.current, sent = q.status !== "draft";
    const share = encodeURIComponent(`${q.title}: варианты запчастей — выберите подходящие ${absUrl(q)}`);
    $("q-editor").innerHTML = `
      <div class="row" style="justify-content:space-between">
        <input id="qe-title" value="${esc(q.title)}" style="flex:1 1 260px;font-weight:600">
        <span class="st ${STATUS_CLS[q.status] || "muted"}">${esc(q.status_label)}</span>
      </div>
      <div class="row" style="margin-top:8px">
        <input id="qe-client" placeholder="Клиент" value="${esc(q.client)}" style="flex:1 1 180px">
        <input id="qe-phone" placeholder="Телефон" style="flex:0 1 160px">
        <button class="secondary small" id="qe-save">Сохранить</button>
        ${q.id === Q.active ? '<span class="note">📋 сюда добавляются варианты из поиска</span>' : '<button class="secondary small" id="qe-activate">Добавлять сюда из поиска</button>'}
      </div>
      <div class="note" style="margin-top:6px">${[q.sent_at && "отправлен " + dt(q.sent_at), q.expires_at && "цены до " + dt(q.expires_at) + (q.expired ? " (истекли)" : ""),
        q.viewed_at && "открыт " + dt(q.viewed_at), q.chosen_at && "выбран " + dt(q.chosen_at), q.order_id && "заказ " + q.order_id].filter(Boolean).join(" · ")}</div>
      ${q.client_comment ? `<div class="banner-ok" style="margin-top:8px">Комментарий клиента: ${esc(q.client_comment)}${q.contact ? " · " + esc(q.contact) : ""}</div>` : ""}
      ${q.lines.map((line) => `<div class="q-line" data-line="${line.id}">
          <div class="row"><input data-req value="${esc(line.request)}" style="flex:1 1 220px" title="Как увидит клиент: «Фильтр масляный», «Колодки передние»">
            <input data-qty type="number" min="1" value="${line.qty}" style="width:70px"> шт.
            <button class="link" data-del-line>убрать позицию</button>
            ${line.choice === "skip" ? '<span class="st muted">клиенту не нужно</span>' : ""}</div>
          ${line.variants.map((v) => `<div class="q-var${line.choice === v.key ? " chosen" : ""}">
              <span>${line.choice === v.key ? "✓" : ""}</span>
              <span><strong>${esc(v.brand)} ${esc(v.article)}</strong>${v.is_cross ? '<span class="an">АН</span>' : ""} <span class="note">${esc(v.name || "")}</span>
                <div class="note purchase">${esc(v.provider || "")} ${esc(v.warehouse || "")}${v.returnable === false ? ' · <span class="st bad">без возврата</span>' : ""}</div></span>
              <span class="q-term note">${days(v.delivery_hours)}</span>
              <span class="q-pur note purchase num">${money(v.purchase_price)}</span>
              <span class="num"><strong>${money(v.sale_price)} ₽</strong></span>
              <button class="link" data-del-var="${esc(v.key)}" title="Убрать вариант">✕</button></div>`).join("")
            || '<div class="note">Вариантов нет — найдите деталь в поиске и нажмите 📋.</div>'}
        </div>`).join("") || '<p class="note" style="margin-top:12px">Пусто. Найдите деталь на вкладке «Поиск» и добавьте варианты кнопкой 📋 — каждый поиск станет позицией подбора.</p>'}
      <div class="row" style="margin-top:12px">
        <button class="secondary" id="qe-line">+ позиция вручную</button>
        <select id="qe-hours" title="Сколько действуют цены"><option value="24">цены на 24 часа</option><option value="48">на 2 дня</option><option value="72">на 3 дня</option><option value="168">на неделю</option></select>
        <button id="qe-send">${sent ? "Обновить срок и ссылку" : "Отправить клиенту"}</button>
        ${q.status === "chosen" && !q.order_id ? '<button id="qe-order">Оформить заказ по выбору</button>' : ""}
        <button class="danger" id="qe-delete">Удалить</button>
        <span class="msg" id="qe-msg"></span>
      </div>
      ${sent ? `<div class="q-share">
          <input id="qe-url" readonly value="${esc(absUrl(q))}">
          <button class="secondary small" id="qe-copy">Копировать</button>
          <a class="cartbtn" target="_blank" rel="noopener" href="https://wa.me/?text=${share}">WhatsApp</a>
          <a class="cartbtn" target="_blank" rel="noopener" href="https://t.me/share/url?url=${encodeURIComponent(absUrl(q))}&text=${encodeURIComponent(q.title)}">Telegram</a>
          <a class="cartbtn" target="_blank" rel="noopener" href="${esc(q.url)}">Как видит клиент</a></div>` : ""}`;
    bindEditor();
  }
  function bindEditor() {
    const q = Q.current, msg = $("qe-msg");
    const run = async (fn, okText) => {
      try {
        const r = await fn();
        if (r && r.lines) {
          Q.current = r;
          const i = Q.list.findIndex((x) => x.id === r.id);  // строка списка слева — тоже свежая
          if (i >= 0) Q.list[i] = {...Q.list[i], ...r}; else Q.list.unshift(r);
        }
        renderEditor(); renderList(); if (okText) { $("qe-msg").className = "msg ok"; $("qe-msg").textContent = okText; } refreshBadge(); }
      catch (err) { msg.className = "msg"; msg.textContent = err.message; }
    };
    $("qe-save").onclick = () => run(() => api(`/api/quotes/${q.id}`, {method: "PUT", body: {title: $("qe-title").value, client: $("qe-client").value, phone: $("qe-phone").value}}), "Сохранено");
    if ($("qe-activate")) $("qe-activate").onclick = () => { setActive(q); renderEditor(); renderList(); };
    $("qe-line").onclick = () => run(() => api(`/api/quotes/${q.id}/lines`, {method: "POST", body: {request: "Позиция", qty: 1}}));
    $("qe-send").onclick = () => run(() => api(`/api/quotes/${q.id}/send`, {method: "POST", body: {hours: Number($("qe-hours").value)}}), "Ссылка готова — отправьте её клиенту");
    $("qe-delete").onclick = () => { if (confirm(`Удалить подбор «${q.title}»?`)) run(async () => {
      await api(`/api/quotes/${q.id}`, {method: "DELETE"}); if (Q.active === q.id) setActive(null); Q.current = null;
      $("q-editor").innerHTML = '<p class="note">Подбор удалён.</p>'; await loadQuotes();
    }); };
    if ($("qe-order")) $("qe-order").onclick = async () => {
      try {
        const r = await api(`/api/quotes/${q.id}/order`, {method: "POST"});
        document.querySelector('[data-tab="orders"]').click(); openOrder(r.order.order_id);
      } catch (err) { msg.className = "msg"; msg.textContent = err.message; }
    };
    if ($("qe-copy")) $("qe-copy").onclick = async () => {
      try { await navigator.clipboard.writeText($("qe-url").value); } catch (e) { $("qe-url").select(); document.execCommand("copy"); }
      $("qe-copy").textContent = "Скопировано";
    };
    document.querySelectorAll("#q-editor [data-line]").forEach((el) => {
      const id = el.dataset.line;
      el.querySelector("[data-req]").onchange = (e) => run(() => api(`/api/quotes/${q.id}/lines/${id}`, {method: "PUT", body: {request: e.target.value}}));
      el.querySelector("[data-qty]").onchange = (e) => run(() => api(`/api/quotes/${q.id}/lines/${id}`, {method: "PUT", body: {qty: Number(e.target.value) || 1}}));
      el.querySelector("[data-del-line]").onclick = () => run(() => api(`/api/quotes/${q.id}/lines/${id}`, {method: "DELETE"}));
      el.querySelectorAll("[data-del-var]").forEach((b) => b.onclick = () =>
        run(() => api(`/api/quotes/${q.id}/lines/${id}/variants/${encodeURIComponent(b.dataset.delVar)}`, {method: "DELETE"})));
    });
  }

  // ---------- уведомления: клиент выбрал ----------
  let otherLast = null;
  window.checkOtherNotifications = async function (baseKey) {
    const key = baseKey + ".other";
    if (otherLast === null) { try { const v = localStorage.getItem(key); otherLast = v == null ? -1 : Number(v); } catch (e) { otherLast = -1; } }
    try {
      const r = await api(`/api/notifications/other?after=${otherLast}`);
      for (const n of r.items) if (n.kind === "quote_chosen") {
        showToast(`Клиент выбрал: ${n.title}`, `${n.client ? n.client + " · " : ""}выбрано ${n.chosen} из ${n.positions}${n.comment ? "\n«" + n.comment + "»" : ""}`, null,
          {label: "Открыть подбор", fn: () => { document.querySelector('[data-tab="quotes"]').click(); openQuote(n.quote_id); }});
      }
      otherLast = r.last_id;
      try { localStorage.setItem(key, String(otherLast)); } catch (e) {}
    } catch (e) {}
  };
  refreshBadge();
})();
