"""Field mapping for the desk, built from raw samples in logs/raw/ (STEP 2, 2026-11-14).
Every path was found in a real response. None = key not present in the source; do not guess.
Path syntax: dotted keys; "[]" = iterate a list; "[pool]" = list item selected by a rule (see notes).
"""

SOURCES = {
    "pumpportal_migration": "logs/raw/pumpportal_migration.json (+ pumpportal_migrations_more.json)",
    "dexscreener": "logs/raw/dexscreener_pair.json (response is a JSON list of pairs)",
    "rugcheck_report": "logs/raw/rugcheck_report.json",
    "rugcheck_graph": "logs/raw/rugcheck_insiders_graph.json (fresh coin: []), shape from rugcheck_insiders_graph_shape_sample_fartcoin.json",
    "rpc_getaccountinfo": "logs/raw/rpc_getaccountinfo.json",
    "rpc_gettokensupply": "logs/raw/rpc_gettokensupply.json",
    "rpc_getprogramaccounts": "logs/raw/rpc_gpa_token.json, rpc_gpa_token2022.json",
}

FIELDS = {
    # --- PumpPortal subscribeMigration message (keys seen: signature, mint, txType, pool) ---
    "migration": {
        "mint": "mint",
        "signature": "signature",
        "tx_type": "txType",          # value "migrate"
        "pool_label": "pool",         # value "pump-amm": a pool TYPE label, NOT the pool address
        "pool": None,                 # no pool address in message -> use dexscreener pairAddress
        "creator": None,              # not in message -> rugcheck_report.creator
        "name": None,                 # not in message -> dexscreener baseToken.name
        "symbol": None,               # not in message -> dexscreener baseToken.symbol
    },
    # --- DexScreener /tokens/v1/solana/{mints}: list; pick item with dexId=="pumpswap", max liquidity.usd ---
    "dexscreener": {
        "base_mint": "baseToken.address",
        "name": "baseToken.name",
        "symbol": "baseToken.symbol",
        "quote_mint": "quoteToken.address",
        "quote_symbol": "quoteToken.symbol",
        "dexId": "dexId",
        "pairAddress": "pairAddress",
        "priceUsd": "priceUsd",                 # STRING, cast to float
        "liquidity_usd": "liquidity.usd",
        "marketCap": "marketCap",
        "volume_m5": "volume.m5",
        "volume_h1": "volume.h1",
        "volume_h24": "volume.h24",
        "txns_m5_buys": "txns.m5.buys",
        "txns_m5_sells": "txns.m5.sells",
        "txns_h1_buys": "txns.h1.buys",
        "txns_h1_sells": "txns.h1.sells",
        "priceChange_m5": "priceChange.m5",     # may be missing (priceChange was {} on one sample)
        "priceChange_h1": "priceChange.h1",
        "priceChange_h24": "priceChange.h24",
        "pairCreatedAt": "pairCreatedAt",       # epoch milliseconds
    },
    # --- RugCheck /v1/tokens/{mint}/report ---
    "rugcheck_report": {
        "creator": "creator",
        "creator_balance": "creatorBalance",    # NOT trusted: 0 while topHolders shows creator-owned acct (see notes)
        "token_program": "tokenProgram",
        "markets": "markets",                   # list, or null when no market indexed yet
        "market_pubkey": "markets[].pubkey",    # == dexscreener pairAddress (verified)
        "market_type": "markets[].marketType",  # "pump_fun_amm"
        "market_lp_locked_pct": "markets[pool].lp.lpLockedPct",
        "market_lp_burned_pct": None,           # no burned-pct key exists in markets[].lp
        "market_lp_unlocked": "markets[pool].lp.lpUnlocked",
        "top_holders": "topHolders",            # [] of {address, amount, decimals, pct, uiAmount, owner, insider}
        "known_accounts": "knownAccounts",      # {address: {name, type}} type in AMM|CREATOR|LOCKER
        "total_holders": "totalHolders",
        "graph_insiders_detected": "graphInsidersDetected",
        "token_supply": "token.supply",          # STRING/int raw units (verified Nyajira, Fartcoin, USPR)
        "insider_networks": "insiderNetworks",  # list of networks, or null when none detected
        # insiderNetworks[] verified 2026-11-14 on Nyajira (4qyk…pump) and Fartcoin (9BB6…pump):
        #   {id, size, type, tokenAmount, currentHolding, activeAccounts}
        "insider_network_current_holding": "insiderNetworks[].currentHolding",  # raw amount still held
        "insider_network_token_amount": "insiderNetworks[].tokenAmount",        # raw; can be slightly above currentHolding
        "insider_network_size": "insiderNetworks[].size",                       # wallets in that network
        "insider_network_active": "insiderNetworks[].activeAccounts",
    },
    # --- RugCheck /v1/tokens/{mint}/insiders/graph ---
    # Top level is a LIST of networks (fresh coins return []).
    # network = {net_id, network_type ("transfer"), nodes, links, relatedMint}
    "rugcheck_graph": {
        "networks": "",                         # the root list itself
        "nodes": "[].nodes",                    # {id: wallet, participant: bool, holdings: int raw amount}
        "node_wallet": "[].nodes[].id",
        "node_holdings": "[].nodes[].holdings",
        "edges": "[].links",                    # key is "links", NOT "edges"
        "edge_from": "[].links[].source",
        "edge_to": "[].links[].target",
    },
    # --- RPC getAccountInfo(mint, jsonParsed) ---
    "rpc_mint": {
        "program_owner": "result.value.owner",  # Token or Token-2022 program id
        "program": "result.value.data.program", # "spl-token" | "spl-token-2022"
        "mintAuthority": "result.value.data.parsed.info.mintAuthority",
        "freezeAuthority": "result.value.data.parsed.info.freezeAuthority",
        "supply": "result.value.data.parsed.info.supply",     # STRING raw units
        "decimals": "result.value.data.parsed.info.decimals",
        "extensions": "result.value.data.parsed.info.extensions",
    },
    # --- RPC getTokenSupply(mint) ---
    "rpc_supply": {
        "amount": "result.value.amount",
        "decimals": "result.value.decimals",
        "uiAmount": "result.value.uiAmount",
    },
    # --- RPC getTokenLargestAccounts: METHOD DISABLED on api.mainnet-beta (429, x-ratelimit-method-limit: 0) ---
    "rpc_largest": None,
    # --- RPC getProgramAccounts (Token + Token-2022, memcmp mint@0, jsonParsed) ---
    "rpc_token_account": {
        "pubkey": "pubkey",
        "owner": "account.data.parsed.info.owner",
        "amount": "account.data.parsed.info.tokenAmount.amount",   # STRING raw units
        "delegate": "account.data.parsed.info.delegate",           # key absent when no delegate
        "delegatedAmount": "account.data.parsed.info.delegatedAmount",
        "state": "account.data.parsed.info.state",
    },
}

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"   # verified on-chain, executable


MISSING = object()   # key absent (distinct from a JSON null, which returns None)


def has(obj, path):
    return get(obj, path, MISSING) is not MISSING


def get(obj, path, default=None):
    """Read a dotted path. Missing key / None path -> default; a JSON null returns None.
    Never invents a value.
    Supports '[]' (map over list) and 'markets[pool]' style is resolved by caller via select_market()."""
    if path is None:
        return default
    if path == "":
        return obj
    cur = obj
    parts = path.split(".")
    for i, part in enumerate(parts):
        if part.startswith("[]"):
            rest = ".".join(([part[2:]] if part[2:] else []) + parts[i + 1:])
            if not isinstance(cur, list):
                return default
            return [get(x, rest, default) for x in cur]
        if part.endswith("[]"):
            key = part[:-2]
            cur = cur.get(key) if isinstance(cur, dict) else None
            if not isinstance(cur, list):
                return default
            rest = ".".join(parts[i + 1:])
            return [get(x, rest, default) for x in cur]
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def select_pair(dex_list):
    """PumpSwap pair with most liquidity, else None."""
    pairs = [p for p in (dex_list or []) if p.get("dexId") == "pumpswap"]
    return max(pairs, key=lambda p: get(p, "liquidity.usd", 0) or 0) if pairs else None


def select_market(report, pool):
    """RugCheck market whose pubkey == Scanner's pool, else None (-> layer 5 red 'no data: liquidity')."""
    for m in (report or {}).get("markets") or []:
        if m.get("pubkey") == pool:
            return m
    return None
