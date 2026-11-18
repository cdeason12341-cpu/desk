"""DECISION 79: getTransaction must accept versioned txs (maxSupportedTransactionVersion >= 1).

A 0 only covers legacy + v0. Version-1 txs (e.g. Blimbo #67's buy) return RPC -32015 and left
LIVE orders stuck in 'exiting', still shown as open Strat A positions the wallet no longer holds.
"""
import chain, devexit, solana
from tests.chainsim import ChainSim, W
from tests.helpers import make_ctx, run

M = "MintVersion1pump"


def test_tx_opts_accept_version_1():
    assert chain.TX_OPTS["encoding"] == "jsonParsed"
    assert chain.TX_OPTS["commitment"] == "confirmed"
    assert chain.TX_OPTS["maxSupportedTransactionVersion"] >= 1


def test_tx_delta_sends_max_supported_version():
    c, _ = make_ctx()
    seen = []

    async def rpc(method, params, timeout=30):
        seen.append((method, params))
        return None

    c.net.rpc = rpc
    assert run(chain.tx_delta(c.net, "Sig123", "Wallet", M)) is None
    assert seen[0][0] == "getTransaction"
    assert seen[0][1][0] == "Sig123"
    opts = seen[0][1][1]
    assert opts["encoding"] == "jsonParsed"
    assert opts["maxSupportedTransactionVersion"] >= 1


def test_wallet_sells_skips_unreadable_sigs_and_still_finds_the_sell():
    c, clock = make_ctx()
    ch = ChainSim(c.net, M)
    buy_ts = int(clock.t)
    ch.add("BuySig", buy_ts, -10 ** 8, 10 ** 9)
    ch.add("BadSig", buy_ts + 5, 0, 0)
    ch.add("SellSig", buy_ts + 10, 9 * 10 ** 7, -10 ** 9)
    real_get = ch.get_tx

    def get_tx(params):
        opts = params[1] if len(params) > 1 else {}
        assert opts.get("maxSupportedTransactionVersion", -1) >= 1
        if params[0] == "BadSig":
            raise RuntimeError('rpc error getTransaction: {"code": -32015, "message": "Transaction version (1) is not supported"}')
        return real_get(params)

    c.net.rpc_map["getTransaction"] = get_tx
    sells = run(chain.wallet_sells(c.net, W, M, buy_ts, skip={"BuySig"}))
    assert len(sells) == 1 and sells[0]["sig"] == "SellSig" and sells[0]["tok"] < 0


def test_devexit_get_transaction_supports_version_1():
    c, _ = make_ctx()
    seen = []
    ata = devexit.ata("Dev1111111111111111111111111111111111111111", M, solana.TOKEN_PROGRAM)
    assert ata

    def sigs(prm):
        return [{"signature": "DevSell", "err": None}]

    def get_tx(prm):
        seen.append(prm)
        return {"blockTime": 1_800_000_000, "meta": {
            "preTokenBalances": [{"mint": M, "owner": "Dev1111111111111111111111111111111111111111",
                                  "uiTokenAmount": {"amount": "1000"}}],
            "postTokenBalances": [{"mint": M, "owner": "Dev1111111111111111111111111111111111111111",
                                   "uiTokenAmount": {"amount": "0"}}]}}

    c.net.rpc_map["getSignaturesForAddress"] = sigs
    c.net.rpc_map["getTransaction"] = get_tx
    ts, note = run(devexit.last_dev_sell(c.net, "Dev1111111111111111111111111111111111111111", M, solana.TOKEN_PROGRAM))
    assert ts == 1_800_000_000 and note == "ok"
    assert seen and seen[0][0] == "DevSell"
    assert seen[0][1]["encoding"] == "jsonParsed"
    assert seen[0][1]["maxSupportedTransactionVersion"] >= 1
