"""Одноразовый патч app/static/app.js для issue #5 (UI). Каждая замена обязана сработать ровно 1 раз."""
import sys
p = "app/static/app.js"
s = open(p, encoding="utf-8").read()


def rep(old, new):
    global s
    n = s.count(old)
    if n != 1:
        sys.exit(f"pattern found {n} times:\n{old[:200]}")
    s = s.replace(old, new)


# ---------- helpers: copy button, liquidity badge, action tag, bytes
rep("""  const time = (ts) => (ts ? String(ts).slice(11, 16) : "—");""",
"""  const time = (ts) => (ts ? String(ts).slice(11, 16) : "—");
  // Кнопка «скопировать тикер» (issue #5, п. 11) — обработчик делегирован на document
  const copyBtn = (t) => `<button class="copy-btn" data-copy="${esc(t)}" title="Скопировать ${esc(t)}" aria-label="Скопировать ${esc(t)}">⧉</button>`;
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch (e) { /* ниже — запасной путь */ }
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  }
  document.addEventListener("click", async (e) => {
    const b = e.target.closest && e.target.closest("[data-copy]");
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    const ok = await copyText(b.dataset.copy);
    toast(ok ? `Скопировано: ${b.dataset.copy}` : "Не удалось скопировать", !ok);
  }, true);
  // Низколиквидная бумага (issue #5, DeepSeek T2): метка рядом с тикером
  const liqBadge = (v) => {
    const min = +(S.settings && S.settings.values && S.settings.values.liquidity_min_score) || 0;
    if (v === null || v === undefined || !min || v >= min) return "";
    return ` <span class="tag warn" title="Ликвидность ${num(v, 2)} ниже порога ${num(min, 2)}: сигналы глушатся">низколикв.</span>`;
  };
  const ACTION_CLS = { buy: "up", plateau_buy: "up", rebound: "up", sell: "down", short: "down", exit: "down" };
  const fmtBytes = (b) => {
    if (b === null || b === undefined) return "—";
    const u = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let i = 0, v = +b;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return num(v, i ? 2 : 0) + " " + u[i];
  };""")

# ---------- status bar: пауза по диску
rep("""        c.warmup && c.status !== "closed" ?""",
"""        c.status === "disk_paused" ? `<span class="tag down" title="Мало места на диске — сканирование приостановлено. Админ → «Проверить память»">пауза: мало места на диске</span>` : "",
        c.warmup && c.status !== "closed" ?""")

# ---------- таблица рынка: тикер, ликвидность, действия
rep("""        <b>${esc(v)}</b>${r.triggered ? ' <span class="tag accent">триггер</span>' : ""}</td>`;""",
"""        <b>${esc(v)}</b>${copyBtn(v)}${r.triggered ? ' <span class="tag accent">триггер</span>' : ""}${liqBadge(r.liquidity_score)}</td>`;""")
rep("""      case "actions": return `<td>${paperBtn(r.ticker, r.paper)}</td>`;""",
"""      case "actions": return `<td class="actions-cell">${r.last_action_label
        ? `<span class="tag ${ACTION_CLS[r.last_action] || ""}" title="Последний сигнал по вашим порогам · ${esc(String(r.last_action_ts || "").slice(5, 16))}">${esc(r.last_action_label)}</span> `
        : '<span class="muted small">—</span> '}${paperBtn(r.ticker, r.paper)}</td>`;
      case "liquidity_score": return `<td class="num">${v === null || v === undefined ? "—" : num(v, 2)}${liqBadge(v)}</td>`;""")
rep("""      const k = S.sort.key, dir = S.sort.asc ? 1 : -1;
      rows = rows.slice().sort((a, b) => {""",
"""      const k = S.sort.key === "actions" ? "last_action_ts" : S.sort.key, dir = S.sort.asc ? 1 : -1;
      rows = rows.slice().sort((a, b) => {""")
rep("""      if (e.target.dataset.fav || e.target.closest("[data-paper-toggle]")) return;""",
"""      if (e.target.dataset.fav || e.target.closest("[data-paper-toggle]") || e.target.closest("[data-copy]")) return;""")

# ---------- карточка тикера: кнопка копирования
rep("""<span class="star ${r.favorite ? "on" : ""}" id="fav">★</span> ${esc(r.ticker)}</h2>""",
"""<span class="star ${r.favorite ? "on" : ""}" id="fav">★</span> ${esc(r.ticker)}${copyBtn(r.ticker)}</h2>${liqBadge(sec.liquidity_score)}""")

# ---------- сигналы: копирование и фильтр по времени
rep("""        ${withTicker ? `<td><a href="#ticker/${esc(a.ticker)}"><b>${esc(a.ticker)}</b></a>${""",
"""        ${withTicker ? `<td><a href="#ticker/${esc(a.ticker)}"><b>${esc(a.ticker)}</b></a>${a.ticker !== "РЫНОК" ? copyBtn(a.ticker) : ""}${""")
rep("""        <input id="s-day" type="date">""",
"""        <input id="s-day" type="date" title="День (пусто — сегодня)">
        <label class="small muted" title="Показать сигналы начиная с этого времени (МСК) выбранного дня">с <input id="s-time" type="time" style="width:100px"></label>""")
rep("""      if ($("#s-day").value) p.set("day", $("#s-day").value);""",
"""      if ($("#s-day").value) p.set("day", $("#s-day").value);
      if ($("#s-time").value) p.set("time", $("#s-time").value);""")
rep("""["s-scope", "s-dir", "s-act", "s-kind", "s-day", "s-sort", "s-fav", "s-trig"]""",
"""["s-scope", "s-dir", "s-act", "s-kind", "s-day", "s-time", "s-sort", "s-fav", "s-trig"]""")

# ---------- отчёт: статистика рынка и корреляция с IMOEX
rep("""        ${topBlock("▲ Вверх", r.live.up, "up")}${topBlock("▼ Вниз", r.live.down, "down")}</div></div>`;""",
"""        ${topBlock("▲ Вверх", r.live.up, "up")}${topBlock("▼ Вниз", r.live.down, "down")}</div></div>
      ${marketStatsHtml(r.market_stats)}`;""")
rep("""  async function report(day) {""",
"""  function marketStatsHtml(m) {
    if (!m) return "";
    const list = (arr) => (arr.length ? arr.map((t) => `<a href="#ticker/${esc(t)}">${esc(t)}</a>`).join(", ") : '<span class="muted">нет</span>');
    const corrCls = (c) => (c >= m.high_threshold ? "up" : c < m.low_threshold ? "down" : "");
    return `<div class="card" id="mkt-stats"><h2>Статистика рынка <span class="muted small">· дневные свечи за ${m.days} дн., бумаг: ${m.tickers}</span></h2>
      <div class="stats">${stat("Среднее внутридневное колебание", m.avg_intraday_pct === null ? "—" : num(m.avg_intraday_pct, 2) + "%")}
        ${stat(`Среднее колебание за неделю (${m.week} дн.)`, m.avg_weekly_pct === null ? "—" : num(m.avg_weekly_pct, 2) + "%")}
        ${stat("Точек IMOEX", m.imoex_points)}</div>
      <p class="small">Высокая корреляция с IMOEX (≥ ${m.high_threshold}): ${list(m.high)}</p>
      <p class="small">Низкая корреляция (&lt; ${m.low_threshold}): ${list(m.low)}</p>
      ${m.correlation.length ? `<details><summary class="small">Все бумаги по убыванию корреляции (${m.correlation.length})</summary>
        <div class="table-wrap"><table><thead><tr><th>Тикер</th><th>Название</th><th class="num">Корреляция</th><th class="num">Дней</th>
          <th class="num">Внутри дня, %</th><th class="num">За неделю, %</th></tr></thead><tbody>
          ${m.correlation.map((c) => `<tr><td><a href="#ticker/${esc(c.ticker)}"><b>${esc(c.ticker)}</b></a>${copyBtn(c.ticker)}</td>
            <td class="muted small">${esc(c.name || "")}</td><td class="num ${corrCls(c.corr)}">${num(c.corr, 3)}</td><td class="num">${c.n}</td>
            <td class="num">${num(c.intraday_pct, 2)}</td><td class="num">${num(c.weekly_pct, 2)}</td></tr>`).join("")}
        </tbody></table></div></details>`
        : '<p class="muted small">Корреляция появится, когда накопится история IMOEX и дневных свечей (≥ 10 общих дней).</p>'}
      <p class="muted small">Корреляция Пирсона дневных доходностей бумаги и индекса. Высокая — бумага ходит «с рынком», низкая — живёт своей жизнью.</p></div>`;
  }
  async function report(day) {""")

# ---------- Paper
rep("""  async function paper() {
    const r = await api("/api/paper");""",
"""  // Колонки Paper для сортировки (issue #5, п. 6): ключ → значение строки
  const PAPER_SORT = {
    id: (t) => t.id, ticker: (t) => t.ticker, side: (t) => t.side, qty: (t) => t.qty, entry: (t) => t.entry_price,
    stop: (t) => t.stop, take: (t) => t.take, price: (t) => (t.status === "open" ? t.current_price : t.exit_price),
    result: (t) => (t.status === "open" ? t.unrealized_pct : t.result_pct),
    profit: (t) => (t.status === "open" ? t.unrealized_rub : t.profit_rub),
    cost: (t) => (t.money || {}).cost_rub, net: (t) => (t.money || {}).net_rub,
    status: (t) => t.status, opened: (t) => t.opened_at,
  };
  async function paper(keepScroll) {
    // сохранить позицию прокрутки страницы и таблицы при перерисовке (п. 2)
    const wrap0 = $("#pp-wrap");
    const scroll = keepScroll === false ? null : { y: window.scrollY, t: wrap0 ? wrap0.scrollTop : 0, l: wrap0 ? wrap0.scrollLeft : 0 };
    const r = S.paperData && keepScroll === "local" ? S.paperData : await api("/api/paper");
    S.paperData = r;
    S.paperSort = S.paperSort || { key: "id", asc: false };
    const sk = S.paperSort;
    const getter = PAPER_SORT[sk.key] || PAPER_SORT.id;
    const trades = r.trades.slice().sort((a, b) => {
      const x = getter(a), y = getter(b);
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * (sk.asc ? 1 : -1);
    });
    const th = (k, label, num_) => `<th data-psort="${k}" class="${num_ ? "num" : ""} ${sk.key === k ? "sorted" + (sk.asc ? " asc" : "") : ""}">${label}</th>`;""")
rep("""        ${stat("Винрейт", st.winrate === null ? "—" : num(st.winrate, 1) + "%")}${stat("Сумма результатов", pct(st.total_pct))}
        ${stat("Профит закрытых", rub(st.total_rub))}${stat("Плавающий профит", rub(st.open_rub))}</div>""",
"""        ${stat("Винрейт", st.winrate === null ? "—" : num(st.winrate, 1) + "%")}
        ${stat("Профит закрытых", rub(st.total_rub))}${stat("Плавающий профит", rub(st.open_rub))}
        ${stat("Потрачено на покупку", st.spent_rub ? num(st.spent_rub, 2) + " ₽" : "—")}${stat("В т.ч. в открытых", st.spent_open_rub ? num(st.spent_open_rub, 2) + " ₽" : "—")}
        ${stat("Комиссии брокера", st.fees_rub ? "−" + num(st.fees_rub, 2) + " ₽" : "—")}${stat("Налог " + num(ps.tax_profit_pct) + "%", st.tax_rub ? "−" + num(st.tax_rub, 2) + " ₽" : "—")}
        ${stat("Чистыми (закрытые)", rub(st.net_rub))}</div>
      <p class="small muted">Комиссия брокера: вход <b>${num(ps.fee_entry_pct)}%</b>, выход <b>${num(ps.fee_exit_pct)}%</b>; налог <b>${num(ps.tax_profit_pct)}%</b> с положительного результата после комиссий.
        «Чистыми» = профит − комиссии − налог. Сторона по умолчанию: <b>${esc({ auto: "авто", long: "лонг", short: "шорт" }[ps.paper_default_side] || ps.paper_default_side || "авто")}</b>.</p>""")
rep("""<div class="row"><input id="pp-t" placeholder="Тикер" style="width:110px"><select id="pp-side"><option value="long">long</option><option value="short">short</option></select>""",
"""<div class="row"><input id="pp-t" placeholder="Тикер" style="width:110px"><select id="pp-side"><option value="">сторона по умолчанию</option><option value="long">long</option><option value="short">short</option></select>""")
rep("""        <input id="pp-k" placeholder="Тейк (пусто — от ATR)" style="width:170px"><button id="pp-go" class="primary">Открыть</button></div></div>""",
"""        <input id="pp-k" placeholder="Тейк (пусто — от ATR)" style="width:170px"><button id="pp-go" class="primary">Открыть</button>
        <span class="spacer"></span><button id="pp-clear" class="danger" title="Удалить все свои paper-сделки и обнулить статистику">Очистить статистику</button></div></div>""")
rep("""      <div class="card">${r.trades.length ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>Тикер</th><th>Сторона</th><th class="num">Кол-во, шт</th><th class="num">Вход</th>
        <th class="num">Стоп</th><th class="num">Тейк</th><th class="num">Текущая/выход</th><th class="num">Результат</th><th class="num">Профит, ₽</th><th>Статус</th><th>Открыта</th><th></th></tr></thead><tbody>
        ${r.trades.map((t) => `<tr><td class="muted">${t.id}</td><td><a href="#ticker/${esc(t.ticker)}"><b>${esc(t.ticker)}</b></a></td><td>${esc(t.side)}</td>
          <td class="num"><input class="qty" type="number" min="0" step="any" data-qty="${t.id}" value="${t.qty ?? ""}" placeholder="—" title="Количество акций${t.lot ? " (лот " + t.lot + ")" : ""}"></td>
          <td class="num">${num(t.entry_price)}</td><td class="num">${num(t.stop)}</td><td class="num">${num(t.take)}</td>""",
"""      <div class="card">${r.trades.length ? `<div class="table-wrap" id="pp-wrap"><table><thead><tr>${th("id", "#")}${th("ticker", "Тикер")}${th("side", "Сторона")}
        ${th("qty", "Кол-во, шт", 1)}${th("entry", "Вход", 1)}${th("stop", "Стоп", 1)}${th("take", "Тейк", 1)}${th("price", "Текущая/выход", 1)}
        ${th("result", "Результат", 1)}${th("profit", "Профит, ₽", 1)}${th("cost", "Потрачено, ₽", 1)}${th("net", "Чистыми, ₽", 1)}
        ${th("status", "Статус")}${th("opened", "Открыта")}<th></th></tr></thead><tbody>
        ${trades.map((t) => `<tr><td class="muted">${t.id}</td><td><a href="#ticker/${esc(t.ticker)}"><b>${esc(t.ticker)}</b></a>${copyBtn(t.ticker)}</td>
          <td>${t.status === "open" ? `<select class="side" data-side="${t.id}" title="Сменить сторону: стоп и тейк зеркалируются относительно входа">
            <option value="long" ${t.side === "long" ? "selected" : ""}>long</option><option value="short" ${t.side === "short" ? "selected" : ""}>short</option></select>` : esc(t.side)}</td>
          <td class="num"><input class="qty" type="number" min="0" step="any" data-qty="${t.id}" value="${t.qty ?? ""}" placeholder="—" title="Количество акций${t.lot ? " (лот " + t.lot + ")" : ""}"></td>
          <td class="num">${num(t.entry_price)}</td>
          ${["stop", "take"].map((k) => `<td class="num">${t.status === "open"
            ? `<input class="lvl ${t.levels_valid ? "" : "bad"}" type="number" step="any" data-lvl="${k}" data-id="${t.id}" value="${t[k] ?? ""}" placeholder="—"
                title="${k === "stop" ? "Стоп" : "Тейк"}: правка вручную — срабатывание проверяется только по свечам после правки${t.levels_valid ? "" : " · уровни не соответствуют стороне"}">`
            : num(t[k])}</td>`).join("")}""")
rep("""          <td class="num">${rub(t.status === "open" ? t.unrealized_rub : t.profit_rub)}</td>""",
"""          <td class="num">${rub(t.status === "open" ? t.unrealized_rub : t.profit_rub)}</td>
          <td class="num">${(t.money || {}).cost_rub ? num(t.money.cost_rub, 2) : "—"}</td>
          <td class="num" title="${t.money && t.money.fee_rub !== null ? `комиссии ${num(t.money.fee_rub, 2)} ₽` + (t.money.tax_rub ? `, налог ${num(t.money.tax_rub, 2)} ₽` : "") : ""}">${rub((t.money || {}).net_rub)}</td>""")
rep("""          <p class="muted small">Количество можно дописать в любой момент (и у закрытых сделок) — профит в ₽ пересчитается.</p>`""",
"""          <p class="muted small">Количество можно дописать в любой момент (и у закрытых сделок) — профит в ₽ пересчитается. Стоп и тейк открытой сделки
            правятся прямо в таблице; сторона — переключателем long/short. Нажмите на заголовок колонки, чтобы отсортировать.</p>`""")
rep("""    $("#pp-go").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const body = { ticker: $("#pp-t").value.trim(), side: $("#pp-side").value };""",
"""    if (scroll) {
      window.scrollTo(0, scroll.y);
      const w = $("#pp-wrap");
      if (w) { w.scrollTop = scroll.t; w.scrollLeft = scroll.l; }
    }
    $$("[data-psort]").forEach((h) => h.addEventListener("click", () => {
      const k = h.dataset.psort;
      S.paperSort = { key: k, asc: S.paperSort.key === k ? !S.paperSort.asc : k === "ticker" || k === "side" || k === "status" };
      paper("local");
    }));
    $("#pp-clear").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      if (!confirm("Удалить ВСЕ свои paper-сделки (открытые и закрытые) и обнулить статистику?")) return;
      const res = await api("/api/paper", { method: "DELETE" });
      toast("Удалено сделок: " + res.deleted);
      paper(false);
    }));
    $$("[data-side]").forEach((sel) => sel.addEventListener("change", async () => {
      try {
        await api(`/api/paper/${sel.dataset.side}`, { method: "PATCH", body: { side: sel.value } });
        toast("Сторона изменена — стоп и тейк перевёрнуты");
      } catch (e) { toast(e.message, true); }
      paper();
    }));
    $$("[data-lvl]").forEach((inp) => inp.addEventListener("change", async () => {
      try {
        await api(`/api/paper/${inp.dataset.id}`, { method: "PATCH",
          body: { [inp.dataset.lvl]: inp.value === "" ? null : +inp.value.replace(",", ".") } });
        toast((inp.dataset.lvl === "stop" ? "Стоп" : "Тейк") + " сохранён");
      } catch (e) { toast(e.message, true); }
      paper();
    }));
    $("#pp-go").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const body = { ticker: $("#pp-t").value.trim() };
      if ($("#pp-side").value) body.side = $("#pp-side").value;""")

# ---------- настройки: select для str с вариантами, сброс поля к умолчанию
rep("""  function fieldHtml(sp, value, overridden) {
    const id = "f-" + sp.key;
    const help = sp.help ? `<span class="help">${esc(sp.help)}</span>` : "";""",
"""  function fieldHtml(sp, value, overridden, resettable) {
    const id = "f-" + sp.key;
    // «↺» — вернуть одно поле к значению по умолчанию (issue #5, п. 5)
    const reset = resettable && overridden
      ? `<button type="button" class="reset-btn" data-reset="${sp.key}" title="Вернуть значение по умолчанию">↺</button>` : "";
    const help = (sp.help ? `<span class="help">${esc(sp.help)}</span>` : "") + reset;""")
rep("""    const type = sp.type === "time" ? "time" : sp.type === "str" ? "text" : "number";""",
"""    if (sp.type === "str" && sp.choices) {
      return `<label class="${cls}"><span>${esc(sp.label)}</span>
        <select id="${id}" data-key="${sp.key}">${Object.entries(sp.choices).map(([k, l]) =>
          `<option value="${esc(k)}" ${String(value) === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>${help}</label>`;
    }
    const type = sp.type === "time" ? "time" : sp.type === "str" ? "text" : "number";""")
rep("""  function formHtml(schema, values, overrides, scopeFilter) {
    const groups = {};
    schema.filter(scopeFilter).forEach((sp) => (groups[sp.group] = groups[sp.group] || []).push(sp));
    return Object.entries(groups).map(([g, items]) => `<fieldset><legend>${esc(g)}</legend>
      ${items.map((sp) => fieldHtml(sp, values[sp.key], overrides && sp.key in overrides)).join("")}</fieldset>`).join("");""",
"""  function formHtml(schema, values, overrides, scopeFilter) {
    const groups = {};
    schema.filter(scopeFilter).forEach((sp) => (groups[sp.group] = groups[sp.group] || []).push(sp));
    return Object.entries(groups).map(([g, items]) => `<fieldset><legend>${esc(g)}</legend>
      ${items.map((sp) => fieldHtml(sp, values[sp.key], overrides && sp.key in overrides, !!overrides)).join("")}</fieldset>`).join("");""")
rep("""      <div class="card"><div class="row between"><h2>Мои пороги и дашборд</h2>""",
"""      <div class="card" id="away-card"><div class="row between"><h2>«Отошёл»</h2>
          <label class="switch"><input type="checkbox" id="away" ${me.away_since ? "checked" : ""}><span>${me.away_since ? "включено с " + esc(String(me.away_since).slice(5, 16)) : "выключено"}</span></label></div>
        <p class="small muted">Пока включено, пуши о сигналах не приходят (сигналы пишутся в историю с пометкой «отошёл»).
          При выключении досылаются только сигналы последних двух сканирований — до 10 отдельными пушами, остальное одним сводным.</p></div>
      <div class="card"><div class="row between"><h2>Мои пороги и дашборд</h2>""")
rep("""        <span class="muted small">Не заданные вами значения берутся из глобальных настроек админа. «• свой» — переопределено.</span></div>""",
"""        <span class="muted small">Не заданные вами значения берутся из глобальных настроек админа. «• свой» — переопределено, «↺» — вернуть значение по умолчанию.</span></div>""")
rep("""    pushEnvNote();
    $("#s-save").addEventListener""",
"""    pushEnvNote();
    $("#away").addEventListener("change", async (e) => {
      const on = e.currentTarget.checked;
      try {
        const r = await api("/api/away", { method: "POST", body: { on } });
        toast(on ? "«Отошёл» включён: пуши заморожены" : `С возвращением! Дослано сигналов: ${r.redelivered || 0}` +
          (r.summary ? ` (сводным пушем: ${r.summary})` : ""));
      } catch (ex) { toast(ex.message, true); }
      settings();
    });
    $$("[data-reset]", $("#sf")).forEach((b) => b.addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const r = await api("/api/settings?key=" + encodeURIComponent(b.dataset.reset), { method: "DELETE" });
      S.settings.values = r.values; S.settings.overrides = r.overrides;
      toast("Значение по умолчанию восстановлено");
      settings();
    })));
    $("#s-save").addEventListener""")

# ---------- админ: память
rep("""          <button id="a-purge" title="Удалить данные старше срока хранения (настройка «Хранить историю, дней»)">Очистить старше ${esc(st.global.retention_days)} дн.</button>""",
"""          <button id="a-purge" title="Удалить данные старше срока хранения (${esc(st.global.retention_days)} дн., настройка «Хранить историю, дней») и сжать базу (VACUUM)">Освободить память</button>
          <button id="a-check" title="Проверить размер базы и свободное место на диске; если места достаточно — снять паузу сканера">Проверить память</button>""")
rep("""        <input id="a-body" placeholder="Текст (необязательно)" style="width:100%;margin-top:8px"></div></div>""",
"""        <input id="a-body" placeholder="Текст (необязательно)" style="width:100%;margin-top:8px">
        <h3>Память</h3><div id="a-storage"><span class="spin"></span></div></div></div>""")
rep("""    drawKeys(keys.keys);
    $("#k-new")""",
"""    drawKeys(keys.keys);
    const drawStorage = (s, before) => {
      const usedPct = s.disk_total ? (1 - s.disk_free / s.disk_total) * 100 : 0;
      $("#a-storage").innerHTML = `<div class="stats">${stat("База данных", fmtBytes(s.db_bytes) + (before !== undefined ? ` <span class="muted small">было ${fmtBytes(before)}</span>` : ""))}
        ${stat("Свободно на диске", `<span class="${s.low ? "down" : ""}">${fmtBytes(s.disk_free)}</span> из ${fmtBytes(s.disk_total)}`)}
        ${stat("Минимум свободного", s.min_free_gb ? num(s.min_free_gb, 1) + " ГБ" : "выкл.")}
        ${stat("Сканер", s.paused ? '<span class="tag down">пауза: мало места</span>' : '<span class="tag up">работает</span>')}</div>
        <div class="closeness wide-bar ${usedPct > 90 ? "full" : ""}" title="Занято ${num(usedPct, 1)}%"><i style="width:${Math.min(100, usedPct)}%"></i></div>
        <p class="muted small">Проверено ${esc(String(s.checked_at || "").slice(5, 19))} · хранение истории ${s.retention_days} дн.
          ${s.paused ? `· пауза с ${esc(String(s.paused.at || "").slice(5, 16))}: освободите место и нажмите «Проверить память».` : ""}</p>`;
    };
    api("/api/admin/storage").then((s) => drawStorage(s)).catch((ex) => { $("#a-storage").innerHTML = `<p class="error small">${esc(ex.message)}</p>`; });
    $("#a-check").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const s = await api("/api/admin/storage/check", { method: "POST" });
      drawStorage(s);
      toast(s.resumed ? "Места достаточно — сканер продолжает работу" : s.low ? "Свободного места всё ещё мало — сканер на паузе"
        : `Свободно ${fmtBytes(s.disk_free)} — всё в порядке`, s.low);
      refreshStatus();
    }));
    $("#k-new")""")
rep("""      const r = await api("/api/admin/purge", { method: "POST" });
      toast("Удалено: " + Object.entries(r.deleted || {}).map(([k, v]) => `${k} ${v}`).join(", "));""",
"""      const r = await api("/api/admin/purge", { method: "POST" });
      drawStorage(r.storage, r.before_bytes);
      toast(`База: ${fmtBytes(r.before_bytes)} → ${fmtBytes(r.after_bytes)}. Удалено строк: ` +
        Object.entries(r.deleted || {}).map(([k, v]) => `${k} ${v}`).join(", "));""")

open(p, "w", encoding="utf-8").write(s)
print("ok")
