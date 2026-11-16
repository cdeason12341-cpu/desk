/* Grok Bot desk app (Plan B, read-only). Decrypts the desk's published JSON with WebCrypto.
   Never builds FOMO URLs: every link comes from coin_url / link / profile_url / SIGNUP_URL in the data. */
(function () {
  "use strict";
  const POLL_S = 30, LIVE_POLL_S = 15, SALT = "grok-bot-desk/v1/salt", ITER = 200000;
  const AGENTS = ["scanner", "auditor", "analyst", "executor", "risk"];
  const CORE = ["state.json", "chart.json", "activity.json", "account.json", "scores.json", "feed.json", "trades.json", "scanner.json", ...AGENTS.map(a => `agents/${a}.json`)];
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const enc = new TextEncoder();
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------- credentials from the fragment, once
  (function readFragment() {
    const h = location.hash.replace(/^#/, "");
    if (h.includes("t=") && h.includes("d=")) {
      const p = new URLSearchParams(h);
      if (p.get("t") && p.get("d")) { localStorage.setItem("desk.t", p.get("t")); localStorage.setItem("desk.d", p.get("d")); }
      history.replaceState(null, "", location.pathname + location.search + "#/home");
    }
  })();
  const TOKEN = () => localStorage.getItem("desk.t"), DPATH = () => localStorage.getItem("desk.d");

  // ---------- crypto
  let keyP = null;
  function key() {
    if (!keyP) keyP = crypto.subtle.importKey("raw", enc.encode(TOKEN()), "PBKDF2", false, ["deriveKey"])
      .then(k => crypto.subtle.deriveKey({ name: "PBKDF2", salt: enc.encode(SALT), iterations: ITER, hash: "SHA-256" }, k, { name: "AES-GCM", length: 256 }, false, ["decrypt"]));
    return keyP;
  }
  const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  async function decrypt(env) {
    if (!env || env.v !== 1 || !env.iv || !env.ct) throw new Error("bad envelope");
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(env.iv) }, await key(), b64(env.ct));
    return JSON.parse(new TextDecoder().decode(pt));
  }

  // ---------- data store
  const S = { files: {}, lastGood: null, offline: null, badKey: false, missing: new Set(), loadedOnce: false };
  window.__desk = S;
  const base = () => `d/${encodeURIComponent(DPATH())}/`;
  async function fetchFile(f) {
    let r;
    try { r = await fetch(base() + f + "?t=" + Date.now(), { cache: "no-store" }); }
    catch (e) { throw Object.assign(new Error("network"), { net: true }); }
    if (r.status === 404) { S.missing.add(f); return null; }
    if (!r.ok) throw Object.assign(new Error("http " + r.status), { net: true });
    const env = await r.json();
    try { const obj = await decrypt(env); S.missing.delete(f); S.files[f] = obj; return obj; }
    catch (e) { S.badKey = true; throw e; }
  }
  let pollTimer = null, countdown = null, nextAt = 0;
  async function poll() {
    clearTimeout(pollTimer);
    if (!TOKEN() || !DPATH()) { render(); return; }
    const want = CORE.slice(); const r = route();
    if (r.name === "coin") want.push(`coin/${r.arg}.json`, `audit/${r.arg}.json`);
    if (r.name === "audit") want.push(`audit/${r.arg}.json`, `coin/${r.arg}.json`);
    let failed = false;
    await Promise.all(want.map(f => fetchFile(f).catch(e => { if (e.net) failed = true; })));
    S.loadedOnce = true;
    if (failed) { S.offline = S.offline || Date.now(); }
    else { S.offline = null; if (S.files["state.json"]) S.lastGood = S.files["state.json"].ts; }
    const delay = (r.name === "live" || r.name === "scores" || r.name === "scanner") ? LIVE_POLL_S : POLL_S;
    nextAt = Date.now() + delay * 1000;
    render();
    pollTimer = setTimeout(poll, delay * 1000);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  setInterval(() => { if (S.offline) statusRow(); }, 1000);

  // ---------- formatting
  const money = v => v == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pct = v => v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%";
  const tm = t => t ? new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "—";
  const tmCT = t => t ? new Date(t).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/Chicago" }) + " CT" : "—";
  const price = v => v == null ? "—" : "$" + Charts.fmtP(Number(v));
  const ago = s => s == null ? "—" : s < 90 ? Math.round(s) + "s" : Math.round(s / 60) + "m";
  const short = m => m ? m.slice(0, 4) + "…" + m.slice(-4) : "";
  const solAmt = v => v == null ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 }) + " SOL";
  const F = f => S.files[f];
  function gmgnState() { return F("state.json")?.gmgn || F("account.json")?.gmgn || null; }

  // ---------- routing
  function route() {
    const p = location.hash.replace(/^#\/?/, "").split("/");
    return { name: p[0] || "home", arg: p[1] ? decodeURIComponent(p[1]) : null };
  }
  window.addEventListener("hashchange", () => { window.scrollTo(0, 0); const r = route(); if (r.name === "coin" || r.name === "audit" || r.name === "live" || r.name === "scores" || r.name === "scanner") poll(); else render(); });

  // ---------- chrome
  function statusRow() {
    const el = $("#statusRow");
    if (S.badKey) { el.className = "status"; el.textContent = "Can't read the desk with this link. Open the latest desk link from Telegram."; return; }
    if (S.offline) {
      const n = Math.max(0, Math.round((nextAt - Date.now()) / 1000));
      el.className = "status"; el.textContent = `Desk offline · retrying in ${n}s · last update ${S.lastGood ? tm(S.lastGood) : "never"}`; return;
    }
    const st = F("state.json");
    if (st) { const age = (Date.now() - new Date(st.ts)) / 1000; if (age > 600) { el.className = "status info"; el.textContent = `Desk data is ${ago(age)} old · last update ${tm(st.ts)}`; return; } }
    el.className = "status hidden";
  }
  function chrome() {
    const st = F("state.json");
    const b = $("#modeBadge");
    if (st) { b.classList.remove("hidden"); b.textContent = st.mode; b.classList.toggle("live", st.mode === "LIVE"); } else b.classList.add("hidden");
    const act = F("activity.json"); const seen = +(localStorage.getItem("desk.seen") || 0);
    const top = act && act.items && act.items[0] ? act.items[0].id : 0;
    $("#bellDot").classList.toggle("hidden", !(top > seen));
    const r = route().name;
    document.querySelectorAll(".tabs a").forEach(a => a.classList.toggle("on", a.dataset.tab === r || ((r === "scores" || r === "scanner") && a.dataset.tab === "live") || (r === "paper" && a.dataset.tab === "home") || (r === "coin" || r === "audit") && a.dataset.tab === "home"));
    statusRow();
  }
  $("#bellBtn").onclick = () => { location.hash = "#/activity"; };
  $("#searchBtn").onclick = () => { const s = $("#searchBar"); s.classList.toggle("hidden"); if (!s.classList.contains("hidden")) $("#searchIn").focus(); };
  $("#searchIn").oninput = e => search(e.target.value);

  // ---------- coin index for search / names (only from published data)
  function coins() {
    const m = new Map();
    const add = (mint, name, symbol, link, extra) => { if (!mint) return; const c = m.get(mint) || { mint }; c.name = c.name || name; c.symbol = c.symbol || symbol; c.link = c.link || link; Object.assign(c, extra || {}); m.set(mint, c); };
    const sc = F("agents/scanner.json");
    (sc?.room || []).forEach(r => add(r.mint, r.name, r.symbol, r.link, { room: true }));
    (sc?.feed || []).forEach(r => add(r.mint, r.name, r.symbol, r.link, { scan: r }));
    (F("agents/auditor.json")?.recent || []).forEach(r => add(r.mint, null, null, r.link || r.coin_url));
    (F("state.json")?.positions || []).forEach(p => add(p.mint, p.name, p.symbol, p.link));
    (F("activity.json")?.items || []).forEach(a => add(a.mint, null, null, a.link));
    (F("scores.json")?.items || []).forEach(a => add(a.mint, a.name, a.symbol, a.link));
    (F("feed.json")?.items || []).forEach(a => add(a.mint, a.name, a.symbol, a.link));
    return m;
  }
  function search(q) {
    q = q.trim().toLowerCase(); const out = $("#searchRes");
    if (!q) { out.innerHTML = ""; return; }
    const res = [...coins().values()].filter(c => [c.name, c.symbol, c.mint].some(v => v && v.toLowerCase().includes(q))).slice(0, 8);
    out.innerHTML = res.length ? res.map(c => `<a class="row" href="#/coin/${encodeURIComponent(c.mint)}"><div class="ico cyan">${esc((c.symbol || c.name || "?").slice(0, 2))}</div><div class="grow"><div class="t">${esc(c.name || c.symbol || short(c.mint))}</div><div class="s">${esc(c.symbol || "")} · ${esc(short(c.mint))}</div></div></a>`).join("")
      : `<div class="empty">No coin matches “${esc(q)}” in tonight's data</div>`;
    out.querySelectorAll("a").forEach(a => a.onclick = () => { $("#searchBar").classList.add("hidden"); $("#searchIn").value = ""; out.innerHTML = ""; });
  }

  // ---------- screens
  const V = $("#view");
  function skeleton() { return `<div class="card"><div class="skel" style="width:40%"></div><div class="skel" style="height:40px;width:70%"></div><div class="skel"></div></div><div class="card"><div class="skel"></div><div class="skel" style="width:60%"></div></div>`; }
  function emptyBox(t, s) { return `<div class="card empty"><div class="e1">${esc(t)}</div><div>${esc(s || "")}</div></div>`; }
  function gate() {
    if (!TOKEN() || !DPATH()) return emptyBox("Open your desk link", "Open the desk link the bot sent you in Telegram (@Denarpobot). It unlocks this app on this phone.");
    if (!S.loadedOnce) return skeleton();
    if (S.badKey && !F("state.json")) return emptyBox("Can't read the desk", "This link doesn't match the desk. Open the latest link from Telegram.");
    if (!F("state.json")) return S.offline ? emptyBox("Desk offline", "No data yet. Retrying every 30s.") : emptyBox("Waiting for the desk", "The desk hasn't published yet. It publishes every few minutes.");
    return null;
  }

  // heartbeat age as of now: published age + time since that publish (so an offline desk ages out to "down")
  function hbAge(a) { const st = F("state.json"); if (!a || a.heartbeatAgeS == null || !st) return null; return a.heartbeatAgeS + Math.max(0, (Date.now() - new Date(st.ts)) / 1000); }
  function hbClass(a) { if (!a) return "down"; if (a.paused) return "paused"; const g = hbAge(a); return g == null || g > 120 ? "down" : ""; }

  function agentCard(name, full) {
    const st = F("state.json"), ag = F(`agents/${name}.json`), hb = st?.agents?.[name];
    const title = name[0].toUpperCase() + name.slice(1);
    const head = `<div class="hd"><div class="nm"><span class="hb ${hbClass(hb)}"></span>${title}</div><div class="sub">${hb?.paused ? "paused" : "♥ " + ago(hbAge(hb))}</div></div>`;
    if (!ag) return `<div class="card agent">${head}<div class="sub">No data published yet</div></div>`;
    let body = "";
    if (name === "scanner") {
      body = `<div class="num">${ag.pools} pools</div><div class="sub">${ag.room.length} in the waiting room</div>`;
      const feed = (ag.feed || []).slice(0, full ? 25 : 4);
      body += `<div class="feed">${feed.length ? feed.map(f => f.verdict === "pass"
        ? `<a class="f" href="#/coin/${encodeURIComponent(f.mint)}"><span class="n">${esc(f.name || short(f.mint))} · liq $${Math.round(f.liquidityUsd || 0).toLocaleString()} · → auditor</span></a>`
        : `<a class="f kill" href="#/coin/${encodeURIComponent(f.mint)}"><span class="n">${esc(f.name || short(f.mint))}</span><span class="why">${esc(f.reason || "")}</span></a>`).join("") : `<div class="f"><span class="n muted">No decisions yet tonight</span></div>`}</div>`;
    } else if (name === "auditor") {
      const run = ag.running?.[0], last = run || ag.recent?.[0];
      const strips = last ? last.layers.map(l => `<i class="strip ${l.pass === true ? "ok" : l.pass === false ? "bad" : (run ? "run" : "")}"></i>`).join("") : Array(8).fill('<i class="strip"></i>').join("");
      const line = run ? `<span class="cyan">taking apart...</span>` : last ? (last.passed ? `<span class="green">8/8 → Executor</span>` : `<span class="red">${esc(last.reason)}</span>`) : `<span class="muted">Nothing audited yet</span>`;
      body = `<div class="num">${ag.takenApart} taken apart</div><div class="strips">${strips}</div><div class="rowx" style="margin-top:8px"><div>${line}</div><div class="sub">${ag.rejected} rejected</div></div>`;
      if (last) body = `<a href="#/audit/${encodeURIComponent(last.mint)}">${body}</a>`;
      if (full) body += `<div class="feed">${(ag.recent || []).slice(0, 12).map(a => `<a class="f" href="#/audit/${encodeURIComponent(a.mint)}"><span class="n">${esc(coins().get(a.mint)?.name || short(a.mint))}</span><span class="${a.passed ? "green" : "why"}">${a.passed ? "8/8" : esc(a.reason)}</span></a>`).join("")}</div>`;
    } else if (name === "analyst") {
      const last = ag.recent?.[0];
      body = `<div class="rowx"><div><div class="num">${ag.scoring?.length ? "Scoring" : ag.scored + " scored"}</div><div class="sub">${ag.mode === "rules" ? "Analyst (rules)" : "Jev"} · min ${ag.minScore}${ag.inputTokens ? " · " + ag.inputTokens.toLocaleString() + " tokens" : ""}</div>
        ${last ? `<div class="sub">${esc(coins().get(last.mint)?.name || short(last.mint))} · ${last.passed ? '<span class="green">pass</span>' : '<span class="red">' + esc(last.failed[0]) + "</span>"}</div>` : `<div class="sub">No coin scored yet</div>`}</div>
        <canvas class="ring" data-score="${last ? last.score : ""}" data-min="${ag.minScore}" width="64" height="64"></canvas></div>`;
      body += `<a class="hint" href="#/scores" style="display:block;margin-top:10px">Scores · min ${ag.minScore} to buy · tap a row for why</a>`;
    } else if (name === "executor") {
      const last = ag.recent?.[0], st2 = F("state.json");
      const filled = last && last.status === "bought";
      body = `<div class="num">${ag.trades} trades</div><div class="sub">${last ? money(last.sizeUsd) + " · " + (last.stackPct ?? 0).toFixed(2) + "% of stack" : ag.stackPct + "% of stack per ticket · min $47.50"}</div>
        ${last ? `<div class="bar"><i data-fill="${filled || last.status === "sent" ? (filled ? 100 : 60) : 0}"></i></div><div class="sub" style="margin-top:6px">${filled ? '<span class="green">✓ Filled</span>' : last.status === "sent" ? '<span class="cyan">Executing... waiting for your tap</span>' : esc(last.status + (last.reason ? " · " + last.reason : ""))}</div>` : ""}`;
    } else if (name === "risk") {
      body = `<div class="num">${ag.guarding ? "Guarding" : "Idle"}</div><div class="sub">${ag.positions.length} open${ag.lastExitLabel ? " · last exit " + esc(ag.lastExitLabel) : ""}</div>
        <div class="pills"><span class="pill red">${esc(ag.stopPill)}</span><span class="pill green">${esc(ag.trailPill)}</span></div>`;
    }
    const pause = full ? `<div class="rowx" style="margin-top:12px"><div class="sub">${hb?.paused ? "Paused" : "Running"}</div><span class="toggle ${hb?.paused ? "" : "on"}" aria-disabled="true"></span></div><div class="hint">Change in Telegram: ${hb?.paused ? "resume" : "pause"} ${title}</div>` : "";
    return `<div class="card agent">${head}${body}${pause}</div>`;
  }

  function gmgnWalletCard(g) {
    if (!g) return "";
    const live = g.mode === "LIVE";
    const title = `Real wallet (GMGN ${esc(g.mode)})`;
    const badges = [`<span class="badge ${live ? "live" : ""}">${esc(g.mode)}</span>`]
      .concat(g.halted ? [`<span class="badge">HALTED</span>`] : []).join("");
    const usd = g.usd != null ? money(g.usd) : "USD —";
    const when = g.updatedAt ? ` · updated ${tm(g.updatedAt)}` : "";
    const tickets = `${g.ticketsUsed}/${g.ticketsMax} tickets tonight`;
    return `<div class="card real"><div class="rowx"><div class="sub">${title}</div><div class="pills" style="margin:0">${badges}</div></div>
      <div class="big">${solAmt(g.sol)}</div>
      <div class="sub">${usd} · ${esc(g.walletShort || short(g.wallet))}${when}</div>
      <div class="pills"><span class="pill">${money(g.capTradeUsd)} / trade</span><span class="pill">${esc(tickets)}</span></div></div>`;
  }
  function gmgnPosRow(p) {
    const name = p.name || p.symbol || short(p.mint);
    const ex = p.exits || {};
    const exits = `stop -${ex.stopPct ?? 15}% · trail ${ex.trailPct ?? 20}%` + (ex.attached ? " · exits on" : " · exits?");
    const inner = `<div class="ico ${(p.pct ?? 0) >= 0 ? "green" : "red"}">${esc((p.symbol || p.name || "?").slice(0, 2))}</div>
      <div class="grow"><div class="t">${esc(name)} <span class="badge ${p.mode === "LIVE" ? "live" : ""}" style="margin-left:6px">${esc(p.mode)}</span></div>
      <div class="s">${money(p.sizeUsd)} · ${exits}</div></div>
      <div class="r"><div class="${(p.pct ?? 0) >= 0 ? "green" : "red"}">${p.pnlUsd != null ? money(p.pnlUsd) : pct(p.pct)}</div><div class="s">${pct(p.pct)}</div></div>`;
    return p.mint ? `<a class="row" href="#/coin/${encodeURIComponent(p.mint)}">${inner}</a>` : `<div class="row">${inner}</div>`;
  }
  function moneyTabs() {
    const onPaper = route().name === "paper";
    return `<div class="seg" role="tablist" aria-label="Real money or Paper">
      <a href="#/home" class="${onPaper ? "" : "on real"}" role="tab" aria-selected="${!onPaper}">Real money</a>
      <a href="#/paper" class="${onPaper ? "on paper" : ""}" role="tab" aria-selected="${onPaper}">Paper</a>
    </div>`;
  }
  function bypassBadge(r) { return r.bypass ? `<span class="badge bypass${r.tag === "OVERRIDE" ? " override" : ""}">${esc(r.tag || "BYPASS")} · score ${r.score ?? "?"}</span>` : ""; }
  function tradeRow(r) {
    const name = r.name || r.symbol || short(r.mint);
    const p = r.pct ?? 0, up = p >= 0;
    const src = r.source === "GMGN" ? "GMGN" : "FOMO";
    let sub;
    if (r.open) {
      const ex = r.exits || {};
      sub = r.source === "GMGN" ? `${money(r.sizeUsd)} · stop -${ex.stopPct ?? 15}% · trail ${ex.trailPct ?? 20}%${ex.attached ? " · exits on" : " · exits?"}`
        : `${money(r.sizeUsd)} · stop -${r.stopPct}%${r.trailActive ? " · trailing" : ""}`;
    } else {
      const why = r.exitReason ? (r.exitReason === "trail" ? "trailed out" : r.exitReason === "stop" ? "stopped" : r.exitReason) : r.status;
      sub = `${money(r.sizeUsd)} · ${esc(why || "closed")} ${tm(r.closedAt)}`;
    }
    const right = r.pnlUsd != null ? money(r.pnlUsd) : pct(r.pct);
    const inner = `<div class="ico ${up ? "green" : "red"}">${esc((r.symbol || r.name || "?").slice(0, 2))}</div>
      <div class="grow"><div class="t">${esc(name)} <span class="src">${src}</span></div>${r.bypass ? `<div class="bpline">${bypassBadge(r)}</div>` : ""}<div class="s">${sub}</div></div>
      <div class="r"><div class="${up ? "green" : "red"}">${right}</div><div class="s">${pct(r.pct)}</div></div>`;
    return r.mint ? `<a class="row${r.bypass ? " byp" : ""}" href="#/coin/${encodeURIComponent(r.mint)}">${inner}</a>` : `<div class="row">${inner}</div>`;
  }
  function pnlCard(side, label, cls) {
    const t = side.pnlTonightUsd ?? 0, a = side.pnlAllUsd ?? 0;
    return `<div class="card ${cls}"><div class="sub">${label}</div>
      <div class="grid2" style="margin-top:8px"><div><div class="${t >= 0 ? "green" : "red"}" style="font:700 22px var(--mono)">${money(t)}</div><div class="sub">tonight · ${side.closedTonight || 0} closed</div></div>
      <div><div class="${a >= 0 ? "green" : "red"}" style="font:700 22px var(--mono)">${money(a)}</div><div class="sub">all time · ${side.wins || 0}W ${side.losses || 0}L</div></div></div></div>`;
  }
  function tonightAndAgents(st) {
    const c = st.counts;
    let h = `<h1>Tonight</h1><div class="grid2">
      <div class="card stat"><div class="v">${c.pools}</div><div class="k">pools seen</div></div>
      <div class="card stat"><div class="v">${c.scannerPassed}</div><div class="k">passed Scanner</div></div>
      <div class="card stat"><div class="v">${c.audited - c.auditRejected}/${c.audited}</div><div class="k">cleared Auditor</div></div>
      <div class="card stat"><div class="v">${c.trades}</div><div class="k">trades</div></div></div>`;
    h += scoresTeaser();
    h += `<h1>Agents</h1>` + AGENTS.map(a => agentCard(a, false)).join("");
    return h;
  }
  function home() {
    const g = gate(); if (g) return g;
    const st = F("state.json"), gm = st.gmgn || gmgnState(), tr = F("trades.json");
    let h = moneyTabs();
    if (route().name === "paper") return h + paperView(st, tr) + tonightAndAgents(st);
    // ---- Real money: GMGN LIVE only
    h += gmgnWalletCard(gm);
    const real = tr?.real;
    if (real) {
      h += pnlCard(real, "Real P&L (GMGN LIVE)", "real");
      h += `<h1>Real positions</h1>` + (real.open.length ? `<div class="card list">${real.open.map(tradeRow).join("")}</div>`
        : emptyBox("No real positions open", gm?.mode === "LIVE" ? "GMGN buys on its own when a coin passes. Exits sit on GMGN." : "GMGN is in PAPER mode."));
      if (real.closed.length) h += `<h1>Real trades</h1><div class="card list">${real.closed.map(tradeRow).join("")}</div>`;
    } else {
      const gOpen = (gm?.open || []).filter(p => p.mode === "LIVE");
      h += `<h1>Real positions</h1>` + (gOpen.length ? `<div class="card list">${gOpen.map(gmgnPosRow).join("")}</div>` : emptyBox("No real positions open", "Waiting for the next update."));
    }
    return h + tonightAndAgents(st);
  }
  function paperView(st, tr) {
    const start = st.startBalance ?? st.balance, d = st.balance - start;
    const fomoLabel = st.mode === "PAPER" ? "FOMO practice balance (PAPER, not real money)" : "FOMO LIVE stack";
    let h = `<div class="card paper"><div class="rowx"><div class="sub">${fomoLabel}</div><span class="badge">PAPER</span></div>
      <div class="big">${money(st.balance)}</div>
      <div class="sub"><span class="${d >= 0 ? "green" : "red"}">${d >= 0 ? "+" : ""}${money(d).replace("$-", "-$")} (${pct(start ? d / start * 100 : 0)})</span> · free ${money(st.free)} · in positions ${money(st.inPositions)}</div>
      <canvas id="homeChart" style="margin-top:12px"></canvas>
      <div id="homeChartEmpty" class="sub hidden" style="padding:20px 0;text-align:center">The chart starts with the first trade</div></div>`;
    if (st.ticket && st.ticket.kind !== "ok") h += `<div class="status info" style="margin:0 0 10px">${esc(st.ticket.msg)}</div>`;
    const paper = tr?.paper;
    if (!paper) {
      const ps = st.positions.filter(p => p.mode === "PAPER");
      return h + `<h1>Paper positions</h1>` + (ps.length ? `<div class="card list">${ps.map(p => tradeRow(Object.assign({ open: true, source: "FOMO" }, p))).join("")}</div>` : emptyBox("No paper positions", "Waiting for the next update."));
    }
    h += pnlCard(paper, "Paper P&L (not real money)", "paper");
    if (paper.bypassOn) h += `<div class="status info" style="margin:0 0 10px">Analyst bypass is on for testing: coins that failed scoring still paper-buy and show BYPASS · score N. Never real money.</div>`;
    h += `<h1>Paper positions</h1>` + (paper.open.length ? `<div class="card list">${paper.open.map(r => tradeRow(r) + (r.source === "FOMO" && r.link ? `<a class="btn fomo" href="${esc(r.link)}" target="_blank" rel="noopener">Open on FOMO</a>` : "")).join("")}</div>`
      : emptyBox("No paper positions open", "The desk is scanning. Paper buys arrive as Telegram pushes."));
    if (paper.closed.length) h += `<h1>Paper trades</h1><div class="card list">${paper.closed.map(tradeRow).join("")}</div>`;
    return h;
  }
  function scoreBar(score, min) {
    const fill = Math.max(0, Math.min(100, Number(score) || 0));
    const mark = Math.max(0, Math.min(100, Number(min) || 0));
    return `<div class="bar thresh"><i data-fill="${fill}"></i><span class="mark" style="left:${mark}%"></span></div>`;
  }
  function barsOf() { return F("feed.json")?.bars || {}; }
  function scoreVerdict(it, min) {
    const score = it.score, minN = min == null ? null : Number(min);
    if (it.passed || it.outcome === "buy") return { label: "BUY", cls: "buy", buy: true };
    if (score != null && minN != null && Number(score) < minN) {
      const n = Math.round(minN - Number(score));
      return { label: `missed ${minN} by ${n}`, cls: "miss", buy: false };
    }
    return { label: "NO BUY", cls: "miss", buy: false };
  }
  function checkRow(label, val, bar, lower) {
    const fmt = x => Number.isInteger(Number(x)) ? String(Math.round(Number(x))) : Number(x).toFixed(2);
    const has = val != null && val !== "";
    const vs = has ? fmt(val) : "—";
    if (bar == null || bar === "") {
      return `<div class="check"><span class="k">${esc(label)}</span><span class="v">${vs}</span><span class="muted"> </span></div>`;
    }
    const barS = fmt(bar);
    if (!has) return `<div class="check"><span class="k">${esc(label)}</span><span class="v">— / ${barS}</span><span class="muted">—</span></div>`;
    const ok = lower ? Number(val) <= Number(bar) : Number(val) >= Number(bar);
    return `<div class="check"><span class="k">${esc(label)}</span><span class="v">${vs} / ${barS}</span><span class="${ok ? "ok" : "bad"}">${ok ? "PASS" : "FAIL"}</span></div>`;
  }
  // Owner override (DECISION 36): Telegram deep links only; the app stays read-only. Analyst rejects only, <=30 min, once.
  const MINT_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
  function overrideBtns(it) {
    const bot = F("feed.json")?.tgBot || F("scores.json")?.tgBot;
    const okP = it.overridablePaper === true, okR = it.overridableReal === true;
    if (!(okP || okR) || !bot || !/^[A-Za-z0-9_]{5,32}$/.test(bot) || !MINT_RE.test(it.mint || "")) return "";
    if (Date.now() - new Date(it.t || it.ts).getTime() > 30 * 60 * 1000) return "";
    const u = k => `https://t.me/${bot}?start=${k}_${it.mint}`;
    const bp = okP ? `<a class="btn ovp" href="${esc(u("ovp"))}" target="_blank" rel="noopener">Override · Paper</a>`
      : `<span class="btn ovdone">On paper (bypass)</span>`;
    const br = okR ? `<a class="btn ovreal" href="${esc(u("ovr"))}" target="_blank" rel="noopener">Override · Real $</a>` : "";
    return `<div class="ovr">${bp}${br}</div>
      <div class="ovnote">Opens Telegram. Skips only the Analyst score; Real $ asks you to confirm.</div>`;
  }
  function scoreDetail(it, min, bars, noOv) {
    const a = it.answers || {}, b = bars || {};
    const v = scoreVerdict(it, min);
    const minN = min == null ? null : Number(min);
    const scoreOk = it.score != null && minN != null && Number(it.score) >= minN;
    const bought = it.bought ? `<span class="pill cyan">bought</span>` : "";
    const coin = it.mint ? `<a class="btn ghost" href="#/coin/${encodeURIComponent(it.mint)}" style="margin-top:10px">View coin</a>` : "";
    return `<div class="checks">
        ${checkRow("Staying power", a.alive_6h, b.alive_6h, false)}
        ${checkRow("Real buyers", a.organic, b.organic, false)}
        ${checkRow("Flush", a.flush, null, false)}
        ${checkRow("Chasing", a.chase, b.chase, true)}
        ${checkRow("Score", it.score, minN, false)}
      </div>
      <div class="verdict ${v.cls}">${esc(v.buy ? "BUY" : v.label)}</div>
      <div class="pills" style="margin-top:8px"><span class="pill ${v.buy ? "green" : "red"}">${esc(v.buy ? "BUY" : "NO BUY")}</span>${bought}</div>
      ${noOv ? "" : overrideBtns(it)}
      ${coin}`;
  }
  function scoreRow(it, min, open, bars) {
    const v = scoreVerdict(it, min);
    const label = it.name || it.symbol || it.mintShort || short(it.mint);
    const b = bars || barsOf();
    return `<div class="score-row${open ? " open" : ""}" data-mint="${esc(it.mint || "")}">
      <div class="rowx">
        <div class="grow"><div class="t">${esc(label)}</div>
          <div class="s">${esc(it.mintShort || short(it.mint))} · ${tmCT(it.t || it.ts)}</div>
          <div class="s ${v.buy ? "green" : "red"}">${esc(v.label)}</div></div>
        <div class="sc ${v.buy || (it.score != null && Number(it.score) >= Number(min)) ? "green" : "red"}">${it.score == null ? "—" : it.score}</div>
      </div>
      ${scoreBar(it.score, min)}
      <div class="detail">${scoreDetail(it, min, b)}</div>
    </div>`;
  }
  function scoresTeaser() {
    const sc = F("scores.json"), fd0 = F("feed.json");
    if (!sc && !fd0) return "";
    const min = (fd0 && fd0.minScore != null) ? fd0.minScore : sc?.minScore, items = sc?.items || [];
    const last = items[0];
    const fd = F("feed.json");
    const nFeed = (fd?.items || []).length;
    const latest = (fd?.items || [])[0];
    const latestLabel = latest ? esc(latest.name || latest.symbol || latest.mintShort || short(latest.mint)) : "";
    return `<h1>Live feed</h1><a class="card tap" href="#/live">
      <div class="rowx"><div><div class="sub">Every coin the desk evaluates · min ${min} to buy</div>
        <div class="num">${nFeed || items.length} event${(nFeed || items.length) === 1 ? "" : "s"} tonight</div>
        ${latest ? `<div class="sub">${latestLabel} · ${esc(liveOutcome(latest))}</div>` : (last ? `<div class="sub">${esc(last.name || last.mintShort || short(last.mint))} · <span class="${last.passed ? "green" : "red"}">${last.score} ${last.passed ? "pass" : "reject"}</span></div>` : `<div class="sub">Waiting for the next coin</div>`)}
      </div></div>
      ${scoreBar(last ? last.score : 0, min)}
      <div class="sub" style="margin-top:8px">Tap for the live feed, or switch to Scores for every Analyst decision</div>
    </a>`;
  }
  function liveOutcome(it) {
    const st = it.stage, o = it.outcome;
    if (st === "analyst") return (it.score == null ? "" : it.score + " ") + (o === "buy" ? "BUY" : "NO BUY");
    if (st === "auditor") return o === "pass" ? "Auditor pass" : "Auditor fail" + (it.layerName ? " · " + it.layerName : "");
    if (st === "recheck") return o === "pass" ? "Recheck " + (it.recheckN || "") + " passed" : "Recheck " + (it.recheckN || "");
    if (st === "room") return "Waiting room";
    if (o === "kill") return "Scanner kill";
    if (o === "pass") return "Passed Scanner";
    return (st || "") + (o ? " · " + o : "");
  }
  function liveMetric(label, val, bar, lower) {
    if (val == null || bar == null) return `<span class="muted">${esc(label)} —</span>`;
    const ok = lower ? Number(val) <= Number(bar) : Number(val) >= Number(bar);
    return `<span class="${ok ? "ok" : "bad"}">${esc(label)} ${Number(val).toFixed(2)}/${Number(bar).toFixed(2)}</span>`;
  }
  function usdK(v) { v = Number(v) || 0; return v >= 1e6 ? "$" + (v / 1e6).toFixed(2) + "M" : "$" + (v / 1e3).toFixed(1) + "K"; }
  function liveRow(it, min, bars) {
    const label = it.name || it.symbol || it.mintShort || short(it.mint);
    const hot = it.stage === "analyst" && it.score != null && Number(it.score) >= Number(min);
    const stage = it.stage === "recheck" && it.recheckN ? `Recheck ${it.recheckN}` : (it.stage || "").replace(/^./, c => c.toUpperCase());
    if (it.stage === "analyst") {
      const v = scoreVerdict(it, min);
      return `<div class="live-row score-row${hot ? " hot" : ""}${it.outcome === "buy" ? " buy" : ""}" data-mint="${esc(it.mint || "")}">
        <div class="rowx">
          <div class="grow"><div class="t">${esc(label)}</div>
            <div class="s">${esc(stage)} · ${tmCT(it.t)}${it.symbol && it.name ? " · " + esc(it.symbol) : ""}</div>
            <div class="s ${v.buy ? "green" : "red"}">${esc(v.label)}</div></div>
          <div class="sc ${hot || v.buy ? "green" : "red"}">${it.score == null ? "—" : it.score}</div>
        </div>
        ${overrideBtns(it)}
        <div class="sub">Tap for the numbers vs each bar</div>
        <div class="detail">${scoreDetail(it, min, bars, true)}</div>
      </div>`;
    }
    let right = "";
    if (it.outcome === "buy") right = `<div class="sc green">BUY</div>`;
    else if (it.outcome === "no_buy") right = `<div class="pill red">NO BUY</div>`;
    else if (it.outcome === "kill") right = `<div class="pill red">kill</div>`;
    else if (it.outcome === "fail") right = `<div class="pill red">fail</div>`;
    else if (it.outcome === "pass") right = `<div class="pill green">pass</div>`;
    else if (it.outcome === "waiting") right = `<div class="pill">waiting</div>`;
    else if (it.outcome === "recheck") right = `<div class="pill">recheck</div>`;
    let detail = "";
    if (it.reason || it.layerName || it.mc != null || it.liq != null) {
      const bits = [];
      if (it.stage === "auditor" && it.layer) bits.push("layer " + it.layer + (it.layerName ? " · " + it.layerName : ""));
      if (it.reason) bits.push(it.reason);
      if ((it.stage === "scanner" || it.stage === "recheck" || it.stage === "room") && (it.mc != null || it.liq != null))
        bits.push("MC " + (it.mc != null ? usdK(it.mc) : "?") + " · liq " + (it.liq != null ? usdK(it.liq) : "?"));
      detail = `<div class="sub">${esc(bits.join(" · "))}</div>`;
    }
    const inner = `<div class="rowx">
        <div class="grow"><div class="t">${esc(label)}</div>
          <div class="s">${esc(stage)} · ${tmCT(it.t)}${it.symbol && it.name ? " · " + esc(it.symbol) : ""}</div></div>
        ${right}
      </div>${detail}`;
    const cls = `live-row${hot ? " hot" : ""}${it.outcome === "buy" ? " buy" : ""}`;
    return it.mint ? `<a class="${cls}" href="#/coin/${encodeURIComponent(it.mint)}">${inner}</a>` : `<div class="${cls}">${inner}</div>`;
  }
  function liveFeed(min) {
    const fd = F("feed.json");
    const items = fd?.items || [];
    const bars = fd?.bars || {};
    const m = min ?? fd?.minScore;
    let h = `<h1>Live feed</h1>
      <div class="card"><div class="sub">Newest first · every coin the desk evaluates · times in CT</div>
      <div class="sub" style="margin-top:6px">Updates about every minute while this tab is open (the desk publishes shortly after each batch, then this app checks every 15s). Tap an Analyst row for the numbers vs each bar.</div></div>`;
    if (!fd) return h + emptyBox("Waiting for the live feed", "The desk will publish it on the next update.");
    if (!items.length) return h + emptyBox("Nothing evaluated yet", "Scanner kills, rechecks, waiting-room moves, Auditor results and Analyst scores show up here.");
    h += `<div class="card list">${items.map(it => liveRow(it, m, bars)).join("")}</div>`;
    return h;
  }
  function tabToggle() {
    const r = route().name;
    const tab = (h, n, label) => `<a href="#/${h}" class="${r === n ? "on" : ""}" role="tab" aria-selected="${r === n}">${label}</a>`;
    return `<div class="seg seg3" role="tablist" aria-label="Live, Scores or Scanner">
      ${tab("live", "live", "Live")}${tab("scores", "scores", "Scores")}${tab("scanner", "scanner", "Scanner")}
    </div>`;
  }
  // ---------- Scanner view (owner request 2026-11-15 6:25 PM CT): every scanner decision, each check vs its bar
  function scanVal(c) {
    if (c.fmt === "bool") return c.value == null ? "n/a" : (c.value ? "yes" : "no");
    if (c.value == null) return "n/a";
    if (c.fmt === "usd") return usdK(c.value);
    return (c.key === "h1" && c.value > 0 ? "+" : "") + Number(c.value).toFixed(c.key === "h1" ? 0 : 1) + "%";
  }
  function scanBar(c) {
    if (c.op === "off") return "off";
    if (c.bar == null) return "";
    const b = c.fmt === "usd" ? usdK(c.bar) : (c.key === "h1" ? "+" : "") + Number(c.bar).toFixed(0) + "%";
    return (c.op === ">=" ? "≥ " : "≤ ") + b;
  }
  function scanCheck(c) {
    const st = c.pass === true ? `<span class="pf ok">PASS</span>` : c.pass === false ? `<span class="pf bad">FAIL</span>` : `<span class="pf">—</span>`;
    return `<div class="scheck"><span class="k">${esc(c.label)}</span><span class="v">${esc(scanVal(c))}</span><span class="b">${esc(scanBar(c))}</span>${st}</div>`;
  }
  function scanPill(it) {
    if (it.pill === "pass") return `<span class="pill green">pass</span>`;
    if (it.pill === "waiting") return `<span class="pill">waiting</span>`;
    if (it.pill === "recheck") return `<span class="pill amber">recheck</span>`;
    return `<span class="pill red">kill</span>`;
  }
  function scanRow(it) {
    const label = it.name || it.symbol || it.mintShort || short(it.mint);
    let sub2 = "";
    if (it.pill === "recheck") sub2 = `${it.recheckN ? `recheck ${it.recheckN} failed · ` : "killed · "}recheck ${it.nextRecheckN} at ${tmCT(it.nextAt)}`;
    else if (it.pill === "kill" && it.final) sub2 = `recheck ${it.recheckN} · final`;
    else if (it.pill === "waiting") sub2 = it.recheckN ? `recheck ${it.recheckN} cleared · back in the waiting room` : "waiting room";
    const reason = it.pill !== "pass" && it.reason ? `<div class="s red">${esc(it.reason)}</div>` : "";
    const fails = (it.checks || []).filter(c => c.pass === false).map(c => c.label).join(", ");
    return `<div class="score-row scan-row" data-mint="${esc(it.mint || "")}">
      <div class="rowx"><div class="grow"><div class="t">${esc(label)}</div>
        <div class="s">${tmCT(it.t)}${it.symbol && it.name && it.symbol !== it.name ? " · " + esc(it.symbol) : ""}${it.ageMin != null ? " · " + it.ageMin + "m old" : ""}</div>
        ${sub2 ? `<div class="s">${esc(sub2)}</div>` : ""}${reason}</div>
        <div>${scanPill(it)}</div></div>
      <div class="schecks mini">${(it.checks || []).slice(0, 4).map(c => `<span class="${c.pass === true ? "ok" : c.pass === false ? "bad" : ""}">${esc(c.label.split(" ")[0])} ${esc(scanVal(c))}</span>`).join("")}</div>
      <div class="detail">
        <div class="schecks">${(it.checks || []).map(scanCheck).join("")}</div>
        ${fails ? `<div class="sub" style="margin-top:6px">Failed: ${esc(fails)}</div>` : ""}
        <div class="sub" style="margin-top:6px">${esc(it.mint)}</div>
        <a class="btn fomo" href="${esc(it.link)}" target="_blank" rel="noopener">Open on FOMO</a>
        <a class="btn ghost" href="#/coin/${encodeURIComponent(it.mint)}">View coin</a>
      </div></div>`;
  }
  function scannerView() {
    const sf = F("scanner.json");
    let h = `<h1>Scanner</h1>`;
    if (!sf) return h + emptyBox("Waiting for the scanner feed", "The desk will publish it on the next update.");
    const c = sf.counts1h || {}, L = sf.limits || {}, nm = L.nearMiss || {};
    h += `<div class="grid4">
      <div class="card stat"><div class="v">${c.seen ?? 0}</div><div class="k">seen 1h</div></div>
      <div class="card stat"><div class="v green">${c.passed ?? 0}</div><div class="k">passed</div></div>
      <div class="card stat"><div class="v red">${c.killed ?? 0}</div><div class="k">killed</div></div>
      <div class="card stat"><div class="v amber">${c.rechecking ?? 0}</div><div class="k">rechecking</div></div></div>`;
    const age = Number(L.maxAgeH) ? `max age ${L.maxAgeH}h` : "no age limit";
    h += `<div class="card"><div class="sub">Current limits (from settings)</div><div class="pills">
      <span class="pill">liq ≥ ${usdK(L.minLiquidityUsd)}</span>
      <span class="pill">MC ${Number(L.minMarketCapUsd) ? "≥ " + usdK(L.minMarketCapUsd) : "off"}</span>
      <span class="pill">top 10 ≤ ${L.maxTop10Pct}%</span><span class="pill">1h ≤ +${L.maxH1ChangePct}%</span>
      <span class="pill">pump mint</span><span class="pill">SOL-quoted</span>
      <span class="pill">pool age ≥ ${L.minPoolAgeMin}m</span><span class="pill">${esc(age)}</span></div>
      <div class="sub" style="margin-top:8px">Rechecks at ${esc(L.recheckMinutes)} min, only if close: liq ≥ ${usdK(nm.minLiqUsd)}, MC ≥ ${usdK(nm.minMcUsd)}, top 10 ≤ ${nm.maxTop10Pct}%, 1h ≤ +${nm.maxH1Pct}%</div></div>`;
    const items = sf.items || [];
    if (!items.length) return h + emptyBox("Nothing scanned yet tonight", "New PumpSwap coins show up here as the scanner checks them.");
    h += `<div class="sub" style="margin:0 2px 8px">Newest first · last ${items.length} tonight · times in CT · tap a row for every check</div>`;
    return h + `<div class="card list">${items.map(scanRow).join("")}</div>`;
  }

  function scoresList(min, items, bars) {
    let h = `<h1>Analyst scores</h1>
      <div class="card"><div class="sub">Min score to buy tonight</div><div class="big">${min ?? "—"}</div>
      ${scoreBar(min, min)}
      <div class="sub" style="margin-top:8px">${items.length} decision${items.length === 1 ? "" : "s"} · newest first · tap a row for each number vs its bar</div></div>`;
    if (!items.length) return h + emptyBox("No coin scored yet", "The desk is scanning. Scores appear after Auditor passes a coin.");
    h += `<div class="card list">${items.map(it => scoreRow(it, min, false, bars)).join("")}</div>`;
    return h;
  }
  function scores() {
    const g = gate(); if (g) return g;
    const sc = F("scores.json"), ag = F("agents/analyst.json"), fd = F("feed.json");
    const min = (fd && fd.minScore != null) ? fd.minScore : (sc?.minScore ?? ag?.minScore);
    const items = sc?.items || [];
    const bars = (fd && fd.bars) || barsOf();
    if (route().name === "scores") return tabToggle() + scoresList(min, items, bars);
    if (route().name === "scanner") return tabToggle() + scannerView();
    return tabToggle() + liveFeed(min);
  }
  function agents() { const g = gate(); if (g) return g; return `<h1>Your 5 agents</h1>` + AGENTS.map(a => agentCard(a, true)).join("") + `<div class="hint">All agent changes go through Telegram (@Denarpobot). This app is read-only.</div>`; }

  function gmgnActivityItems(orders) {
    return (orders || []).filter(o => o.status !== "refused").map(o => {
      const closed = o.status === "closed";
      const stop = closed && (o.exitReason === "loss_stop");
      const kind = !closed ? "buy" : stop ? "stop" : "sell";
      const title = !closed ? `Bought ${o.name || o.symbol || short(o.mint)}`
        : stop ? `Stopped ${o.name || o.symbol || short(o.mint)}`
        : `Sold ${o.name || o.symbol || short(o.mint)}`;
      const tone = !closed ? "cyan" : (o.pnlUsd ?? 0) >= 0 && !stop ? "green" : "red";
      const right1 = !closed ? money(o.sizeUsd) : money(o.pnlUsd);
      const right2 = !closed ? o.mode : (o.pct != null ? pct(o.pct) : (o.exitReason || ""));
      return {id: "g" + o.id, t: o.closedAt || o.openedAt || o.t, kind, title,
              sub: `GMGN · ${o.mode}` + (o.exitReason && closed ? ` · ${o.exitReason.replace(/_/g, " ")}` : ""),
              right1, right2, mint: o.mint, tone, mode: o.mode};
    });
  }
  function activity() {
    const g = gate(); if (g) return g;
    const a = F("activity.json");
    if (a?.items?.length) localStorage.setItem("desk.seen", String(a.items[0].id));
    const names = coins();
    const items = (a?.items || []).concat(gmgnActivityItems(gmgnState()?.orders)).sort((x, y) => new Date(y.t) - new Date(x.t));
    if (!items.length) return `<h1>Activity</h1>` + emptyBox("Nothing yet tonight", "Buys, sells, stops and rejects show up here.");
    return `<h1>Activity</h1><div class="card list">${items.map(i => {
      const ic = { buy: "↗", sell: "↘", stop: "■", reject: "✕" }[i.kind] || "•";
      const live = i.mode === "LIVE" || (i.sub || "").includes("LIVE");
      const inner = `<div class="ico ${i.tone === "cyan" ? "cyan" : i.tone}">${ic}</div><div class="grow"><div class="t">${esc(i.title)}${live ? ` <span class="badge live">LIVE</span>` : ""}</div><div class="s">${esc(i.sub)} · ${tm(i.t)}</div></div>
        <div class="r"><div class="${i.tone === "red" ? "red" : i.tone === "green" ? "green" : ""}">${esc(i.right1)}</div><div class="s">${esc(i.right2)}</div></div>`;
      return i.mint ? `<a class="row" href="#/coin/${encodeURIComponent(i.mint)}">${inner}</a>` : `<div class="row">${inner}</div>`;
    }).join("")}</div>`;
  }

  function audit(mint) {
    const g = gate(); if (g) return g;
    const a = F(`audit/${mint}.json`), c = F(`coin/${mint}.json`), info = coins().get(mint);
    const name = c?.name || info?.name || short(mint), link = a?.coin_url || a?.link || c?.coin_url || info?.link;
    let h = `<a class="back" href="#/agents">‹ Agents</a><h1>Auditor · ${esc(name)}</h1>`;
    if (!a) return h + (S.missing.has(`audit/${mint}.json`) ? emptyBox("No audit for this coin", "Scanner didn't pass it to the Auditor tonight.") : skeleton()) + (link ? `<a class="btn ghost" href="${esc(link)}" target="_blank" rel="noopener">Open on FOMO</a>` : "");
    h += `<div class="card"><canvas id="disc" width="220" height="220" style="width:220px;margin:4px auto 10px"></canvas>
      <div id="auditVerdict" class="rowx"><div class="t" style="font-weight:700">${a.running ? '<span class="cyan">taking apart...</span>' : a.passed ? '<span class="green">8/8 clean</span>' : '<span class="red">Rejected · ' + esc(a.reason) + "</span>"}</div><div class="sub">${a.holders != null ? a.holders + " holders" : ""}</div></div>
      <ul class="layers" id="layers">${a.layers.map(l => `<li data-n="${l.n}" class="${l.pass === true ? "ok" : l.pass === false ? "bad" : "nc"}"><span class="mk">${l.pass === true ? "✓" : l.pass === false ? "✕" : l.n}</span><span>${esc(l.name)}</span><span class="lv">${esc(l.value)}</span></li>`).join("")}</ul></div>`;
    if (link) h += `<a class="btn fomo" href="${esc(link)}" target="_blank" rel="noopener">Open ${esc(name)} on FOMO</a>`;
    return h;
  }
  function discAnim(a, still) {
    const cv = $("#disc"); if (!cv || !a) return;
    const dpr = devicePixelRatio || 1, S2 = 220; cv.width = S2 * dpr; cv.height = S2 * dpr; const x = cv.getContext("2d"); x.setTransform(dpr, 0, 0, dpr, 0, 0);
    const lis = [...document.querySelectorAll("#layers li")];
    const draw = (upto, beam) => {
      x.clearRect(0, 0, S2, S2); const c = S2 / 2, R = 92;
      a.layers.forEach((l, i) => {
        const a0 = -Math.PI / 2 + i * Math.PI / 4 + 0.04, a1 = a0 + Math.PI / 4 - 0.08;
        const shown = i < upto; x.lineWidth = 16; x.lineCap = "butt";
        x.strokeStyle = !shown ? "#1f242b" : l.pass === true ? "#22c55e" : l.pass === false ? "#ef4444" : "#2a3038";
        if (i === beam) x.strokeStyle = "#22d3ee";
        x.beginPath(); x.arc(c, c, R, a0, a1); x.stroke();
      });
      x.fillStyle = "#f5f7fa"; x.font = "700 30px system-ui"; x.textAlign = "center"; x.textBaseline = "middle";
      const n = Math.min(upto, a.layersChecked); x.fillText(n + "/8", c, c - 6);
      x.fillStyle = "#8a93a0"; x.font = "12px system-ui"; x.fillText("layers", c, c + 18);
    };
    const total = a.layersChecked || 0;
    if (reduced || still || a.running) { draw(a.running ? a.layers.filter(l => l.pass != null).length : total, -1); return; }
    lis.forEach(li => li.style.opacity = ".25");
    let i = 0;
    const step = () => {
      if (!document.body.contains(cv)) return;
      if (i < total) { draw(i, i); lis[i] && (lis[i].classList.add("run"), lis[i].style.opacity = "1");
        setTimeout(() => { lis[i] && lis[i].classList.remove("run"); i++; draw(i, -1); setTimeout(step, 0); }, 350); }
      else setTimeout(() => { draw(total, -1); lis.forEach(li => li.style.opacity = ""); }, 150);
    };
    step();
  }

  function coin(mint) {
    const g = gate(); if (g) return g;
    const c = F(`coin/${mint}.json`), info = coins().get(mint);
    const name = c?.name || info?.name || short(mint), link = c?.coin_url || info?.link;
    let h = `<a class="back" href="javascript:history.back()">‹ Back</a>`;
    if (!c) {
      const sc = info?.scan;
      h += `<div class="card"><div class="sub">${esc(info?.symbol || "")} · ${esc(short(mint))}</div><div class="big" style="font-size:28px">${esc(name)}</div>
        ${sc ? `<div class="sub" style="margin-top:6px">Scanner · ${sc.verdict === "pass" ? '<span class="green">passed</span>' : '<span class="red">' + esc(sc.reason) + "</span>"} · liq $${Math.round(sc.liquidityUsd || 0).toLocaleString()} · ${tm(sc.ts)}</div>` : ""}</div>`;
      h += S.missing.has(`coin/${mint}.json`) ? emptyBox("No chart for this coin", "The desk publishes charts for coins it audits or trades.") : skeleton();
      if (link) h += `<a class="btn fomo" href="${esc(link)}" target="_blank" rel="noopener">Open on FOMO</a>`;
      return h;
    }
    const p = c.position;
    h += `<div class="card"><div class="rowx"><div><div class="sub">${esc(c.symbol || "")} · ${esc(short(mint))}</div><div class="big" style="font-size:28px">${esc(name)}</div></div>
      <div class="r" style="text-align:right"><div style="font-weight:700;font-size:18px">${price(c.priceUsd)}</div><div class="sub">${c.holders != null ? c.holders + " holders" : ""}</div></div></div>
      <canvas id="coinChart" style="margin-top:10px"></canvas><div id="coinChartEmpty" class="sub hidden" style="padding:24px 0;text-align:center">No price data published yet</div>
      ${c.riskLine ? `<div class="pills"><span class="pill amber">Risk trailing stop ${price(c.riskLine.price)}</span></div>` : ""}</div>`;
    if (p) h += `<div class="card"><div class="rowx"><div><div class="t" style="font-weight:700">${p.status === "closed" ? "Closed" : "Open"} · ${money(p.sizeUsd)}</div><div class="sub">entry ${price(p.entryPrice)} · stop -${p.stopPct}%${p.trailActive ? " · trailing" : ""}</div></div>
      <div class="r ${((p.pnlUsd ?? 0) >= 0 && p.pct >= 0) ? "green" : "red"}" style="font-weight:700">${p.status === "closed" ? money(p.pnlUsd) : pct(p.pct)}</div></div></div>`;
    const au = c.audit, an = c.analyst;
    h += `<div class="grid2">
      <a class="card tap" href="#/audit/${encodeURIComponent(mint)}"><div class="sub">Auditor</div><div style="font-weight:700;margin-top:4px">${au ? (au.passed ? '<span class="green">8/8</span>' : '<span class="red">' + esc(au.reason) + "</span>") : "—"}</div></a>
      <div class="card"><div class="rowx"><div><div class="sub">Analyst</div><div style="font-weight:700;margin-top:4px">${an ? (an.passed ? '<span class="green">pass</span>' : '<span class="red">' + esc(an.failed[0]) + "</span>") : "—"}</div></div>
      <canvas class="ring" data-score="${an ? an.score : ""}" data-min="70"></canvas></div></div></div>`;
    if (link) h += `<a class="btn fomo" href="${esc(link)}" target="_blank" rel="noopener">${p && p.status !== "closed" ? "Sell" : "Open"} ${esc(name)} on FOMO</a>`;
    return h;
  }

  const GROUPS = { scanner: "Scanner", auditor: "Auditor", analyst: "Analyst", executor: "Executor", risk: "Risk", gmgn: "GMGN", desk: "Desk" };
  function account() {
    const g = gate(); if (g) return g;
    const a = F("account.json"), st = F("state.json");
    if (!a) return emptyBox("Account not published yet", "");
    const gm = a.gmgn || st?.gmgn;
    let h = `<h1>Account</h1>`;
    if (gm) h += gmgnWalletCard(gm);
    h += `<div class="card list">
      <div class="row"><div class="grow"><div class="t">FOMO mode</div><div class="s">Change in Telegram: paper | live</div></div><span class="badge ${a.mode === "LIVE" ? "live" : ""}">${esc(a.mode)}</span></div>
      <div class="row"><div class="grow"><div class="t">${a.mode === "PAPER" ? "FOMO practice balance (PAPER, not real money)" : "FOMO LIVE stack"}</div><div class="s">Change in Telegram: stack 1240</div></div><div class="r">${money(a.stack)}</div></div>
      <a class="row" href="${esc(a.profile_url)}" target="_blank" rel="noopener"><div class="grow"><div class="t">FOMO profile</div><div class="s">@${esc(a.fomoHandle)}</div></div><div class="r cyan">Open ›</div></a>
      <button class="row" id="invite" style="width:100%;text-align:left"><div class="grow"><div class="t">Invite a friend</div><div class="s">10% off FOMO fees through your link</div></div><div class="r cyan">Share ›</div></button>
      <div class="row"><div class="grow"><div class="t">Notifications</div><div class="s wrap">Notifications come through Telegram (@Denarpobot)</div></div></div>
    </div>`;
    h += `<h1>Agents</h1><div class="card list">${AGENTS.map(n => { const hb = st?.agents?.[n]; return `<div class="row"><div class="grow"><div class="t">${n[0].toUpperCase() + n.slice(1)}</div><div class="s">${hb?.paused ? "paused · resume " : "running · pause "}${n[0].toUpperCase() + n.slice(1)}</div></div><span class="toggle ${hb?.paused ? "" : "on"}" aria-disabled="true"></span></div>`; }).join("")}</div>`;
    const by = {};
    a.settings.forEach(s => { const gk = s.key.split(".")[0]; (by[gk] = by[gk] || []).push(s); });
    Object.keys(GROUPS).forEach(gk => {
      if (!by[gk]) return;
      h += `<h1>${GROUPS[gk]}</h1><div class="card">${by[gk].map(s => {
        const label = esc(s.key.split(".")[1].replace(/_/g, " "));
        const slider = (s.kind === "float" || s.kind === "int") && s.min != null && s.max != null
          ? `<input type="range" disabled min="${s.min}" max="${s.max}" step="any" value="${s.value}" aria-label="${label}">` : "";
        return `<div class="set"><span class="k">${label}</span><span class="v">${esc(s.display)}</span>${slider}${s.min != null ? `<span class="sub" style="grid-column:1/-1">range ${esc(s.min)} to ${esc(s.max)}</span>` : ""}</div>`;
      }).join("")}<div class="hint">Change in Telegram: e.g. set stop to -8%</div></div>`;
    });
    if (a.changeLog?.length) h += `<h1>Changes</h1><div class="card list">${a.changeLog.slice(0, 10).map(c => `<div class="row"><div class="grow"><div class="t">${esc(c.key.split(".")[1].replace(/_/g, " "))}</div><div class="s">${esc(c.source)} · ${tm(c.t)}</div></div><div class="r">${esc(JSON.stringify(c.old))} → ${esc(JSON.stringify(c.new))}</div></div>`).join("")}</div>`;
    h += `<a class="btn ghost" href="${esc(a.SIGNUP_URL)}" target="_blank" rel="noopener">New to FOMO? Sign up</a>`;
    return h;
  }

  // ---------- render
  function render() {
    chrome();
    const r = route();
    const html = r.name === "agents" ? agents() : r.name === "activity" ? activity() : r.name === "account" ? account()
      : r.name === "audit" ? audit(r.arg) : r.name === "coin" ? coin(r.arg) : (r.name === "scores" || r.name === "live" || r.name === "scanner") ? scores() : home();
    V.innerHTML = html;
    chrome();
    document.querySelectorAll(".score-row").forEach(el => {
      el.addEventListener("click", e => { if (e.target.closest("a")) return; el.classList.toggle("open"); });
    });
    // post-render drawing
    const ch = F("chart.json"), st = F("state.json");
    const hc = $("#homeChart");
    if (hc && ch) {
      const marks = (ch.trades || []).flatMap(t => [{ t: t.openedAt, color: "#22d3ee" }].concat(t.closedAt ? [{ t: t.closedAt, color: (t.pnlUsd ?? 0) >= 0 ? "#22c55e" : "#ef4444" }] : []));
      const ok = Charts.line(hc, ch.points, { start: st?.startBalance, marks });
      if (!ok) { hc.classList.add("hidden"); $("#homeChartEmpty").classList.remove("hidden"); }
    }
    const cc = $("#coinChart"), cr = route();
    if (cc) { const c = F(`coin/${cr.arg}.json`); const ok = c && Charts.candles(cc, c.candles, { riskLine: c.riskLine?.price, marks: c.marks });
      if (!ok) { cc.classList.add("hidden"); $("#coinChartEmpty").classList.remove("hidden"); } }
    document.querySelectorAll("canvas.ring").forEach(cv => Charts.ring(cv, cv.dataset.score === "" ? null : +cv.dataset.score, +cv.dataset.min, 56));
    document.querySelectorAll(".bar>i").forEach(i => { const v = i.dataset.fill; requestAnimationFrame(() => i.style.width = v + "%"); });
    if (cr.name === "audit") { const a = F(`audit/${cr.arg}.json`); const key2 = a && (cr.arg + a.ts);
      if (a) { const fresh = render._audit !== key2; render._audit = key2; discAnim(a, !fresh); } }
    else render._audit = null;
    const inv = $("#invite");
    if (inv) inv.onclick = async () => {
      const url = F("account.json")?.SIGNUP_URL; if (!url) return;
      try { if (navigator.share) { await navigator.share({ title: "FOMO", text: "New to FOMO? 10% off fees with my link", url }); return; } } catch (e) { if (e.name === "AbortError") return; }
      try { await navigator.clipboard.writeText(url); inv.querySelector(".r").textContent = "Copied ✓"; } catch (e) { prompt("Copy your invite link", url); }
    };
  }

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  render(); poll();
})();
