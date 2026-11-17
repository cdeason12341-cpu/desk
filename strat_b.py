"""STRATEGY B (DECISION 54; PAPER or LIVE since DECISION 66).

A second, separate strategy built from the research Page "What top memecoin traders do vs the desk".
It reads the same scanner + auditor feed as the live GMGN strategy (A) but decides on its own:
it waits out the post-graduation dump, buys a pullback that is bouncing, sizes flat from its own simulated
bankroll, and exits with a wide stop, a 2x full sell, a spike sell, a late-arming trail and a 24h time stop.

PAPER: every fill is simulated from a DexScreener price with slippage and fees.
LIVE (strat_b.mode, owner command only): entry logic is identical; new buys and their exits go through strat_b_live.BLive,
which uses Strategy A's signed GMGN client, wallet safety and caps. This module itself never signs or reads a key.
Paper and live rows share strat_b_orders (column mode) and are never mixed in P&L.
"""
from __future__ import annotations
import asyncio, collections, json, time
import fields, links
from ctx import iso, make_logger

LABEL = "STRATEGY B · PAPER"
LIVE_LABEL = "B LIVE"
OPEN, CLOSED = "open", "closed"
STOP, TP, SPIKE, TRAIL, TIME, PULLED = "stop", "take profit 2x", "spike", "trail", "time stop", "liquidity pulled"
TEMP_KEY = "strat_b_tonight"   # meta: settings changed for tonight only, reverted at the next night rollover (DECISION 63)
NO_PRICE_HOLD_S = 1800  # with no price, keep holding (the time stop still applies)
# DECISION 69: drop watches that stay under the $30K MC floor, or show $0 liquidity
UNDER_MC_DROP_EVALS = 10
UNDER_MC_DROP_S = 15 * 60


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def audit_top10(audit) -> float | None:
    for l in (audit or {}).get("layers") or []:
        if l.get("n") == 4:
            return num(str(l.get("value") or "").rstrip("%").strip())
    return None


# ---------- static filters (checked once, when the auditor passes a coin)
def static_check(s, audit) -> str | None:
    if audit.get("b_block"):
        return audit["b_block"]
    top10 = audit_top10(audit)
    if top10 is None:
        return "no top-10 data"
    if top10 > s["strat_b.max_top10_pct"]:
        return f"top 10 hold {top10:g}% > {s['strat_b.max_top10_pct']:g}%"
    holders = audit.get("holders")
    if holders is None:
        return "no holder count"
    if int(holders) < s["strat_b.min_holders"]:
        return f"{int(holders)} holders < {int(s['strat_b.min_holders'])}"
    bp = num(audit.get("bundle_pct"), 0.0) or 0.0
    if bp > s["strat_b.max_bundle_pct"]:
        return f"bundled wallets hold {bp:g}% > {s['strat_b.max_bundle_pct']:g}%"
    return None


# ---------- entry (checked every poll while watching)
def entry_check(s, w, pair, now) -> tuple[bool, str, dict]:
    """w: watch row with high/low (since the high). Returns (buy?, reason, metrics)."""
    g = lambda p: fields.get(pair, p)
    price, liq, mc = num(g("priceUsd")), num(g("liquidity.usd"), 0.0), num(g("marketCap")) or num(g("fdv"))
    m = {"price": price, "liq": liq, "mc": mc}
    if not price:
        return False, "no price", m
    age_min = (now - w["grad_at"]) / 60.0 if w.get("grad_at") else None
    m["ageMin"] = None if age_min is None else round(age_min)
    if age_min is None or age_min < s["strat_b.min_age_min"]:
        return False, f"too fresh ({0 if age_min is None else age_min:.0f}m < {s['strat_b.min_age_min']}m after graduation)", m
    if mc is None or mc < s["strat_b.min_mc_usd"] or mc > s["strat_b.max_mc_usd"]:
        return False, f"mc ${(mc or 0):,.0f} outside ${s['strat_b.min_mc_usd']:,.0f}-${s['strat_b.max_mc_usd']:,.0f}", m
    if liq < s["strat_b.min_liq_usd"]:
        return False, f"liquidity ${liq:,.0f} < ${s['strat_b.min_liq_usd']:,.0f}", m
    if s["strat_b.trade_usd"] > liq * s["strat_b.max_pool_pct"] / 100.0:
        return False, f"${s['strat_b.trade_usd']:g} is over {s['strat_b.max_pool_pct']:g}% of the pool", m
    vol = num(g("volume.h24"), 0.0)
    m["volMc"] = round(vol / mc, 2) if mc else None
    if vol < s["strat_b.min_vol_mc"] * mc:
        return False, f"volume {vol / mc:.2f}x mc < {s['strat_b.min_vol_mc']:g}x", m
    high, low = w.get("high") or price, w.get("low") or price
    dd = (1 - price / high) * 100 if high else 0.0
    m["pullbackPct"] = round(dd, 1)
    if dd < s["strat_b.pullback_min_pct"]:
        return False, f"no pullback yet ({dd:.0f}% off the high, need {s['strat_b.pullback_min_pct']:g}%)", m
    if dd > s["strat_b.pullback_max_pct"]:
        return False, f"fell too far ({dd:.0f}% off the high > {s['strat_b.pullback_max_pct']:g}%)", m
    bounce = (price / low - 1) * 100 if low else 0.0
    m["bouncePct"] = round(bounce, 1)
    if bounce < s["strat_b.bounce_pct"]:
        return False, f"not bouncing yet (+{bounce:.0f}% off the low, need +{s['strat_b.bounce_pct']:g}%)", m
    buys, sells = num(g("txns.m5.buys"), 0), num(g("txns.m5.sells"), 0)
    if buys < sells:
        return False, f"sellers in control (5m {buys:.0f} buys < {sells:.0f} sells)", m
    return True, f"pullback -{dd:.0f}% then +{bounce:.0f}% bounce, {age_min:.0f}m after graduation", m


# ---------- display: every B check with pass/fail and the actual value (live view; decisions stay in entry_check)
def _usd(v):
    if v is None:
        return "n/a"
    v = float(v)
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}K"


def checks(s, w, pair, now) -> list[dict]:
    g = lambda p: fields.get(pair, p) if pair else None
    out = []

    def add(k, label, ok, value, bar):
        out.append({"k": k, "label": label, "pass": ok, "value": value, "bar": bar})
    t10, hol, bp = w.get("top10"), w.get("holders"), w.get("bundle_pct")
    add("top10", "Top 10", None if t10 is None else t10 <= s["strat_b.max_top10_pct"],
        "n/a" if t10 is None else f"{t10:g}%", f"≤ {s['strat_b.max_top10_pct']:g}%")
    add("holders", "Holders", None if hol is None else int(hol) >= s["strat_b.min_holders"],
        "n/a" if hol is None else f"{int(hol):,}", f"≥ {s['strat_b.min_holders']}")
    bpv = num(bp, 0.0) or 0.0
    add("bundle", "Bundled", bpv <= s["strat_b.max_bundle_pct"], f"{bpv:.1f}%", f"≤ {s['strat_b.max_bundle_pct']:g}%")
    age = (now - w["grad_at"]) / 60.0 if w.get("grad_at") else None
    add("wait", "Since grad", None if age is None else age >= s["strat_b.min_age_min"],
        "n/a" if age is None else f"{age:.0f}m", f"≥ {s['strat_b.min_age_min']}m")
    price = num(g("priceUsd"))
    if not price:
        for k, label, bar in (("mc", "Market cap", f"{_usd(s['strat_b.min_mc_usd'])}–{_usd(s['strat_b.max_mc_usd'])}"),
                              ("volmc", "Vol/MC", f"≥ {s['strat_b.min_vol_mc']:g}x"), ("liq", "Liquidity", f"≥ {_usd(s['strat_b.min_liq_usd'])}"),
                              ("dip", "Dip from high", f"{s['strat_b.pullback_min_pct']:g}–{s['strat_b.pullback_max_pct']:g}%"),
                              ("bounce", "Bounce", f"≥ +{s['strat_b.bounce_pct']:g}%"), ("buys", "5m buys/sells", "buys ≥ sells")):
            add(k, label, None, "no price yet", bar)
        return out
    liq, mc = num(g("liquidity.usd"), 0.0), num(g("marketCap")) or num(g("fdv"))
    add("mc", "Market cap", None if mc is None else s["strat_b.min_mc_usd"] <= mc <= s["strat_b.max_mc_usd"], _usd(mc),
        f"{_usd(s['strat_b.min_mc_usd'])}–{_usd(s['strat_b.max_mc_usd'])}")
    vol = num(g("volume.h24"), 0.0)
    r = vol / mc if mc else None
    add("volmc", "Vol/MC", None if r is None else r >= s["strat_b.min_vol_mc"], "n/a" if r is None else f"{r:.2f}x", f"≥ {s['strat_b.min_vol_mc']:g}x")
    pool_ok = s["strat_b.trade_usd"] <= liq * s["strat_b.max_pool_pct"] / 100.0
    add("liq", "Liquidity", liq >= s["strat_b.min_liq_usd"] and pool_ok, _usd(liq), f"≥ {_usd(s['strat_b.min_liq_usd'])}")
    high, low = w.get("high") or price, w.get("low") or price
    dd = (1 - price / high) * 100 if high else 0.0
    add("dip", "Dip from high", s["strat_b.pullback_min_pct"] <= dd <= s["strat_b.pullback_max_pct"], f"-{dd:.1f}%",
        f"{s['strat_b.pullback_min_pct']:g}–{s['strat_b.pullback_max_pct']:g}%")
    b = (price / low - 1) * 100 if low else 0.0
    add("bounce", "Bounce", b >= s["strat_b.bounce_pct"], f"+{b:.1f}%", f"≥ +{s['strat_b.bounce_pct']:g}%")
    buys, sells = num(g("txns.m5.buys"), 0), num(g("txns.m5.sells"), 0)
    add("buys", "5m buys/sells", buys >= sells, f"{buys:.0f}/{sells:.0f}", "buys ≥ sells")
    return out


def away(s, w, ck, now) -> list[str]:
    """How far a watched coin is from the buy zone, in words (failing or unknown checks only)."""
    c = {x["k"]: x for x in ck}
    out = []
    if c["wait"]["pass"] is False and w.get("grad_at"):
        out.append(f"{max(0, s['strat_b.min_age_min'] - (now - w['grad_at']) / 60):.0f}m left of the wait")
    for k in ("mc", "volmc", "liq", "buys"):
        if c[k]["pass"] is False:
            out.append(f"{c[k]['label']} {c[k]['value']} (needs {c[k]['bar']})")
    if c["dip"]["pass"] is False:
        dd = float(c["dip"]["value"].lstrip("-").rstrip("%"))
        out.append(f"needs {s['strat_b.pullback_min_pct'] - dd:.1f}% more dip" if dd < s["strat_b.pullback_min_pct"]
                   else f"{dd - s['strat_b.pullback_max_pct']:.1f}% too deep")
    if c["bounce"]["pass"] is False and c["dip"]["pass"]:
        b = float(c["bounce"]["value"].lstrip("+").rstrip("%"))
        out.append(f"needs +{s['strat_b.bounce_pct'] - b:.1f}% more bounce")
    if any(x["pass"] is None for x in ck[4:]):
        out.append("waiting for a price")
    return out


def compact(ck) -> str:
    return " ".join(f"{x['k']}={x['value']}{'✓' if x['pass'] else '✗' if x['pass'] is False else '?'}" for x in ck)


# ---------- simulated fills
def slip_pct(s, usd, liq) -> float:
    impact = (2.0 * usd / liq * 100.0) if liq else 5.0        # constant-product impact ~ 2 x size / liquidity
    return s["strat_b.slippage_pct"] + impact


def fill_buy(s, price, size, liq) -> dict:
    fill = price * (1 + slip_pct(s, size, liq) / 100.0)
    fees = size * s["strat_b.fee_pct"] / 100.0 + s["strat_b.net_fee_usd"]
    return {"price": fill, "tokens": max(0.0, size - fees) / fill, "fees": fees}


def fill_sell(s, price, tokens, liq) -> dict:
    fill = price * (1 - min(99.0, slip_pct(s, tokens * price, liq)) / 100.0)
    gross = tokens * fill
    fees = gross * s["strat_b.fee_pct"] / 100.0 + s["strat_b.net_fee_usd"]
    return {"price": fill, "proceeds": max(0.0, gross - fees), "fees": fees}


# ---------- exits
def exit_check(s, o, price, liq, hist, now) -> tuple[str | None, dict]:
    """o: open order row. hist: [(ts, price)] recent samples (incl. this one). Returns (reason|None, updates)."""
    e = o["entry_price"]
    peak = max(o["peak"] or e, price)
    armed = bool(o["trail_armed"]) or peak >= e * (1 + s["strat_b.trail_start_pct"] / 100.0)
    upd = {"peak": peak, "trail_armed": 1 if armed else 0}
    if o.get("entry_liq") and liq is not None and liq < o["entry_liq"] * 0.2:
        return PULLED, upd
    if price <= e * (1 - s["strat_b.stop_pct"] / 100.0):
        return STOP, upd
    if price >= e * (1 + s["strat_b.take_profit_pct"] / 100.0):
        return TP, upd
    win = [p for t, p in hist if now - t <= s["strat_b.spike_window_s"]]
    if win and price > e and price >= min(win) * (1 + s["strat_b.spike_pct"] / 100.0):
        return SPIKE, upd
    if armed and price <= peak * (1 - s["strat_b.trail_pct"] / 100.0):
        return TRAIL, upd
    if now - o["opened_at"] >= s["strat_b.time_stop_h"] * 3600:
        return TIME, upd
    return None, upd


def rules(s) -> list[dict]:
    """Plain-words rules with the live value and the reason (shown in the app and the scorecard)."""
    v = lambda k: s[k]
    return [
        {"k": "Entry · holders", "v": f"top 10 ≤ {v('strat_b.max_top10_pct'):g}%, ≥ {v('strat_b.min_holders')} holders, bundled ≤ {v('strat_b.max_bundle_pct'):g}%",
         "why": "Fried Dev / Parasol holder checks; bundles graduate then fall (pumpdata.fun)"},
        {"k": "Entry · wait", "v": f"{v('strat_b.min_age_min')}m after graduation, coins up to {v('strat_b.max_age_min') / 1440:g}d old, watch up to {v('strat_b.watch_max_min') / 60:g}h",
         "why": "87% of graduates are below half their graduation price an hour later (Hidden Hand); B's own GeckoTerminal feed adds coins 1h–7d old"},
        {"k": "Entry · zone", "v": f"mc ${v('strat_b.min_mc_usd'):,.0f}–${v('strat_b.max_mc_usd'):,.0f}, 24h volume ≥ {v('strat_b.min_vol_mc'):g}x mc",
         "why": "Swing holder buys $30K–$1M; volume under half of mc is a walk-away (Parasol)"},
        {"k": "Entry · trigger", "v": f"pullback {v('strat_b.pullback_min_pct'):g}–{v('strat_b.pullback_max_pct'):g}% off the high, +{v('strat_b.bounce_pct'):g}% bounce, 5m buys ≥ sells",
         "why": "Buy pullbacks, not a vertical candle (Parasol)"},
        {"k": "Size", "v": (f"LIVE: ${v('strat_b.trade_usd'):,.0f} when free SOL covers it, else Strategy A's rule (${v('gmgn.fallback_trade_usd'):,.0f}, then whatever free SOL covers down to ${v('gmgn.min_trade_usd'):,.0f}) after a {v('gmgn.sol_reserve'):g} SOL reserve"
                            if v("strat_b.mode") == "LIVE" else f"${v('strat_b.trade_usd'):,.0f} flat of a ${v('strat_b.bankroll_usd'):,.0f} paper bankroll") + f", max {v('strat_b.max_open')} open, one buy per coin",
         "why": "1–5% per trade (Trojan); never average down (Memecoin Guru)"},
        {"k": "Daily loss stop",
         "v": (f"no new LIVE buys after -${v('strat_b.live_daily_loss_usd'):,.0f} real P&L in a night, capped at the wallet"
               if v("strat_b.mode") == "LIVE"
               else f"no new buys after -{v('strat_b.daily_loss_stop_pct'):g}% of bankroll in a night"),
         "why": "LIVE uses real money, not the paper bankroll (DECISION 69); PAPER stays a -10% daily stop (MoonHydra)"},
        {"k": "Stop", "v": f"-{v('strat_b.stop_pct'):g}% from entry, from the first second" + (" (server-side on GMGN in LIVE)" if v("strat_b.mode") == "LIVE" else ""),
         "why": "Guides cluster at -30% to -40% (three.ws uses -35%)"},
        {"k": "Take profit", "v": f"sell everything at +{v('strat_b.take_profit_pct'):g}%",
         "why": "Guides take money at 2x; pros sell 90%+ in one go (KOL Brain)"},
        {"k": "Spike", "v": f"+{v('strat_b.spike_pct'):g}% within {v('strat_b.spike_window_s')}s (above entry) → sell everything now",
         "why": "Pros sell into strength in one sell on 71–87% of positions (KOL Brain)"},
        {"k": "Trail", "v": f"arms at +{v('strat_b.trail_start_pct'):g}%, sells on a {v('strat_b.trail_pct'):g}% drop from the high",
         "why": "clusterz: trail from +50% on a 20% drop locks in about +20%"},
        {"k": "Time stop", "v": f"sell after {v('strat_b.time_stop_h'):g}h", "why": "24h time stop (three.ws)"},
        {"k": "Fills", "v": f"DexScreener price ± {v('strat_b.slippage_pct'):g}% + 2×size/liquidity impact, {v('strat_b.fee_pct'):g}% fee + ${v('strat_b.net_fee_usd'):.2f} per side",
         "why": "Close to GMGN's 1% fee and priority/tip fees"},
    ]


class StratB:
    name = "strat_b"

    def __init__(self, ctx, log_files=True):
        self.c = ctx
        self.log = make_logger("strat_b", log_files)
        self.hist: dict[str, collections.deque] = {}
        self.backoff_until = 0.0
        self._last_reason: dict[str, str] = {}
        self._loss_logged = None
        from strat_b_live import BLive                 # the only path to real orders; acts only when strat_b.mode == LIVE
        self.live = BLive(self)

    # ---- mode
    def mode(self) -> str:
        return "LIVE" if self.c.s["strat_b.mode"] == "LIVE" else "PAPER"

    def paper_locked(self) -> bool:
        return self.mode() == "PAPER"

    def enabled(self) -> bool:
        return bool(self.c.s["strat_b.enabled"])

    # ---- feed (called by the auditor for every coin it passes; must never raise or block)
    def offer(self, scan, audit):
        try:
            self._offer(scan, audit)
        except Exception as e:
            self.c.error("strat_b", "offer", f"{(scan or {}).get('mint')} {str(e)[:200]}")

    def _offer(self, scan, audit):
        c, s = self.c, self.c.s
        if not self.enabled():
            return
        mint = scan["mint"]
        if c.db.one("SELECT mint FROM strat_b_watch WHERE mint=?", (mint,)):
            return
        now = c.clock()
        coin = c.coin(mint) or {}
        grad = coin.get("migrated_at") or (now - float(scan.get("ageMin") or 0) * 60)
        name = scan.get("name") or coin.get("name") or c.label(mint)
        src = scan.get("b_source") or ("desk scanner" + (f" · {scan['source']}" if scan.get("source") else ""))
        bad = static_check(s, audit)
        status = "skipped" if bad else "watching"
        c.db.x("INSERT OR IGNORE INTO strat_b_watch(mint,name,symbol,added_at,grad_at,top10,holders,bundle_pct,high,low,last_price,status,reason,updated,night,source)"
               " VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?)",
               (mint, name, scan.get("symbol"), now, grad, audit_top10(audit), audit.get("holders"), audit.get("bundle_pct"),
                status, bad, now, c.night, src))
        w = {"mint": mint, "name": name, "symbol": scan.get("symbol"), "grad_at": grad, "top10": audit_top10(audit),
             "holders": audit.get("holders"), "bundle_pct": audit.get("bundle_pct"), "source": src}
        ck = checks(s, w, None, now)
        c.db.x("UPDATE strat_b_watch SET checks=?, eval_at=?, evals=1 WHERE mint=?", (json.dumps(ck), now, mint))
        self._eval(w, "offer", status, bad or "filters ok, waiting for a pullback", ck, None, now)
        self.log.info(f"{iso(now)}\t{'skip' if bad else 'watch'}\t{mint}\t{name}\t{bad or 'filters ok, waiting for a pullback'}"
                      f"\ttop10={audit_top10(audit)}\tholders={audit.get('holders')}\tbundle={audit.get('bundle_pct')}"
                      f"\tage={round((now - grad) / 60)}m\tsrc={src}")

    # ---- evaluation history (live view): one row per change of verdict, plus offer/buy/expire
    def _eval(self, w, kind, status, reason, ck, price, now):
        src = w.get("source") or "desk scanner"
        self.c.db.x("INSERT INTO strat_b_evals(ts,night,mint,name,symbol,kind,status,reason,checks,price,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (now, self.c.night, w["mint"], w.get("name"), w.get("symbol"), kind, status, reason, json.dumps(ck), price, src))

    # ---- tonight-only settings (DECISION 63): reverted at the next night rollover, or at the stored deadline
    def set_tonight(self, key, value, source, until):
        s = self.c.s
        old = s[key]
        temps = [t for t in (self.c.db.meta(TEMP_KEY, []) or []) if t["key"] != key]
        prior = next((t["revert_to"] for t in (self.c.db.meta(TEMP_KEY, []) or []) if t["key"] == key), old)
        msg = s.set(key, value, source=source)
        temps.append({"key": key, "revert_to": prior, "night": self.c.night, "until": until, "source": source})
        self.c.db.set_meta(TEMP_KEY, temps)
        self.log.info(f"{iso(self.c.clock())}\ttonight\t{key}\t{msg}\trevert to {prior} at the next rollover (by {iso(until)})")
        return msg

    def apply_reverts(self, now=None):
        now = self.c.clock() if now is None else now
        temps = self.c.db.meta(TEMP_KEY, []) or []
        keep, done = [], []
        for t in temps:
            if self.c.night != t["night"] or now >= float(t["until"]):
                try:
                    msg = self.c.s.set(t["key"], t["revert_to"], source=f"auto revert at night rollover ({t['source']}: tonight only)")
                except Exception as e:
                    self.c.error("strat_b", "revert", f"{t['key']} {str(e)[:150]}")
                    keep.append(t)
                    continue
                done.append(t["key"])
                self.log.info(f"{iso(now)}\treverted\t{t['key']}\t{msg}\tnight {t['night']} -> {self.c.night}")
            else:
                keep.append(t)
        if done:
            self.c.db.set_meta(TEMP_KEY, keep)
        return done

    # ---- money
    def bankroll(self) -> float:
        """PAPER bankroll only: live P&L is real money and never mixed in."""
        r = self.c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM strat_b_orders WHERE status='closed' AND mode='PAPER'")
        return round(float(self.c.s["strat_b.bankroll_usd"]) + (r["p"] or 0), 6)

    def in_positions(self) -> float:
        r = self.c.db.one("SELECT COALESCE(SUM(size_usd),0) s FROM strat_b_orders WHERE status='open' AND mode='PAPER'")
        return r["s"] or 0.0

    def night_pnl(self, mode="PAPER") -> float:
        r = self.c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM strat_b_orders WHERE status='closed' AND night=? AND mode=?", (self.c.night, mode))
        return r["p"] or 0.0

    def daily_loss_limit(self) -> float:
        """PAPER: daily_loss_stop_pct of the paper bankroll.
        LIVE: strat_b.live_daily_loss_usd, capped at the cached wallet USD when we have one (DECISION 69)."""
        s = self.c.s
        if self.mode() != "LIVE":
            return float(s["strat_b.bankroll_usd"]) * float(s["strat_b.daily_loss_stop_pct"]) / 100.0
        lim = float(s["strat_b.live_daily_loss_usd"])
        import strat_b_live
        snap, _ = strat_b_live.wallet_view(self.c)
        usd = (snap or {}).get("usd") if snap else None
        try:
            if usd is not None and float(usd) > 0:
                lim = min(lim, float(usd))
        except (TypeError, ValueError):
            pass
        return lim

    def can_buy(self) -> str | None:
        s, mode = self.c.s, self.mode()
        if mode == "LIVE":                               # held or being bought/sold for real
            n = self.c.db.one("SELECT COUNT(*) n FROM strat_b_orders WHERE mode='LIVE' AND status IN ('submitting','pending','open','exiting')")["n"]
        else:
            n = self.c.db.one("SELECT COUNT(*) n FROM strat_b_orders WHERE status='open' AND mode='PAPER'")["n"]
        if n >= s["strat_b.max_open"]:
            return f"{n} open (max {s['strat_b.max_open']})"
        lim = self.daily_loss_limit()
        pnl = self.night_pnl(mode)                       # LIVE: real P&L only
        if pnl <= -lim:
            return f"daily loss stop: ${pnl:,.2f} {'real ' if mode == 'LIVE' else ''}tonight"
        if mode == "PAPER" and self.bankroll() - self.in_positions() < s["strat_b.trade_usd"]:
            return "paper bankroll too low"
        return None

    # ---- loop
    async def loop(self):
        self.log.info(f"{iso()}\tstart\tmode={self.c.s['strat_b.mode']}\tenabled={self.c.s['strat_b.enabled']}"
                      f"\tA gmgn.mode={self.c.s['gmgn.mode']}\tgmgn client={'yes' if getattr(self.c, 'gmgn', None) is not None else 'no'}"
                      f"\tlive open={self.c.db.one('SELECT COUNT(*) n FROM strat_b_orders WHERE mode=? AND status=?', ('LIVE', OPEN))['n']}"
                      f"\tbankroll=${self.bankroll():,.2f}\topen={self.c.db.one('SELECT COUNT(*) n FROM strat_b_orders WHERE status=?', (OPEN,))['n']}"
                      f"\twatching={self.c.db.one('SELECT COUNT(*) n FROM strat_b_watch WHERE status=?', ('watching',))['n']}")
        while True:
            try:
                note = await self.tick()
            except Exception as e:
                note = "error"
                self.c.error("strat_b", "tick", str(e)[:300])
            self.c.hb("strat_b", note or "")
            await asyncio.sleep(float(self.c.s["strat_b.poll_s"]))

    async def tick(self, pairs=None):
        c, s = self.c, self.c.s
        now = c.clock()
        self.apply_reverts(now)
        opens = c.db.rows("SELECT * FROM strat_b_orders WHERE status='open'")
        watch = []
        if self.enabled():
            exp = now - s["strat_b.watch_max_min"] * 60
            old = now - s["strat_b.max_age_min"] * 60
            for w in c.db.rows("SELECT * FROM strat_b_watch WHERE status='watching' AND (added_at<? OR grad_at<?)", (exp, old)):
                why = (f"older than {s['strat_b.max_age_min'] / 1440:g}d" if w["grad_at"] and w["grad_at"] < old
                       else f"no entry in {s['strat_b.watch_max_min']}m")
                self._expire_watch(w, why, now, row_reason=self._last_reason.get(w["mint"]) or "no entry")
            watch = c.db.rows("SELECT * FROM strat_b_watch WHERE status='watching' ORDER BY added_at DESC LIMIT ?", (int(s["strat_b.max_watch"]),))
            if pairs is None:                            # live: watched coins every watch_poll_s, open positions every tick
                if now - getattr(self, "_watch_polled", 0.0) < s["strat_b.watch_poll_s"]:
                    watch = []
                else:
                    self._watch_polled = now
        mints = list(dict.fromkeys([o["mint"] for o in opens] + [w["mint"] for w in watch]))
        if not mints:
            return f"open=0 watching=0 bankroll=${self.bankroll():,.2f}"
        if pairs is None:
            if now < self.backoff_until:
                return "429 backoff"
            try:
                pairs = await c.net.dex_tokens(mints)
            except Exception as e:
                if type(e).__name__ == "RateLimited":
                    self.backoff_until = now + float(s["risk.backoff_429_s"])
                return f"dex error {type(e).__name__}"
        from agents.scanner import pick_pair
        best = {m: pick_pair(pairs, m) for m in mints}
        for o in opens:
            await self._manage(o, best.get(o["mint"]), now)
        for w in watch:
            if w["mint"] in {o["mint"] for o in opens}:
                continue
            await self._consider(w, best.get(w["mint"]), now)
        n_open = c.db.one("SELECT COUNT(*) n FROM strat_b_orders WHERE status='open'")["n"]
        return f"open={n_open} watching={len(watch)} bankroll=${self.bankroll():,.2f}"

    def _expire_watch(self, w, why, now, row_reason=None):
        last = self._last_reason.pop(w["mint"], None) or "no entry"
        self.c.db.x("UPDATE strat_b_watch SET status='expired', reason=?, updated=? WHERE mint=?",
                    (row_reason if row_reason is not None else why, now, w["mint"]))
        ev = why if " · last:" in why else f"{why} · last: {last}"
        self._eval(w, "expired", "expired", ev, json.loads(w.get("checks") or "[]"), w.get("last_price"), now)
        self.log.info(f"{iso(now)}\texpired\t{w['mint']}\t{w['name']}\t{why}\tlast: {last}")

    def _stale_watch(self, w, pair, m, now) -> str | None:
        """Drop a watch whose liquidity is $0, or whose market cap stays under the floor (DECISION 69)."""
        price = m.get("price")
        if not price:
            return None
        liq_raw = fields.get(pair, "liquidity.usd") if pair else None
        liq = num(liq_raw)
        if liq is not None and liq <= 0:
            return "liquidity $0"
        mc = m.get("mc")
        floor = float(self.c.s["strat_b.min_mc_usd"])
        under = mc is not None and mc < floor
        if under:
            n = int(w.get("under_floor_evals") or 0) + 1
            since = w.get("under_floor_since") or now
            self.c.db.x("UPDATE strat_b_watch SET under_floor_evals=?, under_floor_since=? WHERE mint=?",
                        (n, since, w["mint"]))
            w["under_floor_evals"], w["under_floor_since"] = n, since
            held = now - since
            if n >= UNDER_MC_DROP_EVALS or held >= UNDER_MC_DROP_S:
                if n >= UNDER_MC_DROP_EVALS:
                    return f"mc ${mc:,.0f} under ${floor:,.0f} for {n} evals"
                return f"mc ${mc:,.0f} under ${floor:,.0f} for {held / 60:.0f}m"
        elif w.get("under_floor_evals") or w.get("under_floor_since"):
            self.c.db.x("UPDATE strat_b_watch SET under_floor_evals=0, under_floor_since=NULL WHERE mint=?", (w["mint"],))
            w["under_floor_evals"], w["under_floor_since"] = 0, None
        return None

    def _sample(self, mint, now, price):
        h = self.hist.setdefault(mint, collections.deque())
        h.append((now, price))
        keep = max(float(self.c.s["strat_b.spike_window_s"]), 60.0) + 30
        while h and now - h[0][0] > keep:
            h.popleft()
        return list(h)

    async def _consider(self, w, pair, now):
        c, s = self.c, self.c.s
        price = num(fields.get(pair, "priceUsd")) if pair else None
        if price:
            if w["high"] is None or price >= w["high"]:
                high, low = price, price                     # new high: the pullback (and its low) restarts
            else:
                high, low = w["high"], min(w["low"] or price, price)
            c.db.x("UPDATE strat_b_watch SET high=?, low=?, last_price=?, updated=? WHERE mint=?", (high, low, price, now, w["mint"]))
            w = dict(w, high=high, low=low, last_price=price)
        ok, why, m = entry_check(s, w, pair or {}, now)
        ck = checks(s, w, pair, now)
        c.db.x("UPDATE strat_b_watch SET checks=?, eval_at=?, evals=COALESCE(evals,0)+1 WHERE mint=?", (json.dumps(ck), now, w["mint"]))
        w = dict(w, checks=json.dumps(ck), eval_at=now)
        drop = self._stale_watch(w, pair, m, now)
        if drop:
            self._expire_watch(w, drop, now)
            return
        if ok:
            block = self.can_buy() or (self.live.blocked_retry(w["mint"], now) if self.mode() == "LIVE" else None)
            if block:
                ok, why = False, block
        if not ok:
            if self._last_reason.get(w["mint"]) != why.split(" (")[0]:
                self._last_reason[w["mint"]] = why.split(" (")[0]
                self._eval(w, "wait", "watching", why, ck, price, now)
                self.log.info(f"{iso(now)}\twait\t{w['mint']}\t{w['name']}\t{why}\tprice={price}\tmc={m.get('mc')}\t{compact(ck)}")
            return
        if self.mode() == "LIVE":
            res = await self.live.buy(w, m, why, now)
            if res.get("status") == "refused":
                self._last_reason[w["mint"]] = f"B LIVE refused: {res['reason']}"
                self._eval(w, "buy", "refused", f"B LIVE refused: {res['reason']}", ck, price, now)
            else:
                self._eval(w, "buy", "bought", f"B LIVE · {why}", ck, price, now)
                self._last_reason.pop(w["mint"], None)
            return
        self._eval(w, "buy", "bought", why, ck, price, now)
        await self._buy(w, pair, m, why, now)

    async def _buy(self, w, pair, m, why, now):
        """PAPER fill (simulated)."""
        c, s = self.c, self.c.s
        size = float(s["strat_b.trade_usd"])
        f = fill_buy(s, m["price"], size, m["liq"])
        cur = c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,symbol,status,size_usd,signal_price,entry_price,tokens,buy_fees_usd,peak,"
                     "last_price,last_liq,entry_liq,mc_entry,trail_armed,opened_at,why,last_seen,mode) VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,0,?,?,?,'PAPER')",
                     (now, c.night, w["mint"], w["name"], w["symbol"], size, m["price"], f["price"], f["tokens"], f["fees"], f["price"],
                      m["price"], m["liq"], m["liq"], m["mc"], now, json.dumps(dict(m, reason=why)), now))
        c.db.x("UPDATE strat_b_watch SET status='bought', reason=?, updated=? WHERE mint=?", (why, now, w["mint"]))
        self._last_reason.pop(w["mint"], None)
        self.hist[w["mint"]] = collections.deque([(now, m["price"])])
        self.log.info(f"{iso(now)}\tBUY\t{w['mint']}\t{w['name']}\t#{cur.lastrowid}\t${size:.2f}\tsignal={m['price']:.6g}\tfill={f['price']:.6g}"
                      f"\tfees=${f['fees']:.2f}\tmc={m['mc']}\tliq={m['liq']}\t{why}")
        text = (f"{LABEL} · Bought {w['name']} · ${size:,.2f} @ ${f['price']:.6g}\n"
                f"mc ${m['mc']:,.0f} · {why}\n"
                f"stop -{s['strat_b.stop_pct']:g}% · sell all at +{s['strat_b.take_profit_pct']:g}% or on a spike · trail from +{s['strat_b.trail_start_pct']:g}%")
        await self._push(text, w["mint"])
        c.event("fill", w["mint"])

    async def _manage(self, o, pair, now):
        c, s = self.c, self.c.s
        price = num(fields.get(pair, "priceUsd")) if pair else None
        liq = num(fields.get(pair, "liquidity.usd")) if pair else None
        live = (o.get("mode") or "PAPER") == "LIVE"
        if not price:
            if now - o["opened_at"] >= s["strat_b.time_stop_h"] * 3600 and o["last_price"]:
                if live:
                    await self.live.sell(o, TIME + " (no price)", o["last_price"], now)
                else:
                    await self._sell(o, o["last_price"], o["last_liq"], TIME + " (no price)", now)
            return
        if live:                                         # real position: B's exits with real sells (DECISION 66)
            await self.live.manage(o, price, liq, now)
            return
        hist = self._sample(o["mint"], now, price)
        reason, upd = exit_check(s, o, price, liq, hist, now)
        c.db.x("UPDATE strat_b_orders SET peak=?, trail_armed=?, last_price=?, last_liq=?, last_seen=? WHERE id=?",
               (upd["peak"], upd["trail_armed"], price, liq, now, o["id"]))
        if upd["trail_armed"] and not o["trail_armed"]:
            self.log.info(f"{iso(now)}\ttrail armed\t{o['mint']}\t{o['name']}\t#{o['id']}\tpeak={upd['peak']:.6g}")
        if reason:
            await self._sell(dict(o, **upd), price, liq, reason, now)

    async def _sell(self, o, price, liq, reason, now):
        c, s = self.c, self.c.s
        f = fill_sell(s, price, o["tokens"], liq or o["last_liq"] or o["entry_liq"])
        pnl = f["proceeds"] - o["size_usd"]
        pct = pnl / o["size_usd"] * 100 if o["size_usd"] else 0.0
        cur = c.db.x("UPDATE strat_b_orders SET status='closed', closed_at=?, exit_reason=?, exit_signal_price=?, exit_price=?, sell_fees_usd=?,"
                     " pnl_usd=?, pct=? WHERE id=? AND status='open'",
                     (now, reason, price, f["price"], f["fees"], round(pnl, 4), round(pct, 2), o["id"]))
        if not cur.rowcount:
            return                                          # already closed: never a second sell
        self.hist.pop(o["mint"], None)
        held = now - o["opened_at"]
        held_txt = f"{held / 3600:.1f}h" if held >= 5400 else f"{held / 60:.0f}m"
        self.log.info(f"{iso(now)}\tSELL\t{o['mint']}\t{o['name']}\t#{o['id']}\t{reason}\tsignal={price:.6g}\tfill={f['price']:.6g}"
                      f"\tpnl=${pnl:+.2f}\t{pct:+.1f}%\theld={held_txt}\tbankroll=${self.bankroll():,.2f}")
        sign = "+" if pnl >= 0 else "-"
        text = (f"{LABEL} · Sold {o['name']} ({reason}) · {sign}${abs(pnl):,.2f} ({pct:+.1f}%) · held {held_txt}\n"
                f"B paper bankroll ${self.bankroll():,.2f}")
        await self._push(text, o["mint"])
        c.event("exit", o["mint"])

    async def _push(self, text, mint):
        try:
            await self.c.notifier.push(text, [[{"text": "Open on FOMO", "url": links.coin_url(mint, "solana")}]], kind="strat_b", mint=mint)
        except Exception as e:
            self.c.error("strat_b", "push", str(e)[:200])

    # ---- scorecard (publisher strat_b.json + strat_b_report.py)
    def scorecard(self, limit=40) -> dict:
        return scorecard(self.c, limit)


def _order_view(s, o):
    last = o["last_price"] or o["signal_price"]
    e = o["entry_price"]
    mode = o.get("mode") or "PAPER"
    v = {"id": o["id"], "mint": o["mint"], "name": o["name"], "symbol": o["symbol"], "mode": mode, "status": o["status"],
         "label": LIVE_LABEL if mode == "LIVE" else "B PAPER",
         "sizeUsd": round(o["size_usd"], 2), "entryPrice": e, "signalPrice": o["signal_price"], "lastPrice": last,
         "peakPrice": o["peak"], "openedAt": iso(o["opened_at"]), "mcEntry": o["mc_entry"],
         "why": (json.loads(o["why"]).get("reason") if o["why"] else None), "link": links.coin_url(o["mint"], "solana")}
    if o["status"] == OPEN:
        if mode == "LIVE":                               # estimate from the real entry (fees + rent included)
            pnl = (last / e - 1) * o["size_usd"] if (e and last) else 0.0
        else:
            est = fill_sell(s, last, o["tokens"], o["last_liq"] or o["entry_liq"])
            pnl = est["proceeds"] - o["size_usd"]
        v.update(pnlUsd=round(pnl, 2), pct=round(pnl / o["size_usd"] * 100, 2), trailArmed=bool(o["trail_armed"]),
                 stopLine=e * (1 - s["strat_b.stop_pct"] / 100.0), tpLine=e * (1 + s["strat_b.take_profit_pct"] / 100.0),
                 trailLine=(o["peak"] * (1 - s["strat_b.trail_pct"] / 100.0)) if o["trail_armed"] else None,
                 timeStopAt=iso(o["opened_at"] + s["strat_b.time_stop_h"] * 3600))
    else:
        v.update(pnlUsd=round(o["pnl_usd"] or 0, 2), pct=o["pct"], exitReason=o["exit_reason"], exitPrice=o["exit_price"],
                 closedAt=iso(o["closed_at"]) if o["closed_at"] else None)
    if mode == "LIVE":
        v.update(sol=round(o["lamports"] / 1e9, 6) if o.get("lamports") else None,           # no order ids / tx in the app
                 note=(o.get("note") or "")[:160] or None, stopOnGmgn=bool(o.get("strategy_order_id")))
        if o["status"] not in (OPEN, CLOSED):            # submitting / pending / exiting / failed / unknown
            v.update(pnlUsd=round(o["pnl_usd"] or 0, 2), pct=o["pct"], exitReason=o.get("exit_reason") or o["status"])
    return v


def live_card(c, limit=40) -> dict:
    """Real-money Strategy B results (DECISION 66): only mode='LIVE' rows, P&L from the chain. Never the paper bankroll."""
    s, db = c.s, c.db
    opens = db.rows("SELECT * FROM strat_b_orders WHERE mode='LIVE' AND status IN ('submitting','pending','open','exiting') ORDER BY opened_at DESC")
    closed = db.rows("SELECT * FROM strat_b_orders WHERE mode='LIVE' AND status='closed' ORDER BY closed_at DESC")
    failed = db.rows("SELECT * FROM strat_b_orders WHERE mode='LIVE' AND status IN ('failed','unknown') ORDER BY ts DESC LIMIT 10")
    refused = db.rows("SELECT * FROM strat_b_orders WHERE mode='LIVE' AND status='refused' AND night=? ORDER BY ts DESC", (c.night,))
    wins = [o for o in closed if (o["pnl_usd"] or 0) > 0]
    losses = [o for o in closed if (o["pnl_usd"] or 0) <= 0]
    gw, gl = sum(o["pnl_usd"] for o in wins), -sum(o["pnl_usd"] or 0 for o in losses)
    tonight = [o for o in closed if o["night"] == c.night]
    import strat_b_live
    wallet, free_sol = strat_b_live.wallet_view(c)
    return {"label": LIVE_LABEL, "mode": "LIVE", "active": s["strat_b.mode"] == "LIVE",
            "pnlAllUsd": round(sum(o["pnl_usd"] or 0 for o in closed), 2), "pnlTonightUsd": round(sum(o["pnl_usd"] or 0 for o in tonight), 2),
            "closedTonight": len(tonight), "closedAll": len(closed), "wins": len(wins), "losses": len(losses),
            "winRatePct": round(100 * len(wins) / len(closed), 1) if closed else None,
            "profitFactor": round(gw / gl, 2) if gl > 0 else None,
            "unrealizedUsd": round(sum(_order_view(s, o)["pnlUsd"] or 0 for o in opens), 2),
            "inPositions": round(sum(o["size_usd"] or 0 for o in opens), 2),
            "wallet": wallet, "freeSol": free_sol, "reserveSol": s["gmgn.sol_reserve"],
            "refusedTonight": len(refused), "lastRefusal": ({"t": iso(refused[0]["ts"]), "name": refused[0]["name"], "reason": refused[0]["note"]} if refused else None),
            "open": [_order_view(s, o) for o in opens], "closed": [_order_view(s, o) for o in closed[:limit]],
            "failed": [_order_view(s, o) for o in failed]}


def scorecard(c, limit=40) -> dict:
    """PAPER fields at the top level (unchanged shape, paper rows only) + 'live' (real money, B LIVE) section."""
    s, db = c.s, c.db
    opens = db.rows("SELECT * FROM strat_b_orders WHERE status='open' AND mode='PAPER' ORDER BY opened_at DESC")
    closed = db.rows("SELECT * FROM strat_b_orders WHERE status='closed' AND mode='PAPER' ORDER BY closed_at DESC")
    pnl_all = sum(o["pnl_usd"] or 0 for o in closed)
    wins = [o for o in closed if (o["pnl_usd"] or 0) > 0]
    losses = [o for o in closed if (o["pnl_usd"] or 0) <= 0]
    gw, gl = sum(o["pnl_usd"] for o in wins), -sum(o["pnl_usd"] or 0 for o in losses)
    tonight = [o for o in closed if o["night"] == c.night]
    start = float(s["strat_b.bankroll_usd"])
    in_pos = sum(o["size_usd"] for o in opens)
    unreal = sum(_order_view(s, o)["pnlUsd"] for o in opens)
    w = {r["status"]: r["n"] for r in db.rows("SELECT status, COUNT(*) n FROM strat_b_watch GROUP BY status")}
    mode = "LIVE" if s["strat_b.mode"] == "LIVE" else "PAPER"
    return {"v": 2, "ts": iso(), "label": LIVE_LABEL if mode == "LIVE" else LABEL, "mode": mode, "paperLabel": "B PAPER",
            "live": live_card(c, limit), "enabled": bool(s["strat_b.enabled"]), "night": c.night,
            "bankrollStart": round(start, 2), "bankroll": round(start + pnl_all, 2), "free": round(start + pnl_all - in_pos, 2),
            "inPositions": round(in_pos, 2), "unrealizedUsd": round(unreal, 2),
            "pnlAllUsd": round(pnl_all, 2), "pnlTonightUsd": round(sum(o["pnl_usd"] or 0 for o in tonight), 2),
            "closedTonight": len(tonight), "closedAll": len(closed), "wins": len(wins), "losses": len(losses),
            "winRatePct": round(100 * len(wins) / len(closed), 1) if closed else None,
            "profitFactor": round(gw / gl, 2) if gl > 0 else None,
            "watching": w.get("watching", 0), "skipped": w.get("skipped", 0), "expired": w.get("expired", 0),
            "open": [_order_view(s, o) for o in opens], "closed": [_order_view(s, o) for o in closed[:limit]],
            "rules": rules(s)}


def _src_chip(src):
    """Shown first in each row's check list so the existing app shows where B found the coin (not a check)."""
    return {"k": "source", "label": "Via", "pass": None, "value": src or "desk scanner", "bar": ""}


def live_feed(c, limit=100) -> dict:
    """strat_b_live.json: newest evaluations tonight + the current watchlist (DECISION 60)."""
    s, db, now = c.s, c.db, c.clock() if callable(getattr(c, "clock", None)) else time.time()
    items = []
    for r in db.rows("SELECT * FROM strat_b_evals WHERE night=? ORDER BY id DESC LIMIT ?", (c.night, limit)):
        items.append({"id": r["id"], "t": iso(r["ts"]), "mint": r["mint"], "name": r["name"], "symbol": r["symbol"],
                      "kind": r["kind"], "status": r["status"], "source": r["source"] or "desk scanner",
                      "reason": f"via {r['source'] or 'desk scanner'} · {r['reason']}", "price": r["price"],
                      "checks": [_src_chip(r["source"])] + json.loads(r["checks"] or "[]"), "link": links.coin_url(r["mint"], "solana")})
    wl = []
    for w in db.rows("SELECT * FROM strat_b_watch WHERE status='watching' ORDER BY added_at DESC"):
        ck = json.loads(w["checks"] or "[]")
        exp = w["added_at"] + s["strat_b.watch_max_min"] * 60
        if w["grad_at"]:
            exp = min(exp, w["grad_at"] + s["strat_b.max_age_min"] * 60)
        wl.append({"mint": w["mint"], "name": w["name"], "symbol": w["symbol"], "addedAt": iso(w["added_at"]),
                   "expiresAt": iso(exp), "minLeft": max(0, round((exp - now) / 60)),
                   "ageMin": round((now - w["grad_at"]) / 60) if w["grad_at"] else None,
                   "source": w["source"] or "desk scanner",
                   "price": w["last_price"], "high": w["high"], "low": w["low"], "checks": [_src_chip(w["source"])] + ck,
                   "passCount": sum(1 for x in ck if x["pass"]), "checkCount": len(ck),
                   "away": away(s, w, ck, now) if ck else ["waiting for a price"],
                   "evalAt": iso(w["eval_at"]) if w["eval_at"] else None, "link": links.coin_url(w["mint"], "solana")})
    n = {r["status"]: r["n"] for r in db.rows("SELECT status, COUNT(*) n FROM strat_b_watch WHERE night=? GROUP BY status", (c.night,))}
    mode = "LIVE" if s["strat_b.mode"] == "LIVE" else "PAPER"
    return {"v": 1, "ts": iso(), "label": LIVE_LABEL if mode == "LIVE" else LABEL, "mode": mode, "night": c.night, "enabled": bool(s["strat_b.enabled"]),
            "tonightOnly": c.db.meta(TEMP_KEY, []) or [],
            "counts": {"watching": len(wl), "olderFeedWatching": sum(1 for w in wl if (w["source"] or "").startswith("gecko")), "skippedTonight": n.get("skipped", 0), "boughtTonight": n.get("bought", 0),
                       "expiredTonight": n.get("expired", 0), "evaluationsTonight": db.one("SELECT COUNT(*) n FROM strat_b_evals WHERE night=?", (c.night,))["n"]},
            "items": items, "watchlist": wl}
