"""HTTP clients: DexScreener, RugCheck, Solana RPC, Jev. Errors are explicit; nothing is invented."""
from __future__ import annotations
import asyncio, json, time
import httpx


class RateLimited(Exception):
    pass


class SourceError(Exception):
    pass


class MinuteBucket:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self.stamps: list[float] = []

    async def take(self):
        while True:
            now = time.time()
            self.stamps = [t for t in self.stamps if now - t < 60]
            if len(self.stamps) < self.per_min:
                self.stamps.append(now)
                return
            await asyncio.sleep(60 - (now - self.stamps[0]) + 0.05)


class Net:
    def __init__(self, settings, log=None):
        self.s = settings
        self.http = httpx.AsyncClient(timeout=20, headers={"User-Agent": "grokbot-desk/1"})
        self.dex_bucket = MinuteBucket(int(settings["scanner.max_dex_rpm"]))
        self.rpc_sem = asyncio.Semaphore(4)
        self.rug_sem = asyncio.Semaphore(2)
        self.log = log or (lambda *a, **k: None)
        self.dex_calls = 0

    async def close(self):
        await self.http.aclose()

    # ---- DexScreener
    async def dex_tokens(self, mints: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            await self.dex_bucket.take()
            self.dex_calls += 1
            r = await self.http.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk))
            if r.status_code == 429:
                raise RateLimited("dexscreener 429")
            if r.status_code != 200:
                raise SourceError(f"dexscreener {r.status_code}")
            d = r.json()
            if isinstance(d, list):
                out.extend(d)
        return out

    async def gecko_new_pools(self, page: int = 1) -> dict:
        """Recent Solana pools (GeckoTerminal public API). Used only as a WS-gap backstop."""
        r = await self.http.get(
            "https://api.geckoterminal.com/api/v2/networks/solana/new_pools",
            params={"page": page},
            headers={"Accept": "application/json"},
        )
        if r.status_code == 429:
            raise RateLimited("gecko 429")
        if r.status_code != 200:
            raise SourceError(f"gecko new_pools {r.status_code}")
        d = r.json()
        return d if isinstance(d, dict) else {"data": []}

    # ---- RugCheck
    async def rug_report(self, mint):
        return await self._rug(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")

    async def rug_graph(self, mint):
        return await self._rug(f"https://api.rugcheck.xyz/v1/tokens/{mint}/insiders/graph")

    async def _rug(self, url):
        async with self.rug_sem:
            r = await self.http.get(url, timeout=20)
        if r.status_code == 429:
            raise RateLimited("rugcheck 429")
        if r.status_code != 200:
            raise SourceError(f"rugcheck {r.status_code}")
        return r.json()

    # ---- Solana RPC
    async def rpc(self, method, params, timeout=30):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with self.rpc_sem:
            r = await self.http.post(self.s["desk.rpc_url"], json=body, timeout=timeout)
        if r.status_code == 429:
            raise RateLimited(f"rpc 429 {method}")
        try:
            d = r.json()
        except Exception:
            raise SourceError(f"rpc {r.status_code} {method}")
        if "error" in d:
            e = d["error"]
            if isinstance(e, dict) and e.get("code") == 429:
                raise RateLimited(f"rpc 429 {method}")
            raise SourceError(f"rpc error {method}: {json.dumps(e)[:200]}")
        return d.get("result")

    async def token_accounts_for_mint(self, mint, program, timeout=60):
        filters = [{"memcmp": {"offset": 0, "bytes": mint}}]
        if program.startswith("Tokenkeg"):
            filters.insert(0, {"dataSize": 165})
        return await self.rpc("getProgramAccounts", [program, {"encoding": "jsonParsed", "filters": filters}], timeout=timeout)

    # ---- Jev
    async def jev(self, key, state, questions, timeout):
        r = await self.http.post("https://api.typesafe.ai/v1/systemone",
                                 headers={"Authorization": f"Bearer {key}"},
                                 json={"model": "jev-latest", "state": state, "questions": questions}, timeout=timeout)
        if r.status_code == 429:
            raise RateLimited("jev 429")
        if r.status_code != 200:
            raise SourceError(f"jev http {r.status_code}")
        return r.json()
