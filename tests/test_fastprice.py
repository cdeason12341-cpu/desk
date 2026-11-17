"""DECISION 57: real-time exit prices for open LIVE GMGN positions (owner 2026-11-16 6:52 PM CT, after DexScreener
lagged Petryk The Hare's pool by 20-80 s). Pool reserves over RPC -> GMGN sell quote -> DexScreener."""
import asyncio, base64, struct
import pytest
import config
import fastprice
import poolprice as pp
from fastprice import FastPrice
from solana import b58decode, b58encode
from tests.helpers import make_ctx, pair, run
from tests.test_gmgn import M
from tests.test_gmgn_grace import GraceClient, _held, _live, _nosleep
import net as netmod

WSOL = pp.WSOL


def key(n):
    """Deterministic fake pubkey #n."""
    return b58encode(bytes([n]) * 32)


def acc(data: bytes, owner: str):
    return {"owner": owner, "data": [base64.b64encode(data).decode(), "base64"]}


def put_pk(buf, off, k):
    buf[off:off + 32] = b58decode(k)


def token_acc(amount):
    b = bytearray(165)
    struct.pack_into("<Q", b, 64, amount)
    return bytes(b)


def mint_acc(dec):
    b = bytearray(82)
    b[44] = dec
    return bytes(b)


def pumpswap_pool(mint, base_vault, quote_vault, adj=0, base=None, quote=WSOL, n=301):
    b = bytearray(n)
    put_pk(b, 43, base or mint); put_pk(b, 75, quote)
    put_pk(b, 139, base_vault); put_pk(b, 171, quote_vault)
    if n >= 253:
        struct.pack_into("<q", b, 245, adj)
    return bytes(b)


# ---------------- parsers

def test_pumpswap_vaults_plus_signed_quote_adjustment():
    mint, va, vb = key(1), key(2), key(3)
    spec = pp.describe("P", pp.PUMP_AMM, pumpswap_pool(mint, va, vb, adj=17_580_000_000), mint)
    assert spec["kind"] == "pumpswap" and spec["accounts"] == ["P", va, vb] and spec["a_is_mint"]
    spec["dec_a"], spec["dec_b"] = 6, 9
    datas = [pumpswap_pool(mint, va, vb, adj=17_580_000_000), token_acc(573_081_636 * 10**6), token_acc(27_172_093_405)]
    # 51KN4u live numbers 2026-11-16: (27.172 + 17.580 SOL) / 573.08M tokens = 7.809e-08 (trades 7.74e-08..7.87e-08)
    assert pp.price_in_quote(spec, datas) == pytest.approx(7.809e-08, rel=1e-3)
    datas[0] = pumpswap_pool(mint, va, vb, adj=-27_172_093_405)
    with pytest.raises(pp.Unsupported):
        pp.price_in_quote(spec, datas)                      # adjusted reserve <= 0: unusable


def test_pumpswap_reversed_pool_and_quote_checks():
    mint, va, vb = key(1), key(2), key(3)
    spec = pp.describe("P", pp.PUMP_AMM, pumpswap_pool(mint, va, vb, base=WSOL, quote=mint, adj=0), mint)
    assert not spec["a_is_mint"] and spec["quote"] == WSOL
    spec["dec_a"], spec["dec_b"] = 9, 6
    p = pp.price_in_quote(spec, [pumpswap_pool(mint, va, vb, base=WSOL, quote=mint), token_acc(65 * 10**9), token_acc(922_629_589 * 10**6)])
    assert p == pytest.approx(65 / 922_629_589, rel=1e-6)
    with pytest.raises(pp.Unsupported):
        pp.describe("P", pp.PUMP_AMM, pumpswap_pool(mint, va, vb, quote=key(9)), mint)       # not SOL/USD quoted
    with pytest.raises(pp.Unsupported):
        pp.describe("P", pp.PUMP_AMM, pumpswap_pool(key(7), va, vb), mint)                    # other coin's pool
    with pytest.raises(pp.Unsupported):
        pp.describe("P", "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB", bytes(944), mint)   # Meteora DAMM v1


def test_pump_bonding_curve():
    b = bytearray(166)
    struct.pack_into("<QQ", b, 8, 1_000_000_000 * 10**6, 30 * 10**9)
    spec = pp.describe("C", pp.PUMP_PROGRAM, bytes(b), key(1))
    assert spec["kind"] == "pump_curve"
    assert pp.price_in_quote(spec, [bytes(b)]) == pytest.approx(3e-8)
    done = bytearray(b); done[48] = 1
    with pytest.raises(pp.Unsupported):
        pp.price_in_quote(spec, [bytes(done)])
    other = bytearray(b); put_pk(other, 83, key(5))           # curve quoted in another token
    with pytest.raises(pp.Unsupported):
        pp.describe("C", pp.PUMP_PROGRAM, bytes(other), key(1))


def test_raydium_v4_subtracts_need_take_pnl():
    mint, va, vb = key(1), key(2), key(3)
    b = bytearray(752)
    struct.pack_into("<QQ", b, 32, 6, 9)
    struct.pack_into("<QQ", b, 192, 10 * 10**6, 1 * 10**9)
    put_pk(b, 336, va); put_pk(b, 368, vb); put_pk(b, 400, mint); put_pk(b, 432, WSOL)
    spec = pp.describe("R", pp.RAY_V4, bytes(b), mint)
    p = pp.price_in_quote(spec, [bytes(b), token_acc(110 * 10**6), token_acc(11 * 10**9)])
    assert p == pytest.approx(10 / 100)                       # (11-1) SOL / (110-10) tokens


def test_clmm_whirlpool_damm_v2_and_dlmm():
    mint = key(1)
    sq = int((2.5e-9 * 10**(9 - 6)) ** 0.5 * 2**64)           # token a (6 dp) priced 2.5e-9 SOL... in raw units
    for kind, prog, n, ma, mb, sq_off in (("clmm", pp.RAY_CLMM, 1544, 73, 105, 253), ("whirlpool", pp.ORCA_WP, 653, 101, 181, 65),
                                          ("damm_v2", pp.MET_DAMM2, 1112, 168, 200, 456)):
        b = bytearray(n)
        put_pk(b, ma, mint); put_pk(b, mb, WSOL)
        struct.pack_into("<QQ", b, sq_off, sq & (2**64 - 1), sq >> 64)
        spec = pp.describe("X", prog, bytes(b), mint)
        assert spec["kind"] == kind and spec["accounts"] == ["X"]
        spec["dec_a"], spec["dec_b"] = 6, 9
        assert pp.price_in_quote(spec, [bytes(b)]) == pytest.approx(2.5e-9, rel=1e-6)
    b = bytearray(904)
    put_pk(b, 88, WSOL); put_pk(b, 120, mint)                 # DLMM with SOL as token x
    struct.pack_into("<i", b, 76, -100); struct.pack_into("<H", b, 80, 25)
    spec = pp.describe("D", pp.MET_DLMM, bytes(b), mint)
    spec["dec_a"], spec["dec_b"] = 9, 6
    p_yx = (1.0025 ** -100) * 10 ** (9 - 6)                    # token y per token x
    assert pp.price_in_quote(spec, [bytes(b)]) == pytest.approx(1 / p_yx, rel=1e-9)


# ---------------- the feed

class Net:
    """Fake chain + DexScreener with call counters and switchable 429s."""

    def __init__(self, accounts, dex_prices, jup=None):
        self.accounts, self.dex_prices, self.jup = accounts, dex_prices, jup or {}
        self.calls = []
        self.fail = None
        self.http = None

    async def rpc(self, method, params, timeout=30):
        self.calls.append(method)
        if self.fail == "429":
            raise netmod.RateLimited("rpc 429 " + method)
        if self.fail == "err":
            raise netmod.SourceError("rpc 500")
        if method == "getAccountInfo":
            return {"value": self.accounts.get(params[0])}
        if method == "getMultipleAccounts":
            return {"value": [self.accounts.get(k) for k in params[0]]}
        raise RuntimeError(method)

    async def dex_tokens(self, mints):
        self.calls.append("dex")
        out = []
        for m in mints:
            if m in self.dex_prices:
                p = pair(m, price=self.dex_prices[m])
                p["pairAddress"] = "POOL" + m[:4]
                out.append(p)
        return out


class Trader:
    def __init__(self, client=None):
        self.client, self.plan_blocked_at, self.logs = client, 0.0, []

    def _log(self, *parts):
        self.logs.append(" ".join(str(p) for p in parts if p))

    async def sol_usd(self):
        return 100.0


class QuoteClient:
    def __init__(self, out=None, fail=False):
        self.out, self.fail, self.calls = out, fail, []

    async def quote(self, inp, outp, amount, slippage):
        self.calls.append(("quote", inp, outp, amount))
        if self.fail:
            raise RuntimeError("quote down")
        return {"output_amount": str(self.out(amount))}

    async def swap(self, body):                                # must never be called by the feed
        raise AssertionError("price feed must never trade")


def _world(m1="27Ggmg2ZmaehHX7L5vZqCeTRoQmPaBtR5vqvaRLbpump", m2="EvuHJhGvEYiWVfdFKW9DhuNm4STxhV2x7mkRwBaupump", p1=1e-6, p2=4e-6):
    """Two PumpSwap pools; SOL $100. Pool prices in SOL: m1 p1, m2 p2 (USD = x100)."""
    accounts = {}
    for i, (m, p) in enumerate(((m1, p1), (m2, p2))):
        va, vb = key(10 + 2 * i), key(11 + 2 * i)
        tokens = 1_000_000 * 10**6
        accounts["POOL" + m[:4]] = acc(pumpswap_pool(m, va, vb), pp.PUMP_AMM)
        accounts[va] = acc(token_acc(tokens), pp.TOKEN_PROGRAM if hasattr(pp, "TOKEN_PROGRAM") else "Tok")
        accounts[vb] = acc(token_acc(int(p * 1_000_000 * 10**9)), "Tok")
        accounts[m] = acc(mint_acc(6), "Tok")
    accounts[WSOL] = acc(mint_acc(9), "Tok")
    return accounts


def _feed(dex=None, client=None, **settings):
    c, clock = make_ctx(**settings)
    m1, m2 = "27Ggmg2ZmaehHX7L5vZqCeTRoQmPaBtR5vqvaRLbpump", "EvuHJhGvEYiWVfdFKW9DhuNm4STxhV2x7mkRwBaupump"
    n = Net(_world(m1, m2), dex if dex is not None else {m1: 1e-4, m2: 4e-4})
    c.net = n
    t = Trader(client)
    f = FastPrice(c, t, clock=clock)
    return c, clock, f, n, t, m1, m2


def test_settings_new_and_not_protected():
    c, _ = make_ctx()
    assert c.s["risk.price_source"] == "pool" and c.s["risk.fast_poll_s"] == 1.5
    assert "risk.price_source" not in config.PROTECTED and "risk.fast_poll_s" not in config.PROTECTED
    assert c.s.set("price source", "dex", source="test") == "price source pool -> dex"
    with pytest.raises(config.SettingError):
        c.s.set("fast poll", "0.1", source="test")


def test_pool_prices_one_rpc_call_per_tick_for_all_mints():
    c, clock, f, n, t, m1, m2 = _feed()
    px = run(f.prices([m1, m2]))
    assert px[m1] == (pytest.approx(1e-4), "pool:pumpswap") and px[m2] == (pytest.approx(4e-4), "pool:pumpswap")
    assert any("pool price check ok" in l for l in t.logs)
    assert any("price source pool:pumpswap" in l for l in t.logs)
    n.calls.clear()
    run(f.prices([m1, m2]))                                     # same instant: served from cache
    assert n.calls == []
    clock.t += 1.5
    run(f.prices([m1, m2]))
    assert n.calls == ["getMultipleAccounts"]                  # one batched call, no DexScreener
    n.accounts[key(11)] = acc(token_acc(int(0.5e-6 * 1_000_000 * 10**9)), "Tok")   # m1's pool halves
    clock.t += 1.5
    assert run(f.prices([m1]))[m1][0] == pytest.approx(0.5e-4)


def test_429_backs_off_then_gmgn_quote_then_dex():
    qc = QuoteClient(out=lambda amount: int(amount / 1e6 * 1e-6 * 0.99 * 1e9))     # 1% under pool
    c, clock, f, n, t, m1, m2 = _feed(client=qc)
    c.db.x("INSERT INTO gmgn_orders(mint,status,mode,entry_price) VALUES(?,?,?,?)", (m1, "open", "LIVE", 1e-4))
    run(f.prices([m1]))                                         # resolves + pool price
    n.fail = "429"
    clock.t += 1.5
    px = run(f.prices([m1]))
    assert px[m1][1] == "gmgn_quote" and px[m1][0] == pytest.approx(0.99e-4, rel=1e-3)
    assert f.rpc_backoff == 2.0 and f.stats["rpc_429"] == 1
    assert qc.calls and qc.calls[0][1] == m1 and qc.calls[0][2] == "So11111111111111111111111111111111111111112"
    assert qc.calls[0][3] == 10_000 * 10**6                     # ~$1 of the token at entry 1e-4
    n.calls.clear()
    clock.t += 1.45                                             # inside the 2 s backoff: no RPC; quote gap 2 s -> dex
    px = run(f.prices([m1]))
    assert "getMultipleAccounts" not in n.calls and px[m1][1] == "dex"
    clock.t += 1.5                                              # still 429: backoff doubles
    run(f.prices([m1]))
    assert f.rpc_backoff == 4.0
    n.fail = None
    clock.t += 5
    assert run(f.prices([m1]))[m1][1] == "pool:pumpswap"        # recovered, backoff reset
    assert f.rpc_backoff == 0.0
    assert any("price source gmgn_quote" in l and "rpc backoff" not in l or "why=rpc" in l for l in t.logs)


def test_quote_failure_backs_off_and_dex_is_last():
    qc = QuoteClient(fail=True)
    c, clock, f, n, t, m1, m2 = _feed(client=qc)
    c.db.x("INSERT INTO gmgn_orders(mint,status,mode,entry_price) VALUES(?,?,?,?)", (m1, "open", "LIVE", 1e-4))
    n.fail = "err"
    px = run(f.prices([m1]))
    assert px[m1] == (pytest.approx(1e-4), "dex")
    assert len(qc.calls) == 1                                   # tried once (pump mint: 6 decimals), failed
    assert f.quote_until == clock.t + fastprice.QUOTE_BACKOFF_S
    clock.t += 3
    assert run(f.prices([m1]))[m1][1] == "dex"
    assert len(qc.calls) == 1                                   # quote backed off 30 s


def test_sanity_check_rejects_pool_far_from_dex_without_jupiter():
    c, clock, f, n, t, m1, m2 = _feed(dex={"27Ggmg2ZmaehHX7L5vZqCeTRoQmPaBtR5vqvaRLbpump": 3e-4, "EvuHJhGvEYiWVfdFKW9DhuNm4STxhV2x7mkRwBaupump": 4e-4})
    px = run(f.prices([m1, m2]))
    assert px[m1] == (pytest.approx(3e-4), "dex")               # pool 1e-4 vs dex 3e-4: not trusted
    assert px[m2][1] == "pool:pumpswap"
    assert any("pool price check FAILED" in l for l in t.logs)
    assert "bad" in f.specs[m1]


def test_sanity_check_accepts_pool_when_jupiter_agrees(monkeypatch):
    c, clock, f, n, t, m1, m2 = _feed(dex={"27Ggmg2ZmaehHX7L5vZqCeTRoQmPaBtR5vqvaRLbpump": 3e-4, "EvuHJhGvEYiWVfdFKW9DhuNm4STxhV2x7mkRwBaupump": 4e-4})

    async def jup(mint):
        return 1.01e-4 if mint == m1 else None
    monkeypatch.setattr(f, "_jup_price", jup)
    px = run(f.prices([m1]))
    assert px[m1][1] == "pool:pumpswap"
    assert any("pool price check ok (jupiter)" in l for l in t.logs)


def test_price_source_dex_uses_only_dexscreener():
    c, clock, f, n, t, m1, m2 = _feed(**{"risk.price_source": "dex"})
    px = run(f.prices([m1, m2]))
    assert {v[1] for v in px.values()} == {"dex"} and n.calls == ["dex"]


def test_dexscreener_fallback_respects_3s_gap():
    c, clock, f, n, t, m1, m2 = _feed(**{"risk.price_source": "dex"})
    run(f.prices([m1]))
    clock.t += 1.5
    run(f.prices([m1]))
    assert n.calls.count("dex") == 1
    clock.t += 1.6
    run(f.prices([m1]))
    assert n.calls.count("dex") == 2


# ---------------- watchers use it (LIVE only)

class FakeFast:
    def __init__(self, price):
        self.price, self.asked = price, []

    def enabled(self):
        return True

    def poll_s(self):
        return 1.5

    def note(self):
        return "pool=1"

    async def prices(self, mints):
        self.asked.append(list(mints))
        return {m: (self.price, "pool:pumpswap") for m in mints}


def test_surge_watcher_sells_on_pool_price_not_dexscreener(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc)
    _held(t)
    assert run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))["status"] == "open"
    fc.calls.clear()
    c.net.pairs[M] = pair(M, price=1.0)                         # DexScreener stuck at entry (lagging)
    t.fast = FakeFast(1.0)
    assert t.exit_poll_s(3.0) == 1.5
    for p in (1.10, 1.25, 1.40, 1.27):                          # surge then -9% from the 1.40 top, pool only
        t.fast.price = p
        clock.t += 1.5
        run(t.surge_once())
    sells = [b for k, b in fc.calls if k == "swap" and b["input_token"] == M]
    assert len(sells) == 1
    assert all(M in a for a in t.fast.asked)
    assert c.db.one("SELECT exit_reason FROM gmgn_orders WHERE mint=?", (M,))["exit_reason"] == "surge top"
    assert not any(k == "swap" and b.get("input_token") != M for k, b in fc.calls)          # never a buy


def test_no_fast_feed_keeps_dexscreener_and_3s():
    c, clock, t = _live(GraceClient())
    assert t.fast is None and t.exit_poll_s(3.0) == 3.0


def test_trail_watcher_uses_pool_price(monkeypatch):
    from tests.test_gmgn_desk_trail import _promoted_limit
    c, clock, t, fc = _promoted_limit(monkeypatch)
    c.net.pairs[M] = pair(M, price=1.0)                         # DexScreener lagging at entry
    t.fast = FakeFast(1.0)
    for p in (1.30, 1.50, 1.19):                                # arms at +20%, then -20.7% from 1.50 on the pool
        t.fast.price = p
        clock.t += 1.5
        run(t.trail_once())
    sells = [b for k, b in fc.calls if k == "swap" and b["input_token"] == M]
    assert len(sells) == 1
    assert c.db.one("SELECT exit_reason FROM gmgn_orders WHERE mint=?", (M,))["exit_reason"] == "desk trail"
