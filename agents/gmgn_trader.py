"""GMGN hands-off mode (separate from FOMO tap-to-trade; FOMO is unchanged).

PAPER: on a coin that passed the desk, size it, run every cap check, call the real quote when the plan allows it,
       record a simulated fill + the exact swap body / condition orders that LIVE would send. Never calls swap.
LIVE:  signed swap from the owner's main wallet ONLY, with server-side exits attached in the same request.
       With risk.grace_min > 0 (default 5), the first window uses loss_stop at risk.grace_stop_pct (default 30);
       the drawdown trail (loss_stop_trace) is skipped so it can't fire early. The take-profit trail is run by the
       desk (never by profit_stop_trace, which can sell below entry) and will not sell below entry + risk.trail_floor_pct
       (DECISION 80). After grace_min from the fill, those exits are swapped for the normal set (loss_stop at
       risk.stop_pct, loss_stop_trace) via GMGN's strategy API, still server-side. If the box was paused past
       the window, the swap runs on resume. With risk.grace_extend_below_stop (default on, DECISION 65), if the
       fast pool price is still below the normal stop but above the grace stop when the window ends, the swap
       waits until price has recovered; the -grace_stop_pct stop stays on in the meantime.
       Hard caps are enforced in code immediately before every signed buy. /halt stops all buys.
Missing strategy after a fill -> create a server-side stop_loss strategy -> if that fails, market-sell + alert.
The watcher (heartbeat 'gmgn') follows holdings + open strategies while the desk is awake, and promotes grace exits."""
from __future__ import annotations
import asyncio, json, math, time
import gmgn, chain
from gmgn import GmgnError, LAMPORTS, MAIN_WALLET, SOL
from ctx import iso

ACTIVE = ("submitting", "pending", "open", "exiting")          # count toward caps / block a 2nd buy
COUNTED = ACTIVE + ("closed", "unknown")                        # tickets used tonight
GATE_RULE = "LIVE gate: 1 full PAPER night -> 2h PAPER, user request 2026-11-14 5:42 PM CT"

PENDING_ADOPTS_META = "gmgn_pending_adopts"   # DECISION 78: one-shot break-even watches; survives restarts
PENDING_ADOPT_POLL_S = 10.0                    # price check cadence while a pending adopt is armed




class CapError(RuntimeError):
    pass


def grace_active(s) -> bool:
    """True when new buys get a first-window looser hard stop (risk.grace_min > 0)."""
    return float(s["risk.grace_min"] or 0) > 0


def grace_phase(s, o=None) -> str:
    """'grace' or 'normal' for a new buy (o is None) or an existing order."""
    if o is not None:
        return o.get("exits_phase") or "normal"
    return "grace" if grace_active(s) else "normal"


# After an extension, swap only once price is this far above the normal stop, for this many ticks
# at least GRACE_RECOVER_TICK_GAP_S apart (DECISION 65). Avoids attaching -stop_pct and having it fire
# on the same dip. Example with stop 20%: recover at >= entry x 0.81 (-19%) for 2 ticks.
GRACE_RECOVER_EXTRA_PCT = 1.0
GRACE_RECOVER_TICKS = 2
GRACE_RECOVER_TICK_GAP_S = 1.0


def _grace_until(s, o, now):
    until = o.get("grace_until")
    if until is None:
        until = (o.get("ts") or now) + float(s["risk.grace_min"]) * 60
    return until


def grace_window_ended(s, o, now) -> bool:
    """True when an order is still in the grace phase and the grace_min window has elapsed,
    or grace was turned off after the buy. Existing orders with no phase are left alone."""
    if (o.get("exits_phase") or "") != "grace":
        return False
    if not grace_active(s):
        return True
    return now >= _grace_until(s, o, now)


def grace_recover_price(s, o) -> float:
    """Price that counts as recovered enough to attach the normal stop (1% above it, DECISION 65)."""
    return float(o["entry_price"]) * (1 - float(s["risk.stop_pct"]) / 100.0 + GRACE_RECOVER_EXTRA_PCT / 100.0)


def grace_is_extended(s, o, now) -> bool:
    """Still holding the steep grace stop after the window, waiting for price to recover."""
    return (bool(s["risk.grace_extend_below_stop"]) and grace_active(s)
            and (o.get("exits_phase") or "") == "grace" and now >= _grace_until(s, o, now))


def grace_should_extend(s, o, now, price=None) -> bool:
    """Keep the grace stop: window ended, extend is on, and price is missing or still below the recover level."""
    if not s["risk.grace_extend_below_stop"] or not grace_active(s):
        return False
    if (o.get("exits_phase") or "") != "grace" or now < _grace_until(s, o, now):
        return False
    if not o.get("entry_price") or price is None:
        return True
    return float(price) < grace_recover_price(s, o)


def grace_due(s, o, now, price=None, *, extended=False, recover_streak=0) -> bool:
    """True when an order in the grace phase should switch to normal exits.
    Also true if grace was turned off after the buy. Existing orders with no phase are left alone.
    When risk.grace_extend_below_stop is on, a missing price or a price still below the recover
    threshold (normal stop + 1%) keeps grace. After an extension, two consecutive recovered ticks
    (DECISION 65) are required so the new stop does not fire on the same dip."""
    if (o.get("exits_phase") or "") != "grace":
        return False
    if not grace_active(s):
        return True
    if now < _grace_until(s, o, now):
        return False
    if grace_should_extend(s, o, now, price):
        return False
    if extended:
        return recover_streak + 1 >= GRACE_RECOVER_TICKS
    return True


def conditions(s, phase=None) -> list[dict]:
    """Server-side exits. phase='grace': loss_stop at grace_stop_pct; the drawdown trail
    (loss_stop_trace) is skipped so a 10% peak-to-trough dip can't end grace early.
    phase='normal': loss_stop at stop_pct + loss_stop_trace.
    The take-profit trail is not sent as profit_stop_trace (that order can sell below entry);
    the desk trail runs it with risk.trail_floor_pct so it never sells below entry + the floor
    (DECISION 80). phase=None means whatever a new buy would attach."""
    if phase is None:
        phase = "grace" if grace_active(s) else "normal"
    stop = s["risk.grace_stop_pct"] if phase == "grace" else s["risk.stop_pct"]
    out = [{"order_type": "loss_stop", "side": "sell", "price_scale": f"{stop:g}", "sell_ratio": "100"}]
    if phase != "grace":
        out.append({"order_type": "loss_stop_trace", "side": "sell", "sell_ratio": "100",
                    "drawdown_rate": f"{s['risk.trail_pct']:g}"})
    return out


def exits_label(s, phase=None, o=None, now=None) -> str:
    """Short line for Telegram and the app: 'stop -30% for first 5 min, then -15%' during grace;
    'grace extended · stop -50% until back above -20%' after the window while still waiting."""
    if o is not None and now is not None and grace_is_extended(s, o, now):
        return (f"grace extended · stop -{s['risk.grace_stop_pct']:g}% until back above "
                f"-{s['risk.stop_pct']:g}%")
    if phase is None:
        phase = "grace" if grace_active(s) else "normal"
    if phase == "grace" and grace_active(s):
        return (f"stop -{s['risk.grace_stop_pct']:g}% for first {s['risk.grace_min']:g} min, "
                f"then -{s['risk.stop_pct']:g}%")
    return f"stop -{s['risk.stop_pct']:g}% · trail {s['risk.trail_pct']:g}%"


def exits_text(s, phase=None) -> str:
    """Full exits sentence for buy pushes."""
    if phase is None:
        phase = "grace" if grace_active(s) else "normal"
    trail = (f"take-profit trail from +{s['risk.trail_start_pct']:g}%, "
             f"never below +{s['risk.trail_floor_pct']:g}% net after fees (desk)")
    if phase == "grace" and grace_active(s):
        return f"{exits_label(s, 'grace')} · trail {s['risk.trail_pct']:g}% after grace · {trail}"
    return f"stop -{s['risk.stop_pct']:g}% · trail {s['risk.trail_pct']:g}% · {trail}"


def grace_fields(s, now) -> dict:
    """exits_phase / grace_until stored at fill time. Existing open rows stay NULL and are never promoted."""
    if grace_active(s):
        return {"exits_phase": "grace", "grace_until": now + float(s["risk.grace_min"]) * 60}
    return {"exits_phase": "normal", "grace_until": None}


def buy_body(s, mint: str, lamports: int) -> dict:
    return {"chain": gmgn.CHAIN, "from_address": MAIN_WALLET, "input_token": SOL, "output_token": mint,
            "input_amount": str(int(lamports)), "slippage": int(s["gmgn.slippage_pct"]), "is_anti_mev": True,
            "priority_fee": f"{s['gmgn.priority_fee_sol']:g}", "tip_fee": f"{s['gmgn.tip_fee_sol']:g}",
            "condition_orders": conditions(s), "sell_ratio_type": "hold_amount"}


def sell_all_body(s, mint: str) -> dict:
    return {"chain": gmgn.CHAIN, "from_address": MAIN_WALLET, "input_token": mint, "output_token": SOL,
            "input_amount": "0", "input_amount_bps": "10000", "slippage": max(int(s["gmgn.slippage_pct"]), 25),
            "is_anti_mev": True, "priority_fee": f"{s['gmgn.priority_fee_sol']:g}", "tip_fee": f"{s['gmgn.tip_fee_sol']:g}"}


def stop_strategy_body(s, mint: str, fill_price_usd: float, sol_usd: float, amount_in: int | None = None, stop_pct=None) -> dict:
    """Repair: a server-side limit stop_loss for 100% of the position. check_price is in USD (DECISION 52: GMGN
    compares limit-order check_price against the USD price; the old SOL-denominated value sat ~99% below entry,
    so those stops never fired). sol_usd is kept for call compatibility and not used.
    amount_in = the raw token amount actually filled/held (GMGN rejects the order with 'amount_in must be greater
    than zero' before the fill has landed; see DECISION 39)."""
    pct = s["risk.stop_pct"] if stop_pct is None else stop_pct
    check = fill_price_usd * (1 - pct / 100.0)
    extra = {"amount_in": str(int(amount_in))} if amount_in else {}
    return {**extra, "chain": gmgn.CHAIN, "from_address": MAIN_WALLET, "base_token": mint, "quote_token": SOL,
            "order_type": "limit_order", "sub_order_type": "stop_loss", "check_price": f"{check:.12g}",
            "amount_in_percent": "100", "slippage": max(int(s["gmgn.slippage_pct"]), 25), "is_anti_mev": True,
            "priority_fee": f"{s['gmgn.priority_fee_sol']:g}", "tip_fee": f"{s['gmgn.tip_fee_sol']:g}"}


def fee_lamports(s) -> int:
    return int(round((s["gmgn.priority_fee_sol"] + s["gmgn.tip_fee_sol"]) * LAMPORTS))


RENT_LAMPORTS = 2_100_000        # new token account rent (~0.00204 SOL), paid by the buy tx


def buy_cost_lamports(s, lamports: int) -> int:
    """Everything a buy takes from the wallet besides the reserve: input + GMGN fee (on top) + priority/tip + rent.
    The Same Coin: input 0.660547907 SOL, wallet paid 0.669872226 (1% fee 0.0066 + fees 0.0012 + rent 0.0015)."""
    return int(lamports) + int(math.ceil(int(lamports) * s["gmgn.fee_pct"] / 100.0)) + fee_lamports(s) + RENT_LAMPORTS


def _b_live(db, sql, args=()):
    """Strategy B LIVE rows in strat_b_orders (DECISION 66). Same wallet, so LIVE caps / committed SOL are global."""
    try:
        return db.one(sql, args)
    except Exception:                                    # very old DB without the B LIVE columns
        return None


def open_count(db, mode: str, strategy: str | None = None) -> int:
    """GMGN positions held or being bought right now (pending + open + exiting).
    strategy=None (old global view): LIVE includes Strategy B LIVE. strategy='A': gmgn_orders only.
    strategy='B': Strategy B LIVE rows only (DECISION 71: each strategy has its own LIVE slots)."""
    ph = ','.join('?' * len(ACTIVE))
    n = 0
    if strategy in (None, "A"):
        n += int(db.one(f"SELECT COUNT(*) n FROM gmgn_orders WHERE mode=? AND status IN ({ph})", (mode, *ACTIVE))["n"])
    if mode == "LIVE" and strategy in (None, "B"):
        r = _b_live(db, f"SELECT COUNT(*) n FROM strat_b_orders WHERE mode='LIVE' AND status IN ({ph})", ACTIVE)
        n += int((r or {}).get("n") or 0)
    return n


def committed_lamports(db, mode: str) -> int:
    """SOL promised to buys still in flight (submitting/pending): the wallet read may not show them yet."""
    n = int(db.one("SELECT COALESCE(SUM(lamports),0) l FROM gmgn_orders WHERE mode=? AND status IN ('submitting','pending')", (mode,))["l"] or 0)
    if mode == "LIVE":
        r = _b_live(db, "SELECT COALESCE(SUM(lamports),0) l FROM strat_b_orders WHERE mode='LIVE' AND status IN ('submitting','pending')")
        n += int((r or {}).get("l") or 0)
    return n


def free_lamports(s, wallet_lamports, committed: int) -> int | None:
    if wallet_lamports is None:
        return None
    return int(wallet_lamports) - int(s["gmgn.sol_reserve"] * LAMPORTS) - int(committed)


def max_affordable_lamports(s, free: int) -> int:
    """Largest swap input whose full cost (input + 1% GMGN fee + priority/tip + rent) fits in free SOL."""
    pct = s["gmgn.fee_pct"] / 100.0
    lam = int(math.floor((free - fee_lamports(s) - RENT_LAMPORTS) / (1 + pct)))
    while lam > 0 and buy_cost_lamports(s, lam) > free:
        lam -= 1
    return max(lam, 0)


def pick_size(s, free: int | None, sol_usd: float, pool_cap_usd: float | None = None, top_usd: float | None = None):
    """Adaptive LIVE size (owner 2026-11-15 8:33 PM + 8:40 PM CT): the top size (gmgn.cap_trade_usd, $50) when free SOL
    covers it with fees, else the fallback (gmgn.fallback_trade_usd, $20), else the largest amount free SOL covers,
    down to the floor gmgn.min_trade_usd ($1). Returns (size_usd, lamports, reason).
    top_usd: Strategy B LIVE passes strat_b.trade_usd (never above the per-trade cap) and then follows A's same
    fallback / shrink / $1 floor (owner 2026-11-17 8:15 AM CT, DECISION 66)."""
    top, mid, low = float(s["gmgn.cap_trade_usd"]), float(s["gmgn.fallback_trade_usd"]), float(s["gmgn.min_trade_usd"])
    if top_usd is not None:
        top = min(float(top_usd), top)
    ceiling = top if pool_cap_usd is None else min(top, pool_cap_usd)
    if ceiling < low:
        return None, 0, f"pool too thin: max ${ceiling:,.2f} per trade is under ${low:,.2f}"
    if free is None:
        return None, 0, "wallet balance unknown"
    if not sol_usd:
        return None, 0, "no SOL price"
    for t in [x for x in (top, mid) if x <= ceiling + 1e-9]:
        lam = int(math.floor(t / sol_usd * LAMPORTS))
        if buy_cost_lamports(s, lam) <= free:
            return t, lam, None
    aff = max_affordable_lamports(s, max(free, 0))
    usd = math.floor(min(aff / LAMPORTS * sol_usd, ceiling) * 100) / 100.0           # whole cents, rounded down
    if aff >= int(math.floor(low / sol_usd * LAMPORTS)):
        usd = max(usd, low)                                                           # float dust at exactly the floor
    if usd >= low:
        return usd, min(int(math.floor(usd / sol_usd * LAMPORTS)), aff), None
    lam = int(math.floor(low / sol_usd * LAMPORTS))
    return None, 0, (f"free {max(free, 0) / LAMPORTS:.4f} SOL can't cover even ${low:,.2f} "
                     f"(needs {buy_cost_lamports(s, lam) / LAMPORTS:.4f} SOL with fees and rent, after the {s['gmgn.sol_reserve']:g} SOL reserve)")


# DECISION 68: Strategy A PAPER uses its own USD bankroll, never the real wallet or gmgn.cap_trade_usd.
PAPER_BANKROLL_META = "gmgn_paper_bankroll_since"


def paper_since_ts(db) -> float:
    try:
        return float(db.meta(PAPER_BANKROLL_META) or 0)
    except (TypeError, ValueError):
        return 0.0


def paper_realized_pnl(db, since: float | None = None) -> float:
    since = paper_since_ts(db) if since is None else since
    r = db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM gmgn_orders WHERE mode='PAPER' AND status='closed' AND ts>=?",
               (since,))
    return float(r["p"] or 0)


def paper_open_usd(db, since: float | None = None) -> float:
    since = paper_since_ts(db) if since is None else since
    ph = ",".join("?" * len(ACTIVE))
    r = db.one(f"SELECT COALESCE(SUM(size_usd),0) s FROM gmgn_orders WHERE mode='PAPER' AND status IN ({ph}) AND ts>=?",
               (*ACTIVE, since))
    return float(r["s"] or 0)


def paper_free_usd(s, db, since: float | None = None) -> float:
    """Bankroll + realized paper P&L since the reset, minus open paper tickets. Old rows before the baseline don't count."""
    since = paper_since_ts(db) if since is None else since
    return round(float(s["gmgn.paper_bankroll_usd"]) + paper_realized_pnl(db, since) - paper_open_usd(db, since), 6)


def paper_snapshot(s, db) -> dict:
    since = paper_since_ts(db)
    start = float(s["gmgn.paper_bankroll_usd"])
    realized = paper_realized_pnl(db, since)
    opened = paper_open_usd(db, since)
    return {"bankrollUsd": start, "tradeUsd": float(s["gmgn.paper_trade_usd"]),
            "pnlUsd": round(realized, 2), "inPositionsUsd": round(opened, 2),
            "freeUsd": round(start + realized - opened, 2), "since": since or None}


def pick_paper_size(s, free_usd: float, sol_usd: float, pool_cap_usd: float | None = None):
    """A PAPER size (DECISION 68): gmgn.paper_trade_usd ($500) when paper free covers it, else whatever is left
    down to gmgn.min_trade_usd, else refused. Never uses the wallet or gmgn.cap_trade_usd."""
    top, low = float(s["gmgn.paper_trade_usd"]), float(s["gmgn.min_trade_usd"])
    ceiling = top if pool_cap_usd is None else min(top, float(pool_cap_usd))
    if ceiling < low:
        return None, 0, f"pool too thin: max ${ceiling:,.2f} per trade is under ${low:,.2f}"
    if not sol_usd:
        return None, 0, "no SOL price"
    if free_usd is None:
        return None, 0, "paper balance unknown"
    if free_usd + 1e-9 >= ceiling:
        size = round(ceiling, 2)
    else:
        size = math.floor(max(free_usd, 0) * 100) / 100.0
    if size >= low:
        lam = int(math.floor(size / sol_usd * LAMPORTS))
        return size, lam, None
    return None, 0, (f"paper free ${max(free_usd, 0):,.2f} can't cover even ${low:,.2f} "
                     f"(bankroll ${s['gmgn.paper_bankroll_usd']:,.2f})")


def night_usage(db, night: str, mode: str, strategy: str | None = None) -> tuple[int, float]:
    """Tickets / dollars used tonight. strategy=None: LIVE counts A + B (old global view); 'A' or 'B': that one only."""
    ph = ','.join('?' * len(COUNTED))
    n, u = 0, 0.0
    if strategy in (None, "A"):
        r = db.one(f"SELECT COUNT(*) n, COALESCE(SUM(size_usd),0) u FROM gmgn_orders WHERE night=? AND mode=? AND status IN ({ph})",
                   (night, mode, *COUNTED))
        n, u = int(r["n"]), float(r["u"])
    if mode == "LIVE" and strategy in (None, "B"):
        b = _b_live(db, f"SELECT COUNT(*) n, COALESCE(SUM(size_usd),0) u FROM strat_b_orders WHERE night=? AND mode='LIVE' AND status IN ({ph})",
                    (night, *COUNTED))
        n, u = n + int((b or {}).get("n") or 0), u + float((b or {}).get("u") or 0)
    return n, u


# ---- DECISION 71: Strategy A LIVE and Strategy B LIVE share one wallet, each with its own share of it.
# Owner 2026-11-17 5:59 PM CT: "Split the wallet 50/50 and run both live."
#   free       = wallet SOL - gmgn.sol_reserve - SOL promised to in-flight buys (A + B, submitting/pending)
#   open(X)    = swap input (lamports) of strategy X's LIVE positions still held or being bought/sold
#   total      = free + open(A) + open(B)                (what the wallet is worth at cost basis)
#   budget(X)  = share(X)% x total - open(X)              (X's untouched part of its share)
#   avail(X)   = max(0, min(budget(X), free))             (never more than the SOL really on chain)
# pick_size sizes against avail(X) ($50, else $20, else down to $1), and check_caps refuses a buy whose full cost
# (input + GMGN fee + priority/tip + rent) is over avail(X). Cost basis, not mark-to-market: a losing position keeps
# its slot of the share until it is sold; the sale proceeds return to free SOL and are split again.
# Shares are gmgn.live_wallet_share_pct (A) and strat_b.live_wallet_share_pct (B). 100 = the whole wallet (old behaviour).
STRATEGY_SHARE_KEY = {"A": "gmgn.live_wallet_share_pct", "B": "strat_b.live_wallet_share_pct"}


def strategy_caps(s, strategy: str) -> tuple[int, int]:
    """(max open, tickets per night) for one strategy's LIVE buys."""
    if strategy == "B":
        return int(s["strat_b.live_max_open"]), int(s["strat_b.live_max_tickets_night"])
    return int(s["gmgn.max_open"]), int(s["gmgn.max_tickets_night"])


def open_cost_lamports(db, strategy: str) -> int:
    ph = ','.join('?' * len(ACTIVE))
    if strategy == "B":
        r = _b_live(db, f"SELECT COALESCE(SUM(lamports),0) l FROM strat_b_orders WHERE mode='LIVE' AND status IN ({ph})", ACTIVE)
        return int((r or {}).get("l") or 0)
    return int(db.one(f"SELECT COALESCE(SUM(lamports),0) l FROM gmgn_orders WHERE mode='LIVE' AND status IN ({ph})", ACTIVE)["l"] or 0)


def budget_lamports(s, db, strategy: str, wallet_lamports) -> dict | None:
    """The DECISION 71 numbers for one strategy, in lamports. None when the wallet balance is unknown."""
    free = free_lamports(s, wallet_lamports, committed_lamports(db, "LIVE"))
    if free is None:
        return None
    own, other = open_cost_lamports(db, strategy), open_cost_lamports(db, "B" if strategy == "A" else "A")
    share = float(s[STRATEGY_SHARE_KEY[strategy]])
    total = max(free, 0) + own + other
    budget = int(math.floor(total * share / 100.0)) - own
    return {"strategy": strategy, "sharePct": share, "free": free, "open": own, "otherOpen": other, "total": total,
            "budget": budget, "avail": max(0, min(budget, free))}


def strategy_free_lamports(s, db, strategy: str, wallet_lamports) -> int | None:
    b = budget_lamports(s, db, strategy, wallet_lamports)
    return None if b is None else b["avail"]


def budget_view(s, db, strategy: str, wallet_lamports, sol_usd) -> dict:
    """Public (app / Telegram) view of one strategy's share of the wallet. No secrets, no network call."""
    b = budget_lamports(s, db, strategy, wallet_lamports)
    mx, tickets = strategy_caps(s, strategy)
    out = {"strategy": strategy, "sharePct": float(s[STRATEGY_SHARE_KEY[strategy]]), "maxOpen": mx, "ticketsMax": tickets,
           "openCount": open_count(db, "LIVE", strategy), "budgetSol": None, "budgetUsd": None, "inPositionsSol": None,
           "inPositionsUsd": None, "shareSol": None, "shareUsd": None}
    if b is None:
        return out
    usd = (lambda lam: round(lam / LAMPORTS * sol_usd, 2)) if sol_usd else (lambda lam: None)
    out.update(budgetSol=round(b["avail"] / LAMPORTS, 6), budgetUsd=usd(b["avail"]),
               inPositionsSol=round(b["open"] / LAMPORTS, 6), inPositionsUsd=usd(b["open"]),
               shareSol=round(b["total"] * b["sharePct"] / 100.0 / LAMPORTS, 6), shareUsd=usd(int(b["total"] * b["sharePct"] / 100.0)))
    return out


def mint_held_live(db, mint: str) -> str | None:
    """'A' or 'B' when that strategy holds (or is buying / selling) this mint LIVE, else None. One coin, one strategy:
    both sell the whole wallet balance of a mint, so two strategies must never hold the same coin (DECISION 71)."""
    ph = ','.join('?' * len(ACTIVE))
    if db.one(f"SELECT id FROM gmgn_orders WHERE mint=? AND mode='LIVE' AND status IN ({ph})", (mint, *ACTIVE)):
        return "A"
    if _b_live(db, f"SELECT id FROM strat_b_orders WHERE mint=? AND mode='LIVE' AND status IN ({ph})", (mint, *ACTIVE)):
        return "B"
    return None


def check_caps(s, db, night, mode, size_usd, lamports, sol_usd, wallet_lamports, strategy: str | None = None) -> None:
    """Hard caps. Raises CapError with a plain reason. Called before the order row exists.
    strategy='A'|'B' (DECISION 71): that strategy's own open slots / night tickets, and its share of the wallet."""
    cap = float(s["gmgn.cap_trade_usd"])
    if strategy is None:
        mx, tickets = int(s["gmgn.max_open"]), int(s["gmgn.max_tickets_night"])
    else:
        mx, tickets = strategy_caps(s, strategy)
    night_cap = cap * tickets
    who = "GMGN" if strategy is None else f"{strategy} LIVE"
    if not (size_usd > 0) or not (sol_usd and sol_usd > 0) or lamports <= 0:
        raise CapError("bad size or SOL price")
    if size_usd > cap + 1e-9:
        raise CapError(f"${size_usd:,.2f} is over the per-trade cap ${cap:,.2f}")
    spend_usd = lamports / LAMPORTS * sol_usd
    if spend_usd > cap * 1.0001:
        raise CapError(f"{lamports / LAMPORTS:.4f} SOL (${spend_usd:,.2f}) is over the per-trade cap ${cap:,.2f}")
    op = open_count(db, mode, strategy)
    if op >= mx:
        raise CapError(f"{op}/{mx} {who} positions already open (max {mx} at once)")
    n, used = night_usage(db, night, mode, strategy)
    if n >= tickets:
        raise CapError(f"night ticket cap reached ({n}/{tickets})")
    if used + size_usd > night_cap + 1e-9:
        raise CapError(f"night spend cap ${night_cap:,.2f} (${used:,.2f} used, +${size_usd:,.2f} would exceed it)")
    if wallet_lamports is None:
        raise CapError("wallet balance unknown")
    need = buy_cost_lamports(s, lamports) + int(s["gmgn.sol_reserve"] * LAMPORTS) + committed_lamports(db, mode)
    if need > wallet_lamports:
        raise CapError(f"wallet has {wallet_lamports / LAMPORTS:.4f} SOL, needs {need / LAMPORTS:.4f} SOL "
                       f"(ticket + fees + {s['gmgn.sol_reserve']:g} SOL reserve)")
    if strategy is not None and mode == "LIVE":
        b = budget_lamports(s, db, strategy, wallet_lamports)
        cost = buy_cost_lamports(s, lamports)
        if b is None or cost > b["avail"]:
            av = 0 if b is None else b["avail"]
            raise CapError(f"{strategy}'s {s[STRATEGY_SHARE_KEY[strategy]]:g}% share has {av / LAMPORTS:.4f} SOL free "
                           f"(${av / LAMPORTS * sol_usd:,.2f}), this buy needs {cost / LAMPORTS:.4f} SOL with fees")


def check_paper_caps(s, db, night, size_usd) -> None:
    """A PAPER-only caps. Open and night tickets count only gmgn_orders mode=PAPER, so they never take
    Strategy B LIVE slots. No per-trade $50 cap, no night dollar cap, no wallet check (DECISION 68)."""
    top, low = float(s["gmgn.paper_trade_usd"]), float(s["gmgn.min_trade_usd"])
    if not (size_usd > 0):
        raise CapError("bad size or SOL price")
    if size_usd > top + 1e-9:
        raise CapError(f"${size_usd:,.2f} is over the paper trade size ${top:,.2f}")
    mx, op = int(s["gmgn.max_open"]), open_count(db, "PAPER")
    if op >= mx:
        raise CapError(f"{op}/{mx} GMGN positions already open (max {mx} at once)")
    tickets = int(s["gmgn.max_tickets_night"])
    n, _used = night_usage(db, night, "PAPER")
    if n >= tickets:
        raise CapError(f"night ticket cap reached ({n}/{tickets})")
    if paper_free_usd(s, db) + 1e-9 < size_usd:
        raise CapError(f"paper free ${paper_free_usd(s, db):,.2f} can't cover ${size_usd:,.2f}")


def sim_exit(s, o, price, now, below_floor=False):
    """PAPER mirror of the server-side exits + the desk timeout. Returns (reason|None, peak).
    During grace: only the steep hard stop and the take-profit trail; no drawdown trail.
    The take-profit trail never sells when expected net SOL is under the floor (DECISION 81)."""
    entry = o["entry_price"]; peak = max(o["peak"] or entry, price)
    phase = "grace" if (o.get("exits_phase") or "") == "grace" and not grace_due(s, o, now, price) else "normal"
    stop = s["risk.grace_stop_pct"] if phase == "grace" else s["risk.stop_pct"]
    if price <= entry * (1 - stop / 100.0):
        return "loss_stop", peak
    if trail_should_sell(s, o, price, peak, below_floor=below_floor):
        return "profit_stop_trace", peak
    if phase != "grace" and price <= peak * (1 - s["risk.trail_pct"] / 100.0):
        return "loss_stop_trace", peak
    if now - o["ts"] >= s["risk.timeout_h"] * 3600 and price < entry * (1 + s["risk.timeout_min_gain_pct"] / 100.0):
        return "timeout", peak
    return None, peak


# GMGN strategy condition that fired -> exit_reason for a LIVE close (DECISION 42)
LIVE_EXIT = {"loss_stop": "server-side stop", "loss_stop_trace": "server-side trailing stop",
             "profit_stop_trace": "server-side trail (take profit)", "profit_stop": "server-side take profit"}
REENTRY_REASON = "re-entry"
DUST_DIV = 1000                  # <= 0.1% of the tokens bought counts as empty

EXIT_LABEL = {"loss_stop": "hard stop", "loss_stop_trace": "trailing stop", "profit_stop_trace": "trailing take-profit",
              "timeout": "timeout", "pulled": "liquidity pulled", "server": "server-side exit", "manual": "sold",
              "surge top": "surge top", "surge floor": "surge profit floor", "desk trail": "trailing take-profit (desk)", "desk stop": "hard stop (desk)"}


# ---- desk-side trailing take-profit for positions whose GMGN exit is a plain limit stop (DECISION 52)
DESK_TRAIL = "desk trail"
DESK_STOP = "desk stop"
LIMIT_STOP_MARK = "limit_stop"   # repair note: the live GMGN exit is a limit_order stop_loss (no server-side trail)
TRAIL_POLL_S = 3.0
TRAIL_RETRY_S = 15.0


def limit_stop_only(o) -> bool:
    """True when this order's live GMGN exit is a limit_order stop only (adopted app buy, a repair stop, or the
    post-grace fallback after GMGN refused the smart_trade), so nothing server-side takes profit."""
    rep_ = o.get("repair") or ""
    if "stop_loss strategy created" in rep_ or LIMIT_STOP_MARK in rep_:
        return True
    try:
        conds = json.loads(o.get("conditions") or "[]")
    except Exception:
        conds = []
    return bool(conds) and not any((c or {}).get("order_type") == "profit_stop_trace" for c in conds)


def trail_floor_price(s, entry) -> float:
    """Mark-price floor only (no fees): entry * (1 + risk.trail_floor_pct/100). Prefer trail_net_floor_price."""
    return float(entry) * (1 + float(s["risk.trail_floor_pct"] or 0) / 100.0)


def trail_trigger(s, entry, peak, floor=None) -> float:
    """Armed-trail sell price: trail_pct below the peak, but never below `floor` (net floor if provided)."""
    raw = float(peak) * (1 - float(s["risk.trail_pct"]) / 100.0)
    fl = trail_floor_price(s, entry) if floor is None else float(floor)
    return max(raw, fl)


def _trail_audit(o) -> dict:
    raw = o.get("audit") if o else None
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {}


def _trail_adopted(o) -> bool:
    if "adopted" in (o.get("reason") or "") or "adopted" in (o.get("repair") or ""):
        return True
    return bool(_trail_audit(o).get("adopted"))


def _trail_chain_basis(o) -> bool:
    """True when entry_price is a cost basis (SOL spent / tokens), so buy fees are already in it."""
    return (o.get("entry_src") == "chain") or _trail_adopted(o)


def trail_sol_in(s, o) -> float | None:
    """SOL spent on the buy, fees and rent included. None if we cannot estimate."""
    au = _trail_audit(o)
    try:
        if au.get("sol_lamports"):
            return int(au["sol_lamports"]) / LAMPORTS
    except (TypeError, ValueError):
        pass
    try:
        lam = int(o.get("lamports") or 0)
    except (TypeError, ValueError):
        lam = 0
    if lam <= 0:
        return None
    if _trail_adopted(o):
        return lam / LAMPORTS          # adopted lamports is the wallet's full debit
    return buy_cost_lamports(s, lam) / LAMPORTS


def trail_expected_sol_out(s, o, price, quote_sol_out=None) -> float | None:
    """Expected SOL received from selling the whole position at `price`, or from a GMGN sell quote.
    Quote output_amount (in SOL) is preferred because it includes impact. Without a quote: mark
    proceeds minus gmgn.fee_pct and the sell priority+tip. Does not assume the 25% max sell
    slippage (that is only the tx limit). None if we cannot estimate."""
    if quote_sol_out is not None:
        q = float(quote_sol_out)
        return q if q > 0 else None
    entry = o.get("entry_price")
    if not entry or entry <= 0 or not price or price <= 0:
        return None
    fee = float(s["gmgn.fee_pct"] or 0) / 100.0
    tip = fee_lamports(s) / LAMPORTS
    if _trail_chain_basis(o):
        sol_in = trail_sol_in(s, o)
        if not sol_in:
            return None
        return sol_in * (float(price) / float(entry)) * (1 - fee) - tip
    try:
        input_sol = int(o.get("lamports") or 0) / LAMPORTS
    except (TypeError, ValueError):
        input_sol = 0
    if input_sol > 0 and not _trail_adopted(o):
        return input_sol * (float(price) / float(entry)) * (1 - fee) - tip
    return None


def trail_net_ok(s, o, price, quote_sol_out=None) -> bool:
    """True when expected sol_out / sol_in is at least 1 + trail_floor_pct/100 (DECISION 81)."""
    floor = 1 + float(s["risk.trail_floor_pct"] or 0) / 100.0
    sol_in = trail_sol_in(s, o)
    sol_out = trail_expected_sol_out(s, o, price, quote_sol_out=quote_sol_out)
    if sol_in and sol_out is not None:
        return sol_out + 1e-12 >= sol_in * floor
    entry = o.get("entry_price")
    if not entry or entry <= 0 or not price or price <= 0:
        return False
    fee = float(s["gmgn.fee_pct"] or 0) / 100.0
    if fee >= 1:
        return False
    if _trail_chain_basis(o):
        return (float(price) / float(entry)) * (1 - fee) + 1e-12 >= floor
    # no size: buy + sell fee_pct, same shape as paper _pnl (tips unknown)
    return (float(price) / float(entry)) * (1 - fee) - fee + 1e-12 >= floor


def trail_net_floor_price(s, o) -> float | None:
    """Lowest mark price at which the estimated net meets the floor. None if not computable."""
    entry = o.get("entry_price")
    if not entry or entry <= 0:
        return None
    floor = 1 + float(s["risk.trail_floor_pct"] or 0) / 100.0
    fee = float(s["gmgn.fee_pct"] or 0) / 100.0
    if fee >= 1:
        return None
    tip = fee_lamports(s) / LAMPORTS
    sol_in = trail_sol_in(s, o)
    if _trail_chain_basis(o):
        tip_frac = (tip / sol_in) if sol_in else 0.0
        return float(entry) * (floor + tip_frac) / (1 - fee)
    try:
        input_sol = int(o.get("lamports") or 0) / LAMPORTS
    except (TypeError, ValueError):
        input_sol = 0
    if input_sol > 0 and sol_in and not _trail_adopted(o):
        return float(entry) * (floor * sol_in + tip) / (input_sol * (1 - fee))
    return float(entry) * (floor + fee) / (1 - fee)


def trail_should_sell(s, o, price, peak, below_floor=False, quote_sol_out=None) -> bool:
    """Armed-trail sell decision (DECISION 81). Never sells the take when expected net is under the
    floor. A poll below the net floor does not sell; the trail stays armed with its peak. It sells
    later when price is in [net floor, trigger], or when price has recovered to the net floor after
    a gap (bounce exit) and the trigger is the floor itself. A strong bounce back above a higher
    trail line keeps trailing. quote_sol_out, when given, is the source of truth for the net."""
    entry = o.get("entry_price")
    if not entry or entry <= 0 or not price or price <= 0 or not peak:
        return False
    if float(peak) < float(entry) * (1 + float(s["risk.trail_start_pct"]) / 100.0):
        return False
    floor_px = trail_net_floor_price(s, o)
    if floor_px is None:
        return False
    if quote_sol_out is None and price < floor_px:
        return False
    if not trail_net_ok(s, o, price, quote_sol_out=quote_sol_out):
        return False
    trigger = trail_trigger(s, entry, peak, floor=floor_px)
    if price <= trigger:
        return True
    if below_floor and trigger <= floor_px * (1 + 1e-12):
        return True
    return False


def has_profit_stop_trace(o) -> bool:
    """True when the stored condition set still includes a server-side take-profit trail."""
    try:
        conds = json.loads(o.get("conditions") or "[]")
    except Exception:
        return False
    return any((c or {}).get("order_type") == "profit_stop_trace" for c in conds)


def desk_trail_check(s, o, price, now, peak=None, below_floor=False, quote_sol_out=None):
    """One price sample for an open LIVE A position. Returns (reason|None, peak, armed).
    Peak = highest price seen since the buy. Armed once peak >= entry * (1 + risk.trail_start_pct).
    The take-profit trail sells only when expected net SOL (after sell fee, priority/tip, and a
    GMGN quote's impact when provided) is at least +trail_floor_pct vs SOL spent (DECISION 81).
    A poll under the net floor does not sell; peak and armed stay. Backup hard stop at -stop_pct
    (-grace_stop_pct inside grace) is unchanged and can still sell at a loss. All values live."""
    entry = o["entry_price"]
    peak = max(peak if peak is not None else (o.get("peak") or entry), price)
    in_grace = (o.get("exits_phase") or "") == "grace" and not grace_due(s, o, now, price)
    stop = float(s["risk.grace_stop_pct"] if in_grace else s["risk.stop_pct"])
    armed = peak >= entry * (1 + float(s["risk.trail_start_pct"]) / 100.0)
    if price <= entry * (1 - stop / 100.0):
        return DESK_STOP, peak, armed
    if trail_should_sell(s, o, price, peak, below_floor=below_floor, quote_sol_out=quote_sol_out):
        return DESK_TRAIL, peak, armed
    return None, peak, armed


# ---- surge-top sell (DECISION 49)
SURGE_TOP = "surge top"
SURGE_FLOOR = "surge floor"      # surge profit floor (DECISION 56)
SURGE_POLL_S = 3.0               # price check on open positions (one DexScreener call for all open mints)
SURGE_FAST_PCT = 20.0            # ...or +20% inside any 60s window
SURGE_FAST_S = 60.0
SURGE_RETRY_S = 15.0             # after a failed surge sell, wait before trying again


def surge_check(s, st: dict, now: float, price: float, entry: float):
    """Feed one price sample into a position's surge state `st` (mutated in place).
    st = {'hist': [(ts, price)...], 'armed_at': ts|None, 'peak': float|None, 'gain': float|None}.
    Surge = price at least risk.surge_min_entry_pct above entry (DECISION 61; 0 = just above entry) AND either
    up >= risk.surge_min_pct from the lowest price in the last risk.surge_window_s, or up >= SURGE_FAST_PCT from
    the lowest price in the last SURGE_FAST_S.
    Once armed, track the post-surge peak; top = price falls risk.surge_top_drop_pct from that peak.
    Profit floor (DECISION 56): once armed, price <= entry * (1 + risk.surge_floor_pct) also sells; whichever
    of floor / top comes first wins. If price is already below entry when either would fire, the surge rule
    stands down ('missed') and the normal stop / trail handle the coin, the same as a non-surged one.
    Returns 'armed' on the tick a surge is first seen, 'floor' / 'top' when it should sell, 'missed' when it
    would have sold but price is under entry, else None."""
    win = float(s["risk.surge_window_s"])
    hist = st.setdefault("hist", [])
    hist.append((now, price))
    keep = max(win, SURGE_FAST_S) + 5
    while hist and now - hist[0][0] > keep:
        hist.pop(0)
    if st.get("armed_at") is None:
        if not entry or price <= entry:
            return None
        if price < entry * (1 + float(s["risk.surge_min_entry_pct"]) / 100.0):
            return None                 # not far enough above the buy price yet (DECISION 61)
        low_w = min(p for t, p in hist if now - t <= win)
        low_f = min(p for t, p in hist if now - t <= SURGE_FAST_S)
        g_w = 100 * (price / low_w - 1) if low_w > 0 else 0
        g_f = 100 * (price / low_f - 1) if low_f > 0 else 0
        if g_w >= float(s["risk.surge_min_pct"]) or g_f >= SURGE_FAST_PCT:
            st.update(armed_at=now, peak=price, gain=max(g_w, g_f))
            return "armed"
        return None
    st["peak"] = max(st.get("peak") or price, price)
    floor_pct = float(s["risk.surge_floor_pct"])
    hit_floor = floor_pct > 0 and bool(entry) and price <= entry * (1 + floor_pct / 100.0)
    hit_top = price <= st["peak"] * (1 - float(s["risk.surge_top_drop_pct"]) / 100.0)
    if not (hit_floor or hit_top):
        return None
    if entry and price < entry:
        return "missed"                 # missed both: leave it to the normal stop / trail
    return "floor" if hit_floor else "top"


class GmgnTrader:
    name = "gmgn"

    def __init__(self, ctx, client=None):
        self.c = ctx
        self.log = ctx.logs.get("gmgn") or ctx.logs["executor"]
        self.client = client
        self.lock = asyncio.Lock()
        self._sol = (0.0, None)          # (ts, usd)
        self._bal = (0.0, None)          # (ts, lamports)
        self._bal_stale = False          # DECISION 73: set on every LIVE fill / exit (A or B) so the next refresh re-reads at once
        self.plan_blocked_at = 0.0
        self._txc: dict = {}             # (sig, mint) -> parsed wallet delta (chain truth cache)
        self._surge: dict = {}           # order id -> surge_check state (in memory; rebuilt from entry after a restart)
        self._surge_retry: dict = {}     # order id -> earliest retry time after a failed surge sell
        self._trail_armed: set = set()   # order ids whose desk trail is armed (logged once)
        self._trail_seen: set = set()    # order ids the desk trail has started watching (logged once)
        self._trail_retry: dict = {}     # order id -> earliest retry after a failed desk-trail sell
        self._trail_peak: dict = {}      # order id -> trail peak (in memory; seeded from the row on first sight)
        self._strip_retry: dict = {}     # order id -> earliest retry after a failed profit_stop_trace strip
        self._trail_below_floor: set = set()  # armed ids last seen under the net floor (DECISION 81 bounce)
        self.fast = None                 # fastprice.FastPrice for LIVE exit watchers (DECISION 57), set by main.Desk
        self._settling: set = set()      # order ids with a settle/exit-attach in progress (watcher must not race it)
        self._grace_extended: set = set()  # order ids holding the grace stop past the window (DECISION 65)
        self._grace_recover: dict = {}     # order id -> consecutive recover ticks after an extension
        self._grace_tick_ts: dict = {}     # order id -> last recover-tick time (avoid double-counting two loops)
        if ctx.db.meta("gmgn_paper_since") is None:
            ctx.db.set_meta("gmgn_paper_since", ctx.clock())
            ctx.db.set_meta("gmgn_paper_since_note", "gmgn PAPER start")

    # ---- state
    @property
    def s(self):
        return self.c.s

    def mode(self):
        return self.s["gmgn.mode"]

    def prefix(self):
        return "PAPER · GMGN · " if self.mode() == "PAPER" else "GMGN · "

    def halted(self) -> bool:
        return bool(self.c.db.meta("gmgn_halted", False))

    def halt(self, on: bool, who="telegram"):
        self.c.db.set_meta("gmgn_halted", bool(on))
        self.c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
                    (time.time(), "gmgn.halted", json.dumps(not on), json.dumps(bool(on)), who))

    def paper_since(self):
        return self.c.db.meta("gmgn_paper_since")

    def _log(self, *parts):
        import secrets_store
        self.log.info(secrets_store.redact("\t".join([iso()] + [str(p) for p in parts])))

    async def push(self, text, kind="gmgn", mint=None):
        first = (text or "").split("\n", 1)[0]
        if mint and any(tag in first for tag in ("Bought ", "Buying ", "Buy ")):
            from agents.auditor import mint_l8_skipped, tag_l8_skip
            text = tag_l8_skip(text, mint_l8_skipped(self.c, mint))
        try:
            return await self.c.notifier.push(text, None, kind=kind, mint=mint)
        except Exception as e:
            self.c.error("gmgn", "push", str(e)[:200])
            return False

    # ---- market data
    async def sol_usd(self):
        ts, v = self._sol
        if v and time.time() - ts < 60:
            return v
        try:
            d = await self.client.gas_price()
            v = float(d.get("native_token_usd_price") or 0) or None
        except Exception as e:
            self.c.error("gmgn", "sol_usd", str(e)[:200]); v = None
        if v:
            self._sol = (time.time(), v)
        return v or self._sol[1]

    async def wallet_lamports(self, fresh=False):
        ts, v = self._bal
        if v is not None and not fresh and time.time() - ts < 30:
            return v
        try:
            d = await self.client.user_info()
            for w in d.get("wallets") or []:
                if w.get("chain") == "sol" and w.get("address") == MAIN_WALLET:
                    for b in w.get("balances") or []:
                        if b.get("token_address") in (SOL, "So11111111111111111111111111111111111111111", "") or b.get("symbol") == "SOL":
                            v = int(round(float(b.get("balance") or 0) * LAMPORTS))
                            break
        except Exception as e:
            self.c.error("gmgn", "balance", str(e)[:200]); v = None
        if v is None and getattr(self.c, "net", None) is not None:
            try:
                r = await self.c.net.rpc("getBalance", [MAIN_WALLET])
                v = int((r or {}).get("value"))
            except Exception:
                v = None
        if v is not None:
            self._bal = (time.time(), v)
            self._bal_stale = False
        return v

    def mark_wallet_stale(self):
        """A LIVE buy filled or a position closed: the cached balance is out of date (no network call here)."""
        self._bal_stale = True

    def wallet_snapshot(self):
        """Last cached wallet SOL + USD. Never calls the API. Used by the read-only publisher."""
        bal_ts, lamports = self._bal
        _sol_ts, sol = self._sol
        sol_amt = (lamports / LAMPORTS) if lamports is not None else None
        usd = round(sol_amt * sol, 2) if (sol_amt is not None and sol) else None
        return {
            "sol": round(sol_amt, 6) if sol_amt is not None else None,
            "usd": usd,
            "solUsd": sol,
            "updatedAt": iso(bal_ts) if (lamports is not None and bal_ts) else None,
        }

    async def refresh_wallet(self, min_age=60):
        """Refresh cached balance and SOL price at most once every min_age seconds."""
        if self.client is None:
            return self.wallet_snapshot()
        now = time.time()
        if self._sol[1] is None or now - self._sol[0] >= min_age:
            await self.sol_usd()
        if self._bal[1] is None or getattr(self, "_bal_stale", False) or now - self._bal[0] >= min_age:
            await self.wallet_lamports(fresh=True)
        return self.wallet_snapshot()

    def pool_cap_usd(self, liq):
        return None if liq is None else float(liq) * self.s["executor.max_pool_pct"] / 100.0

    def ticket_usd(self, liq):
        """Top GMGN size for this pool. LIVE: gmgn.cap_trade_usd ($50). PAPER: gmgn.paper_trade_usd ($500)
        (DECISION 68). Independent of the FOMO paper stack/min-ticket sizing (DECISION 44)."""
        top_key = "gmgn.paper_trade_usd" if self.mode() == "PAPER" else "gmgn.cap_trade_usd"
        top, low = float(self.s[top_key]), float(self.s["gmgn.min_trade_usd"])     # floor $1
        pc = self.pool_cap_usd(liq)
        if pc is not None and pc < low:
            return None, f"pool too thin: max ${pc:,.2f} per trade is under ${low:,.2f}"
        return round(min(top, pc) if pc is not None and pc < top else top, 2), None

    @staticmethod
    def _tag(o):
        return "RE-ENTRY · " if (o.get("reason") or "") == REENTRY_REASON else ""

    def open_label(self, mode=None):
        mode = mode or self.mode()
        return f"{open_count(self.c.db, mode, 'A')}/{int(self.s['gmgn.max_open'])} open"

    # ---- entry
    async def on_pass(self, mint, name, symbol, price, liq, score=None, reentry=None, *, mc=None, approved=False, from_dip=False):
        if not self.s["gmgn.enabled"] or self.client is None:
            return None
        async with self.lock:                          # one buy decision at a time: caps can't race
            try:
                return await self._on_pass(mint, name, symbol, price, liq, score, reentry, mc=mc, approved=approved, from_dip=from_dip)
            except Exception as e:
                self.c.error("gmgn", "on_pass", f"{mint} {str(e)[:300]}")
                return None

    # ---- market-cap approval for LIVE buys (DECISION 53)
    APPROVE_TTL_S = 10 * 60
    APPROVE_MAX_DROP_PCT = 20.0

    async def _fresh(self, mint):
        from agents.scanner import pick_pair
        import fields
        try:
            pair = pick_pair(await self.c.net.dex_tokens([mint]), mint)
        except Exception:
            pair = None
        if not pair or not fields.get(pair, "priceUsd"):
            return None
        g = lambda k: fields.get(pair, k)
        return {"price": float(g("priceUsd")), "liq": float(g("liquidity.usd") or 0), "mc": float(g("marketCap") or 0) or None,
                "name": g("baseToken.name"), "symbol": g("baseToken.symbol")}

    async def _ask_approval(self, mint, name, symbol, price, liq, score, reentry, mc, thr):
        c = self.c
        now = c.clock()
        pend = c.db.one("SELECT * FROM gmgn_approvals WHERE mint=? AND status='asked' AND expires>? ORDER BY id DESC LIMIT 1", (mint, now))
        if pend:
            return {"status": "approval", "reason": f"waiting for your approval (#{pend['id']})"}
        if reentry is None and c.db.one("SELECT id FROM gmgn_approvals WHERE mint=? AND night=? AND status IN ('skipped','expired')",
                                        (mint, c.night)):
            return None                                  # owner already skipped / let it expire tonight: don't ask again
        aid = c.db.x("INSERT INTO gmgn_approvals(ts,night,mint,name,symbol,mc,price,liq,score,reentry,status,expires) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (now, c.night, mint, name, symbol, mc, price, liq, score, json.dumps(reentry) if reentry else None,
                      "asked", now + self.APPROVE_TTL_S)).lastrowid
        import links
        url = links.coin_url(mint)
        mc_txt = f"${float(mc):,.0f}" if mc else "unknown"
        text = (f"GMGN · APPROVE? {name}{' (' + symbol + ')' if symbol else ''}{' · re-entry' if reentry else ''}\n"
                f"Market cap {mc_txt} is over your ${thr:,.0f} auto-buy limit, so I didn't buy it.\n"
                f"Price ${price:.8g} · liquidity ${float(liq or 0):,.0f}{' · score ' + str(score) if score is not None else ''}\n"
                f"{url}\n"
                f"Approve buys it at the normal size with the normal exits, after a fresh price check. "
                f"Expires in {self.APPROVE_TTL_S // 60} min. No buttons? Reply \"approve {aid}\" or \"skip {aid}\".")
        buttons = [[{"text": "Approve buy", "callback_data": f"gmap:{aid}"}, {"text": "Skip", "callback_data": f"gmsk:{aid}"}],
                   [{"text": "Open on FOMO", "url": url}]]
        try:
            await c.notifier.push(text, buttons, kind="gmgn_approve", mint=mint)
        except Exception as e:
            c.error("gmgn", "push", str(e)[:200])
        self._log("LIVE", mint, "approval asked", f"#{aid}", f"mc={mc_txt}", f"limit=${thr:,.0f}")
        return {"status": "approval", "reason": f"market cap {mc_txt} over ${thr:,.0f}: asked owner (#{aid})"}

    def _approval(self, aid):
        try:
            return self.c.db.one("SELECT * FROM gmgn_approvals WHERE id=?", (int(str(aid).lstrip("#")),))
        except (TypeError, ValueError):
            return None

    def _set_approval(self, aid, **kw):
        self.c.db.x(f"UPDATE gmgn_approvals SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), aid))

    async def skip_approval(self, aid):
        a = self._approval(aid)
        if not a:
            return f"No approval request #{aid}."
        if a["status"] != "asked":
            return f"{a['name']} (#{a['id']}) is already {a['status']}. Nothing bought."
        self._set_approval(a["id"], status="skipped", result="skipped by you")
        self._log("LIVE", a["mint"], "approval skipped", f"#{a['id']}")
        return f"Skipped {a['name']}. Nothing was bought."

    async def expire_approvals(self):
        now = self.c.clock()
        for a in self.c.db.rows("SELECT * FROM gmgn_approvals WHERE status='asked' AND expires<=?", (now,)):
            self._set_approval(a["id"], status="expired", result="no answer in 10 min")
            self._log("LIVE", a["mint"], "approval expired", f"#{a['id']}")
            await self.push(f"GMGN · {a['name']}: approval expired after {self.APPROVE_TTL_S // 60} min. Nothing was bought.",
                            kind="gmgn_approve", mint=a["mint"])

    async def approve(self, aid):
        c, s = self.c, self.s
        a = self._approval(aid)
        if not a:
            return f"No approval request #{aid}."
        if a["status"] != "asked":
            return f"{a['name']} (#{a['id']}) is already {a['status']}. Nothing new bought."
        if c.clock() > (a["expires"] or 0):
            self._set_approval(a["id"], status="expired", result="approve after 10 min")
            return f"The approval for {a['name']} expired (10 min). Nothing was bought."
        self._set_approval(a["id"], status="approved")  # spend it BEFORE anything can buy: a 2nd tap can't buy twice
        rid = a["id"]

        def refuse(why):
            self._set_approval(rid, status="refused", result=why)
            self._log("LIVE", a["mint"], "approval refused", f"#{rid}", why)
            return f"{a['name']}: not bought, {why}."

        if self.mode() != "LIVE" or not s["gmgn.enabled"] or self.client is None:
            return refuse("GMGN isn't LIVE right now")
        fr = await self._fresh(a["mint"])
        if fr is None:
            return refuse("no fresh price right now")
        floor = float(s["scanner.min_liquidity_usd"])
        if fr["liq"] < floor:
            return refuse(f"liquidity ${fr['liq']:,.0f} is under the ${floor:,.0f} floor")
        if a["price"]:
            drop = (1 - fr["price"] / a["price"]) * 100
            if drop > self.APPROVE_MAX_DROP_PCT:
                return refuse(f"price is down {drop:.0f}% since I asked (limit {self.APPROVE_MAX_DROP_PCT:g}%)")
        reentry = json.loads(a["reentry"]) if a["reentry"] else None
        self._log("LIVE", a["mint"], "approved", f"#{rid}", f"price={fr['price']:.10g}", f"mc={fr['mc']}")
        res = await self.on_pass(a["mint"], a["name"] or fr["name"], a["symbol"] or fr["symbol"], fr["price"], fr["liq"],
                                 a["score"], reentry, mc=fr["mc"], approved=True)
        st = (res or {}).get("status")
        if res is None:
            return refuse("GMGN skipped it (already bought or open tonight, or GMGN off)")
        if st == "refused":
            return refuse(res.get("reason") or "GMGN refused")
        mc_txt = f"${fr['mc']:,.0f}" if fr["mc"] else "unknown"
        if st == "dip":
            self._set_approval(rid, status="done", result=f"gmgn dip-watch · mc {mc_txt}")
            return (f"Approved {a['name']} · market cap now {mc_txt}. "
                    f"Watching for a {s['gmgn.dip_pct']:g}% dip (buy anyway in {int(s['gmgn.dip_timeout_s'])}s).")
        self._set_approval(rid, status="done", result=f"gmgn {st} · mc {mc_txt}")
        return f"Approved {a['name']} · market cap now {mc_txt} · GMGN {st}. The buy message has the details."

    def _row(self, **kw):
        cols = ",".join(kw); qs = ",".join("?" * len(kw))
        return self.c.db.x(f"INSERT INTO gmgn_orders({cols}) VALUES({qs})", tuple(kw.values())).lastrowid

    def _upd(self, oid, **kw):
        self.c.db.x(f"UPDATE gmgn_orders SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), oid))

    def _refuse(self, mint, name, mode, size, reason):
        self._row(ts=self.c.clock(), night=self.c.night, mode=mode, mint=mint, name=name, status="refused", reason=reason, size_usd=size or 0)
        self._log(mode, mint, "refused", reason)

    async def _on_pass(self, mint, name, symbol, price, liq, score, reentry=None, mc=None, approved=False, from_dip=False):
        c, s = self.c, self.s
        mode = self.mode()
        if c.db.one(f"SELECT id FROM gmgn_orders WHERE mint=? AND status IN ({','.join('?'*len(ACTIVE))})", (mint, *ACTIVE)):
            return None                                  # never a 2nd buy on an open one
        if mode == "LIVE" and _b_live(c.db, f"SELECT id FROM strat_b_orders WHERE mode='LIVE' AND mint=? AND status IN ({','.join('?'*len(ACTIVE))})", (mint, *ACTIVE)):
            return None                                  # Strategy B LIVE holds it in the same wallet (DECISION 66)
        if reentry is None and c.db.one("SELECT id FROM gmgn_orders WHERE mint=? AND night=? AND status NOT IN ('refused')", (mint, c.night)):
            return None                                  # one normal GMGN buy per coin per night
        if reentry is not None:                          # DECISION 46: re-entry after a LIVE exit, max N per coin per night
            used = c.db.one("SELECT COUNT(*) n FROM gmgn_orders WHERE mint=? AND night=? AND reason=? AND status NOT IN ('refused')",
                            (mint, c.night, REENTRY_REASON))["n"]
            mx = int(s["gmgn.reentry_max_per_coin"])
            if used >= mx:
                why = f"re-entry limit reached ({used}/{mx} tonight)"
                self._refuse(mint, name, mode, 0, why)
                return {"status": "refused", "reason": why}
        if self.halted():
            self._refuse(mint, name, mode, 0, "halted")
            return {"status": "refused", "reason": "halted"}
        import dip as _dip
        mx, op = int(s["gmgn.max_open"]), open_count(c.db, mode, "A") + (_dip.watching_count(c.db) if not from_dip else 0)
        if op >= mx:                                     # 5th buy: refused before anything else is spent
            why = f"{op}/{mx} GMGN positions already open (max {mx} at once)"
            self._refuse(mint, name, mode, 0, why)
            await self.push(f"{self.prefix()}Refused {name}: {why}", kind="gmgn_refused", mint=mint)
            return {"status": "refused", "reason": why}
        thr = float(s["gmgn.approve_above_mc_usd"] or 0)           # DECISION 53: big caps wait for the owner
        if mode == "LIVE" and thr > 0 and not approved and not from_dip:
            if mc is None:
                fr = await self._fresh(mint)
                mc = fr["mc"] if fr and fr["mc"] else None
            if mc is None or float(mc) > thr:
                return await self._ask_approval(mint, name, symbol, price, liq, score, reentry, mc, thr)
        # DECISION 64: after the approval gate, wait for a small dip (or 30s) before buying
        if s["gmgn.dip_enabled"] and not from_dip:
            return await _dip._watch(c, trader=self).start(
                mint, name, symbol, price, liq, score, reentry, want_gmgn=True, mc=mc)
        top, why = self.ticket_usd(liq)
        if top is None:
            self._refuse(mint, name, mode, 0, why)
            return {"status": "refused", "reason": why}
        sol_usd = await self.sol_usd()
        if not sol_usd:
            self._refuse(mint, name, mode, top, "no SOL price")
            await self.push(f"{self.prefix()}Skipped {name}: no SOL price from GMGN", mint=mint)
            return {"status": "refused", "reason": "no SOL price"}
        if mode == "PAPER":                              # DECISION 68: paper bankroll, never the real wallet or the $50 live cap
            size, lamports, why = pick_paper_size(s, paper_free_usd(s, c.db), sol_usd, self.pool_cap_usd(liq))
            if size is None:
                self._refuse(mint, name, mode, 0, why)
                await self.push(f"{self.prefix()}Skipped {name}: {why}", kind="gmgn_refused", mint=mint)
                return {"status": "refused", "reason": why}
            try:
                check_paper_caps(s, c.db, c.night, size)
            except CapError as e:
                self._refuse(mint, name, mode, size, str(e))
                await self.push(f"{self.prefix()}Refused {name}: {e}", kind="gmgn_refused", mint=mint)
                return {"status": "refused", "reason": str(e)}
            body = buy_body(s, mint, lamports)
            exits = exits_text(s)
            q, qnote = await self.try_quote(mint, lamports)
            oid = self._row(ts=c.clock(), night=c.night, mode="PAPER", mint=mint, name=name, symbol=symbol, status="open",
                            size_usd=size, sol_usd=sol_usd, lamports=lamports, entry_price=price, peak=price, last_price=price,
                            last_liq=liq, quote=json.dumps(q) if q else qnote, body=json.dumps(body),
                            conditions=json.dumps(body["condition_orders"]), **grace_fields(s, c.clock()))
            self._log("PAPER", mint, "sim-fill", f"${size:.2f}", f"{lamports}", f"price={price}", qnote)
            await self.push(f"{self.prefix()}Buy {name} · ${size:,.2f} ({lamports / LAMPORTS:.4f} SOL) · {self.open_label('PAPER')} · simulated fill\n{exits}\nQuote: {qnote}", mint=mint)
            c.event("fill", mint)
            return {"status": "paper", "id": oid, "body": body}
        wallet = await self.wallet_lamports(fresh=True)          # /v1/user/info before every LIVE sizing
        # DECISION 71: size against A's share of the wallet (never more than the free SOL on chain)
        size, lamports, why = pick_size(s, strategy_free_lamports(s, c.db, "A", wallet), sol_usd, self.pool_cap_usd(liq))
        if size is None:
            self._refuse(mint, name, mode, 0, why)
            await self.push(f"{self.prefix()}Skipped {name}: {why}", kind="gmgn_refused", mint=mint)
            return {"status": "refused", "reason": why}
        try:
            check_caps(s, c.db, c.night, mode, size, lamports, sol_usd, wallet, strategy="A")
        except CapError as e:
            self._refuse(mint, name, mode, size, str(e))
            await self.push(f"{self.prefix()}Refused {name}: {e}", kind="gmgn_refused", mint=mint)
            return {"status": "refused", "reason": str(e)}
        body = buy_body(s, mint, lamports)
        exits = exits_text(s)
        return await self.live_buy(mint, name, symbol, price, liq, size, sol_usd, lamports, body, exits, reentry=reentry)

    async def try_quote(self, mint, lamports):
        if time.time() - self.plan_blocked_at < 600:
            return None, "not available on your GMGN plan (free); simulated at the DexScreener price"
        try:
            q = await self.client.quote(SOL, mint, lamports, int(self.s["gmgn.slippage_pct"]))
            return q, f"GMGN quote out {q.get('output_amount')} (min {q.get('min_output_amount')})"
        except GmgnError as e:
            if e.plan_blocked:
                self.plan_blocked_at = time.time()
                return None, "not available on your GMGN plan (free); simulated at the DexScreener price"
            return None, f"quote failed ({e.error or e.status}); simulated at the DexScreener price"

    async def live_buy(self, mint, name, symbol, price, liq, size, sol_usd, lamports, body, exits, reentry=None):
        c, s = self.c, self.s
        # last line of defence, right before the signed swap
        if self.halted() or self.mode() != "LIVE" or not s["gmgn.enabled"]:
            self._refuse(mint, name, "LIVE", size, "halted or not LIVE at send time")
            return {"status": "refused", "reason": "halted"}
        wallet = await self.wallet_lamports(fresh=True)
        try:
            check_caps(s, c.db, c.night, "LIVE", size, lamports, sol_usd, wallet, strategy="A")
            held = mint_held_live(c.db, mint)                # sync with the row insert below: B can't slip in between
            if held:
                raise CapError(f"already held by {held} LIVE in the wallet")
            if body["from_address"] != MAIN_WALLET or body["input_token"] != SOL or int(body["input_amount"]) != lamports:
                raise CapError("swap body mismatch")
        except CapError as e:
            self._refuse(mint, name, "LIVE", size, str(e))
            await self.push(f"GMGN · Refused {name}: {e}", kind="gmgn_refused", mint=mint)
            return {"status": "refused", "reason": str(e)}
        oid = self._row(ts=c.clock(), night=c.night, mode="LIVE", mint=mint, name=name, symbol=symbol, status="submitting",
                        reason=REENTRY_REASON if reentry is not None else None,
                        size_usd=size, sol_usd=sol_usd, lamports=lamports, entry_price=price, peak=price, last_price=price, last_liq=liq,
                        body=json.dumps(body), conditions=json.dumps(body["condition_orders"]))
        try:
            r = await self.client.swap(body)
        except Exception as e:
            st = "unknown" if isinstance(e, GmgnError) and e.status == 0 else "failed"     # network error: may or may not have landed
            self._upd(oid, status=st, reason=str(e)[:300])
            self._log("LIVE", mint, "swap", st, str(e)[:200])
            await self.push(f"GMGN · Buy {name} {st}: {str(e)[:160]}" + ("\nCheck your GMGN wallet; I won't retry." if st == "unknown" else ""),
                            kind="gmgn_alert", mint=mint)
            return {"status": st}
        self._upd(oid, status="pending", order_id=r.get("order_id"), tx=r.get("hash"), strategy_order_id=r.get("strategy_order_id"))
        self._log("LIVE", mint, "swap sent", r.get("order_id"), r.get("status"))
        tag = "RE-ENTRY · " if reentry is not None else ""
        why = f" · bounced +{reentry['bouncePct']:.0f}% off post-sell low" if reentry is not None and reentry.get("bouncePct") is not None else ""
        await self.push(f"GMGN · {tag}Buying {name} · ${size:,.2f} ({lamports / LAMPORTS:.4f} SOL){why} · {self.open_label('LIVE')}\n{exits}", mint=mint)
        return await self.settle(oid, r, polls=8, gap=5)

    async def settle(self, oid, r=None, polls=8, gap=5.0):
        """Poll query_order until confirmed/failed; on confirm make sure a server-side exit exists.
        Runs once per order at a time: the watcher skips orders being settled (DECISION 39)."""
        if oid in self._settling:
            return {"status": "pending"}
        self._settling.add(oid)
        try:
            return await self._settle(oid, r, polls, gap)
        finally:
            self._settling.discard(oid)

    async def _settle(self, oid, r=None, polls=8, gap=5.0):
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        st = (r or {}).get("status")
        for i in range(polls):
            if st in ("confirmed", "successful", "failed", "expired"):
                break
            await asyncio.sleep(gap)
            try:
                r = await self.client.query_order(o["order_id"])
                st = r.get("status")
            except GmgnError as e:
                self._log("LIVE", o["mint"], "query_order", str(e)[:160])
        if st in ("failed", "expired"):
            self._upd(oid, status="failed", reason=(r or {}).get("error_status") or st)
            await self.push(f"GMGN · Buy {o['name']} {st}: {(r or {}).get('error_status') or ''}".strip(), kind="gmgn_alert", mint=o["mint"])
            return {"status": "failed"}
        if st not in ("confirmed", "successful"):
            return {"status": "pending"}                # watcher keeps polling
        rep = (r or {}).get("report") or {}
        fill = float(rep.get("price_usd") or 0) or o["entry_price"]
        sid = (r or {}).get("strategy_order_id") or o["strategy_order_id"]
        tx = (r or {}).get("hash") or o["tx"]
        src = "gmgn"
        buy = await self.buy_delta({**o, "tx": tx})
        if buy:                                          # real fill: SOL paid (fees + rent) / tokens received
            fill, src = chain.result(buy, [], o["sol_usd"])["entry_price"], "chain"
        self._upd(oid, status="open", entry_price=fill, entry_src=src, peak=fill, last_price=fill, tx=tx, strategy_order_id=sid,
                  **grace_fields(self.s, self.c.clock()))
        self.c.event("fill", o["mint"])
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        if not sid:
            try:
                filled = int(rep.get("output_amount") or 0) or None
            except (TypeError, ValueError):
                filled = None
            return await self.ensure_exits(oid, filled)
        await self.push(f"GMGN · {self._tag(o)}Bought {o['name']} · ${o['size_usd']:,.2f} · {self.open_label('LIVE')} · "
                        f"exits live on GMGN · {exits_label(self.s, grace_phase(self.s, o))}", mint=o["mint"])
        return {"status": "open", "strategy_order_id": sid}

    # ---- exits after a confirmed fill (DECISION 39: the 2026-11-15 nema stop-attach race)
    ATTACH_POLLS = 15            # x ATTACH_GAP_S = up to ~45s waiting for the fill to land / GMGN's own strategy
    ATTACH_GAP_S = 3.0
    ATTACH_GRACE_POLLS = 4       # first ~12s: only look for the strategy GMGN creates from the swap's condition_orders

    async def find_strategy(self, mint):
        """Order id of an OPEN GMGN strategy on this mint (the swap's condition_orders become one), else None.
        GMGN does not return strategy_order_id in the swap / query_order response, so this is the source of truth."""
        try:
            d = await self.client.strategy_orders(base_token=mint, kind="open")
        except Exception as e:
            self._log("LIVE", mint, "strategy_orders", str(e)[:160]); return None
        for x in (d or {}).get("list") or []:
            if (x.get("base_token") or x.get("token_address")) == mint and (x.get("status") or x.get("strategy_status")) not in ("canceled", "cancel", "closed", "finished"):
                return x.get("order_id") or x.get("id") or "open"
        return None

    async def token_balance(self, mint):
        """Raw token amount the main wallet holds: on-chain first, GMGN holdings as fallback. None = unknown."""
        try:
            r = await self.c.net.rpc("getTokenAccountsByOwner", [MAIN_WALLET, {"mint": mint}, {"encoding": "jsonParsed"}])
            vals = (r or {}).get("value") if isinstance(r, dict) else None
            if vals is not None:
                return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in vals)
        except Exception:
            pass
        try:
            d = await self.client.wallet_holdings()
            for h in (d or {}).get("list") or []:
                addr = (h.get("token") or {}).get("address") or h.get("token_address") or h.get("address")
                if addr == mint:
                    dec = int((h.get("token") or {}).get("decimals") or h.get("decimals") or 6)
                    return int(round(float(h.get("balance") or 0) * 10 ** dec))
        except Exception:
            pass
        return None

    async def ensure_exits(self, oid, filled=None):
        """Confirmed fill, no strategy id in the response: wait for GMGN's own strategy and for the tokens to land,
        then (only if still missing) create a stop for the real filled amount; market-sell only if that fails."""
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        mint, held = o["mint"], None
        for i in range(self.ATTACH_POLLS):
            sid = await self.find_strategy(mint)
            if sid:
                self._upd(oid, strategy_order_id=sid)
                self._log("LIVE", mint, "exits found on GMGN", sid)
                o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,)) or o
                await self.push(f"GMGN · {self._tag(o)}Bought {o['name']} · ${o['size_usd']:,.2f} · exits live on GMGN · "
                                f"{exits_label(self.s, grace_phase(self.s, o))}", mint=mint)
                return {"status": "open", "strategy_order_id": sid}
            held = await self.token_balance(mint)
            if i >= self.ATTACH_GRACE_POLLS - 1 and (held is None or held > 0):
                break
            await asyncio.sleep(self.ATTACH_GAP_S)
        if held == 0:
            self._log("LIVE", mint, "no tokens seen after fill", f"filled={filled}")
            await self.push(f"GMGN · {o['name']}: the buy confirmed but the tokens haven't shown in your wallet yet. "
                            f"I'm not selling anything; I'll keep checking.", kind="gmgn_alert", mint=mint)
            return {"status": "open", "repair": "waiting"}
        return await self.repair(oid, amount=held or filled)

    async def repair(self, oid, amount=None):
        """No GMGN strategy on a held position: create a server-side stop for the real amount (retries while GMGN says
        amount_in is 0, i.e. the fill hasn't settled). If that still fails, market-sell (guarded) and alert truthfully."""
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        if await self.find_strategy(o["mint"]):
            return {"status": "open", "repair": "none needed"}
        sol_usd = o["sol_usd"] or await self.sol_usd()
        stop_pct = (self.s["risk.grace_stop_pct"]
                    if (o.get("exits_phase") or "") == "grace"
                    and not grace_due(self.s, o, self.c.clock(), o.get("last_price"))
                    else self.s["risk.stop_pct"])
        for attempt in range(4):
            try:
                r = await self.client.strategy_create(stop_strategy_body(self.s, o["mint"], o["entry_price"], sol_usd, amount, stop_pct=stop_pct))
                sid = (r or {}).get("order_id")
                if not sid:
                    raise GmgnError(200, error="NO_ORDER_ID", message="strategy create returned no order_id")
                self._upd(oid, strategy_order_id=sid, repair="stop_loss strategy created")
                await self.push(f"GMGN · {o['name']}: exits weren't attached, so I created a server-side -{stop_pct:g}% stop. "
                                f"The trailing stop runs from my computer while it's awake.", kind="gmgn_alert", mint=o["mint"])
                return {"status": "open", "repair": "strategy"}
            except Exception as e:
                self._log("LIVE", o["mint"], "repair failed", str(e)[:200])
                if "amount_in must be greater than zero" not in str(e):
                    break
                await asyncio.sleep(self.ATTACH_GAP_S)
                amount = await self.token_balance(o["mint"]) or amount
                if await self.find_strategy(o["mint"]):
                    return {"status": "open", "repair": "none needed"}
        res = await self.safe_sell(oid, "no server-side exit")
        if res in ("sent", "already"):
            await self.push(f"GMGN · ALERT {o['name']}: couldn't attach a server-side stop, so I market-sold it.", kind="gmgn_alert", mint=o["mint"])
            return {"status": "exiting", "repair": "market_sell"}
        if res == "none_held":
            await self.push(f"GMGN · {o['name']}: no tokens left in your wallet, nothing to sell.", kind="gmgn_alert", mint=o["mint"])
            return {"status": "exiting", "repair": "none_held"}
        held = await self.token_balance(o["mint"])
        if held and held > 0:
            await self.push(f"GMGN · ALERT {o['name']}: couldn't attach a stop and the market sell FAILED. "
                            f"Your wallet still holds it. Open GMGN and sell it yourself now.", kind="gmgn_alert", mint=o["mint"])
        elif held == 0:
            await self.push(f"GMGN · {o['name']}: sold, your wallet holds none of it.", kind="gmgn_alert", mint=o["mint"])
            return {"status": "exiting", "repair": "none_held"}
        else:
            await self.push(f"GMGN · ALERT {o['name']}: couldn't attach a stop and the market sell failed; I can't read your "
                            f"balance right now. Check GMGN.", kind="gmgn_alert", mint=o["mint"])
        return {"status": "open", "repair": "failed"}

    async def safe_sell(self, oid, reason) -> str:
        """Market sell with guards: never a second sell while one is pending/done, never a sell with 0 tokens held.
        -> 'sent' | 'already' | 'none_held' | 'failed'."""
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        if o["sell_order_id"]:
            try:
                q = await self.client.query_order(o["sell_order_id"])
                st = (q or {}).get("status")
            except Exception:
                st = "unknown"
            if st not in ("failed", "expired"):
                self._log("LIVE", o["mint"], "market sell skipped", f"previous sell {st}")
                return "already"
        held = await self.token_balance(o["mint"])
        if held == 0:
            self._upd(oid, status="exiting", exit_reason=o["exit_reason"] or reason)
            self._log("LIVE", o["mint"], "market sell skipped", "0 tokens held")
            return "none_held"
        try:
            r = await self.client.swap(sell_all_body(self.s, o["mint"]))
            self._upd(oid, status="exiting", exit_reason=reason, sell_order_id=(r or {}).get("order_id"), repair=(o["repair"] or "") + " market_sell")
            self._log("LIVE", o["mint"], "market sell", reason, (r or {}).get("order_id"))
            return "sent"
        except Exception as e:
            self._upd(oid, repair=(o["repair"] or "") + f" market_sell failed: {str(e)[:120]}")
            self._log("LIVE", o["mint"], "market sell failed", str(e)[:200])
            return "failed"

    async def market_sell(self, oid, reason):
        return await self.safe_sell(oid, reason) in ("sent", "already", "none_held")

    def _clear_grace_extend(self, oid):
        self._grace_extended.discard(oid)
        self._grace_recover.pop(oid, None)
        self._grace_tick_ts.pop(oid, None)

    def _grace_due_tick(self, o, now, price, src=None) -> bool:
        """Advance extension / recover-streak state. True when it's time to swap to the normal stop."""
        s, oid = self.s, o["id"]
        extended = oid in self._grace_extended
        if grace_due(s, o, now, price, extended=extended, recover_streak=self._grace_recover.get(oid, 0)):
            return True
        if grace_should_extend(s, o, now, price):
            self._grace_recover[oid] = 0
            if oid not in self._grace_extended:
                self._grace_extended.add(oid)
                entry = o.get("entry_price") or 0
                pct = (100 * (float(price) / entry - 1)) if price is not None and entry else None
                pnl = f"{pct:+.1f}%" if pct is not None else "no price"
                self._log(o.get("mode") or "", o.get("mint") or "", "grace extended", f"#{oid}",
                          f"price={price if price is not None else 'none'}",
                          f"entry={entry or 'none'}", pnl,
                          f"keep -{s['risk.grace_stop_pct']:g}% stop until back above -{s['risk.stop_pct']:g}%",
                          f"src={src or 'none'}")
            return False
        if extended and price is not None:
            last = self._grace_tick_ts.get(oid)
            if last is None or now - last >= GRACE_RECOVER_TICK_GAP_S:
                self._grace_recover[oid] = self._grace_recover.get(oid, 0) + 1
                self._grace_tick_ts[oid] = now
        return False

    async def grace_extend_once(self, rows=None, prices=None, pairs=None, now=None) -> int:
        """After the grace window, keep the steep stop while price is still below the recover
        threshold; swap only once it has recovered. Uses the fast pool price when `prices`
        isn't passed (DECISION 65). PAPER phase flips stay in _paper_tick. Returns LIVE swaps started."""
        now = self.c.clock() if now is None else now
        if rows is None:
            rows = self.c.db.rows(f"SELECT * FROM gmgn_orders WHERE status IN ({','.join('?'*len(ACTIVE))})", ACTIVE)
        grace_rows = [r for r in rows if (r.get("exits_phase") or "") == "grace" and r["status"] == "open"
                      and r["mode"] == "LIVE" and r["id"] not in self._settling]
        if not grace_rows:
            return 0
        if prices is None:
            prices = await self.exit_prices(grace_rows, pairs=pairs)
        n = 0
        for r in grace_rows:
            fresh = prices.get(r["id"])
            price, src = (fresh[0], fresh[1]) if fresh else (None, None)
            if self._grace_due_tick(r, now, price, src):
                await self.promote_exits(r["id"])
                self._clear_grace_extend(r["id"])
                n += 1
        return n

    # ---- grace -> normal exits (DECISION 48)
    SWAP_TRIES = 4
    SWAP_GAP_S = 2.0

    def _cancel_body(self, sid, order_type="smart_trade"):
        return {"chain": gmgn.CHAIN, "from_address": MAIN_WALLET, "order_id": sid, "order_type": order_type}

    def _strategy_type(self, o) -> str:
        rep_ = o.get("repair") or ""
        return "limit_order" if ("stop_loss strategy created" in rep_ or LIMIT_STOP_MARK in rep_) else "smart_trade"

    def _normal_strategy_body(self, s, mint, amount_in=None, phase="normal") -> dict:
        extra = {"amount_in": str(int(amount_in))} if amount_in else {}
        return {**extra, "chain": gmgn.CHAIN, "from_address": MAIN_WALLET, "base_token": mint, "quote_token": SOL,
                "order_type": "smart_trade", "sub_order_type": "mix_trade", "amount_in_percent": "100",
                "sell_ratio_type": "hold_amount", "slippage": max(int(s["gmgn.slippage_pct"]), 25), "is_anti_mev": True,
                "priority_fee": f"{s['gmgn.priority_fee_sol']:g}", "tip_fee": f"{s['gmgn.tip_fee_sol']:g}",
                "sell_param": {"slippage": max(int(s["gmgn.slippage_pct"]), 25), "is_anti_mev": True,
                               "priority_fee": f"{s['gmgn.priority_fee_sol']:g}", "tip_fee": f"{s['gmgn.tip_fee_sol']:g}"},
                "condition_orders": conditions(s, phase)}

    async def _create_strategy(self, body) -> str:
        r = await self.client.strategy_create(body)
        sid = (r or {}).get("order_id")
        if not sid:
            raise GmgnError(200, error="NO_ORDER_ID", message="strategy create returned no order_id")
        return sid

    async def _cancel_strategy(self, sid, order_type="smart_trade") -> bool:
        """True if the order is gone (cancelled now, or already cancelled/closed/missing)."""
        if not sid or sid == "open":
            return True
        try:
            await self.client.strategy_cancel(self._cancel_body(sid, order_type))
            return True
        except Exception as e:
            msg = str(e).lower()
            if any(x in msg for x in ("not found", "already", "canceled", "cancelled", "closed", "finished")):
                return True
            self._log("LIVE", "strategy_cancel", str(e)[:200])
            return False

    async def promote_exits(self, oid):
        """Replace grace exits with the normal -stop / trail set. Never leave a gap with no server-side stop.
        Prefer create-new then cancel-old. If GMGN rejects a second strategy, cancel then create with retries.
        If price is already below -stop_pct when the tighter stop is attached, GMGN may sell immediately (intended).
        If the swap fails and the position would have no stop, alert; market-sell is the last resort."""
        if oid in self._settling:
            return {"status": "busy"}
        self._settling.add(oid)
        try:
            return await self._promote_exits(oid)
        finally:
            self._settling.discard(oid)

    async def _attach_normal(self, o, amount):
        """Create the normal (post-grace) server-side exits. Prefer a smart_trade with the full condition set;
        fall back to a limit_order stop at -stop_pct; the take-profit trail then runs from the desk (trail_once)."""
        s = self.s
        try:
            sid = await self._create_strategy(self._normal_strategy_body(s, o["mint"], amount or None))
            return sid, "smart_trade", conditions(s, "normal")
        except Exception as e:
            self._log("LIVE", o["mint"], "promote smart_trade failed", str(e)[:200])
        sol_usd = o["sol_usd"] or await self.sol_usd()
        sid = await self._create_strategy(stop_strategy_body(s, o["mint"], o["entry_price"], sol_usd, amount or None))
        return sid, "limit_order", [{"order_type": "loss_stop", "price_scale": f"{s['risk.stop_pct']:g}", "sell_ratio": "100"}]

    def _promoted_text(self, o, new_type):
        s = self.s
        if o.get("id") in self._grace_extended:
            head = (f"GMGN · {self._tag(o)}{o['name']}: price back above -{s['risk.stop_pct']:g}% · "
                    f"stop -{s['risk.stop_pct']:g}%")
        else:
            head = f"GMGN · {self._tag(o)}{o['name']}: {s['risk.grace_min']:g} min grace over · stop -{s['risk.stop_pct']:g}%"
        trail = (f"take-profit trail {s['risk.trail_pct']:g}% from +{s['risk.trail_start_pct']:g}%, "
                 f"never below +{s['risk.trail_floor_pct']:g}% net after fees, runs from my computer")
        if new_type == "limit_order":
            return f"{head} live on GMGN · {trail}"
        return f"{head} · {trail}"

    async def _promote_exits(self, oid):
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        if not o or o["status"] != "open" or (o.get("exits_phase") or "") != "grace":
            return {"status": o["status"] if o else "missing"}
        s = self.s
        old_sid, old_type = o.get("strategy_order_id"), self._strategy_type(o)
        amount = await self.token_balance(o["mint"])
        if amount == 0:
            return {"status": "open", "promote": "none_held"}
        new_sid = new_type = new_conds = None
        created_first = False
        try:
            new_sid, new_type, new_conds = await self._attach_normal(o, amount)
            created_first = True
        except Exception as e:
            self._log("LIVE", o["mint"], "promote create-first failed", str(e)[:200])

        if created_first:
            cancelled = await self._cancel_strategy(old_sid, old_type)
            note = "grace_swap" if cancelled else "grace_swap: new stop on, old cancel failed"
            if new_type == "limit_order":
                note += " " + LIMIT_STOP_MARK
            self._upd(oid, strategy_order_id=new_sid, exits_phase="normal",
                      conditions=json.dumps(new_conds), repair=(o.get("repair") or "") + (" " + note if o.get("repair") else note))
            if cancelled:
                await self.push(self._promoted_text(o, new_type), mint=o["mint"])
                return {"status": "open", "promote": "ok"}
            await self.push(f"GMGN · ALERT {o['name']}: the grace window is over and the normal -{s['risk.stop_pct']:g}% stop is on, "
                            f"but I couldn't cancel the old -{s['risk.grace_stop_pct']:g}% grace stop. Both are live.",
                            kind="gmgn_alert", mint=o["mint"])
            return {"status": "open", "promote": "new_on_old_cancel_failed"}

        # GMGN rejected a second strategy: cancel the old one, then create, with retries. Brief gap possible.
        cancelled = False
        for _ in range(self.SWAP_TRIES):
            if await self._cancel_strategy(old_sid, old_type):
                cancelled = True
                break
            await asyncio.sleep(self.SWAP_GAP_S)
        if not cancelled:
            if "grace_swap: cancel_failed" not in (o.get("repair") or ""):
                self._upd(oid, repair=(o.get("repair") or "") + " grace_swap: cancel_failed")
                await self.push(f"GMGN · ALERT {o['name']}: the {s['risk.grace_min']:g} min grace ended, but I couldn't switch to the normal "
                                f"-{s['risk.stop_pct']:g}% stop. The -{s['risk.grace_stop_pct']:g}% grace stop is still on. I'll keep trying.",
                                kind="gmgn_alert", mint=o["mint"])
            return {"status": "open", "promote": "cancel_failed"}

        for _ in range(self.SWAP_TRIES):
            try:
                new_sid, new_type, new_conds = await self._attach_normal(o, amount)
                self._upd(oid, strategy_order_id=new_sid, exits_phase="normal",
                          conditions=json.dumps(new_conds),
                          repair=(o.get("repair") or "") + " grace_swap: cancel-then-create"
                          + (" " + LIMIT_STOP_MARK if new_type == "limit_order" else ""))
                await self.push(self._promoted_text(o, new_type), mint=o["mint"])
                return {"status": "open", "promote": "cancel_then_create"}
            except Exception as e:
                self._log("LIVE", o["mint"], "promote create after cancel failed", str(e)[:200])
                await asyncio.sleep(self.SWAP_GAP_S)

        res = await self.safe_sell(oid, "no server-side exit after grace swap")
        if res in ("sent", "already"):
            await self.push(f"GMGN · ALERT {o['name']}: the grace stop was cancelled and I couldn't attach the normal "
                            f"-{s['risk.stop_pct']:g}% stop, so I market-sold it.", kind="gmgn_alert", mint=o["mint"])
            return {"status": "exiting", "promote": "market_sell"}
        if res == "none_held":
            await self.push(f"GMGN · {o['name']}: no tokens left in your wallet after the grace-stop swap.",
                            kind="gmgn_alert", mint=o["mint"])
            return {"status": "exiting", "promote": "none_held"}
        await self.push(f"GMGN · ALERT {o['name']}: the grace stop was cancelled and I couldn't attach a new stop or "
                        f"market-sell. Open GMGN and sell it yourself now.", kind="gmgn_alert", mint=o["mint"])
        return {"status": "open", "promote": "failed"}


    # ---- one-shot break-even adoption of a manual wallet buy (DECISION 78)
    def pending_adopts(self) -> list:
        v = self.c.db.meta(PENDING_ADOPTS_META, []) or []
        return v if isinstance(v, list) else []

    def set_pending_adopts(self, rows: list):
        self.c.db.set_meta(PENDING_ADOPTS_META, list(rows or []))

    def queue_pending_adopt(self, mint, name, symbol, sigs, **extra) -> dict:
        """Persist a one-shot watch: adopt into A LIVE when price is back at entry. Does not touch the position."""
        rows = [r for r in self.pending_adopts() if r.get("mint") != mint]
        rec = {"mint": mint, "name": name, "symbol": symbol, "sigs": list(sigs or []),
               "created_at": self.c.clock(), "notified": False}
        rec.update(extra)
        rec["mint"], rec["name"], rec["symbol"], rec["sigs"] = mint, name, symbol, list(sigs or [])
        rows.append(rec)
        self.set_pending_adopts(rows)
        return rec

    def _loop_wait_s(self) -> float:
        wait = float(self.s["risk.poll_s"])
        if self.pending_adopts():
            wait = min(wait, PENDING_ADOPT_POLL_S)
        return wait

    def _pending_drop(self, mint):
        self.set_pending_adopts([r for r in self.pending_adopts() if r.get("mint") != mint])

    def _pending_save(self, rec):
        rows = [r for r in self.pending_adopts() if r.get("mint") != rec["mint"]]
        rows.append(rec)
        self.set_pending_adopts(rows)

    async def _pending_legs(self, rec):
        if rec.get("legs"):
            return rec["legs"]
        legs = []
        for sig in rec.get("sigs") or []:
            d = await chain.tx_delta(self.c.net, sig, MAIN_WALLET, rec["mint"], self._txc)
            if not d or d["err"] or d["tok"] <= 0 or d["sol"] >= 0:
                return None
            legs.append({"sig": d.get("sig") or sig, "ts": d["ts"], "sol": d["sol"], "tok": d["tok"],
                         "dec": d.get("dec") or 6})
        return legs or None

    async def _freeze_pending(self, rec) -> dict | None:
        """Fill entry_price / size_usd from the buy legs and the desk's SOL price. None = try again later."""
        if rec.get("entry_price") and rec.get("size_usd") and rec.get("lamports") and rec.get("tok_raw"):
            return rec
        legs = await self._pending_legs(rec)
        if not legs:
            return None
        sol_usd = rec.get("sol_usd") or await self.sol_usd()
        if not sol_usd:
            return None
        tok = int(sum(l["tok"] for l in legs))
        lam = int(-sum(l["sol"] for l in legs))
        dec = int(legs[0].get("dec") or 6)
        ts = min((l.get("ts") or 0) for l in legs)
        entry = chain.result({"sol": -lam, "tok": tok, "dec": dec}, [], sol_usd)["entry_price"]
        size = round(lam / LAMPORTS * sol_usd, 2)
        entry_mc = rec.get("entry_mc")
        if entry_mc is None:
            try:
                r = await self.c.net.rpc("getTokenSupply", [rec["mint"]])
                supply_ui = float((r or {}).get("value", {}).get("uiAmount") or 0) or None
                if entry and supply_ui:
                    entry_mc = round(entry * supply_ui, 2)
            except Exception:
                pass
        rec = {**rec, "legs": legs, "tok_raw": tok, "lamports": lam, "dec": dec,
               "sol_usd": sol_usd, "entry_price": entry, "size_usd": size,
               "entry_mc": entry_mc, "opened_at": rec.get("opened_at") or (float(ts) if ts else self.c.clock())}
        return rec

    async def _pending_prices(self, mints, prices=None) -> dict:
        """{mint: price_usd} from the caller (tests), else the desk's existing exit quote path. No new API family."""
        if prices is not None:
            return {m: float(prices[m]) for m in mints if m in prices and prices[m]}
        mints = list(mints)
        if not mints:
            return {}
        if self.fast is not None:
            got = await self.fast.prices(mints)
            return {m: float(p) for m, (p, _src) in got.items() if p}
        from agents.scanner import pick_pair
        import fields
        pairs = await self.c.net.dex_tokens(sorted(set(mints)))
        out = {}
        for m in mints:
            pair = pick_pair(pairs, m)
            v = fields.get(pair, "priceUsd") if pair else None
            if v:
                out[m] = float(v)
        return out

    async def check_pending_adopts(self, prices=None):
        """One-shot watches: adopt at break-even, or cancel if the owner already sold. Never touches the bag before then."""
        rows = self.pending_adopts()
        if not rows:
            return
        px = await self._pending_prices([r["mint"] for r in rows], prices=prices)
        for rec in list(self.pending_adopts()):
            await self._tick_pending_adopt(rec, px)

    async def _tick_pending_adopt(self, rec, px: dict):
        mint, symbol = rec["mint"], rec.get("symbol") or rec.get("name") or "coin"
        frozen = await self._freeze_pending(rec)
        if frozen is None:
            return
        rec = frozen
        self._pending_save(rec)
        if not rec.get("notified"):
            await self.push(
                f"Desk · watching {symbol}, Strat A protection at break even (${rec['size_usd']:,.2f})",
                kind="info", mint=mint)
            rec = {**rec, "notified": True}
            self._pending_save(rec)
        bal = await chain.token_balance(self.c.net, MAIN_WALLET, mint)
        if bal is None:
            return
        if bal < max(1, int((rec.get("tok_raw") or 0) * 0.01)):
            self._pending_drop(mint)
            await self.push(f"Desk · {symbol} watch cancelled (sold)", kind="info", mint=mint)
            self._log("LIVE", mint, "pending adopt cancelled (sold)")
            return
        price = px.get(mint)
        if price is None or price < rec["entry_price"]:
            return
        r = await self.adopt(mint, rec.get("name") or symbol, symbol, rec.get("sigs") or [],
                             note=rec.get("note") or "adopted manual buy", pending=rec)
        if r.get("status") == "open":
            self._pending_drop(mint)
            await self.push(f"Strat A · {symbol} back to break even, now protected", kind="gmgn", mint=mint)
            self._log("LIVE", mint, "pending adopt filled", f"#{r.get('id')}", f"px={price:.10g}")
        elif r.get("status") == "refused" and "no longer holds" in (r.get("reason") or ""):
            self._pending_drop(mint)
            await self.push(f"Desk · {symbol} watch cancelled (sold)", kind="info", mint=mint)
        else:
            self._log("LIVE", mint, "pending adopt retry", r.get("reason") or r.get("status"))

    async def adopt(self, mint, name, symbol, sigs, *, note=None, pending=None) -> dict:
        """Adopt a manual wallet buy into A LIVE, same shape as gmgn_orders #62 Dior (DECISION 78).
        Real entry from the buy tx(s) (or a frozen pending-adopt snapshot), a GMGN limit_order stop at
        -risk.stop_pct for 100% of the tokens held now, desk trail / surge as normal A exits. No grace
        window. Does not spend new SOL and does not change modes, caps, or settings."""
        if self.client is None:
            return {"status": "refused", "reason": "GMGN client not configured"}
        held_by = mint_held_live(self.c.db, mint)
        if held_by:
            return {"status": "refused", "reason": f"already held by {held_by} LIVE in the wallet"}
        if pending and pending.get("entry_price") and pending.get("lamports") and pending.get("tok_raw"):
            legs = pending.get("legs") or []
            if not legs:
                legs = await self._pending_legs({"mint": mint, "sigs": sigs, "legs": pending.get("legs")})
                if not legs:
                    return {"status": "refused", "reason": "no buy signature"}
            tok, lam, dec = int(pending["tok_raw"]), int(pending["lamports"]), int(pending.get("dec") or legs[0].get("dec") or 6)
            sol_usd, entry = float(pending["sol_usd"]), float(pending["entry_price"])
            size = float(pending["size_usd"])
            ts = pending.get("opened_at") or min((l.get("ts") or 0) for l in legs) or self.c.clock()
            entry_mc = pending.get("entry_mc")
        else:
            if not sigs:
                return {"status": "refused", "reason": "no buy signature"}
            legs = []
            for sig in sigs:
                d = await chain.tx_delta(self.c.net, sig, MAIN_WALLET, mint, self._txc)
                if not d or d["err"] or d["tok"] <= 0 or d["sol"] >= 0:
                    return {"status": "refused", "reason": f"buy tx is not a wallet buy of this mint ({sig[:8]})"}
                legs.append(d)
            legs.sort(key=lambda l: l["ts"] or 0)
            tok, lam = sum(l["tok"] for l in legs), -sum(l["sol"] for l in legs)
            ts = min((l["ts"] or 0) for l in legs)
            dec = legs[0].get("dec") or 6
            sol_usd = await self.sol_usd()
            if not sol_usd:
                return {"status": "refused", "reason": "no SOL price from GMGN"}
            entry = chain.result({"sol": -lam, "tok": tok, "dec": dec}, [], sol_usd)["entry_price"]
            size = round(lam / LAMPORTS * sol_usd, 2)
            entry_mc = None
            try:
                r = await self.c.net.rpc("getTokenSupply", [mint])
                supply_ui = float((r or {}).get("value", {}).get("uiAmount") or 0) or None
                if entry and supply_ui:
                    entry_mc = round(entry * supply_ui, 2)
            except Exception:
                pass
        bal = await chain.token_balance(self.c.net, MAIN_WALLET, mint)
        if bal is None:
            return {"status": "refused", "reason": "wallet balance unknown"}
        if bal < max(1, int(tok * 0.01)):
            return {"status": "refused", "reason": "wallet no longer holds the bought tokens"}
        stop_pct = float(self.s["risk.stop_pct"])
        existing = []
        for tag in ("STMix", "LimitOrder"):
            try:
                d = await self.client.strategy_orders(base_token=mint, kind="open", group_tag=tag)
            except TypeError:
                d = await self.client.strategy_orders(base_token=mint, kind="open")
            existing += [x for x in (d or {}).get("list") or []
                         if (x.get("base_token") or x.get("token_address")) == mint
                         and (x.get("order_id") or x.get("id"))]
        if existing:
            sid = existing[0].get("order_id") or existing[0].get("id")
            repair = "adopted manual buy; existing GMGN strategy"
        else:
            body = stop_strategy_body(self.s, mint, entry, sol_usd, bal, stop_pct=stop_pct)
            try:
                r = await self.client.strategy_create(body)
            except Exception as e:
                self._log("LIVE adopt stop failed", mint, str(e)[:200])
                return {"status": "refused", "reason": f"could not create GMGN stop: {str(e)[:160]}"}
            sid = (r or {}).get("order_id")
            if not sid:
                return {"status": "refused", "reason": "could not create GMGN stop"}
            repair = "adopted manual buy; stop_loss strategy created"
        held_by = mint_held_live(self.c.db, mint)
        if held_by:
            return {"status": "refused", "reason": f"already held by {held_by} LIVE in the wallet"}
        now = self.c.clock()
        opened = float(ts) or now
        audit = {"adopted": True,
                 "buys": [{"sig": l.get("sig"), "ts": l.get("ts"), "sol": l.get("sol"), "tok": l.get("tok")} for l in legs],
                 "tok_raw": tok, "sol_lamports": lam, "phase": "normal", "strategy_kind": "limit_order",
                 "note": note or "adopted manual buy"}
        conds = [{"order_type": "loss_stop", "price_scale": f"{stop_pct:g}", "sell_ratio": "100"}]
        oid = self._row(ts=opened, night=self.c.night, mode="LIVE", mint=mint, name=name, symbol=symbol,
                        status="open", reason="adopted manual buy", size_usd=size, sol_usd=sol_usd,
                        lamports=lam, entry_price=entry, peak=entry, last_price=entry,
                        order_id="", strategy_order_id=sid, repair=repair, tx=(legs[-1].get("sig") if legs else (sigs or [""])[-1]),
                        entry_src="chain", audit=json.dumps(audit), grace_until=None, exits_phase="normal",
                        no_surge=0, conditions=json.dumps(conds), last_check=now)
        self._log("LIVE adopted", mint, name, f"#{oid}", f"${size:.2f}", f"stop={sid}", repair)
        return {"status": "open", "id": oid, "strategy_order_id": sid, "entry_price": entry,
                "size_usd": size, "lamports": lam, "tokens": tok / 10 ** dec, "mc_entry": entry_mc,
                "check_price": entry * (1 - stop_pct / 100.0) if entry else None, "note": repair,
                "held_raw": bal}

    # ---- watcher (Risk side of GMGN)
    async def loop(self):
        while True:
            t0 = time.time()
            try:
                await self.watch_once()
                await self.check_pending_adopts()
                await self.expire_approvals()
                n = self.c.db.one(f"SELECT COUNT(*) n FROM gmgn_orders WHERE status IN ({','.join('?'*len(ACTIVE))})", ACTIVE)["n"]
                pend = self.pending_adopts()
                extra = f" pending_adopt={len(pend)}" if pend else ""
                self.c.hb("gmgn", f"{self.mode()} open={n}{' HALTED' if self.halted() else ''}{extra}")
            except Exception as e:
                self.c.error("gmgn", "watch", str(e)[:300])
            await asyncio.sleep(max(1, self._loop_wait_s() - (time.time() - t0)))

    async def watch_once(self, pairs=None, holdings=None, strategies=None):
        c, s = self.c, self.s
        rows = c.db.rows(f"SELECT * FROM gmgn_orders WHERE status IN ({','.join('?'*len(ACTIVE))})", ACTIVE)
        if not rows:
            return
        from agents.scanner import pick_pair
        import fields
        now = c.clock()
        # Prices first so grace-extend (DECISION 65) can use the fast pool price (or caller-supplied
        # DexScreener pairs in tests). Promote as soon as the window ends and price has recovered
        # (or immediately after a pause past it). Existing rows with no exits_phase are never touched.
        prices = await self.exit_prices(rows, pairs=pairs)
        if pairs is None:
            pairs = await c.net.dex_tokens(sorted({r["mint"] for r in rows}))
        promoted = await self.grace_extend_once(rows, prices, pairs=pairs, now=now)
        if promoted:
            rows = c.db.rows(f"SELECT * FROM gmgn_orders WHERE status IN ({','.join('?'*len(ACTIVE))})", ACTIVE)
        live_due = [r for r in rows if r["mode"] == "LIVE" and now - (r["last_check"] or 0) >= s["gmgn.watch_s"]]
        # GMGN wallet_holdings is NOT used to decide a close (DECISION 42): it is paginated (50 per page) and lags
        # the chain; a missing mint used to read as "0 held" and closed The Same Coin 73s before the real sell.
        if live_due and strategies is None:
            strategies = await self._strategies()
        for o in rows:
            pair = pick_pair(pairs, o["mint"])
            price = float(fields.get(pair, "priceUsd")) if pair and fields.get(pair, "priceUsd") else None
            liq = fields.get(pair, "liquidity.usd") if pair else None
            if o["mode"] == "PAPER":
                await self._paper_tick(o, price, liq, now)
            elif o in live_due:
                await self._live_tick(o, price, liq, now, holdings, strategies)

    async def _paper_tick(self, o, price, liq, now):
        if price is None:
            return
        if self._grace_due_tick(o, now, price, src="dex"):
            self._upd(o["id"], exits_phase="normal")
            self._clear_grace_extend(o["id"])
            o = {**o, "exits_phase": "normal"}
        if liq is not None and float(liq) <= 0:
            return await self._paper_close(o, "pulled", o["last_price"] or price, now)
        oid = o["id"]
        reason, peak = sim_exit(self.s, o, price, now, below_floor=oid in self._trail_below_floor)
        self._upd(oid, peak=peak, last_price=price, last_liq=liq)
        floor_px = trail_net_floor_price(self.s, o)
        armed = peak >= o["entry_price"] * (1 + float(self.s["risk.trail_start_pct"]) / 100.0)
        if reason == "profit_stop_trace":
            self._trail_below_floor.discard(oid)
        elif armed and floor_px is not None and price < floor_px:
            self._trail_below_floor.add(oid)
        if reason:
            await self._paper_close(o, reason, price, now)

    def _pnl(self, o, exit_price):
        size = o["size_usd"]; fee = self.s["gmgn.fee_pct"] / 100.0
        gross = size * (exit_price / o["entry_price"] - 1)
        fees = size * fee + size * (exit_price / o["entry_price"]) * fee
        net = gross - fees
        return round(net, 2), round(100 * net / size, 2)

    async def _paper_close(self, o, reason, price, now):
        pnl, pct = self._pnl(o, price)
        self._upd(o["id"], status="closed", exit_reason=reason, exit_price=price, pnl_usd=pnl, pct=pct, closed_at=now)
        sign = "+" if pnl >= 0 else "-"
        await self.push(f"PAPER · GMGN · Sold {o['name']} · {EXIT_LABEL.get(reason, reason)} · {sign}${abs(pnl):,.2f} ({pct:+.1f}%)", mint=o["mint"])
        self.c.event("exit", o["mint"])

    async def _holdings(self):
        try:
            d = await self.client.wallet_holdings()
            return {h.get("token", {}).get("address") or h.get("token_address") or h.get("address"): float(h.get("balance") or 0)
                    for h in (d or {}).get("list") or []}
        except Exception as e:
            self.c.error("gmgn", "holdings", str(e)[:200]); return None

    async def _strategies(self):
        try:
            d = await self.client.strategy_orders()
            out = {}
            for x in (d or {}).get("list") or []:
                out.setdefault(x.get("base_token") or x.get("token_address"), []).append(x)
            return out
        except Exception as e:
            self.c.error("gmgn", "strategies", str(e)[:200]); return None

    async def _live_tick(self, o, price, liq, now, holdings, strategies):
        if o["id"] in self._settling:
            return                                       # settle/exit-attach in progress: never race it (DECISION 39)
        self._upd(o["id"], last_check=now)
        if price is not None:
            self._upd(o["id"], last_price=price, peak=max(o["peak"] or price, price), last_liq=liq)
        if o["status"] == "pending":
            return await self.settle(o["id"], None, polls=1, gap=0)
        if o["status"] == "submitting":
            return                                       # crashed mid-send: never resend; owner was alerted at send time
        if await self.check_closed(o, now):
            return
        if o["status"] == "exiting":
            return
        if strategies is not None and o["mint"] not in strategies and not o["repair"]:
            await self.repair(o["id"]); return
        if liq is not None and float(liq) <= 0:
            if await self.market_sell(o["id"], "pulled"):
                await self.push(f"GMGN · {o['name']}: liquidity pulled, market-selling", kind="gmgn_alert", mint=o["mint"])
            return
        if price is not None:
            reason, _ = sim_exit(self.s, o, price, now)
            if reason == "timeout":
                if await self.market_sell(o["id"], "timeout"):
                    await self.push(f"GMGN · {o['name']}: dead money after {self.s['risk.timeout_h']:g}h, market-selling", mint=o["mint"])

    # ---- prices for the exit watchers (DECISION 57)
    def exit_poll_s(self, default: float) -> float:
        """Watcher cadence: risk.fast_poll_s while the pool feed is on and a LIVE position is open, else the old 3 s."""
        f = self.fast
        if f is not None and f.enabled() and self.c.db.one("SELECT 1 x FROM gmgn_orders WHERE status='open' AND mode='LIVE' LIMIT 1"):
            return f.poll_s()
        return default

    async def exit_prices(self, rows, pairs=None) -> dict:
        """{order id: (price_usd, src)}. Given `pairs` (tests / callers with a DexScreener batch): those, src 'dex'.
        Otherwise LIVE rows use the fast feed (pool reserves -> GMGN quote -> DexScreener) and PAPER rows DexScreener."""
        from agents.scanner import pick_pair
        import fields

        def from_pairs(ps, rs):
            out = {}
            for o in rs:
                pair = pick_pair(ps, o["mint"])
                v = fields.get(pair, "priceUsd") if pair else None
                if v:
                    out[o["id"]] = (float(v), "dex")
            return out
        if pairs is not None:
            return from_pairs(pairs, rows)
        live = [o for o in rows if o["mode"] == "LIVE"] if self.fast is not None else []
        other = [o for o in rows if o not in live]
        out = {}
        if live:
            px = await self.fast.prices([o["mint"] for o in live])
            out.update({o["id"]: px[o["mint"]] for o in live if o["mint"] in px})
        if other:
            out.update(from_pairs(await self.c.net.dex_tokens(sorted({o["mint"] for o in other})), other))
        return out

    # ---- surge-top sell (DECISION 49)
    async def surge_loop(self):
        backoff = 0.0
        while True:
            t0 = time.time()
            try:
                n = await self.surge_once()
                self.c.hb("gmgn_surge", f"{'on' if self.s['risk.surge_sell_enabled'] else 'off'} watching={n}"
                                        + (f" src={self.fast.note()}" if self.fast is not None and n else ""))
                backoff = 0.0
            except Exception as e:
                import net as _net
                if isinstance(e, _net.RateLimited):
                    backoff = float(self.s["risk.backoff_429_s"])
                self.c.error("gmgn", "surge", str(e)[:300])
                self.c.hb("gmgn_surge", f"error {type(e).__name__}")
            await asyncio.sleep(max(0.5, self.exit_poll_s(SURGE_POLL_S) - (time.time() - t0)) + backoff)

    async def surge_once(self, pairs=None) -> int:
        """One fast price check on every open GMGN position. Returns how many were checked."""
        if not self.s["risk.surge_sell_enabled"]:
            self._surge.clear()
            return 0
        rows = self.c.db.rows("SELECT * FROM gmgn_orders WHERE status='open'")
        live_ids = {r["id"] for r in rows if not r.get("no_surge")}
        for k in list(self._surge):
            if k not in live_ids:
                self._surge.pop(k, None); self._surge_retry.pop(k, None)
        rows = [r for r in rows if not r.get("no_surge")]
        if not rows:
            return 0
        px = await self.exit_prices(rows, pairs)
        now = self.c.clock()
        for o in rows:
            if o["id"] not in px or not o["entry_price"]:
                continue
            price, src = px[o["id"]]
            st = self._surge.get(o["id"])
            if st is None:
                st = self._surge[o["id"]] = {"hist": []}
                fill_ts = (o["grace_until"] - float(self.s["risk.grace_min"]) * 60) if o.get("grace_until") else o["ts"]
                if fill_ts and now - fill_ts <= float(self.s["risk.surge_window_s"]):
                    st["hist"].append((fill_ts, o["entry_price"]))      # the buy counts as the surge's starting low
            res = surge_check(self.s, st, now, price, o["entry_price"])
            if res == "armed":
                self._log(o["mode"], o["mint"], "surge armed", f"+{st['gain']:.1f}%", f"price={price:.10g}", f"entry={o['entry_price']:.10g}", f"src={src}")
            elif res == "missed":
                if not st.get("missed_logged"):
                    st["missed_logged"] = True
                    self._log(o["mode"], o["mint"], "surge missed", "under entry: left to the normal stop/trail",
                              f"price={price:.10g}", f"entry={o['entry_price']:.10g}", f"peak={st.get('peak') or 0:.10g}", f"src={src}")
            elif res in ("top", "floor") and now >= self._surge_retry.get(o["id"], 0):
                st["src"] = src
                await self.surge_sell(o, st, price, now, floor=(res == "floor"))
        return len(rows)

    async def surge_sell(self, o, st, price, now, floor=False):
        """Market-sell the whole position because it surged then topped (or fell to the profit floor, DECISION 56).
        Never a second sell; if a server-side exit already sold it, leave exit_reason alone and let check_closed
        record the real reason."""
        peak, gain = st.get("peak") or price, st.get("gain") or 0
        drop = 100 * (1 - price / peak) if peak else 0
        reason = SURGE_FLOOR if floor else SURGE_TOP
        if o["mode"] == "PAPER":
            self._surge.pop(o["id"], None)
            return await self._paper_close(o, reason, price, now)
        oid = o["id"]
        if oid in self._settling:
            return                                       # settle / grace swap in progress: next tick
        self._settling.add(oid)
        try:
            o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
            if not o or o["status"] != "open" or o["sell_order_id"]:
                self._surge.pop(oid, None); return
            held = await self.token_balance(o["mint"])
            if held == 0:                                # a server-side exit already sold it
                self._log("LIVE", o["mint"], "surge top", "0 tokens held: server-side exit already fired")
                self._upd(oid, last_check=0)             # watcher reconciles on its next pass
                self._surge.pop(oid, None); return
            self._log("LIVE", o["mint"], reason, f"#{oid}", "trigger", f"price={price:.10g}", f"peak={peak:.10g}",
                      f"entry={o['entry_price']:.10g}", f"src={st.get('src') or 'dex'}")
            res = await self.safe_sell(oid, reason)
            if res == "none_held":
                self._upd(oid, exit_reason=o["exit_reason"], last_check=0)
                self._surge.pop(oid, None); return
            if res == "already":
                self._upd(oid, last_check=0)
                self._surge.pop(oid, None); return
            if res == "failed":
                self._surge_retry[oid] = now + SURGE_RETRY_S
                if not o["repair"] or "surge sell failed" not in (o["repair"] or ""):
                    self._upd(oid, repair=((self.c.db.one("SELECT repair FROM gmgn_orders WHERE id=?", (oid,)) or {}).get("repair") or "") + " surge sell failed")
                    await self.push(f"GMGN · ALERT {o['name']}: it surged and topped, but the market sell failed. "
                                    f"The server-side stops are still on. I'll retry.", kind="gmgn_alert", mint=o["mint"])
                return
            # sent: the server-side exits are no longer needed; cancel them so nothing fires on a later re-buy
            await self._cancel_mint_strategies(o)
            self._upd(oid, last_check=0)                 # watcher confirms the close from the chain next pass
            self._surge.pop(oid, None); self._surge_retry.pop(oid, None)
            if floor:
                up = 100 * (price / o["entry_price"] - 1) if o["entry_price"] else 0
                await self.push(f"GMGN · {self._tag(o)}{o['name']}: surged +{gain:.0f}% then fell to the "
                                f"+{float(self.s['risk.surge_floor_pct']):g}% profit floor ({up:+.1f}% vs entry), market-selling",
                                mint=o["mint"])
            else:
                await self.push(f"GMGN · {self._tag(o)}{o['name']}: surged +{gain:.0f}% then topped "
                                f"(-{drop:.1f}% from peak), market-selling", mint=o["mint"])
        finally:
            self._settling.discard(oid)

    async def _cancel_mint_strategies(self, o):
        """Cancel the position's server-side exits after our own market sell (known id first, then any open one)."""
        done = set()
        sid = o.get("strategy_order_id")
        if sid and sid != "open":
            ok = await self._cancel_strategy(sid, self._strategy_type(o))
            done.add(sid)
            self._log("LIVE", o["mint"], "surge top", f"cancel exits {'ok' if ok else 'failed'}")
        items = []
        for tag in ("STMix", "LimitOrder"):
            try:
                d = await (self.client.strategy_orders(base_token=o["mint"], kind="open") if tag == "STMix"
                           else self.client.strategy_orders(base_token=o["mint"], kind="open", group_tag=tag))
                items += [{**x, "order_type": x.get("order_type") or ("limit_order" if tag == "LimitOrder" else "smart_trade")}
                          for x in (d or {}).get("list") or []]
            except TypeError:
                continue                                 # client without group_tag support (tests)
            except Exception as e:
                self._log("LIVE", o["mint"], "strategy list", tag, str(e)[:160])
        for x in items:
            xid = x.get("order_id") or x.get("id")
            if not xid or xid in done or (x.get("base_token") or x.get("token_address")) not in (o["mint"], None):
                continue
            if (x.get("status") or "") in ("canceled", "cancel", "closed", "finished"):
                continue
            done.add(xid)
            await self._cancel_strategy(xid, x.get("order_type") or "smart_trade")

    # ---- drop leftover server-side take-profit trails (DECISION 80)
    STRIP_RETRY_S = 30.0

    async def strip_server_trails(self) -> int:
        """Replace open LIVE smart_trade strategies that still have profit_stop_trace, keeping the
        same exits_phase and the hard stop (and the drawdown trail in the normal phase). Limit-stop
        positions are left alone (their live GMGN exit is already the -stop_pct stop only).
        Create-new then cancel-old, so the stop is never dropped. Returns how many were updated."""
        rows = self.c.db.rows("SELECT * FROM gmgn_orders WHERE status='open' AND mode='LIVE'")
        n = 0
        now = self.c.clock()
        for o in rows:
            if o["id"] in self._settling or limit_stop_only(o) or not has_profit_stop_trace(o):
                continue
            if now < self._strip_retry.get(o["id"], 0):
                continue
            if await self._strip_server_trail(o):
                n += 1
        return n

    async def _strip_server_trail(self, o) -> bool:
        """True if the stored conditions no longer include profit_stop_trace."""
        oid, s = o["id"], self.s
        phase = o.get("exits_phase") or "normal"
        if phase not in ("grace", "normal"):
            phase = "normal"
        self._settling.add(oid)
        try:
            amount = await self.token_balance(o["mint"])
            if amount == 0:
                return False
            new_conds = conditions(s, phase)
            try:
                new_sid = await self._create_strategy(self._normal_strategy_body(s, o["mint"], amount or None, phase=phase))
            except Exception as e:
                self._log("LIVE", o["mint"], "trail_floor_strip create failed", str(e)[:200])
                self._strip_retry[oid] = self.c.clock() + self.STRIP_RETRY_S
                return False
            cancelled = await self._cancel_strategy(o.get("strategy_order_id"), self._strategy_type(o))
            note = "trail_floor_strip" if cancelled else "trail_floor_strip: new stop on, old cancel failed"
            self._upd(oid, strategy_order_id=new_sid, conditions=json.dumps(new_conds),
                      repair=(o.get("repair") or "") + (" " + note if o.get("repair") else note))
            self._log("LIVE", o["mint"], "trail_floor_strip", f"#{oid}", f"phase={phase}",
                      f"stop kept, profit_stop_trace removed", f"cancelled={cancelled}")
            if cancelled:
                await self.push(f"GMGN · {self._tag(o)}{o['name']}: take-profit trail now only sells at a profit "
                                f"(never below +{s['risk.trail_floor_pct']:g}%). The -{s['risk.stop_pct' if phase == 'normal' else 'risk.grace_stop_pct']:g}% stop is unchanged.",
                                mint=o["mint"])
            return True
        finally:
            self._settling.discard(oid)

    # ---- desk-side trailing take-profit (DECISION 52, profit floor DECISION 80)
    async def trail_loop(self):
        backoff = 0.0
        while True:
            t0 = time.time()
            try:
                await self.grace_extend_once()
                await self.strip_server_trails()
                n = await self.trail_once()
                self.c.hb("gmgn_trail", f"watching={n} armed={len(self._trail_armed)}"
                                        + (f" src={self.fast.note()}" if self.fast is not None and n else ""))
                backoff = 0.0
            except Exception as e:
                import net as _net
                if isinstance(e, _net.RateLimited):
                    backoff = float(self.s["risk.backoff_429_s"])
                self.c.error("gmgn", "trail", str(e)[:300])
                self.c.hb("gmgn_trail", f"error {type(e).__name__}")
            await asyncio.sleep(max(0.5, self.exit_poll_s(TRAIL_POLL_S) - (time.time() - t0)) + backoff)

    def _trail_rows(self):
        rows = self.c.db.rows("SELECT * FROM gmgn_orders WHERE status='open' AND mode='LIVE'")
        return [r for r in rows if r.get("entry_price") and not r.get("exit_reason")]

    async def trail_once(self, pairs=None) -> int:
        """One price check on every open LIVE A position. Returns how many. The take-profit trail
        runs on the desk and only sells at a +trail_floor_pct net after fees (DECISION 81)."""
        rows = self._trail_rows()
        ids = {r["id"] for r in rows}
        for k in list(self._trail_armed | self._trail_seen | set(self._trail_retry) | set(self._trail_peak)
                      | self._trail_below_floor):
            if k not in ids:
                self._trail_armed.discard(k); self._trail_seen.discard(k); self._trail_retry.pop(k, None)
                self._trail_peak.pop(k, None); self._trail_below_floor.discard(k)
        if not rows:
            return 0
        px = await self.exit_prices(rows, pairs)
        s, now = self.s, self.c.clock()
        for o in rows:
            if o["id"] in self._settling:
                continue
            if o["id"] not in px:
                continue
            price, src = px[o["id"]]
            if o["id"] not in self._trail_seen:
                # First sight: never surprise-sell on a stale peak at or above the net floor (restart from now).
                # Under the net floor: keep the peak, stay armed, wait for a bounce (DECISION 81).
                self._trail_seen.add(o["id"])
                seed = max(o.get("peak") or o["entry_price"], price)
                note = ""
                floor_px = trail_net_floor_price(s, o)
                if floor_px is not None and price < floor_px:
                    note = (f"price {price:.10g} under net floor {floor_px:.10g}: holding, peak kept")
                    if seed >= o["entry_price"] * (1 + float(s["risk.trail_start_pct"]) / 100.0):
                        self._trail_below_floor.add(o["id"])
                elif price <= trail_trigger(s, o["entry_price"], seed, floor=floor_px):
                    note = f"old peak {seed:.10g} already {100 * (1 - price / seed):.1f}% back: trail restarts from now"
                    seed = price
                self._trail_peak[o["id"]] = seed
                self._log("LIVE", o["mint"], "desk trail watching", f"#{o['id']}", f"entry={o['entry_price']:.10g}",
                          f"price={price:.10g}", f"peak={seed:.10g}", f"start=+{s['risk.trail_start_pct']:g}%",
                          f"trail={s['risk.trail_pct']:g}%", f"floor=+{s['risk.trail_floor_pct']:g}% net",
                          f"src={src}", note)
            reason, peak, armed = desk_trail_check(s, o, price, now, peak=self._trail_peak.get(o["id"]),
                                                   below_floor=o["id"] in self._trail_below_floor)
            self._trail_peak[o["id"]] = peak
            if peak > (o.get("peak") or 0):
                self._upd(o["id"], peak=peak)
            if armed and o["id"] not in self._trail_armed:
                self._trail_armed.add(o["id"])
                floor_px = trail_net_floor_price(s, o)
                self._log("LIVE", o["mint"], "desk trail armed", f"#{o['id']}", f"peak={peak:.10g}",
                          f"+{100 * (peak / o['entry_price'] - 1):.1f}%",
                          f"sell_below={trail_trigger(s, o['entry_price'], peak, floor=floor_px):.10g}")
            floor_px = trail_net_floor_price(s, o)
            if reason == DESK_TRAIL and now >= self._trail_retry.get(o["id"], 0):
                qsol = await self._trail_sell_quote_sol(o)
                if qsol is not None and not trail_net_ok(s, o, price, quote_sol_out=qsol):
                    self._log("LIVE", o["mint"], "desk trail hold", f"#{o['id']}",
                              f"quote {qsol:.6f} SOL under net floor")
                    reason = None
                    if armed:
                        self._trail_below_floor.add(o["id"])
                else:
                    self._trail_below_floor.discard(o["id"])
            elif armed and floor_px is not None and price < floor_px:
                self._trail_below_floor.add(o["id"])
            elif reason != DESK_TRAIL and floor_px is not None and price >= floor_px and o["id"] in self._trail_below_floor:
                # recovered above a higher trail line: keep the dipped flag only if we still need the bounce exit
                pass
            if reason and now >= self._trail_retry.get(o["id"], 0):
                self._trail_src = src
                await self.desk_sell(o, reason, price, peak, now)
        return len(rows)

    async def _trail_sell_quote_sol(self, o) -> float | None:
        """Expected SOL out for a 100% sell, from a GMGN quote. None if no client, no size, or the quote fails.
        A failed quote does not block a sell — the fee estimate is used instead."""
        if self.client is None or not hasattr(self.client, "quote"):
            return None
        try:
            amount = await self.token_balance(o["mint"])
        except Exception:
            amount = None
        if not amount:
            au = _trail_audit(o)
            try:
                amount = int(au["tok_raw"]) if au.get("tok_raw") else None
            except (TypeError, ValueError):
                amount = None
        if not amount:
            return None
        try:
            q = await self.client.quote(o["mint"], SOL, int(amount), max(int(self.s["gmgn.slippage_pct"]), 25))
            out = int(float((q or {}).get("output_amount") or 0))
        except Exception:
            return None
        if out <= 0:
            return None
        sol = out / LAMPORTS
        sol_in = trail_sol_in(self.s, o)
        # Ignore a stub / probe quote (e.g. FakeClient's 123 lamports, or a $1 price probe).
        if sol_in and sol < sol_in * 0.1:
            return None
        return sol

    async def desk_sell(self, o, reason, price, peak, now):
        """Market-sell 100% for the desk trail / desk backup stop. Same guards as the surge sell: never a second
        sell, never with 0 held (a GMGN stop already fired -> the watcher records that reason)."""
        oid = o["id"]
        if oid in self._settling:
            return
        self._settling.add(oid)
        try:
            o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
            if not o or o["status"] != "open" or o["sell_order_id"]:
                return
            held = await self.token_balance(o["mint"])
            if held is None:
                self._trail_retry[oid] = now + TRAIL_RETRY_S   # balance unknown: never sell blind, retry soon
                return
            if held == 0:
                self._log("LIVE", o["mint"], reason, "0 tokens held: already sold")
                self._upd(oid, last_check=0); return
            res = await self.safe_sell(oid, reason)
            if res in ("none_held", "already"):
                if res == "none_held":
                    self._upd(oid, exit_reason=o["exit_reason"])
                self._upd(oid, last_check=0); return
            if res == "failed":
                self._trail_retry[oid] = now + TRAIL_RETRY_S
                if f"{reason} sell failed" not in (o["repair"] or ""):
                    self._upd(oid, repair=((self.c.db.one("SELECT repair FROM gmgn_orders WHERE id=?", (oid,)) or {}).get("repair") or "") + f" {reason} sell failed")
                    await self.push(f"GMGN · ALERT {o['name']}: {EXIT_LABEL[reason]} hit, but the market sell failed. "
                                    f"The GMGN stop is still on. I'll retry.", kind="gmgn_alert", mint=o["mint"])
                return
            await self._cancel_mint_strategies(o)
            self._upd(oid, last_check=0)
            entry = o["entry_price"]
            if reason == DESK_TRAIL:
                txt = (f"GMGN · {self._tag(o)}{o['name']}: trailing take-profit · peaked +{100 * (peak / entry - 1):.0f}%, "
                       f"now {100 * (price / entry - 1):+.0f}% (-{100 * (1 - price / peak):.1f}% from peak), market-selling")
            else:
                txt = (f"GMGN · {self._tag(o)}{o['name']}: hit the stop ({100 * (price / entry - 1):+.0f}% from entry), "
                       f"market-selling")
            self._log("LIVE", o["mint"], reason, f"#{oid}", f"price={price:.10g}", f"peak={peak:.10g}", f"entry={entry:.10g}",
                      f"src={getattr(self, '_trail_src', 'dex')}")
            await self.push(txt, mint=o["mint"])
        finally:
            self._settling.discard(oid)

    # ---- LIVE close from the chain only (DECISION 42)
    def _buy_sigs(self, o) -> list[str]:
        """Buy signatures for this order: audit['buys'] for adopted multi-leg fills, else the single tx."""
        sigs = []
        raw = o.get("audit")
        if raw:
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
                for b in (data or {}).get("buys") or []:
                    if b.get("sig"):
                        sigs.append(b["sig"])
            except Exception:
                sigs = []
        if not sigs and o.get("tx"):
            sigs = [s.strip() for s in str(o["tx"]).replace(";", ",").split(",") if s.strip()]
        return sigs

    async def buy_delta(self, o):
        """The buy tx's wallet deltas (SOL paid incl. fees/rent, tokens received), or None if not on chain yet.
        Adopted multi-leg buys (audit.buys) are summed so close/P&L uses the full fill."""
        sigs = self._buy_sigs(o)
        if not sigs:
            return None
        parts = []
        for sig in sigs:
            try:
                d = await chain.tx_delta(self.c.net, sig, MAIN_WALLET, o["mint"], self._txc)
            except Exception as e:
                self._log("LIVE", o["mint"], "buy tx", str(e)[:160]); return None
            if not d or d["err"] or d["tok"] <= 0 or d["sol"] >= 0:
                return None
            parts.append(d)
        if len(parts) == 1:
            return parts[0]
        return {"sig": parts[0]["sig"], "ts": min(p["ts"] or 0 for p in parts), "err": None,
                "fee": sum(p.get("fee") or 0 for p in parts), "sol": sum(p["sol"] for p in parts),
                "tok": sum(p["tok"] for p in parts), "dec": parts[0].get("dec"),
                "sigs": [p["sig"] for p in parts]}

    async def strategy_exit(self, o):
        """Which GMGN condition sold it (from the strategy history), as an exit_reason; None if unknown."""
        try:
            d = await self.client.strategy_orders(base_token=o["mint"], kind="history")
        except Exception as e:
            self._log("LIVE", o["mint"], "strategy history", str(e)[:160]); return None
        for x in (d or {}).get("list") or []:
            if o["strategy_order_id"] and x.get("order_id") not in (o["strategy_order_id"], None):
                continue
            if (x.get("base_token") or x.get("token_address")) not in (o["mint"], None):
                continue
            fired = [c for c in x.get("condition_orders") or [] if c.get("status") == "success"]
            kind = (fired[0].get("order_type") if fired else None) or x.get("place_action")
            if kind:
                return LIVE_EXIT.get(kind, f"server-side {kind}")
        # limit_order stops (adopted / fallback, DECISION 52): filled -> the stop sold it; cancelled -> sold by hand
        sid = o.get("strategy_order_id")
        if sid and sid != "open":
            try:
                d = await self.client.strategy_orders(base_token=o["mint"], kind="history", group_tag="LimitOrder")
            except TypeError:
                return None
            except Exception as e:
                self._log("LIVE", o["mint"], "limit history", str(e)[:160]); return None
            for x in (d or {}).get("list") or []:
                if x.get("order_id") != sid:
                    continue
                st = (x.get("status") or x.get("strategy_status") or "").lower()
                if st in ("success", "filled", "finished", "done", "triggered"):
                    return LIVE_EXIT["loss_stop"]
                if st in ("canceled", "cancelled", "cancel"):
                    return "manual"
        return None

    async def check_closed(self, o, now) -> bool:
        """True when this tick handled the order as sold (closed, or 'sold, confirming result').
        Closed ONLY when the on-chain balance is ~0 AND the sell tx(s) are found in the wallet's signatures."""
        buy = await self.buy_delta(o)
        if not buy:
            return False                                 # buy not visible on chain: never infer a close
        if o.get("entry_src") != "chain":
            e = chain.result(buy, [], o["sol_usd"])["entry_price"]
            self._upd(o["id"], entry_price=e, entry_src="chain")
            o = {**o, "entry_price": e, "entry_src": "chain"}
        bal = await chain.token_balance(self.c.net, MAIN_WALLET, o["mint"])
        dust = max(1, buy["tok"] // DUST_DIV)
        if bal is None or bal > dust:
            return False
        try:
            sells = await chain.wallet_sells(self.c.net, MAIN_WALLET, o["mint"], buy["ts"], skip=set(self._buy_sigs(o)), cache=self._txc)
        except Exception as e:
            self._log("LIVE", o["mint"], "wallet sigs", str(e)[:160]); sells = []
        sold = -sum(x["tok"] for x in sells)
        if sells and sold >= (buy["tok"] - bal) * 0.99:
            r = chain.result(buy, sells, o["sol_usd"])
            reason = o["exit_reason"] or await self.strategy_exit(o) or "server-side exit"
            self._upd(o["id"], status="closed", exit_reason=reason, exit_price=r["exit_price"], pnl_usd=r["pnl_usd"],
                      pct=r["pct"], closed_at=r["sell_ts"] or now, sell_tx=r["sell_tx"])
            sign = "+" if r["pnl_usd"] >= 0 else "-"
            head = f"GMGN · {self._tag(o)}{o['name']} final result" if o.get("confirm_push_at") else f"GMGN · {self._tag(o)}{o['name']} sold"
            await self.push(f"{head} ({EXIT_LABEL.get(reason, reason)}) · {sign}${abs(r['pnl_usd']):,.2f} ({r['pct']:+.1f}%)\n"
                            f"On-chain: {r['sol_in']:.4f} SOL in → {r['sol_out']:.4f} SOL out, fees included", mint=o["mint"])
            self._log("LIVE", o["mint"], "closed", reason, r["sell_tx"], f"sol_in={r['sol_in']:.9f}", f"sol_out={r['sol_out']:.9f}")
            self.c.event("exit", o["mint"])
            re = getattr(self.c, "reentry", None)
            if re is not None:
                try:
                    re.arm({**o, "status": "closed", "exit_reason": reason, "closed_at": r["sell_ts"] or now})
                except Exception as e:
                    self.c.error("gmgn", "reentry_arm", f"{o['mint']} {str(e)[:200]}")
            return True
        if not o.get("confirm_push_at"):                 # wallet is empty but the sell tx isn't visible yet
            self._upd(o["id"], status="exiting", confirm_push_at=now)
            await self.push(f"GMGN · {o['name']} sold, confirming result", mint=o["mint"])
            self._log("LIVE", o["mint"], "sold, confirming", f"bal={bal}")
        return True

    async def sell_result(self, o):
        """(exit_price_usd, pnl_usd, pct) from GMGN's report of OUR market sell, or None. SOL in vs SOL out, at the
        buy's SOL price (network fees not included)."""
        if not o["sell_order_id"]:
            return None
        try:
            q = await self.client.query_order(o["sell_order_id"])
        except Exception:
            return None
        rep = (q or {}).get("report") or {}
        if (q or {}).get("status") not in ("confirmed", "successful") or not rep.get("output_amount") or not o["lamports"]:
            return None
        sol_in, sol_out = o["lamports"] / LAMPORTS, int(rep["output_amount"]) / LAMPORTS
        pnl = round((sol_out - sol_in) * (o["sol_usd"] or 0), 2)
        pct = round(100 * (sol_out / sol_in - 1), 2)
        return float(rep.get("price_usd") or 0) or o["last_price"], pnl, pct

    # ---- gate
    def record_gate_rule(self):
        """Owner rule change (2026-11-14 5:42 PM CT): GMGN LIVE after 2h of GMGN PAPER (was 1 full PAPER night). Logged once."""
        if self.c.db.meta("gmgn_gate_rule_logged"):
            return
        self.c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
                    (time.time(), "gmgn.live_gate", json.dumps("1 full PAPER night"), json.dumps("2h PAPER"),
                     "owner via Grok Bot app · " + GATE_RULE))
        self.c.db.set_meta("gmgn_gate_rule_logged", True)

    async def live_check(self) -> str | None:
        """None when GMGN may go LIVE, else the plain reason."""
        s, c = self.s, self.c
        if self.mode() == "LIVE":
            return "GMGN is already LIVE."
        since = self.paper_since()
        need = s["gmgn.live_gate_h"] * 3600
        if since is None or c.clock() - since < need:
            left = need - (c.clock() - (since or c.clock()))
            return f"GMGN LIVE needs {s['gmgn.live_gate_h']:g}h of GMGN PAPER first ({left / 60:.0f} min left)."
        if self.halted():
            return "GMGN is halted. Send /unhalt first."
        if self.client is None or not self.client.configured:
            return "GMGN keys aren't set up."
        sol = await self.sol_usd()
        try:
            await self.client.quote(SOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 1_000_000, 15)
        except GmgnError as e:
            if e.plan_blocked:
                return ("Your GMGN plan (free) can't place trades through the API. "
                        "Upgrade at gmgn.ai/ai (Plus or Pro), then send 'gmgn live' again.")
            return f"GMGN check failed: {e.error or e.status}"
        wallet = await self.wallet_lamports(fresh=True)
        cap = s["gmgn.min_trade_usd"]
        if not sol or wallet is None:
            return "Couldn't read the wallet balance or SOL price."
        need_l = buy_cost_lamports(s, int(cap / sol * LAMPORTS)) + int(s["gmgn.sol_reserve"] * LAMPORTS)
        if wallet < need_l:
            return f"Wallet has {wallet / LAMPORTS:.4f} SOL, one ${cap:,.2f} ticket needs {need_l / LAMPORTS:.4f} SOL."
        return None

    def _next_size(self):
        """' · next $50.00' from the last wallet read (no network call)."""
        ts, wallet = self._bal
        sol = self._sol[1]
        if wallet is None or not sol:
            return ""
        b = budget_lamports(self.s, self.c.db, "A", wallet)
        size, _, why = pick_size(self.s, b["avail"], sol)
        head = (f" · A's {b['sharePct']:g}% share: {b['avail'] / LAMPORTS:.4f} SOL (${b['avail'] / LAMPORTS * sol:,.2f}) free"
                f" of {b['total'] * b['sharePct'] / 100.0 / LAMPORTS:.4f} SOL")
        return head + (f" → next ${size:,.2f}" if size else " → next buy skipped")

    def status_line(self):
        n, used = night_usage(self.c.db, self.c.night, self.mode(), "A")
        s = self.s
        el = (self.c.clock() - (self.paper_since() or self.c.clock())) / 3600
        head = (f"GMGN {self.mode()}{' · HALTED' if self.halted() else ''}{'' if s['gmgn.enabled'] else ' · off'} · ")
        if self.mode() == "PAPER":
            snap = paper_snapshot(s, self.c.db)
            return (head +
                    f"paper bankroll ${snap['bankrollUsd']:,.2f}, free ${snap['freeUsd']:,.2f}, P&L ${snap['pnlUsd']:+,.2f} · "
                    f"size ${snap['tradeUsd']:,.2f}, then whatever paper free covers down to ${s['gmgn.min_trade_usd']:,.2f} · "
                    f"tonight {n}/{s['gmgn.max_tickets_night']} tickets · {self.open_label('PAPER')}" +
                    (f" · PAPER {el:.1f}h of {s['gmgn.live_gate_h']:g}h"))
        try:                                             # DECISION 72: A's profit since the owner's reset (display only)
            import pnl_since
            head += pnl_since.telegram_line(pnl_since.a_live(self.c.db, s, self.c.night), "A") + " · "
        except Exception:
            pass
        return (head +
                f"tonight {n}/{s['gmgn.max_tickets_night']} tickets, ${used:,.2f}/${s['gmgn.cap_trade_usd'] * s['gmgn.max_tickets_night']:,.2f} · "
                f"size ${s['gmgn.cap_trade_usd']:,.2f}, then ${s['gmgn.fallback_trade_usd']:,.2f}, then whatever free SOL covers down to ${s['gmgn.min_trade_usd']:,.2f}{self._next_size()} · "
                f"{self.open_label()}")
