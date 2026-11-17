"""Owner rule 2026-11-15 12:48 CT: dev fully out > 60s and price steady since -> layer 3 passes."""
import config, devexit, solana
from agents.auditor import Auditor
from tests.helpers import make_ctx, pair, run

NOW = 1_800_000_000.0
MINT = "D" * 40 + "pump"
DEV = "Dev1111111111111111111111111111111111111111"
SUPPLY = 1_000_000_000_000


def _ctx(dev_now=0, at_mig=50_000_000_000, sold_ago=240, sell_price=1.0, now_price=1.03, m5=2.0, h1=5.0, sigs=True):
    c, clock = make_ctx(**{"auditor.retries": 0})
    clock.t = NOW
    c.db.x("INSERT INTO coins(mint,creator,creator_balance_raw,migrated_at,first_seen,status,night,pool) VALUES(?,?,?,?,?,?,?,?)",
           (MINT, DEV, str(at_mig), NOW - 1800, NOW - 1800, "passed", c.night, "Pool" + MINT[:8]))
    if sell_price is not None:
        c.db.x("INSERT INTO prices(ts,mint,price,vol_m5,src) VALUES(?,?,?,?,?)", (NOW - sold_ago - 20, MINT, sell_price, 0, "scanner"))
    p = pair(MINT, price=now_price, m5=m5, h1=h1) if now_price is not None else None
    if p is not None and m5 is None:
        p["priceChange"].pop("m5")
    c.net.pairs = {MINT: p} if p else {}
    ata = devexit.ata(DEV, MINT, solana.TOKEN_2022_PROGRAM)
    tx_other = {"blockTime": NOW - 5, "meta": {"preTokenBalances": [], "postTokenBalances": []}}
    tx_sell = {"blockTime": NOW - sold_ago, "meta": {
        "preTokenBalances": [{"mint": MINT, "owner": DEV, "uiTokenAmount": {"amount": str(at_mig)}}],
        "postTokenBalances": [{"mint": MINT, "owner": DEV, "uiTokenAmount": {"amount": str(dev_now)}}]}}
    txs = {"s0": tx_other, "s1": tx_sell}
    c.net.rpc_map = {
        "getAccountInfo": {"value": {"owner": solana.TOKEN_2022_PROGRAM, "data": {"parsed": {"info": {
            "mintAuthority": None, "freezeAuthority": None, "supply": str(SUPPLY)}}}}},
        "getTokenAccountsByOwner": {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(dev_now)}}}}}}]},
        "getSignaturesForAddress": (lambda prm: ([{"signature": "s0", "err": None}, {"signature": "s1", "err": None}]
                                                 if sigs and prm[0] == ata else [])),
        "getTransaction": lambda prm: txs[prm[0]],
    }
    return c


def _l3(c):
    r = run(Auditor(c).run(MINT, {"mint": MINT, "pool": "Pool" + MINT[:8]}))
    return {l["n"]: l for l in r["layers"]}[3]


def test_settings():
    c, _ = make_ctx()
    assert c.s["auditor.dev_exit_wait_s"] == 60 and c.s["auditor.dev_exit_max_drop_pct"] == 20
    assert config.resolve("dev exit wait").key == "auditor.dev_exit_wait_s"
    assert config.resolve("dev exit drop").key == "auditor.dev_exit_max_drop_pct"


def test_dev_out_and_steady_passes():
    L = _l3(_ctx())
    assert L["pass"] is True and L["value"] == "0.0% · dev out 4m ago, price +3% since"


def test_dust_counts_as_out():
    L = _l3(_ctx(dev_now=412))
    assert L["pass"] is True


def test_dev_selling_recently_fails():
    L = _l3(_ctx(sold_ago=45))
    assert L["pass"] is False and L["value"] == "dev selling (last sell 45s ago)"


def test_price_dropped_fails():
    L = _l3(_ctx(now_price=0.75))
    assert L["pass"] is False and L["value"] == "dev out 4m ago, price -25% since"
    L = _l3(_ctx(now_price=0.80))                  # exactly -20% passes
    assert L["pass"] is True


def test_still_holding_after_selling_is_unchanged():
    L = _l3(_ctx(dev_now=10_000_000_000))          # 1% left: still "dev sold"
    assert L["pass"] is False and L["value"] == "dev sold"


def test_sell_time_unknown_fails():
    L = _l3(_ctx(sigs=False))
    assert L["pass"] is False and L["value"] == "dev sold (sell time unknown)"


def test_fallback_m5_h1_when_no_sell_price():
    L = _l3(_ctx(sell_price=None, m5=-5.0, h1=10.0))
    assert L["pass"] is True and "m5 -5%" in L["value"] and "no sell price" in L["value"]
    assert _l3(_ctx(sell_price=None, m5=-25.0))["pass"] is False
    assert _l3(_ctx(sell_price=None, m5=1.0, h1=-60.0))["pass"] is False
    assert _l3(_ctx(sell_price=None, now_price=None))["pass"] is False


def test_never_sold_unchanged():
    c = _ctx(dev_now=50_000_000_000, at_mig=50_000_000_000)    # 5% now: holds too much
    assert _l3(c)["value"] == "dev holds 5%"
    c = _ctx(dev_now=0, at_mig=0)
    assert _l3(c)["value"] == "0.0% · sold 0"


def test_price_near_window():
    assert devexit.price_near([(100, 1.0), (300, 2.0)], 280) == 2.0
    assert devexit.price_near([(100, 1.0)], 500) is None
