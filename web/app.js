/* Grok Bot desk app (Plan B, read-only). Decrypts the desk's published JSON with WebCrypto.
   Never builds FOMO URLs: every link comes from coin_url / link / profile_url / SIGNUP_URL in the data. */
(function () {
  "use strict";
  const POLL_S = 30, LIVE_POLL_S = 15, SALT = "grok-bot-desk/v1/salt", ITER = 200000;
  const AGENTS = ["scanner", "auditor", "analyst", "executor", "risk"];
  const CORE = ["state.json", "chart.json", "activity.json", "account.json", "scores.json", "feed.json", "trades.json", "scanner.json", "strat_b.json", "strat_b_live.json", "strat_feed.json", ...AGENTS.map(a => `agents/${a}.json`)];
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
      history.replaceState(null, "", location.pathname + location.search + "#/wallet");
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

  // ---------- phone notifications (DECISION 62). The phone tells the desk where to push through an encrypted note on a
  // private ntfy.sh topic (a hash of the token, never the token). Nothing here can trade: Approve/Skip open Telegram.
  const PUSH_SALT = "grok-bot-desk/v1/push:", NTFY = "https://ntfy.sh/";
  const PCATS = [["trades", "Trades", "GMGN live buys and sells with P&L"], ["approvals", "Approvals", "Big market-cap buys waiting for you, with Approve / Skip"],
    ["alerts", "Alerts", "Stop or sell failures, out of SOL, desk problems"], ["pipeline", "Pipeline", "A coin passing Scanner → Auditor → Analyst (one notification per coin, updated in place)"],
    ["stratb", "Strategy B", "Paper buys and sells, labelled PAPER"], ["paper", "FOMO paper test", "Paper test buys and sells"]];
  const PDEF = { trades: true, approvals: true, alerts: true, pipeline: true, stratb: true, paper: false };
  const P = { busy: false, msg: "", sub: null, hash: null, checked: false };
  function pushCats() { try { return Object.assign({}, PDEF, JSON.parse(localStorage.getItem("desk.pushCats") || "{}")); } catch (_) { return Object.assign({}, PDEF); } }
  let ekeyP = null;
  function ekey() {
    if (!ekeyP) ekeyP = crypto.subtle.importKey("raw", enc.encode(TOKEN()), "PBKDF2", false, ["deriveKey"])
      .then(k => crypto.subtle.deriveKey({ name: "PBKDF2", salt: enc.encode(SALT), iterations: ITER, hash: "SHA-256" }, k, { name: "AES-GCM", length: 256 }, false, ["encrypt"]));
    return ekeyP;
  }
  const toB64 = u8 => btoa(String.fromCharCode(...new Uint8Array(u8)));
  const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
  function b64uBytes(s) { s = s.replace(/-/g, "+").replace(/_/g, "/"); return Uint8Array.from(atob(s + "=".repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0)); }
  async function topicName() { return "gbdesk-" + hex(await crypto.subtle.digest("SHA-256", enc.encode(PUSH_SALT + TOKEN()))).slice(0, 40); }
  async function relay(msg) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await ekey(), enc.encode(JSON.stringify(Object.assign({}, msg, { ts: Date.now() }))));
    const r = await fetch(NTFY + await topicName(), { method: "POST", body: JSON.stringify({ v: 1, iv: toB64(iv), ct: toB64(ct) }) });
    if (!r.ok) throw new Error("relay HTTP " + r.status);
  }
  const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const deviceName = () => (/Android/.test(navigator.userAgent) ? "Android" : /iPhone|iPad/.test(navigator.userAgent) ? "iPhone" : "Desktop") + (/Chrome\//.test(navigator.userAgent) ? " Chrome" : "");
  async function refreshSub() {
    if (!pushSupported()) { P.checked = true; return; }
    try {
      const reg = await navigator.serviceWorker.ready;
      P.sub = await reg.pushManager.getSubscription();
      P.hash = P.sub ? hex(await crypto.subtle.digest("SHA-256", enc.encode(P.sub.endpoint))).slice(0, 16) : null;
    } catch (_) { P.sub = null; P.hash = null; }
    P.checked = true;
  }
  function pushOn() { return pushSupported() && Notification.permission === "granted" && !!P.sub; }
  function deskHasMe() { const subs = F("state.json")?.push?.subs; return subs && P.hash ? subs.some(s => s.id === P.hash) : null; }
  async function enablePush() {
    const vapid = F("state.json")?.push?.vapid;
    if (!vapid) { P.msg = "The desk hasn't published its notification key yet. Pull down to refresh in a minute and tap again."; render(); return; }
    P.busy = true; P.msg = ""; render();
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") throw new Error(perm === "denied" ? "blocked" : "dismissed");
      const reg = await navigator.serviceWorker.ready;
      const want = b64uBytes(vapid);
      let sub = await reg.pushManager.getSubscription();
      const have = sub?.options?.applicationServerKey;
      if (sub && have && toB64(have) !== toB64(want)) { await sub.unsubscribe(); sub = null; }
      if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: want });
      await relay({ type: "sub", sub: sub.toJSON(), cats: pushCats(), device: deviceName() });
      localStorage.setItem("desk.pushSent", String(Date.now()));
      P.msg = "Done ✓ A “Notifications on ✓” alert should pop up within a few seconds.";
    } catch (e) {
      P.msg = e.message === "blocked" ? "Notifications are blocked for this app. In Chrome tap ⋮ → Settings → Site settings → Notifications, allow this site (or long-press the app icon → App info → Notifications → on), then tap the button again."
        : e.message === "dismissed" ? "The prompt was closed. Tap the button again and choose Allow." : "Couldn't turn on notifications: " + e.message;
    }
    P.busy = false; await refreshSub(); render();
  }
  async function savePushCats(cats) {
    localStorage.setItem("desk.pushCats", JSON.stringify(cats));
    if (P.sub) { try { await relay({ type: "sub", sub: P.sub.toJSON(), cats, device: deviceName() }); P.msg = "Saved ✓"; } catch (e) { P.msg = "Couldn't save: " + e.message; } }
    render();
  }
  async function testPush() {
    if (!P.sub) return;
    try { await relay({ type: "test", endpoint: P.sub.endpoint }); P.msg = "Test sent. It should arrive within a few seconds."; } catch (e) { P.msg = "Couldn't send the test: " + e.message; }
    render();
  }
  async function disablePush() {
    try { if (P.sub) { await relay({ type: "unsub", endpoint: P.sub.endpoint }); await P.sub.unsubscribe(); } P.msg = "Notifications are off on this phone."; } catch (e) { P.msg = "Couldn't turn off: " + e.message; }
    await refreshSub(); render();
  }
  async function pushResync() {                 // the phone is subscribed but the desk doesn't list it: tell it again (max every 10 min)
    if (!pushOn() || deskHasMe() !== false) return;
    if (Date.now() - +(localStorage.getItem("desk.pushSent") || 0) < 10 * 60 * 1000) return;
    try { await relay({ type: "sub", sub: P.sub.toJSON(), cats: pushCats(), device: deviceName() }); localStorage.setItem("desk.pushSent", String(Date.now())); } catch (_) {}
  }
  function pushBanner() {
    if (!P.checked || !pushSupported() || pushOn()) return "";
    if (+(localStorage.getItem("desk.pushLater") || 0) > Date.now()) return "";
    const denied = Notification.permission === "denied";
    return `<div class="card pushcta"><div class="t1">🔔 Get live notifications</div>
      <div class="sub wrap">Buys, sells with P&amp;L, approvals and alerts pop up on your phone right away. Telegram keeps working as a backup.</div>
      <button class="btn pushbtn" id="pushOn"${P.busy ? " disabled" : ""}>${P.busy ? "Turning on…" : denied ? "Notifications blocked · how to fix" : "Turn on notifications"}</button>
      ${P.msg ? `<div class="hint">${esc(P.msg)}</div>` : ""}<button class="later" id="pushLater">Not now</button></div>`;
  }
  function pushSection() {
    if (!pushSupported()) return `<h1>Notifications</h1><div class="card"><div class="sub wrap">This browser can't show notifications. Open the app in Chrome on your phone.</div></div>`;
    const on = pushOn(), cats = pushCats(), me = deskHasMe(), last = F("state.json")?.push?.last;
    let h = `<h1>Notifications</h1><div class="card">`;
    if (!on) {
      h += `<div class="sub wrap">Off on this phone. Telegram (@Denarpobot) still gets everything.</div>
        <button class="btn pushbtn" id="pushOn"${P.busy ? " disabled" : ""}>${P.busy ? "Turning on…" : "🔔 Turn on notifications"}</button>`;
    } else {
      h += `<div class="row"><div class="grow"><div class="t">On for this phone</div><div class="s wrap">${me === true ? "The desk has this phone ✓" : me === false ? "Waiting for the desk to confirm (next update)" : "Checking…"}${last ? " · last sent " + esc(tm(last.t)) : ""}</div></div><span class="toggle on"></span></div>`;
      h += PCATS.map(([k, label, sub]) => `<label class="row pcat"><div class="grow"><div class="t">${label}</div><div class="s wrap">${sub}</div></div>
        <input type="checkbox" class="pchk" data-cat="${k}"${cats[k] ? " checked" : ""}><span class="toggle${cats[k] ? " on" : ""}"></span></label>`).join("");
      h += `<div class="ovr"><button class="btn ghost" id="pushTest">Send a test</button><button class="btn ghost" id="pushOff">Turn off</button></div>`;
    }
    if (P.msg) h += `<div class="hint">${esc(P.msg)}</div>`;
    return h + `</div>`;
  }
  function bindPush() {
    const on = $("#pushOn");
    if (on) on.onclick = () => { if (Notification.permission === "denied") { P.msg = "Notifications are blocked for this app. In Chrome tap ⋮ → Settings → Site settings → Notifications, allow this site (or long-press the app icon → App info → Notifications → on), then tap the button again."; render(); return; } enablePush(); };
    const later = $("#pushLater"); if (later) later.onclick = () => { localStorage.setItem("desk.pushLater", String(Date.now() + 24 * 3600 * 1000)); render(); };
    const t = $("#pushTest"); if (t) t.onclick = testPush;
    const off = $("#pushOff"); if (off) off.onclick = disablePush;
    document.querySelectorAll(".pchk").forEach(cb => cb.onchange = () => { const c = pushCats(); c[cb.dataset.cat] = cb.checked; savePushCats(c); });
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
  // ---------- live source (DECISION 75): the droplet serves the same encrypted files ~2s fresh; Pages is the fallback.
  // The bearer is a hash of the token (the token itself never leaves the phone); the data stays encrypted end to end.
  const LIVE_URL = "https://165-227-20-72.sslip.io/live/v1/bundle", LIVE_S = 2, LIVE_TIMEOUT_MS = 4000, LIVE_RETRY_MS = 30000;
  const L = { boot: null, seq: {}, heavyAt: 0, builtAtMs: 0, okAt: 0, failAt: 0, src: null, bearer: null, bearerFor: null };
  window.__deskLive = L;
  // small files every poll; the big, constantly changing ones only for the view on screen (+ all of them every 2 min)
  const LIGHT = ["state.json", "chart.json", "activity.json", "account.json", "scores.json", "trades.json", "strat_b.json", "strat_feed.json"];
  const HEAVY_ALL = ["feed.json", "scanner.json", "strat_b_live.json", ...AGENTS.map(a => `agents/${a}.json`)];
  const VIEW_HEAVY = { alive: ["feed.json"], scores: ["feed.json"], scanner: ["scanner.json"], bwatch: ["strat_b_live.json"],
    agents: AGENTS.map(a => `agents/${a}.json`), coin: ["feed.json", "agents/analyst.json"], audit: ["feed.json", "agents/analyst.json"] };
  async function liveBearer() {
    if (L.bearerFor !== TOKEN()) {
      const h = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode("grok-bot-desk/v1/live:" + TOKEN())));
      L.bearer = Array.from(h, b => b.toString(16).padStart(2, "0")).join(""); L.bearerFor = TOKEN();
    }
    return L.bearer;
  }
  async function fetchLive(view, coin) {
    const all = !S.files["feed.json"] || Date.now() - L.heavyAt > 120000;
    const want = LIGHT.concat(all ? HEAVY_ALL : (VIEW_HEAVY[view] || []));
    const q = new URLSearchParams();
    if (L.boot) q.set("b", L.boot);
    q.set("w", want.map(f => f.replace(/\.json$/, "") + "." + (L.seq[f] || 0)).join(","));
    if (coin) q.set("coin", coin);
    const ac = new AbortController(); const to = setTimeout(() => ac.abort(), LIVE_TIMEOUT_MS);
    let r;
    try { r = await fetch(LIVE_URL + "?" + q, { cache: "no-store", headers: { Authorization: "Bearer " + await liveBearer() }, signal: ac.signal, referrerPolicy: "no-referrer" }); }
    finally { clearTimeout(to); }
    if (!r.ok) throw new Error("live " + r.status);
    const out = await r.json();
    const dec = await Promise.all(Object.entries(out.files || {}).map(async ([f, env]) => [f, await decrypt(env)]));
    if (out.boot !== L.boot) { L.boot = out.boot; L.seq = {}; }
    for (const [f, obj] of dec) { S.files[f] = obj; S.missing.delete(f); L.seq[f] = (out.seqs || {})[f] || 0; }
    // a file left out is unchanged, or only its clock fields ticked (ages grow from its own ts), so it is kept as is
    if (all) L.heavyAt = Date.now();
    L.builtAtMs = out.builtAtMs || Date.now(); L.okAt = Date.now(); L.src = "droplet";
    return dec.length;
  }
  let pollTimer = null, countdown = null, nextAt = 0;
  async function poll() {
    clearTimeout(pollTimer);
    if (!TOKEN() || !DPATH()) { render(); return; }
    if (document.hidden) return;                      // paused while hidden; visibilitychange resumes
    const r = route();
    const coin = (r.name === "coin" || r.name === "audit") ? r.arg : null;
    let live = false, changed = 1;
    if (!L.failAt || Date.now() - L.failAt > LIVE_RETRY_MS) {
      try { changed = await fetchLive(r.name, coin); live = true; L.failAt = 0; }
      catch (e) { L.failAt = Date.now(); L.src = "pages"; }
    }
    let failed = false;
    const want = live ? [] : CORE.slice();
    if (coin) for (const f of [`coin/${coin}.json`, `audit/${coin}.json`]) if (!live || !S.files[f]) want.push(f);
    await Promise.all(want.map(f => fetchFile(f).catch(e => { if (e.net && !live) failed = true; })));
    if (!live && !failed) L.src = "pages";
    S.loadedOnce = true;
    if (failed) { S.offline = S.offline || Date.now(); }
    else { S.offline = null; if (S.files["state.json"]) S.lastGood = S.files["state.json"].ts; }
    const delay = live ? LIVE_S : LIVE_HASH[r.name] ? LIVE_POLL_S : POLL_S;
    nextAt = Date.now() + delay * 1000;
    const view = r.name + "/" + (r.arg || "");
    if (!live || changed || want.length || poll._view !== view || !S.rendered) { render(); S.rendered = true; } else chrome();
    poll._view = view;
    if (!live || Date.now() - (poll._sync || 0) > 30000) { poll._sync = Date.now(); pushResync(); }
    pollTimer = setTimeout(poll, delay * 1000);
  }
  function srcPill() {
    const el = $("#srcPill"); if (!el) return;
    const st = F("state.json");
    if (L.src === "droplet" && Date.now() - L.okAt < 15000) {
      const age = Math.max(0, (Date.now() - L.builtAtMs) / 1000);
      el.className = "srcpill on"; el.textContent = `● live ${ago(age)}`;
      el.title = "Straight from the desk server";
    } else if (st) {
      const age = Math.max(0, (Date.now() - new Date(st.ts)) / 1000);
      el.className = "srcpill"; el.textContent = `Pages ${ago(age)}`;
      el.title = "Backup copy via GitHub Pages";
    } else el.className = "srcpill hidden";
  }
  setInterval(srcPill, 1000);
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
  // Owner 2026-11-17 5:59 PM CT: paper views hidden (data kept); old paper routes land on the LIVE views.
  // Owner 2026-11-17 6:20 PM CT (DECISION 72): Wallet / Strat A / Strat B tabs, each strategy with its own feed. Home folds into Wallet.
  const ALIAS = { home: "wallet", live: "bwatch", stratblive: "bwatch", paper: "strata", apaper: "strata", bpaper: "stratb" };
  const LIVE_HASH = { scores: 1, scanner: 1, alive: 1, bfeed: 1, bwatch: 1, wallet: 1, strata: 1, stratb: 1 };   // 15s polls: wallet + budgets stay fresh
  function applyAlias() {
    const raw = location.hash.replace(/^#\/?/, "");
    const name = raw.split("/")[0] || "wallet";
    if (!ALIAS[name]) return;
    const rest = raw.split("/").slice(1).filter(Boolean).join("/");
    history.replaceState(null, "", location.pathname + location.search + "#/" + ALIAS[name] + (rest ? "/" + rest : ""));
  }
  function route() {
    applyAlias();
    const p = location.hash.replace(/^#\/?/, "").split("/");
    return { name: p[0] || "wallet", arg: p[1] ? decodeURIComponent(p[1]) : null };
  }
  window.addEventListener("hashchange", () => { window.scrollTo(0, 0); const r = route(); if (r.name === "coin" || r.name === "audit" || LIVE_HASH[r.name]) poll(); else render(); });

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
    const bm = F("strat_b.json")?.mode, am = gmgnState()?.mode;
    if (bm === "LIVE" || am === "LIVE") { b.classList.remove("hidden"); b.textContent = bm === "LIVE" && am === "LIVE" ? "A+B LIVE" : bm === "LIVE" ? "B LIVE" : "A LIVE"; b.classList.add("live"); }
    else if (st) { b.classList.remove("hidden"); b.textContent = st.mode; b.classList.toggle("live", st.mode === "LIVE"); }
    else b.classList.add("hidden");
    const act = F("activity.json"); const seen = +(localStorage.getItem("desk.seen") || 0);
    const top = act && act.items && act.items[0] ? act.items[0].id : 0;
    $("#bellDot").classList.toggle("hidden", !(top > seen));
    const r = route().name;
    const on = { wallet: r === "wallet" || r === "coin" || r === "audit", stratb: r === "stratb" || r === "bfeed" || r === "bwatch",
      strata: r === "strata" || r === "alive" || r === "scores" || r === "scanner" };
    document.querySelectorAll(".tabs a").forEach(a => a.classList.toggle("on", a.dataset.tab === r || !!on[a.dataset.tab]));
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
    const title = `Real wallet · shared by A LIVE and B LIVE`;
    const badges = [`<span class="badge live">LIVE</span>`]
      .concat(g.halted ? [`<span class="badge">HALTED</span>`] : []).join("");
    const usd = g.usd != null ? money(g.usd) : "USD —";
    const when = g.updatedAt ? ` · updated ${tm(g.updatedAt)}` : "";
    const bg = g.budgets || {};
    const split = (bg.A && bg.B) ? `<span class="pill">A ${bg.A.sharePct}% · B ${bg.B.sharePct}%</span>` : "";
    return `<div class="card real"><div class="rowx"><div class="sub">${title}</div><div class="pills" style="margin:0">${badges}</div></div>
      <div class="big">${solAmt(g.sol)}</div>
      <div class="sub">${usd} · ${esc(g.walletShort || short(g.wallet))}${when} · ${g.reserveSol} SOL kept for fees</div>
      <div class="pills"><span class="pill">${money(g.capTradeUsd)} max / trade</span>${split}</div></div>`;
  }
  function exitsLine(ex) {
    const on = ex && ex.attached ? " · exits on" : " · exits?";
    if (ex && ex.label) return ex.label + on;
    if (ex && (ex.phase === "grace" || (ex.graceMin && ex.graceStopPct != null && ex.normalStopPct != null)))
      return `stop -${ex.graceStopPct}% for first ${ex.graceMin ?? 5} min, then -${ex.normalStopPct}%` + on;
    return `stop -${(ex && ex.stopPct) ?? 15}% · trail ${(ex && ex.trailPct) ?? 20}%` + on;
  }
  function gmgnPosRow(p) {
    const name = p.name || p.symbol || short(p.mint);
    const ex = p.exits || {};
    const exits = exitsLine(ex);
    const inner = `<div class="ico ${(p.pct ?? 0) >= 0 ? "green" : "red"}">${esc((p.symbol || p.name || "?").slice(0, 2))}</div>
      <div class="grow"><div class="t">${esc(name)} <span class="badge ${p.mode === "LIVE" ? "live" : ""}" style="margin-left:6px">${esc(p.mode)}</span></div>
      <div class="s">${money(p.sizeUsd)} · ${exits}</div></div>
      <div class="r"><div class="${(p.pct ?? 0) >= 0 ? "green" : "red"}">${p.pnlUsd != null ? money(p.pnlUsd) : pct(p.pct)}</div><div class="s">${pct(p.pct)}</div></div>`;
    return p.mint ? `<a class="row" href="#/coin/${encodeURIComponent(p.mint)}">${inner}</a>` : `<div class="row">${inner}</div>`;
  }
  function segBar(items, label) {
    const r = route().name;
    return `<div class="seg seg${items.length}" role="tablist" aria-label="${esc(label)}">` + items.map(([h, n, t]) => {
      const on = Array.isArray(n) ? n.includes(r) : r === n;
      return `<a href="#/${h}" class="${on ? "on" : ""}" role="tab" aria-selected="${on}">${t}</a>`; }).join("") + `</div>`;
  }
  function stratATabs() { return segBar([["strata", "strata", "Overview"], ["alive", "alive", "Feed"], ["scores", "scores", "Scores"], ["scanner", "scanner", "Scanner"]], "Strategy A views"); }
  function stratBTabs() { return segBar([["stratb", "stratb", "Overview"], ["bfeed", "bfeed", "Feed"], ["bwatch", "bwatch", "Watchlist"]], "Strategy B views"); }
  function bypassBadge(r) { return r.bypass ? `<span class="badge bypass${r.tag === "OVERRIDE" ? " override" : ""}">${esc(r.tag || "BYPASS")} · score ${r.score ?? "?"}</span>` : ""; }
  function tradeRow(r) {
    const name = r.name || r.symbol || short(r.mint);
    const p = r.pct ?? 0, up = p >= 0;
    const src = r.source === "GMGN" ? "GMGN" : "FOMO";
    let sub;
    if (r.open) {
      const ex = r.exits || {};
      sub = r.source === "GMGN" ? `${money(r.sizeUsd)} · ${exitsLine(ex)}`
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
  function tonightCounts(st) {
    if (!st || !st.counts) return "";
    const c = st.counts;
    return `<h1>Desk pipeline tonight</h1><div class="grid2">
      <div class="card stat"><div class="v">${c.pools}</div><div class="k">pools seen</div></div>
      <div class="card stat"><div class="v">${c.scannerPassed}</div><div class="k">passed Scanner</div></div>
      <div class="card stat"><div class="v">${c.audited - c.auditRejected}/${c.audited}</div><div class="k">cleared Auditor</div></div>
      <div class="card stat"><div class="v">${F("trades.json")?.real?.closedTonight ?? 0}</div><div class="k">A LIVE trades closed</div></div></div>`;
  }
  function shareLine(bu, who) {
    if (!bu) return "";
    const b = bu.budgetUsd != null ? money(bu.budgetUsd) : (bu.budgetSol != null ? solAmt(bu.budgetSol) : "—");
    const sh = bu.shareUsd != null ? money(bu.shareUsd) : (bu.shareSol != null ? solAmt(bu.shareSol) : "—");
    return `<div class="pills"><span class="pill">${bu.sharePct}% of wallet · ${sh}</span><span class="pill green">${b} free to buy</span>`
      + `<span class="pill">${money(bu.inPositionsUsd ?? 0)} in ${who} positions</span><span class="pill">${bu.openCount ?? 0}/${bu.maxOpen ?? "?"} open</span></div>`;
  }
  function aBadge(mode) {
    return mode === "LIVE" ? `<span class="badge" style="background:#c62828;color:#fff">A LIVE</span>` : `<span class="badge">A off</span>`;
  }
  // DECISION 72: profit since the owner's reset (strat_feed.json pnl.A / pnl.B), history kept below it
  function sinceText(p) { return p && p.sinceLabel ? `since ${p.sinceLabel}` : "all time"; }
  function profitCard(label, p, mode, badge, budget, who) {
    const v = p?.pnlUsd ?? 0;
    return `<div class="card real"><div class="rowx"><div class="sub">${label}${mode === "LIVE" ? "" : " · not active"}</div>${badge}</div>
      <div class="k2">Profit ${esc(sinceText(p))}</div>
      <div class="big ${v >= 0 ? "green" : "red"}">${money(v)}</div>
      <div class="sub">${p?.closed ?? 0} closed · ${p?.wins ?? 0}W ${p?.losses ?? 0}L${p?.winRatePct == null ? "" : " · " + p.winRatePct + "% win rate"} · tonight ${money(p?.pnlTonightUsd ?? 0)}</div>
      ${shareLine(budget, who)}</div>`;
  }
  const afterSince = (since, t) => !since || (t && new Date(t) >= new Date(since));
  function earlierBlock(rows, rowFn, n) {
    if (!rows.length) return "";
    return `<details class="card list earlier"><summary>Before the reset · ${rows.length} trade${rows.length === 1 ? "" : "s"} (kept, not counted)</summary>${rows.slice(0, n || 15).map(rowFn).join("")}</details>`;
  }
  function aLiveBody(gm, tr, full) {
    const real = tr?.real, on = gm?.mode === "LIVE";
    const p = F("strat_feed.json")?.pnl?.A || (real ? { pnlUsd: real.pnlAllUsd, pnlTonightUsd: real.pnlTonightUsd, wins: real.wins, losses: real.losses, closed: (real.wins || 0) + (real.losses || 0) } : null);
    let h = profitCard("Strategy A · real money · GMGN", p, gm?.mode, aBadge(gm?.mode), gm?.budgets?.A, "A");
    const open = real ? real.open : (gm?.open || []).filter(q => q.mode === "LIVE");
    h += `<h1>A positions</h1>` + (open.length ? `<div class="card list">${open.map(real ? tradeRow : gmgnPosRow).join("")}</div>`
      : emptyBox("No A LIVE positions open", on ? "A buys on its own when a coin passes the desk. Its stop sits on GMGN." : "Strategy A isn't LIVE right now."));
    const since = p?.since || real?.since;
    const closed = real ? real.closed.filter(r => r.pnlUsd != null || r.closedAt) : [];
    const now = closed.filter(r => afterSince(since, r.closedAt)), before = closed.filter(r => !afterSince(since, r.closedAt));
    h += `<h1>A trades ${esc(sinceText(p))}</h1>` + (now.length ? `<div class="card list">${(full ? now : now.slice(0, 10)).map(tradeRow).join("")}</div>` : emptyBox("No A trades closed since the reset", "New closes count toward the profit above."));
    if (full) h += earlierBlock(before, tradeRow);
    return h;
  }
  function stratCard(href, title, badge, p, bu, openN) {
    const v = p?.pnlUsd ?? 0;
    return `<a class="card tap scard" href="${href}"><div class="rowx"><div class="t1">${title}</div>${badge}</div>
      <div class="${v >= 0 ? "green" : "red"} sv">${money(v)}</div><div class="sub">profit ${esc(sinceText(p))}</div>
      <div class="sub" style="margin-top:6px">${bu?.sharePct ?? "?"}% share · <b class="green">${bu?.budgetUsd != null ? money(bu.budgetUsd) : "—"}</b> free</div>
      <div class="sub">${openN ?? 0} open · ${p?.wins ?? 0}W ${p?.losses ?? 0}L ›</div></a>`;
  }
  function wallet() {
    const g = gate(); if (g) return g;
    const gm = F("state.json")?.gmgn || gmgnState(), sf = F("strat_feed.json"), b = F("strat_b.json");
    let h = pushBanner() + `<h1>Main wallet</h1>`;
    if (!gm) return h + emptyBox("Wallet hasn't published yet", "It shows up after the desk's next publish.");
    const bg = gm.budgets || {};
    const usd = gm.usd != null ? money(gm.usd) : "USD —";
    h += `<div class="card real"><div class="rowx"><div class="sub">Shared by A LIVE and B LIVE</div><div class="pills" style="margin:0"><span class="badge live">LIVE</span>${gm.halted ? `<span class="badge">HALTED</span>` : ""}</div></div>
      <div class="big">${solAmt(gm.sol)}</div>
      <div class="sub">${usd}${gm.solUsd ? " · 1 SOL = " + money(gm.solUsd) : ""} · ${esc(gm.walletShort || short(gm.wallet))}${gm.updatedAt ? " · updated " + tm(gm.updatedAt) : ""}</div>
      <div class="pills"><span class="pill">${gm.reserveSol} SOL kept for fees</span><span class="pill">${money(gm.capTradeUsd)} max / trade</span></div></div>`;
    if (bg.A && bg.B) {
      const bar = `<div class="splitbar"><i style="width:${Math.max(0, Math.min(100, bg.A.sharePct))}%"></i></div>`;
      h += `<h1>Split · A ${bg.A.sharePct}% · B ${bg.B.sharePct}%</h1><div class="card">${bar}
        <div class="grid2 split2"><div><div class="sub">A's half</div><div class="sv">${bg.A.shareUsd != null ? money(bg.A.shareUsd) : solAmt(bg.A.shareSol)}</div><div class="sub green">${bg.A.budgetUsd != null ? money(bg.A.budgetUsd) : "—"} free</div><div class="sub">${money(bg.A.inPositionsUsd ?? 0)} in positions</div></div>
        <div><div class="sub">B's half</div><div class="sv">${bg.B.shareUsd != null ? money(bg.B.shareUsd) : solAmt(bg.B.shareSol)}</div><div class="sub green">${bg.B.budgetUsd != null ? money(bg.B.budgetUsd) : "—"} free</div><div class="sub">${money(bg.B.inPositionsUsd ?? 0)} in positions</div></div></div></div>`;
    }
    h += `<h1>Strategies</h1><div class="grid2">` + stratCard("#/strata", "Strat A", aBadge(gm.mode), sf?.pnl?.A, bg.A, bg.A?.openCount)
      + stratCard("#/stratb", "Strat B", bBadge(gm.bMode || b?.mode), sf?.pnl?.B, bg.B, bg.B?.openCount) + `</div>`;
    return h + `<div class="hint">Both strategies trade real money from this wallet, each only from its own share. They never hold the same coin. This app is read-only.</div>`;
  }
  function stratA() {
    const g = gate(); if (g) return g;
    const st = F("state.json"), gm = st.gmgn || gmgnState(), tr = F("trades.json");
    return pushBanner() + stratATabs() + aLiveBody(gm, tr, true);
  }
  // per-strategy event feed (strat_feed.json A / B)
  function evRow(e) {
    const ic = { buy: "↗", sell: "↘", stop: "■", skip: "✕", fail: "!", approval: "?" }[e.kind] || "•";
    const right = e.pnlUsd != null ? `<div class="${e.pnlUsd >= 0 ? "green" : "red"}">${money(e.pnlUsd)}</div><div class="s">${pct(e.pct)}</div>`
      : e.sizeUsd != null ? `<div>${money(e.sizeUsd)}</div>` : "";
    const inner = `<div class="ico ${e.tone === "amber" ? "" : e.tone}">${ic}</div><div class="grow"><div class="t">${esc(e.title)}</div><div class="s wrap">${esc(e.sub || "")} · ${tmCT(e.t)}</div></div><div class="r">${right}</div>`;
    return e.mint ? `<a class="row" href="#/coin/${encodeURIComponent(e.mint)}">${inner}</a>` : `<div class="row">${inner}</div>`;
  }
  function evFeed(who, items, empty) {
    return `<h1>${who} buys, exits and skips</h1>` + ((items || []).length ? `<div class="card list">${items.slice(0, 40).map(evRow).join("")}</div>`
      : emptyBox(`No ${who} activity in the last 24 hours`, empty));
  }
  function aFeed() { return evFeed("A's", F("strat_feed.json")?.A, "A's real buys, sells, stops, skips and approvals show up here."); }
  // ---------- Strategy B (read-only; DECISION 54, B LIVE real money since DECISION 66)
  function bBadge(mode) {
    return mode === "LIVE" ? `<span class="badge" style="background:#c62828;color:#fff">B LIVE</span>` : `<span class="badge">B off</span>`;
  }
  function stratBRow(r) {
    const name = r.name || r.symbol || short(r.mint);
    const p = r.pct ?? 0, up = p >= 0;
    const sub = r.status === "open"
      ? `${money(r.sizeUsd)} in ${tm(r.openedAt)} · stop ${price(r.stopLine)}${r.mode === "LIVE" ? (r.stopOnGmgn ? " (on GMGN)" : " (desk)") : ""} · ${r.trailArmed ? "trail armed " + price(r.trailLine) : "2x " + price(r.tpLine)}`
      : r.status === "closed" ? `${money(r.sizeUsd)} · ${esc(r.exitReason || "closed")} ${tm(r.closedAt)}`
      : `${money(r.sizeUsd)} · ${esc(r.status)} ${tm(r.openedAt)}${r.note ? " · " + esc(r.note) : ""}`;
    return `<a class="row" href="${esc(r.link)}" target="_blank" rel="noopener"><div class="ico ${up ? "green" : "red"}">${esc((r.symbol || r.name || "?").slice(0, 2))}</div>
      <div class="grow"><div class="t">${esc(name)} <span style="margin-left:6px">${bBadge(r.mode)}</span></div><div class="s">${sub}</div>${r.why ? `<div class="s" style="white-space:normal">${esc(r.why)}</div>` : ""}</div>
      <div class="r"><div class="${up ? "green" : "red"}">${money(r.pnlUsd)}</div><div class="s">${pct(r.pct)}</div></div></a>`;
  }

  function bCheck(c) {
    const st = c.pass === true ? `<span class="pf ok">PASS</span>` : c.pass === false ? `<span class="pf bad">FAIL</span>` : `<span class="pf">—</span>`;
    return `<div class="scheck"><span class="k">${esc(c.label)}</span><span class="v">${esc(c.value)}</span><span class="b">${esc(c.bar)}</span>${st}</div>`;
  }
  function bMini(cks) {
    return `<div class="schecks mini">${(cks || []).map(c => `<span class="${c.pass === true ? "ok" : c.pass === false ? "bad" : ""}">${esc(c.label)} ${esc(c.value)}</span>`).join("")}</div>`;
  }
  function bPill(st) {
    const cls = st === "bought" ? "green" : st === "skipped" ? "red" : st === "watching" ? "amber" : "";
    return `<span class="pill ${cls}">${esc(st)}</span>`;
  }
  function bEvalRow(it) {
    const label = it.name || it.symbol || short(it.mint);
    const fails = (it.checks || []).filter(c => c.pass === false).map(c => c.label).join(", ");
    return `<div class="score-row scan-row">
      <div class="rowx"><div class="grow"><div class="t">${esc(label)}</div>
        <div class="s">${tmCT(it.t)}${it.symbol && it.name && it.symbol !== it.name ? " · " + esc(it.symbol) : ""}${it.price ? " · " + price(it.price) : ""}</div>
        <div class="s ${it.status === "skipped" ? "red" : ""}" style="white-space:normal">${esc(it.reason || "")}</div></div>
        <div class="pillcol">${bPill(it.status)}</div></div>
      ${bMini(it.checks)}
      <div class="detail"><div class="schecks">${(it.checks || []).map(bCheck).join("")}</div>
        ${fails ? `<div class="sub" style="margin-top:6px">Failed: ${esc(fails)}</div>` : ""}
        <div class="sub" style="margin-top:6px">${esc(it.mint)}</div>
        <a class="btn fomo" href="${esc(it.link)}" target="_blank" rel="noopener">Open on FOMO</a></div></div>`;
  }
  function bWatchRow(w) {
    const label = w.name || w.symbol || short(w.mint);
    return `<div class="score-row scan-row">
      <div class="rowx"><div class="grow"><div class="t">${esc(label)}</div>
        <div class="s">${w.minLeft}m left · ${w.ageMin ?? "?"}m since graduation${w.price ? " · " + price(w.price) : ""}</div>
        <div class="s" style="white-space:normal">${(w.away || []).length ? "Needs: " + esc(w.away.join(" · ")) : "In the buy zone"}</div></div>
        <div class="pillcol"><span class="pill amber">${w.passCount}/${w.checkCount}</span></div></div>
      ${bMini(w.checks)}
      <div class="detail"><div class="schecks">${(w.checks || []).map(bCheck).join("")}</div>
        <div class="sub" style="margin-top:6px">High ${price(w.high)} · low since high ${price(w.low)} · watching until ${tmCT(w.expiresAt)}</div>
        <a class="btn fomo" href="${esc(w.link)}" target="_blank" rel="noopener">Open on FOMO</a></div></div>`;
  }
  function stratBLive() {
    const g = gate(); if (g) return g;
    const L = F("strat_b_live.json");
    if (route().name === "bfeed") return stratBTabs() + evFeed("B's", F("strat_feed.json")?.B, "B's real buys, sells, stops and skips show up here.");
    let h = stratBTabs();
    if (!L) return h + emptyBox("Strategy B live view hasn't published yet", "It shows up after the desk's next publish.");
    const n = L.counts || {};
    h += `<h1>B watchlist</h1><div class="pills" style="margin:0 0 12px"><span class="pill amber">${n.watching ?? 0} watching</span><span class="pill green">${n.boughtTonight ?? 0} bought</span><span class="pill red">${n.skippedTonight ?? 0} skipped</span><span class="pill">${n.expiredTonight ?? 0} expired</span><span class="pill">${n.evaluationsTonight ?? 0} evaluations</span></div>`;
    h += ((L.watchlist || []).length ? `<div class="card list">${L.watchlist.map(bWatchRow).join("")}</div>`
      : emptyBox("Nothing on the watchlist", "Coins the auditor passes and B's holder checks clear show up here for up to 3 hours."));
    h += `<h1>B evaluations tonight</h1>` + ((L.items || []).length ? `<div class="card list">${L.items.map(bEvalRow).join("")}</div>`
      : emptyBox("No Strategy B evaluations tonight yet", "B looks at every coin the auditor passes."));
    return h + `<div class="sub" style="margin-top:6px">Newest first. A row is added when a coin's verdict changes. Tap a row for every check. ${L.mode === "LIVE" ? "Strategy B is LIVE: its buys are real GMGN orders from your wallet." : "Strategy B isn't LIVE right now and places no real orders."} This app is read-only.</div>`;
  }
  function stratB() {
    const g = gate(); if (g) return g;
    const b = F("strat_b.json");
    let h = pushBanner() + stratBTabs();
    if (!b) return h + emptyBox("Strategy B hasn't published yet", "It shows up after the desk's next publish.");
    return h + bLiveBody(b);
  }
  function bLiveBody(b) {
    const lv = b.live || {};
    const lr = lv.lastRefusal;
    const p = F("strat_feed.json")?.pnl?.B || { pnlUsd: lv.pnlAllUsd, pnlTonightUsd: lv.pnlTonightUsd, closed: lv.closedAll, wins: lv.wins, losses: lv.losses, winRatePct: lv.winRatePct, sinceLabel: lv.sinceLabel };
    const gb = (F("state.json")?.gmgn || gmgnState())?.budgets?.B;     // same numbers as the Wallet tab
    let h = profitCard("Strategy B · real money · GMGN", p, b.mode, bBadge("LIVE"), (gb && gb.budgetSol != null) ? gb : lv.budget, "B");
    if (lr) h += `<div class="sub" style="white-space:normal;margin:-4px 2px 10px">Last skipped: ${esc(lr.name)} ${tmCT(lr.t)}: ${esc(lr.reason)} (${lv.refusedTonight} tonight)</div>`;
    h += `<h1>B positions</h1>` + ((lv.open || []).length ? `<div class="card list">${lv.open.map(stratBRow).join("")}</div>` : emptyBox("No B LIVE positions open", "B will buy when a coin qualifies and its share of the wallet can cover it."));
    h += `<h1>B trades ${esc(sinceText(p))}</h1>` + ((lv.closed || []).length ? `<div class="card list">${lv.closed.map(stratBRow).join("")}</div>` : emptyBox("No B trades closed since the reset", "New closes count toward the profit above."));
    h += earlierBlock(lv.earlier || [], stratBRow);
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
    const okP = false && it.overridablePaper === true, okR = it.overridableReal === true;   // paper overrides hidden (owner 2026-11-17)
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
    return `<h1>Live feed</h1><a class="card tap" href="#/alive">
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
    if (st === "dip") return o === "watching" ? "Waiting for a dip" : o === "dip_buy" ? "Bought the dip" : o === "timeout_buy" ? "Bought (no dip)" : o === "skipped" ? "Dip skipped" : "Dip";
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
    else if (it.outcome === "watching") right = `<div class="pill amber">dip</div>`;
    else if (it.outcome === "dip_buy") right = `<div class="sc green">DIP</div>`;
    else if (it.outcome === "timeout_buy") right = `<div class="sc green">BUY</div>`;
    else if (it.outcome === "skipped" && it.stage === "dip") right = `<div class="pill red">skip</div>`;
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
    const dips = fd?.dips || [];
    let h = `<h1>Live feed</h1>
      <div class="card"><div class="sub">Newest first · every coin the desk evaluates · times in CT</div>
      <div class="sub" style="margin-top:6px">Updates about every minute while this tab is open (the desk publishes shortly after each batch, then this app checks every 15s). Tap an Analyst row for the numbers vs each bar.</div></div>`;
    if (dips.length) {
      h += `<div class="card list"><div class="sub" style="margin:0 0 8px">Waiting for a dip · buy at −${dips[0].needPct ?? 5}% from the high, or at market after ${dips[0].timeoutS ?? 30}s</div>` +
        dips.map(d => {
          const label = d.name || d.symbol || short(d.mint);
          const drop = d.dropPct == null ? "—" : (d.dropPct >= 0 ? "−" + d.dropPct.toFixed(1) + "%" : "+" + Math.abs(d.dropPct).toFixed(1) + "%");
          const href = d.mint ? `#/coin/${encodeURIComponent(d.mint)}` : (d.link || "#/alive");
          return `<a class="live-row" href="${href}"><div class="rowx">
            <div class="grow"><div class="t">${esc(label)}</div>
              <div class="s">Dip watch · high ${price(d.high)} · now ${price(d.lastPrice)} · ${esc(drop)}${d.expiresAt ? " · until " + tmCT(d.expiresAt) : ""}</div></div>
            <div class="pill amber">watching</div>
          </div></a>`;
        }).join("") + `</div>`;
    }
    if (!fd) return h + emptyBox("Waiting for the live feed", "The desk will publish it on the next update.");
    if (!items.length && !dips.length) return h + emptyBox("Nothing evaluated yet", "Scanner kills, rechecks, waiting-room moves, Auditor results and Analyst scores show up here.");
    if (items.length) h += `<div class="card list">${items.map(it => liveRow(it, m, bars)).join("")}</div>`;
    return h;
  }
  function tabToggle() {
    const r = route().name;
    const tab = (h, n, label) => `<a href="#/${h}" class="${r === n ? "on" : ""}" role="tab" aria-selected="${r === n}">${label}</a>`;
    return `<div class="seg seg3" role="tablist" aria-label="Live, Scores or Scanner">
      ${tab("alive", "alive", "Live")}${tab("scores", "scores", "Scores")}${tab("scanner", "scanner", "Scanner")}
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
  function surgePill(it) {
    const g = it.surge; if (!g) return "";
    const w = Number(g.windowS || 60), wl = w % 60 === 0 ? `${w / 60}m` : `${w}s`;
    return `<span class="pill surge" title="MC ${esc(usdK(g.mcFrom))} → ${esc(usdK(g.mcTo))} in ${esc(g.secs)}s">⚡ +${Math.round(Number(g.pct) || 0)}% in ${wl}</span>`;
  }
  function scanRow(it) {
    const label = it.name || it.symbol || it.mintShort || short(it.mint);
    let sub2 = "";
    if (it.pill === "recheck") sub2 = `${it.recheckN ? `recheck ${it.recheckN} failed · ` : "killed · "}recheck ${it.nextRecheckN} at ${tmCT(it.nextAt)}`;
    else if (it.pill === "kill" && it.final) sub2 = `recheck ${it.recheckN} · final`;
    else if (it.pill === "waiting") sub2 = it.recheckN ? `recheck ${it.recheckN} cleared · back in the waiting room` : "waiting room";
    const reason = it.pill !== "pass" && it.reason ? `<div class="s ${it.pill === "waiting" ? "" : "red"}">${esc(it.reason)}</div>` : "";
    const fails = (it.checks || []).filter(c => c.pass === false).map(c => c.label).join(", ");
    return `<div class="score-row scan-row" data-mint="${esc(it.mint || "")}">
      <div class="rowx"><div class="grow"><div class="t">${esc(label)}</div>
        <div class="s">${tmCT(it.t)}${it.symbol && it.name && it.symbol !== it.name ? " · " + esc(it.symbol) : ""}${it.ageMin != null ? " · " + it.ageMin + "m old" : ""}</div>
        ${sub2 ? `<div class="s">${esc(sub2)}</div>` : ""}${reason}</div>
        <div class="pillcol">${surgePill(it)}${scanPill(it)}</div></div>
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
    const sg = L.surge || {};
    const age = Number(L.maxAgeH) ? `max age ${L.maxAgeH}h` : "no age limit";
    h += `<div class="card"><div class="sub">Current limits (from settings)</div><div class="pills">
      <span class="pill">liq ≥ ${usdK(L.minLiquidityUsd)}</span>
      <span class="pill">MC ${Number(L.minMarketCapUsd) ? "≥ " + usdK(L.minMarketCapUsd) : "off"}</span>
      <span class="pill">top 10 ≤ ${L.maxTop10Pct}%</span><span class="pill">1h ≤ +${L.maxH1ChangePct}%</span>
      <span class="pill">pump mint</span><span class="pill">SOL-quoted</span>
      <span class="pill">pool age ≥ ${L.minPoolAgeMin}m</span><span class="pill">${esc(age)}</span>
      ${Number(L.youngPriorityMin) ? `<span class="pill">young first &lt; ${L.youngPriorityMin}m</span>` : ""}</div>
      <div class="sub" style="margin-top:8px">Rechecks at ${esc(L.recheckMinutes)} min, only if close: liq ≥ ${usdK(nm.minLiqUsd)}, MC ≥ ${usdK(nm.minMcUsd)}, top 10 ≤ ${nm.maxTop10Pct}%, 1h ≤ +${nm.maxH1Pct}%</div>
      <div class="sub" style="margin-top:6px">${Number(sg.watchMax) ? `⚡ Surge watch: MC +${sg.pct}% within ${sg.windowS}s re-scans a coin first (all checks still apply) · up to ${sg.watchMax} coins · every ${sg.pollS}s · ${c.surges ?? 0} surge${c.surges === 1 ? "" : "s"} in the last hour` : "⚡ Surge watch off"}</div></div>`;
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
    if (route().name === "scores") return stratATabs() + scoresList(min, items, bars);
    if (route().name === "scanner") return stratATabs() + scannerView();
    return stratATabs() + aFeed() + liveFeed(min) + tonightCounts(F("state.json"));
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
    const fomoPaper = (F("state.json")?.mode || "PAPER") !== "LIVE";
    const isPaperTrade = i => i.mode === "PAPER" || /\bPAPER\b/.test(i.sub || "") || /\bPAPER\b/.test(i.title || "")
      || (fomoPaper && (i.agent === "executor" || i.agent === "risk") && ["buy", "sell", "stop", "dip"].includes(i.kind));
    const items = (a?.items || []).concat(gmgnActivityItems(gmgnState()?.liveOrders || gmgnState()?.orders)).filter(i => !isPaperTrade(i)).sort((x, y) => new Date(y.t) - new Date(x.t));
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
    const p = c.position && c.position.mode !== "PAPER" ? c.position : null;
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
      ${a.mode === "PAPER" ? "" : `<div class="row"><div class="grow"><div class="t">FOMO LIVE stack</div><div class="s">Change in Telegram: stack 1240</div></div><div class="r">${money(a.stack)}</div></div>`}
      <a class="row" href="${esc(a.profile_url)}" target="_blank" rel="noopener"><div class="grow"><div class="t">FOMO profile</div><div class="s">@${esc(a.fomoHandle)}</div></div><div class="r cyan">Open ›</div></a>
      <button class="row" id="invite" style="width:100%;text-align:left"><div class="grow"><div class="t">Invite a friend</div><div class="s">10% off FOMO fees through your link</div></div><div class="r cyan">Share ›</div></button>
    </div>`;
    h += pushSection();
    h += `<h1>Agents</h1><div class="card list">${AGENTS.map(n => { const hb = st?.agents?.[n]; return `<div class="row"><div class="grow"><div class="t">${n[0].toUpperCase() + n.slice(1)}</div><div class="s">${hb?.paused ? "paused · resume " : "running · pause "}${n[0].toUpperCase() + n.slice(1)}</div></div><span class="toggle ${hb?.paused ? "" : "on"}" aria-disabled="true"></span></div>`; }).join("")}</div>`;
    const by = {};
    a.settings.filter(s => !/paper/.test(s.key)).forEach(s => { const gk = s.key.split(".")[0]; (by[gk] = by[gk] || []).push(s); });
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
      : r.name === "audit" ? audit(r.arg) : r.name === "coin" ? coin(r.arg)
      : (r.name === "bfeed" || r.name === "bwatch") ? stratBLive() : r.name === "stratb" ? stratB()
      : (r.name === "scores" || r.name === "scanner" || r.name === "alive") ? scores()
      : r.name === "strata" ? stratA()
      : wallet();
    // same view re-rendered by a poll (every ~2s on the live source): keep what the user opened, don't replay animations
    const same = render._route === r.name + "/" + (r.arg || ""); render._route = r.name + "/" + (r.arg || "");
    const rowKey = (el, i) => (el.dataset.mint || "") + "#" + i;
    const openRows = same ? new Set([...V.querySelectorAll(".score-row")].map((el, i) => el.classList.contains("open") ? rowKey(el, i) : null).filter(Boolean)) : null;
    const closedRows = same ? new Set([...V.querySelectorAll(".score-row")].map((el, i) => el.classList.contains("open") ? null : rowKey(el, i)).filter(Boolean)) : null;
    const openDet = same ? [...V.querySelectorAll("details")].map(d => d.open) : null;
    V.innerHTML = html;
    chrome();
    document.querySelectorAll(".score-row").forEach((el, i) => {
      if (same) { const k = rowKey(el, i); if (openRows.has(k)) el.classList.add("open"); else if (closedRows.has(k)) el.classList.remove("open"); }
      el.addEventListener("click", e => { if (e.target.closest("a")) return; el.classList.toggle("open"); });
    });
    if (same) V.querySelectorAll("details").forEach((d, i) => { if (openDet[i] != null) d.open = openDet[i]; });
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
    document.querySelectorAll(".bar>i").forEach(i => { const v = i.dataset.fill; if (same) { i.style.transition = "none"; i.style.width = v + "%"; } else requestAnimationFrame(() => i.style.width = v + "%"); });
    if (cr.name === "audit") { const a = F(`audit/${cr.arg}.json`); const key2 = a && (cr.arg + a.ts);
      if (a) { const fresh = render._audit !== key2; render._audit = key2; discAnim(a, !fresh); } }
    else render._audit = null;
    bindPush();
    const inv = $("#invite");
    if (inv) inv.onclick = async () => {
      const url = F("account.json")?.SIGNUP_URL; if (!url) return;
      try { if (navigator.share) { await navigator.share({ title: "FOMO", text: "New to FOMO? 10% off fees with my link", url }); return; } } catch (e) { if (e.name === "AbortError") return; }
      try { await navigator.clipboard.writeText(url); inv.querySelector(".r").textContent = "Copied ✓"; } catch (e) { prompt("Copy your invite link", url); }
    };
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
    navigator.serviceWorker.addEventListener("message", e => { const n = e.data && e.data.nav; if (typeof n === "string" && n.startsWith("#/")) location.hash = n; });
  }
  refreshSub().then(() => { render(); pushResync(); });
  render(); poll();
})();
