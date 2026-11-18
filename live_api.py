"""DECISION 75: live app data straight from the droplet (read-only), ~2 s fresh, Pages stays the backup.

* The desk process keeps an in-memory snapshot of the app's core JSON files (the same Builder the publisher uses),
  each encrypted exactly like the published files (crypto.py, AES-GCM with the APP_TOKEN-derived key).
* Rebuilt only while the app is polling (a request in the last ACTIVE_S), one file at a time with a yield to the
  event loop between files, at most every MIN_INTERVAL_S and never more than MAX_DUTY of the loop's time.
  Builder only reads the db / in-memory state: no GMGN / RPC / Gecko calls are added.
* A tiny HTTP server on 127.0.0.1 in a daemon thread serves it; Caddy terminates TLS in front of it.
  GET only. Bearer = sha256(AUTH_SALT + APP_TOKEN) (the raw token never leaves the phone), constant-time compare.
  CORS for the Pages origin only. Per-IP rate limit. Responses carry only encrypted envelopes + timing numbers.
"""
from __future__ import annotations
import asyncio, hashlib, hmac, json, re, secrets as _pyrand, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import crypto, secrets_store
from ctx import AGENTS, iso

HOST, PORT = "127.0.0.1", 8787
ORIGIN = "https://cdeason12341-cpu.github.io"
PATH = "/live/v1/bundle"
AUTH_SALT = "grok-bot-desk/v1/live:"
MIN_INTERVAL_S = 2.0
MAX_DUTY = 0.10                 # build time / interval
ACTIVE_S = 20.0
RATE_PER_MIN = 120              # per client IP (app polls ~30/min)
MAX_COINS = 2
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,48}$")


def auth_token(app_token: str) -> str:
    return hashlib.sha256((AUTH_SALT + app_token).encode()).hexdigest()


def _digest(obj) -> str:
    """Change detector: top-level 'ts' (build time) ignored so an unchanged file isn't re-sent every build."""
    if isinstance(obj, dict) and "ts" in obj:
        obj = {k: v for k, v in obj.items() if k != "ts"}
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Snapshot:
    def __init__(self):
        self.lock = threading.Lock()
        self.boot = _pyrand.token_hex(4)
        self.seq = 0
        self.files: dict[str, tuple[int, str, dict]] = {}     # rel -> (seq, digest, envelope)
        self.built_at = 0.0
        self.build_ms = 0.0

    def update(self, objs: dict, token: str, build_ms: float = 0.0, now: float | None = None):
        prepared = {}
        with self.lock:
            cur = dict(self.files)
        for rel, obj in objs.items():
            d = _digest(obj)
            if rel in cur and cur[rel][1] == d:
                continue
            prepared[rel] = (d, crypto.encrypt_obj(obj, token))
        with self.lock:
            if prepared:
                self.seq += 1
                for rel, (d, env) in prepared.items():
                    self.files[rel] = (self.seq, d, env)
            self.built_at = now or time.time()
            self.build_ms = build_ms
        return len(prepared)

    def cursor(self) -> str:
        return f"{self.boot}.{self.seq}"

    def diff(self, since: str | None, coins=()) -> dict:
        with self.lock:
            boot, _, n = (since or "").partition(".")
            try:
                n = int(n) if boot == self.boot else -1
            except ValueError:
                n = -1
            files = {}
            for rel, (sq, _, env) in self.files.items():
                if rel.startswith(("coin/", "audit/")):
                    if rel.split("/", 1)[1][:-5] in coins:      # the coin page being viewed: always sent (small)
                        files[rel] = env
                elif sq > n:
                    files[rel] = env
            return {"v": 1, "cursor": f"{self.boot}.{self.seq}", "full": n < 0, "builtAt": iso(self.built_at) if self.built_at else None,
                    "builtAtMs": int(self.built_at * 1000), "buildMs": round(self.build_ms, 1), "files": files}


class RateLimit:
    def __init__(self, per_min=RATE_PER_MIN):
        self.per_min, self.lock, self.hits = per_min, threading.Lock(), {}

    def allow(self, ip: str, now: float | None = None) -> bool:
        now = now or time.time()
        with self.lock:
            q = [t for t in self.hits.get(ip, []) if now - t < 60]
            ok = len(q) < self.per_min
            if ok:
                q.append(now)
            self.hits[ip] = q
            if len(self.hits) > 500:                                  # bounded memory
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 60]:
                    self.hits.pop(k, None)
            return ok


def make_handler(api: "LiveApi"):
    class H(BaseHTTPRequestHandler):
        server_version = "desk"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # no access log (never log headers / query)
            pass

        def _ip(self):
            return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

        def _send(self, code, body: bytes = b"", ctype="application/json", extra=None):
            self.send_response(code)
            origin = self.headers.get("Origin")
            if origin == ORIGIN:
                self.send_header("Access-Control-Allow-Origin", ORIGIN)
                self.send_header("Vary", "Origin")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def _err(self, code, msg):
            self._send(code, json.dumps({"error": msg}).encode())

        def do_OPTIONS(self):
            if urlsplit(self.path).path != PATH or self.headers.get("Origin") != ORIGIN:
                return self._err(403, "forbidden")
            self._send(204, b"", extra={"Access-Control-Allow-Methods": "GET", "Access-Control-Allow-Headers": "Authorization",
                                        "Access-Control-Max-Age": "7200"})

        def do_GET(self):
            u = urlsplit(self.path)
            if u.path != PATH:
                return self._err(404, "not found")
            origin = self.headers.get("Origin")
            if origin is not None and origin != ORIGIN:
                return self._err(403, "forbidden")
            if not api.rate.allow(self._ip()):
                return self._send(429, b'{"error":"slow down"}', extra={"Retry-After": "5"})
            if not api.check_auth(self.headers.get("Authorization") or ""):
                return self._err(401, "unauthorized")
            q = parse_qs(u.query)
            coins = [m for m in q.get("coin", [])[:MAX_COINS] if MINT_RE.match(m)]
            api.touch(coins)
            if not api.snap.built_at:
                return self._err(503, "warming up")
            self._send(200, json.dumps(api.snap.diff((q.get("since") or [None])[0], coins), separators=(",", ":")).encode())

        def _no(self):
            self._err(405, "read only")
        do_POST = do_PUT = do_DELETE = do_PATCH = _no

        def do_HEAD(self):
            self._err(405, "read only")
    return H


class LiveApi:
    def __init__(self, desk, host=HOST, port=PORT, token_fn=None):
        self.d, self.c = desk, desk.ctx
        self.builder = desk.publisher.builder
        self.host, self.port = host, port
        self.token_fn = token_fn or (lambda: secrets_store.get("APP_TOKEN"))
        self.snap, self.rate = Snapshot(), RateLimit()
        self.last_client = 0.0
        self.coins: dict[str, float] = {}
        self.server = None
        self._lock = threading.Lock()
        self.builds = 0
        self._err_at: dict[str, float] = {}

    # ---- server thread side
    def check_auth(self, header: str) -> bool:
        tok = self.token_fn()
        if not tok or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode(), auth_token(tok).encode())

    def touch(self, coins=()):
        now = time.time()
        with self._lock:
            self.last_client = now
            for m in coins:
                self.coins[m] = now
            for m in [m for m, t in self.coins.items() if now - t > ACTIVE_S]:
                self.coins.pop(m, None)
            if len(self.coins) > MAX_COINS:
                for m in sorted(self.coins, key=self.coins.get)[:-MAX_COINS]:
                    self.coins.pop(m, None)

    def start_server(self):
        if self.server is not None:
            return
        self.server = ThreadingHTTPServer((self.host, self.port), make_handler(self))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="live-api", daemon=True).start()

    # ---- event loop side
    def parts(self, coins=()):
        b = self.builder
        out = [("state.json", b.state), ("chart.json", b.chart), ("activity.json", b.activity), ("account.json", b.account),
               ("scores.json", b.scores), ("feed.json", b.feed), ("trades.json", b.trades), ("scanner.json", b.scanner_feed),
               ("strat_b.json", b.strat_b), ("strat_b_live.json", b.strat_b_live), ("strat_feed.json", b.strat_feed)]
        out += [(f"agents/{a}.json", (lambda a=a: b.agent(a))) for a in AGENTS]
        if coins:
            known = set(b.coin_mints())
            for m in coins:
                if m in known:
                    out.append((f"coin/{m}.json", (lambda m=m: b.coin(m))))
                    out.append((f"audit/{m}.json", (lambda m=m: b.audit(m))))
        return out

    async def build_once(self):
        token = self.token_fn()
        if not token:
            return 0
        with self._lock:
            coins = list(self.coins)
        objs, busy = {}, 0.0
        for rel, fn in self.parts(coins):
            t = time.perf_counter()
            try:
                o = fn()
            except Exception as e:
                now = time.time()
                if now - self._err_at.get(rel, 0) > 300:          # at most one error row per file per 5 min
                    self._err_at[rel] = now
                    self.c.error("live_api", "build", f"{rel}: {str(e)[:150]}")
                o = None
            busy += time.perf_counter() - t
            if o is not None:
                objs[rel] = o
            await asyncio.sleep(0)                          # let trading tasks run between files
        n = await asyncio.to_thread(self.snap.update, objs, token, busy * 1000)
        self.builds += 1
        return n

    def active(self) -> bool:
        return time.time() - self.last_client < ACTIVE_S

    async def loop(self):
        while self.server is None:
            try:
                self.start_server()
            except OSError as e:                              # port busy: retry quietly, never crash the desk
                self.c.error("live_api", "bind", str(e)[:120])
                await asyncio.sleep(60)
        while True:
            if not self.active():
                await asyncio.sleep(0.5)
                continue
            t0 = time.time()
            try:
                await self.build_once()
            except Exception as e:
                self.c.error("live_api", "loop", str(e)[:200])
            wait = max(MIN_INTERVAL_S, self.snap.build_ms / 1000 / MAX_DUTY) - (time.time() - t0)
            await asyncio.sleep(max(0.2, wait))
