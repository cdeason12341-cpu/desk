"""Real-time token price straight from the pool's on-chain state (DECISION 57).
DexScreener lagged the pool by 20-80 s on Petryk The Hare (2026-11-16), so the GMGN exit watchers (gmgn_surge,
gmgn_trail) read open LIVE positions' pools over RPC: one getMultipleAccounts call per poll for every open mint.

Supported pool programs (layouts verified on live pools 2026-11-16, see DECISIONS 57):
  pump.fun bonding curve (virtual reserves), PumpSwap (vault balances), Raydium AMM v4 (vaults - need_take_pnl),
  Raydium CPMM (vaults - protocol/fund/creator fees), Raydium CLMM + PancakeSwap CLMM fork (sqrt_price_x64),
  Orca Whirlpool (sqrt_price), Meteora DLMM (active bin), Meteora DAMM v2 (sqrt_price).
Anything else (Meteora DAMM v1, DBC, Fluxbeam, ...) is 'unsupported' and the caller falls back.
Read-only: this module never signs or sends a transaction."""
from __future__ import annotations
import base64, struct
from solana import b58encode, bonding_curve, PUMP_PROGRAM, WSOL

PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAY_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAY_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAY_CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
PANCAKE_CLMM = "HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq"
ORCA_WP = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
MET_DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
MET_DAMM2 = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
USD_MINTS = (USDC, USDT)

KIND_BY_PROGRAM = {PUMP_PROGRAM: "pump_curve", PUMP_AMM: "pumpswap", RAY_V4: "raydium_v4", RAY_CPMM: "raydium_cpmm",
                   RAY_CLMM: "clmm", PANCAKE_CLMM: "clmm", ORCA_WP: "whirlpool", MET_DLMM: "dlmm", MET_DAMM2: "damm_v2"}

# (mint_a offset, mint_b offset, vault_a offset, vault_b offset) of each pool account
LAYOUT = {"pumpswap": (43, 75, 139, 171), "raydium_v4": (400, 432, 336, 368), "raydium_cpmm": (168, 200, 72, 104),
          "clmm": (73, 105, 137, 169), "whirlpool": (101, 181, 133, 213), "dlmm": (88, 120, 152, 184),
          "damm_v2": (168, 200, 232, 264)}
MIN_LEN = {"pump_curve": 49, "pumpswap": 203, "raydium_v4": 464, "raydium_cpmm": 381, "clmm": 269, "whirlpool": 245,
           "dlmm": 216, "damm_v2": 472}
VAULT_KINDS = ("pumpswap", "raydium_v4", "raydium_cpmm")      # price from vault balances (constant product)


class Unsupported(Exception):
    pass


def b64(acc) -> bytes | None:
    """Raw bytes of a getMultipleAccounts / getAccountInfo base64 account value (None = account missing)."""
    if not acc:
        return None
    d = acc.get("data")
    if isinstance(d, list):
        d = d[0]
    return base64.b64decode(d) if isinstance(d, str) else None


def pk(d: bytes, off: int) -> str:
    return b58encode(d[off:off + 32])


def u64(d: bytes, off: int) -> int:
    return struct.unpack_from("<Q", d, off)[0]


def u128(d: bytes, off: int) -> int:
    lo, hi = struct.unpack_from("<QQ", d, off)
    return lo | (hi << 64)


def token_amount(d: bytes | None) -> int | None:
    """SPL Token / Token-2022 account amount (u64 at offset 64)."""
    if not d or len(d) < 72:
        return None
    return u64(d, 64)


def mint_decimals(d: bytes | None) -> int | None:
    if not d or len(d) < 45:
        return None
    return d[44]


def describe(pool_addr: str, owner: str, data: bytes, mint: str) -> dict:
    """Pool spec from the pool account (fetched once per position). Raises Unsupported.
    spec = {kind, pool, mint, quote, a_is_mint, accounts (fetched every poll), vault_a, vault_b}."""
    kind = KIND_BY_PROGRAM.get(owner)
    if kind is None:
        raise Unsupported(f"program {owner[:8]}")
    if len(data) < MIN_LEN[kind]:
        raise Unsupported(f"{kind} account too short ({len(data)})")
    if kind == "pump_curve":
        if data[48]:
            raise Unsupported("pump curve complete (migrated)")
        if curve_quote(data) != WSOL:
            raise Unsupported(f"pump curve quote {curve_quote(data)[:6]} not SOL")
        return {"kind": kind, "pool": pool_addr, "mint": mint, "quote": WSOL, "a_is_mint": True,
                "accounts": [pool_addr], "dec_mint": 6, "dec_quote": 9}
    ma, mb, va, vb = LAYOUT[kind]
    a, b = pk(data, ma), pk(data, mb)
    if mint not in (a, b):
        raise Unsupported(f"{kind} pool does not hold this mint")
    a_is_mint = a == mint
    quote = b if a_is_mint else a
    if quote != WSOL and quote not in USD_MINTS:
        raise Unsupported(f"{kind} quote {quote[:6]} not SOL/USD")
    spec = {"kind": kind, "pool": pool_addr, "mint": mint, "quote": quote, "a_is_mint": a_is_mint,
            "vault_a": pk(data, va), "vault_b": pk(data, vb), "mint_a": a, "mint_b": b}
    spec["accounts"] = [pool_addr, spec["vault_a"], spec["vault_b"]] if kind in VAULT_KINDS else [pool_addr]
    if kind == "raydium_v4":                                    # decimals live in the pool (u64 at 32 / 40)
        spec["dec_a"], spec["dec_b"] = u64(data, 32), u64(data, 40)
    elif kind in ("raydium_cpmm",):
        spec["dec_a"], spec["dec_b"] = data[331], data[332]
    elif kind == "clmm":
        spec["dec_a"], spec["dec_b"] = data[233], data[234]
    return spec


def needs_decimals(spec) -> list[str]:
    return [m for m, k in ((spec.get("mint_a"), "dec_a"), (spec.get("mint_b"), "dec_b")) if m and k not in spec]


def _sqrt_price(x: int, frac_bits: int = 64) -> float:
    return (x / float(1 << frac_bits)) ** 2


def price_in_quote(spec: dict, datas: list) -> float:
    """Price of 1 whole token in the quote asset (SOL or USD) from this poll's account bytes (same order as
    spec['accounts']). Raises Unsupported on missing / unusable data."""
    kind = spec["kind"]
    pool = datas[0] if datas else None
    if pool is None:
        raise Unsupported("pool account missing")
    if kind == "pump_curve":
        if pool[48]:
            raise Unsupported("pump curve complete (migrated)")
        vtr, vsr = u64(pool, 8), u64(pool, 16)
        if not vtr:
            raise Unsupported("empty curve")
        return (vsr / 1e9) / (vtr / 1e6)
    da, db = spec.get("dec_a"), spec.get("dec_b")
    if da is None or db is None:
        raise Unsupported("decimals unknown")
    if kind in VAULT_KINDS:
        ra, rb = token_amount(datas[1]), token_amount(datas[2])
        if ra is None or rb is None:
            raise Unsupported("vault missing")
        if kind == "pumpswap" and len(pool) >= 253:
            # Signed quote-reserve adjustment (i64 @245, after coin_creator + 2 flag bytes). Not in the old IDL; found
            # 2026-11-16: quote vault + this matches executed trades / GeckoTerminal / DexScreener (e.g. +17.58 SOL on
            # 51KN4u: 4.74e-08 raw vs 7.8e-08 traded), and small negative values fix thin pools too.
            adj = struct.unpack_from("<q", pool, 245)[0]
            if spec["a_is_mint"]:
                rb += adj
            else:
                ra += adj
        if kind == "raydium_v4":
            ra -= u64(pool, 192); rb -= u64(pool, 200)            # base/quote need_take_pnl belong to the protocol
        elif kind == "raydium_cpmm":
            ra -= u64(pool, 341) + u64(pool, 357); rb -= u64(pool, 349) + u64(pool, 365)   # protocol + fund fees
            if len(pool) >= 413:                                   # newer pools: creator fees accrued in the vaults
                ra -= u64(pool, 397); rb -= u64(pool, 405)
        if ra <= 0 or rb <= 0:
            raise Unsupported("empty reserves")
        p_ba = (rb / 10 ** db) / (ra / 10 ** da)                   # token b per token a
    elif kind in ("clmm",):
        p_ba = _sqrt_price(u128(pool, 253)) * 10 ** (da - db)
    elif kind == "whirlpool":
        p_ba = _sqrt_price(u128(pool, 65)) * 10 ** (da - db)
    elif kind == "damm_v2":
        p_ba = _sqrt_price(u128(pool, 456)) * 10 ** (da - db)
    elif kind == "dlmm":
        active = struct.unpack_from("<i", pool, 76)[0]
        step = struct.unpack_from("<H", pool, 80)[0]
        p_ba = (1 + step / 10000.0) ** active * 10 ** (da - db)
    else:
        raise Unsupported(kind)
    if p_ba <= 0:
        raise Unsupported("zero price")
    return p_ba if spec["a_is_mint"] else 1.0 / p_ba


def curve_quote(d: bytes) -> str:
    """Quote mint of a pump.fun bonding curve: newer curves can be quoted in another token (pubkey @83, after
    creator @49 and two flag bytes); all-zero there = SOL."""
    if len(d) >= 115 and any(d[83:115]):
        return pk(d, 83)
    return WSOL


def curve_address(mint: str) -> str | None:
    return bonding_curve(mint)
