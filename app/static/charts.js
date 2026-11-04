/* Лёгкие SVG-графики без библиотек (п. 28).
 * Палитра (тёмная поверхность #0d1220) проверена validate_palette.js:
 *   серии: #3987e5 (синий), #d95926 (оранжевый), #9085e9 (фиолетовый)
 *   свечи: рост #199e70 — полая свеча, падение #e66767 — залитая
 *   (CVD ΔE 6.5 между ними — поэтому форма полая/залитая обязательна).
 * Одна ось Y на график, легенда при ≥2 сериях, перекрестие + подсказка.
 */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const C = {
    s1: "#3987e5", s2: "#d95926", s3: "#9085e9", up: "#199e70", down: "#e66767",
    grid: "#1c2333", axis: "#2b3448", muted: "#8a93a6", ink: "#e8ecf4", ref: "#5b6478",
  };
  const PAD = { l: 8, r: 58, t: 8, b: 20 };

  function el(name, attrs, parent) {
    const n = document.createElementNS(NS, name);
    for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function fmt(v, nd) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const a = Math.abs(v);
    if (nd === undefined) nd = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : 4;
    return Number(v).toLocaleString("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: nd });
  }

  function niceTicks(min, max, n) {
    if (!(isFinite(min) && isFinite(max))) return [];
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;
    const step0 = span / Math.max(n, 1);
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + 1e-12; v += step) out.push(+v.toFixed(10));
    return out;
  }

  function shortTs(ts) {
    // 'YYYY-MM-DD HH:MM:SS' → 'DD.MM HH:MM'
    if (!ts) return "";
    return ts.slice(8, 10) + "." + ts.slice(5, 7) + " " + ts.slice(11, 16);
  }

  /** Каркас: svg, шкалы, сетка, ось времени, подсказка. */
  function frame(container, opts) {
    container.innerHTML = "";
    container.classList.add("chart");
    const W = container.clientWidth || 640;
    const H = opts.height || 140;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
      "aria-label": opts.title || "график" }, container);
    const n = opts.n;
    const pw = W - PAD.l - PAD.r, ph = H - PAD.t - PAD.b;
    const x = (i) => PAD.l + (n <= 1 ? pw / 2 : (i / (n - 1)) * pw);
    let lo = opts.min, hi = opts.max;
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = opts.fixed ? 0 : (hi - lo) * 0.06;
    lo -= pad; hi += pad;
    const y = (v) => PAD.t + (1 - (v - lo) / (hi - lo)) * ph;
    const g = el("g", {}, svg);
    for (const t of niceTicks(lo, hi, opts.ticks || 4)) {
      el("line", { x1: PAD.l, x2: PAD.l + pw, y1: y(t), y2: y(t), stroke: C.grid, "stroke-width": 1 }, g);
      const tx = el("text", { x: W - PAD.r + 6, y: y(t) + 4, fill: C.muted, "font-size": 10, class: "tick" }, g);
      tx.textContent = fmt(t);
    }
    el("line", { x1: PAD.l, x2: PAD.l + pw, y1: PAD.t + ph, y2: PAD.t + ph, stroke: C.axis }, g);
    // подписи времени: ~5 штук
    const labels = opts.labels || [];
    const every = Math.max(1, Math.ceil(n / 5));
    for (let i = 0; i < n; i += every) {
      const tx = el("text", { x: x(i), y: H - 5, fill: C.muted, "font-size": 10, "text-anchor": "middle", class: "tick" }, g);
      tx.textContent = shortTs(labels[i]);
    }
    return { svg, W, H, x, y, pw, ph, n, lo, hi };
  }

  function refLine(f, v, label) {
    if (v < f.lo || v > f.hi) return;
    el("line", { x1: PAD.l, x2: PAD.l + f.pw, y1: f.y(v), y2: f.y(v), stroke: C.ref, "stroke-dasharray": "3 4" }, f.svg);
    if (label) {
      const t = el("text", { x: PAD.l + 4, y: f.y(v) - 3, fill: C.muted, "font-size": 10 }, f.svg);
      t.textContent = label;
    }
  }

  function path(f, values, color, width, dash) {
    let d = "", pen = false;
    values.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      d += (pen ? "L" : "M") + f.x(i).toFixed(1) + "," + f.y(v).toFixed(1);
      pen = true;
    });
    if (d) el("path", { d, fill: "none", stroke: color, "stroke-width": width || 2, "stroke-linejoin": "round",
      "stroke-linecap": "round", "stroke-dasharray": dash || "" }, f.svg);
  }

  function legend(container, items) {
    if (items.length < 2) return;
    const lg = document.createElement("div");
    lg.className = "legend";
    lg.innerHTML = items.map((it) =>
      `<span><i style="background:${it.color}${it.dash ? ";height:0;border-top:2px dashed " + it.color : ""}"></i>${it.name}</span>`).join("");
    container.prepend(lg);
  }

  /** Перекрестие + подсказка; rows(i) → [[имя, значение, цвет]] */
  function hover(container, f, labels, rows) {
    const line = el("line", { y1: PAD.t, y2: PAD.t + f.ph, stroke: C.muted, "stroke-width": 1, visibility: "hidden" }, f.svg);
    const tip = document.createElement("div");
    tip.className = "tip";
    tip.hidden = true;
    container.appendChild(tip);
    const hit = el("rect", { x: PAD.l, y: 0, width: f.pw, height: f.H, fill: "transparent" }, f.svg);
    function move(ev) {
      const r = f.svg.getBoundingClientRect();
      const px = ((ev.clientX - r.left) / r.width) * f.W;
      const i = Math.max(0, Math.min(f.n - 1, Math.round(((px - PAD.l) / f.pw) * (f.n - 1))));
      line.setAttribute("x1", f.x(i)); line.setAttribute("x2", f.x(i));
      line.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.innerHTML = `<b>${shortTs(labels[i])}</b>` + rows(i).map(([k, v, c]) =>
        `<div><i style="background:${c || "transparent"}"></i>${k}<span>${typeof v === "string" ? v : fmt(v)}</span></div>`).join("");
      const left = (f.x(i) / f.W) * r.width;
      tip.style.left = Math.min(left + 12, r.width - tip.offsetWidth - 4) + "px";
    }
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerleave", () => { tip.hidden = true; line.setAttribute("visibility", "hidden"); });
  }

  function range(arrays) {
    let lo = Infinity, hi = -Infinity;
    for (const a of arrays) for (const v of a) if (v !== null && v !== undefined && isFinite(v)) {
      if (v < lo) lo = v; if (v > hi) hi = v;
    }
    return lo === Infinity ? [0, 1] : [lo, hi];
  }

  /** Свечи + объём (+ полосы Боллинджера поверх). candles: [[ts,o,h,l,c,v]] */
  function candles(container, candles, opts) {
    opts = opts || {};
    const n = candles.length;
    if (!n) { container.innerHTML = '<div class="empty">Нет свечей</div>'; return; }
    const labels = candles.map((c) => c[0]);
    const bb = opts.bb;
    const series = [candles.map((c) => c[2]), candles.map((c) => c[3])];
    if (bb) series.push(bb.upper, bb.lower);
    const [lo, hi] = range(series);
    const f = frame(container, { n, min: lo, max: hi, labels, height: opts.height || 240, title: "Свечи" });
    // объём — нижние 22% поля, своя доля высоты (не вторая ось: подписей нет, только форма)
    const vmax = Math.max(...candles.map((c) => c[5] || 0)) || 1;
    const vh = f.ph * 0.22, base = PAD.t + f.ph;
    const bw = Math.max(1, Math.min(10, (f.pw / n) * 0.7));
    const gv = el("g", { opacity: 0.35 }, f.svg);
    candles.forEach((c, i) => {
      const h = ((c[5] || 0) / vmax) * vh;
      el("rect", { x: f.x(i) - bw / 2, y: base - h, width: bw, height: Math.max(h, 0.5),
        fill: c[4] >= c[1] ? C.up : C.down }, gv);
    });
    if (bb) {
      path(f, bb.upper, C.s1, 1.5);
      path(f, bb.middle, C.s1, 1, "2 3");
      path(f, bb.lower, C.s1, 1.5);
    }
    const g = el("g", {}, f.svg);
    candles.forEach((c, i) => {
      const up = c[4] >= c[1], col = up ? C.up : C.down;
      el("line", { x1: f.x(i), x2: f.x(i), y1: f.y(c[2]), y2: f.y(c[3]), stroke: col, "stroke-width": 1 }, g);
      const top = f.y(Math.max(c[1], c[4])), bot = f.y(Math.min(c[1], c[4]));
      el("rect", { x: f.x(i) - bw / 2, y: top, width: bw, height: Math.max(bot - top, 1),
        fill: up ? "#0d1220" : col, stroke: col, "stroke-width": 1, rx: bw > 4 ? 1 : 0 }, g);
    });
    // последняя цена — прямая подпись
    const last = candles[n - 1][4];
    const t = el("text", { x: f.W - PAD.r + 6, y: f.y(last) + 4, fill: C.ink, "font-size": 11, "font-weight": 600 }, f.svg);
    t.textContent = fmt(last);
    const items = [{ name: "Рост (полая)", color: C.up }, { name: "Падение", color: C.down }];
    if (bb) items.push({ name: "Боллинджер 20/2", color: C.s1 });
    legend(container, items);
    hover(container, f, labels, (i) => {
      const c = candles[i];
      const rows = [["Откр.", c[1]], ["Макс.", c[2]], ["Мин.", c[3]], ["Закр.", c[4], c[4] >= c[1] ? C.up : C.down],
        ["Объём", c[5]]];
      if (bb) rows.push(["BB верх", bb.upper[i], C.s1], ["BB низ", bb.lower[i], C.s1]);
      return rows;
    });
  }

  /** Линии. series: [{name, values, color, dash}], refs: [[v, label]], bars: {values, name} */
  function lines(container, labels, series, opts) {
    opts = opts || {};
    const n = labels.length;
    if (!n || series.every((s) => !s.values.some((v) => v !== null && v !== undefined))) {
      container.innerHTML = `<div class="empty">${opts.empty || "Нет данных"}</div>`;
      return;
    }
    const arrays = series.map((s) => s.values);
    if (opts.bars) arrays.push(opts.bars.values, [0]);
    let [lo, hi] = range(arrays);
    if (opts.min !== undefined) lo = Math.min(lo, opts.min);
    if (opts.max !== undefined) hi = Math.max(hi, opts.max);
    const f = frame(container, { n, min: lo, max: hi, labels, height: opts.height || 120, title: opts.title,
      fixed: opts.fixed });
    for (const [v, label] of opts.refs || []) refLine(f, v, label);
    if (opts.bars) {
      const bw = Math.max(1, Math.min(8, (f.pw / n) * 0.7));
      const zero = f.y(Math.max(lo, Math.min(hi, 0)));
      opts.bars.values.forEach((v, i) => {
        if (v === null || v === undefined) return;
        const yv = f.y(v);
        el("rect", { x: f.x(i) - bw / 2, y: Math.min(yv, zero), width: bw, height: Math.max(Math.abs(zero - yv), 0.5),
          fill: v >= 0 ? C.up : C.down, opacity: 0.7 }, f.svg);
      });
    }
    series.forEach((s) => path(f, s.values, s.color, s.width || 2, s.dash));
    // прямая подпись последнего значения первой серии
    const s0 = series[0].values;
    for (let i = s0.length - 1; i >= 0; i--) if (s0[i] !== null && s0[i] !== undefined) {
      const t = el("text", { x: f.W - PAD.r + 6, y: f.y(s0[i]) + 4, fill: C.ink, "font-size": 11, "font-weight": 600 }, f.svg);
      t.textContent = fmt(s0[i]);
      break;
    }
    const items = series.map((s) => ({ name: s.name, color: s.color, dash: s.dash }));
    if (opts.bars) items.push({ name: opts.bars.name, color: C.up });
    legend(container, items);
    hover(container, f, labels, (i) => {
      const rows = series.map((s) => [s.name, s.values[i], s.color]);
      if (opts.bars) rows.push([opts.bars.name, opts.bars.values[i], (opts.bars.values[i] || 0) >= 0 ? C.up : C.down]);
      return rows;
    });
  }

  /** Серия точек [[ts, value]] → линия по своему времени */
  function points(container, pts, name, opts) {
    pts = pts || [];
    lines(container, pts.map((p) => p[0]), [{ name, values: pts.map((p) => p[1]), color: C.s1 }], opts);
  }

  /** Горизонтальные полоски «расстояние до порога» по всем тикерам */
  function bars(container, rows, opts) {
    opts = opts || {};
    container.innerHTML = "";
    container.classList.add("hbars");
    if (!rows.length) { container.innerHTML = '<div class="empty">Нет данных</div>'; return; }
    const max = opts.max || Math.max(...rows.map((r) => r.value || 0), 1);
    for (const r of rows) {
      const div = document.createElement("div");
      div.className = "hbar";
      const w = Math.max(0, Math.min(1, (r.value || 0) / max)) * 100;
      div.innerHTML = `<span class="hb-label">${r.label}</span><span class="hb-track"><span class="hb-fill" style="width:${w}%;background:${r.color || C.s1}"></span></span><span class="hb-val">${r.text !== undefined ? r.text : fmt(r.value)}</span>`;
      if (r.title) div.title = r.title;
      if (r.onclick) { div.classList.add("click"); div.addEventListener("click", r.onclick); }
      container.appendChild(div);
    }
  }

  window.TBCharts = { candles, lines, points, bars, fmt, shortTs, COLORS: C };
})();
