"""STRATEGY B (DECISION 54; PAPER or LIVE since DECISION 66).

A second, separate strategy built from the research Page "What top memecoin traders do vs the desk".
It reads the same scanner + auditor feed as the live GMGN strategy (A) but decides on its own:
it waits out the post-graduation dump, buys a pullback that is bouncing, sizes flat from its own simulated
bankroll, and exits with a wide stop (a looser first-window stop, then the normal -35%), a 2x full sell,
Strategy A's trailing take-profit (arm at +20%, sell on a 20% drop from the peak, only at +5% net after fees),
and a 24h time stop. The old spike sell was replaced by that trail (DECISION 85).

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
PAPER_SINCE_META = "strat_b_paper_since"   # owner 2026-11-19 4:51 PM CT reset: B paper P&L counts closes at/after this (history kept)
NO_PRICE_HOLD_S = 1800  # with no price, keep holding (the time stop still applies)


def paper_since(db) -> float:
    try:
        return float(db.meta(PAPER_SINCE_META) or 0)
    except (TypeError, ValueError):
        return 0.0


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
def entry_check(s, w, pair, now, hist=None) -> tuple[bool, str, dict]:
    """w: watch row with high/low (since the high). Returns (buy?, reason, metrics)."""
    import strat_b_gta as G
    import strat_b_research as R
    if R.is_research_profile(s):
        return R.entry_check(s, w, pair, now)
    g = lambda p: fields.get(pair, p)
    price, liq, mc = num(g("priceUsd")), num(g("liquidity.usd"), 0.0), num(g("marketCap")) or num(g("fdv"))
    m = {"price": price, "liq": liq, "mc": mc}
    if G.is_gta_profile(s):
        if not price:
            return False, "no tradable price", m
        name, symbol = w.get("name"), w.get("symbol")
        if not G.gta_related(name, symbol):
            return False, "not a GTA-related coin", m
        ok, why = G.gta_spike_hit(s, hist or [], now, price, pair)
        return ok, why, m
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
    import strat_b_research as R
    if R.is_research_profile(s):
        return R.checks(s, w, pair, now)
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
    if "wait" not in c:                                  # research profile (DECISION 91) has its own checks
        import strat_b_research as R
        return R.away(ck)
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


# ---------- exits (DECISION 85: A's trail + first-window wider stop; spike sell retired)
class _BTrailS:
    """Settings view so A's trail_net_* helpers use B's trail keys and the same GMGN fee math as A."""
    MAP = {"risk.trail_start_pct": ("strat_b.trail_start_pct", "risk.trail_start_pct"),
           "risk.trail_pct": ("strat_b.trail_pct", "risk.trail_pct"),
           "risk.trail_floor_pct": ("strat_b.trail_floor_pct", "risk.trail_floor_pct")}

    def __init__(self, s):
        self._s = s

    def __getitem__(self, k):
        if k in self.MAP:
            primary, fallback = self.MAP[k]
            try:
                v = self._s[primary]
            except Exception:
                v = None
            if v is not None:
                return v
            return self._s[fallback]
        return self._s[k]


def b_trail_settings(s):
    return _BTrailS(s)


def b_size_ladder_text(s) -> str:
    """Public B size text, e.g. '$50, then $20'. LIVE uses strat_b.trade_usd then A's fallback / shrink."""
    from agents.gmgn_trader import _usd_plain
    top, mid = float(s["strat_b.trade_usd"]), float(s["gmgn.fallback_trade_usd"])
    parts = [_usd_plain(top)]
    if 0 < mid < top:
        parts.append(_usd_plain(mid))
    return ", then ".join(parts)


def early_fields(s, start, profile=None) -> dict:
    """exits_phase / early_until at fill (or at the original buy time for an adopt)."""
    prof = profile or ((s["strat_b.profile"] if s is not None else None) or "default")
    if prof in ("gta", "research"):
        return {"exits_phase": "normal", "early_until": None}
    mins = float(s["strat_b.early_stop_min"] or 0)
    if mins > 0:
        return {"exits_phase": "early", "early_until": float(start) + mins * 60}
    return {"exits_phase": "normal", "early_until": None}


def early_clock_open(s, o, now) -> bool:
    """True while this order is still inside the first-window wider stop."""
    if float(s["strat_b.early_stop_min"] or 0) <= 0:
        return False
    if (o.get("exits_phase") or "") != "early":
        return False
    until = o.get("early_until")
    if until is None:
        until = (o.get("opened_at") or 0) + float(s["strat_b.early_stop_min"]) * 60
    return now < until


def early_window_ended(s, o, now) -> bool:
    """Still marked early, but the clock has elapsed (or the early window was turned off)."""
    if (o.get("exits_phase") or "") != "early":
        return False
    return not early_clock_open(s, o, now)


def b_stop_pct(s, o, now) -> float:
    """Desk stop: -early_stop_pct in the first window, then -stop_pct. Pre-DECISION 85 rows (no phase) use -35%.
    GTA-profile orders use a flat -gta_stop_pct with no early window (DECISION 87)."""
    import strat_b_gta as G
    import strat_b_research as R
    if G.is_gta_order(o, s):
        return float(s["strat_b.gta_stop_pct"])
    if R.is_research_order(o):
        return float(s["strat_b.rs_stop_pct"])
    if early_clock_open(s, o, now):
        return float(s["strat_b.early_stop_pct"])
    return float(s["strat_b.stop_pct"])


def b_exits_text(s, o=None, now=None) -> str:
    """Buy-push / rules line for B's exits."""
    import strat_b_gta as G
    import strat_b_research as R
    if R.is_research_order(o) or (o is None and R.is_research_profile(s)):
        return R.exits_text(s)
    if G.is_gta_order(o, s) or (o is None and G.is_gta_profile(s)):
        return (f"flat stop -{s['strat_b.gta_stop_pct']:g}% (desk + GMGN in LIVE) · "
                f"sell all at +{s['strat_b.gta_tp_pct']:g}% net after fees · "
                f"or when net profit holds more than {s['strat_b.gta_profit_hold_s']:g}s · "
                f"or at the end of the spike if still in net profit "
                f"(drop {s['strat_b.gta_spike_drop_pct']:g}% from the peak or no new high for "
                f"{s['strat_b.gta_spike_stall_s']:g}s). No trail, no time stop. "
                f"A night loss of {s['strat_b.gta_kill_pct']:g}% of the wallet baseline restores standard B.")
    early_m = float(s["strat_b.early_stop_min"] or 0)
    stop = (f"stop -{s['strat_b.early_stop_pct']:g}% for the first {early_m:g} min, then -{s['strat_b.stop_pct']:g}%"
            if early_m > 0 else f"stop -{s['strat_b.stop_pct']:g}%")
    if o is not None and now is not None and early_clock_open(s, o, now):
        stop = f"stop -{s['strat_b.early_stop_pct']:g}% for the first {early_m:g} min, then -{s['strat_b.stop_pct']:g}%"
    trail = (f"trail arms at +{s['strat_b.trail_start_pct']:g}%, sells on a {s['strat_b.trail_pct']:g}% drop from the high, "
             f"only when expected net after fees is at least +{s['strat_b.trail_floor_pct']:g}%")
    return f"{stop} · sell all at +{s['strat_b.take_profit_pct']:g}% · {trail}"


def _b_trail_order(o) -> dict:
    """Order row for A's trail_net_* helpers (entry + optional chain size)."""
    return o if isinstance(o, dict) else dict(o)


def exit_check(s, o, price, liq, hist, now, below_floor=False, quote_sol_out=None) -> tuple[str | None, dict]:
    """o: open order row. hist is kept for callers; the spike sell is gone (DECISION 85).
    Returns (reason|None, updates). The take-profit trail reuses A's net-floor math.
    GTA-profile orders use DECISION 87 exits (no trail, no time stop)."""
    import strat_b_gta as G
    if G.is_gta_order(o, s):
        return G.gta_exit_check(s, o, price, liq, now, quote_sol_out=quote_sol_out)
    import strat_b_research as R
    if R.is_research_order(o):
        return R.exit_check(s, o, price, liq, now)
    from agents.gmgn_trader import trail_should_sell, trail_net_floor_price
    e = o["entry_price"]
    peak = max(o["peak"] or e, price)
    ts = b_trail_settings(s)
    armed = bool(o["trail_armed"]) or peak >= e * (1 + float(ts["risk.trail_start_pct"]) / 100.0)
    upd = {"peak": peak, "trail_armed": 1 if armed else 0, "below_floor": 0}
    if o.get("entry_liq") and liq is not None and liq < o["entry_liq"] * 0.2:
        return PULLED, upd
    if price <= e * (1 - b_stop_pct(s, o, now) / 100.0):
        return STOP, upd
    if price >= e * (1 + s["strat_b.take_profit_pct"] / 100.0):
        return TP, upd
    row = _b_trail_order(o)
    if trail_should_sell(ts, row, price, peak, below_floor=below_floor):
        return TRAIL, upd
    floor_px = trail_net_floor_price(ts, row)
    if armed and floor_px is not None and price < floor_px:
        upd["below_floor"] = 1
    if now - o["opened_at"] >= s["strat_b.time_stop_h"] * 3600:
        return TIME, upd
    return None, upd


def rules(s) -> list[dict]:
    """Plain-words rules with the live value and the reason (shown in the app and the scorecard)."""
    import strat_b_gta as G
    v = lambda k: s[k]
    import strat_b_research as R
    if R.is_research_profile(s):
        return R.rules(s)
    if G.is_gta_profile(s):
        return [
            {"k": "Profile", "v": "GTA — only Grand Theft Auto / GTA / GTA VI / GTA 6 / Rockstar / Vice City coins, and only when they are in an active spike",
             "why": "Name and symbol match (word boundaries, so 'gtable' does not count). Candidates come from raw new/trending sources, not the scanner's pass list."},
            {"k": "Entry", "v": f"in a spike of at least +{v('strat_b.gta_spike_min_pct'):g}% inside {v('strat_b.gta_spike_window_s'):g}s (same hurdle as Strategy A's fast surge)",
             "why": "Buy the burst as it hits, not a pullback. No market-cap, liquidity, age, holder, auditor, or dex/pool filters. A tradable price, wallet room, $50 then $20, and max open are the only execution checks."},
            {"k": "Size", "v": f"${v('strat_b.trade_usd'):,.0f}, then ${v('gmgn.fallback_trade_usd'):,.0f}, then shrink to ${v('gmgn.min_trade_usd'):,.0f}; max {v('strat_b.live_max_open') if v('strat_b.mode')=='LIVE' else v('strat_b.max_open')} open",
             "why": "Same $50 / $20 ladder as standard B"},
            {"k": "Stop", "v": f"flat -{v('strat_b.gta_stop_pct'):g}% from entry (desk + a GMGN server-side stop in LIVE). No first-window -50%.",
             "why": "A hard floor so a failed spike cannot go to zero"},
            {"k": "Take profit", "v": f"sell all at +{v('strat_b.gta_tp_pct'):g}% net after fees, or when net profit stays > 0 for more than {v('strat_b.gta_profit_hold_s'):g}s (the clock resets if net dips to 0 or below)",
             "why": "Same net estimate as A's trail floor: a full-position GMGN sell quote first, else mark minus the 1% fee and priority/tip"},
            {"k": "Spike over", "v": f"sell when the price drops {v('strat_b.gta_spike_drop_pct'):g}% from the spike peak or makes no new high for {v('strat_b.gta_spike_stall_s'):g}s, but only if expected net after fees is still > 0. Otherwise hold for the stop or a later take-profit.",
             "why": "Get off the spike when momentum dies, without selling a loser that the -30% stop or a bounce can still handle"},
            {"k": "Not used", "v": "no trailing stop, no 24h time stop, none of standard B's entry filters",
             "why": "This profile is a spike scalp, not the pullback strategy"},
            {"k": "Kill switch", "v": f"if GTA B's night losses (realized + unrealized) reach {v('strat_b.gta_kill_pct'):g}% of the wallet value recorded when GTA was first enabled this night (re-baselined at 8 AM CT), B restores the saved standard Strat B preset, stays LIVE, and keeps any open GTA positions on their existing exits",
             "why": "Automatic, on the desk, even if the phone is off (DECISION 87)"},
        ]
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
        {"k": "Stop", "v": (f"-{v('strat_b.early_stop_pct'):g}% for the first {v('strat_b.early_stop_min'):g} min, then -{v('strat_b.stop_pct'):g}% from entry"
                            if float(v('strat_b.early_stop_min') or 0) > 0
                            else f"-{v('strat_b.stop_pct'):g}% from entry, from the first second")
               + (" (server-side on GMGN in LIVE; placed at the early stop, then tightened)" if v("strat_b.mode") == "LIVE" else ""),
         "why": "Normal stop stays -35% (three.ws). First 5 min uses -50% so a volatile open doesn't get stopped out (DECISION 85)"},
        {"k": "Take profit", "v": f"sell everything at +{v('strat_b.take_profit_pct'):g}%",
         "why": "Guides take money at 2x; pros sell 90%+ in one go (KOL Brain)"},
        {"k": "Trail", "v": (f"arms at +{v('strat_b.trail_start_pct'):g}%, sells on a {v('strat_b.trail_pct'):g}% drop from the high, "
                             f"only when expected net after fees is at least +{v('strat_b.trail_floor_pct'):g}%"),
         "why": "Same trailing take-profit as Strategy A (DECISION 85); replaced the old spike sell so winners can run"},
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
        self._trail_below_floor: set = set()    # order ids whose trail is under the net floor (DECISION 85)
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
        import strat_b_gta as G
        if G.is_gta_profile(s):
            if not G.gta_related(name, scan.get("symbol") or (c.coin(mint) or {}).get("symbol")):
                bad = "not a GTA-related coin"
            else:
                bad = None
        elif __import__("strat_b_research").is_research_profile(s):
            bad = __import__("strat_b_research").static_check(s, audit, src)
        else:
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
        ok_msg = ("GTA name match, waiting for a spike" if __import__("strat_b_gta").is_gta_profile(s)
                  else "research filters ok, checking the entry" if __import__("strat_b_research").is_research_profile(s)
                  else "filters ok, waiting for a pullback")
        self._eval(w, "offer", status, bad or ok_msg, ck, None, now)
        self.log.info(f"{iso(now)}\t{'skip' if bad else 'watch'}\t{mint}\t{name}\t{bad or ok_msg}"
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
        r = self.c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM strat_b_orders WHERE status='closed' AND mode='PAPER' AND COALESCE(closed_at,0)>=?",
                          (paper_since(self.c.db),))
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
        import strat_b_gta as G
        import strat_b_research as R
        s, mode = self.c.s, self.mode()
        if R.is_research_profile(s):                     # DECISION 91: one at a time; PAPER: the $500 book must cover $20
            if mode == "LIVE":
                n = self.c.db.one("SELECT COUNT(*) n FROM strat_b_orders WHERE mode='LIVE' AND status IN ('submitting','pending','open','exiting')")["n"]
                if n >= int(s["strat_b.rs_max_open"]):
                    return f"{n} open (max {int(s['strat_b.rs_max_open'])})"
                until = float(self.c.db.meta(R.META_BROKE_UNTIL, 0) or 0)
                if self.c.clock() < until:               # insufficient balance seen: no wallet read or GMGN call until then
                    return f"insufficient balance (re-check after {iso(until)})"
                return None
            n = self.c.db.one("SELECT COUNT(*) n FROM strat_b_orders WHERE status='open' AND mode='PAPER'")["n"]
            if n >= int(s["strat_b.rs_max_open"]):
                return f"{n} open (max {int(s['strat_b.rs_max_open'])})"
            if R.large_book_free(self.c) < float(s["strat_b.rs_trade_usd"]):
                return f"${s['strat_b.rs_book_large_usd']:,.0f} paper book can't cover ${s['strat_b.rs_trade_usd']:g}"
            return None
        if mode == "LIVE":                               # held or being bought/sold for real
            n = self.c.db.one("SELECT COUNT(*) n FROM strat_b_orders WHERE mode='LIVE' AND status IN ('submitting','pending','open','exiting')")["n"]
        else:
            n = self.c.db.one("SELECT COUNT(*) n FROM strat_b_orders WHERE status='open' AND mode='PAPER'")["n"]
        mx = s["strat_b.live_max_open"] if mode == "LIVE" else s["strat_b.max_open"]    # DECISION 71: B LIVE's own slots
        if n >= mx:
            return f"{n} open (max {mx})"
        if G.is_gta_profile(s):
            if mode == "PAPER" and self.bankroll() - self.in_positions() < s["strat_b.trade_usd"]:
                return "paper bankroll too low"
            return None
        lim = self.daily_loss_limit()
        pnl = self.night_pnl(mode)                       # LIVE: real P&L only
        if pnl <= -lim:
            return f"daily loss stop: ${pnl:,.2f} {'real ' if mode == 'LIVE' else ''}tonight"
        if mode == "PAPER" and self.bankroll() - self.in_positions() < s["strat_b.trade_usd"]:
            return "paper bankroll too low"
        return None

    # ---- loop
    async def loop(self):
        import strat_b_gta as G
        self.log.info(f"{iso()}\tstart\tmode={self.c.s['strat_b.mode']}\tenabled={self.c.s['strat_b.enabled']}"
                      f"\tprofile={self.c.s['strat_b.profile']}"
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
            wait = float(self.c.s["strat_b.gta_poll_s"] if G.gta_active(self.c.s) else self.c.s["strat_b.poll_s"])
            await asyncio.sleep(wait)

    async def tick(self, pairs=None):
        c, s = self.c, self.c.s
        import strat_b_gta as G
        now = c.clock()
        self.apply_reverts(now)
        if G.gta_active(s):
            await G.ensure_baseline(c, log=self.log)
            fired = await G.check_kill(c, log=self.log, notifier=c.notifier)
            if fired:
                s = self.c.s
        if G.gta_active(s) and now - getattr(self, "_gta_hunted", 0.0) >= float(s["strat_b.gta_hunt_s"]):
            self._gta_hunted = now
            await self.gta_hunt(now)
        opens = c.db.rows("SELECT * FROM strat_b_orders WHERE status='open'")
        watch = []
        if self.enabled():
            exp = now - s["strat_b.watch_max_min"] * 60
            old_age = now - s["strat_b.max_age_min"] * 60
            if __import__("strat_b_research").is_research_profile(s):
                win = now - float(s["strat_b.rs_entry_window_s"])
                for w in c.db.rows("SELECT * FROM strat_b_watch WHERE status='watching' AND added_at<?", (win,)):
                    self._expire_watch(w, f"no entry within {s['strat_b.rs_entry_window_s']:g}s of the auditor pass", now,
                                       row_reason=self._last_reason.get(w["mint"]) or "no entry")
            elif not G.is_gta_profile(s):
                for w in c.db.rows("SELECT * FROM strat_b_watch WHERE status='watching' AND (added_at<? OR grad_at<?)", (exp, old_age)):
                    why = (f"older than {s['strat_b.max_age_min'] / 1440:g}d" if w["grad_at"] and w["grad_at"] < old_age
                           else f"no entry in {s['strat_b.watch_max_min']}m")
                    self._expire_watch(w, why, now, row_reason=self._last_reason.get(w["mint"]) or "no entry")
            watch = c.db.rows("SELECT * FROM strat_b_watch WHERE status='watching' ORDER BY added_at DESC LIMIT ?", (int(s["strat_b.max_watch"]),))
            poll = float(s["strat_b.gta_poll_s"] if G.is_gta_profile(s) else s["strat_b.watch_poll_s"])
            if pairs is None:                            # live: watched coins every watch_poll_s, open positions every tick
                if now - getattr(self, "_watch_polled", 0.0) < poll:
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

    def _sample(self, mint, now, price):
        h = self.hist.setdefault(mint, collections.deque())
        h.append((now, price))
        keep = max(float(self.c.s["strat_b.spike_window_s"]), float(self.c.s["strat_b.gta_spike_window_s"] or 0), 60.0) + 30
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
        hist = self._sample(w["mint"], now, price) if price else list(self.hist.get(w["mint"]) or [])
        ok, why, m = entry_check(s, w, pair or {}, now, hist=hist)
        ck = checks(s, w, pair, now)
        c.db.x("UPDATE strat_b_watch SET checks=?, eval_at=?, evals=COALESCE(evals,0)+1 WHERE mint=?", (json.dumps(ck), now, w["mint"]))
        if ok:
            block = self.can_buy() or (self.live.blocked_retry(w["mint"], now) if self.mode() == "LIVE" else None)
            if block:
                ok, why = False, block
        if ok and self.mode() == "LIVE" and __import__("strat_b_research").is_research_profile(s):
            broke = await self._research_live_balance(w, m, pair, now)    # before any Jupiter / pool / GMGN call
            if broke:
                self._last_reason[w["mint"]] = broke.split(":")[0]
                self._eval(w, "buy", "refused", f"B LIVE refused: {broke}", ck, price, now)
                return
        if ok and __import__("strat_b_research").is_research_profile(s):
            ok, why, skip = await self._research_prebuy(w, m, why, pair)
            if skip:
                c.db.x("UPDATE strat_b_watch SET status='skipped', reason=?, updated=? WHERE mint=?", (why, now, w["mint"]))
                self._eval(w, "wait", "skipped", why, ck, price, now)
                self._last_reason.pop(w["mint"], None)
                self.log.info(f"{iso(now)}	skip	{w['mint']}	{w['name']}	{why}	price={price}	mc={m.get('mc')}")
                return
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

    async def _research_prebuy(self, w, m, why, pair=None):
        """DECISION 91 pre-buy checks at the buy moment: 30% dex/pool gate, then the Jupiter can-it-sell round trip.
        Returns (ok, reason, skip_permanently)."""
        import fastprice as fp, strat_b_research as R
        s = self.c.s
        sol = None                                       # SOL/USD from the DexScreener pair (no GMGN call from B paper)
        if pair and (fields.get(pair, "quoteToken.address") or "") == R.SOL:
            pn, pu = num(fields.get(pair, "priceNative")), num(fields.get(pair, "priceUsd"))
            sol = (pu / pn) if (pn and pu) else None
        try:
            res = await fp.check_buy_dex_pool(self.c, w["mint"], m["price"], sol_usd=sol, pair=pair or None)
        except Exception as e:
            res = {"blocked": False, "error": type(e).__name__}
        if res.get("blocked"):
            return False, f"dex vs pool: {res['reason']}", True
        if res.get("error") and res["error"] != "off":
            self.log.info(f"{iso(self.c.clock())}	dex vs pool: no pool price, not blocking	{w['mint']}	{res['error']}")
        try:
            sc = await R.sell_check(s, w["mint"])
        except Exception as e:
            sc = {"ok": False, "skip": False, "why": f"can't check sellability now ({type(e).__name__})"}
        if not sc["ok"]:
            return False, sc["why"], bool(sc.get("skip"))
        m["sellCheck"] = sc["why"]
        return True, f"{why} · {sc['why']}", False

    async def _research_live_balance(self, w, m, pair, now):
        """DECISION 91 (owner 4:42 PM CT 'send b live'): before any GMGN call, read the wallet over public RPC and refuse
        locally when B's share can't cover the fixed $20 + fees. Logs 'insufficient balance', pauses LIVE buy attempts for
        REFUSE_RETRY_S, pushes once a night. Returns the refusal text or None."""
        import strat_b_research as R
        from agents.gmgn_trader import MAIN_WALLET, LAMPORTS, strategy_free_lamports, buy_cost_lamports
        c, s = self.c, self.c.s
        size = float(s["strat_b.rs_trade_usd"])
        sol = None
        if pair and (fields.get(pair, "quoteToken.address") or "") == R.SOL:
            pn, pu = num(fields.get(pair, "priceNative")), num(fields.get(pair, "priceUsd"))
            sol = (pu / pn) if (pn and pu) else None
        try:
            lam = int(((await c.net.rpc("getBalance", [MAIN_WALLET])) or {}).get("value"))
        except Exception:
            lam = None
        if lam is None or not sol:
            return None                                  # unknown here: BLive.buy decides with its own (GMGN) read
        free = strategy_free_lamports(s, c.db, "B", lam)
        need = buy_cost_lamports(s, int(size / sol * LAMPORTS))
        if free is not None and free >= need:
            return None
        import strat_b_live
        why = (f"insufficient balance: B's share of the wallet is {(free or 0) / LAMPORTS:.4f} SOL (${(free or 0) / LAMPORTS * sol:,.2f}),"
               f" a ${size:g} buy needs {need / LAMPORTS:.4f} SOL with fees")
        until = now + strat_b_live.REFUSE_RETRY_S
        c.db.set_meta(R.META_BROKE_UNTIL, until)
        self.live._row(ts=now, night=c.night, mode="LIVE", mint=w["mint"], name=w["name"], symbol=w.get("symbol"), status="refused",
                       size_usd=0, note=why, opened_at=now)
        self.log.info(f"{iso(now)}	LIVE refused	{w['mint']}	{w['name']}	insufficient balance	{why}	no GMGN call; next try after {iso(until)}")
        if c.db.meta(R.META_BROKE_PUSH_NIGHT) != c.night:
            c.db.set_meta(R.META_BROKE_PUSH_NIGHT, c.night)
            await self._push(f"{LIVE_LABEL} (research) · Can't buy {w['name']}: {why}. I'll keep checking quietly; no more pushes about this tonight.", w["mint"])
        return why

    async def _buy(self, w, pair, m, why, now):
        """PAPER fill (simulated)."""
        c, s = self.c, self.c.s
        import strat_b_research as R
        prof = (s["strat_b.profile"] or "default")
        research = prof == R.PROFILE
        size = float(s["strat_b.rs_trade_usd"] if research else s["strat_b.trade_usd"])
        f = (R.fill_buy if research else fill_buy)(s, m["price"], size, m["liq"])
        ef = early_fields(s, now, profile=prof)
        cur = c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,symbol,status,size_usd,signal_price,entry_price,tokens,buy_fees_usd,peak,"
                     "last_price,last_liq,entry_liq,mc_entry,trail_armed,opened_at,why,last_seen,mode,exits_phase,early_until,profile,profit_since,spike_high_at) "
                     "VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,0,?,?,?,'PAPER',?,?,?,?,?)",
                     (now, c.night, w["mint"], w["name"], w["symbol"], size, m["price"], f["price"], f["tokens"], f["fees"], f["price"],
                      m["price"], m["liq"], m["liq"], m["mc"], now, json.dumps(dict(m, reason=why)), now,
                      ef["exits_phase"], ef["early_until"], prof, None, now))
        c.db.x("UPDATE strat_b_watch SET status='bought', reason=?, updated=? WHERE mint=?", (why, now, w["mint"]))
        self._last_reason.pop(w["mint"], None)
        self.hist[w["mint"]] = collections.deque([(now, m["price"])])
        self.log.info(f"{iso(now)}\tBUY\t{w['mint']}\t{w['name']}\t#{cur.lastrowid}\t${size:.2f}\tsignal={m['price']:.6g}\tfill={f['price']:.6g}"
                      f"\tfees=${f['fees']:.2f}\tmc={m['mc']}\tliq={m['liq']}\t{why}")
        text = (f"{LABEL}{' (research)' if research else ''} · Bought {w['name']} · ${size:,.2f} @ ${f['price']:.6g}\n"
                f"mc ${m['mc']:,.0f} · {why}\n"
                f"{b_exits_text(s)}")
        await self._push(text, w["mint"])
        c.event("fill", w["mint"])

    async def _manage(self, o, pair, now):
        c, s = self.c, self.c.s
        price = num(fields.get(pair, "priceUsd")) if pair else None
        liq = num(fields.get(pair, "liquidity.usd")) if pair else None
        import strat_b_gta as G
        live = (o.get("mode") or "PAPER") == "LIVE"
        import strat_b_research as R
        if not price and R.is_research_order(o) and not live and not o["trail_armed"] and o["last_price"] \
                and now - o["opened_at"] >= float(s["strat_b.rs_hard_close_min"]) * 60:
            await self._sell(o, o["last_price"], o["last_liq"], R.HARD_CLOSE + " (no price)", now)
            return
        if not price:
            if (not G.is_gta_order(o, s)) and now - o["opened_at"] >= s["strat_b.time_stop_h"] * 3600 and o["last_price"]:
                if live:
                    await self.live.sell(o, TIME + " (no price)", o["last_price"], now)
                else:
                    await self._sell(o, o["last_price"], o["last_liq"], TIME + " (no price)", now)
            return
        if live:                                         # real position: B's exits with real sells (DECISION 66)
            await self.live.manage(o, price, liq, now)
            return
        if early_window_ended(s, o, now):
            c.db.x("UPDATE strat_b_orders SET exits_phase='normal' WHERE id=?", (o["id"],))
            o = dict(o, exits_phase="normal")
            self.log.info(f"{iso(now)}\tearly stop over\t{o['mint']}\t{o['name']}\t#{o['id']}\tnow -{s['strat_b.stop_pct']:g}%")
        hist = self._sample(o["mint"], now, price)
        below = o["id"] in self._trail_below_floor
        reason, upd = exit_check(s, o, price, liq, hist, now, below_floor=below)
        c.db.x("UPDATE strat_b_orders SET peak=?, trail_armed=?, last_price=?, last_liq=?, last_seen=?, profit_since=?, spike_high_at=? WHERE id=?",
               (upd["peak"], upd["trail_armed"], price, liq, now, upd.get("profit_since"), upd.get("spike_high_at"), o["id"]))
        if upd.get("below_floor"):
            self._trail_below_floor.add(o["id"])
        elif o["id"] in self._trail_below_floor and not reason:
            self._trail_below_floor.discard(o["id"])
        if upd["trail_armed"] and not o["trail_armed"]:
            self.log.info(f"{iso(now)}\ttrail armed\t{o['mint']}\t{o['name']}\t#{o['id']}\tpeak={upd['peak']:.6g}")
        if reason:
            self._trail_below_floor.discard(o["id"])
            await self._sell(dict(o, **upd), price, liq, reason, now)

    async def _sell(self, o, price, liq, reason, now):
        c, s = self.c, self.c.s
        import strat_b_research as R
        research = R.is_research_order(o)
        f = (R.fill_sell if research else fill_sell)(s, price, o["tokens"], liq or o["last_liq"] or o["entry_liq"])
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
        if research:
            bk = R.books(c)
            foot = " · ".join(f"${b['startUsd']:,.0f} book ${b['balanceUsd']:,.2f}" for b in bk)
        else:
            foot = f"B paper bankroll ${self.bankroll():,.2f}"
        text = (f"{LABEL}{' (research)' if research else ''} · Sold {o['name']} ({reason}) · {sign}${abs(pnl):,.2f} ({pct:+.1f}%) · held {held_txt}\n"
                f"{foot}")
        await self._push(text, o["mint"])
        c.event("exit", o["mint"])

    async def _push(self, text, mint):
        try:
            await self.c.notifier.push(text, [[{"text": "Open on FOMO", "url": links.coin_url(mint, "solana")}]], kind="strat_b", mint=mint)
        except Exception as e:
            self.c.error("strat_b", "push", str(e)[:200])

    def see_raw(self, scan):
        """GTA intake from a raw source (no auditor / scanner / rugcheck). No-op unless GTA is on and B is enabled."""
        import strat_b_gta as G
        if not (self.enabled() and G.is_gta_profile(self.c.s)):
            return
        mint = (scan or {}).get("mint")
        if not mint:
            return
        coin = self.c.coin(mint) or {}
        name = scan.get("name") or coin.get("name")
        symbol = scan.get("symbol") or coin.get("symbol")
        if not (name or symbol) or not G.gta_related(name, symbol):
            return
        self._offer({**scan, "name": name, "symbol": symbol,
                     "b_source": scan.get("b_source") or "gta hunt"}, {"passed": True})

    async def gta_hunt(self, now=None):
        """Pull GTA-named candidates from DexScreener search, the coins table, and Gecko new_pools."""
        import strat_b_gta as G
        c = self.c
        now = now or c.clock()
        seen = set()

        def take(mint, name=None, symbol=None, src="gta hunt"):
            if not mint or mint in seen:
                return
            seen.add(mint)
            try:
                self.see_raw({"mint": mint, "name": name, "symbol": symbol, "b_source": src})
            except Exception as e:
                c.error("strat_b", "gta_hunt", f"{mint} {str(e)[:200]}")

        try:
            for r in c.db.rows("SELECT mint,name,symbol FROM coins ORDER BY first_seen DESC LIMIT 400"):
                if G.gta_related(r["name"], r["symbol"]):
                    take(r["mint"], r["name"], r["symbol"], "coins")
        except Exception as e:
            c.error("strat_b", "gta_hunt", f"coins {str(e)[:200]}")

        fn = getattr(c.net, "gecko_new_pools", None)
        if fn:
            try:
                from agents.scanner import parse_new_pools
                payload = await fn(1)
                for r in parse_new_pools(payload, others=True):
                    take(r["mint"], r.get("name"), None, "gecko new_pools")
            except Exception as e:
                c.error("strat_b", "gta_hunt", f"new_pools {str(e)[:200]}")

        search = getattr(c.net, "dex_search", None)
        if search:
            for term in ("gta", "rockstar", "vice city", "grand theft auto"):
                try:
                    rows = await search(term)
                except Exception as e:
                    c.error("strat_b", "gta_hunt", f"dex_search {term} {str(e)[:160]}")
                    continue
                for pair in rows or []:
                    if not isinstance(pair, dict):
                        continue
                    if (pair.get("chainId") or "").lower() not in ("solana", "sol", ""):
                        # keep solana; a missing chainId still gets a look
                        if pair.get("chainId"):
                            continue
                    base = pair.get("baseToken") or {}
                    take(base.get("address"), base.get("name"), base.get("symbol"), f"dex search {term}")

        self.log.info(f"{iso(now)}\tgta hunt\tcandidates={len(seen)}")

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
         "profile": o.get("profile") or "default",
         "why": (json.loads(o["why"]).get("reason") if o["why"] else None), "link": links.coin_url(o["mint"], "solana")}
    if o["status"] == OPEN:
        import strat_b_research as R
        if mode == "LIVE":                               # estimate from the real entry (fees + rent included)
            pnl = (last / e - 1) * o["size_usd"] if (e and last) else 0.0
        elif R.is_research_order(o):
            est = R.fill_sell(s, last, o["tokens"], o["last_liq"] or o["entry_liq"])
            pnl = est["proceeds"] - o["size_usd"]
        else:
            est = fill_sell(s, last, o["tokens"], o["last_liq"] or o["entry_liq"])
            pnl = est["proceeds"] - o["size_usd"]
        from agents.gmgn_trader import trail_trigger, trail_net_floor_price
        now = o.get("last_seen") or o.get("opened_at")
        ts = b_trail_settings(s)
        floor_px = trail_net_floor_price(ts, o) if o["trail_armed"] else None
        trail_line = (trail_trigger(ts, e, o["peak"], floor=floor_px) if o["trail_armed"] and e else None)
        v.update(pnlUsd=round(pnl, 2), pct=round(pnl / o["size_usd"] * 100, 2), trailArmed=bool(o["trail_armed"]),
                 stopLine=e * (1 - b_stop_pct(s, o, now) / 100.0) if e else None,
                 tpLine=e * (1 + s["strat_b.take_profit_pct"] / 100.0) if e else None,
                 trailLine=trail_line,
                 timeStopAt=iso(o["opened_at"] + s["strat_b.time_stop_h"] * 3600))
        if R.is_research_order(o):
            v.update(tpLine=None, trailLine=R.trail_line(s, o), feeCheckAt=iso(o["opened_at"] + float(s["strat_b.rs_fee_check_min"]) * 60),
                     timeStopAt=None if o["trail_armed"] else iso(o["opened_at"] + float(s["strat_b.rs_hard_close_min"]) * 60))
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
    closed_all = db.rows("SELECT * FROM strat_b_orders WHERE mode='LIVE' AND status='closed' ORDER BY closed_at DESC")
    import pnl_since                                     # DECISION 72: display baseline (owner reset); risk code doesn't use live_card
    since_ts = pnl_since.since(s, "B")
    closed = [o for o in closed_all if since_ts is None or (o["closed_at"] or 0) >= since_ts]
    earlier = [o for o in closed_all if since_ts is not None and (o["closed_at"] or 0) < since_ts]
    failed = db.rows("SELECT * FROM strat_b_orders WHERE mode='LIVE' AND status IN ('failed','unknown') ORDER BY ts DESC LIMIT 10")
    refused = db.rows("SELECT * FROM strat_b_orders WHERE mode='LIVE' AND status='refused' AND night=? ORDER BY ts DESC", (c.night,))
    wins = [o for o in closed if (o["pnl_usd"] or 0) > 0]
    losses = [o for o in closed if (o["pnl_usd"] or 0) <= 0]
    gw, gl = sum(o["pnl_usd"] for o in wins), -sum(o["pnl_usd"] or 0 for o in losses)
    tonight = [o for o in closed if o["night"] == c.night]
    import strat_b_live
    wallet, free_sol = strat_b_live.wallet_view(c)
    prof = (s["strat_b.profile"] or "default")
    return {"label": LIVE_LABEL, "mode": "LIVE", "active": s["strat_b.mode"] == "LIVE",
            "profile": prof, "profileLabel": {"gta": "GTA", "research": "Research"}.get(prof, "Standard"),
            "pnlAllUsd": round(sum(o["pnl_usd"] or 0 for o in closed), 2), "pnlTonightUsd": round(sum(o["pnl_usd"] or 0 for o in tonight), 2),
            "closedTonight": len(tonight), "closedAll": len(closed), "wins": len(wins), "losses": len(losses),
            "winRatePct": round(100 * len(wins) / len(closed), 1) if closed else None,
            "profitFactor": round(gw / gl, 2) if gl > 0 else None,
            "unrealizedUsd": round(sum(_order_view(s, o)["pnlUsd"] or 0 for o in opens), 2),
            "inPositions": round(sum(o["size_usd"] or 0 for o in opens), 2),
            "wallet": wallet, "freeSol": free_sol, "reserveSol": s["gmgn.sol_reserve"],
            "budget": strat_b_live.budget(c),
            "refusedTonight": len(refused), "lastRefusal": ({"t": iso(refused[0]["ts"]), "name": refused[0]["name"], "reason": refused[0]["note"]} if refused else None),
            "since": pnl_since.iso(since_ts), "sinceLabel": pnl_since.label(since_ts),
            "earlierClosed": len(earlier), "earlierPnlUsd": round(sum(o["pnl_usd"] or 0 for o in earlier), 2),
            "open": [_order_view(s, o) for o in opens], "closed": [_order_view(s, o) for o in closed[:limit]],
            "earlier": [_order_view(s, o) for o in earlier[:10]],
            "failed": [_order_view(s, o) for o in failed]}


def scorecard(c, limit=40) -> dict:
    """PAPER fields at the top level (unchanged shape, paper rows only) + 'live' (real money, B LIVE) section."""
    s, db = c.s, c.db
    opens = db.rows("SELECT * FROM strat_b_orders WHERE status='open' AND mode='PAPER' ORDER BY opened_at DESC")
    closed = db.rows("SELECT * FROM strat_b_orders WHERE status='closed' AND mode='PAPER' AND COALESCE(closed_at,0)>=? ORDER BY closed_at DESC",
                     (paper_since(db),))
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
    prof = (s["strat_b.profile"] or "default")
    return {"v": 2, "ts": iso(), "label": LIVE_LABEL if mode == "LIVE" else LABEL, "mode": mode, "paperLabel": "B PAPER",
            "live": live_card(c, limit), "enabled": bool(s["strat_b.enabled"]), "profile": prof,
            "profileLabel": {"gta": "GTA", "research": "Research"}.get(prof, "Standard"),
            "gtaKillPct": float(s["strat_b.gta_kill_pct"]),
            "gtaStopPct": float(s["strat_b.gta_stop_pct"]),
            "gtaTpPct": float(s["strat_b.gta_tp_pct"]),
            "gtaProfitHoldS": float(s["strat_b.gta_profit_hold_s"]), "night": c.night,
            "paperSince": iso(paper_since(db)) if paper_since(db) else None,
            "bankrollStart": round(start, 2), "bankroll": round(start + pnl_all, 2), "free": round(start + pnl_all - in_pos, 2),
            "inPositions": round(in_pos, 2), "unrealizedUsd": round(unreal, 2),
            "pnlAllUsd": round(pnl_all, 2), "pnlTonightUsd": round(sum(o["pnl_usd"] or 0 for o in tonight), 2),
            "closedTonight": len(tonight), "closedAll": len(closed), "wins": len(wins), "losses": len(losses),
            "winRatePct": round(100 * len(wins) / len(closed), 1) if closed else None,
            "profitFactor": round(gw / gl, 2) if gl > 0 else None,
            "watching": w.get("watching", 0), "skipped": w.get("skipped", 0), "expired": w.get("expired", 0),
            "open": [_order_view(s, o) for o in opens], "closed": [_order_view(s, o) for o in closed[:limit]],
            "rules": rules(s), "tradeUsd": float(s["strat_b.trade_usd"]),
            "fallbackTradeUsd": float(s["gmgn.fallback_trade_usd"]), "minTradeUsd": float(s["gmgn.min_trade_usd"]),
            "sizeLabel": b_size_ladder_text(s), "research": research_card(c, limit)}


def research_card(c, limit=40) -> dict:
    """DECISION 91: research-profile PAPER results only, replayed on the $34 and $500 books. Never real money."""
    import strat_b_research as R
    s, db = c.s, c.db
    opens = db.rows("SELECT * FROM strat_b_orders WHERE mode='PAPER' AND profile=? AND status='open' ORDER BY opened_at DESC", (R.PROFILE,))
    closed = db.rows("SELECT * FROM strat_b_orders WHERE mode='PAPER' AND profile=? AND status='closed' AND COALESCE(closed_at,0)>=? ORDER BY closed_at DESC",
                     (R.PROFILE, paper_since(db)))
    return {"label": "B PAPER · research", "mode": "PAPER",
            "active": R.is_research_profile(s) and bool(s["strat_b.enabled"]) and s["strat_b.mode"] != "LIVE",
            "tradeUsd": float(s["strat_b.rs_trade_usd"]), "books": R.books(c), "exits": R.exits_text(s),
            "open": [_order_view(s, o) for o in opens], "closed": [_order_view(s, o) for o in closed[:limit]],
            "closedAll": len(closed), "rules": R.rules(s)}


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
