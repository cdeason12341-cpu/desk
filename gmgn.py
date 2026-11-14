"""GMGN OpenAPI client (Agent API). Mirrors the official gmgn-cli 1.7.2 client (src/client/OpenApiClient.ts + signer.ts).

Auth: X-APIKEY header + query timestamp (unix s) + client_id (fresh uuid4 per attempt).
Signed routes add X-Signature = base64(Ed25519(msg)), msg = "{sub_path}:{sorted_query}:{body}:{timestamp}",
sorted_query = all query params incl. timestamp/client_id, sorted by key, encodeURIComponent'd, joined by '&'.
IPv4 only; Cloudflare rejects default python UAs (1010) so the UA is the CLI's.
Keys come from .secrets (GMGN_API_KEY, GMGN_SIGNING_KEY) and are never printed, logged or returned.
Never retries a POST (swap / strategy create / cancel are real transactions)."""
from __future__ import annotations
import asyncio, base64, json, time, uuid
from urllib.parse import quote, urlencode
import httpx
import secrets_store

HOST = "https://openapi.gmgn.ai"
UA = "gmgn-cli/1.7.2"
CHAIN = "sol"
SOL = "So11111111111111111111111111111111111111112"
MAIN_WALLET = "JAP5hC5dxCz8Q9kKXMshRKEwoN7fXX3JFpkKQ7Mo5zC2"   # owner-confirmed main wallet (2026-11-14). The only --from ever used.
LAMPORTS = 1_000_000_000


class GmgnError(RuntimeError):
    def __init__(self, status, code=None, error=None, message="", reset_at=None, upgrade_url=None):
        self.status, self.code, self.error, self.reset_at, self.upgrade_url = status, code, error, reset_at, upgrade_url
        super().__init__(secrets_store.redact(f"gmgn {status} {error or code or ''}: {message}"[:400]))

    @property
    def plan_blocked(self):
        return self.error == "PLAN_UPGRADE_REQUIRED"


class WalletRefused(RuntimeError):
    pass


def enc(s) -> str:
    """JS encodeURIComponent."""
    return quote(str(s), safe="-_.!~*'()")


def build_message(sub_path: str, query: dict, body: str, timestamp: int) -> str:
    parts = []
    for k in sorted(query):
        v = query[k]
        if isinstance(v, (list, tuple)):
            parts += [f"{enc(k)}={enc(i)}" for i in sorted(str(x) for x in v)]
        else:
            parts.append(f"{enc(k)}={enc(v if not isinstance(v, bool) else str(v).lower())}")
    return f"{sub_path}:{'&'.join(parts)}:{body}:{timestamp}"


def sign(message: str, pem: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    msg = message.encode("utf-8")
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return base64.b64encode(key.sign(msg)).decode()
    if isinstance(key, rsa.RSAPrivateKey):
        return base64.b64encode(key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())).decode()
    raise ValueError("unsupported key type (Ed25519 or RSA only)")


def dumps(body) -> str:
    """JSON.stringify equivalent (compact, insertion order)."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def _qs(query: dict) -> str:
    items = []
    for k, v in query.items():
        if isinstance(v, (list, tuple)):
            items += [(k, str(i)) for i in v]
        else:
            items.append((k, str(v).lower() if isinstance(v, bool) else str(v)))
    return urlencode(items)


class Gmgn:
    def __init__(self, api_key=None, pem=None, transport=None, clock=time.time):
        self._key = api_key if api_key is not None else secrets_store.get("GMGN_API_KEY")
        self._pem = pem if pem is not None else (secrets_store.get("GMGN_SIGNING_KEY") or "")
        if self._pem and not self._pem.endswith("\n"):
            self._pem += "\n"
        self.clock = clock
        self.http = httpx.AsyncClient(transport=transport or httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=0),
                                      timeout=20, headers={"User-Agent": UA})

    @property
    def configured(self) -> bool:
        return bool(self._key and self._pem)

    def __repr__(self):
        return "<Gmgn client>"          # never show keys

    def prepare(self, method, sub_path, query, body_str, signed):
        ts = int(self.clock())
        q = dict(query or {})
        q.update(timestamp=ts, client_id=str(uuid.uuid4()))
        headers = {"X-APIKEY": self._key or "", "Content-Type": "application/json", "User-Agent": UA}
        if signed:
            if not self._pem:
                raise GmgnError(0, error="NO_SIGNING_KEY", message="signing key missing")
            headers["X-Signature"] = sign(build_message(sub_path, q, body_str or "", ts), self._pem)
        return f"{HOST}{sub_path}?{_qs(q)}", headers, q

    async def request(self, method, sub_path, query=None, body=None, signed=False):
        body_str = dumps(body) if body is not None else ("" if signed else None)
        attempts = 2 if method == "GET" else 1             # never retry a POST
        for attempt in range(attempts):
            url, headers, _ = self.prepare(method, sub_path, query, body_str, signed)
            try:
                r = await self.http.request(method, url, headers=headers, content=(body_str or None) if method == "POST" else None)
            except Exception as e:
                raise GmgnError(0, error="NETWORK", message=f"{method} {sub_path} {type(e).__name__}")
            try:
                d = r.json()
            except Exception:
                raise GmgnError(r.status_code, error="NON_JSON", message=f"{method} {sub_path} {r.text[:120]}")
            if r.status_code == 200 and d.get("code") == 0:
                return d.get("data")
            err = GmgnError(r.status_code, d.get("code"), d.get("error"), f"{method} {sub_path} {d.get('message', '')}",
                            d.get("reset_at") or r.headers.get("x-ratelimit-reset"), d.get("upgrade_url"))
            if r.status_code == 429 and attempt + 1 < attempts and d.get("error") == "RATE_LIMIT_EXCEEDED":
                try:
                    wait = max(0.0, float(err.reset_at) - self.clock()) + 1
                except Exception:
                    wait = 6
                if wait <= 6:
                    await asyncio.sleep(wait)
                    continue
            raise err
        raise GmgnError(0, error="RETRY_EXIT")

    # ---- read routes
    async def user_info(self):
        return await self.request("GET", "/v1/user/info")

    async def gas_price(self):
        return await self.request("GET", "/v1/trade/gas_price", {"chain": CHAIN})

    async def quote(self, input_token, output_token, amount: int, slippage):
        return await self.request("GET", "/v1/trade/quote", {"chain": CHAIN, "from_address": MAIN_WALLET, "input_token": input_token,
                                                             "output_token": output_token, "input_amount": str(int(amount)), "slippage": slippage})

    async def query_order(self, order_id):
        return await self.request("GET", "/v1/trade/query_order", {"order_id": order_id, "chain": CHAIN}, signed=True)

    async def strategy_orders(self, base_token=None, kind="open"):
        q = {"chain": CHAIN, "group_tag": "STMix", "type": kind, "from": MAIN_WALLET}
        if base_token:
            q["base_token"] = base_token
        return await self.request("GET", "/v1/trade/strategy/orders", q, signed=True)

    async def wallet_holdings(self):
        return await self.request("GET", "/v1/user/wallet_holdings", {"chain": CHAIN, "wallet_address": MAIN_WALLET}, signed=True)

    # ---- write routes (real transactions). Only the main wallet.
    @staticmethod
    def _check_wallet(body):
        if body.get("from_address") != MAIN_WALLET:
            raise WalletRefused("refused: only the owner's main GMGN wallet may trade")

    async def swap(self, body: dict):
        self._check_wallet(body)
        return await self.request("POST", "/v1/trade/swap", body=body, signed=True)

    async def strategy_create(self, body: dict):
        self._check_wallet(body)
        return await self.request("POST", "/v1/trade/strategy/create", body=body, signed=True)

    async def strategy_cancel(self, body: dict):
        self._check_wallet(body)
        return await self.request("POST", "/v1/trade/strategy/cancel", body=body, signed=True)
