"""Strategy B GTA profile (DECISION 87).

Buy only if (1) the name/symbol is GTA-related and (2) the coin is in an active price spike
right now. No scanner, auditor, market-cap, liquidity, age, holder, bundle, or dex/pool gates.
A tradable price/route, wallet affordability, $50 then $20 then shrink, and max-open are the
only execution requirements.

Exits, whichever fires first: +gta_tp_pct net after fees; net > 0 continuously for
gta_profit_hold_s; spike-over (drop from peak or no new high for stall_s) but ONLY when
expected net after fees is > 0 (otherwise hold); flat -gta_stop_pct desk and GMGN stop.
No trailing stop, no time stop.

Kill switch: if tonight's GTA B realized+unrealized PnL reaches -gta_kill_pct of the wallet
baseline taken when the GTA profile was first enabled this night (re-baselined at 8 AM CT
rollover), the running desk restores the saved standard B preset in-process (force=True so
PROTECTED keys take effect without a restart), leaves B LIVE, and keeps any open GTA
positions on their existing GTA exits (no force-sell).
"""
from __future__ import annotations
import re, time
import fields
from ctx import iso

GTA_TP = "gta take profit"
GTA_HOLD = "gta profit hold"
GTA_SPIKE = "gta spike over"
META_BASE_USD = "gta_kill_baseline_usd"
META_BASE_NIGHT = "gta_kill_baseline_night"
META_BASE_TS = "gta_kill_baseline_ts"
META_FIRED_NIGHT = "gta_kill_fired_night"

# Longer tokens first. Word / compact boundaries so "gtable" and "dogtag" do not match,
# but "GTA6INU" / "GTAVI" still count as GTA-related.
_GTA_WORD = re.compile(r"(?:^|[^a-z])gta(?:[^a-z]|$)")
_GTA_FAM = re.compile(r"(?:^|[^a-z])(?:gtavi|gta6|gtav)")
_GRAND = re.compile(r"\bgrand\s+theft\b")
_ROCK = re.compile(r"\brockstar\b")
_VICE = re.compile(r"\bvice\s+city\b")

HUNT_TERMS = ("gta", "gtavi", "gta 6", "gta6", "grand theft auto", "rockstar", "vice city")


def gta_related(name=None, symbol=None) -> bool:
    """True for GTA / GTA VI / GTA 6 / Grand Theft Auto / Rockstar / Vice City, case-insensitive."""
    blob = f"{name or ''} {symbol or ''}".lower()
    if not blob.strip():
        return False
    norm = re.sub(r"[^a-z0-9]+", " ", blob).strip()
    compact = norm.replace(" ", "")
    if _GTA_WORD.search(norm) or _GTA_WORD.search(compact) or _GTA_FAM.search(compact):
        return True
    if _GRAND.search(norm) or _ROCK.search(norm) or _VICE.search(norm):
        return True
    return False


def is_gta_profile(s) -> bool:
    try:
        return (s["strat_b.profile"] or "default") == "gta"
    except Exception:
        return False


def is_gta_order(o, s=None) -> bool:
    """Open/closed rows stamped 'gta' keep GTA exits even after the profile is switched back."""
    if o is not None and (o.get("profile") or "") == "gta":
        return True
    if o is not None and o.get("profile"):
        return False
    return bool(s is not None and is_gta_profile(s))


def gta_active(s) -> bool:
    """B is on the GTA profile and enabled (will hunt and buy)."""
    return bool(is_gta_profile(s) and s["strat_b.enabled"])


def gta_net_pct(s, o, price, quote_sol_out=None) -> float | None:
    """Expected net % after fees vs SOL/USD spent. Quote first, else mark minus 1% and priority/tip.
    Same shape as Strategy A's trail floor (trail_sol_in / trail_expected_sol_out)."""
    from agents.gmgn_trader import trail_sol_in, trail_expected_sol_out
    from strat_b import b_trail_settings
    ts = b_trail_settings(s)
    sol_in = trail_sol_in(ts, o)
    sol_out = trail_expected_sol_out(ts, o, price, quote_sol_out=quote_sol_out)
    if sol_in and sol_out is not None:
        return 100.0 * (sol_out / sol_in - 1.0)
    entry = o.get("entry_price") if o else None
    if not entry or not price or float(entry) <= 0 or float(price) <= 0:
        return None
    try:
        fee = float(s["gmgn.fee_pct"] or 0) / 100.0
    except Exception:
        fee = 0.01
    if fee >= 1:
        return None
    # no size: buy fee + sell fee, same fallback as trail_net_ok
    return 100.0 * ((float(price) / float(entry)) * (1 - fee) - fee - 1.0)


def gta_spike_hit(s, hist, now, price, pair=None) -> tuple[bool, str]:
    """Active spike: price is up at least gta_spike_min_pct from the low in gta_spike_window_s.
    Defaults 20% / 60s match Strategy A's fast surge (SURGE_FAST_PCT / SURGE_FAST_S) so we
    buy the same kind of just-hit burst, not a slow grind. If history is still thin, a
    DexScreener m5 change at or above that threshold counts as already mid-spike."""
    import strat_b as B
    if not price or price <= 0:
        return False, "no price"
    need = float(s["strat_b.gta_spike_min_pct"])
    win = float(s["strat_b.gta_spike_window_s"])
    samples = [(t, p) for t, p in (hist or []) if p and now - t <= win]
    if price not in [p for _, p in samples]:
        samples.append((now, price))
    pct = None
    if len(samples) >= 2:
        low = min(p for _, p in samples)
        if low > 0:
            pct = (price / low - 1.0) * 100.0
            if pct >= need:
                return True, f"spike +{pct:.0f}% in {win:.0f}s"
    # first sight of a coin already running: m5 change as a backstop
    if pair is not None and pct is None:
        m5 = B.num(fields.get(pair, "priceChange.m5"))
        if m5 is not None and m5 >= need:
            return True, f"spike +{m5:.0f}% on the 5m print (need +{need:g}% / {win:.0f}s)"
    if pct is None:
        return False, f"no spike yet (need +{need:g}% in {win:.0f}s)"
    return False, f"no spike ({pct:.0f}% in {win:.0f}s, need +{need:g}%)"


def gta_spike_over(s, o, price, now) -> bool:
    """Momentum ended: dropped gta_spike_drop_pct from the spike peak, or no new high for stall_s.
    Defaults 8% (same as A's surge-top drop) and 15s (fast enough that a 60s spike can end cleanly)."""
    e = o.get("entry_price") or price
    peak = max(o.get("peak") or e, price or 0)
    if not peak or not price:
        return False
    drop = float(s["strat_b.gta_spike_drop_pct"])
    if price <= peak * (1 - drop / 100.0):
        return True
    stall = float(s["strat_b.gta_spike_stall_s"])
    high_at = o.get("spike_high_at") or o.get("opened_at") or now
    return (now - float(high_at)) >= stall


def gta_exit_check(s, o, price, liq, now, quote_sol_out=None) -> tuple[str | None, dict]:
    """Whichever fires first: -30% stop, +20% net TP, 60s in net profit, spike-over only if net > 0."""
    import strat_b as B
    e = o["entry_price"]
    peak = max(o["peak"] or e, price)
    high_at = o.get("spike_high_at") or o.get("opened_at") or now
    if price > (o.get("peak") or e or 0) + 1e-16:
        high_at = now
    upd = {"peak": peak, "trail_armed": 0, "below_floor": 0, "spike_high_at": high_at,
           "profit_since": o.get("profit_since")}
    # liquidity gone is an execution failure, not a strategy filter; still exit if the pool vanishes
    if o.get("entry_liq") and liq is not None and liq < o["entry_liq"] * 0.2 and liq == 0:
        return B.PULLED, upd
    stop = float(s["strat_b.gta_stop_pct"])
    if price <= e * (1 - stop / 100.0):
        return B.STOP, upd
    net = gta_net_pct(s, o, price, quote_sol_out=quote_sol_out)
    if net is not None and net > 0:
        if not upd["profit_since"]:
            upd["profit_since"] = now
    else:
        upd["profit_since"] = None
    if net is not None and net >= float(s["strat_b.gta_tp_pct"]):
        return GTA_TP, upd
    held = upd["profit_since"]
    if held and (now - float(held)) > float(s["strat_b.gta_profit_hold_s"]):
        return GTA_HOLD, upd
    # spike-over only in net profit; if the spike ends underwater, hold for stop / later TP
    if gta_spike_over(s, {**o, "peak": peak, "spike_high_at": high_at}, price, now):
        if net is not None and net > 0:
            return GTA_SPIKE, upd
    return None, upd


def gta_night_pnl(c, night=None) -> float:
    """Realized PnL of GTA B closes this night plus unrealized mark PnL on open GTA B positions."""
    night = night or c.night
    total = 0.0
    for r in c.db.rows("SELECT pnl_usd FROM strat_b_orders WHERE profile='gta' AND status='closed' AND night=?", (night,)):
        total += float(r["pnl_usd"] or 0)
    for r in c.db.rows("SELECT size_usd, entry_price, last_price FROM strat_b_orders WHERE profile='gta' AND status IN ('open','pending','exiting') AND night=?", (night,)):
        size, entry, last = float(r["size_usd"] or 0), r["entry_price"], r["last_price"]
        if size and entry and last and float(entry) > 0:
            total += size * (float(last) / float(entry) - 1.0)
    return total


def baseline_usd(c):
    v = c.db.meta(META_BASE_USD)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def record_baseline(c, usd, *, source="gta baseline") -> float:
    usd = float(usd)
    c.db.set_meta(META_BASE_USD, usd)
    c.db.set_meta(META_BASE_NIGHT, c.night)
    c.db.set_meta(META_BASE_TS, c.clock() if callable(getattr(c, "clock", None)) else time.time())
    try:
        c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
               (time.time(), "gta_kill_baseline_usd", "null", str(usd), source))
    except Exception:
        pass
    return usd


async def wallet_mark_usd(c) -> float | None:
    """SOL + token positions at mark, in USD. Tests may set c.gta_wallet_usd to a number or callable."""
    hook = getattr(c, "gta_wallet_usd", None)
    if callable(hook):
        v = hook()
        return None if v is None else float(v)
    if isinstance(hook, (int, float)):
        return float(hook)
    t = getattr(c, "gmgn", None)
    total = 0.0
    try:
        sol_usd = await t.sol_usd() if t is not None else None
        lam = await t.wallet_lamports(fresh=True) if t is not None else None
        if sol_usd and lam:
            total += (int(lam) / 1e9) * float(sol_usd)
    except Exception:
        pass
    try:
        if t is not None and t.client is not None:
            d = await t.client.wallet_holdings()
            for h in (d or {}).get("list") or []:
                for k in ("usd_value", "usdValue", "value_usd", "valueUsd", "usd"):
                    if h.get(k) is not None:
                        total += float(h[k])
                        break
    except Exception:
        pass
    return total or None


async def ensure_baseline(c, log=None) -> float | None:
    """Record the wallet mark the first time GTA is enabled this night. None if not measurable yet."""
    if not gta_active(c.s):
        return baseline_usd(c)
    if c.db.meta(META_BASE_NIGHT) == c.night and baseline_usd(c) is not None:
        return baseline_usd(c)
    usd = await wallet_mark_usd(c)
    if usd is None or usd <= 0:
        if log:
            log.info(f"{iso()}\tgta baseline\twaiting for a wallet mark")
        return None
    record_baseline(c, usd, source=f"gta profile first enabled this night ({c.night})")
    if log:
        log.info(f"{iso()}\tgta baseline\t${usd:,.2f}\tnight={c.night}")
    return usd


async def on_night_rollover(c, log=None) -> float | None:
    """8 AM CT: if GTA is still running, take a fresh baseline for the new night."""
    c.db.set_meta(META_FIRED_NIGHT, None)
    if not gta_active(c.s):
        c.db.set_meta(META_BASE_USD, None)
        c.db.set_meta(META_BASE_NIGHT, None)
        return None
    usd = await wallet_mark_usd(c)
    if usd is None or usd <= 0:
        c.db.set_meta(META_BASE_USD, None)
        c.db.set_meta(META_BASE_NIGHT, c.night)
        return None
    record_baseline(c, usd, source=f"gta baseline at night rollover ({c.night})")
    if log:
        log.info(f"{iso()}\tgta baseline\trollover ${usd:,.2f}\tnight={c.night}")
    return usd


def kill_reason(c, pnl, base) -> str:
    pct = 0 if not base else abs(pnl) / base * 100.0
    return (f"GTA B lost ${abs(pnl):,.2f} ({pct:.0f}% of the ${base:,.2f} night wallet baseline), "
            f"so I restored the saved standard Strat B preset and left B LIVE. "
            f"Open GTA positions stay open with their -30% stop and take-profit exits; I didn't sell them.")


async def check_kill(c, log=None, notifier=None) -> dict | None:
    """If tonight's GTA losses reach gta_kill_pct of the baseline, restore standard B in this process."""
    s = c.s
    if not gta_active(s):
        return None
    if c.db.meta(META_FIRED_NIGHT) == c.night:
        return None
    base = await ensure_baseline(c, log=log)
    if not base or base <= 0:
        return None
    pnl = gta_night_pnl(c)
    cap = float(s["strat_b.gta_kill_pct"] or 50) / 100.0
    if pnl > -cap * base + 1e-9:
        return None
    import presets
    source = f"gta kill switch: night {c.night}, pnl={pnl:.2f}, baseline={base:.2f}"
    # Stop GTA buys first (profile+enabled change below). Restore standard B LIVE in-process.
    changed = presets.restore_standard_b(s, source=source, enabled=True, mode="LIVE")
    c.db.set_meta(META_FIRED_NIGHT, c.night)
    c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
           (time.time(), "strat_b.gta_kill", "gta", "default", source))
    why = kill_reason(c, pnl, base)
    if log:
        log.info(f"{iso()}\tgta kill\t{why}\tchanged={','.join(changed) or 'none'}")
    if notifier is not None:
        try:
            await notifier.push("Desk · " + why, kind="info")
        except Exception:
            pass
    return {"fired": True, "pnl": pnl, "baseline": base, "changed": changed, "reason": why}
