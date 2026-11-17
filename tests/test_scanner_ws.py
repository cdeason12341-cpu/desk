"""PumpPortal WS liveness (no silence-kill) and GeckoTerminal graduation catchup."""
import asyncio, json
from contextlib import asynccontextmanager

from agents.scanner import CATCHUP_LOOKBACK_S, Scanner, parse_new_pools
from tests.helpers import make_ctx, run

PUMP = "Catch11111111111111111111111111111111pump"
OTHER = "Catch22222222222222222222222222222222pump"
OLD = "Catch33333333333333333333333333333333pump"
NOTPUMP = "J" * 44
NOW = 1_800_000_000.0


def _pool(mint, created_iso, dex="pumpswap", quote="solana_So11111111111111111111111111111111111111112", name="X / SOL"):
    return {
        "id": "solana_" + mint[:8],
        "type": "pool",
        "attributes": {"name": name, "address": "Pool" + mint[:8], "pool_created_at": created_iso},
        "relationships": {
            "base_token": {"data": {"id": "solana_" + mint, "type": "token"}},
            "quote_token": {"data": {"id": quote, "type": "token"}},
            "dex": {"data": {"id": dex, "type": "dex"}},
        },
    }


def test_parse_new_pools_keeps_only_pumpswap_pump_sol():
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(NOW - 30, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"data": [
        _pool(PUMP, ts),
        _pool("HODL" + "1" * 40, ts, dex="pumpswap"),          # not a pump mint
        _pool("Bond" + "1" * 36 + "pump", ts, dex="pump-fun"),  # bonding curve, not graduated
        _pool("UsdC" + "1" * 36 + "pump", ts, quote="solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
        {"no": "relationships"},
    ]}
    rows = parse_new_pools(payload)
    assert [r["mint"] for r in rows] == [PUMP]
    assert abs(rows[0]["created_at"] - (NOW - 30)) < 1
    assert parse_new_pools({"data": []}) == []
    assert parse_new_pools(None) == []


def test_catchup_admits_recent_missing_and_skips_existing_or_old():
    c, clock = make_ctx()
    clock.t = NOW
    # already known
    c.db.x("INSERT INTO coins(mint,migrated_at,first_seen,status,night) VALUES(?,?,?,?,?)",
           (OTHER, NOW - 10, NOW - 10, "room", c.night))
    from datetime import datetime, timezone
    iso = lambda age: datetime.fromtimestamp(NOW - age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # page 1: new + already known + too old (6h, past max_age_h=5)
    c.net.new_pools[1] = {"data": [
        _pool(PUMP, iso(20)),
        _pool(OTHER, iso(15)),
        _pool(OLD, iso(6 * 3600)),
        _pool(NOTPUMP, iso(20), dex="pumpswap"),
    ]}
    c.net.new_pools[2] = {"data": []}
    sc = Scanner(c)
    n = run(sc.catchup_graduations())
    assert n == 1  # only PUMP: existing, too-old, and non-pump are skipped
    assert c.coin(PUMP)["status"] == "room"
    assert c.coin(PUMP)["pool_label"] == "pumpswap"
    assert abs(c.coin(PUMP)["migrated_at"] - (NOW - 20)) < 1
    assert c.coin(OTHER)["status"] == "room"  # unchanged
    assert c.coin(OLD) is None
    assert c.coin(NOTPUMP) is None
    assert run(sc.catchup_graduations()) == 0  # idempotent


def test_poll_once_runs_catchup_before_dex_and_empty_net_is_fine():
    c, clock = make_ctx()
    clock.t = NOW
    sc = Scanner(c)
    run(sc.poll_once())  # no pools, no room — must not raise
    assert c.db.rows("SELECT * FROM coins") == []
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(NOW - 10, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    c.net.new_pools[1] = {"data": [_pool(PUMP, ts)]}
    run(sc.poll_once())
    assert c.coin(PUMP)["status"] == "room"


class _FakeWS:
    def __init__(self, messages, then_exc=None, hang_s=0, hang_pong=False):
        self._messages = list(messages)
        self._then_exc = then_exc
        self._hang_s = hang_s
        self._hang_pong = hang_pong
        self._dead = asyncio.Event()
        self.sent = []
        self.pings = 0
        self.aborted = False

    async def send(self, raw):
        self.sent.append(raw)

    async def ping(self):
        self.pings += 1
        if self._dead.is_set():
            raise ConnectionError("closed")
        if self._hang_pong:
            await self._dead.wait()
            raise ConnectionError("aborted")
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(0.001)
        return fut

    def abort(self):
        self.aborted = True
        self._dead.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        if self._hang_s:
            try:
                await asyncio.wait_for(self._dead.wait(), timeout=self._hang_s)
            except asyncio.TimeoutError:
                pass
            else:
                raise ConnectionError("aborted")
        if self._then_exc:
            raise self._then_exc
        if self._dead.is_set():
            raise ConnectionError("aborted")
        raise StopAsyncIteration


def test_ws_loop_stays_open_through_silence_then_handles_migration():
    """A quiet socket must not be logged as a reconnect. Protocol close still is."""
    c, clock = make_ctx()
    clock.t = NOW
    sc = Scanner(c)
    silent_ws = _FakeWS([], hang_s=0.05)  # silence, then clean end — not a reconnect
    drop_ws = _FakeWS([json.dumps({"mint": PUMP, "signature": "sig", "pool": "pump-amm"})],
                      then_exc=ConnectionError("keepalive ping timeout"))
    calls = {"n": 0}

    @asynccontextmanager
    async def connect():
        calls["n"] += 1
        yield silent_ws if calls["n"] == 1 else drop_ws

    async def run_loop():
        task = asyncio.create_task(sc.ws_loop(connect=connect))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(run_loop())
    kinds = [r["kind"] for r in c.db.rows("SELECT kind, note FROM ws_events ORDER BY id")]
    notes = [r["note"] for r in c.db.rows("SELECT note FROM ws_events WHERE kind='reconnect'")]
    assert kinds.count("open") >= 1
    assert all("silent" not in (n or "") for n in notes)
    assert c.coin(PUMP) is not None
    # the ping-timeout close is a real reconnect
    assert any("keepalive ping timeout" in (n or "") for n in notes)


def test_ws_loop_aborts_and_reconnects_when_pong_hangs():
    """A hung ping/drain must abort the socket and reconnect — not wait for TCP timeout."""
    c, clock = make_ctx()
    clock.t = NOW
    sc = Scanner(c)
    hung = _FakeWS([], hang_s=30, hang_pong=True)
    ok = _FakeWS([json.dumps({"mint": PUMP, "signature": "sig", "pool": "pump-amm"})], hang_s=0.2)
    calls = {"n": 0}

    @asynccontextmanager
    async def connect():
        calls["n"] += 1
        yield hung if calls["n"] == 1 else ok

    async def run_loop():
        task = asyncio.create_task(sc.ws_loop(connect=connect, ping_interval=0.05, ping_timeout=0.05, backoff_start=0.05))
        await asyncio.sleep(0.35)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(run_loop())
    assert hung.aborted and hung.pings >= 1
    assert calls["n"] >= 2
    notes = [r["note"] for r in c.db.rows("SELECT note FROM ws_events WHERE kind='reconnect'")]
    assert any("pong timeout" in (n or "") for n in notes)
    assert c.coin(PUMP) is not None
