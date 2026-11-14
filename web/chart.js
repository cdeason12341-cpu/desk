/* Hand-drawn canvas charts, DPR-aware. No numbers are invented: charts draw only what the data has. */
(function () {
  const C = { cyan: "#22d3ee", green: "#22c55e", red: "#ef4444", orange: "#f59e0b", grid: "#1f242b", muted: "#8a93a0", text: "#f5f7fa" };
  function setup(cv, h) {
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const w = cv.clientWidth || cv.parentNode.clientWidth;
    cv.style.height = h + "px";
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    const x = cv.getContext("2d"); x.setTransform(dpr, 0, 0, dpr, 0, 0); x.clearRect(0, 0, w, h);
    return { x, w, h };
  }
  const fmt$ = v => "$" + (Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toFixed(2));
  function fmtP(v) { if (!isFinite(v)) return ""; if (v >= 1) return v.toFixed(4); const e = Math.floor(Math.log10(v)); return v.toFixed(Math.min(12, 2 - e + 1)); }

  /* Home balance line: gradient fill, dashed start line, trade marks that never overlap. */
  function line(cv, points, opts = {}) {
    const { x, w, h } = setup(cv, opts.height || 170);
    const pad = { l: 4, r: 4, t: 14, b: 18 };
    if (!points || points.length < 2) return false;
    const ts = points.map(p => +new Date(p.t)), vs = points.map(p => p.balance);
    const start = opts.start ?? vs[0];
    let lo = Math.min(...vs, start), hi = Math.max(...vs, start);
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    const span = hi - lo; lo -= span * 0.12; hi += span * 0.12;
    const t0 = ts[0], t1 = Math.max(ts[ts.length - 1], t0 + 1);
    const X = t => pad.l + (t - t0) / (t1 - t0) * (w - pad.l - pad.r);
    const Y = v => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);
    const up = vs[vs.length - 1] >= start, col = up ? C.green : C.red;
    // dashed start line
    x.setLineDash([4, 4]); x.strokeStyle = C.muted; x.globalAlpha = .6; x.lineWidth = 1;
    x.beginPath(); x.moveTo(pad.l, Y(start)); x.lineTo(w - pad.r, Y(start)); x.stroke(); x.setLineDash([]); x.globalAlpha = 1;
    // fill
    const g = x.createLinearGradient(0, pad.t, 0, h - pad.b);
    g.addColorStop(0, up ? "rgba(34,197,94,.35)" : "rgba(239,68,68,.35)"); g.addColorStop(1, "rgba(0,0,0,0)");
    x.beginPath(); points.forEach((p, i) => { const px = X(ts[i]), py = Y(vs[i]); i ? x.lineTo(px, py) : x.moveTo(px, py); });
    x.lineTo(X(ts[ts.length - 1]), h - pad.b); x.lineTo(X(ts[0]), h - pad.b); x.closePath(); x.fillStyle = g; x.fill();
    x.beginPath(); points.forEach((p, i) => { const px = X(ts[i]), py = Y(vs[i]); i ? x.lineTo(px, py) : x.moveTo(px, py); });
    x.strokeStyle = col; x.lineWidth = 2; x.lineJoin = "round"; x.stroke();
    // marks (non-overlapping: skip a mark closer than 14px to the previous drawn one)
    let lastX = -1e9;
    (opts.marks || []).forEach(m => {
      const t = +new Date(m.t); if (t < t0 || t > t1) return;
      const px = X(t); if (px - lastX < 14) return; lastX = px;
      let i = ts.findIndex(z => z >= t); if (i < 0) i = ts.length - 1;
      const py = Y(vs[i]);
      x.beginPath(); x.arc(px, py, 4, 0, 7); x.fillStyle = m.color || C.cyan; x.fill(); x.strokeStyle = "#000"; x.lineWidth = 2; x.stroke();
    });
    x.fillStyle = C.muted; x.font = "11px system-ui"; x.textAlign = "left";
    x.fillText("start " + fmt$(start), pad.l + 2, Math.max(10, Y(start) - 4));
    return true;
  }

  /* Coin candles: 1-min OHLC + volume bars + orange dashed Risk trailing stop line + marks. */
  function candles(cv, cs, opts = {}) {
    const { x, w, h } = setup(cv, opts.height || 240);
    if (!cs || !cs.length) return false;
    const pad = { l: 4, r: 58, t: 10, b: 8 }, vh = 36;
    const ph = h - pad.t - pad.b - vh - 6;
    let lo = Math.min(...cs.map(c => c.l)), hi = Math.max(...cs.map(c => c.h));
    if (opts.riskLine) { lo = Math.min(lo, opts.riskLine); hi = Math.max(hi, opts.riskLine); }
    if (hi - lo <= 0) { hi *= 1.01; lo *= 0.99; }
    const sp = hi - lo; hi += sp * .06; lo -= sp * .06;
    const n = cs.length, cw = (w - pad.l - pad.r) / Math.max(n, 20);
    const X = i => pad.l + (i + .5) * cw, Y = v => pad.t + (1 - (v - lo) / (hi - lo)) * ph;
    x.strokeStyle = C.grid; x.lineWidth = 1;
    for (let k = 0; k <= 3; k++) { const yy = pad.t + k * ph / 3; x.beginPath(); x.moveTo(pad.l, yy); x.lineTo(w - pad.r, yy); x.stroke();
      x.fillStyle = C.muted; x.font = "10px ui-monospace,monospace"; x.textAlign = "left"; x.fillText(fmtP(hi - k * (hi - lo) / 3), w - pad.r + 4, yy + 3); }
    const vmax = Math.max(...cs.map(c => c.volM5 || 0), 1);
    cs.forEach((c, i) => {
      const upc = c.c >= c.o, col = upc ? C.green : C.red;
      x.strokeStyle = col; x.fillStyle = col; x.lineWidth = 1;
      x.beginPath(); x.moveTo(X(i), Y(c.h)); x.lineTo(X(i), Y(c.l)); x.stroke();
      const yb = Y(Math.max(c.o, c.c)), hb = Math.max(1, Math.abs(Y(c.o) - Y(c.c)));
      x.fillRect(X(i) - cw * .35, yb, cw * .7, hb);
      const vv = (c.volM5 || 0) / vmax * vh; x.globalAlpha = .35;
      x.fillRect(X(i) - cw * .35, h - pad.b - vv, cw * .7, vv); x.globalAlpha = 1;
    });
    if (opts.riskLine) {
      const yy = Y(opts.riskLine); x.setLineDash([6, 4]); x.strokeStyle = C.orange; x.lineWidth = 1.5;
      x.beginPath(); x.moveTo(pad.l, yy); x.lineTo(w - pad.r, yy); x.stroke(); x.setLineDash([]);
      x.fillStyle = C.orange; x.font = "10px system-ui"; x.textAlign = "left"; x.fillText("Risk trailing stop", pad.l + 4, yy - 4);
    }
    let lastX = -1e9;
    (opts.marks || []).forEach(m => {
      const t = +new Date(m.t); let i = cs.findIndex(c => +new Date(c.t) + 60000 > t); if (i < 0) i = n - 1;
      const px = X(i); if (px - lastX < 16) return; lastX = px;
      const py = Y(m.price); x.fillStyle = m.kind === "buy" ? C.cyan : C.orange;
      x.beginPath(); x.moveTo(px, py + (m.kind === "buy" ? 10 : -10)); x.lineTo(px - 5, py + (m.kind === "buy" ? 17 : -17)); x.lineTo(px + 5, py + (m.kind === "buy" ? 17 : -17)); x.fill();
      x.font = "10px system-ui"; x.textAlign = "center"; x.fillText(m.label || "", px, py + (m.kind === "buy" ? 28 : -20));
    });
    return true;
  }

  /* Score ring 0-100: turns green from min. */
  function ring(cv, score, min, size = 64) {
    cv.style.width = size + "px"; const { x } = setup(cv, size);
    const r = size / 2 - 5, c = size / 2;
    x.lineWidth = 6; x.strokeStyle = C.grid; x.beginPath(); x.arc(c, c, r, 0, 7); x.stroke();
    if (score == null) return;
    x.strokeStyle = score >= min ? C.green : (score >= min - 15 ? C.orange : C.red); x.lineCap = "round";
    x.beginPath(); x.arc(c, c, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.max(0, Math.min(100, score)) / 100); x.stroke();
    x.fillStyle = C.text; x.font = "700 18px system-ui"; x.textAlign = "center"; x.textBaseline = "middle"; x.fillText(String(score), c, c + 1);
  }
  window.Charts = { line, candles, ring, fmtP };
})();
