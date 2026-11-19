"""AGENT 1 · SCANNER. Counts and cuts. Never judges quality."""
from __future__ import annotations
import stage_pings
import asyncio, json, time, unicodedata
from collections import deque
import fields, holders, solana, sources
from ctx import iso
from net import RateLimited

WS_URL = "wss://pumpportal.fun/api/data"


def norm_name(s: str | None) -> str:
    """lowercase, strip spaces, digits, emoji (keep letters only)."""
    if not s:
        return ""
    return "".join(ch for ch in s.lower() if unicodedata.category(ch).startswith("L"))


def is_copycat(name, symbol, runners: list[dict]) -> str | None:
    mine = [n for n in (norm_name(name), norm_name(symbol)) if n]
    for r in runners:
        for theirs in (norm_name(r.get("name")), norm_name(r.get("symbol"))):
            if not theirs:
                continue
            for m in mine:
                if m == theirs or (min(len(m), len(theirs)) >= 3 and (m in theirs or theirs in m)):
                    return r.get("name") or r.get("symbol")
    return None


def market_cap(pair: dict | None) -> float | None:
    """DexScreener marketCap, falling back to fdv. None = unknown (missing / zero / junk)."""
    if not pair:
        return None
    for k in ("marketCap", "fdv"):
        v = fields.get(pair, k)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def age_label(s) -> str:
    return "no age limit" if age_limit_off(s) else f"{float(_get(s, 'scanner.max_age_h', 5)):g}h"


def fmt_k(v: float) -> str:
    return f"${v/1000:.1f}K" if v < 1_000_000 else f"${v/1_000_000:.2f}M"


def pick_pair(pairs: list[dict], mint: str) -> dict | None:
    mine = [p for p in pairs or [] if fields.get(p, "baseToken.address") == mint]
    if sources.is_pump(mint):
        return fields.select_pair(mine)                 # pump mints: PumpSwap pool only (unchanged)
    return sources.select_other_pair(mine)              # other launchpads/DEXes: graduated pool, SOL-quoted first


def token_pairs(pairs: list[dict], mint: str) -> list[dict]:
    return [p for p in pairs or [] if fields.get(p, "baseToken.address") == mint]


# scanner.max_age_h = 0 (off): no age limit. The waiting room / re-check bookkeeping still needs a
# finite horizon so dead coins DexScreener never lists are not polled forever: 24h (old setting max).
NO_AGE_LIMIT_HORIZON_MIN = 24 * 60.0


def _get(s, key, default=None):
    try:
        return s[key]
    except (KeyError, TypeError):
        return default


def age_limit_off(s) -> bool:
    h = _get(s, "scanner.max_age_h")
    return h is not None and float(h) <= 0


def watch_window_min(s) -> float:
    """Minutes a migrated coin stays in the waiting room and is re-checked."""
    h = _get(s, "scanner.max_age_h")
    if h is not None:
        return NO_AGE_LIMIT_HORIZON_MIN if float(h) <= 0 else float(h) * 60.0
    try:
        return float(s["scanner.max_wait_min"])
    except (KeyError, TypeError):
        return 60.0


WSOL_GECKO_ID = "solana_So11111111111111111111111111111111111111112"
# GeckoTerminal new_pools page 1 is only ~30s of Solana pool creates; a few pages cover a short WS gap.
CATCHUP_PAGES = 8
CATCHUP_LOOKBACK_S = 5 * 60


def parse_new_pools(payload, others: bool = False) -> list[dict]:
    """Pure. PumpSwap (graduated) pump-mint pools quoted in SOL from a GeckoTerminal new_pools body.
    others=True (scanner.sources all) also returns SOL-quoted non-pump pools on graduated DEXes (Raydium, Meteora,
    PumpSwap, Orca...) with at least sources.DUST_RESERVE_USD reserve; bonding curves and pump mints elsewhere are skipped."""
    out = []
    if not isinstance(payload, dict):
        return out
    for x in payload.get("data") or []:
        if not isinstance(x, dict):
            continue
        rel = x.get("relationships") or {}
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id")
        base = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
        quote = ((rel.get("quote_token") or {}).get("data") or {}).get("id") or ""
        mint = base.split("_", 1)[-1] if base else ""
        attrs = x.get("attributes") or {}
        if sources.is_pump(mint):
            if dex != "pumpswap":
                continue
        else:
            if not others or dex not in sources.GRADUATED_DEX_GECKO or sources.excluded(mint):
                continue
            if not quote or quote != WSOL_GECKO_ID:
                continue
            try:
                if float(attrs.get("reserve_in_usd") or 0) < sources.DUST_RESERVE_USD:
                    continue
            except (TypeError, ValueError):
                continue
        if quote and quote != WSOL_GECKO_ID:
            continue
        created = _iso_ts(attrs.get("pool_created_at"))
        if created is None:
            continue
        out.append({"mint": mint, "created_at": created, "name": attrs.get("name"),
                    "pool": attrs.get("address"), "dex": dex})
    return out


def _iso_ts(v):
    if not v:
        return None
    try:
        from datetime import datetime, timezone
        if isinstance(v, (int, float)):
            return float(v) / (1000.0 if v > 10_000_000_000 else 1.0)
        s = str(v).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def recheck_offsets(s) -> list[float]:
    """Re-check ages (minutes) for fixable scanner kills, clipped to the watch window."""
    try:
        raw = str(s["scanner.recheck_minutes"]).strip().lower()
    except (KeyError, TypeError):
        return []
    if raw in ("", "off", "none", "0"):
        return []
    out = []
    for x in raw.replace(" ", "").split(","):
        try:
            v = float(x)
        except ValueError:
            continue
        if 0 < v < watch_window_min(s) and (not out or v > out[-1]):
            out.append(v)
    return out


TOO_FAR = "too far for recheck"


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _mc_from_reason(reason):
    """'mc $12.3K' / 'mc $1.20M' -> 12300.0; 'mc unknown' or other -> None."""
    import re
    m = re.search(r"\bmc \$([\d.]+)([KM])", reason or "")
    return float(m.group(1)) * (1e3 if m.group(2) == "K" else 1e6) if m else None


def near_miss(s, liq=None, mc=None, top10=None, h1=None) -> tuple[bool, list[str]]:
    """Owner rule 2026-11-15 12:26 CT: re-check a first-scan fixable kill only if EVERY failing fixable value
    is within its near-miss gate (gate 0 = off = always within). Unknown values never block (mc unknown keeps
    re-checking). Returns (ok, [too-far bits])."""
    far = []
    liq, mc, top10, h1 = _num(liq), _num(mc), _num(top10), _num(h1)
    g = lambda k: float(_get(s, k, 0) or 0)
    if liq is not None and liq < g("scanner.min_liquidity_usd"):
        gate = g("scanner.recheck_min_liq_usd")
        if gate > 0 and liq < gate:
            far.append(f"liq {fmt_k(liq)} < {fmt_k(gate)}")
    floor = g("scanner.min_market_cap_usd")
    if floor > 0 and mc is not None and mc < floor:
        gate = g("scanner.recheck_min_mc_usd")
        if gate > 0 and mc < gate:
            far.append(f"mc {fmt_k(mc)} < {fmt_k(gate)}")
    if top10 is not None and top10 > g("scanner.max_top10_pct"):
        gate = g("scanner.recheck_max_top10_pct")
        if gate > 0 and top10 > gate:
            far.append(f"top 10 {top10:.0f}% > {gate:g}%")
    if h1 is not None and h1 > g("scanner.max_h1_change_pct"):
        gate = g("scanner.recheck_max_h1_pct")
        if gate > 0 and h1 > gate:
            far.append(f"+{h1:.0f}% > +{gate:g}%")
    return (not far), far


def fixable(reason: str | None) -> bool:
    """Kills that can change as a coin ages: thin pool, concentrated top 10, already pumped.
    Permanent kills (not a pump mint, not SOL-quoted, copycat, never qualified) never re-check."""
    if not reason or TOO_FAR in reason:
        return False
    r = reason.split(": ", 1)[1] if reason.startswith("recheck ") and ": " in reason else reason
    return r.startswith("liq ") or r.startswith("mc ") or r.startswith("top 10 hold ") or (r.startswith("+") and r.endswith(" in 1h"))


def base_reason(reason: str | None) -> str:
    """'recheck 2: mc $26.7K · next at 10m' -> 'mc $26.7K'."""
    r = (reason or "").strip()
    if r.startswith("recheck ") and ": " in r:
        r = r.split(": ", 1)[1]
    return r.split(" · ", 1)[0].strip()


def surge_eligible(status: str | None, reason: str | None) -> bool:
    """Coins seen tonight that may be re-scanned on an MC surge. Safety kills never are:
    not a pump mint, not SOL-quoted, copycat, and any scanner top-10 kill stay dead."""
    if status == "room":
        return True
    if status not in ("recheck", "killed"):
        return False
    r = base_reason(reason)
    if not r or r.startswith("top 10 hold"):
        return False
    return r.startswith("liq ") or r.startswith("mc ") or (r.startswith("+") and r.endswith(" in 1h"))


def priority_key(s, coin: dict, now: float):
    """Queue order: surges first, then pools younger than scanner.young_priority_min (youngest first),
    then everything else in its existing order (sort is stable). Older coins are never dropped."""
    surge = 0 if coin.get("surge") else 1
    young = float(_get(s, "scanner.young_priority_min", 0) or 0)
    age = (now - float(coin.get("migrated_at") or now)) / 60.0
    if young > 0 and age < young:
        return (surge, 0, age)
    return (surge, 1, 0.0)


def prioritize(s, coins: list[dict], now: float) -> list[dict]:
    return sorted(coins, key=lambda c: priority_key(s, c, now))


SURGE_MIN_MC_USD = 5000.0     # dust pools (MC in the tens of dollars) jump 50% on one trade: not a surge


class SurgeWatch:
    """MC samples per mint over the last window; a surge is MC >= the window's low * (1 + pct/100)."""

    def __init__(self):
        self.hist: dict[str, deque] = {}
        self.cool: dict[str, float] = {}

    def observe(self, mint, mc, now, pct, window_s) -> dict | None:
        if mc is None or mc <= 0:
            return None
        h = self.hist.setdefault(mint, deque())
        h.append((now, float(mc)))
        while h and h[0][0] < now - window_s:
            h.popleft()
        if self.cool.get(mint, 0) > now:
            return None
        lo_ts, lo = min(h, key=lambda x: x[1])
        if lo <= 0 or lo_ts >= now or mc < lo * (1 + pct / 100.0) or mc < SURGE_MIN_MC_USD:
            return None
        self.cool[mint] = now + max(5 * window_s, 300)      # one trigger, then a cooldown
        h.clear()
        h.append((now, float(mc)))
        return {"pct": round((mc / lo - 1) * 100, 1), "windowS": int(window_s), "secs": int(round(now - lo_ts)),
                "mcFrom": round(lo, 2), "mcTo": round(float(mc), 2)}

    def prune(self, keep: set):
        for m in list(self.hist):
            if m not in keep:
                self.hist.pop(m, None)
        now = time.time()
        for m in list(self.cool):
            if self.cool[m] < now - 3600:
                self.cool.pop(m, None)


def next_recheck_at(s, migrated_at: float, now: float) -> float | None:
    """Next scheduled re-check time strictly after the coin's current age, or None if the schedule is spent."""
    age = (now - migrated_at) / 60.0
    for off in recheck_offsets(s):
        if off > age:
            return migrated_at + off * 60.0
    return None


def decide(s, coin: dict, pair: dict | None, top10: float | None, runners: list[dict], now: float) -> tuple[str, str | None]:
    """('pass'|'kill'|'wait'|'drop', reason). Pure; used by the loop, tests and replay."""
    mint = coin["mint"]
    age_min = (now - coin["migrated_at"]) / 60.0
    limit = watch_window_min(s)
    other = not sources.is_pump(mint)
    if other and sources.pump_only(s):
        return "kill", "not a pump mint"
    ex = sources.excluded(mint, fields.get(pair, "baseToken.symbol") if pair else None)
    if ex:
        return "kill", f"excluded: {ex}"
    if pair is None:                                   # DexScreener doesn't know it yet: it waits
        return ("drop", "never qualified") if age_min >= limit else ("wait", None)
    if fields.get(pair, "quoteToken.address") != solana.WSOL:
        return "kill", f"not SOL-quoted ({fields.get(pair, 'quoteToken.symbol') or '?'})"
    if other:                                          # non-pump coins only: keep big / established tokens out
        cap = float(_get(s, "scanner.other_max_mc_usd", 0) or 0)
        mc = market_cap(pair)
        if cap > 0 and mc is not None and mc > cap:
            return "kill", f"too big: mc {fmt_k(mc)}"
        fp = coin.get("first_pool_at")
        if fp and (now - float(fp)) / 60.0 >= limit:
            return "kill", f"old token: first pool {(now - float(fp)) / 3600:.0f}h ago"
    liq = float(fields.get(pair, "liquidity.usd") or 0)
    if liq < s["scanner.min_liquidity_usd"]:
        return "kill", f"liq {fmt_k(liq)}"
    mc_floor = float(_get(s, "scanner.min_market_cap_usd", 0) or 0)
    if mc_floor > 0:                                    # 0 = off
        mc = market_cap(pair)
        if mc is None:
            return "kill", "mc unknown"                 # unknown is not a pass; recheckable like liq
        if mc < mc_floor:
            return "kill", f"mc {fmt_k(mc)}"
    if top10 is not None and top10 > s["scanner.max_top10_pct"]:
        return "kill", f"top 10 hold {top10:.0f}%"
    h1 = fields.get(pair, "priceChange.h1")
    if h1 is not None and float(h1) > s["scanner.max_h1_change_pct"]:
        return "kill", f"+{float(h1):.0f}% in 1h"
    cc = is_copycat(fields.get(pair, "baseToken.name"), fields.get(pair, "baseToken.symbol"),
                    [r for r in runners if r.get("mint") != mint])
    if cc:
        return "kill", f"copycat of {cc}"
    if age_min >= limit:
        return "drop", "never qualified"
    if top10 is None:
        return "wait", None                              # never pass without a top-10 number
    if age_min >= s["scanner.min_pool_age_min"]:
        return "pass", None
    return "wait", None


async def creator_balance(net, creator, mint) -> int | None:
    try:
        r = await net.rpc("getTokenAccountsByOwner", [creator, {"mint": mint}, {"encoding": "jsonParsed"}])
        tot = 0
        for a in (r or {}).get("value", []):
            tot += int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        return tot
    except Exception:
        return None


class Scanner:
    name = "scanner"

    def __init__(self, ctx):
        self.c = ctx
        self.log = ctx.logs["scanner"]
        self.ws_last = 0.0
        self.ws_pong = 0.0
        self.supply_cache: dict[str, int] = {}
        self.extra: dict[str, dict] = {}
        self._ws = None
        self._ws_fail = None
        self.surge = SurgeWatch()
        self.surge_queue: dict[str, dict] = {}
        self.source_counts: dict[str, int] = {}         # admitted since start, by intake feed
        self._latest_at = 0.0
        self._latest_seen: set[str] = set()
        self._capturing: set[str] = set()               # creator capture still running: never pass before it lands
        self._wake: asyncio.Event | None = None

    # ---------- websocket (ONE connection for the desk)
    # Migrations are naturally sparse, so silence is not a kill switch.
    # Library ping_timeout is not enough: websockets 17 waits on drain() before the pong
    # clock starts, so a half-open TCP socket can stall the keepalive for the kernel
    # retry window (~45–60 min). We run our own ping and abort the transport on timeout.
    # scanner.ws_silent_s stays in the registry but is not used as a kill switch.
    def abort_ws(self, ws):
        """Hard-close a socket. close()/drain() can hang on a half-open TCP connection."""
        transport = getattr(ws, "transport", None)
        if transport is not None and hasattr(transport, "abort"):
            try:
                transport.abort()
            except Exception:
                pass
        for name in ("abort", "fail_connection"):
            fn = getattr(ws, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    async def _expect_pong(self, ws):
        ping = getattr(ws, "ping", None)
        if ping is None:
            return 0.0
        pong = await ping()
        if asyncio.isfuture(pong) or asyncio.iscoroutine(pong) or hasattr(pong, "__await__"):
            return await pong
        return pong

    async def ws_ping_loop(self, ws, interval, timeout):
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.wait_for(self._expect_pong(ws), timeout=timeout)
                self.ws_pong = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._ws_fail = f"pong timeout: {type(e).__name__}: {e}"
                self.abort_ws(ws)
                raise ConnectionError(self._ws_fail) from e

    async def ws_loop(self, connect=None, ping_interval=20, ping_timeout=20, backoff_start=1):
        import websockets
        backoff = backoff_start
        def _connect():
            return websockets.connect(WS_URL, max_size=2**22, ping_interval=ping_interval, ping_timeout=ping_timeout)
        open_ws = connect or _connect
        while True:
            ping_task = None
            try:
                async with open_ws() as ws:
                    self._ws = ws
                    await asyncio.wait_for(ws.send(json.dumps({"method": "subscribeMigration"})), timeout=20)
                    self.c.db.x("INSERT INTO ws_events(ts,kind,note) VALUES(?,?,?)", (time.time(), "open", ""))
                    self.log.info(f"{iso()}\tws\topen\tsubscribeMigration")
                    backoff = backoff_start
                    now = time.time()
                    self.ws_last = now
                    self.ws_pong = now
                    ping_task = asyncio.create_task(self.ws_ping_loop(ws, ping_interval, ping_timeout), name="ws_ping")
                    try:
                        async for raw in ws:
                            self.ws_last = time.time()
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue
                            if isinstance(msg, dict) and msg.get("mint"):
                                await self.on_migration(msg)
                    finally:
                        self.abort_ws(ws)
                        ping_task.cancel()
                        try:
                            await ping_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        self._ws = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                note = (self._ws_fail or str(e))[:200]
                self._ws_fail = None
                self.c.db.x("INSERT INTO ws_events(ts,kind,note) VALUES(?,?,?)", (time.time(), "reconnect", note))
                self.log.info(f"{iso()}\tws\treconnect\t{note[:120]}\tbackoff={backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)

    async def on_migration(self, msg: dict):
        m = fields.FIELDS["migration"]
        mint = fields.get(msg, m["mint"])
        self.admit(mint, self.c.clock(), "migration",
                   signature=fields.get(msg, m["signature"]),
                   pool_label=fields.get(msg, m["pool_label"]), raw=msg)

    def admit(self, mint, migrated_at, source, signature=None, pool_label=None, raw=None) -> bool:
        """Insert a newly seen graduation into the waiting room. Idempotent on mint."""
        if not mint or self.c.coin(mint):
            return False
        now = self.c.clock()
        self.c.db.x("INSERT OR IGNORE INTO coins(mint,signature,pool_label,creator,migrated_at,first_seen,status,night,raw) VALUES(?,?,?,?,?,?,?,?,?)",
                    (mint, signature, pool_label or source, None, migrated_at, now, "room",
                     self.c.night, json.dumps(raw if raw is not None else {"source": source, "mint": mint})))
        if source == "migration":
            self.c.hb("scanner", "migration")
        if not sources.is_pump(mint):
            if sources.pump_only(self.c.s):
                self.emit({"mint": mint, "migrated_at": migrated_at}, None, None, "kill", "not a pump mint", now)
                return True
            ex = sources.excluded(mint)
            if ex:
                self.emit({"mint": mint, "migrated_at": migrated_at}, None, None, "kill", f"excluded: {ex}", now)
                return True
        self.source_counts[source] = self.source_counts.get(source, 0) + 1
        try:
            asyncio.get_running_loop().create_task(self.capture_creator(mint))
            self._capturing.add(mint)
        except RuntimeError:
            pass
        self.c.event("scan", mint)
        try:
            sb = getattr(self.c, "strat_b", None)
            if sb is not None:
                raw = raw or {}
                sb.see_raw({"mint": mint, "name": raw.get("name"), "symbol": raw.get("symbol"),
                            "b_source": f"scanner {source}"})
        except Exception:
            pass
        return True

    async def catchup_graduations(self) -> int:
        """Backstop: pull recent PumpSwap graduates so a short WS gap does not drop a coin."""
        added = 0
        oldest_ok = self.c.clock() - watch_window_min(self.c.s) * 60.0
        page_cutoff = self.c.clock() - CATCHUP_LOOKBACK_S
        fn = getattr(self.c.net, "gecko_new_pools", None)
        if fn is None:
            return 0
        for page in range(1, CATCHUP_PAGES + 1):
            try:
                payload = await fn(page)
            except Exception:
                if page == 1:
                    raise
                break
            rows = parse_new_pools(payload, others=not sources.pump_only(self.c.s))
            if not rows:
                break
            page_oldest = min(r["created_at"] for r in rows)
            for r in rows:
                if r["created_at"] < oldest_ok:
                    continue
                if sources.is_pump(r["mint"]):
                    src, label = "gap_backfill", "pumpswap"
                else:
                    src, label = "new_pools", r.get("dex")
                raw = {"source": src, "mint": r["mint"], "pool": r.get("pool"), "name": r.get("name")}
                if src != "gap_backfill":
                    raw["dex"] = r.get("dex")
                    raw["launchpad"] = sources.launchpad(r["mint"], label)
                if self.admit(r["mint"], r["created_at"], src, pool_label=label, raw=raw):
                    added += 1
            if page_oldest < page_cutoff:
                break
        return added

    LATEST_EVERY_S = 60
    LATEST_KINDS = ("profiles", "boosts")

    async def dex_latest_intake(self) -> int:
        """Other launchpads via DexScreener: newest token profiles + boosts (Solana, non-pump). One tokens/v1 call
        gives each new address's pools; admitted only if it has a graduated pool and its OLDEST pool is inside the
        watch window. migrated_at = that oldest pool's creation time. ~3 DexScreener calls a minute."""
        if sources.pump_only(self.c.s):
            return 0
        fn = getattr(self.c.net, "dex_latest", None)
        now = self.c.clock()
        if fn is None or now - self._latest_at < self.LATEST_EVERY_S:
            return 0
        self._latest_at = now
        cands = []
        for kind in self.LATEST_KINDS:
            try:
                for a in sources.parse_dex_latest(await fn(kind)):
                    if a not in cands and a not in self._latest_seen and not self.c.coin(a):
                        cands.append(a)
            except RateLimited:
                raise
            except Exception as e:
                self.c.error("scanner", "dex_latest", f"{kind} {str(e)[:200]}")
        if not cands:
            return 0
        pairs = await self.c.net.dex_tokens(cands[:30])
        horizon = watch_window_min(self.c.s) * 60.0
        added = 0
        for a in cands[:30]:
            mine = token_pairs(pairs, a)
            if len(self._latest_seen) > 5000:
                self._latest_seen.clear()
            self._latest_seen.add(a)                      # looked up once; a later admit still goes via new_pools
            pair = pick_pair(pairs, a)
            fp = sources.first_pool_at(mine)
            if pair is None or fp is None or now - fp >= horizon:
                continue                                  # bonding curve only / unknown age / older than the window
            dex = fields.get(pair, "dexId")
            raw = {"source": "dex_latest", "mint": a, "dex": dex, "pool": fields.get(pair, "pairAddress"),
                   "name": fields.get(pair, "baseToken.name"), "launchpad": sources.launchpad(a, None, dex)}
            if self.admit(a, fp, "dex_latest", pool_label=f"dex:{dex}", raw=raw):
                added += 1
        return added

    def source_label(self, mint) -> str:
        row = self.c.coin(mint) or {}
        return sources.launchpad(mint, row.get("pool_label"))

    async def capture_creator(self, mint):
        """Creator: migration message has none -> RugCheck report. Creator balance NOW via RPC."""
        try:
            await self._capture_creator(mint)
        finally:
            self._capturing.discard(mint)

    async def _capture_creator(self, mint):
        creator, amm = None, []
        for _ in range(3):
            try:
                rep = await self.c.net.rug_report(mint)
                creator = fields.get(rep, fields.FIELDS["rugcheck_report"]["creator"])
                ka = fields.get(rep, "knownAccounts") or {}
                amm = [k for k, v in ka.items() if (v or {}).get("type") == "AMM"]
                break
            except Exception:
                await asyncio.sleep(2)
        self.extra[mint] = {"amm": amm}
        bal = await creator_balance(self.c.net, creator, mint) if creator else None
        self.c.db.x("UPDATE coins SET creator=?, creator_balance_raw=?, creator_known=? WHERE mint=?",
                    (creator, None if bal is None else str(bal), 1 if creator else 0, mint))

    # ---------- polling
    async def poll_loop(self):
        try:
            n = self.backfill_room()
            if n:
                self.log.info(f"{iso()}\tbackfill\treopened {n} dropped mint(s) under {age_label(self.c.s)}")
        except Exception as e:
            self.c.error("scanner", "backfill", str(e)[:300])
        try:
            n = self.backfill_rechecks()
            if n:
                self.log.info(f"{iso()}\trecheck\tscheduled {n} recent fixable kill(s) from before start")
        except Exception as e:
            self.c.error("scanner", "recheck_backfill", str(e)[:300])
        while True:
            t0 = time.time()
            try:
                if "scanner" not in self.c.paused:
                    await self.poll_once()
                self.c.hb("scanner", f"room={len(self.room())} rechecks={self.recheck_queue_len()}" + (" paused" if "scanner" in self.c.paused else ""))
            except RateLimited:
                self.log.info(f"{iso()}\tdexscreener\t429\twait {self.c.s['scanner.backoff_429_s']}s")
                await asyncio.sleep(self.c.s["scanner.backoff_429_s"])
            except Exception as e:
                self.c.error("scanner", "poll", str(e)[:300])
            await self._nap(max(1, self.c.s["scanner.poll_s"] - (time.time() - t0)))

    async def _nap(self, secs):
        """Sleep until the next poll, or until a surge asks for a scan now."""
        if self._wake is None:
            self._wake = asyncio.Event()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    # ---------- MC surge watch (owner 2026-11-15 6:52 PM CT, DECISION 41)
    def surge_candidates(self, now) -> list[dict]:
        cap = int(_get(self.c.s, "scanner.surge_watch_max", 0) or 0)
        if cap <= 0:
            return []
        horizon = now - watch_window_min(self.c.s) * 60.0
        rows = self.c.db.rows(
            "SELECT mint, status, reason, migrated_at FROM coins WHERE night=? AND status IN ('room','recheck','killed') "
            "AND sent=0 AND migrated_at>=? ORDER BY migrated_at DESC", (self.c.night, horizon))
        po = sources.pump_only(self.c.s)
        out = [r for r in rows if (sources.is_pump(r["mint"]) or (not po and not sources.excluded(r["mint"])))
               and r["mint"] not in self.surge_queue
               and surge_eligible(r["status"], r["reason"])]
        return out[:cap]                                  # youngest first when over the cap

    def dex_headroom(self, calls: int) -> bool:
        """Surge polls only use DexScreener budget the scanner/risk/auditor are not using (<= 60% of the bucket)."""
        b = getattr(self.c.net, "dex_bucket", None)
        if b is None:
            return True
        now = time.time()
        used = len([t for t in b.stamps if now - t < 60])
        return used + calls <= int(b.per_min * 0.6)

    async def surge_once(self) -> int:
        now = self.c.clock()
        cands = self.surge_candidates(now)
        self.surge.prune({r["mint"] for r in cands})
        if not cands:
            return 0
        if not self.dex_headroom((len(cands) + 29) // 30):
            self.c.hb("surge", f"skip: dex budget busy watch={len(cands)}")
            return len(cands)
        pairs = await self.c.net.dex_tokens([r["mint"] for r in cands])
        now = self.c.clock()
        pct, win = float(self.c.s["scanner.surge_pct"]), float(self.c.s["scanner.surge_window_s"])
        for r in cands:
            pair = pick_pair(pairs, r["mint"])
            hit = self.surge.observe(r["mint"], market_cap(pair) if pair else None, now, pct, win)
            if hit:
                self.flag_surge(r, hit, now)
        return len(cands)

    def flag_surge(self, row, hit, now):
        mint = row["mint"]
        self.surge_queue[mint] = hit
        self.c.db.x("INSERT INTO surges(ts,mint,pct,mc_from,mc_to,secs,window_s,status,night) VALUES(?,?,?,?,?,?,?,?,?)",
                    (now, mint, hit["pct"], hit["mcFrom"], hit["mcTo"], hit["secs"], hit["windowS"], row["status"], self.c.night))
        self.log.info(f"{iso(now)}\t{mint}\tsurge\t+{hit['pct']:.0f}% in {hit['secs']}s\tmc {fmt_k(hit['mcFrom'])} -> {fmt_k(hit['mcTo'])}\twas={row['status']}: {row.get('reason') or ''}")
        if self._wake is not None:
            self._wake.set()                               # front of the queue: scan now, not at the next poll

    def take_surges(self) -> list[dict]:
        out = []
        for mint, hit in list(self.surge_queue.items()):
            self.surge_queue.pop(mint, None)
            row = self.c.coin(mint)
            if not row or row.get("sent") or not surge_eligible(row.get("status"), row.get("reason")):
                continue                                  # passed/sent meanwhile, or now a safety kill
            row = dict(row)
            row["surge"] = hit
            self.c.db.x("UPDATE surges SET scanned=1 WHERE mint=? AND night=? AND scanned=0", (mint, self.c.night))
            out.append(row)
        return out

    async def surge_loop(self):
        while True:
            t0 = time.time()
            n = 0
            try:
                if "scanner" not in self.c.paused:
                    n = await self.surge_once()
                self.c.hb("surge", f"watch={n} queued={len(self.surge_queue)}" + (" paused" if "scanner" in self.c.paused else ""))
            except RateLimited:
                self.log.info(f"{iso()}\tsurge\tdexscreener 429\twait {self.c.s['scanner.backoff_429_s']}s")
                await asyncio.sleep(self.c.s["scanner.backoff_429_s"])
            except Exception as e:
                self.c.error("scanner", "surge", str(e)[:300])
            await asyncio.sleep(max(1, self.c.s["scanner.surge_poll_s"] - (time.time() - t0)))

    def backfill_room(self) -> int:
        """Re-open dropped ('never qualified') migrations still inside the watch window."""
        now = self.c.clock()
        cutoff = now - watch_window_min(self.c.s) * 60.0
        rows = self.c.db.rows(
            "SELECT mint FROM coins WHERE status='dropped' AND migrated_at>=? AND (reason='never qualified' OR reason IS NULL)",
            (cutoff,))
        for r in rows:
            self.c.db.x("UPDATE coins SET status='room', reason=NULL WHERE mint=?", (r["mint"],))
        return len(rows)

    def room(self):
        return self.c.db.rows("SELECT * FROM coins WHERE status='room'")

    # ---------- re-check of fixable kills (thin pool / top 10 / already pumped)
    def recheck_queue_len(self) -> int:
        return self.c.db.one("SELECT COUNT(*) n FROM coins WHERE status='recheck'")["n"]

    def schedule_recheck(self, mint, migrated_at, reason, now) -> float | None:
        """Park a fixable kill until its next re-check age. None = no slot left (schedule spent / queue full)."""
        if not fixable(reason):
            return None
        nxt = next_recheck_at(self.c.s, migrated_at, now)
        if nxt is None:
            return None
        if self.recheck_queue_len() >= self.c.s["scanner.recheck_max_queue"]:
            return None
        self.c.db.x("INSERT INTO rechecks(mint,n,next_at,first_reason,last_reason,updated) VALUES(?,0,?,?,?,?) "
                    "ON CONFLICT(mint) DO UPDATE SET next_at=excluded.next_at, last_reason=excluded.last_reason, updated=excluded.updated",
                    (mint, nxt, reason, reason, now))
        return nxt

    def backfill_rechecks(self) -> int:
        """At start: recent fixable kills that never got a re-check schedule (killed before this feature / restart)."""
        offs = recheck_offsets(self.c.s)
        if not offs:
            return 0
        now = self.c.clock()
        rows = self.c.db.rows(
            "SELECT c.mint, c.migrated_at, c.reason, c.liq_at_scan, c.top10, c.h1 FROM coins c LEFT JOIN rechecks r ON r.mint=c.mint "
            "WHERE c.status='killed' AND c.sent=0 AND r.mint IS NULL AND c.migrated_at>=? ORDER BY c.migrated_at DESC",
            (now - offs[-1] * 60.0,))
        n = 0
        for r in rows:
            if not fixable(r["reason"]):
                continue
            ok, _ = near_miss(self.c.s, r["liq_at_scan"], _mc_from_reason(r["reason"]), r["top10"], r["h1"])
            if not ok:                                    # same near-miss gate as a live first kill
                continue
            nxt = self.schedule_recheck(r["mint"], r["migrated_at"], r["reason"], now)
            if nxt is not None:
                self.c.db.x("UPDATE coins SET status='recheck' WHERE mint=?", (r["mint"],))
                n += 1
        return n

    def due_rechecks(self, now) -> list[dict]:
        """Coins whose re-check time has come, bounded per poll: pools younger than
        scanner.young_priority_min first (youngest first), then the rest oldest-due first."""
        young = float(_get(self.c.s, "scanner.young_priority_min", 0) or 0)
        young_cut = now - young * 60.0 if young > 0 else float("inf")
        rows = self.c.db.rows(
            "SELECT c.*, r.n AS recheck_n FROM coins c JOIN rechecks r ON r.mint=c.mint "
            "WHERE c.status='recheck' AND r.next_at<=? "
            "ORDER BY (c.migrated_at < ?), CASE WHEN c.migrated_at >= ? THEN -c.migrated_at ELSE r.next_at END LIMIT ?",
            (now, young_cut, young_cut, int(self.c.s["scanner.recheck_per_poll"])))
        limit = watch_window_min(self.c.s) * 60.0
        out = []
        for r in rows:
            n = int(r.pop("recheck_n") or 0) + 1
            if now - r["migrated_at"] >= limit:          # never past scanner max age (24h horizon when off)
                self.c.db.x("UPDATE coins SET status='killed' WHERE mint=?", (r["mint"],))
                continue
            self.c.db.x("UPDATE rechecks SET n=?, next_at=NULL, updated=? WHERE mint=?", (n, now, r["mint"]))
            r["recheck_n"] = n
            out.append(r)
        return out

    def recheck_n(self, mint) -> int:
        r = self.c.db.one("SELECT n FROM rechecks WHERE mint=?", (mint,))
        return int(r["n"] or 0) if r else 0

    def runners(self):
        return self.c.db.rows("SELECT mint,name,symbol FROM coins WHERE runner=1 AND first_seen>=?", (time.time() - 86400,))

    async def poll_once(self):
        self.c.hb("scanner", f"poll room={len(self.room())}")
        n = self.backfill_room()
        if n:
            self.log.info(f"{iso()}\tbackfill\treopened {n} dropped mint(s) under {age_label(self.c.s)}")
        try:
            added = await self.catchup_graduations()
            if added:
                self.log.info(f"{iso()}\tcatchup\tadmitted {added} missed graduation(s) from new_pools")
        except Exception as e:
            self.c.error("scanner", "catchup", str(e)[:300])
        try:
            added = await self.dex_latest_intake()
            if added:
                self.log.info(f"{iso()}\tdex_latest\tadmitted {added} new non-pump coin(s) from DexScreener profiles/boosts")
        except RateLimited:
            raise
        except Exception as e:
            self.c.error("scanner", "dex_latest", str(e)[:300])
        room = self.room()
        due = self.due_rechecks(self.c.clock())
        for coin in due:
            self.log.info(f"{iso()}\t{coin['mint']}\trecheck\t{coin['recheck_n']}\tage={int((self.c.clock() - coin['migrated_at']) / 60)}m\twas={coin.get('reason') or ''}")
        queue = {c["mint"]: c for c in room}
        for c in due:
            queue[c["mint"]] = c
        for c in self.take_surges():
            if c["mint"] in queue:
                queue[c["mint"]]["surge"] = c["surge"]
            else:
                queue[c["mint"]] = c
        room = prioritize(self.c.s, list(queue.values()), self.c.clock())
        if not room:
            return
        pairs = await self.c.net.dex_tokens([r["mint"] for r in room])
        now = self.c.clock()
        thr = self.c.s["scanner.runner_h24_pct"]
        for coin in room:
            if coin["mint"] in self._capturing:
                continue                                  # dev wallet not recorded yet (an old-enough coin could pass at once)
            pair = pick_pair(pairs, coin["mint"])
            if not sources.is_pump(coin["mint"]):
                coin["first_pool_at"] = sources.first_pool_at(token_pairs(pairs, coin["mint"]))
            if pair:
                price = fields.get(pair, "priceUsd")
                h24 = fields.get(pair, "priceChange.h24")
                self.c.db.x("UPDATE coins SET name=?, symbol=?, pool=?, price=?, quote=?, h1=?, h24=?, runner=? WHERE mint=?",
                            (fields.get(pair, "baseToken.name"), fields.get(pair, "baseToken.symbol"),
                             fields.get(pair, "pairAddress"), float(price) if price else None,
                             fields.get(pair, "quoteToken.address"), fields.get(pair, "priceChange.h1"), h24,
                             1 if (h24 is not None and float(h24) >= thr) else 0, coin["mint"]))
                if price:
                    self.c.db.x("INSERT INTO prices(ts,mint,price,vol_m5,src) VALUES(?,?,?,?,?)",
                                (now, coin["mint"], float(price), fields.get(pair, "volume.m5"), "scanner"))
            top10 = None
            pre, _ = decide(self.c.s, coin, pair, None, [], now)
            if pair and pre == "wait" and (now - coin["migrated_at"]) / 60.0 >= 0:
                self.c.hb("scanner", f"top10 {coin['mint'][:8]}")
                top10 = await self.top10(coin["mint"], fields.get(pair, "pairAddress"))
            verdict, reason = decide(self.c.s, coin, pair, top10, self.runners(), now)
            if verdict != "wait":
                self.emit(coin, pair, top10, verdict, reason, now)
            elif coin.get("recheck_n") or (coin.get("surge") and coin.get("status") != "room"):
                self.requeue(coin, pair, top10, now)

    def requeue(self, coin, pair, top10, now):
        """A re-checked coin that now clears the kill filters but still needs age/top-10: back to the waiting room."""
        mint, n = coin["mint"], coin.get("recheck_n") or 0
        reason = f"recheck {n}: clears filters, back in waiting room" if n else "surge: clears filters, back in waiting room"
        self.c.db.x("UPDATE coins SET status='room', reason=NULL WHERE mint=?", (mint,))
        out = {"agent": "scanner", "mint": mint, "recheck": n,
               "liquidityUsd": float(fields.get(pair, "liquidity.usd") or 0) if pair else None,
               "marketCapUsd": market_cap(pair),
               "top10Pct": top10, "h1ChangePct": fields.get(pair, "priceChange.h1") if pair else None,
               "ageMin": int((now - coin["migrated_at"]) / 60), "verdict": "recheck", "reason": reason, "ts": iso(now)}
        if coin.get("surge"):
            out["surge"] = coin["surge"]
        self.c.db.x("INSERT INTO scans(ts,mint,verdict,reason,json,night) VALUES(?,?,?,?,?,?)",
                    (now, mint, "recheck", reason, json.dumps(out), self.c.night))
        self.log.info(f"{out['ts']}\t{mint}\trecheck\t{reason}\tliq={out['liquidityUsd']}\tmc={out['marketCapUsd']}\ttop10={top10}\th1={out['h1ChangePct']}\tage={out['ageMin']}")
        self.c.event("scan", mint)

    async def top10(self, mint, pool):
        try:
            supply = self.supply_cache.get(mint)
            if supply is None:
                r = await self.c.net.rpc("getTokenSupply", [mint])
                supply = int(r["value"]["amount"])
                self.supply_cache[mint] = supply
            accts = await holders.fetch_accounts(self.c.net, mint)
            ex = holders.excluded_owners(pool, mint, (self.extra.get(mint) or {}).get("amm"))
            return holders.top10_pct(accts, supply, ex)
        except Exception as e:
            self.c.error("scanner", "top10", f"{mint} {str(e)[:200]}")
            return None

    def emit(self, coin, pair, top10, verdict, reason, now):
        mint = coin["mint"]
        row = self.c.coin(mint) or {}
        out = {"agent": "scanner", "mint": mint,
               "name": (fields.get(pair, "baseToken.name") if pair else None) or row.get("name"),
               "symbol": (fields.get(pair, "baseToken.symbol") if pair else None) or row.get("symbol"),
               "pool": fields.get(pair, "pairAddress") if pair else None,
               "liquidityUsd": float(fields.get(pair, "liquidity.usd") or 0) if pair else None,
               "marketCapUsd": market_cap(pair),
               "top10Pct": top10,
               "h1ChangePct": fields.get(pair, "priceChange.h1") if pair else None,
               "ageMin": int((now - coin["migrated_at"]) / 60),
               "verdict": "pass" if verdict == "pass" else "kill",
               "reason": reason, "ts": iso(now),
               "source": sources.launchpad(mint, row.get("pool_label"), fields.get(pair, "dexId") if pair else None),
               "dexId": fields.get(pair, "dexId") if pair else None}
        if coin.get("surge"):
            out["surge"] = coin["surge"]
        status = {"pass": "passed", "kill": "killed", "drop": "dropped"}[verdict]
        n = self.recheck_n(mint)
        scan_verdict, scan_reason = out["verdict"], reason
        nxt = None
        if verdict == "kill":
            if not n and fixable(reason):              # near-miss gate applies at the first kill only
                ok, far = near_miss(self.c.s, out["liquidityUsd"], out["marketCapUsd"], top10, out["h1ChangePct"])
                if not ok:
                    reason = f"{reason} · {TOO_FAR}"
                    out["reason"], scan_reason = reason, reason
                    out["tooFar"] = far
            nxt = self.schedule_recheck(mint, coin["migrated_at"], reason, now)
        if nxt is not None:
            status = "recheck"
            out["recheckAt"] = iso(nxt)
            out["recheckAgeMin"] = int(round((nxt - coin["migrated_at"]) / 60))
        if n:
            out["recheck"] = n
            if verdict == "pass":
                scan_reason = f"recheck {n}: passed"
            else:                                   # a later re-check result, not a new kill (keeps kill counts honest)
                scan_verdict = "recheck"
                tail = f"next at {out['recheckAgeMin']}m" if nxt is not None else "final"
                scan_reason = f"recheck {n}: {reason} · {tail}"
            out["verdict"], out["reason"] = scan_verdict, scan_reason
            if verdict == "pass":
                out["verdict"] = "pass"
        self.c.db.x("UPDATE coins SET status=?, reason=?, liq_at_scan=?, top10=? WHERE mint=?",
                    (status, scan_reason if n else reason, out["liquidityUsd"], top10, mint))
        if out.get("surge"):                               # the scans row says why it was scanned now; coins.reason stays plain
            tag = stage_pings.surge_label(out["surge"])
            scan_reason = f"{tag} · {scan_reason}" if scan_reason else tag
        self.c.db.x("INSERT INTO scans(ts,mint,verdict,reason,json,night) VALUES(?,?,?,?,?,?)",
                    (now, mint, scan_verdict, scan_reason, json.dumps(out), self.c.night))
        self.log.info(f"{out['ts']}\t{mint}\t{out['verdict']}\t{out['reason'] or ''}" + (f" · recheck at {out['recheckAgeMin']}m" if nxt is not None and not n else "") + f"\tliq={out['liquidityUsd']}\tmc={out['marketCapUsd']}\ttop10={top10}\th1={out['h1ChangePct']}\tage={out['ageMin']}\tsrc={out['source']}")
        if verdict == "pass":
            sent = self.c.db.one("SELECT sent FROM coins WHERE mint=?", (mint,))
            if not (sent and sent["sent"]):                     # never sends the same mint twice
                self.c.db.x("UPDATE coins SET sent=1 WHERE mint=?", (mint,))
                if self.c.bus:
                    self.c.bus.auditor.put_nowait(out)
                stage_pings.ping(self.c, "scanner", mint, stage_pings.scanner_text(out), stage_pings.coin_button(mint))
        self.c.event("scan", mint)
        return out
