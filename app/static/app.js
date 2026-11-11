/* SPA дашборда: вход по ключу, таблица рынка, карточка тикера с графиками,
 * P0-кнопки (бывшее CLI-меню), сигналы, отчёт дня, «почему молчит», paper,
 * метрики планировщика, настройки и пуши, админка. Без фреймворков. */
(function () {
  "use strict";
  const TB = window.TBCharts;
  const C = TB.COLORS;
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const view = $("#view");
  const S = { me: null, settings: null, table: null, sort: { key: null, asc: false }, statusTimer: null, viewTimer: null,
    lastAnalysis: [] };

  // ------------------------------------------------------------ утилиты
  function esc(v) {
    return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  const num = (v, nd) => TB.fmt(v, nd);
  // Близость к порогу в %: не сработавшее не показываем как «100%» (0.996 → 99%)
  const closePct = (v, triggered) => ((triggered ?? (v >= 1)) ? 100 : Math.min(99, Math.floor((v || 0) * 100)));
  const COND = { amplitude: "амплитуда", volume: "объём", level: "пробой уровня", plate: "плита" };
  const condLabels = (failed) => (failed || "").split(",").filter(Boolean).map((c) => COND[c] || c).join(", ");
  function pct(v, nd) {
    if (v === null || v === undefined) return '<span class="muted">—</span>';
    const cls = v > 0 ? "up" : v < 0 ? "down" : "";
    return `<span class="${cls}">${v > 0 ? "+" : ""}${num(v, nd === undefined ? 2 : nd)}%</span>`;
  }
  function compact(v) {
    if (v === null || v === undefined) return "—";
    const a = Math.abs(v);
    if (a >= 1e9) return num(v / 1e9, 2) + " млрд";
    if (a >= 1e6) return num(v / 1e6, 2) + " млн";
    if (a >= 1e3) return num(v / 1e3, 1) + " тыс";
    return num(v, 0);
  }
  const time = (ts) => (ts ? String(ts).slice(11, 16) : "—");
  const dayOf = (ts) => (ts ? String(ts).slice(0, 10) : "");
  function toast(msg, err) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast" + (err ? " err" : "");
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), err ? 7000 : 3500);
  }
  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    const r = await fetch(path, init);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (r.status === 401 && !opts.noAuthRedirect) { showLogin(); throw new Error("Нужен вход"); }
    if (!r.ok) {
      let d = data && data.detail;
      if (d && d.errors) d = Object.entries(d.errors).map(([k, v]) => `${k}: ${v}`).join("; ");
      throw new Error(typeof d === "string" ? d : `HTTP ${r.status}`);
    }
    return data;
  }
  async function busy(btn, fn) {
    const label = btn ? btn.innerHTML : "";
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>' + label; }
    try { return await fn(); }
    catch (e) { toast(e.message, true); }
    finally { if (btn) { btn.disabled = false; btn.innerHTML = label; } }
  }
  const dirTag = (d, label) => {
    const cls = /^up/.test(d) ? "up" : /^down|sector/.test(d) ? "down" : "";
    return `<span class="tag ${cls}">${esc(label || d)}</span>`;
  };
  const verdictCls = (v) => "v-" + (v || "HOLD");

  // ------------------------------------------------------------- вход
  function showLogin() {
    $("#app").hidden = true;
    $("#login").hidden = false;
    clearInterval(S.statusTimer);
    $("#login-key").focus();
  }
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#login-error");
    err.hidden = true;
    try {
      await api("/api/login", { method: "POST", body: { key: $("#login-key").value.trim(),
        secure: location.protocol === "https:" }, noAuthRedirect: true });
      $("#login-key").value = "";
      await boot();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  });
  $("#logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    S.me = null;
    showLogin();
  });

  async function boot() {
    try { S.me = await api("/api/me", { noAuthRedirect: true }); }
    catch (e) { showLogin(); return; }
    S.settings = await api("/api/settings");
    $("#login").hidden = true;
    $("#app").hidden = false;
    $("#who").textContent = (S.me.is_admin ? "админ · " : "") + (S.me.user.label || "ключ #" + S.me.user.id);
    $$(".admin-only").forEach((n) => (n.hidden = !S.me.is_admin));
    registerSW();
    refreshStatus();
    clearInterval(S.statusTimer);
    S.statusTimer = setInterval(() => { if (!document.hidden) refreshStatus(); }, 15000);
    route();
  }

  // ----------------------------------------------------------- статус
  async function refreshStatus() {
    try {
      const st = await api("/api/status");
      const c = st.cycle || {};
      const idx = st.index || {};
      const q = (st.scheduler && st.scheduler.queue) || {};
      $("#statusbar").innerHTML = [
        `<span><i class="dot ${st.cycle_running ? "busy" : c.status === "ok" ? "on" : ""}"></i>Цикл <b>#${esc(c.cycle_id || "—")}</b>
          ${st.cycle_running ? "идёт…" : c.finished_at ? "завершён " + time(c.finished_at) : ""}</span>`,
        c.status === "closed" ? `<span class="tag warn" title="Сканирование на паузе (настройки админа «Сканер»)">пауза: ${esc(c.closed_reason || "биржа закрыта")}${c.reopen_at ? " · до " + esc(String(c.reopen_at).slice(5, 16)) : ""}</span>` : "",
        c.warmup && c.status !== "closed" ? `<span class="tag" title="Первый цикл после запуска/перерыва: сигналы записаны, но пуши не отправлялись">прогрев: ${esc(c.warmup)}</span>` : "",
        c.next_cycle_at ? `<span>Следующий <b>${time(c.next_cycle_at)}</b></span>` : "",
        c.triggered !== undefined ? `<span>Триггеров <b>${c.triggered}</b> · алертов <b>${c.alerts}</b> · пушей <b>${c.pushed}</b></span>` : "",
        `<span>Бумаг <b>${st.tickers}</b></span>`,
        idx.CURRENTVALUE ? `<span>IMOEX <b>${num(idx.CURRENTVALUE, 2)}</b> ${pct(idx.LASTCHANGEPRC)}</span>` : "",
        `<span>Очередь P0/P1/P2 <b>${q.P0 || 0}/${q.P1 || 0}/${q.P2 || 0}</b></span>`,
        `<span class="spacer"></span><span>МСК <b>${time(st.now)}</b></span>`,
      ].join("");
    } catch (e) { /* молча */ }
  }

  // ------------------------------------------------------------ роутер
  window.addEventListener("hashchange", route);
  function route() {
    if (!S.me) return;
    clearInterval(S.viewTimer);
    const [name, arg] = (location.hash.slice(1) || "market").split("/");
    $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === (name === "ticker" ? "market" : name)));
    const views = { market, ticker: () => tickerView(decodeURIComponent(arg || "")), signals, report, silent, paper,
      metrics, settings, admin };
    (views[name] || market)().catch((e) => {
      view.innerHTML = `<div class="card error">${esc(e.message)}</div>`;
    });
  }
  const go = (hash) => { location.hash = hash; };

  // =================================================== Рынок (таблица + P0)
  async function market() {
    view.innerHTML = `
      <div class="card">
        <div class="row between"><h2>Анализ по запросу <span class="muted small">· P0 — вне очереди цикла</span></h2>
          <span class="muted small">Кнопки заменяют пункты старого CLI-меню</span></div>
        <div class="row">
          <input id="q" list="q-list" placeholder="Тикер или название (SBER, Газпром…)" style="width:280px">
          <datalist id="q-list"></datalist>
          <button id="b-one" class="primary">Анализ акции</button>
          <input id="q-many" placeholder="Несколько: SBER, GAZP, LKOH" style="width:240px">
          <button id="b-many">Анализ нескольких</button>
          <button id="b-fav">★ Избранное</button>
          <button id="b-top">Топ-10 голубых фишек</button>
          <button id="b-pulse">Пульсирующие 1–5%</button>
        </div>
        <div id="results"></div>
      </div>
      <div class="card">
        <div class="row between">
          <h2>Рынок <span id="tbl-meta" class="muted small"></span></h2>
          <div class="row">
            <input id="flt" placeholder="Фильтр" style="width:160px">
            <label class="small muted"><input type="checkbox" id="only-fav"> только избранное</label>
            <label class="small muted"><input type="checkbox" id="only-trig"> только триггеры</label>
            <a href="#settings" class="small">Колонки…</a>
          </div>
        </div>
        <div id="tbl" class="table-wrap"><div class="empty">Загрузка…</div></div>
      </div>`;
    let tq;
    $("#q").addEventListener("input", () => {
      clearTimeout(tq);
      tq = setTimeout(async () => {
        const v = $("#q").value.trim();
        if (v.length < 2) return;
        const r = await api("/api/search?q=" + encodeURIComponent(v)).catch(() => ({ results: [] }));
        $("#q-list").innerHTML = r.results.map((x) => `<option value="${esc(x.ticker)}">${esc(x.name || "")}</option>`).join("");
      }, 250);
    });
    $("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#b-one").click(); });
    $("#b-one").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const t = $("#q").value.trim().toUpperCase();
      if (!t) throw new Error("Введите тикер");
      const r = await api("/api/analyze", { method: "POST", body: { ticker: t } });
      showResults([r]);
    }));
    $("#b-many").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const r = await api("/api/analyze-many", { method: "POST", body: { tickers: $("#q-many").value } });
      showResults(r.results, r.unknown);
    }));
    $("#b-fav").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const r = await api("/api/analyze-many", { method: "POST", body: { favorites: true } });
      if (!r.results.length) throw new Error("Избранное пусто — отметьте ★ в таблице");
      showResults(r.results, r.unknown);
    }));
    $("#b-top").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      showTop10(await api("/api/top10", { method: "POST", body: {} }));
    }));
    $("#b-pulse").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      showPulsating(await api("/api/pulsating", { method: "POST", body: { min_pct: 1, max_pct: 5 } }));
    }));
    ["flt", "only-fav", "only-trig"].forEach((id) => $("#" + id).addEventListener("input", drawTable));
    await loadTable();
    S.viewTimer = setInterval(() => { if (!document.hidden) loadTable(); }, 60000);
  }

  async function loadTable() {
    S.table = await api("/api/table");
    drawTable();
  }

  function cell(key, r) {
    const v = r[key];
    switch (key) {
      case "ticker": return `<td><span class="star ${r.favorite ? "on" : ""}" data-fav="${esc(r.ticker)}" title="Избранное">★</span>
        <b>${esc(v)}</b>${r.triggered ? ' <span class="tag accent">триггер</span>' : ""}</td>`;
      case "name": return `<td class="muted">${esc(v || "")}</td>`;
      case "day_change_pct": case "change_hour_pct": return `<td class="num">${pct(v)}</td>`;
      case "volume_ratio": return `<td class="num">${v === null || v === undefined ? "—" : "×" + num(v, 2)}</td>`;
      case "volume": case "volume_today": case "value_today": return `<td class="num">${compact(v)}</td>`;
      case "level_break": return `<td class="num">${v > 0 ? '<span class="up">▲ вверх</span>' : v < 0 ? '<span class="down">▼ вниз</span>' : '<span class="muted">—</span>'}</td>`;
      case "impulse_age_min": return `<td class="num">${v ? num(v, 0) : "—"}${r.late ? ' <span class="tag warn">поздно</span>' : ""}</td>`;
      case "plate_score": return `<td class="num">${v === null || v === undefined ? "—" : v + "/4"}${v >= 3 ? ` <span class="tag ${r.direction < 0 ? "down" : "up"}">плита</span>` : ""}</td>`;
      case "threshold_closeness": {
        const w = closePct(v, r.triggered);
        return `<td class="num"><span class="closeness ${w >= 100 ? "full" : ""}"><i style="width:${w}%"></i></span>${w}%</td>`;
      }
      case "updated": return `<td class="num muted">${time(v)}</td>`;
      case "actions": return `<td>${paperBtn(r.ticker, r.paper)}</td>`;
      case "approach_intensity": return `<td class="num" title="Интенсивность подхода к уровню (гипотеза прогноза пика пампа)">${v === null || v === undefined ? "—" : num(v, 2)}</td>`;
      case "pullback_pct": case "amplitude_pct": return `<td class="num">${v === null || v === undefined ? "—" : num(v, 2)}</td>`;
      default: return `<td class="num">${num(v)}</td>`;
    }
  }

  function drawTable() {
    const T = S.table;
    if (!T) return;
    const cols = T.columns.length ? T.columns : ["ticker"];
    const f = ($("#flt") && $("#flt").value.trim().toUpperCase()) || "";
    const onlyFav = $("#only-fav") && $("#only-fav").checked;
    const onlyTrig = $("#only-trig") && $("#only-trig").checked;
    let rows = T.rows.filter((r) => (!f || r.ticker.includes(f) || (r.name || "").toUpperCase().includes(f)) &&
      (!onlyFav || r.favorite) && (!onlyTrig || r.triggered));
    if (S.sort.key) {
      const k = S.sort.key, dir = S.sort.asc ? 1 : -1;
      rows = rows.slice().sort((a, b) => {
        const x = a[k], y = b[k];
        if (x === y) return 0;
        if (x === null || x === undefined) return 1;
        if (y === null || y === undefined) return -1;
        return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
      });
    }
    const c = T.cycle || {};
    $("#tbl-meta").textContent = `· ${rows.length} из ${T.rows.length} бумаг · окно ${T.window} мин · цикл #${c.cycle_id || "—"}` +
      " · сортировка по близости к порогу";
    if (!T.rows.length) {
      $("#tbl").innerHTML = '<div class="empty">Нет данных: дождитесь первого цикла сканера (до нескольких минут после запуска).</div>';
      return;
    }
    const head = cols.map((k) => `<th data-k="${k}" class="${k === "ticker" || k === "name" ? "" : "num"} ${S.sort.key === k ? "sorted" + (S.sort.asc ? " asc" : "") : ""}">${esc(T.labels[k] || k)}</th>`).join("");
    const body = rows.map((r) => `<tr class="click ${r.triggered ? "hot" : ""}" data-t="${esc(r.ticker)}">${cols.map((k) => cell(k, r)).join("")}</tr>`).join("");
    $("#tbl").innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    $$("#tbl th").forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.k;
      S.sort = { key: k, asc: S.sort.key === k ? !S.sort.asc : false };
      drawTable();
    }));
    $$("#tbl tbody tr").forEach((tr) => tr.addEventListener("click", (e) => {
      if (e.target.dataset.fav || e.target.closest("[data-paper-toggle]")) return;
      go("ticker/" + encodeURIComponent(tr.dataset.t));
    }));
    $$("#tbl [data-fav]").forEach((s) => s.addEventListener("click", async (e) => {
      e.stopPropagation();
      const t = s.dataset.fav, on = s.classList.contains("on");
      await api(on ? "/api/favorites/" + encodeURIComponent(t) : "/api/favorites",
        on ? { method: "DELETE" } : { method: "POST", body: { ticker: t } }).catch((ex) => toast(ex.message, true));
      const row = S.table.rows.find((r) => r.ticker === t);
      if (row) row.favorite = !on;
      drawTable();
    }));
    bindPaperToggles($("#tbl"), (t, paper) => {
      const row = S.table.rows.find((r) => r.ticker === t);
      if (row) row.paper = paper;
    });
  }

  // ------------------------------------------- кнопка Paper (вкл/выкл)
  // Нажата (светится синим) — по тикеру открыта paper-позиция и идёт
  // сопровождение; повторное нажатие отменяет (удаляет) позицию.
  function paperBtn(ticker, paper) {
    const on = !!paper;
    const tip = on ? `Paper открыта: ${num(paper.qty)} шт. по ${num(paper.entry_price)} — нажмите, чтобы отменить`
      : "Открыть paper-позицию по текущей цене и сопровождать";
    return `<button class="small paper-btn ${on ? "on" : ""}" data-paper-toggle="${esc(ticker)}" aria-pressed="${on}" title="${esc(tip)}">Paper${on ? " ✓" : ""}</button>`;
  }
  function bindPaperToggles(root, onChange) {
    $$("[data-paper-toggle]", root).forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      busy(b, async () => {
        const t = b.dataset.paperToggle;
        const r = await api("/api/paper/toggle", { method: "POST", body: { ticker: t } });
        const tmp = document.createElement("div");
        tmp.innerHTML = paperBtn(t, r.paper);
        const nb = tmp.firstChild;
        b.replaceWith(nb);
        bindPaperToggles(nb.parentNode, onChange);
        if (onChange) onChange(t, r.paper);
        toast(r.paper ? `Paper ${t}: ${num(r.paper.qty)} шт. по ${num(r.paper.entry_price)} — сопровождение включено`
          : `Paper ${t} отменена`);
      });
    }));
  }

  function showResults(list, unknown) {
    const box = $("#results");
    box.innerHTML = (unknown && unknown.length ? `<p class="warn">Не найдены: ${esc(unknown.join(", "))}</p>` : "") +
      list.map((r) => `<div class="card tight">${analysisHtml(r)}</div>`).join("");
    bindAnalysis(box);
  }

  function showTop10(r) {
    const order = ["BUY", "LATE", "SELL", "HOLD"];
    $("#results").innerHTML = order.map((g) => {
      const items = r.groups[g] || [];
      if (!items.length) return "";
      return `<h3 class="${verdictCls(g)}"><span class="label">${esc(r.labels[g])}</span> · ${items.length}</h3>
        <div class="table-wrap"><table><thead><tr><th>Тикер</th><th class="num">Цена</th><th class="num">День</th>
        <th class="num">Сила сигнала*</th><th class="num">Стоп</th><th class="num">Тейк</th><th class="num">R/R</th><th>Почему</th></tr></thead><tbody>
        ${items.map((x) => `<tr class="click" data-t="${esc(x.ticker)}"><td><b>${esc(x.ticker)}</b> <span class="muted">${esc(x.name || "")}</span></td>
          <td class="num">${num(x.price)}</td><td class="num">${pct(x.day_change_pct)}</td><td class="num">${x.verdict.strength}/100</td>
          <td class="num">${num(x.verdict.stop)}</td><td class="num">${num(x.verdict.take)}</td><td class="num">${num(x.verdict.risk_reward, 2)}</td>
          <td class="small">${esc((x.verdict.late_reasons || []).join("; "))}</td></tr>`).join("")}</tbody></table></div>`;
    }).join("") + '<p class="muted small">* сила сигнала в текущих условиях, не вероятность успеха.</p>' +
      (r.errors.length ? `<p class="error small">Ошибки: ${esc(r.errors.map((e) => e.ticker + ": " + e.error).join("; "))}</p>` : "");
    $$("#results tr[data-t]").forEach((tr) => tr.addEventListener("click", () => go("ticker/" + tr.dataset.t)));
  }

  function showPulsating(r) {
    const idx = r.index || {};
    $("#results").innerHTML = `<h3>Пульсирующие: амплитуда свечи ${r.range[0]}–${r.range[1]}% · IMOEX ${num(idx.CURRENTVALUE, 2)} ${pct(idx.LASTCHANGEPRC)}</h3>
      ${r.rows.length ? `<div class="table-wrap"><table><thead><tr><th>Тикер</th><th class="num">Цена</th><th class="num">Амплитуда, %</th>
        <th class="num">Амплитуда/ATR</th><th class="num">Объём/ср.</th><th class="num">День</th></tr></thead><tbody>
        ${r.rows.map((x) => `<tr class="click" data-t="${esc(x.ticker)}"><td><b>${esc(x.ticker)}</b> <span class="muted">${esc(x.name || "")}</span></td>
          <td class="num">${num(x.price)}</td><td class="num">${num(x.amplitude_pct, 2)}</td><td class="num">${num(x.amplitude_atr, 2)}</td>
          <td class="num">×${num(x.volume_ratio, 2)}</td><td class="num">${pct(x.day_change_pct)}</td></tr>`).join("")}
      </tbody></table></div>` : '<div class="empty">Нет бумаг в диапазоне</div>'}
      ${r.detail.length ? "<h3>Детальный анализ топ-3</h3>" + r.detail.map((d) => `<div class="card tight">${analysisHtml(d)}</div>`).join("") : ""}`;
    $$("#results tr[data-t]").forEach((tr) => tr.addEventListener("click", () => go("ticker/" + tr.dataset.t)));
    bindAnalysis($("#results"));
  }

  // ---------------------------------------------- карточка анализа (P0)
  function kv(pairs) {
    return `<dl class="kv">${pairs.filter((p) => p).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
  }
  function analysisHtml(r) {
    if (r.error) return `<b>${esc(r.ticker)}</b> <span class="error">${esc(r.error)}</span>`;
    const v = r.verdict || {};
    const d = r.daily || {};
    const it = r.intraday || {};
    const sn = r.snapshot || {};
    const wt = d.weekly_trend || {};
    const ev = r.evaluation || {};
    const a = ev.alert;
    const ob = r.orderbook;
    return `
      <div class="row between">
        <div><a href="#ticker/${esc(r.ticker)}"><b style="font-size:16px">${esc(r.ticker)}</b></a>
          <span class="muted">${esc(r.full_name || r.name || "")}</span></div>
        <div class="mono">${num(r.price)} ₽ · ${pct(r.day_change_pct)} · <span class="muted small">${esc(r.ts || "")}</span></div>
      </div>
      <div class="verdict ${verdictCls(v.verdict)}" style="margin:10px 0">
        <span class="label">${esc(v.label || "—")}</span>
        <div class="strength"><div class="row between small"><span>Сила сигнала</span><b class="mono">${v.strength ?? "—"}/100</b></div>
          <div class="bar"><i style="width:${v.strength || 0}%"></i></div><small>${esc(v.strength_note || "")}</small></div>
        ${v.overheat && v.overheat.label ? `<span class="tag warn">${esc(v.overheat.label)} ${v.overheat.score}/4</span>` : ""}
        ${a ? dirTag(a.direction, a.arrow + " " + a.direction_label) + ` <span class="tag">${esc(a.action_label)}</span>` : ""}
      </div>
      ${(v.late_reasons || []).length ? `<p class="warn small">Почему «поздно»: ${esc(v.late_reasons.join("; "))}</p>` : ""}
      ${a ? `<p class="small">${esc(a.reason)}${(a.labels || []).length ? " · " + esc(a.labels.join(", ")) : ""}</p>` : ""}
      <div class="grid three">
        <div><h3>Решение</h3>${kv([
          ["Стоп", num(v.stop)], ["Тейк", num(v.take)], ["R/R", num(v.risk_reward, 2)],
          ["Зона покупки", v.zones ? `${num(v.zones.buy[0])} – ${num(v.zones.buy[1])}` : "—"],
          ["Зона продажи", v.zones ? `${num(v.zones.sell[0])} – ${num(v.zones.sell[1])}` : "—"],
          ["Очки BUY/SELL", `${v.buy_score ?? "—"} / ${v.sell_score ?? "—"}`],
        ])}</div>
        <div><h3>Дневные индикаторы</h3>${kv([
          ["RSI(14)", num(d.rsi, 1)], ["SMA20 / SMA50", `${num(d.sma20)} / ${num(d.sma50)}`],
          ["MACD / сигнал / hist", `${num(d.macd_line, 3)} / ${num(d.macd_signal, 3)} / ${num(d.macd_hist, 3)}`],
          ["Боллинджер", `${num(d.bb_lower)} · ${num(d.bb_middle)} · ${num(d.bb_upper)}`],
          ["Стохастик K/D", `${num(d.stoch_k, 1)} / ${num(d.stoch_d, 1)}`],
          ["ATR(14)", `${num(d.atr)} (${num(d.atr_pct, 2)}%)`],
          ["Пивот", num(d.pivot)], ["S1 / S2", `${num(d.s1)} / ${num(d.s2)}`], ["R1 / R2", `${num(d.r1)} / ${num(d.r2)}`],
          ["Тренд недели", `${esc(wt.trend || "—")} ${wt.strength ? "(" + num(wt.strength, 2) + ")" : ""}`],
          ["Объём сегодня / ср.20", `${compact(r.volume_today)} / ${compact(d.volume_avg20)}`],
        ])}</div>
        <div><h3>Окно ${esc(sn.window || "")} мин</h3>${kv([
          ["Амплитуда", `${num(sn.amplitude_pct, 2)}% = ${num(sn.amplitude_atr, 2)}×ATR`],
          ["Объём / средний", sn.volume_ratio !== undefined ? "×" + num(sn.volume_ratio, 2) : "—"],
          ["Пробой уровня", sn.level_break > 0 ? "▲ вверх" : sn.level_break < 0 ? "▼ вниз" : "нет"],
          ["Возраст импульса", sn.impulse_age_min !== undefined ? num(sn.impulse_age_min, 0) + " мин" : "—"],
          ["Плита", sn.plate_score !== undefined ? sn.plate_score + "/4" : "—"],
          ["RSI / Stoch K", `${num(it.rsi, 1)} / ${num(it.stoch_k, 1)}`],
          ["MACD hist", num(it.macd_hist, 4)], ["BB верх/низ", `${num(it.bb_upper)} / ${num(it.bb_lower)}`],
        ])}</div>
      </div>
      ${(r.signals || []).length ? `<h3>Сигналы</h3><ul class="plain">${r.signals.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
      <h3>Стакан</h3>${ob ? `<div class="row small mono">
          <div>Покупка: ${ob.bids.slice(0, 5).map((b) => `${num(b.price)}×${b.quantity}`).join(" · ")}</div>
          <div>Продажа: ${ob.asks.slice(0, 5).map((b) => `${num(b.price)}×${b.quantity}`).join(" · ")}</div>
          <div>Спред ${num(ob.spread_pct, 3)}%</div></div>`
        : `<p class="muted small">${r.orderbook_available === false ? "Стакан недоступен без подписки ISS — запросы отключены." : "Нет данных стакана."}</p>`}
      <div class="row" style="margin-top:10px">
        <button class="small" data-paper="${esc(r.ticker)}" data-side="long" data-stop="${v.stop ?? ""}" data-take="${v.take ?? ""}">Paper: long</button>
        <button class="small" data-paper="${esc(r.ticker)}" data-side="short">Paper: short</button>
      </div>`;
  }
  function bindAnalysis(root) {
    $$("[data-paper]", root).forEach((b) => b.addEventListener("click", () => busy(b, async () => {
      const body = { ticker: b.dataset.paper, side: b.dataset.side };
      if (b.dataset.stop) body.stop = +b.dataset.stop;
      if (b.dataset.take) body.take = +b.dataset.take;
      await api("/api/paper", { method: "POST", body });
      toast(`Paper-сделка ${body.side} ${body.ticker} открыта`);
    })));
  }

  // ======================================================= Карточка тикера
  async function tickerView(t, days) {
    days = days || 7;
    view.innerHTML = '<div class="card"><span class="spin"></span>Загрузка…</div>';
    const r = await api(`/api/ticker/${encodeURIComponent(t)}?days=${days}`);
    const sec = r.security || {};
    const row = r.row || {};
    const ev = r.evaluation;
    view.innerHTML = `
      <div class="card">
        <div class="row between">
          <div class="row"><a href="#market" class="muted">← Рынок</a>
            <h2 style="margin:0"><span class="star ${r.favorite ? "on" : ""}" id="fav">★</span> ${esc(r.ticker)}</h2>
            <span class="muted">${esc(sec.full_name || sec.name || "")}</span></div>
          <div class="row mono" id="price-box">${num(row.price)} ₽ · ${pct(row.day_change_pct)}
            ${paperBtn(r.ticker, r.paper)}
            <button id="p0" class="primary small">P0-анализ сейчас</button></div>
        </div>
        <div class="stats" style="margin-top:14px">
          ${stat("Амплитуда/ATR", num(row.amplitude_atr, 2))}
          ${stat("Объём / средний", row.volume_ratio !== undefined ? "×" + num(row.volume_ratio, 2) : "—")}
          ${stat("Изм. за час", pct(row.change_hour_pct))}
          ${stat("Возраст импульса", row.impulse_age_min !== undefined ? num(row.impulse_age_min, 0) + " мин" + (row.late ? ' <span class="tag warn">поздно</span>' : "") : "—")}
          ${stat("Плита", row.plate_score !== undefined ? row.plate_score + "/4" : "—")}
          ${stat("Близость к порогу", row.threshold_closeness !== undefined ? closePct(row.threshold_closeness, row.triggered) + "%" : "—")}
          ${stat("ATR дня", sec.daily_atr_pct ? num(sec.daily_atr_pct, 2) + "%" : "—")}
        </div>
        ${ev ? checksHtml(ev) : '<p class="muted">Снапшота ещё нет — бумага не обработана в цикле.</p>'}
      </div>
      <div id="analysis">${r.analysis ? `<div class="card"><p class="muted small">Последний P0-анализ · ${esc(r.analysis.ts || "")}</p>${analysisHtml(r.analysis)}</div>` : ""}</div>
      <div class="card">
        <div class="row between"><h2>Графики · окно ${r.series.window} мин</h2>
          <div class="row small">История:
            ${[[1, "1 д"], [3, "3 д"], [7, "7 д"], [14, "14 д"], [30, "месяц"]].map(([d, l]) =>
              `<button class="small ${d === days ? "primary" : "ghost"}" data-days="${d}">${l}</button>`).join("")}
          </div></div>
        ${days >= 14 ? `<p class="scroll-hint">${r.series.window >= 60 ? "Часовые свечи" : "Свечи"}${r.series.source === "iss_hourly" ? " из ISS (локальная история короче периода)" : ""} · листайте график по горизонтали ←→${r.series.note ? " · " + esc(r.series.note) : ""}</p>` : ""}
        <div class="choices small" id="chart-pick">${Object.entries(r.chart_labels).map(([k, l]) =>
          `<label><input type="checkbox" value="${k}" ${r.charts.includes(k) ? "checked" : ""}> ${esc(l)}</label>`).join("")}</div>
        <div id="charts"></div>
      </div>
      <div class="card"><h2>Сигналы по ${esc(r.ticker)}</h2>${alertsTable(r.alerts, false, true)}</div>`;
    drawCharts($("#charts"), r.series, r.charts, r.chart_labels);
    bindAnalysis(view);
    bindPaperToggles($("#price-box"), (_, paper) => { r.paper = paper; });
    $("#p0").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const a = await api("/api/analyze", { method: "POST", body: { ticker: r.ticker } });
      $("#analysis").innerHTML = `<div class="card"><p class="muted small">P0-анализ · ${esc(a.ts || "")}</p>${analysisHtml(a)}</div>`;
      bindAnalysis($("#analysis"));
    }));
    $("#fav").addEventListener("click", async () => {
      await api(r.favorite ? "/api/favorites/" + r.ticker : "/api/favorites",
        r.favorite ? { method: "DELETE" } : { method: "POST", body: { ticker: r.ticker } });
      r.favorite = !r.favorite;
      $("#fav").classList.toggle("on", r.favorite);
    });
    $$("[data-days]").forEach((b) => b.addEventListener("click", () => tickerView(t, +b.dataset.days)));
    $$("#chart-pick input").forEach((cb) => cb.addEventListener("change", async () => {
      const charts = $$("#chart-pick input").filter((x) => x.checked).map((x) => x.value);
      r.charts = charts;
      drawCharts($("#charts"), r.series, charts, r.chart_labels);
      try {
        const res = await api("/api/settings", { method: "PUT", body: { values: { charts } } });
        S.settings.values = res.values; S.settings.overrides = res.overrides;
      } catch (e) { toast(e.message, true); }
    }));
  }
  const stat = (k, v) => `<div class="stat"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;

  function checksHtml(ev) {
    return `<div class="table-wrap" style="max-height:none"><table><thead><tr><th>Условие</th><th class="num">Значение</th>
      <th class="num">Порог</th><th class="num">Выполнено на</th><th>Статус</th></tr></thead><tbody>
      ${ev.checks.map((c) => `<tr><td>${esc(c.label)}${c.note ? ` <span class="muted small">${esc(c.note)}</span>` : ""}</td>
        <td class="num">${num(c.value, 2)}</td><td class="num">${num(c.threshold, 2)}</td>
        <td class="num"><span class="closeness ${c.passed ? "full" : ""}"><i style="width:${Math.min(1, c.ratio || 0) * 100}%"></i></span>${num((c.ratio || 0) * 100, 0)}%</td>
        <td>${c.passed ? '<span class="tag up">да</span>' : '<span class="tag">нет</span>'}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="small">${ev.triggered ? '<span class="tag accent">триггер сработал</span>' : `Не хватает: ${esc(condLabels(ev.failed.join(",")))}`} ·
        близость ${closePct(ev.closeness, ev.triggered)}%</p>`;
  }

  function drawCharts(box, s, chosen, labels) {
    box.innerHTML = "";
    // ≥ 6 px на свечу: длинные периоды листаются, графики индикаторов — синхронно
    const sc = { step: 6, group: "ticker" };
    const ts = s.candles.map((c) => c[0]);
    const I = s.indicators;
    const add = (key, fn) => {
      if (!chosen.includes(key)) return;
      const wrap = document.createElement("div");
      wrap.innerHTML = `<div class="chart-title">${esc(labels[key])}</div><div class="chart"></div>`;
      box.appendChild(wrap);
      fn(wrap.querySelector(".chart"));
    };
    const bb = chosen.includes("bollinger") ? { upper: I.bb_upper, middle: I.bb_middle, lower: I.bb_lower } : null;
    if (chosen.includes("price")) add("price", (el) => TB.candles(el, s.candles, { bb, ...sc }));
    else if (bb) add("bollinger", (el) => TB.candles(el, s.candles, { bb, ...sc }));
    add("atr", (el) => TB.lines(el, ts, [{ name: "ATR(14)", values: I.atr, color: C.s1 }], { height: 100, ...sc }));
    add("rsi", (el) => TB.lines(el, ts, [{ name: "RSI(14)", values: I.rsi, color: C.s1 }],
      { min: 0, max: 100, fixed: true, refs: [[70, "70"], [30, "30"]], height: 110, ...sc }));
    add("macd", (el) => TB.lines(el, ts, [{ name: "MACD", values: I.macd_line, color: C.s1 },
      { name: "Сигнал", values: I.macd_signal, color: C.s2 }], { bars: { name: "Гистограмма", values: I.macd_hist }, refs: [[0, ""]], height: 120, ...sc }));
    add("stoch", (el) => TB.lines(el, ts, [{ name: "%K", values: I.stoch_k, color: C.s1 },
      { name: "%D", values: I.stoch_d, color: C.s2 }], { min: 0, max: 100, fixed: true, refs: [[80, "80"], [20, "20"]], height: 110, ...sc }));
    const M = s.metrics || {};
    const st = S.settings.values;
    add("amplitude_atr", (el) => TB.points(el, M.amplitude_atr, "Амплитуда/ATR",
      { refs: [[st.amplitude_atr_min, "порог " + st.amplitude_atr_min]], min: 0, step: 3, empty: "Метрика появится после циклов сканера" }));
    add("volume_ratio", (el) => TB.points(el, M.volume_ratio, "Объём / средний",
      { refs: [[st.volume_mult, "порог " + st.volume_mult]], min: 0, step: 3, empty: "Метрика появится после циклов сканера" }));
    add("impulse_age", (el) => TB.points(el, M.impulse_age_min, "Возраст импульса, мин",
      { refs: [[st.impulse_age_max_min, "поздно > " + st.impulse_age_max_min]], min: 0, step: 3, empty: "Метрика появится после циклов сканера" }));
    add("threshold_distance", (el) => TB.points(el, (M.threshold_closeness || []).map((p) => [p[0], p[1] * 100]),
      "Близость к порогу, %", { refs: [[100, "порог"]], min: 0, step: 3, empty: "Метрика появится после циклов сканера" }));
    if (!box.children.length) box.innerHTML = '<div class="empty">Графики не выбраны</div>';
  }

  // ============================================================= Сигналы
  function alertsTable(alerts, withTicker, noPaper) {
    if (!alerts.length) return '<div class="empty">Сигналов нет</div>';
    return `<div class="table-wrap"><table><thead><tr><th>Время</th>${withTicker ? "<th>Тикер</th>" : ""}<th>Тип</th>
      <th class="num" title="Цена в момент сигнала / текущая">Цена</th><th class="num">Оборот дня, ₽</th>
      <th class="num">Сила*</th><th class="num">Амп/ATR</th><th class="num">Объём/ср.</th><th class="num">Возраст, мин</th>
      <th>Действие</th><th>Причина</th><th>Пуш</th>${noPaper ? "" : "<th></th>"}</tr></thead><tbody>
      ${alerts.map((a) => `<tr><td class="mono small">${esc(String(a.ts).slice(5, 16))}</td>
        ${withTicker ? `<td><a href="#ticker/${esc(a.ticker)}"><b>${esc(a.ticker)}</b></a>${a.sectype && a.sectype !== "share" && S.kinds ? ` <span class="tag">${esc(S.kinds[a.sectype] || a.sectype)}</span>` : ""}</td>` : ""}
        <td>${dirTag(a.direction, a.arrow + " " + a.direction_label)}</td>
        <td class="num">${num(a.price)}${a.current_price && a.price && Math.abs(a.current_price - a.price) > 1e-9 ? `<br><span class="muted small" title="текущая">${num(a.current_price)}</span>` : ""}</td>
        <td class="num">${compact(a.value_today)}</td>
        <td class="num">${a.score ?? "—"}</td><td class="num">${num(a.amplitude_atr, 2)}</td>
        <td class="num">${a.volume_ratio ? "×" + num(a.volume_ratio, 2) : "—"}</td><td class="num">${num(a.impulse_age_min, 0)}</td>
        <td class="small">${esc(a.action_label || "")}${(a.labels || []).length ? `<br><span class="muted">${esc(a.labels.join(", "))}</span>` : ""}</td>
        <td class="small" style="white-space:normal;min-width:280px">${esc(a.reason)}</td>
        <td class="small">${a.key_id === null ? '<span class="muted">журнал</span>' : a.pushed ? '<span class="up">отправлен</span>'
          : a.suppressed ? `<span class="muted">${esc(a.suppressed)}</span>` : '<span class="muted">нет устройств</span>'}</td>
        ${noPaper ? "" : `<td>${a.ticker !== "РЫНОК" ? `<button class="small ghost" data-paper-alert="${a.id}">paper</button>` : ""}</td>`}</tr>`).join("")}
      </tbody></table></div><p class="muted small">* сила сигнала 0–100 — нормированная сумма признаков, не вероятность.
      ${noPaper ? "Paper-позиция по бумаге — кнопка «Paper» рядом с ценой вверху карточки." : ""}</p>`;
  }
  function bindAlertPaper(root) {
    $$("[data-paper-alert]", root).forEach((b) => b.addEventListener("click", () => busy(b, async () => {
      await api("/api/paper", { method: "POST", body: { alert_id: +b.dataset.paperAlert } });
      toast("Paper-сделка по сигналу открыта — сопровождение включено");
    })));
  }

  async function signals() {
    view.innerHTML = `<div class="card"><div class="row between"><h2>История сигналов</h2>
      <div class="row">
        <select id="s-scope"><option value="mine">Мои (по моим порогам)</option><option value="all">Журнал сканера (глобальные пороги)</option></select>
        <select id="s-dir"><option value="">Все типы</option></select>
        <select id="s-act"><option value="">Все действия</option></select>
        <select id="s-kind"><option value="">Акции и фонды</option></select>
        <input id="s-t" placeholder="Тикер" style="width:100px">
        <input id="s-day" type="date">
        <select id="s-sort"><option value="time">Сначала новые</option><option value="price_asc">Цена ↑</option>
          <option value="price_desc">Цена ↓</option><option value="value_desc">Оборот дня ↓</option></select>
        <label class="small muted"><input type="checkbox" id="s-fav"> только избранное</label>
        <label class="small muted" title="Только сигналы, прошедшие ваши фильтры пушей (без подавленных)"><input type="checkbox" id="s-trig"> только прошедшие фильтры</label>
        <button id="s-go">Показать</button></div></div>
      <div id="s-list"><div class="empty">Загрузка…</div></div></div>`;
    const load = async () => {
      const p = new URLSearchParams({ scope: $("#s-scope").value, sort: $("#s-sort").value });
      if ($("#s-dir").value) p.set("direction", $("#s-dir").value);
      if ($("#s-act").value) p.set("action", $("#s-act").value);
      if ($("#s-kind").value) p.set("kind", $("#s-kind").value);
      if ($("#s-t").value.trim()) p.set("ticker", $("#s-t").value.trim());
      if ($("#s-day").value) p.set("day", $("#s-day").value);
      if ($("#s-fav").checked) p.set("fav", "1");
      if ($("#s-trig").checked) p.set("triggered", "1");
      const r = await api("/api/alerts?" + p);
      S.kinds = r.kinds;
      if ($("#s-dir").options.length === 1) {
        $("#s-dir").innerHTML += Object.entries(r.directions).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
        $("#s-act").innerHTML += Object.entries(r.actions).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
        $("#s-kind").innerHTML = '<option value="">Все бумаги</option>' +
          Object.entries(r.kinds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
      }
      $("#s-list").innerHTML = alertsTable(r.alerts, true) +
        ($("#s-kind").value === "bond" && !r.alerts.length ? '<p class="muted small">Сканер смотрит режим TQBR (акции, фонды, расписки) — облигации торгуются в других режимах и сюда не попадают.</p>' : "");
      bindAlertPaper($("#s-list"));
    };
    $("#s-go").addEventListener("click", (e) => busy(e.currentTarget, load));
    ["s-scope", "s-dir", "s-act", "s-kind", "s-day", "s-sort", "s-fav", "s-trig"].forEach((id) => $("#" + id).addEventListener("change", load));
    $("#s-t").addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
    await load();
  }

  // =========================================================== Отчёт дня
  function topBlock(title, items, cls) {
    return `<div class="card"><h2 class="${cls}">${title}</h2>${items.length ? `<ol class="plain">${items.map((a, i) => `
      <li class="card tight" style="margin:0 0 8px"><div class="row between"><b>${i + 1}. <a href="#ticker/${esc(a.ticker)}">${esc(a.ticker)}</a></b>
        <span>${a.direction === "mover" ? pct(a.day_change_pct) : dirTag(a.direction, a.direction_label) + ` <span class="mono">сила ${a.score}</span>`}</span></div>
        <div class="small">${esc(a.reason)}</div>${a.action_label ? `<div class="small muted">${esc(a.action_label)} · ${time(a.ts)}</div>` : ""}</li>`).join("")}
      </ol>` : '<div class="empty">Нет</div>'}</div>`;
  }
  async function report(day) {
    const r = await api("/api/report" + (day ? "?day=" + day : ""));
    const rep = r.stored ? r.stored.top : r.live;
    view.innerHTML = `<div class="card"><div class="row between"><h2>Итог дня · ${esc(r.day)}</h2>
        <div class="row"><select id="r-day"><option value="${esc(r.day)}">${esc(r.day)}</option>
          ${r.days.filter((d) => d.day !== r.day).map((d) => `<option>${esc(d.day)}</option>`).join("")}</select>
          <button id="r-send">Отправить отчёт пушем сейчас</button></div></div>
        <p class="muted small">Автоматический пуш — ежедневно в <b>${esc(r.report_time)}</b> МСК (время меняется в настройках).
          ${r.stored ? "Показан сохранённый отчёт от " + esc(r.stored.created_at) : "Отчёт ещё не сформирован — показан текущий топ."}
          Источник: ${esc(rep.source === "alerts" ? "алерты дня" : "алерты + изменение дня по котировкам")}.</p></div>
      <div class="grid two">${topBlock("▲ Топ-3 вверх", rep.up, "up")}${topBlock("▼ Топ-3 вниз", rep.down, "down")}</div>
      <div class="card"><h2>Живой топ на сейчас</h2><div class="grid two">
        ${topBlock("▲ Вверх", r.live.up, "up")}${topBlock("▼ Вниз", r.live.down, "down")}</div></div>`;
    $("#r-day").addEventListener("change", () => report($("#r-day").value));
    $("#r-send").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const res = await api("/api/report/send", { method: "POST" });
      const p = res.push || {};
      toast(`Отчёт сформирован. Пушей отправлено: ${p.sent ?? 0} из ${p.devices ?? 0} устройств`);
      report(r.day);
    }));
  }

  // ======================================================= Почему молчит
  async function silent() {
    const [r, d] = await Promise.all([api("/api/silent"), api("/api/decisions")]);
    const th = r.thresholds;
    view.innerHTML = `<div class="grid two">
      <div class="card"><h2>Почему молчит <span class="muted small">· ближе всего к срабатыванию по вашим порогам</span></h2>
        <p class="muted small">Окно ${th.candle_window_min} мин · амплитуда ≥ ${th.amplitude_atr_min}×ATR${th.amplitude_atr_max ? " (≤ " + th.amplitude_atr_max + ")" : ""}
          · объём ≥ ×${th.volume_mult}${th.level_break_enabled ? " или пробой уровня" : ""} · либо плита 3/4.
          Сработали сейчас: <b>${r.triggered.length}</b>${r.triggered.length ? " — " + r.triggered.map((x) => `<a href="#ticker/${esc(x.ticker)}">${esc(x.ticker)}</a>`).join(", ") : ""}</p>
        <div id="cl"></div>
        <div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Тикер</th><th class="num">Близость</th><th class="num">Амп/ATR</th>
          <th class="num">Объём/ср.</th><th>Чего не хватило</th></tr></thead><tbody>
          ${r.silent.map((x) => `<tr class="click" data-t="${esc(x.ticker)}"><td><b>${esc(x.ticker)}</b></td>
            <td class="num">${closePct(x.closeness, false)}%</td><td class="num">${num(x.amplitude_atr, 2)}</td><td class="num">×${num(x.volume_ratio, 2)}</td>
            <td class="small wrap">${x.checks.filter((c) => !c.passed).map((c) => `${esc(c.label)}: ${num(c.value, 2)} из ${num(c.threshold, 2)}`).join("; ")}</td></tr>`).join("")}
        </tbody></table></div></div>
      <div class="card"><div class="row between"><h2>Журнал решений</h2>
        <select id="cyc">${d.cycles.map((c) => `<option value="${c.cycle_id}" ${c.cycle_id === d.cycle_id ? "selected" : ""}>
          цикл #${c.cycle_id} · ${esc(String(c.ts).slice(5, 16))} · ${c.passed}/${c.n}</option>`).join("")}</select></div>
        <p class="muted small">Строка на тикер × цикл, глобальные пороги админа.</p><div id="dec"></div></div></div>`;
    TB.bars($("#cl"), r.silent.slice(0, 15).map((x) => ({ label: x.ticker, value: x.closeness, max: 1,
      text: closePct(x.closeness, false) + "%", onclick: () => go("ticker/" + x.ticker) })), { max: 1 });
    $$("tr[data-t]", view).forEach((tr) => tr.addEventListener("click", () => go("ticker/" + tr.dataset.t)));
    const drawDec = (rows) => {
      $("#dec").innerHTML = rows.length ? `<div class="table-wrap"><table><thead><tr><th>Тикер</th><th>Решение</th><th class="num">Близость</th>
        <th>Не прошло</th></tr></thead><tbody>${rows.map((x) => `<tr><td><a href="#ticker/${esc(x.ticker)}">${esc(x.ticker)}</a></td>
        <td>${x.passed ? '<span class="tag up">прошёл</span>' : '<span class="tag">нет</span>'}</td><td class="num">${closePct(x.closeness, x.passed)}%</td>
        <td class="small">${x.passed ? '<span class="muted">—</span>' : esc(condLabels(x.failed))}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">Журнал пуст — дождитесь цикла</div>';
    };
    drawDec(d.decisions);
    if ($("#cyc")) $("#cyc").addEventListener("change", async () => drawDec((await api("/api/decisions?cycle_id=" + $("#cyc").value)).decisions));
  }

  // ================================================================ Paper
  async function paper() {
    const r = await api("/api/paper");
    const st = r.stats;
    const ps = r.settings || {};
    const rub = (v) => (v === null || v === undefined ? '<span class="muted">—</span>'
      : `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${v > 0 ? "+" : ""}${num(v, 2)} ₽</span>`);
    view.innerHTML = `<div class="card"><h2>Paper trading <span class="muted small">· симуляция сделок: сопровождение, пуши на выход, профит в ₽</span></h2>
      <div class="stats">${stat("Открыто", st.open)}${stat("Закрыто", st.closed)}${stat("Прибыльных", st.wins)}
        ${stat("Винрейт", st.winrate === null ? "—" : num(st.winrate, 1) + "%")}${stat("Сумма результатов", pct(st.total_pct))}
        ${stat("Профит закрытых", rub(st.total_rub))}${stat("Плавающий профит", rub(st.open_rub))}</div>
      <p class="small muted">Авто-закрытие по стопу/тейку: <b>${ps.paper_auto_close ? "включено" : "выключено — закрываете вручную по пушам"}</b> ·
        пуши сопровождения: <b>${ps.paper_notify_enabled ? "включены" : "выключены"}</b> · цель: <b>${num(ps.paper_target_pct)}%</b>${ps.paper_target_rub ? ` или <b>${num(ps.paper_target_rub)} ₽</b>` : ""}
        · <a href="#settings">изменить</a>. Пуши сопровождения не учитывают дневной лимит.</p>
      <div class="row"><input id="pp-t" placeholder="Тикер" style="width:110px"><select id="pp-side"><option value="long">long</option><option value="short">short</option></select>
        <input id="pp-q" type="number" min="0" step="any" placeholder="Кол-во, шт (пусто — 1 лот)" style="width:190px">
        <input id="pp-e" placeholder="Цена входа (пусто — текущая)" style="width:200px"><input id="pp-s" placeholder="Стоп (пусто — от ATR)" style="width:170px">
        <input id="pp-k" placeholder="Тейк (пусто — от ATR)" style="width:170px"><button id="pp-go" class="primary">Открыть</button></div></div>
      <div class="card">${r.trades.length ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>Тикер</th><th>Сторона</th><th class="num">Кол-во, шт</th><th class="num">Вход</th>
        <th class="num">Стоп</th><th class="num">Тейк</th><th class="num">Текущая/выход</th><th class="num">Результат</th><th class="num">Профит, ₽</th><th>Статус</th><th>Открыта</th><th></th></tr></thead><tbody>
        ${r.trades.map((t) => `<tr><td class="muted">${t.id}</td><td><a href="#ticker/${esc(t.ticker)}"><b>${esc(t.ticker)}</b></a></td><td>${esc(t.side)}</td>
          <td class="num"><input class="qty" type="number" min="0" step="any" data-qty="${t.id}" value="${t.qty ?? ""}" placeholder="—" title="Количество акций${t.lot ? " (лот " + t.lot + ")" : ""}"></td>
          <td class="num">${num(t.entry_price)}</td><td class="num">${num(t.stop)}</td><td class="num">${num(t.take)}</td>
          <td class="num">${num(t.status === "open" ? t.current_price : t.exit_price)}</td>
          <td class="num">${pct(t.status === "open" ? t.unrealized_pct : t.result_pct)}</td>
          <td class="num">${rub(t.status === "open" ? t.unrealized_rub : t.profit_rub)}</td>
          <td>${t.status === "open" ? '<span class="tag accent">открыта</span>' : `<span class="tag">${esc({ stop: "стоп", take: "тейк", manual: "вручную" }[t.status] || t.status)}</span>`}
            ${Object.keys(t.notified || {}).length ? `<span class="muted small" title="${esc(Object.keys(t.notified).join(", "))}">🔔${Object.keys(t.notified).length}</span>` : ""}</td>
          <td class="small mono">${esc(String(t.opened_at).slice(5, 16))}</td>
          <td>${t.status === "open" ? `<button class="small" data-close="${t.id}">Закрыть</button>` : ""}
            <button class="small ghost" data-del="${t.id}">✕</button></td></tr>`).join("")}</tbody></table></div>
          <p class="muted small">Количество можно дописать в любой момент (и у закрытых сделок) — профит в ₽ пересчитается.</p>`
        : '<div class="empty">Сделок нет. Откройте кнопкой «Paper» в таблице «Рынок» / карточке акции, по сигналу или вручную.</div>'}</div>`;
    $("#pp-go").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const body = { ticker: $("#pp-t").value.trim(), side: $("#pp-side").value };
      if ($("#pp-q").value) body.qty = +$("#pp-q").value.replace(",", ".");
      if ($("#pp-e").value) body.entry_price = +$("#pp-e").value.replace(",", ".");
      if ($("#pp-s").value) body.stop = +$("#pp-s").value.replace(",", ".");
      if ($("#pp-k").value) body.take = +$("#pp-k").value.replace(",", ".");
      await api("/api/paper", { method: "POST", body });
      paper();
    }));
    $$("[data-qty]").forEach((inp) => inp.addEventListener("change", async () => {
      try {
        await api(`/api/paper/${inp.dataset.qty}`, { method: "PATCH", body: { qty: inp.value === "" ? null : +inp.value.replace(",", ".") } });
        toast("Количество сохранено");
        paper();
      } catch (e) { toast(e.message, true); }
    }));
    $$("[data-close]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
      await api(`/api/paper/${b.dataset.close}/close`, { method: "POST", body: {} }); paper();
    })));
    $$("[data-del]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
      await api(`/api/paper/${b.dataset.del}`, { method: "DELETE" }); paper();
    })));
  }

  // ============================================================ Метрики
  async function metrics() {
    view.innerHTML = `<div class="card"><h2>Планировщик запросов ISS</h2><div id="m-sched"><span class="spin"></span></div></div>
      <div class="card"><div class="row between"><h2>Метрика по всем тикерам</h2>
        <div class="row"><select id="m-name"><option value="threshold_closeness">Близость к порогу</option>
          <option value="threshold_distance">Расстояние до порога</option><option value="amplitude_atr">Амплитуда/ATR</option>
          <option value="volume_ratio">Объём / средний</option><option value="impulse_age_min">Возраст импульса</option></select>
          <select id="m-h"><option value="6">6 ч</option><option value="24" selected>24 ч</option><option value="72">3 дня</option><option value="168">7 дней</option></select></div></div>
        <div id="m-all"></div></div>`;
    const loadSched = async () => {
      const m = await api("/api/scheduler");
      const q = m.queue || {};
      $("#m-sched").innerHTML = `<div class="stats">
        ${stat("Очередь P0 · детали триггеров", q.P0 || 0)}${stat("Очередь P1 · хвосты свечей", q.P1 || 0)}${stat("Очередь P2 · фон", q.P2 || 0)}
        ${stat("Запросов/с (за минуту)", num(m.requests_per_sec, 2) + ` <span class="muted small">лимит ${m.rate_limit_rps}</span>`)}
        ${stat("Среднее время ответа", m.avg_response_ms === null ? "—" : num(m.avg_response_ms, 0) + " мс")}
        ${stat("HTTP 429 за час", m.http_429_last_hour)}${stat("Отставание P1", num(m.p1_lag_sec, 0) + " с")}
        ${stat("Повторы / ошибки", `${m.retries} / ${m.failed}`)}${stat("Кэш: записей · попаданий", `${m.cache.items} · ${m.cache.hits}`)}
        ${stat("Стакан ISS", m.orderbook_available ? "доступен" : "нет доступа")}</div>
        <div class="grid two"><div><h3>Выполнено по приоритетам</h3>${kv(Object.entries(m.completed).map(([k, v]) => [k, v]))}
          <h3>Сейчас</h3><p class="mono small">${esc(m.current || "простой")}</p>
          ${m.p1_cycle ? `<h3>Последний цикл P1</h3>${kv([["Цикл", "#" + m.p1_cycle.cycle_id], ["Начат", esc(m.p1_cycle.started_at)],
            ["Длительность", num(m.p1_cycle.duration_sec, 0) + " с"], ["Бумаг", m.p1_cycle.tickers]])}` : ""}</div>
          <div><h3>Порядок последних задач</h3><ul class="plain small mono" style="max-height:300px;overflow:auto">${m.recent_jobs.map((j) =>
            `<li><span class="tag ${j.priority === "P0" ? "accent" : ""}">${j.priority}</span> ${esc(j.name)}</li>`).join("")}</ul>
          ${m.recent_errors.length ? `<h3>Ошибки</h3><ul class="plain small">${m.recent_errors.map((e) => `<li class="error">${esc(e.error)}</li>`).join("")}</ul>` : ""}</div></div>`;
    };
    const loadAll = async () => {
      const name = $("#m-name").value;
      const r = await api(`/api/metrics/${name}?hours=${$("#m-h").value}`);
      const rows = r.tickers.slice().sort((a, b) => (b.last || 0) - (a.last || 0)).slice(0, 40);
      const isPct = name === "threshold_closeness";
      const box = $("#m-all");
      box.innerHTML = '<div id="m-bars"></div><h3>Динамика топ-5</h3><div id="m-lines"></div>';
      TB.bars($("#m-bars"), rows.map((x) => ({ label: x.ticker, value: isPct ? x.last * 100 : x.last,
        text: isPct ? closePct(x.last) + "%" : num(x.last, 2), title: "обновлено " + x.ts, onclick: () => go("ticker/" + x.ticker) })),
        isPct ? { max: 100 } : {});
      // топ-5 на одном графике по общей оси времени
      const top = rows.slice(0, 5);
      const allTs = Array.from(new Set(top.flatMap((x) => x.series.map((p) => p[0])))).sort();
      const pal = [C.s1, C.s2, C.s3, C.up, C.down];
      TB.lines($("#m-lines"), allTs, top.map((x, i) => {
        const m = new Map(x.series.map((p) => [p[0], isPct ? p[1] * 100 : p[1]]));
        return { name: x.ticker, values: allTs.map((t) => (m.has(t) ? m.get(t) : null)), color: pal[i] };
      }), { height: 160, empty: "Метрика появится после циклов сканера" });
    };
    $("#m-name").addEventListener("change", loadAll);
    $("#m-h").addEventListener("change", loadAll);
    await Promise.all([loadSched(), loadAll()]);
    S.viewTimer = setInterval(() => { if (!document.hidden) loadSched().catch(() => {}); }, 5000);
  }

  // ========================================================== Настройки
  function fieldHtml(sp, value, overridden) {
    const id = "f-" + sp.key;
    const help = sp.help ? `<span class="help">${esc(sp.help)}</span>` : "";
    const range = [sp.min !== null ? "мин " + sp.min : "", sp.max !== null ? "макс " + sp.max : ""].filter(Boolean).join(", ");
    const cls = `field ${overridden ? "overridden" : ""}`;
    if (sp.type === "bool") {
      return `<label class="${cls} check"><input type="checkbox" id="${id}" data-key="${sp.key}" ${value ? "checked" : ""}><span>${esc(sp.label)}</span>${help}</label>`;
    }
    if (sp.type === "list") {
      return `<div class="${cls} wide"><span>${esc(sp.label)}</span><div class="choices" data-key="${sp.key}" data-list="1">
        ${Object.entries(sp.choices).map(([k, l]) => `<label><input type="checkbox" value="${k}" ${(value || []).includes(k) ? "checked" : ""}> ${esc(l)}</label>`).join("")}
        </div>${help}</div>`;
    }
    const type = sp.type === "time" ? "time" : sp.type === "str" ? "text" : "number";
    const step = sp.type === "float" ? "any" : "1";
    return `<label class="${cls} ${sp.type === "str" ? "wide" : ""}"><span>${esc(sp.label)}</span>
      <input id="${id}" data-key="${sp.key}" type="${type}" step="${step}" ${sp.min !== null ? `min="${sp.min}"` : ""} ${sp.max !== null ? `max="${sp.max}"` : ""}
        value="${esc(value)}">${help || (range ? `<span class="help">${range}</span>` : "")}</label>`;
  }
  function formHtml(schema, values, overrides, scopeFilter) {
    const groups = {};
    schema.filter(scopeFilter).forEach((sp) => (groups[sp.group] = groups[sp.group] || []).push(sp));
    return Object.entries(groups).map(([g, items]) => `<fieldset><legend>${esc(g)}</legend>
      ${items.map((sp) => fieldHtml(sp, values[sp.key], overrides && sp.key in overrides)).join("")}</fieldset>`).join("");
  }
  function collect(root, schema) {
    const out = {};
    schema.forEach((sp) => {
      const n = $(`[data-key="${sp.key}"]`, root);
      if (!n) return;
      if (n.dataset.list) out[sp.key] = $$("input", n).filter((x) => x.checked).map((x) => x.value);
      else if (sp.type === "bool") out[sp.key] = n.checked;
      else out[sp.key] = n.value;
    });
    return out;
  }
  function changed(values, base) {
    const out = {};
    for (const [k, v] of Object.entries(values)) if (JSON.stringify(v) !== JSON.stringify(base[k]) && String(v) !== String(base[k])) out[k] = v;
    return out;
  }

  async function settings() {
    S.settings = await api("/api/settings");
    const st = S.settings;
    const me = await api("/api/me");
    view.innerHTML = `<div class="grid two">
      <div class="card"><h2>Push-уведомления</h2>
        <p class="small">Сервер: ${me.push_available ? '<span class="up">VAPID готов</span>' : `<span class="error">${esc(me.push_error || "недоступен")}</span>`} ·
          устройств подписано: <b id="dev">${me.devices}</b></p>
        <p class="small muted" id="push-env"></p>
        <div class="row"><button id="sub" class="primary">Подписать это устройство</button><button id="unsub">Отписать</button></div>
        <h3>Тестовый пуш</h3>
        <div class="row"><label class="small">Таймер, сек <input id="t-delay" type="number" min="0" step="0.5" style="width:80px"
          placeholder="${esc(st.global ? st.global.test_push_delay_sec : 1)}"></label>
          <button id="t-push">Отправить себе</button><span id="t-cd" class="countdown"></span></div>
        <p class="muted small">Нажмите и сверните браузер/заблокируйте телефон, пока идёт таймер.</p></div>
      <div class="card"><h2>Формат пуша</h2>
        <p class="muted small">Поля: {ticker} {direction_label} {arrow} {score} {amplitude_atr} {volume_ratio} {level_break} {reason} {action_label} {impulse_age_min} {price} {ts}</p>
        <label class="field"><span>Заголовок</span><input id="tpl-t" value="${esc(st.values.push_title_template)}"></label>
        <label class="field" style="margin-top:8px"><span>Текст</span><textarea id="tpl-b">${esc(st.values.push_body_template)}</textarea></label>
        <div class="row" style="margin-top:8px"><button id="tpl-prev">Предпросмотр</button><button id="tpl-save" class="primary">Сохранить формат</button></div>
        <div id="tpl-out" class="card tight" style="margin-top:10px" hidden></div></div></div>
      <div class="card"><div class="row between"><h2>Мои пороги и дашборд</h2>
        <span class="muted small">Не заданные вами значения берутся из глобальных настроек админа. «• свой» — переопределено.</span></div>
        <div id="sf">${formHtml(st.schema, st.values, st.overrides, (sp) => sp.scope === "user")}</div>
        <div class="row"><button id="s-save" class="primary">Сохранить</button><button id="s-reset" class="danger">Сбросить к глобальным</button></div></div>`;
    pushEnvNote();
    $("#s-save").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const userSchema = st.schema.filter((sp) => sp.scope === "user");
      const vals = changed(collect($("#sf"), userSchema), st.values);
      if (!Object.keys(vals).length) { toast("Изменений нет"); return; }
      const r = await api("/api/settings", { method: "PUT", body: { values: vals } });
      S.settings.values = r.values; S.settings.overrides = r.overrides;
      toast("Сохранено: " + Object.keys(vals).length + " параметров");
      settings();
    }));
    $("#s-reset").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      if (!confirm("Сбросить все свои настройки к глобальным?")) return;
      await api("/api/settings", { method: "DELETE" }); toast("Сброшено"); settings();
    }));
    $("#tpl-prev").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const p = await api("/api/push/preview", { method: "POST", body: { push_title_template: $("#tpl-t").value, push_body_template: $("#tpl-b").value } });
      const o = $("#tpl-out"); o.hidden = false; o.innerHTML = `<b>${esc(p.title)}</b><div class="small">${esc(p.body)}</div>`;
    }));
    $("#tpl-save").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      await api("/api/settings", { method: "PUT", body: { values: { push_title_template: $("#tpl-t").value, push_body_template: $("#tpl-b").value } } });
      toast("Формат пуша сохранён");
    }));
    $("#sub").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const n = await subscribePush(me.vapid_public_key);
      $("#dev").textContent = n; toast("Устройство подписано");
    }));
    $("#unsub").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = reg && (await reg.pushManager.getSubscription());
      const r = await api("/api/push/unsubscribe", { method: "POST", body: sub ? { endpoint: sub.endpoint } : {} });
      if (sub) await sub.unsubscribe();
      $("#dev").textContent = r.devices; toast("Отписано: " + r.removed);
    }));
    $("#t-push").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const d = $("#t-delay").value;
      const r = await api("/api/push/test", { method: "POST", body: d === "" ? {} : { delay_sec: +d } });
      countdown($("#t-cd"), r.scheduled_in_sec, r.devices ? `отправлено на ${r.devices} устр.` : "нет подписанных устройств");
    }));
  }
  function countdown(el, sec, done) {
    const end = Date.now() + sec * 1000;
    clearInterval(el._t);
    const tick = () => {
      const left = Math.max(0, (end - Date.now()) / 1000);
      el.textContent = left > 0 ? left.toFixed(1) + " с" : "✓ " + done;
      if (left <= 0) clearInterval(el._t);
    };
    tick();
    el._t = setInterval(tick, 100);
  }

  // ----------------------------------------------------------- PWA / пуши
  function registerSW() {
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  function pushEnvNote() {
    const el = $("#push-env");
    if (!el) return;
    const notes = [];
    if (!window.isSecureContext) notes.push("Пуши работают только по HTTPS (или на localhost).");
    if (!("PushManager" in window)) notes.push("Браузер не поддерживает Web Push. На iPhone: «Поделиться → На экран Домой», затем откройте приложение с иконки.");
    if ("Notification" in window) notes.push("Разрешение уведомлений: " + ({ granted: "выдано", denied: "запрещено в браузере", default: "не запрошено" }[Notification.permission]));
    el.textContent = notes.join(" ");
  }
  function b64ToBytes(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }
  async function subscribePush(vapid) {
    if (!vapid) throw new Error("На сервере нет VAPID-ключей");
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) throw new Error("Браузер не поддерживает Web Push");
    const perm = await Notification.requestPermission();
    if (perm !== "granted") throw new Error("Уведомления не разрешены");
    const reg = await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(vapid) });
    const r = await api("/api/push/subscribe", { method: "POST", body: { subscription: sub.toJSON() } });
    return r.devices;
  }

  // ============================================================== Админ
  async function admin() {
    if (!S.me.is_admin) { go("market"); return; }
    const [keys, st] = await Promise.all([api("/api/admin/keys"), api("/api/settings")]);
    view.innerHTML = `<div class="grid two">
      <div class="card"><h2>Ключи доступа</h2>
        <div class="row"><input id="k-label" placeholder="Метка (кто это)" style="width:220px"><button id="k-new" class="primary">Сгенерировать ключ</button></div>
        <div id="k-secret"></div><div id="k-list" style="margin-top:12px"></div></div>
      <div class="card"><h2>Управление</h2>
        <div class="row"><button id="a-scan" class="primary">Запустить цикл сканера сейчас</button>
          <button id="a-purge" title="Удалить данные старше срока хранения (настройка «Хранить историю, дней»)">Очистить старше ${esc(st.global.retention_days)} дн.</button>
          <button id="a-stop" class="danger">Остановить сервер (как «0» в консоли)</button></div>
        <h3>Тестовый пуш</h3>
        <div class="row"><select id="a-target"><option value="">Себе</option><option value="all">Всем активным ключам</option>
          ${keys.keys.filter((k) => k.role !== "admin").map((k) => `<option value="${k.id}">#${k.id} ${esc(k.label || k.hint)} (${k.devices} устр.)</option>`).join("")}</select>
          <label class="small">Таймер, сек <input id="a-delay" type="number" min="0" step="0.5" style="width:80px" value="${esc(st.global.test_push_delay_sec)}"></label>
          <button id="a-push">Отправить</button><span id="a-cd" class="countdown"></span></div>
        <input id="a-title" placeholder="Заголовок (необязательно)" style="width:100%;margin-top:8px">
        <input id="a-body" placeholder="Текст (необязательно)" style="width:100%;margin-top:8px"></div></div>
      <div class="card"><div class="row between"><h2>Глобальные настройки</h2>
        <span class="muted small">Значения по умолчанию для всех пользователей + параметры сканера (только админ)</span></div>
        <div id="gf">${formHtml(st.schema, st.global, null, () => true)}</div>
        <div class="row"><button id="g-save" class="primary">Сохранить</button>
          <label class="small"><input type="checkbox" id="g-scan"> и сразу запустить цикл</label></div></div>`;
    const drawKeys = (list) => {
      $("#k-list").innerHTML = `<div class="table-wrap"><table><thead><tr><th>#</th><th>Метка</th><th>Ключ</th><th>Роль</th><th>Статус</th>
        <th class="num">Устр.</th><th>Последний вход</th><th></th></tr></thead><tbody>${list.map((k) => `<tr>
        <td class="muted">${k.id}</td><td>${esc(k.label || "")}</td><td class="mono">${esc(k.hint)}</td><td>${k.role === "admin" ? "админ" : "пользователь"}</td>
        <td>${k.active ? '<span class="tag up">активен</span>' : '<span class="tag down">деактивирован</span>'}</td><td class="num">${k.devices}</td>
        <td class="small mono">${esc(k.last_used_at || "—")}</td>
        <td>${k.role === "admin" ? "" : `<button class="small" data-act="${k.id}" data-on="${k.active ? 0 : 1}">${k.active ? "Деактивировать" : "Активировать"}</button>
          <button class="small ghost" data-kdel="${k.id}">Удалить</button>`}</td></tr>`).join("")}</tbody></table></div>`;
      $$("[data-act]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
        drawKeys((await api("/api/admin/keys/" + b.dataset.act, { method: "PATCH", body: { active: b.dataset.on === "1" } })).keys);
      })));
      $$("[data-kdel]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
        if (!confirm("Удалить ключ навсегда? Его подписки и настройки тоже будут удалены.")) return;
        drawKeys((await api("/api/admin/keys/" + b.dataset.kdel, { method: "DELETE" })).keys);
      })));
    };
    drawKeys(keys.keys);
    $("#k-new").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const k = await api("/api/admin/keys", { method: "POST", body: { label: $("#k-label").value } });
      $("#k-secret").innerHTML = `<p class="small">Новый ключ — передайте пользователю. Он показывается <b>один раз</b>:</p><div class="secret">${esc(k.key)}</div>`;
      $("#k-label").value = "";
      drawKeys((await api("/api/admin/keys")).keys);
    }));
    $("#a-scan").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      await api("/api/admin/scan-now", { method: "POST" }); toast("Цикл поставлен в очередь"); setTimeout(refreshStatus, 800);
    }));
    $("#a-purge").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      if (!confirm(`Удалить свечи, метрики, сигналы и журналы старше ${st.global.retention_days} дн.?`)) return;
      const r = await api("/api/admin/purge", { method: "POST" });
      toast("Удалено: " + Object.entries(r.deleted || {}).map(([k, v]) => `${k} ${v}`).join(", "));
    }));
    $("#a-stop").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      if (!confirm("Остановить сервер и сканер?")) return;
      await api("/api/admin/stop", { method: "POST" }); toast("Сервер останавливается…");
    }));
    $("#a-push").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const tg = $("#a-target").value;
      const body = { delay_sec: $("#a-delay").value === "" ? undefined : +$("#a-delay").value };
      if (tg === "all") body.all = true; else if (tg) body.key_id = +tg;
      if ($("#a-title").value) body.title = $("#a-title").value;
      if ($("#a-body").value) body.body = $("#a-body").value;
      const r = await api("/api/push/test", { method: "POST", body });
      countdown($("#a-cd"), r.scheduled_in_sec, `${r.targets.length} ключ(ей), ${r.devices} устр.` + (r.push_available ? "" : " — VAPID недоступен"));
    }));
    $("#g-save").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const vals = changed(collect($("#gf"), st.schema), st.global);
      if (!Object.keys(vals).length && !$("#g-scan").checked) { toast("Изменений нет"); return; }
      const r = await api("/api/admin/settings", { method: "PUT", body: { values: vals, scan_now: $("#g-scan").checked } });
      st.global = r.global;
      S.settings = await api("/api/settings");
      toast("Глобальные настройки сохранены");
    }));
  }

  boot();
})();
