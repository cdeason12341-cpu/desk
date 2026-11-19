import asyncio, time
import config
from bus import Bus
from ctx import Ctx, NullNotifier
from db import DB

SOL = "So11111111111111111111111111111111111111112"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeNet:
    def __init__(self):
        self.pairs = {}
        self.jev_resp = None
        self.rpc_map = {}
        self.new_pools = {}

    async def dex_tokens(self, mints):
        return [self.pairs[m] for m in mints if m in self.pairs]

    async def gecko_new_pools(self, page=1):
        return self.new_pools.get(page, {"data": []})

    async def dex_search(self, q):
        return list(getattr(self, "search", {}).get(q, []))

    async def rpc(self, method, params, timeout=30):
        fn = self.rpc_map.get(method)
        if fn is None:
            raise RuntimeError("no fake for " + method)
        return fn(params) if callable(fn) else fn

    async def token_accounts_for_mint(self, mint, program, timeout=60):
        return []

    async def rug_report(self, mint):
        return {}

    async def rug_graph(self, mint):
        return []

    async def jev(self, key, state, questions, timeout):
        return self.jev_resp


def pair(mint, price=1.0, liq=50000, h1=10.0, m5=-1.0, vol_h1=30000, buys_h1=200, buys_m5=10, sells_m5=5, name="Test", symbol="TST"):
    return {"chainId": "solana", "dexId": "pumpswap", "pairAddress": "Pool" + mint[:8],
            "baseToken": {"address": mint, "name": name, "symbol": symbol},
            "quoteToken": {"address": SOL, "symbol": "SOL"}, "priceUsd": str(price),
            "txns": {"m5": {"buys": buys_m5, "sells": sells_m5}, "h1": {"buys": buys_h1, "sells": 50}},
            "volume": {"m5": 1000, "h1": vol_h1, "h24": vol_h1}, "priceChange": {"m5": m5, "h1": h1, "h24": h1},
            "liquidity": {"usd": liq}, "marketCap": 200000, "pairCreatedAt": 0}


def make_ctx(**settings):
    settings.setdefault("gmgn.dip_enabled", False)   # existing buy tests stay immediate; dip tests opt in
    db = DB(":memory:")
    s = config.Settings(db)
    for k, v in settings.items():
        s.set(k, v, source="test", force=True)
    clock = Clock()
    c = Ctx(db, s, FakeNet(), Bus(), NullNotifier(), log_files=False, clock=clock)
    return c, clock


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)
