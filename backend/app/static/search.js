// Вкладка «Поиск»: как «Проценка» десктопа и выдача приложения Abcp.
// Артикул -> бренд голосованием поставщиков («ZIC · 6 из 12», «Уточнить бренд») -> выдача:
// три лучших предложения, группы «бренд + номер» (сам номер сверху, ниже аналоги), ★ подтверждений,
// 🏆 рейтинг и гарантия бренда, признак «без возврата», фильтры по брендам и поставщикам.
(() => {
  const S = {
    job: null, article: "", offers: [], highlights: {}, providers: [], states: {}, brands: null,
    sel: {brands: new Set(), providers: new Set()}, facetTab: {brands: "all", providers: "all"},
    facetSort: {brands: "alpha", providers: "alpha"}, facetQuery: {brands: "", providers: ""},
    fav: {brands: [], providers: []}, popular: {brands: [], providers: []}, warranty: {}, warrantyPage: "",
    expanded: new Set(), term: "", me: null, loaded: false, es: null,
  };
  const key = (v) => String(v || "").toUpperCase().replace(/[^A-ZА-ЯЁ0-9]/g, "");
  const hours = (o) => o.delivery_hours == null ? 1e12 : Number(o.delivery_hours);
  const price = (o) => Number(o.sale_price || o.purchase_price || 0) || 1e12;
  // Бренд для группировки — группа справочника брендов; подпись — как чаще пишут сами поставщики
  // (в справочнике десктопа у групп бывают служебные названия: «FILTRON - AĞIR VASıTA»).
  const brandKey = (o) => o.normalized_brand_key || key(o.display_brand || o.brand);
  let labels = new Map();
  function buildLabels() {
    const counts = new Map();
    for (const o of S.offers) {
      const k = brandKey(o), b = String(o.brand || o.display_brand || "").trim();
      if (!b) continue;
      if (!counts.has(k)) counts.set(k, new Map());
      counts.get(k).set(b, (counts.get(k).get(b) || 0) + 1);
    }
    labels = new Map([...counts].map(([k, m]) => [k, [...m].sort((a, b) => b[1] - a[1] || a[0].length - b[0].length)[0][0]]));
  }
  const brandOf = (o) => labels.get(brandKey(o)) || o.display_brand || o.brand || "";
  const articleOf = (o) => o.article || o.original_article || "";  // нормализованный: без косой черты и пробелов, как в десктопе и приложении
  const warrantyOf = (o) => S.warranty[key(o.brand)] || S.warranty[key(o.display_brand)] || null;
  const isFav = (kind, name) => S.fav[kind].includes(name);
  const customer = () => S.me && S.me.role === "customer";

  async function loadOnce() {
    if (S.loaded) return;
    S.loaded = true;
    const safe = (p) => p.catch(() => null);
    const [me, fav, pop, war] = await Promise.all([safe(api("/api/me")), safe(api("/api/me/favorites")),
      safe(api("/api/stats/popular")), safe(api("/api/brands/warranty"))]);
    S.me = me; if (fav) S.fav = fav; if (pop) S.popular = pop;
    if (war) { S.warranty = war.brands || {}; S.warrantyPage = war.page || ""; }
    document.body.classList.toggle("role-customer", customer());
  }

  // ---------- поиск ----------
  window.findArticle = async function (article, {brand = "", hint = ""} = {}) {
    article = String(article || "").trim();
    if (!article) return;
    await loadOnce();
    if (S.es) S.es.close();
    Object.assign(S, {article, offers: [], highlights: {}, states: {}, expanded: new Set(), allBrands: false});
    S.sel = {brands: new Set(), providers: new Set()};
    $("go").disabled = true; $("find-msg").textContent = ""; $("find-msg").className = "msg";
    $("count").textContent = "Ищу…"; $("highlights").innerHTML = ""; $("groups").innerHTML = "";
    $("results").classList.add("hidden");
    if (!brand) { $("brandbar").classList.add("hidden"); $("brand-choices").classList.add("hidden"); }
    const started = performance.now();
    try {
      const r = await api("/api/find", {method: "POST", body: {article, brand, brand_hint: hint}});
      S.job = r.job_id; S.providers = r.providers; lastSearchJob = r.job_id;
      renderChips(S.providers, S.states);
      const es = S.es = new EventSource(`/api/jobs/${r.job_id}/events`);
      const on = (kind, fn) => es.addEventListener(kind, (ev) => fn(ev.data ? JSON.parse(ev.data) : {}));
      on("provider", (d) => {
        S.states[d.provider] = {...(S.states[d.provider] || {}), status: d.status === "done" ? "ok" : d.status === "searching" ? "wait" : d.status};
        if (S.states[d.provider].status === "ok") S.states[d.provider].count = S.offers.filter((o) => o.provider === d.provider).length;
        renderChips(S.providers, S.states);
      });
      on("brands", renderBrandbar);
      on("need_brand", () => {
        es.close(); $("go").disabled = false; $("count").textContent = "";
        $("brand-choices").classList.remove("hidden");
        $("find-msg").textContent = "Поставщики называют разные бренды — выберите нужный";
      });
      on("partial", (d) => { if (d.stale) return; S.offers = d.offers; renderAll(); $("count").textContent = `Найдено ${d.total}, поиск продолжается…`; });
      on("done", async () => {
        es.close();
        let d;
        try { d = await api(`/api/find/${r.job_id}/results`); } catch (err) { $("go").disabled = false; $("count").textContent = "Ошибка: " + err.message; return; }
        $("go").disabled = false;
        S.offers = d.offers; S.highlights = d.highlights || {};
        if (!S.touchedReturn) $("only-return").checked = !!d.hide_no_return;
        renderAll(((performance.now() - started) / 1000).toFixed(1), d.total);
      });
      es.addEventListener("error", (ev) => {
        es.close(); $("go").disabled = false;
        $("count").textContent = ev.data ? "Ошибка: " + JSON.parse(ev.data).message : "Соединение прервано";
      });
    } catch (err) { $("go").disabled = false; $("count").textContent = ""; $("find-msg").textContent = err.message; }
  };

  // ---------- одна строка: артикул, госномер, VIN, FRAME ----------
  // Маски: VIN — 17 знаков без I, O, Q; госномер РФ — буква, 3 цифры, 2 буквы, регион из 2–3 цифр
  // (буквы — те, что есть и в кириллице, и в латинице); FRAME (японский номер кузова) — код модели
  // с буквами, дефис и 5–8 цифр (GX110-6012345). Остальное — артикул.
  const KINDS = [["article", "Поиск по артикулу"], ["plate", "Поиск по госномеру"], ["vin", "Поиск по VIN"], ["frame", "Поиск по FRAME (номер кузова)"]];
  const PLATE = "АВЕКМНОРСТУХABEKMHOPCTYX";
  window.detectKind = function (text) {
    const v = String(text || "").toUpperCase().replace(/\s+/g, "");
    if (/^[A-HJ-NPR-Z0-9]{17}$/.test(v) && /[A-Z]/.test(v) && /\d/.test(v)) return "vin";
    if (new RegExp(`^[${PLATE}]\\d{3}[${PLATE}]{2}\\d{2,3}$`).test(v)) return "plate";
    if (/^[A-Z]{1,5}\d{1,4}[A-Z]{0,2}-\d{5,8}$/.test(v)) return "frame";
    return "article";
  };
  let kind = "article", menuOpen = false;
  function showMenu() {
    const value = $("article").value.trim();
    let menu = $("kind-menu");
    if (!value) { if (menu) menu.remove(); menuOpen = false; return; }
    kind = detectKind(value);
    if (!menu) { menu = document.createElement("div"); menu.id = "kind-menu"; menu.className = "kind-menu"; $("form").appendChild(menu); }
    const order = [kind, ...KINDS.map((k) => k[0]).filter((k) => k !== kind)];
    menu.innerHTML = order.map((k) => `<button type="button" data-kind="${k}" class="${k === kind ? "on" : ""}">${KINDS.find((x) => x[0] === k)[1]}: <strong>${esc(value)}</strong></button>`).join("");
    menu.querySelectorAll("[data-kind]").forEach((b) => b.onmousedown = (e) => { e.preventDefault(); kind = b.dataset.kind; hideMenu(); run(); });
    menuOpen = true;
  }
  function hideMenu() { const m = $("kind-menu"); if (m) m.remove(); menuOpen = false; }
  function run() {
    const value = $("article").value.trim();
    if (!value) return;
    if (kind === "article") { findArticle(value); return; }
    // госномер, VIN, FRAME — подбор машины в каталоге Laximo (панель ниже строки поиска)
    $("vin-panel").open = true; $("vin-q").value = value; $("vin-go").click();
    $("vin-panel").scrollIntoView({behavior: "smooth", block: "start"});
  }
  $("article").addEventListener("input", showMenu);
  $("article").addEventListener("focus", () => { if ($("article").value.trim()) showMenu(); });
  $("article").addEventListener("blur", () => setTimeout(hideMenu, 150));
  $("article").addEventListener("keydown", (e) => { if (e.key === "Escape") hideMenu(); });
  $("form").addEventListener("submit", (e) => {
    e.preventDefault(); if (!menuOpen) kind = detectKind($("article").value); hideMenu(); run();
  });

  function renderBrandbar(d) {
    S.brands = d;
    $("brandbar").classList.remove("hidden");
    const sel = d.choices.find((c) => c.label === d.selected);
    $("brand-now").textContent = sel ? sel.brand : (d.choices.length ? "не выбран" : "не определён");
    $("brand-votes").textContent = sel ? `· ${sel.votes} из ${d.answered} поставщиков${d.auto ? ", выбран автоматически" : ""}` :
      (d.choices.length ? "" : "· поставщики бренд не назвали, ищем по номеру");
    $("brand-change").classList.toggle("hidden", d.choices.length < 2);
    const top = S.allBrands ? d.choices : d.choices.slice(0, 6);
    $("brand-choices").innerHTML = top.map((c) => `<button class="${c.label === d.selected ? "" : "secondary"}" data-brand="${esc(c.label)}"
        title="${esc(c.providers.join(", "))}${c.mentions ? ` · упоминаний в названиях: ${c.mentions}` : ""}">${esc(c.brand)}${c.name ? ` <span class="note">${esc(c.name)}</span>` : ""} · ${c.votes}</button>`).join("") +
      (d.choices.length > top.length ? `<button class="link" id="brand-more">ещё ${d.choices.length - top.length}</button>` : "");
    if ($("brand-more")) $("brand-more").onclick = () => { S.allBrands = true; renderBrandbar(d); };
    document.querySelectorAll("[data-brand]").forEach((b) => b.onclick = () => {
      $("brand-choices").classList.add("hidden"); findArticle(S.article, {brand: b.dataset.brand});
    });
  }
  $("brand-change").onclick = () => $("brand-choices").classList.toggle("hidden");

  // ---------- отбор и сортировка ----------
  const SORTS = {
    fast: (a, b) => hours(a) - hours(b) || (b.provider_confirm_count || 0) - (a.provider_confirm_count || 0) || price(a) - price(b),
    price: (a, b) => price(a) - price(b) || hours(a) - hours(b),
    qty: (a, b) => (b.available_quantity || 0) - (a.available_quantity || 0) || price(a) - price(b),
    purchase: (a, b) => (a.purchase_price || 1e12) - (b.purchase_price || 1e12) || hours(a) - hours(b),
    warehouse: (a, b) => String(a.warehouse || "").localeCompare(String(b.warehouse || ""), "ru") || price(a) - price(b),
  };
  function passes(o, skip) {
    if (S.term === "today" && hours(o) >= 24) return false;
    if (S.term === "3" && hours(o) > 72) return false;
    if ($("only-stock").checked && !(Number(o.available_quantity) > 0)) return false;
    if ($("only-return").checked && o.returnable === false) return false;
    if ($("only-warranty").checked && !warrantyOf(o)) return false;
    if (skip !== "brands" && S.sel.brands.size && !S.sel.brands.has(brandOf(o))) return false;
    if (skip !== "providers" && S.sel.providers.size && !S.sel.providers.has(o.provider)) return false;
    return true;
  }
  function groups(list) {
    const cmp = SORTS[$("sort").value] || SORTS.fast;
    const map = new Map();
    for (const o of list) {
      const k = brandKey(o) + "|" + key(o.article);
      if (!map.has(k)) map.set(k, []);
      map.get(k).push(o);
    }
    const favFirst = (g) => isFav("brands", brandOf(g[0])) ? 0 : 1;
    return [...map.values()].map((g) => g.sort(cmp)).sort((a, b) => favFirst(a) - favFirst(b) || cmp(a[0], b[0]));
  }

  // ---------- отрисовка ----------
  function renderAll(seconds, total) {
    $("results").classList.toggle("hidden", !S.offers.length);
    buildLabels();
    // число предложений у каждого поставщика — на плашках над выдачей
    const perProvider = {};
    for (const o of S.offers) if (o.provider) perProvider[o.provider] = (perProvider[o.provider] || 0) + 1;
    for (const [name, st] of Object.entries(S.states)) if (st.status === "ok") st.count = perProvider[name] || 0;
    renderChips(S.providers, S.states);
    const visible = S.offers.filter((o) => passes(o));
    const own = groups(visible.filter((o) => !o.is_cross)), analogs = groups(visible.filter((o) => o.is_cross));
    renderHighlights();
    let html = "";
    if (own.length) html += `<h3 class="sect">Вы искали</h3>` + own.map(groupCard).join("");
    else if (S.offers.length) html += `<p class="note">${S.offers.some((o) => !o.is_cross) ? "По самому номеру с этими условиями предложений нет" : "Самого номера сейчас нет в продаже — ниже аналоги"}</p>`;
    if (analogs.length) html += `<h3 class="sect">Аналоги · ${analogs.length} арт., ${analogs.reduce((n, g) => n + g.length, 0)} предл.</h3>` + analogs.map(groupCard).join("");
    $("groups").innerHTML = html || (S.offers.length ? '<p class="note">По фильтрам ничего не осталось — снимите часть условий.</p>' : "");
    if (seconds != null) $("count").textContent = `Предложений: ${total}${total > S.offers.length ? `, показаны ${S.offers.length}` : ""}, после фильтров ${visible.length} · ${seconds} с`;
    renderFacet("brands", "Бренды", brandOf);
    renderFacet("providers", "Поставщики", (o) => o.provider);
    bindRows($("groups"));
  }

  function renderHighlights() {
    const cards = [["cheapest", "Самая низкая цена"], ["cheapest_analog", "Самый дешёвый аналог"], ["fastest", "Лучший срок"]];
    $("highlights").innerHTML = cards.filter(([k]) => S.highlights[k] != null && S.offers[S.highlights[k]]).map(([k, title]) => {
      const o = S.offers[S.highlights[k]];
      return `<div class="hl"><div class="hl-title">${title}</div>
        <div class="hl-part"><strong>${esc(brandOf(o))} ${esc(articleOf(o))}</strong>${stars(o)}</div>
        <div class="note hl-name">${esc(o.name || "")}</div>
        <div class="hl-price">${money(o.sale_price)} ₽ <span class="note purchase">закупка ${money(o.purchase_price)}</span></div>
        <div class="note">${days(o.delivery_hours)} · в наличии ${o.available_quantity ?? "—"}${noReturn(o)} <span class="purchase">· ${esc(o.provider || "")}</span></div>
        ${buyControls(o)}</div>`;
    }).join("");
    bindRows($("highlights"));
  }

  const stars = (o) => (o.provider_confirm_count || 0) >= 2
    ? ` <span class="star" title="Номер подтвердили ${o.provider_confirm_count} поставщика(ов) — надёжнее единичного совпадения">★${o.provider_confirm_count}</span>` : "";
  const noReturn = (o) => o.returnable === false ? ' <span class="st bad" title="Поставщик не принимает возврат">без возврата</span>' : "";
  function cups(rating) {
    const full = Math.max(0, Math.min(5, Math.round(rating || 0)));
    return `<span class="cups" title="Рейтинг бренда ${Number(rating || 0).toFixed(1)} из 5">${[0, 1, 2, 3, 4].map((i) => `<span class="${i < full ? "" : "dim"}">🏆</span>`).join("")}</span>`;
  }
  function buyControls(o) {
    const min = Number(o.minimum_quantity) || 1, step = Number(o.quantity_step) || 1;
    const value = Number(o.actual_order_quantity) || min;
    if (!o.internal_offer_id) return "";
    return `<span class="buy"><input type="number" min="${min}" step="${step}" value="${value}" data-qty="${esc(o.internal_offer_id)}" aria-label="Количество">
      <button class="secondary cart-add" data-add="${esc(o.internal_offer_id)}" title="В корзину"${o.can_order_quantity === false ? " disabled" : ""}>🛒</button>
      <button class="secondary quote-add" data-quote="${esc(o.internal_offer_id)}" title="В подбор для клиента">📋</button></span>`;
  }

  function groupCard(list) {
    const head = list[0], w = warrantyOf(head);
    const k = brandKey(head) + "|" + key(head.article);
    const shown = S.expanded.has(k) ? list : list.slice(0, 3);
    const q = encodeURIComponent([brandOf(head), articleOf(head), head.name].filter(Boolean).join(" "));
    const img = list.find((o) => o.image_url);
    return `<div class="group">
      <div class="g-head">
        <div><strong>${esc(brandOf(head))}</strong> <button class="link" data-find="${esc(articleOf(head))}" title="Искать этот номер">${esc(articleOf(head))}</button>${stars(head)}
          ${isFav("brands", brandOf(head)) ? '<span class="note" title="Избранный бренд">♥</span>' : ""}
          <div class="note">${esc(head.name || "")}</div></div>
        <div class="g-side">
          ${w ? `<button class="wbadge" data-warranty="${esc(key(head.brand))}">${esc(w.warranty)}*</button>${cups(w.rating)}` : ""}
          <a class="imgbtn" target="_blank" rel="noopener" title="${img ? "Фото поставщика" : "Картинки в Яндексе"}" href="${img ? esc(img.image_url) : `https://yandex.ru/images/search?text=${q}`}">${img ? "Ф" : "Я"}</a>
          <a class="imgbtn" target="_blank" rel="noopener" title="Картинки в Google" href="https://www.google.com/search?tbm=isch&q=${q}">G</a>
        </div>
      </div>
      <div class="offers">${shown.map((o) => `<div class="offer${o.internal_offer_id && Object.values(S.highlights).some((i) => S.offers[i] === o) ? " best" : ""}">
          <span class="o-prov purchase">${esc(o.provider || "")}${typeof relBadge === "function" ? relBadge(o.provider) : ""}<span class="note"> ${esc(o.warehouse || "")}</span></span>
          <span class="o-qty" title="В наличии" data-term=" · ${esc(days(o.delivery_hours))}">${o.available_quantity ?? "—"}${o.availability_is_lower_bound ? "+" : ""} шт</span>
          <span class="o-term">${days(o.delivery_hours)}</span>
          <span class="o-ret">${noReturn(o)}</span>
          <span class="o-pur purchase num">${money(o.purchase_price)}</span>
          <span class="o-price num"><strong>${money(o.sale_price)} ₽</strong><span class="m-only">${noReturn(o)}</span></span>
          ${buyControls(o)}</div>`).join("")}
      </div>
      ${list.length > 3 ? `<button class="link more" data-more="${esc(k)}">${S.expanded.has(k) ? "Свернуть" : `Показать ещё ${list.length - 3}`}</button>` : ""}
    </div>`;
  }

  function bindRows(root) {
    root.querySelectorAll("[data-add]").forEach((b) => b.onclick = () => addToCart(b.dataset.add, b, root));
    root.querySelectorAll("[data-quote]").forEach((b) => b.onclick = () => {
      const input = root.querySelector(`[data-qty="${CSS.escape(b.dataset.quote)}"]`);
      addToQuote(S.job, b.dataset.quote, Number(input && input.value) || 1, b);
    });
    root.querySelectorAll("[data-more]").forEach((b) => b.onclick = () => {
      S.expanded.has(b.dataset.more) ? S.expanded.delete(b.dataset.more) : S.expanded.add(b.dataset.more); renderAll();
    });
    root.querySelectorAll("[data-find]").forEach((b) => b.onclick = () => { $("article").value = b.dataset.find; findArticle(b.dataset.find); });
    root.querySelectorAll("[data-warranty]").forEach((b) => b.onclick = () => showWarranty(S.warranty[b.dataset.warranty]));
  }

  async function addToCart(offerId, button, root) {
    const input = root.querySelector(`[data-qty="${CSS.escape(offerId)}"]`);
    const qty = Number(input && input.value) || 1;
    try {
      const cart = await api("/api/cart", {method: "POST", body: {job_id: S.job, internal_offer_id: offerId, quantity: qty}});
      renderCartBadge(cart); button.textContent = "✓"; setTimeout(() => button.textContent = "🛒", 1200);
      $("find-msg").className = "msg ok"; $("find-msg").textContent = cart.message || "Добавлено в корзину";
    } catch (err) { $("find-msg").className = "msg"; $("find-msg").textContent = "Корзина: " + err.message; }
  }

  function showWarranty(w) {
    if (!w) return;
    let dlg = $("warranty-dlg");
    if (!dlg) { dlg = document.createElement("dialog"); dlg.id = "warranty-dlg"; document.body.appendChild(dlg); }
    dlg.innerHTML = `<h3 style="margin-top:0">${esc(w.name)}</h3><div>🛡 ${esc(w.warranty)}</div><div>${cups(w.rating)} <span class="note">рейтинг ${Number(w.rating).toFixed(1)} из 5</span></div>
      ${(w.conditions || []).map((c) => `<hr><strong>${esc(c.term)}</strong><div>${esc(c.text)}</div>${c.url ? `<a class="link" target="_blank" rel="noopener" href="${esc(c.url)}">Условия производителя ↗</a>` : ""}`).join("")}
      ${S.warrantyPage ? `<hr><a class="link" target="_blank" rel="noopener" href="${esc(S.warrantyPage)}">Как воспользоваться гарантией ↗</a>` : ""}
      <div style="margin-top:12px"><button id="warranty-close">Закрыть</button></div>`;
    dlg.querySelector("#warranty-close").onclick = () => dlg.close();
    dlg.showModal();
  }

  // ---------- фильтры: бренды и поставщики ----------
  function renderFacet(kind, title, nameOf) {
    const box = $(kind === "brands" ? "f-brands" : "f-providers");
    const base = S.offers.filter((o) => passes(o, kind));
    const stats = new Map();
    for (const o of base) {
      const n = nameOf(o); if (!n) continue;
      const p = price(o), s = stats.get(n);
      if (!s) stats.set(n, {name: n, min: p, count: 1}); else { s.min = Math.min(s.min, p); s.count++; }
    }
    const tab = S.facetTab[kind], query = S.facetQuery[kind].toLowerCase();
    let items = [...stats.values()];
    if (tab === "fav") items = items.filter((i) => isFav(kind, i.name));
    if (tab === "pop") {
      const rank = new Map((S.popular[kind] || []).map((p, i) => [key(p.name), i]));
      items = items.filter((i) => rank.has(key(i.name))).sort((a, b) => rank.get(key(a.name)) - rank.get(key(b.name)));
    } else if (S.facetSort[kind] === "price") items.sort((a, b) => a.min - b.min);
    else items.sort((a, b) => a.name.localeCompare(b.name, "ru"));
    if (query) items = items.filter((i) => i.name.toLowerCase().includes(query));
    const sel = S.sel[kind];
    box.innerHTML = `<div class="facet"><div class="f-title">${title}</div>
      <div class="seg f-tabs">${[["all", "Все"], ["pop", "Популярные"], ["fav", "Избранные"]].map(([v, t]) => `<button data-ftab="${v}" class="${tab === v ? "on" : ""}">${t}</button>`).join("")}</div>
      <input class="f-q" placeholder="Поиск" value="${esc(S.facetQuery[kind])}">
      <div class="f-sort note">${tab === "pop" ? "по частоте заказов" : `<button class="link${S.facetSort[kind] === "alpha" ? " on" : ""}" data-fsort="alpha">По алфавиту</button> · <button class="link${S.facetSort[kind] === "price" ? " on" : ""}" data-fsort="price">По цене</button>`}</div>
      <div class="f-all"><button class="link" data-fall="1">✓ Выбрать все</button><button class="link" data-fall="0">✕ Снять все</button></div>
      <div class="f-list">${items.map((i) => `<label class="f-item"><input type="checkbox" data-fsel="${esc(i.name)}"${sel.has(i.name) ? " checked" : ""}>
          <span class="f-name">${esc(i.name)}</span><span class="note">от ${money(i.min === 1e12 ? null : i.min)} ₽</span>
          <button class="link f-fav" data-ffav="${esc(i.name)}" title="${isFav(kind, i.name) ? "Убрать из избранного" : "В избранное"}">${isFav(kind, i.name) ? "★" : "☆"}</button></label>`).join("")
        || `<div class="note">${tab === "fav" ? "Отметьте ☆ любимые — они будут здесь и первыми в выдаче" : tab === "pop" ? "Статистика появится после заказов" : "Нет"}</div>`}</div></div>`;
    box.querySelectorAll("[data-ftab]").forEach((b) => b.onclick = () => { S.facetTab[kind] = b.dataset.ftab; renderAll(); });
    box.querySelectorAll("[data-fsort]").forEach((b) => b.onclick = () => { S.facetSort[kind] = b.dataset.fsort; renderAll(); });
    box.querySelector(".f-q").oninput = (e) => { S.facetQuery[kind] = e.target.value; const pos = e.target.selectionStart; renderAll();
      const q = $(kind === "brands" ? "f-brands" : "f-providers").querySelector(".f-q"); q.focus(); q.setSelectionRange(pos, pos); };
    box.querySelectorAll("[data-fall]").forEach((b) => b.onclick = () => {
      S.sel[kind] = b.dataset.fall === "1" ? new Set(items.map((i) => i.name)) : new Set(); renderAll();
    });
    box.querySelectorAll("[data-fsel]").forEach((c) => c.onchange = () => { c.checked ? sel.add(c.dataset.fsel) : sel.delete(c.dataset.fsel); renderAll(); });
    box.querySelectorAll("[data-ffav]").forEach((b) => b.onclick = async (e) => {
      e.preventDefault();
      const list = new Set(S.fav[kind]); list.has(b.dataset.ffav) ? list.delete(b.dataset.ffav) : list.add(b.dataset.ffav);
      S.fav = {...S.fav, [kind]: [...list]};
      renderAll();
      try { S.fav = await api("/api/me/favorites", {method: "PUT", body: S.fav}); } catch (err) {}
    });
  }

  // ---------- панель инструментов ----------
  ["sort", "only-stock", "only-warranty"].forEach((id) => $(id).addEventListener("change", () => renderAll()));
  $("only-return").addEventListener("change", () => { S.touchedReturn = true; renderAll(); });
  document.querySelectorAll("#term [data-term]").forEach((b) => b.onclick = () => {
    S.term = b.dataset.term; document.querySelectorAll("#term [data-term]").forEach((x) => x.classList.toggle("on", x === b)); renderAll();
  });
  $("side-open").onclick = () => $("side").classList.add("open");
  $("side-close").onclick = () => $("side").classList.remove("open");
})();
