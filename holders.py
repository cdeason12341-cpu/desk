"""Holder math from getProgramAccounts (jsonParsed) results. Replaces getTokenLargestAccounts
(disabled on the public RPC). Used by Scanner step 3, Auditor layers 4 and 8."""
from __future__ import annotations
from collections import defaultdict
import solana


async def fetch_accounts(net, mint: str, timeout=60) -> list[dict]:
    """All token accounts of this mint under Token AND Token-2022."""
    out = []
    for prog in (solana.TOKEN_PROGRAM, solana.TOKEN_2022_PROGRAM):
        res = await net.token_accounts_for_mint(mint, prog, timeout=timeout)
        for a in res or []:
            info = (((a.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            amt = int(((info.get("tokenAmount") or {}).get("amount")) or 0)
            out.append({"pubkey": a.get("pubkey"), "owner": info.get("owner"), "amount": amt,
                        "delegate": info.get("delegate"), "program": prog})
    return out


# Pool-vault owners that are a program-wide authority, not the pool address (DECISION 51: non-pump DEX pools).
# They only ever hold AMM reserves, so excluding them from holder math is always correct.
AMM_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",   # Raydium AMM v4 authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",   # Raydium CPMM authority (LetsBonk graduations)
    "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",   # Meteora DAMM v2 pool authority
}


def excluded_owners(pool: str | None, mint: str, known_amm: list[str] | None = None) -> set[str]:
    ex = set(known_amm or []) | AMM_AUTHORITIES
    if pool:
        ex.add(pool)
    bc = solana.bonding_curve(mint) if solana.is_pubkey(mint) else None
    if bc:
        ex.add(bc)
    return ex


def top10_pct(accounts: list[dict], supply_raw: int, exclude: set[str]) -> float | None:
    if not supply_raw:
        return None
    by_owner = defaultdict(int)
    for a in accounts:
        if a["amount"] > 0 and a["owner"] not in exclude and a["pubkey"] not in exclude:
            by_owner[a["owner"]] += a["amount"]
    top = sorted(by_owner.values(), reverse=True)[:10]
    return round(100.0 * sum(top) / supply_raw, 2)


def holder_count(accounts: list[dict]) -> int:
    return sum(1 for a in accounts if a["amount"] > 0)


def delegate_groups(accounts: list[dict]) -> list[tuple[str, int]]:
    g = defaultdict(int)
    for a in accounts:
        if a.get("delegate") and a["amount"] > 0:
            g[a["delegate"]] += 1
    return sorted(g.items(), key=lambda kv: -kv[1])
