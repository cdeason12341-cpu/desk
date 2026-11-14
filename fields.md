# Field mapping (STEP 2, 2026-11-14) — sample mint DFev755KT1b6nkfAFNj1fyRQMwoVzNywsSBEBamepump (USPR)

| Agent need | Source | Exact JSON path | Notes |
|---|---|---|---|
| mint | PumpPortal migration | `mint` | |
| signature | PumpPortal migration | `signature` | |
| txType | PumpPortal migration | `txType` | "migrate" |
| pool (address) | PumpPortal migration | **null** | `pool` = "pump-amm" (type label, not address). Use DexScreener `pairAddress` |
| creator | PumpPortal migration | **null** | not in message → RugCheck `creator` |
| name / symbol | PumpPortal migration | **null** | not in message → DexScreener `baseToken.name` / `baseToken.symbol` |
| dexId | DexScreener (list item) | `dexId` | "pumpswap" |
| pairAddress | DexScreener | `pairAddress` | = RugCheck `markets[].pubkey` (verified) |
| priceUsd | DexScreener | `priceUsd` | string |
| liquidity.usd | DexScreener | `liquidity.usd` | |
| marketCap | DexScreener | `marketCap` | |
| volume m5/h1/h24 | DexScreener | `volume.m5` / `volume.h1` / `volume.h24` | |
| txns m5 buys/sells | DexScreener | `txns.m5.buys` / `txns.m5.sells` | |
| txns h1 buys/sells | DexScreener | `txns.h1.buys` / `txns.h1.sells` | |
| priceChange m5/h1/h24 | DexScreener | `priceChange.m5` / `.h1` / `.h24` | `priceChange` was `{}` on the low-liq sample; m5 absent on another |
| pairCreatedAt | DexScreener | `pairCreatedAt` | epoch ms |
| creator | RugCheck report | `creator` | |
| creator balance | RugCheck report | `creatorBalance` | 0 while `topHolders[0].owner` == creator at 79.31% → don't trust; use RPC |
| markets[].pubkey | RugCheck report | `markets[].pubkey` | `markets` is null when not indexed (Chewbarka sample) |
| LP locked % | RugCheck report | `markets[pool].lp.lpLockedPct` | 100 on sample |
| LP burned % | RugCheck report | **null** | no burned key in `markets[].lp` |
| graph root | RugCheck insiders/graph | root is a list of networks | fresh coins returned `[]` (both samples) |
| graph nodes | RugCheck insiders/graph | `[].nodes[]` = `{id, participant, holdings}` | shape from Fartcoin sample |
| graph edges | RugCheck insiders/graph | `[].links[]` = `{source, target}` | key is `links`, **not** `edges` |
| network meta | RugCheck insiders/graph | `[].net_id`, `[].network_type` ("transfer"), `[].relatedMint` | |
| mintAuthority | RPC getAccountInfo | `result.value.data.parsed.info.mintAuthority` | |
| freezeAuthority | RPC getAccountInfo | `result.value.data.parsed.info.freezeAuthority` | |
| supply | RPC getAccountInfo / getTokenSupply | `result.value.data.parsed.info.supply` / `result.value.amount` | strings |
| decimals | RPC | `result.value.data.parsed.info.decimals` / `result.value.decimals` | |
| token program | RPC getAccountInfo | `result.value.owner` | both samples Token-2022 |
| largest accounts | RPC getTokenLargestAccounts | **null** | method disabled on public RPC (429, `x-ratelimit-method-limit: 0`) |
| token acct owner/amount/delegate | RPC getProgramAccounts | `account.data.parsed.info.owner` / `.tokenAmount.amount` / `.delegate` | `delegate` key absent when none |
