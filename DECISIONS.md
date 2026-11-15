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
12. FOMO URLs exactly per spec (links.py; @ stripped from handle): coin_url = https://fomo.family/coin?address={address}&chainId={CHAIN_ID[chain]}&r=savipww&source=share_link
    with CHAIN_ID solana 1399811149, robinhood 4663, base 8453, bnb 56; unknown chain -> logged, SIGNUP_URL.
    profile_url = https://fomo.family/profile/{handle}?source=share_link&r=savipww. Buy push / Risk pushes verbatim from spec.
13. Inferred (PART 1-2 not seen by the builder): a "night" starts when the desk starts and rolls over at report_time;
    a full PAPER night = one rolled over in PAPER. Trail push shows "-$" only if fees turned a trail exit negative.
14. Copycat containment match needs >= 3 letters on both sides (avoids 1-2 letter names matching everything).
15. Missing DexScreener priceChange keys are treated as "no value" (never kill on them; rules mode uses 0).
16. scanner.min_liquidity_usd 40000 -> 10000 (owner via Grok Bot app, 2026-11-14 ~4:56 PM CST). Auditor liquidity rules unchanged.

## GMGN hands-off mode (2026-11-14 ~5:45 PM CT)
17. New mode "gmgn" alongside FOMO tap-to-trade (FOMO flow, links.py and r=savipww unchanged). Own table gmgn_orders,
    own settings gmgn.*, own heartbeat "gmgn", own PAPER/LIVE (gmgn.mode; desk.mode stays FOMO's). Starts PAPER.
    Client gmgn.py mirrors gmgn-cli 1.7.2 (signer.ts): Ed25519 over "{path}:{sorted_query}:{body}:{timestamp}", fresh
    client_id per attempt, IPv4 + UA gmgn-cli/1.7.2, POSTs never retried. Only from_address = owner's main wallet
    JAP5hC5dxCz8Q9kKXMshRKEwoN7fXX3JFpkKQ7Mo5zC2; anything else is refused before any request.
18. Exits attached to every buy (server-side, sell_ratio_type hold_amount): loss_stop price_scale = risk.stop_pct (6),
    loss_stop_trace drawdown_rate = risk.trail_pct (12), profit_stop_trace price_scale = risk.trail_start_pct (20) with
    drawdown 12 (the config has no fixed take-profit; this mirrors "trail arms at +20%"). Missing strategy_order_id after a
    fill -> limit_order stop_loss strategy (100%) -> if that fails, market-sell 100% + Telegram alert. While awake the
    watcher also follows holdings + open STMix strategies, market-sells on liquidity pulled and the 3h timeout.
19. Hard caps in code before every signed buy: per trade = desk ticket (tracks bumps) but never above gmgn.cap_trade_usd
    ($47.50); max gmgn.max_tickets_night (4) per night; night spend <= cap x tickets ($190); ticket + fees + 0.02 SOL reserve
    <= wallet SOL. Caps change only via owner Telegram "cap trade N" / "cap tickets N" + "CONFIRM CAP" (change_log).
    /halt stops all GMGN buys immediately (exits stay on); /unhalt resumes.
20. LIVE gate: 1 full PAPER night -> 2h PAPER, user request 2026-11-14 5:42 PM CT (GMGN mode only; recorded in change_log
    key gmgn.live_gate). GMGN LIVE = 2h of GMGN PAPER + plan check (quote must not be PLAN_UPGRADE_REQUIRED) + wallet covers
    one ticket + owner sends "gmgn live" then types "GMGN LIVE". FOMO's own LIVE gate (1 full PAPER night + LIVE) is unchanged.
21. Quote/swap need a paid GMGN plan (free: 403 PLAN_UPGRADE_REQUIRED, weight 11 > capacity 5). Owner upgraded to Plus
    2026-11-14 ~5:50 PM CT; quote verified code 0. PAPER uses the real quote; if a quote is ever plan-blocked it notes that,
    simulates at the DexScreener price and re-tries the quote after 10 min. Wallet SOL is re-read via /v1/user/info before sizing.

## Scanner max age (user request 2026-11-14 7:21 PM CT)
22. Scanner watch window extended from ~1h (the old scanner.max_wait_min = 60 "never qualified" drop) to
    scanner.max_age_h = 5 (alias "max age", validated 1–24 hours, changeable via Telegram `set max age to N`,
    logged in change_log). PumpPortal subscribeMigration is unchanged: every new graduation is still tracked.
    Instead of dropping a coin as "never qualified" after ~60 min, the Scanner keeps re-checking it (DexScreener
    tokens/v1, batched 30, existing scanner.max_dex_rpm / 429 backoff) until it is max_age_h hours old. On startup
    (and at the start of each poll) it backfills: coins already in the table with age < max_age_h that were dropped
    as "never qualified" are returned to the waiting room. Killed / passed coins are not reopened. All other
    Scanner filters are unchanged (min liq $10K, top10 ≤25%, SOL-quoted, pump mint, +h1 pump kill, copycat,
    min pool age 10m). Older coins take the same Auditor 8/8 → Analyst → Executor → Risk path. Auditor layer 6
    ("pool too young") is a minimum age only (default 10m) and already displays hours when age ≥ 120m, so a 5h
    coin passes it. Analyst scoring is not age-based; state text now says "Xh ago" when age ≥ 120m (wording only).
    scanner.max_wait_min stays in the registry for compatibility but is no longer the drop threshold.

## Real wallet in the phone app (2026-11-14 ~9:40 PM CT)
23. Home and Account now show the owner's real GMGN wallet alongside the FOMO PAPER stack, so the
    $200 practice balance is no longer mistaken for real money. publisher.Builder.gmgn() is included
    in state.json and account.json: mode, halted, wallet (full + short), cached SOL + USD (SOL price
    from GMGN gas_price native_token_usd_price, same source the trader uses; refreshed at most every
    60s, never blocks a publish on a failed read), caps (trade $, tickets used/limit, night spend),
    open positions with mark-to-market P&L and attached exits (stop -6%, trail 12%, TP trail from +20%),
    and the last 20 gmgn_orders. Quote, swap body, conditions, tx, and broker order ids are never
    published. Secrets stay out of the feed. The app is still read-only; FOMO links still come only
    from links.py with r=savipww. FOMO card label is "FOMO practice balance (PAPER, not real money)".
    GMGN trades appear on the Activity tab marked LIVE. Modes, caps and settings were not changed.

## Bundle supply rule (user request 2026-11-14 9:46 PM CT)
24. Auditor layer 7 now rejects a coin only when bundled wallets hold more than 20% of
    supply (`auditor.max_bundle_pct`, default 20, range 1–100, aliases "bundle pct" /
    "bundle percent", Telegram `set bundle pct to N`, logged in change_log). The old
    count reject (5+ linked wallets, `auditor.max_bundled`) is disabled: the setting stays
    in the registry with default 1000 (was 5; logged as a user request) and the Auditor
    no longer rejects on count. The layer row still shows the linked count for info, e.g.
    "4 linked, 4.1% of supply" (or "None"). Source is the RugCheck report already fetched
    for earlier layers: `insiderNetworks[].currentHolding` summed across every network,
    divided by `token.supply` (raw units). Field names were verified on live reports for
    Nyajira (`4qyk…pump`, 4.1% / 4 linked, pass) and Fartcoin (`9BB6…pump`, 92.9% / 2123
    linked, would reject). `currentHolding` is the amount still held (`tokenAmount` can
    be slightly higher). The insiders/graph endpoint is not used for the % rule: on
    Fartcoin it omitted the 2123-wallet network that the report listed. `topHolders[].insider`
    was false on both tokens, so it is not used. `insiderNetworks` null or `[]` (typical
    on fresh coins with `graphInsidersDetected` 0) is 0% / 0 linked, a pass — the same
    convention as an empty graph (DECISIONS #1). Treating null as "no data" would red
    nearly every young coin, so that is fail-safe, not a fail-closed missing-data case.
    A failed report fetch, a non-list `insiderNetworks`, a network missing `currentHolding`,
    or networks present with no usable supply, is still red "no data: bundles". Equal to
    20% passes ("more than 20%"). `audit.bundled` is now the largest network `size` from
    the same report field (so the Analyst penalty sees the full network, not the incomplete
    graph). `analyst.rules_bundled_from` 3 → 5 (user request, change_log, default updated);
    the penalty itself is unchanged (organic × `analyst.rules_bundled_penalty` 0.5). The
    app coin view already renders each layer `value`, so the new wording shows there
    without a client change. Modes, caps, and other settings were not changed.


## Analyst min score for tonight (user request 2026-11-14 11:00 PM CT)
25. `analyst.min_score` 70 → 60 for tonight's testing only (change_log source
    "user request 2026-11-14 11:00 PM CT · tonight only, revert 8 AM CT"). The
    other Analyst bars were left alone: `min_alive_6h` 0.70, `min_organic` 0.65,
    `max_chase` 0.30. Modes and caps were not changed (FOMO PAPER, GMGN LIVE,
    $65 / 4 tickets). Revert to 70 at 8 AM CT.

## PumpPortal websocket (2026-11-14 ~11:05 PM CT)
26. Root cause of the ~2-minute reconnects: `ws_loop` wrapped `ws.recv()` in
    `asyncio.wait_for(..., timeout=scanner.ws_silent_s)` (default 120s) and
    treated a quiet socket as dead (`ConnectionError("silent 120s")`).
    Migrations are naturally sparse, so a healthy connection was torn down
    about every two minutes (and on each tear-down the desk re-subscribed).
    Protocol ping/pong was already on (`ping_interval=20`, `ping_timeout=20`)
    and is the right liveness check; a real dead socket still surfaces as a
    keepalive ping timeout, which still reconnects. The 120s silence timeout
    is no longer used as a kill switch. `scanner.ws_silent_s` stays in the
    registry (aliases unchanged) but does not close the socket. `subscribeNewToken`
    was not added: new-token events are frequent and would flood the connection;
    they are not needed once liveness is ping/pong. `wsReconnects1h` in
    state.json / desk-health is unchanged in SQL (count of `ws_events` kind
    `reconnect` in the last hour) but now means real drops only — it is no
    longer inflated by silence timeouts. A backstop covers any short gap:
    each Scanner poll asks GeckoTerminal `GET /api/v2/networks/solana/new_pools`
    (up to 8 pages or 5 minutes of creates; one page is only ~30s of Solana
    pool creates) and admits PumpSwap, pump-mint, SOL-quoted pools that are
    not already in `coins` and are still inside `scanner.max_age_h`. Existing
    coins are not touched. DexScreener still has no public new-pairs list, so
    it cannot be the discovery backstop; it still enriches coins already in
    the waiting room.

## Analyst scores in the phone app (user request 2026-11-14 ~11:05 PM CT)
27. The read-only app has an Analyst scores list (Scores tab, also reachable
    from a Home card and the Analyst agent card). Publisher writes `scores.json`:
    the last 50 analyses tonight, newest first, with time, name/symbol, short
    mint, score, the four answers (alive_6h, organic, flush, chase), pass/reject,
    failed reasons, whether it was bought (FOMO position, bought execution, or
    a non-refused GMGN order tonight), the current `minScore` bar, and a FOMO
    `link` from links.py (`r=savipww`). Secrets and broker fields are never in
    the feed. Tapping a row reveals the four answers and reasons; pass/reject
    are coloured and the min-score bar is visible. Service worker bumped to
    `desk-shell-v4`. Modes, caps, and other settings were not changed.
