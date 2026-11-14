# Desk decisions (approved by owner 2026-11-14, ~3:59 PM CST)

1. Layer 7 (Bundled wallets): RugCheck insiders/graph returning an empty list `[]` = "None" (0 bundled), pass.
   Edges live under `[].links[]` ({source, target}), not `edges`. Nodes under `[].nodes[]` ({id, participant, holdings}).
2. Top-10 holders (Scanner step 3 and Auditor layer 4): getTokenLargestAccounts is disabled on the default RPC
   (429, x-ratelimit-method-limit: 0). Top 10 is computed from the layer-8 getProgramAccounts data
   (Token TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA + Token-2022 TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb,
   memcmp mint @0, jsonParsed), excluding accounts owned by the pool or the bonding curve.
3. Layer 5 (Liquidity): RugCheck has no LP-burned field. Pass = markets[pool].lp.lpLockedPct >= 99 only
   (plus liquidity not down > 50% since Scanner).
4. Scanner only passes pump mints (mint address ends in "pump") with a PumpSwap pair (dexId "pumpswap")
   quoted in SOL (quoteToken.address So11111111111111111111111111111111111111112).
