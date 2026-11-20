"""Strategy B research profile (DECISION 91). PAPER test of the Page
"How profitable crypto bot users set up their bots" (2026-11-19), tests A1-A4 run as their own book next to Strategy A.

Entry (same coins and timing as A, plus the Page's filters):
  - Only coins the scanner + auditor pass (A's feed). B's GeckoTerminal older-coin feed is ignored: the Page's data says
    later entries do worse, so this profile does not reach for 1h-7d-old coins.
  - Buy within strat_b.rs_entry_window_s of the auditor pass (A buys at the pass after a <=30 s dip wait), otherwise expire.
  - Market cap >= scanner.min_market_cap_usd ($15K) and the 30% DexScreener-vs-pool gate are kept (Page A3).
  - Bundled wallets <= strat_b.max_bundle_pct and the auditor's dev <5% / mint+freeze revoked (Page A3: skip launches where
    the dev and first-block wallets already took a large share; skip live mint/freeze authority).
  - Token-2022 mints with a transfer hook, transfer fee or permanent delegate are skipped (Page A3).
  - A Jupiter quote before every buy: $20 of USDC -> token, then all of those tokens -> SOL. No route on either leg = skip
    the coin (unsellable). Jupiter down / erroring = wait and try again inside the window (fail closed, never buy blind).
  - One open position at a time; fixed $20 (never shrinks, never $50).
Exits (whichever first):
  - -rs_stop_pct (50%) stop for fast dumps (Page A1 keeps A's -50%).
  - At rs_fee_check_min (5 min): sell if the position would not cover its own round-trip fees (paper net <= 0).
  - At rs_hard_close_min (10 min): sell if the trail has not armed.
  - Trail arms at +rs_trail_start_pct (15%), sells on a rs_trail_pct (8%) drop from the high, only when the expected net
    after fees is at least +rs_trail_floor_pct (8%). Under the floor it holds (same as A's trail).
  - Liquidity pulled (pool < 20% of entry) and B's 24 h time stop stay as safety nets.
Fills: B's paper model (DexScreener price, 1% slippage + 2x size/liquidity impact, 1% GMGN fee + $0.25 network per side)
plus rs_protocol_fee_pct (1.25%) per side for pump.fun protocol + creator fees (the Page's ~2.5% round trip).
Books: the same trade list replayed on a $34 book and a $500 book; a book skips a trade it cannot afford.
LIVE (owner 2026-11-19 4:42 PM CT "Go ahead and send b live"): same entry and exits; buys and sells go through
strat_b_live.BLive (A's signed GMGN client) at a fixed $20 (no fallback or shrink; less than $20 free = refuse with
'insufficient balance'), server-side stop -rs_stop_pct. This module itself never signs, sends, or reads a key.
Jupiter quotes are read-only (jupiter.py)."""
from __future__ import annotations
import json
import fields

PROFILE = "research"
FEE_CHECK, HARD_CLOSE, RS_TRAIL, RS_STOP = "time stop 5m (not covering fees)", "time stop 10m (trail not armed)", "trail", "stop"
META_BROKE_UNTIL = "strat_b_rs_broke_until"        # LIVE: insufficient balance seen, no buy attempt until then
META_BROKE_PUSH_NIGHT = "strat_b_rs_broke_push_night"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
BAD_EXT = {"transferHook": "transfer hook", "transferFeeConfig": "transfer fee", "permanentDelegate": "permanent delegate"}



def quote(*a, **k):
    """Read-only Jupiter quote; tests replace this."""
    import jupiter
    return jupiter.quote(*a, **k)


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def is_research_profile(s) -> bool:
    try:
        return (s["strat_b.profile"] or "default") == PROFILE
    except Exception:
        return False


def is_research_order(o) -> bool:
    return o is not None and (o.get("profile") or "") == PROFILE


# ---------- filters
def static_check(s, audit, src=None) -> str | None:
    """Checked once when the auditor passes a coin. None = watch it for the entry window."""
    if (src or "").startswith("gecko"):
        return "research profile buys only fresh scanner + auditor passes (no older-coin feed)"
    if audit.get("b_block"):
        return audit["b_block"]
    need = float(s["strat_b.rs_min_lp_locked_pct"] or 0)    # DECISION 97: B's own LP-lock guard, independent of the auditor's
    if need > 0:
        lp = num(audit.get("lpLockedPct"))
        if lp is None:
            return f"lp unlocked (<{need:g}%): lock unknown"
        if lp < need:
            return f"lp unlocked (<{need:g}%): {lp:g}% locked"
    bp = num(audit.get("bundle_pct"), 0.0) or 0.0
    if bp > s["strat_b.max_bundle_pct"]:
        return f"bundled wallets hold {bp:g}% > {s['strat_b.max_bundle_pct']:g}% (bundled launch)"
    ext = audit.get("token_ext") or []
    bad = [BAD_EXT[e] for e in ext if e in BAD_EXT]
    if bad:
        return "Token-2022 " + ", ".join(bad)
    return None


def entry_check(s, w, pair, now) -> tuple[bool, str, dict]:
    g = lambda p: fields.get(pair, p)
    price, liq, mc = num(g("priceUsd")), num(g("liquidity.usd"), 0.0), num(g("marketCap")) or num(g("fdv"))
    m = {"price": price, "liq": liq, "mc": mc}
    age_s = now - (w.get("added_at") or now)
    m["sinceSeenS"] = round(age_s)
    if age_s > float(s["strat_b.rs_entry_window_s"]):
        return False, f"entry window over ({age_s:.0f}s > {s['strat_b.rs_entry_window_s']:g}s after the auditor pass)", m
    if not price:
        return False, "no price", m
    mn = float(s["scanner.min_market_cap_usd"] or 0)
    if mc is None or (mn > 0 and mc < mn):
        return False, f"mc ${(mc or 0):,.0f} < ${mn:,.0f}", m
    size = float(s["strat_b.rs_trade_usd"])
    if size > liq * s["strat_b.max_pool_pct"] / 100.0:
        return False, f"${size:g} is over {s['strat_b.max_pool_pct']:g}% of the pool", m
    return True, f"auditor pass {age_s:.0f}s ago, mc ${mc:,.0f}", m


def checks(s, w, pair, now) -> list[dict]:
    g = lambda p: fields.get(pair, p) if pair else None
    out = []

    def add(k, label, ok, value, bar):
        out.append({"k": k, "label": label, "pass": ok, "value": value, "bar": bar})
    bpv = num(w.get("bundle_pct"), 0.0) or 0.0
    add("bundle", "Bundled", bpv <= s["strat_b.max_bundle_pct"], f"{bpv:.1f}%", f"≤ {s['strat_b.max_bundle_pct']:g}%")
    age = now - (w.get("added_at") or now)
    add("window", "Since pass", age <= float(s["strat_b.rs_entry_window_s"]), f"{age:.0f}s", f"≤ {s['strat_b.rs_entry_window_s']:g}s")
    price = num(g("priceUsd"))
    mn = float(s["scanner.min_market_cap_usd"] or 0)
    if not price:
        add("mc", "Market cap", None, "no price yet", f"≥ ${mn:,.0f}")
        add("liq", "Pool size", None, "no price yet", f"${s['strat_b.rs_trade_usd']:g} ≤ {s['strat_b.max_pool_pct']:g}%")
        return out
    liq, mc = num(g("liquidity.usd"), 0.0), num(g("marketCap")) or num(g("fdv"))
    add("mc", "Market cap", None if mc is None else mc >= mn, "n/a" if mc is None else f"${mc:,.0f}", f"≥ ${mn:,.0f}")
    add("liq", "Pool size", float(s["strat_b.rs_trade_usd"]) <= liq * s["strat_b.max_pool_pct"] / 100.0, f"${liq:,.0f}",
        f"${s['strat_b.rs_trade_usd']:g} ≤ {s['strat_b.max_pool_pct']:g}%")
    return out


def away(ck) -> list[str]:
    return [f"{x['label']} {x['value']} (needs {x['bar']})" for x in ck if x["pass"] is False] or \
           (["waiting for a price"] if any(x["pass"] is None for x in ck) else [])


async def sell_check(s, mint, size_usd=None) -> dict:
    """Jupiter round trip for the planned size. {'ok': bool, 'skip': bool (permanent), 'why': str}.
    ok=False/skip=False means Jupiter itself failed: do not buy now, try again on the next poll."""
    if not s["strat_b.rs_sell_check"]:
        return {"ok": True, "skip": False, "why": "sell check off"}
    size = float(size_usd if size_usd is not None else s["strat_b.rs_trade_usd"])
    b = await quote(USDC, mint, int(round(size * 1e6)))
    if not b.get("ok"):
        if b.get("no_route"):
            return {"ok": False, "skip": True, "why": f"no Jupiter buy route ({b.get('error')})"}
        return {"ok": False, "skip": False, "why": f"can't check sellability now ({b.get('error')})"}
    q = await quote(mint, SOL, int(b["out"]))
    if not q.get("ok"):
        if q.get("no_route"):
            return {"ok": False, "skip": True, "why": f"unsellable: no Jupiter sell route for ${size:g} ({q.get('error')})"}
        return {"ok": False, "skip": False, "why": f"can't check sellability now ({q.get('error')})"}
    return {"ok": True, "skip": False, "why": f"Jupiter sell route ok ({q.get('route')}, impact {q.get('impact_pct', 0):.1f}%)",
            "sell_lamports": q["out"], "tokens_raw": b["out"]}


# ---------- fills (B's paper model + pump.fun protocol / creator fee)
def fill_buy(s, price, size, liq) -> dict:
    import strat_b
    f = strat_b.fill_buy(s, price, size, liq)
    extra = size * float(s["strat_b.rs_protocol_fee_pct"]) / 100.0
    tokens = max(0.0, size - f["fees"] - extra) / f["price"]
    return {"price": f["price"], "tokens": tokens, "fees": f["fees"] + extra}


def fill_sell(s, price, tokens, liq) -> dict:
    import strat_b
    f = strat_b.fill_sell(s, price, tokens, liq)
    gross = f["proceeds"] + f["fees"]
    extra = gross * float(s["strat_b.rs_protocol_fee_pct"]) / 100.0
    return {"price": f["price"], "proceeds": max(0.0, f["proceeds"] - extra), "fees": f["fees"] + extra}


def net_pct(s, o, price, liq=None) -> float | None:
    if not price or not o.get("size_usd"):
        return None
    tokens = o.get("tokens") or (o["size_usd"] / o["entry_price"] if o.get("entry_price") else 0)
    f = fill_sell(s, price, tokens, liq or o.get("last_liq") or o.get("entry_liq"))
    return (f["proceeds"] / o["size_usd"] - 1) * 100.0


def floor_price(s, o, liq=None) -> float | None:
    """Lowest mark at which expected net >= the trail floor (net is monotonic in price; bisect)."""
    e = o.get("entry_price")
    if not e:
        return None
    target = float(s["strat_b.rs_trail_floor_pct"])
    lo, hi = e * 0.5, e * 10
    if (net_pct(s, o, hi, liq) or -1e9) < target:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if (net_pct(s, o, mid, liq) or -1e9) >= target:
            hi = mid
        else:
            lo = mid
    return hi


def trail_line(s, o, liq=None) -> float | None:
    if not o.get("trail_armed") or not o.get("peak"):
        return None
    raw = o["peak"] * (1 - float(s["strat_b.rs_trail_pct"]) / 100.0)
    fl = floor_price(s, o, liq)
    return max(raw, fl) if fl else raw


# ---------- exits
def exit_check(s, o, price, liq, now) -> tuple[str | None, dict]:
    e = o["entry_price"]
    peak = max(o["peak"] or e, price)
    armed = bool(o["trail_armed"]) or peak >= e * (1 + float(s["strat_b.rs_trail_start_pct"]) / 100.0)
    upd = {"peak": peak, "trail_armed": 1 if armed else 0, "below_floor": 0}
    if o.get("entry_liq") and liq is not None and liq < o["entry_liq"] * 0.2:
        import strat_b
        return strat_b.PULLED, upd
    if price <= e * (1 - float(s["strat_b.rs_stop_pct"]) / 100.0):
        return RS_STOP, upd
    net = net_pct(s, o, price, liq)
    if armed:
        trig = peak * (1 - float(s["strat_b.rs_trail_pct"]) / 100.0)
        if net is not None and net >= float(s["strat_b.rs_trail_floor_pct"]):
            if price <= trig:
                return RS_TRAIL, upd
        else:
            upd["below_floor"] = 1
    held_min = (now - o["opened_at"]) / 60.0
    if not armed and held_min >= float(s["strat_b.rs_hard_close_min"]):
        return HARD_CLOSE, upd
    if not armed and held_min >= float(s["strat_b.rs_fee_check_min"]) and (net is None or net <= 0):
        return FEE_CHECK, upd
    if now - o["opened_at"] >= s["strat_b.time_stop_h"] * 3600:
        import strat_b
        return strat_b.TIME, upd
    return None, upd


def exits_text(s) -> str:
    return (f"stop -{s['strat_b.rs_stop_pct']:g}% · at {s['strat_b.rs_fee_check_min']:g} min sell unless it covers its fees · "
            f"at {s['strat_b.rs_hard_close_min']:g} min sell if the trail hasn't armed · trail arms at +{s['strat_b.rs_trail_start_pct']:g}%, "
            f"sells on a {s['strat_b.rs_trail_pct']:g}% drop, only at +{s['strat_b.rs_trail_floor_pct']:g}% net or better")


# ---------- books ($34 and $500 replays of the same trades)
def books(c) -> list[dict]:
    s, db = c.s, c.db
    import strat_b
    rows = db.rows("SELECT * FROM strat_b_orders WHERE mode='PAPER' AND profile=? AND (status='open' OR COALESCE(closed_at,0)>=?)"
                   " ORDER BY opened_at, id", (PROFILE, strat_b.paper_since(db)))
    out = []
    for key, label in (("strat_b.rs_book_small_usd", "small"), ("strat_b.rs_book_large_usd", "large")):
        start = float(s[key])
        bal, taken, skipped, wins, losses, open_cost, unreal = start, 0, 0, 0, 0, 0.0, 0.0
        for o in rows:
            if bal - open_cost < (o["size_usd"] or 0):
                skipped += 1
                continue
            taken += 1
            if o["status"] == "closed":
                p = o["pnl_usd"] or 0.0
                bal += p
                wins += p > 0
                losses += p <= 0
            elif o["status"] == "open":
                open_cost += o["size_usd"] or 0
                est = fill_sell(s, o["last_price"] or o["entry_price"], o["tokens"], o["last_liq"] or o["entry_liq"])
                unreal += est["proceeds"] - o["size_usd"]
        closed = wins + losses
        out.append({"book": label, "startUsd": round(start, 2), "balanceUsd": round(bal, 2), "pnlUsd": round(bal - start, 2),
                    "pnlPct": round((bal / start - 1) * 100, 1) if start else None, "unrealizedUsd": round(unreal, 2),
                    "trades": taken, "closed": closed, "wins": wins, "losses": losses,
                    "winRatePct": round(100 * wins / closed, 1) if closed else None,
                    "skippedNoCash": skipped, "canTrade": bal - open_cost >= float(s["strat_b.rs_trade_usd"])})
    return out


def large_book_free(c) -> float:
    b = books(c)[1]
    return b["balanceUsd"] - sum(o["size_usd"] or 0 for o in c.db.rows(
        "SELECT size_usd FROM strat_b_orders WHERE mode='PAPER' AND profile=? AND status='open'", (PROFILE,)))


class _TrailView:
    """A's trail_net_ok helpers read risk.trail_* keys; map them to the research trail for LIVE quote checks."""
    MAP = {"risk.trail_start_pct": "strat_b.rs_trail_start_pct", "risk.trail_pct": "strat_b.rs_trail_pct",
           "risk.trail_floor_pct": "strat_b.rs_trail_floor_pct"}

    def __init__(self, s):
        self._s = s

    def __getitem__(self, k):
        return self._s[self.MAP.get(k, k)]


def trail_settings(s):
    return _TrailView(s)


def rules(s) -> list[dict]:
    v = lambda k: s[k]
    return [
        {"k": "Profile", "v": ("Research (LIVE, real money)" if v("strat_b.mode") == "LIVE" else "Research (PAPER)") + ": Strategy A's coins with the Page's filters, short holds and a tight trail",
         "why": "Page 'How profitable crypto bot users set up their bots', tests A1–A4. Copy trading (A5) is not built."},
        {"k": "Entry", "v": f"a coin the scanner + auditor pass, bought within {v('strat_b.rs_entry_window_s'):g}s of the pass; mc ≥ ${v('scanner.min_market_cap_usd'):,.0f}; the 30% DexScreener-vs-pool gate",
         "why": "Same coins and timing as A so the result isolates the exit and filter changes; the Page says keep the $15K floor and 30% gate"},
        {"k": "Can it sell", "v": f"Jupiter quote of ${v('strat_b.rs_trade_usd'):g} in and all of it back out to SOL before every buy; no route = skip",
         "why": "Page A3: quote a sell of the planned size and skip if there is no route (the #21 honeypot had none)"},
        {"k": "Launch filters", "v": f"bundled ≤ {v('strat_b.max_bundle_pct'):g}%, dev < {v('auditor.max_dev_pct'):g}%, mint + freeze revoked, no Token-2022 transfer hook / fee / permanent delegate",
         "why": "Page A3: skip bundled launches, live authorities and Token-2022 traps"},
        {"k": "Size", "v": f"${v('strat_b.rs_trade_usd'):g} fixed, {v('strat_b.rs_max_open')} position at a time, never shrinks"
                           + (" · LIVE: real GMGN orders from B's share of the wallet; under $20 free = 'insufficient balance', no buy" if v("strat_b.mode") == "LIVE" else ""),
         "why": "Page A4: under $20 fees eat more of the trade; one open at a time"},
        {"k": "Stop", "v": f"-{v('strat_b.rs_stop_pct'):g}% from entry", "why": "Page A1 keeps the -50% stop for fast dumps"},
        {"k": "Time stops", "v": f"at {v('strat_b.rs_fee_check_min'):g} min sell unless it covers round-trip fees; at {v('strat_b.rs_hard_close_min'):g} min sell if the trail hasn't armed",
         "why": "Page A1: bots holding 5–30 min had a −32.1% median and 5.1% profitable"},
        {"k": "Trail", "v": f"arms at +{v('strat_b.rs_trail_start_pct'):g}%, sells on a {v('strat_b.rs_trail_pct'):g}% drop from the high, only at +{v('strat_b.rs_trail_floor_pct'):g}% net or better",
         "why": "Page A2: arm +12–15%, trail 8–10%, net floor at least +8% so a winner still clears fees"},
        {"k": "Fees counted", "v": f"{v('strat_b.fee_pct'):g}% GMGN + ${v('strat_b.net_fee_usd'):.2f} network + {v('strat_b.rs_protocol_fee_pct'):g}% pump.fun protocol/creator per side, {v('strat_b.slippage_pct'):g}% slippage + pool impact",
         "why": "Page: count GMGN's 1%, pump.fun protocol fees (~2.5% round trip), network fees and slippage"},
        {"k": "Books", "v": f"same trades on a ${v('strat_b.rs_book_small_usd'):,.0f} book and a ${v('strat_b.rs_book_large_usd'):,.0f} book",
         "why": "Page A4: test the real wallet size and a $500 simulated bankroll; judge after 100+ trades"},
    ]
