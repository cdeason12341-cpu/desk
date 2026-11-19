"""GMGN OpenAPI client (Agent API). Mirrors the official gmgn-cli 1.7.2 client (src/client/OpenApiClient.ts + signer.ts).

Auth: X-APIKEY header + query timestamp (unix s) + client_id (fresh uuid4 per attempt).
Signed routes add X-Signature = base64(Ed25519(msg)), msg = "{sub_path}:{sorted_query}:{body}:{timestamp}",
sorted_query = all query params incl. timestamp/client_id, sorted by key, encodeURIComponent'd, joined by '&'.
IPv4 only; Cloudflare rejects default python UAs (1010) so the UA is the CLI's.
Keys come from .secrets (GMGN_API_KEY, GMGN_SIGNING_KEY) and are never printed, logged or returned.
Never retries a POST (swap / strategy create / cancel are real transactions) and never retries on a 429:
every call passes the process-wide GATE (DECISION 88)."""
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


# ---------- process-wide request gate (DECISION 88)
# GMGN rate-limits by API key AND source IP with a weighted leaky bucket (Plus plan 20/20; swap and quote cost 10).
# Any request sent during a 429 ban resets the ban to 5 minutes (RATE_LIMIT_BANNED_MAX), so one retrying sell loop
# can keep the whole desk banned for hours (night of 2026-11-18). Every GMGN call goes through Gmgn.request, so this
# one gate covers Strategy A, gmgn_surge, gmgn_trail, Strategy B LIVE / GTA, balance, quote and order-status polls.
import contextvars, os
from contextlib import contextmanager

BAN_DEFAULT_S = 360.0          # 6 min when a 429 carries no usable reset_at
BAN_PAD_S = 2.0                # stay closed a little past reset_at
BAN_MAX_S = 3600.0             # sanity cap on a reset_at far in the future
BUCKET_RATE = 10.0             # weight units leaked per second (half of the Plus plan's 20)
BUCKET_CAP = 20.0              # burst capacity in weight units
READ_HEADROOM = 10.0           # reads never fill the last swap's worth of the bucket
SWAP_GAP_S = 2.0               # min spacing between two swaps
ANY_GAP_S = 0.25               # min spacing between any two requests
BUY_AFTER_OPEN_S = 15.0        # after a ban ends, buys wait this long so sells go first
MAX_WAIT_S = {"sell": 30.0, "buy": 10.0, "read": 10.0, "price": 1.0}
SELL_BACKOFF_MAX_S = 120.0     # per-order sell retry backoff ceiling
WEIGHTS = {"/v1/trade/swap": 10, "/v1/trade/quote": 10, "/v1/trade/query_order": 5, "/v1/trade/strategy/create": 5,
           "/v1/trade/strategy/cancel": 2, "/v1/trade/strategy/orders": 1, "/v1/trade/gas_price": 1,
           "/v1/user/info": 2, "/v1/user/wallet_holdings": 5}
_KIND = contextvars.ContextVar("gmgn_kind", default=None)


@contextmanager
def kind(k: str):
    """Tag the GMGN calls made inside this block, e.g. kind('price') for optional price quotes (fail fast, never wait)."""
    tok = _KIND.set(k)
    try:
        yield
    finally:
        _KIND.reset(tok)


class GateClosed(GmgnError):
    """Raised locally: the gate refused to send. Nothing reached GMGN."""
    def __init__(self, why, reset_at=None):
        super().__init__(429, error="GATE_CLOSED", message=why, reset_at=reset_at)


def _hm(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


class Gate:
    def __init__(self, clock=time.time, sleep=asyncio.sleep, path=None):
        self.clock, self.sleep, self.path = clock, sleep, path
        self.banned_until = 0.0
        self.ban_reason = ""
        self.opened_at = 0.0           # when the last ban ended (buys wait BUY_AFTER_OPEN_S past it)
        self.level = 0.0               # bucket fill in weight units
        self.level_at = clock()
        self.last_any = 0.0
        self.last_swap = 0.0
        self.sell_waiters = 0
        self.bans = 0
        self.local_refusals = 0
        self.sent = 0
        self._load()

    # ---- persistence: a restart during a ban must not send straight away
    def _load(self):
        if not self.path:
            return
        try:
            d = json.loads(open(self.path).read())
            self.banned_until = float(d.get("banned_until") or 0)
            self.ban_reason = str(d.get("reason") or "")[:200]
            self.opened_at = self.banned_until
        except Exception:
            pass

    def _save(self):
        if not self.path:
            return
        try:
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as f:
                f.write(json.dumps({"banned_until": self.banned_until, "reason": self.ban_reason}))
            os.replace(tmp, self.path)
        except Exception:
            pass

    def configure(self, path=None):
        self.path = path
        self._load()

    # ---- state
    def remaining(self) -> float:
        return max(0.0, self.banned_until - self.clock())

    def closed(self) -> bool:
        return self.remaining() > 0

    def buy_blocked(self):
        """None when a new buy may be sent, else a short reason. Buys wait while banned, for BUY_AFTER_OPEN_S after a
        ban ends, and while any sell is queued for the bucket."""
        now = self.clock()
        if now < self.banned_until:
            return f"GMGN rate-limit ban, no requests until {_hm(self.banned_until)}"
        if self.opened_at and now < self.opened_at + BUY_AFTER_OPEN_S:
            return f"GMGN ban just ended, sells first until {_hm(self.opened_at + BUY_AFTER_OPEN_S)}"
        if self.sell_waiters:
            return "GMGN sell queued, buys wait"
        return None

    def note(self) -> str:
        if self.closed():
            return f"gmgn gate closed until {_hm(self.banned_until)} ({self.ban_reason})"
        return f"gmgn gate open sent={self.sent} bans={self.bans}"

    def retry_at(self, now, base_s, fails=1) -> float:
        """Next per-order sell retry: exponential backoff from base_s (x2 per failure, capped) and never before the ban ends.
        `now` is the caller's clock; the ban wait is measured on the gate's own clock."""
        back = min(SELL_BACKOFF_MAX_S, float(base_s) * (2 ** max(0, int(fails) - 1)))
        return now + max(back, self.remaining() + 1.0 if self.closed() else 0.0)

    def reset(self):
        self.__init__(self.clock, self.sleep, None)

    # ---- bucket
    def _leak(self, now):
        self.level = max(0.0, self.level - (now - self.level_at) * BUCKET_RATE)
        self.level_at = now

    def _wait_needed(self, k, w, now, swap=False) -> float:
        self._leak(now)
        room = BUCKET_CAP - (READ_HEADROOM if k in ("read", "price") else 0.0)
        wb = max(0.0, (self.level + w - room) / BUCKET_RATE) if w <= room else 0.0
        if w > room and self.level > 0:
            wb = self.level / BUCKET_RATE
        wa = max(0.0, self.last_any + ANY_GAP_S - now)
        ws = max(0.0, self.last_swap + SWAP_GAP_S - now) if swap else 0.0
        return max(wb, wa, ws)

    async def acquire(self, k: str, w: float, swap: bool = False):
        """Wait for room (sells first) or raise GateClosed. Never sends during a ban."""
        start = self.clock()
        queued = False
        if k == "sell":
            self.sell_waiters += 1; queued = True
        try:
            while True:
                now = self.clock()
                if now < self.banned_until:
                    self.local_refusals += 1
                    raise GateClosed(f"not sent: GMGN rate-limit ban until {_hm(self.banned_until)} ({self.ban_reason})",
                                     reset_at=int(self.banned_until))
                if k == "buy":
                    why = self.buy_blocked()
                    if why and not (why.startswith("GMGN sell queued") and now - start < MAX_WAIT_S["buy"]):
                        self.local_refusals += 1
                        raise GateClosed(f"not sent: {why}", reset_at=int(self.banned_until) or None)
                wait = self._wait_needed(k, w, now, swap)
                if k != "sell" and self.sell_waiters and wait < 0.05:
                    wait = 0.05                          # a sell is queued: let it take the bucket first
                if wait <= 0:
                    self.level += w
                    self.last_any = now
                    if swap:
                        self.last_swap = now
                    self.sent += 1
                    return
                if now + wait - start > MAX_WAIT_S.get(k, 10.0):
                    self.local_refusals += 1
                    raise GateClosed(f"not sent: GMGN local rate limit busy ({k}, would wait {wait:.1f}s)")
                await self.sleep(min(wait, 0.5))
        finally:
            if queued:
                self.sell_waiters -= 1

    # ---- responses
    def observe(self, status, body: dict, headers) -> None:
        now = self.clock()
        try:
            lim, rem = headers.get("x-ratelimit-limit"), headers.get("x-ratelimit-remaining")
            if lim is not None and rem is not None:      # sync with the server's bucket when it tells us
                self._leak(now)
                self.level = max(self.level, float(lim) - float(rem))
        except Exception:
            pass
        if status != 429:
            return
        reset = None
        for v in ((body or {}).get("reset_at"), headers.get("x-ratelimit-reset")):
            try:
                if v is not None and float(v) > 0:
                    reset = float(v); break
            except (TypeError, ValueError):
                continue
        if reset is None:
            try:
                ra = headers.get("retry-after")
                reset = now + float(ra) if ra is not None else None
            except (TypeError, ValueError):
                reset = None
        until = (reset + BAN_PAD_S) if reset is not None and reset > now else now + BAN_DEFAULT_S
        until = min(until, now + BAN_MAX_S)
        if until > self.banned_until:
            if self.banned_until <= now:
                self.bans += 1
            self.banned_until = until
            self.opened_at = until
            self.ban_reason = str((body or {}).get("error") or "429")[:60]
            self._save()
        self.level = BUCKET_CAP


GATE = Gate()


def route_kind(method, sub_path, body) -> str:
    k = _KIND.get()
    if k:
        return k
    if sub_path == "/v1/trade/swap":
        return "buy" if (body or {}).get("input_token") == SOL else "sell"
    if sub_path in ("/v1/trade/strategy/create", "/v1/trade/strategy/cancel"):
        return "sell"                                    # protective stops rank with sells
    return "read"


class Gmgn:
    def __init__(self, api_key=None, pem=None, transport=None, clock=time.time, gate=None):
        self.gate = gate if gate is not None else GATE
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
        """One GMGN call through the process-wide GATE (DECISION 88): never sent during a ban, paced by a weighted
        bucket, sells first. Any 429 closes the gate for every caller until reset_at; there is no retry on 429."""
        body_str = dumps(body) if body is not None else ("" if signed else None)
        gate = self.gate
        k = route_kind(method, sub_path, body)
        await gate.acquire(k, WEIGHTS.get(sub_path, 2), swap=(sub_path == "/v1/trade/swap"))
        url, headers, _ = self.prepare(method, sub_path, query, body_str, signed)
        try:
            r = await self.http.request(method, url, headers=headers, content=(body_str or None) if method == "POST" else None)
        except Exception as e:
            raise GmgnError(0, error="NETWORK", message=f"{method} {sub_path} {type(e).__name__}")
        try:
            d = r.json()
        except Exception:
            d = None
        gate.observe(r.status_code, d if isinstance(d, dict) else {}, r.headers)
        if not isinstance(d, dict):
            raise GmgnError(r.status_code, error="NON_JSON", message=f"{method} {sub_path} {r.text[:120]}")
        if r.status_code == 200 and d.get("code") == 0:
            return d.get("data")
        raise GmgnError(r.status_code, d.get("code"), d.get("error"), f"{method} {sub_path} {d.get('message', '')}",
                        d.get("reset_at") or r.headers.get("x-ratelimit-reset"), d.get("upgrade_url"))

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

    async def strategy_orders(self, base_token=None, kind="open", group_tag="STMix"):
        """group_tag: 'STMix' (smart_trade / swap condition orders) or 'LimitOrder' (limit_order stop_loss etc.)."""
        q = {"chain": CHAIN, "group_tag": group_tag, "type": kind, "from": MAIN_WALLET}
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
