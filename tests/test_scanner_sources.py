"""DECISION 51: scan coins beyond pump.fun (scanner.sources all|pump)."""
import asyncio
import config, holders, sources, stage_pings
from agents.scanner import Scanner, decide, parse_new_pools, pick_pair
from tests.helpers import make_ctx, pair, run, SOL

NOW = 1_800_000_000.0
BONK = "Bonk111111111111111111111111111111111bonk"
MET = "Met1111111111111111111111111111111111111111"
PUMP = "Pmp1111111111111111111111111111111111pump"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def _coin(age_min, mint=MET, **extra):
    row = {"mint": mint, "migrated_at": NOW - age_min * 60.0}
    row.update(extra)
    return row


def _p(mint, dex="raydium", liq=50000, quote=SOL, mc=200000, created_s=None, **kw):
    p = pair(mint, liq=liq, **kw)
    p["dexId"] = dex
    p["quoteToken"] = {"address": quote, "symbol": "SOL" if quote == SOL else "USDC"}
    p["marketCap"] = mc
    p["pairCreatedAt"] = int((created_s if created_s is not None else NOW - 3600) * 1000)
    p["pairAddress"] = f"{dex}-{mint[:6]}-{liq}"
    return p


def test_setting_default_all_and_switch_back():
    c, _ = make_ctx()
    assert c.s["scanner.sources"] == "all" and not sources.pump_only(c.s)
    assert config.resolve("sources").key == "scanner.sources"
    c.s.set("scanner.sources", "pump", source="test")
    assert sources.pump_only(c.s)
    assert c.s["scanner.other_max_mc_usd"] == 10_000_000


def test_non_pump_coin_passes_all_filters_in_all_mode_and_dies_in_pump_mode():
    c, _ = make_ctx()
    assert decide(c.s, _coin(20), _p(MET), 10.0, [], NOW) == ("pass", None)
    assert decide(c.s, _coin(20), _p(MET, liq=9000), 10.0, [], NOW)[1].startswith("liq ")     # same liq floor
    assert decide(c.s, _coin(20), _p(MET, mc=12000), 10.0, [], NOW)[1].startswith("mc ")      # same MC floor
    assert decide(c.s, _coin(20), _p(MET), 60.0, [], NOW)[1].startswith("top 10 hold")         # same top 10
    assert decide(c.s, _coin(20), _p(MET, h1=500), 10.0, [], NOW)[1] == "+500% in 1h"          # same 1h limit
    assert decide(c.s, _coin(5), _p(MET), 10.0, [], NOW) == ("wait", None)                    # same pool age
    c2, _ = make_ctx(**{"scanner.sources": "pump"})
    assert decide(c2.s, _coin(20), _p(MET), 10.0, [], NOW) == ("kill", "not a pump mint")


def test_excluded_too_big_old_token():
    c, _ = make_ctx()
    assert decide(c.s, _coin(20, USDT), _p(USDT), 10.0, [], NOW) == ("kill", "excluded: USDT")
    p = _p(MET); p["baseToken"]["symbol"] = "usd1"
    assert decide(c.s, _coin(20), p, 10.0, [], NOW) == ("kill", "excluded: USD1")
    v, r = decide(c.s, _coin(20), _p(MET, mc=50_000_000), 10.0, [], NOW)
    assert v == "kill" and r.startswith("too big: mc $50.00M")
    v, r = decide(c.s, _coin(20, first_pool_at=NOW - 30 * 86400), _p(MET), 10.0, [], NOW)
    assert v == "kill" and r.startswith("old token")
    # pump mints never get the new non-pump rules
    assert decide(c.s, _coin(20, PUMP, first_pool_at=NOW - 30 * 86400), pair(PUMP), 10.0, [], NOW)[0] == "pass"


def test_pick_pair_other_graduated_sol_first_and_pump_unchanged():
    pools = [_p(MET, "meteoradbc", liq=90000), _p(MET, "raydium", liq=60000, quote="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
             _p(MET, "meteora", liq=40000), _p(MET, "raydium", liq=45000)]
    got = pick_pair(pools, MET)
    assert got["dexId"] == "raydium" and got["liquidity"]["usd"] == 45000
    assert pick_pair([_p(MET, "meteoradbc")], MET) is None                       # bonding curve only: waits
    assert pick_pair([_p(PUMP, "raydium")], PUMP) is None                        # pump mints: PumpSwap only, as before
    assert sources.first_pool_at([_p(MET, created_s=NOW - 100), _p(MET, created_s=NOW - 900)]) == NOW - 900


def _gpool(mint, dex, reserve="20000", quote="solana_" + SOL, ts="2027-01-15T08:00:00Z"):
    return {"attributes": {"pool_created_at": ts, "name": "X / SOL", "address": "P" + mint[:6], "reserve_in_usd": reserve},
            "relationships": {"dex": {"data": {"id": dex}}, "base_token": {"data": {"id": "solana_" + mint}},
                              "quote_token": {"data": {"id": quote}}}}


def test_parse_new_pools_others():
    body = {"data": [_gpool(PUMP, "pumpswap"), _gpool(PUMP, "pump-fun"), _gpool(MET, "meteora-damm-v2"),
                     _gpool(BONK, "raydium-cpmm"), _gpool("Dbc" + "1" * 40, "meteora-dbc"),
                     _gpool("Dst" + "1" * 40, "raydium", reserve="12"), _gpool(USDT, "raydium"),
                     _gpool("Usd" + "1" * 40, "orca", quote="solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")]}
    assert [r["mint"] for r in parse_new_pools(body)] == [PUMP]                  # default: unchanged
    got = parse_new_pools(body, others=True)
    assert [(r["mint"], r["dex"]) for r in got] == [(PUMP, "pumpswap"), (MET, "meteora-damm-v2"), (BONK, "raydium-cpmm")]


def test_admit_and_catchup_label_sources():
    c, clock = make_ctx()
    sc = Scanner(c)
    c.net.new_pools = {1: {"data": [_gpool(MET, "meteora-damm-v2", ts="2027-01-15T07:59:00Z")]}}
    clock.t = 1_800_000_000.0
    import agents.scanner as S
    created = S._iso_ts("2027-01-15T07:59:00Z")
    clock.t = created + 60
    assert run(sc.catchup_graduations()) == 1
    row = c.coin(MET)
    assert row["status"] == "room" and row["pool_label"] == "meteora-damm-v2"
    assert sc.source_label(MET) == "meteora"
    assert sc.admit(USDT, clock.t, "migration", pool_label="raydium-cpmm")
    assert c.coin(USDT)["status"] == "killed" and c.coin(USDT)["reason"] == "excluded: USDT"
    c2, _ = make_ctx(**{"scanner.sources": "pump"})
    sc2 = Scanner(c2)
    sc2.admit(BONK, clock.t, "migration", pool_label="raydium-cpmm")
    assert c2.coin(BONK)["reason"] == "not a pump mint"
    assert sources.launchpad(BONK, "raydium-cpmm") == "letsbonk"
    assert sources.launchpad("Abc" + "1" * 40, "raydium-cpmm") == "letsbonk"
    assert sources.launchpad("Abc" + "1" * 40, "pump-amm") == "pump.fun"
    assert sources.launchpad("Abc" + "1" * 40, "dex:meteora") == "meteora"


def test_dex_latest_intake_admits_young_graduated_only():
    c, clock = make_ctx()
    sc = Scanner(c)
    young, old, bond = "You" + "1" * 40, "Xyd" + "1" * 40, "Bnd" + "1" * 40
    feeds = {"profiles": [{"chainId": "solana", "tokenAddress": young}, {"chainId": "base", "tokenAddress": "0xabc"},
                          {"chainId": "solana", "tokenAddress": PUMP}],
             "boosts": [{"chainId": "solana", "tokenAddress": old}, {"chainId": "solana", "tokenAddress": bond},
                        {"chainId": "solana", "tokenAddress": young}]}
    calls = []

    async def dex_latest(kind):
        calls.append(kind); return feeds[kind]

    async def dex_tokens(mints):
        calls.append(tuple(mints))
        return [_p(young, "raydium", created_s=clock.t - 1800), _p(young, "launchlab", created_s=clock.t - 7200),
                _p(old, "raydium", created_s=clock.t - 40 * 3600), _p(bond, "launchlab", created_s=clock.t - 60)]
    c.net.dex_latest, c.net.dex_tokens = dex_latest, dex_tokens
    assert run(sc.dex_latest_intake()) == 1
    assert calls[:2] == ["profiles", "boosts"] and calls[2] == (young, old, bond)
    row = c.coin(young)
    assert row["status"] == "room" and row["pool_label"] == "dex:raydium" and row["migrated_at"] == clock.t - 7200
    assert c.coin(old) is None and c.coin(bond) is None and c.coin(PUMP) is None
    assert run(sc.dex_latest_intake()) == 0 and len(calls) == 3                 # throttled to once a minute
    clock.t += 61
    assert run(sc.dex_latest_intake()) == 0 and len(calls) == 5                 # seen ones not looked up again
    c2, _ = make_ctx(**{"scanner.sources": "pump"})
    c2.net.dex_latest = dex_latest
    assert run(Scanner(c2).dex_latest_intake()) == 0


def test_surge_watch_includes_other_launchpads_unless_pump_only():
    for mode, want in (("all", {PUMP, MET}), ("pump", {PUMP})):
        c, clock = make_ctx(**{"scanner.sources": mode})
        for m in (PUMP, MET, USDT):
            c.db.x("INSERT INTO coins(mint,migrated_at,first_seen,status,reason,night) VALUES(?,?,?,?,?,?)",
                   (m, clock.t - 600, clock.t - 600, "killed", "mc $12.0K", c.night))
        assert {r["mint"] for r in Scanner(c).surge_candidates(clock.t)} == want


def test_stage_pings_show_source():
    c, clock = make_ctx()
    c.db.x("INSERT INTO coins(mint,name,pool_label,migrated_at,first_seen,status,night) VALUES(?,?,?,?,?,?,?)",
           (BONK, "Bonky", "raydium-cpmm", clock.t, clock.t, "passed", c.night))

    async def go():
        stage_pings.ping(c, "auditor", BONK, "🛡️ Audit pass · Bonky · top10 12%")
        stage_pings.ping(c, "analyst", BONK, "Analyst · score 40 · Bonky\nNo buy: x.")
        for t in list(getattr(c, "_ping_tasks", ())):
            await t
    asyncio.run(go())
    texts = [m["text"] for m in c.notifier.sent]
    assert texts == ["🛡️ Audit pass · Bonky · top10 12% · via letsbonk", "Analyst · score 40 · Bonky · via letsbonk\nNo buy: x."]
    out = {"mint": MET, "name": "Met", "liquidityUsd": 50000, "marketCapUsd": 90000, "source": "meteora"}
    assert stage_pings.scanner_text(out).endswith("· via meteora")


def test_holder_math_excludes_dex_authorities():
    ex = holders.excluded_owners("Pool1", MET)
    assert "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL" in ex and "Pool1" in ex
    accts = [{"pubkey": "a", "owner": "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL", "amount": 800},
             {"pubkey": "b", "owner": "w1", "amount": 100}]
    assert holders.top10_pct(accts, 1000, ex) == 10.0


def test_no_pass_before_creator_capture_lands():
    c, clock = make_ctx()
    sc = Scanner(c)
    c.net.pairs[MET] = _p(MET, created_s=clock.t - 3600)

    async def go():
        sc.admit(MET, clock.t - 3600, "dex_latest", pool_label="dex:raydium")
        assert MET in sc._capturing
        sc.c.net.rpc_map["getTokenAccountsByOwner"] = {"value": []}
        await sc.poll_once()
        assert c.coin(MET)["status"] == "room"                     # held while the dev wallet is captured
        for _ in range(5):
            await asyncio.sleep(0)
        while MET in sc._capturing:
            await asyncio.sleep(0.01)
    asyncio.run(go())
    assert MET not in sc._capturing
