"""Dev-exit timing for auditor layer 3 (owner rule 2026-11-15 12:48 CT).
'If the dev is already out and has been out for more than a minute and the coin is still steady we don't worry about it.'
Last dev sell = newest tx on the dev's associated token account (ATA) for this mint where the dev's balance of the
mint went down (from getTransaction pre/postTokenBalances, owner == dev). Its blockTime is the sell time."""
from __future__ import annotations
import solana

ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
DEV_OUT_PCT = 0.1          # dev holding below this % of supply counts as fully out (dust stays behind)
SIG_LIMIT = 10             # newest signatures on the ATA to look at
TX_LIMIT = 5               # getTransaction calls at most
PRICE_WINDOW_S = 180       # desk price sample must be within this of the sell time
CRASH_H1_PCT = -50         # fallback: h1 at or below this is a crash


def ata(owner: str, mint: str, program: str) -> str | None:
    try:
        return solana.find_program_address([solana.b58decode(owner), solana.b58decode(program), solana.b58decode(mint)], ATA_PROGRAM)
    except Exception:
        return None


def owner_balance(balances, mint, owner) -> int:
    tot = 0
    for b in balances or []:
        if isinstance(b, dict) and b.get("mint") == mint and b.get("owner") == owner:
            try:
                tot += int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            except (TypeError, ValueError):
                pass
    return tot


async def last_dev_sell(net, creator: str, mint: str, program: str) -> tuple[float | None, str]:
    """(blockTime of the dev's last balance decrease, note). None = unknown (never a pass)."""
    addr = ata(creator, mint, program)
    if not addr:
        return None, "no dev token account"
    sigs = await net.rpc("getSignaturesForAddress", [addr, {"limit": SIG_LIMIT}])
    if not sigs:
        return None, "no dev token account history"
    looked = 0
    for s in sigs:
        if not isinstance(s, dict) or s.get("err") is not None:
            continue
        if looked >= TX_LIMIT:
            break
        looked += 1
        tx = await net.rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        meta = (tx or {}).get("meta") or {}
        pre = owner_balance(meta.get("preTokenBalances"), mint, creator)
        post = owner_balance(meta.get("postTokenBalances"), mint, creator)
        if post < pre:
            bt = (tx or {}).get("blockTime") or s.get("blockTime")
            return (float(bt) if bt else None), ("ok" if bt else "sell has no blockTime")
    return None, f"no dev sell in last {looked} tx"


def price_near(rows, ts: float) -> float | None:
    """rows: [(ts, price)]. Closest desk price sample to ts within PRICE_WINDOW_S, else None."""
    best = None
    for t, p in rows or []:
        if p is None or p <= 0:
            continue
        d = abs(t - ts)
        if d <= PRICE_WINDOW_S and (best is None or d < best[0]):
            best = (d, float(p))
    return best[1] if best else None


def ago(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 120 else f"{sec // 60}m" if sec < 7200 else f"{sec / 3600:.1f}h"


def judge(s, since_s: float, sell_price, now_price, m5, h1) -> tuple[bool, str]:
    """Pure. (passes, layer text). Dev already out; since_s = seconds since the last dev sell."""
    wait, drop = float(s["auditor.dev_exit_wait_s"]), float(s["auditor.dev_exit_max_drop_pct"])
    if since_s <= wait:
        return False, f"dev selling (last sell {ago(since_s)} ago)"
    if sell_price and now_price:
        ch = (float(now_price) / float(sell_price) - 1) * 100.0
        txt = f"dev out {ago(since_s)} ago, price {ch:+.0f}% since"
        return (ch >= -drop), txt
    if m5 is None:
        return False, f"dev out {ago(since_s)} ago, no price data"
    m5, h1v = float(m5), (None if h1 is None else float(h1))
    txt = f"dev out {ago(since_s)} ago, m5 {m5:+.0f}%" + ("" if h1v is None else f" h1 {h1v:+.0f}%") + " (no sell price)"
    ok = m5 > -drop and (h1v is None or h1v > CRASH_H1_PCT)
    return ok, txt
