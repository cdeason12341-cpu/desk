"""DECISION 63: Strategy B's own older-coin feed (GeckoTerminal trending, 1h-7d), 7-day max age, tonight-only wait."""
import ast, datetime as dt, pathlib
import pytest
import holders, strat_b, strat_b_feed
from strat_b_feed import BFeed, parse_pools, candidate_reason, SOURCES
from tests.test_strat_b import _sb, _pair, _tick
from tests.helpers import run

OLD = "OldB1111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


def _pool(mint=OLD, age_h=48, mc=300000, liq=40000, dex="pumpswap", quote=WSOL, now=1_800_000_000.0, name="Oldie"):
    created = dt.datetime.fromtimestamp(now - age_h * 3600, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": "solana_P" + mint[:10], "type": "pool",
            "attributes": {"address": "P" + mint[:10], "name": f"{name} / SOL", "pool_created_at": created,
                           "market_cap_usd": None, "fdv_usd": str(mc), "reserve_in_usd": str(liq)},
            "relationships": {"base_token": {"data": {"id": "solana_" + mint}}, "quote_token": {"data": {"id": "solana_" + quote}},
                              "dex": {"data": {"id": dex}}}}


def _accts(n_holders=200, top_each=10):
    # 10 whales holding top_each each, rest 1 unit; supply set to match below
    a = [{"pubkey": f"A{i}", "owner": f"O{i}", "amount": top_each, "delegate": None, "program": "t"} for i in range(10)]
    a += [{"pubkey": f"B{i}", "owner": f"Q{i}", "amount": 1, "delegate": None, "program": "t"} for i in range(n_holders - 10)]
    return a


def _feed(monkeypatch, accts=None, mint_auth=None, supply=1000, **settings):
    c, clock, sb = _sb(**settings)
    c.net.rpc_map["getAccountInfo"] = {"value": {"data": {"parsed": {"info": {"supply": str(supply), "mintAuthority": mint_auth, "freezeAuthority": None}}}}}

    async def fetch(net, mint, timeout=60):
        return accts if accts is not None else _accts()
    monkeypatch.setattr(holders, "fetch_accounts", fetch)
    return c, clock, sb, BFeed(sb)


def _payloads(*pools):
    return [{"data": list(pools)}] + [{"data": []}] * (len(SOURCES) - 1)


def test_parse_and_candidate_filter():
    c, clock, sb = _sb()
    now = clock.t
    ps = parse_pools({"data": [_pool(now=now)]}, "gecko trending 24h", now)
    assert ps[0]["mint"] == OLD and round(ps[0]["ageMin"]) == 2880 and ps[0]["mc"] == 300000 and ps[0]["quote"] == WSOL
    assert candidate_reason(c.s, ps[0]) is None
    bad = lambda **kw: candidate_reason(c.s, parse_pools({"data": [_pool(now=now, **kw)]}, "x", now)[0])
    assert bad(age_h=0.5) and bad(age_h=24 * 8)                  # 1h-7d only
    assert bad(mc=5_000_000) and bad(mc=10_000) and bad(liq=3000)
    assert bad(dex="pump-fun") and bad(quote="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")


def test_feed_checks_and_offers_older_coin(monkeypatch):
    c, clock, sb, f = _feed(monkeypatch, supply=1000)            # top 10 = 100/1000 = 10%, 200 holders
    note = run(f.poll_once(payloads=_payloads(_pool(now=clock.t))))
    w = c.db.one("SELECT * FROM strat_b_watch WHERE mint=?", (OLD,))
    assert w["status"] == "watching" and w["top10"] == 10.0 and w["holders"] == 200
    assert w["source"].startswith("gecko trending 1h · 2.0d old")
    assert "checked=1" in note
    ev = strat_b.live_feed(c)["items"][0]
    assert ev["source"].startswith("gecko trending") and ev["reason"].startswith("via gecko trending")
    assert ev["checks"][0]["k"] == "source"
    assert strat_b.live_feed(c)["counts"]["olderFeedWatching"] == 1
    run(f.poll_once(payloads=_payloads(_pool(now=clock.t))))     # never re-checks a known coin
    assert f.stats["checked"] == 1


def test_feed_skips_concentrated_and_mint_authority(monkeypatch):
    c, clock, sb, f = _feed(monkeypatch, supply=200)             # top 10 = 100/200 = 50%
    run(f.poll_once(payloads=_payloads(_pool(now=clock.t))))
    assert "top 10" in c.db.one("SELECT reason FROM strat_b_watch WHERE mint=?", (OLD,))["reason"]
    c2, clock2, sb2, f2 = _feed(monkeypatch, mint_auth="Auth111")
    run(f2.poll_once(payloads=_payloads(_pool(now=clock2.t))))
    assert c2.db.one("SELECT reason FROM strat_b_watch WHERE mint=?", (OLD,))["reason"] == "mint authority not revoked"


def test_feed_rate_limits_and_caps(monkeypatch):
    c, clock, sb, f = _feed(monkeypatch, **{"strat_b.feed_checks_per_poll": 2})
    pools = [_pool(mint=f"Old{i}" + "1" * 38, now=clock.t) for i in range(5)]
    run(f.poll_once(payloads=_payloads(*pools)))
    assert f.stats["checked"] == 2                               # at most feed_checks_per_poll RPC holder checks per poll
    assert run(f.poll_once()).startswith("idle")                 # live calls only every feed_poll_s (300s)
    c.s.set("strat_b.feed_enabled", False, source="test")
    assert run(f.poll_once(payloads=_payloads(*pools))) == "off"


def test_unreadable_holders_retry_later(monkeypatch):
    c, clock, sb, f = _feed(monkeypatch)

    async def boom(net, mint, timeout=60):
        raise RuntimeError("gpa down")
    monkeypatch.setattr(holders, "fetch_accounts", boom)
    run(f.poll_once(payloads=_payloads(_pool(now=clock.t))))
    assert c.db.one("SELECT * FROM strat_b_watch WHERE mint=?", (OLD,)) is None and OLD in f.failed


def test_seven_day_max_age_expiry_and_long_watch():
    c, clock, sb = _sb(**{"strat_b.watch_max_min": 10080})
    from tests.test_strat_b import _watch
    _watch(c, clock, sb, age_min=10080 - 30)                     # 6d 23.5h old
    _tick(sb, clock, 1.0)
    assert c.db.one("SELECT status FROM strat_b_watch")["status"] == "watching"
    clock.t += 3 * 3600                                          # longer than the old 3h cutoff, still within watch length
    _tick(sb, clock, 1.0)
    last = c.db.one("SELECT * FROM strat_b_evals ORDER BY id DESC LIMIT 1")
    assert c.db.one("SELECT status FROM strat_b_watch")["status"] == "expired" and "older than 7d" in last["reason"]


def test_tonight_only_wait_reverts_at_rollover():
    c, clock, sb = _sb()
    src = "chat: owner asked 2026-11-16 8:23 PM CT"
    sb.set_tonight("strat_b.min_age_min", 15, src, until=clock.t + 3600)
    assert c.s["strat_b.min_age_min"] == 15
    assert sb.apply_reverts() == []                              # same night, before the deadline
    c.night = "next-night"                                       # 8 AM rollover
    assert sb.apply_reverts() == ["strat_b.min_age_min"]
    assert c.s["strat_b.min_age_min"] == 60 and c.db.meta(strat_b.TEMP_KEY) == []
    log = c.db.rows("SELECT key, old, new, source FROM change_log WHERE key='strat_b.min_age_min' ORDER BY id")
    assert [(r["new"], r["source"]) for r in log] == [("15", src), ("60", f"auto revert at night rollover ({src}: tonight only)")]


def test_tonight_only_deadline_backstop():
    c, clock, sb = _sb()
    sb.set_tonight("strat_b.min_age_min", 15, "chat", until=clock.t + 60)
    clock.t += 61                                                # night didn't roll (desk down at 8 AM): deadline reverts it
    run(sb.tick(pairs=[]))
    assert c.s["strat_b.min_age_min"] == 60


def test_feed_module_never_reaches_gmgn_or_wallet():
    src = pathlib.Path(strat_b_feed.__file__).read_text()
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module)
    assert mods <= {"__future__", "asyncio", "datetime", "fields", "holders", "sources", "ctx", "net", "agents.auditor"}, mods
    code = src.split('"""', 2)[2]
    for bad in ("gmgn", "safe_sell", "on_pass", "secrets", "keypair", "wallet_", "swap", "bus.analyst", "bus.executor"):
        assert bad not in code, bad


def test_desk_runs_feed(tmp_path):
    import main
    d = main.Desk(db_path=str(tmp_path / "d.db"), log_files=False, with_telegram=False)
    assert d.strat_b_feed.sb is d.strat_b and main.HB_JOBS["strat_b_feed"] == ("strat_b_feed",)


# ---------- DECISION 76: Gecko 429 backoff is a fixed 60s again (pre-5:23 PM behavior)
def test_feed_429_backs_off_60s_and_pauses(monkeypatch):
    from net import RateLimited
    c, clock, sb, f = _feed(monkeypatch)
    calls = {"n": 0}

    async def boom(path, params):
        calls["n"] += 1
        raise RateLimited("gecko 429")

    f.gecko = boom
    run(f.poll_once())
    assert calls["n"] == 1
    assert f.backoff_until == pytest.approx(clock.t + 60)
    n = calls["n"]
    assert run(f.poll_once()).startswith("idle")              # paused: no gecko calls during backoff
    assert calls["n"] == n
    clock.t = f.backoff_until
    run(f.poll_once())
    assert calls["n"] == n + 1
    assert f.backoff_until == pytest.approx(clock.t + 60)     # still a flat 60s, not exponential
