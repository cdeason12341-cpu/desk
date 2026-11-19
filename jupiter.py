"""Jupiter lite-api quotes (DECISION 89): read-only, no key, no wallet. Used to value a position GMGN can't route a
sell for when the desk writes it off. Never signs or sends anything."""
from __future__ import annotations
import asyncio, time
import httpx

QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
SOL = "So11111111111111111111111111111111111111112"
NO_ROUTE_CODES = {"COULD_NOT_FIND_ANY_ROUTE", "NO_ROUTES_FOUND", "TOKEN_NOT_TRADABLE", "ROUTE_PLAN_DOES_NOT_CONSUME_ALL_THE_AMOUNT"}
RPM = 50                         # stay under lite-api's free limit

_http = None
_stamps: list[float] = []


def _client():
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "grokbot-desk/1"})
    return _http


async def _take():
    while True:
        now = time.time()
        _stamps[:] = [t for t in _stamps if now - t < 60]
        if len(_stamps) < RPM:
            _stamps.append(now); return
        await asyncio.sleep(60 - (now - _stamps[0]) + 0.05)


async def _get(params) -> tuple[int, dict]:
    await _take()
    r = await asyncio.wait_for(_client().get(QUOTE_URL, params=params), timeout=12)
    try:
        d = r.json()
    except Exception:
        d = {"error": f"non-json {r.text[:80]}"}
    return r.status_code, d if isinstance(d, dict) else {"error": "bad shape"}


async def quote(input_mint, output_mint, amount: int, slippage_bps: int = 2500) -> dict:
    """{'ok': True, 'out': int, 'impact_pct': float, 'route': 'FluxBeam'} or
    {'ok': False, 'no_route': bool, 'error': str}. no_route=False means Jupiter itself failed (caller fails closed)."""
    if not amount or int(amount) <= 0:
        return {"ok": False, "no_route": False, "error": "zero amount"}
    try:
        st, d = await _get({"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)),
                            "slippageBps": str(int(slippage_bps)), "swapMode": "ExactIn"})
    except Exception as e:
        return {"ok": False, "no_route": False, "error": f"jupiter {type(e).__name__}"}
    code = str(d.get("errorCode") or "")
    err = str(d.get("error") or "")
    if code in NO_ROUTE_CODES or "route" in err.lower() or "not tradable" in err.lower():
        return {"ok": False, "no_route": True, "error": (code or err)[:120]}
    if st != 200 or "outAmount" not in d:
        return {"ok": False, "no_route": False, "error": f"jupiter {st} {(code or err)[:100]}"}
    try:
        out = int(d["outAmount"])
        impact = 100.0 * float(d.get("priceImpactPct") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "no_route": False, "error": "jupiter bad numbers"}
    labels = [((p.get("swapInfo") or {}).get("label") or "?") for p in (d.get("routePlan") or [])]
    if out <= 0:
        return {"ok": False, "no_route": True, "error": "zero out"}
    return {"ok": True, "out": out, "impact_pct": impact, "route": "+".join(labels)}
