"""On-chain truth for LIVE GMGN orders (DECISION 42, the 2026-11-15 The Same Coin early close).
A LIVE order is closed only from the chain: the wallet's token balance for the mint and the sell tx(s) that took it.
Exit / P&L come from the wallet's real SOL deltas in the buy and sell txs (fees and rent included)."""
from __future__ import annotations

TX_OPTS = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}


def parse_delta(t: dict | None, wallet: str, mint: str) -> dict | None:
    """Wallet SOL delta (lamports) and token delta (raw) for one parsed transaction. None = not available yet."""
    if not t or not t.get("meta"):
        return None
    m = t["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in t["transaction"]["message"]["accountKeys"]]
    if wallet not in keys:
        return None
    i = keys.index(wallet)
    pre = {"amt": 0, "dec": None}; post = {"amt": 0, "dec": None}
    for side, arr in ((pre, m.get("preTokenBalances") or []), (post, m.get("postTokenBalances") or [])):
        for b in arr:
            if b.get("owner") == wallet and b.get("mint") == mint:
                side["amt"] += int(b["uiTokenAmount"]["amount"])
                side["dec"] = int(b["uiTokenAmount"].get("decimals") or 0)
    sig = (t["transaction"].get("signatures") or [None])[0]
    return {"sig": sig, "ts": t.get("blockTime"), "err": m.get("err"), "fee": m.get("fee"),
            "sol": m["postBalances"][i] - m["preBalances"][i], "tok": post["amt"] - pre["amt"],
            "dec": post["dec"] if post["dec"] is not None else pre["dec"]}


async def tx_delta(net, sig, wallet, mint, cache: dict | None = None) -> dict | None:
    key = (sig, mint)
    if cache is not None and key in cache:
        return cache[key]
    t = await net.rpc("getTransaction", [sig, TX_OPTS])
    d = parse_delta(t, wallet, mint)
    if d is not None:
        d["sig"] = d["sig"] or sig
        if cache is not None:
            cache[key] = d                               # only cache what the chain has (null = not landed yet)
    return d


async def token_balance(net, wallet, mint) -> int | None:
    """Raw on-chain balance (all token accounts of the wallet for this mint). None = RPC failed (unknown, never 0)."""
    try:
        r = await net.rpc("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        vals = (r or {}).get("value") if isinstance(r, dict) else None
        if vals is None:
            return None
        return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in vals)
    except Exception:
        return None


async def wallet_sells(net, wallet, mint, since_ts, skip=(), cache=None, limit=40) -> list[dict]:
    """Successful wallet txs since the buy that moved this mint OUT of the wallet, oldest first."""
    sigs = await net.rpc("getSignaturesForAddress", [wallet, {"limit": limit, "commitment": "confirmed"}])
    out = []
    for s in sigs or []:
        sig = s.get("signature")
        if not sig or sig in skip or s.get("err") or (s.get("blockTime") or 0) < (since_ts or 0) - 5:
            continue
        d = await tx_delta(net, sig, wallet, mint, cache)
        if d and not d["err"] and d["tok"] < 0:
            out.append(d)
    return sorted(out, key=lambda d: d["ts"] or 0)


def result(buy: dict, sells: list[dict], sol_usd: float) -> dict:
    """Real trade numbers. sol_in includes the buy tx's fees and token-account rent; sol_out is after sell fees."""
    dec = buy.get("dec") or 6
    sol_in = -buy["sol"] / 1e9
    tok_in = buy["tok"] / 10 ** dec
    sol_out = sum(s["sol"] for s in sells) / 1e9
    tok_out = -sum(s["tok"] for s in sells) / 10 ** dec
    entry = sol_in / tok_in * sol_usd if tok_in else None
    exit_ = sol_out / tok_out * sol_usd if tok_out else None
    return {"sol_in": sol_in, "sol_out": sol_out, "tok_in": tok_in, "tok_out": tok_out,
            "entry_price": entry, "exit_price": exit_, "pnl_usd": round((sol_out - sol_in) * sol_usd, 2),
            "pct": round(100 * (sol_out / sol_in - 1), 2) if sol_in else None,
            "sell_tx": sells[-1]["sig"] if sells else None, "sell_ts": sells[-1]["ts"] if sells else None}
