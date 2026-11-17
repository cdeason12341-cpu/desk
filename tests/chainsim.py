"""Fake Solana RPC for wallet txs: getTransaction / getSignaturesForAddress / getTokenAccountsByOwner (DECISION 42 tests)."""
import gmgn

W = gmgn.MAIN_WALLET


class ChainSim:
    def __init__(self, net, mint, dec=6):
        self.mint, self.dec = mint, dec
        self.txs = {}            # sig -> dict(ts, sol, tok, visible)
        self.balance = 0
        net.rpc_map["getTransaction"] = self.get_tx
        net.rpc_map["getSignaturesForAddress"] = self.sigs
        net.rpc_map["getTokenAccountsByOwner"] = self.bal

    def add(self, sig, ts, sol, tok, visible=True):
        self.txs[sig] = {"ts": ts, "sol": sol, "tok": tok, "visible": visible}

    def get_tx(self, params):
        t = self.txs.get(params[0])
        if not t or not t["visible"]:
            return None
        pre_tok = 10 ** 15
        tb = lambda amt: [{"owner": W, "mint": self.mint, "uiTokenAmount": {"amount": str(amt), "decimals": self.dec}}] if t["tok"] else []
        return {"blockTime": t["ts"], "transaction": {"signatures": [params[0]], "message": {"accountKeys": [{"pubkey": "Payer"}, {"pubkey": W}]}},
                "meta": {"err": None, "fee": 5000, "preBalances": [10 ** 10, 10 ** 10], "postBalances": [10 ** 10 - 5000, 10 ** 10 + t["sol"]],
                         "preTokenBalances": tb(pre_tok), "postTokenBalances": tb(pre_tok + t["tok"])}}

    def sigs(self, params):
        return [{"signature": s, "blockTime": t["ts"], "err": None}
                for s, t in sorted(self.txs.items(), key=lambda kv: -kv[1]["ts"]) if t["visible"]]

    def bal(self, params):
        return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(self.balance)}}}}}}]}
