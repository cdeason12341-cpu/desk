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

## Build adaptations (approved 2026-11-14 ~4:22 PM CST)
5. Python 3.13.5 (no 3.12 on the box) in .venv. Libs: websockets, httpx, pydantic, cryptography (+pytest).
   No fastapi/uvicorn/pywebpush: Plan B has no endpoint and no web push. Notifier = Telegram only.
6. Hosting = Plan B (GitHub Pages via Actions). DATA_PATH is a repo secret, never in a git ref.
   Data: desk -> encrypted files -> one bundle.json in a secret gist -> workflow_dispatch(rev) -> Pages.
   Publish on events (signal, fill, exit, audit, analysis) debounced 20 s + heartbeat 180 s; never overlapping runs.
7. Encryption: PBKDF2-SHA256(APP_TOKEN, salt "grok-bot-desk/v1/salt", 200k) -> AES-256-GCM. Envelope {"v":1,"iv","ct"}.
   App URL: {PAGES}#t={APP_TOKEN}&d={DATA_PATH}. STEP 13.7's "/api/state 200 with token / 401 without" becomes:
   files decrypt only with APP_TOKEN; without it the app shows ciphertext only.
8. Settings, pause/resume, mode, stack, Bought/Sold/Skip only via Telegram (owner chat only). App is read-only.
9. No SSE: audit layers reach the app per publish; the app replays the disc animation.
10. getTokenLargestAccounts is never called (disabled on the public RPC). Top 10 = getProgramAccounts (Token +
    Token-2022), grouped by owner, excluding pool, bonding-curve PDA (derived, verified vs RugCheck) and RugCheck AMM accounts.
11. Name/symbol from DexScreener; creator from RugCheck `creator` (not creatorBalance); creator balance via RPC
    getTokenAccountsByOwner at migration and again at audit ("sold 0" = never dropped).
12. FOMO coin URL = https://fomo.family/tokens/{solana|base|bnb|robinhood}/{address}?r=savipww (FOMO's token route);
    unknown chain -> SIGNUP_URL. Profile = /profile/{handle}?source=share_link&r=savipww.
13. Inferred where PART 1-2 / the truncated Executor text were not available to the builder: buy push text
    "Buy {name} · ${size} · {pct}% of stack · score {score}"; Executor rechecks liquidity (-20% vs Scanner) and
    price (+15% vs Analyst read) before the push; size is capped to free balance; LIVE no tap in 5 min = "missed".
    A "night" starts when the desk starts and rolls over at report_time; a full PAPER night = one rolled over in PAPER.
14. Copycat containment match needs >= 3 letters on both sides (avoids 1-2 letter names matching everything).
15. Missing DexScreener priceChange keys are treated as "no value" (never kill on them; rules mode uses 0).
