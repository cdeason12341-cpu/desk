"""Coin sources beyond pump.fun (owner 2026-11-16 5:22 PM CT, DECISION 51). Pure helpers, no I/O.
scanner.sources = all  -> pump.fun graduations + other launchpads/DEXes; = pump -> pump mints only (old behaviour)."""
from __future__ import annotations
import solana

# Stablecoins, SOL / liquid-staked SOL, wrapped majors and big established tokens: never memecoin candidates.
EXCLUDED = {
    solana.WSOL: "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB": "USD1",
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo": "PYUSD",
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH": "USDG",
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA": "USDS",
    "9zNQRsGLjNKwCUU5Gq5LR8beUCPzQMVMqKAi3SSZh54u": "FDUSD",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "JitoSOL",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": "bSOL",
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": "JupSOL",
    "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm": "INF",
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj": "stSOL",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "WETH",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "WBTC",
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": "cbBTC",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": "WIF",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "RAY",
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn": "PUMP",
}
STABLE_SYMBOLS = {"USDC", "USDT", "USD1", "PYUSD", "USDG", "USDS", "FDUSD", "DAI", "USDE", "USDH", "UXD", "EURC", "SOL", "WSOL"}

# Bonding-curve (pre-graduation) markets. The desk only takes coins once they trade on a real pool, as with pump.fun.
BONDING_DEX_DS = {"pumpfun", "meteoradbc", "launchlab", "raydiumlaunchlab", "moonshot", "moonit", "boop", "heaven", "believe", "bags"}
BONDING_DEX_GECKO = {"pump-fun", "meteora-dbc", "raydium-launchlab", "moonshot", "moonit", "boop-fun", "boop", "heaven", "letsbonk-fun"}
# Graduated pools GeckoTerminal new_pools may show for non-pump coins.
GRADUATED_DEX_GECKO = {"pumpswap", "raydium", "raydium-cpmm", "raydium-clmm", "meteora", "meteora-damm-v2", "meteora-dlmm",
                       "orca", "fluxbeam", "saros-dlmm", "lifinity-v2"}
DUST_RESERVE_USD = 1000.0   # new_pools intake only: a pool under $1K reserve is not admitted (it would be a permanent liq kill)

SUFFIX_LABELS = (("pump", "pump.fun"), ("bonk", "letsbonk"), ("boop", "boop"), ("BAGS", "bags"))   # vanity mint suffixes (case-sensitive)


def pump_only(s) -> bool:
    try:
        return str(s["scanner.sources"]).lower() == "pump"
    except (KeyError, TypeError):
        return False


def excluded(mint: str, symbol: str | None = None) -> str | None:
    if mint in EXCLUDED:
        return EXCLUDED[mint]
    sym = (symbol or "").strip().upper()
    if sym in STABLE_SYMBOLS:
        return sym
    return None


def is_pump(mint: str) -> bool:
    return bool(mint) and mint.endswith("pump")


def launchpad(mint: str, pool_label: str | None = None, dex_id: str | None = None) -> str:
    """Short human label for where the coin came from (Telegram pings, app, logs)."""
    for suf, lbl in SUFFIX_LABELS:
        if mint and mint.endswith(suf):
            return lbl
    pl = (pool_label or "").lower()
    if pl in ("pump-amm",):
        return "pump.fun"
    if pl == "raydium-cpmm":                       # PumpPortal migration stream: LetsBonk graduations go to Raydium CPMM
        return "letsbonk"
    d = (dex_id or pl or "").lower()
    if d.startswith("dex:"):
        d = d[4:]
    if d.startswith("meteora"):
        return "meteora"
    if d.startswith("raydium"):
        return "raydium"
    if d == "pumpswap":
        return "pumpswap"
    return d or "other"


def is_bonding(pair: dict) -> bool:
    return (pair.get("dexId") or "").lower() in BONDING_DEX_DS


def select_other_pair(mine: list[dict]) -> dict | None:
    """Non-pump coin: graduated pool with most liquidity, SOL-quoted preferred (a non-SOL best pool is still
    returned so the scanner's 'not SOL-quoted' kill explains it)."""
    pools = [p for p in mine or [] if not is_bonding(p)]
    if not pools:
        return None
    liq = lambda p: float(((p.get("liquidity") or {}).get("usd")) or 0)
    sol = [p for p in pools if ((p.get("quoteToken") or {}).get("address")) == solana.WSOL]
    return max(sol or pools, key=liq)


def first_pool_at(mine: list[dict]) -> float | None:
    """Oldest pairCreatedAt (epoch s) over ALL of the token's pairs: the token's real age, not its newest pool's."""
    ts = []
    for p in mine or []:
        v = p.get("pairCreatedAt")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            ts.append(v / 1000.0 if v > 10_000_000_000 else v)
    return min(ts) if ts else None


def parse_dex_latest(payload) -> list[str]:
    """token-profiles/latest/v1 or token-boosts/latest/v1 body -> Solana non-pump token addresses (order kept, deduped)."""
    out = []
    for x in payload if isinstance(payload, list) else []:
        if not isinstance(x, dict) or x.get("chainId") != "solana":
            continue
        a = x.get("tokenAddress")
        if a and not is_pump(a) and a not in out and not excluded(a) and solana.is_pubkey(a):
            out.append(a)
    return out
