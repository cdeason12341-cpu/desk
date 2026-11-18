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

## Hour-long stalls (user request 2026-11-15, approved t93u)
28. Overnight the desk froze for ~45–60 min at a time (scanner entries clustering
    near hh:22 after a gap; a publisher gap of 56 min; a 2:30–3:17 AM CT stall that
    recovered on a PumpPortal reconnect at 3:17:05 AM CT). Telegram logged a single
    getUpdates ReadTimeout at the *end* of each gap (not every 70s), which means the
    asyncio loop itself was stuck, not just one coroutine. Root cause: after the
    silence-timeout was removed (DECISIONS #26), liveness relied on websockets 17.4
    `ping_interval=20, ping_timeout=20`. In that library `await ws.ping()` enters
    `send_context()`, which `await drain()`s the ping frame *before* the pong clock
    starts. On a half-open TCP socket (proxy/peer drop with no RST) `drain()` waits
    for the kernel retry window (`tcp_retries2=15`, observed 42–60 min). Only then
    does the library surface `keepalive ping timeout; no close frame received`.
    Publisher `sh()` already had a 120s `wait_for`, but that cannot fire while the
    loop is blocked in `drain()`; `publish_once` heartbeated only after a successful
    publish, so the publisher looked mid-publish for the whole gap. Fix: an
    application ping loop (`ws_ping_loop`) with `asyncio.wait_for` around send+pong
    that `transport.abort()`s on timeout (never `close()`/`drain()`, which can hang);
    abort also runs before the connection context exits. Hard `asyncio.wait_for` caps
    on every `Net` HTTP await (DexScreener, Gecko, RugCheck, RPC, Jev) on top of the
    httpx timeout. Publisher: `sh()` timeout 45s and kills the child on timeout or
    cancel; `refresh_wallet` capped at 25s; `wait_idle` 45s (was 300s) with mid-wait
    heartbeats; whole `publish_once` capped at 90s. In-process watchdog (daemon
    thread, so it still runs if the loop is wedged) reads `heartbeats.json`, dumps
    stacks with `faulthandler` (and an asyncio task dump if the loop is alive) to
    `logs/watchdog.log` if any heartbeat or `wsPongAgeS` is stale > 3 min, and
    cancels that agent’s inner task so `supervised()` restarts it. If the event loop
    itself is frozen, it dumps, starts `./run_desk.sh` after this pid exits, sends
    SIGTERM, and SIGKILLs after 8s if the loop cannot handle SIGTERM. Scanner poll
    now heartbeats at the start of `poll_once` and before each top-10 RPC. Settings,
    modes, and caps were not changed (FOMO PAPER, GMGN LIVE, $65 / 4, min_score 60
    tonight with the 8 AM CT revert still in place). No orders were placed.

## Box pauses, not a wedged loop (user request 2026-11-15, after 4:14 AM CT restart)
29. The 3-minute in-process watchdog did fire — but only when the box woke up,
    and it then killed a healthy desk. After pid 203831 (4:14 AM CT) the desk
    "went down" 26 min (5:00:52–5:24:49, pid 226886), 43 min (5:32–6:15, pid
    230845), 54 min (6:22:31–7:16, pid 234576), and again ~39 min (until 8:03,
    pid 238476). Each time `watchdog.log` said `event loop frozen file_age=1437s
    (then 2605s, 3257s, 2364s) loop_tick=15s`. That pair is the tell: wall-clock
    file_age jumped tens of minutes, but `time.monotonic()` (loop_tick) had only
    advanced the 15s watchdog sleep. `faulthandler.dump_traceback` failed with
    `UnsupportedOperation` because it was given a `StringIO` (no fileno), so
    there was no stack — not because a C call held the GIL. The watchdog thread
    was started and ran; it was paused with everything else.

    This is the whole machine/VM pausing. The guest uses `clocksource=kvm-clock`
    (boot 12:24 AM CT; `/proc/uptime` is continuous, no reboot). Wall clock
    (`time.time`) jumps on resume; monotonic does not include the pause.
    Unrelated processes show the same resume instants: `cursor-agent-store-fuse`
    logged `starting` at 5:24:45, 6:15:17, 7:16:42, and 8:03:25 AM CT, within
    a few seconds of each desk watchdog line. Overnight "keepalive ping timeout"
    recoveries also line up ~30–45s after those fuse starts (e.g. 3:16:31 →
    3:17:05 AM CT). Nothing on the box, including a 3-minute watchdog, can run
    during the pause. The downtime *is* the pause. On resume the in-process
    watchdog treated `file_age > 180s` as a wedged loop and SIGTERM/respawned
    a desk that would have recovered (WS ping-abort in ~40s). A second bug:
    publisher only heartbeated around a publish, and `desk.publish_heartbeat_s`
    is 180s, so the watchdog also cancelled an idle publisher every 3 minutes
    while the box was awake.

    Fix: detect a wall-vs-monotonic clock jump (`skew > 60s`) and treat it as
    box pause/resume — log it, wait 90s for heartbeats to refresh, do not kill.
    In-process watchdog only restarts agent *tasks* when the loop is actually
    alive and a heartbeat is stale; it no longer respawns the process on
    wall-clock file_age. `faulthandler` now dumps to a real file. Publisher
    heartbeats every 15s while idle. New `supervise_desk.py` is a separate
    process (`logs/supervise.pid`), started by `run_desk.sh`, that reads
    `heartbeats.json` every 30s and, if the desk is genuinely dead or wedged
    (pid gone, or file_age > 180s with no clock jump), SIGKILLs it and runs
    `./run_desk.sh`. The supervisor uses the same clock-jump + 90s grace test
    so a pause/resume does not look like a stale desk. A blocked-loop hook
    (`DESK_BLOCK_LOOP_S`, never set by run_desk.sh) and tests prove a
    sync-blocked child is killed and restarted. Settings, modes, and caps were
    not changed (FOMO PAPER, GMGN LIVE, $65 / 4, min_score left at 70 after the
    scheduled 8:00 AM CT revert). No orders were placed.

    Confirmation (21-minute watch starting 8:11:58 AM CT, 42 samples,
    1260s monotonic): desk pid 243182 and supervise pid 243183 stayed up
    with 0 alerts and no in-process watchdog lines. Wall time ran 1650s
    (max_skew=390s) from two pauses (~170s between 8:20:58 and 8:24:19 AM
    CT; ~220s between 8:30:19 and 8:34:28 AM CT). A third pause (~68s)
    after the watch ended was also classified correctly at 8:41:49 AM CT.
    `supervise.log` recorded `clock_jump` each time, including one with
    `file_age=235s` (the case that used to SIGTERM a healthy desk), and
    did not kill. After each resume, publisher and ws-pong ages dropped
    back under 15s within one 30s sample (scanner within two). `dmesg`
    logs `random: crng reseeded due to virtual machine fork` at each
    resume (kernel vmgenid). `dmesg -T` timestamps shift after later wall
    jumps, so the watch/supervise ISO times are the authoritative resume
    clock.


## Scanner re-check of fixable kills (owner request 2026-11-15 10:59 AM CT, t99u)
30. Most coins are ~1 minute old when the scanner first looks, so it killed them
    for things that can change with age (thin pool, concentrated top 10, early
    pump) and never looked again. Now a kill whose reason is `liq ...`,
    `top 10 hold ...` or `+N% in 1h` parks the coin as `status='recheck'` with a
    row in the new `rechecks` table, and the scanner re-scans it at the ages in
    `scanner.recheck_minutes` (default `5,10,20,30,45,60`, `off` disables;
    clipped to `scanner.max_age_h`). Permanent kills (not a pump mint, not
    SOL-quoted, copycat, never qualified) are never re-checked. Due coins join
    the normal room poll (same DexScreener batch / rate bucket / timeouts),
    bounded by `scanner.recheck_per_poll` (60) and `scanner.recheck_max_queue`
    (300; when full, new kills stay final). A re-check that passes goes to the
    auditor as usual (scan reason `recheck N: passed`); one that clears the
    filters but is still under min pool age / missing top 10 goes back to the
    waiting room (`recheck N: clears filters, back in waiting room`); one that
    still fails is recorded with verdict `recheck` (`recheck N: liq $3.0K ·
    next at 20m` or `· final`) so the night's kill count only counts the first
    kill. Past the 5h max age a parked coin is just killed. At start the
    scanner schedules recent fixable kills from before the restart.
    Same request: `scanner.max_top10_pct` 25 -> 30, and `auditor.max_top10_pct`
    25 -> 30 too (the auditor rejects at >= its bar, so leaving 25 would have
    rejected every 25-30% coin the scanner now lets through), and
    `analyst.min_score` 70 -> 60 for testing. `scanner.min_liquidity_usd` stays
    $10K. Modes, GMGN caps ($65 / 4) and the 0.02 SOL reserve were not touched.
    Desk restarted 11:03 AM CT (SIGTERM, then `run_desk.sh`).


## No scanner age limit + market-cap floor (owner request 2026-11-15 11:35 AM CT, t110u, "for testing")
31. `scanner.max_age_h` now accepts `0` / `off` (range 0-24, display "off") and is set to off.
    Off means no age cutoff in the scanner's decisions: a coin still in the waiting room past 5h is
    no longer dropped as "never qualified", and `recheck_minutes` are no longer clipped to 5h.
    The bookkeeping keeps a finite 24h horizon (`NO_AGE_LIMIT_HORIZON_MIN`) so dead coins that
    DexScreener never lists are not polled forever, parked re-checks are cleaned up after a long
    VM pause, and `backfill_room` only reopens drops from the last 24h. Re-checks keep their
    5,10,20,30,45,60-minute schedule. What it does NOT change: discovery is still only new
    PumpSwap migrations (PumpPortal websocket + GeckoTerminal new_pools catch-up), so older coins
    are never discovered; in practice the effect is limited to coins already in the waiting room.
    Age checks that remain: scanner `min_pool_age_min` 10m (waiting room) and auditor layer 6
    "Pool age" `auditor.min_pool_age_min` 10m (both minimums), and risk `timeout_h` 3h on held
    positions (not an entry check).
    New `scanner.min_market_cap_usd` = 30000 (aliases `min mc`, `market cap`, `min market cap`,
    `mc`; `30k` accepted; 0/off disables). Scanner kill `mc $X.XK` when DexScreener `marketCap`
    (fallback `fdv`) is below it; checked right after liquidity. Missing MC = `mc unknown` kill.
    Both are recheckable like `liq`. Scan JSON records `marketCapUsd`; Live feed rows show
    `MC … · liq …` (feed items carry `mc` / `liq`; SW `desk-shell-v8`). Modes, GMGN caps
    ($65 / 4), 0.02 SOL reserve, min score 60, liquidity $10K and top 10 30% untouched.
    0 gmgn_orders / 0 positions at restart; desk restarted 11:39 AM CT (SIGTERM, run_desk.sh,
    supervisor kept).


## Re-check only near misses (owner request 2026-11-15 12:26 PM CT, "use the margins")
32. New near-miss gates (Telegram-settable, `off`/0 disables each): `scanner.recheck_min_liq_usd`
    5000 (`recheck min liq` / `near miss liq`; `recheck liq` stays the executor setting),
    `scanner.recheck_min_mc_usd` 20000 (`recheck min mc` / `near miss mc`),
    `scanner.recheck_max_top10_pct` 40 (`recheck top10` / `near miss top10`),
    `scanner.recheck_max_h1_pct` 150 (`recheck h1` / `recheck pump` / `near miss pump`).
    At a coin's FIRST fixable kill the scanner looks at every fixable check that fails (liq, mc,
    top 10 if known, 1h change) and re-checks only if each failing value is within its gate
    (bounds inclusive: liq >= 5K, mc >= 20K, top10 <= 40%, h1 <= +150%). Unknown values never
    block, so `mc unknown` keeps re-checking. Otherwise the kill is final with reason
    `<reason> · too far for recheck` and scan JSON `tooFar` lists why; such reasons are never
    fixable, so the start-up backfill skips them, and the backfill applies the same gate to
    older kills (liq/top10/h1 from the coins row, mc parsed from the reason). Later re-checks are
    not gated and keep their schedule (`scanner.recheck_minutes` = 5,10,45 in DB, unchanged).
    Coins already parked before the restart keep their schedule. No other setting changed.
    0 gmgn_orders / 0 positions; restarted 12:28 PM CT (SIGTERM pid 313684, run_desk.sh ->
    pid 320499, supervisor 243183 kept).


## Wider exits: -15% stop, 20% trail (owner request 2026-11-15 12:36 PM CT)
33. `risk.stop_pct` 6 -> 15 and `risk.trail_pct` 12 -> 20 via Settings.set (source
    `chat: owner asked 2026-11-15 12:36 CT`; neither key is protected, so no force). These are
    shared: the GMGN hands-off condition orders (`gmgn_trader.conditions`: loss_stop
    price_scale, loss_stop_trace drawdown_rate, profit_stop_trace drawdown_rate; plus the
    repair stop_loss check_price and the local exit watch) and the FOMO/PAPER Risk agent all
    read them from settings on every buy, no constants. `risk.trail_start_pct` (+20%) and
    `risk.timeout_h` (3h) unchanged. Telegram buy/exit text and the app pills/GMGN card render
    from settings; the app.js fallback text changed 6/12 -> 15/20 (SW `desk-shell-v9`). Test
    `tests/test_exits_15_20.py`. Defaults in config.py stay 6/12 (DB value wins). 0 gmgn_orders /
    0 positions; restarted 12:37 PM CT (SIGTERM 320499 -> pid 326182, supervisor 243183 kept).


## Dev already out and steady is OK (owner request 2026-11-15 12:48 PM CT)
34. Auditor layer 3 (Dev wallet) used to reject on: no creator / no RPC balance (`no data: dev`),
    dev holding >= `auditor.max_dev_pct` 5% (`dev holds N%`), and ANY drop in the dev's balance
    since the scanner captured it at migration (`dev sold`; 3 of the 5 audit rejects so far).
    New: when the balance dropped but the dev now holds < 0.1% of supply (`devexit.DEV_OUT_PCT`,
    dust counts as out), the auditor finds the dev's last sell and passes if it was more than
    `auditor.dev_exit_wait_s` (60s) ago and the price is not more than
    `auditor.dev_exit_max_drop_pct` (20%) below the price at that sell. Exactly -20% passes.
    Layer text e.g. `0.0% · dev out 4m ago, price +3% since`. Fails: `dev selling (last sell 45s
    ago)`, `dev out 16m ago, price -95% since`, `dev sold (sell time unknown)`. Dev still holding
    >= 0.1% after selling stays `dev sold`; the other rules are unchanged.
    Sell timing (`devexit.last_dev_sell`): derive the dev's associated token account for the mint
    (token program from the mint's owner, Token-2022 for current pump mints), getSignaturesForAddress
    (newest 10), then getTransaction on up to 5 successful ones; the newest where the dev's
    pre/postTokenBalances for this mint went down gives the blockTime. Works when the account was
    closed. Unknown if the dev used a non-ATA account, the sell is older than the last 10 ATA txs,
    or the RPC fails -> fail as before. Sell price: the desk's own `prices` sample closest to the
    sell within 180s (scanner polls room / re-check coins about once a minute). Current price,
    m5, h1: DexScreener. If no sell-time price: fallback passes only if m5 > -20% and h1 > -50%
    (`CRASH_H1_PCT`). Live check: 9Aixk…pump (rejected 12:43 `dev sold`; dev out 12:34:50) and
    39fQb…pump both resolved their sell time and sell price; both are now -95% or more since.
    0 gmgn_orders / 0 positions; restarted 12:51 PM CT (SIGTERM 326182 -> pid 333722, supervisor kept).


## Analyst paper bypass + Real | Paper app tabs (owner requests 2026-11-15 4:03 / 4:05 PM CT)
35. New setting `analyst.paper_bypass` (bool, default False; Telegram alias `paper bypass`), turned ON for testing
    (change_log source `chat: owner asked 2026-11-15 16:03 CT (testing)`). When a coin FAILS the analyst gate, the
    setting is on, AND `desk.mode` is PAPER, `Analyst.forward` still sends it to the executor with `bypass=True`.
    The real score and failed reasons are recorded exactly as before (analyses row, reject activity, skip row).
    Executor, for bypass items: refuses (no signal, no push, no position) unless desk mode is PAPER; NEVER calls
    `gmgn.on_pass` (GMGN paper or live); runs the normal FOMO PAPER fill with the push prefixed
    `PAPER · BYPASS · score N ·`, the position named `BYPASS · <name>`, `signals.reason='analyst bypass'` and
    `executions.reason='analyst bypass'` (json `bypass: true`) so stats can split them. FOMO links unchanged
    (links.py, r=savipww). Normal risk exits apply (-15% stop / 20% trail). Coins that pass the analyst are
    unchanged: GMGN LIVE still only sees coins with score >= `analyst.min_score` (50). Test
    `tests/test_analyst_bypass.py`. Turn off with `set paper bypass off` (or the settings API).
    App: Home now has a `Real money | Paper` toggle (#/home, #/paper). Real = GMGN LIVE orders + LIVE positions,
    the wallet card and real P&L (tonight / all time). Paper = FOMO practice balance + chart, every PAPER trade
    (FOMO paper and GMGN paper), bypass rows badged `BYPASS · score N`. New encrypted feed file `trades.json`
    (publisher `Builder.trades`, no broker ids/bodies; test `tests/test_publisher_trades.py`). Live | Scores,
    candle background and coin detail unchanged. SW `desk-shell-v11`.


## Owner override from the app (owner request 2026-11-15 4:22-4:23 PM CT; "pick paper or real money each time")
36. The app stays read-only. On Analyst rejects only (never Scanner/Auditor kills; safety checks stay), the Live feed row
    and the Scores detail show `Override · Paper` and `Override · Real $`. They are Telegram deep links:
    `https://t.me/Denarpobot?start=ovp_<mint>` / `ovr_<mint>` (bot username from getMe, cached in meta `tg_username`,
    published as `tgBot` in feed.json/scores.json). Shown only while the reject is the coin's newest Analyst decision,
    <= 30 min old, not bought, and no override was used for that mint (publisher flag `overridable` + app age check).
    Telegram (`telegram.cmd_override`, `overrides.py`): owner chat 9127354951 only (non-owner messages/callbacks are
    ignored; the non-Telegram command path is refused).
    - ovp: one FOMO PAPER fill at a fresh price via the executor (`override=True`): push `PAPER · OVERRIDE · score N ·`,
      position `OVERRIDE · <name>`, signals/executions reason `owner override`, normal -15/20 risk exits. Desk must be
      PAPER. Never GMGN.
    - ovr: confirm message (coin, score, fresh price, % since reject, liquidity, MC, size, caps, exits) with Confirm /
      Cancel. Nothing happens without Confirm. Confirm expires after 5 min. The override is marked spent BEFORE the buy, so
      it works once per mint. On Confirm: GMGN must be LIVE, enabled, not halted; liquidity >= `scanner.min_liquidity_usd`;
      price not down > 20% since the reject (price at decision now stored as `priceUsd` in analyses.json); reject
      <= 30 min old. Then `GmgnTrader.on_pass` as a normal buy: $65/trade, 4 tickets/night, 0.02 SOL reserve, fresh
      wallet check and the -15% stop / 20% trail condition orders all apply unchanged. gmgn_orders.reason gets
      `owner override` (refusals `owner override: <reason>`). The result (fill or refusal) is pushed as the reply.
    Every attempt is logged in the new `overrides` table (asked / confirmed / done / refused / blocked / cancelled /
    expired / superseded) and logs/override.log. App: OVERRIDE badge on Real/Paper trade rows. SW `desk-shell-v13`,
    `?v=13`. Tests `tests/test_overrides.py` (8).


## Override buttons vs the analyst paper bypass (review fix 2026-11-15 4:30 PM CT)
37. With `analyst.paper_bypass` on, every Analyst reject got a PAPER position, which made `overridable` false, so the
    override buttons never showed. Now the feed/scores items carry per-button flags `overridablePaper` /
    `overridableReal` (`overridable` = either). Analyst-bypass buys (signals/executions reason `analyst bypass`,
    position `BYPASS · …`) hide ONLY the Paper button (app shows "On paper (bypass)"); Real $ still shows under the
    same 30-min / newest-decision / used-once rules. Any GMGN order tonight not refused/skipped, any non-bypass FOMO
    buy, or a used owner override hides both. Telegram: ovr on a bypass-paper coin works with every existing check;
    ovp on it replies "already on paper". Tests in `tests/test_overrides.py` (+4). SW `desk-shell-v14`, `?v=14`.


## Stage pings (owner request 2026-11-15 4:48 PM CT)
38. New setting `alerts.stage_pings` (bool, default True; `set stage pings off`). Owner-only Telegram pushes (existing
    notifier path, chat 9127354951) when a coin passes each stage, one per (night, stage, mint), deduped in the new
    `stage_pings` table (survives restarts and re-checks):
    - Scanner pass (incl. a re-check pass, suffix `· recheck N`): `✅ Scanner pass · <name> · liq $X · MC $Y` +
      "Open on FOMO" button (links.py, r=savipww). Sent only when the coin is first handed to the Auditor.
    - Audit pass: `🛡️ Audit pass · <name> · top10 N%` (Auditor layer 4 value).
    - Analyst pass: `🎯 Analyst pass · score N · <name>` / "Buy flow next."; the analyst waits at most 3s for it so it
      lands before the buy push.
    - Analyst reject: `Analyst · score N (min M) · <name>` + failed reasons + note that override buttons are in the
      app; inline URL buttons to the same deep links (`t.me/Denarpobot?start=ovp_/ovr_<mint>`). With the paper bypass
      on (desk PAPER) only `Override · Real $` is offered since the bypass already paper-buys it.
    Pings are background tasks (`stage_pings.py`); no trading decision, queue or setting changes. Tests
    `tests/test_stage_pings.py` (7).


## First LIVE GMGN buy: stop-attach race (incident 2026-11-15 6:11 PM CT, nema, gmgn_orders id 1)
39. What happened: owner override (real $) bought 1,334,328.338343 nema for 0.6553532 SOL (wallet -0.664625572 SOL incl.
    fees/rent). GMGN DID attach the swap's condition_orders as its own smart_trade strategy (order 6e4a1520, created 1s
    after the buy), but neither the swap nor query_order response carries `strategy_order_id`, so the desk thought
    exits were missing and "repaired" 3s after the swap (tokens not landed: `amount_in must be greater than zero`),
    market-sold (od..1c09 confirmed: 1,334,328.338343 nema -> 0.665785698 SOL; GMGN canceled its strategy,
    `token_clear`), then a racing second settle (watcher on the 'pending' row) repaired again and sent a second sell
    (InsufficientBalanceErr) and pushed "sell FAILED, sell it yourself" although the wallet held 0 nema.
    Real result: wallet 1.019270006 -> 1.012359329 SOL = -0.006910677 SOL (about -$0.50, -1.04%), which includes
    network/priority/tip fees and ~0.00204 SOL rent still in the now-empty nema token account. Row 1 corrected
    (closed, exit `no server-side exit (stop attach race)`, exit $0.000036135, pnl -$0.50, -1.04%).
    Fix (agents/gmgn_trader.py): settle runs once per order (`_settling`; the watcher skips those orders). On a confirmed
    fill without a strategy id, `ensure_exits` polls up to ~45s (15 x 3s): GMGN's open strategies for the mint
    (`find_strategy`, the source of truth) and the on-chain token balance (`token_balance`, GMGN holdings fallback).
    Only after a ~12s grace and with tokens held does it create a stop, with `amount_in` = the real held/filled raw
    amount, retrying while GMGN says amount_in is 0. Market sell only if that still fails, through `safe_sell`: never a
    second sell while a previous sell is pending/confirmed, never a sell with 0 tokens held. Alerts reflect the real
    on-chain balance ("sell it yourself" only when tokens are actually still held). The watcher closes with the real
    sell report (SOL out vs SOL in). Override confirm reply states the true exit state. Tests
    `tests/test_gmgn_attach_race.py` (5). No settings or modes changed.

## 40. Scanner tab (v15)
Third segment Live | Scores | Scanner. publisher.scanner_feed() writes encrypted scanner.json: newest 100 scans tonight, each with liq/MC/top10/h1/pump/SOL value vs bar (bars read from settings at build time), a pass/waiting/recheck/kill pill, recheck number and next recheck time, kill reason and a links.py FOMO link (r=savipww). Header counts are for the last hour: distinct scans seen, passed/killed by verdict, and coins currently in status 'recheck'. Read-only view; no trading paths touched.

## 41. Young pools first + MC surge re-scans (owner 2026-11-15 6:52 PM CT)
- **Young first** (`scanner.young_priority_min`, default 60, 0 = off; aliases "young first", "young priority", "young"): each poll the waiting room, due rechecks and surge coins are sorted surges first, then pools younger than N min (youngest first), then everything else in its old order. The recheck per-poll limit picks young pools first too. Older coins are still scanned every poll; nothing is excluded.
- **Surge watch**: a separate `surge` loop (own heartbeat, watchdog-restarted like the others) polls DexScreener `tokens/v1` (30 mints per call) every `scanner.surge_poll_s` (default 10s) for up to `scanner.surge_watch_max` coins (default 150, 0 = off; youngest first). Watched coins are coins seen tonight in the waiting room, in rechecks, or killed for liq / MC / 1h pump (including "too far" near-misses). Safety kills are never watched: not a pump mint, not SOL-quoted, copycat, any scanner top-10 kill, dropped "never qualified".
- A surge means MC is at least `scanner.surge_pct` (default 25%) above the lowest MC seen in the last `scanner.surge_window_s` (default 60s). It is logged in the `surges` table and the coin goes to the front of the queue. The scanner is woken for a fresh full scan right away, not at the next 60s poll. All scanner checks run unchanged, then the auditor and analyst. A surge passes nothing by itself, and a failing coin stays killed. If it can't pass yet (pool age or top 10 unknown), it goes back to the waiting room. Each mint then has a cooldown of max(5 × window, 5 min). A surge also needs the new MC to be at least $5K (SURGE_MIN_MC_USD): in the first live minute a dust pool at $14 MC "surged" +56%.
- Tags: the scans row and json carry `surge`. The stage pings show "⚡ Surge +N% in 1m": the scanner reads it from the item, and the auditor and analyst read it from `surges` (last 30 min). The Scanner tab shows a ⚡ pill, a surge count for the last hour and the surge and young settings. App v16.
- Rate budget: at 150 coins that is 5 calls every 10s, about 30 per minute, from the shared 250/min DexScreener bucket. A sweep is skipped when the bucket is already at 60% or more in the last minute, so the scanner, risk and auditor keep priority. PumpPortal trade subscriptions were not used: the desk keeps ONE PumpPortal socket for migrations, and DexScreener MC is what the scanner already decides on.

## 42. LIVE GMGN closes come only from the chain (The Same Coin, gmgn_orders id 2, 2026-11-15)
- **Incident:** the owner's Real $ override bought SAME at 7:00:32 PM CT (CEd1TApS: -0.669872226 SOL, +1,486,846.490944 SAME). At 7:01:44 PM CT the desk marked it closed: 'server', +$2.29 / +4.82%, and it pushed "sold (server-side exit) · about +4.8%". GMGN's loss_stop (strategy 5b0a9c45) really sold at 7:02:57 PM CT (3WZqAqbj: +0.576123734 SOL), a loss.
- **Root cause:** `_live_tick` read GMGN `wallet_holdings` with `holdings.get(mint, 0.0)`. A mint missing from the response counted as "0 held", and after 60s that meant closed. The endpoint returns 50 rows per page with a `next` cursor (ignored) and lags the chain, so the 7:01:44 PM CT read (the first live tick past 60s) had no SAME while the wallet still held every token. The exit price was DexScreener's last price, so the "+4.8%" was invented. Neither the 7:01:30 PM CT h8zb4mgv tx (pump.fun DistributeFeeToHolders, +0.000901172 SOL, no token change) nor the strategy caused it: the strategy stayed open until the stop fired.
- **Fix (chain.py, GmgnTrader.check_closed):**
  - A LIVE order closes only when (1) the buy tx is visible on chain, (2) the on-chain token balance (getTokenAccountsByOwner; an RPC failure is "unknown", never 0) is ≤ 0.1% of the tokens bought, and (3) sell tx(s) moving those tokens out are found in the wallet's recent signatures.
  - Exit and P&L use the wallet's real SOL deltas from the buy and sell txs (fees and rent included) at the order's stored sol_usd. exit_reason comes from GMGN's strategy history (the condition with status success: loss_stop → 'server-side stop', loss_stop_trace → 'server-side trailing stop', profit_stop_trace → 'server-side trail (take profit)'), or the desk's own sell reason.
  - If the balance is 0 but the sell tx isn't visible yet: status 'exiting' and ONE push "sold, confirming result", then one "final result" push with the chain numbers.
  - GMGN wallet_holdings is no longer fetched by the watcher.
- **Entry:** the real fill, (SOL paid ÷ tokens received) × sol_usd, from the buy tx (`entry_src='chain'`). GMGN's fee-less report price is used only until the buy tx is visible, and the watcher then replaces it.
- **Corrections (audit column + change_log; DB backup in data/):**
  - id 2: entry 3.2398e-05, exit 2.7864e-05, -$6.74 / -13.99%, 'server-side stop', closed 7:02:57 PM CT.
  - id 1: entry 3.6102e-05, exit 3.5727e-05; P&L unchanged at -$0.50 / -1.04%.
- New gmgn_orders columns (auto-migrated): sell_tx, entry_src, confirm_push_at, audit.

## 43. Settings written outside the desk hot-load (2026-11-15 7:12 PM CT)
- Settings.sync(), called every 5s from the heartbeat loop: when change_log has a new row, non-protected keys are reloaded from the settings table and listeners fire. Modes, GMGN caps and the live gate (PROTECTED) still need their Telegram flow or a restart.
- Why: the owner's analyst.min_alive_6h=0, min_organic=0 and max_chase=1 were written at 7:12:08 PM CT, 51s after pid 494828 started, so the running desk kept the old bars.
- Loads with the next restart.
- Same session: gmgn_orders id 3 (Cyber Dacity Coin) was falsely closed by the pre-#42 code at 7:10:14 PM CT. I reopened it at 7:13 PM CT (DB backed up, audit note). The new watcher picked it up and replaced the entry from the buy tx (1.7696e-04, chain).

## 44. Adaptive LIVE GMGN size + max 4 open (owner 2026-11-15 8:33 PM CT)
- **Sizing:** each GMGN buy is `gmgn.cap_trade_usd` ($50) when free SOL covers it, else `gmgn.min_trade_usd` ($20), else it is skipped with a plain reason.
  - Free SOL = wallet SOL (fresh /v1/user/info) - `gmgn.sol_reserve` - lamports committed to in-flight LIVE buys (submitting/pending).
  - A tier fits when input + GMGN's 1% fee (charged on top) + priority/tip + ~0.0021 SOL token-account rent fit inside free SOL. That cost is from The Same Coin's real buy.
  - The executor's liquidity cap (`executor.max_pool_pct`) still limits the size.
- GMGN size no longer comes from the FOMO executor (stack % with the $47.50 min-ticket bump), so the $50 tier really is $50. FOMO paper sizing is unchanged.
- **Max open:** `gmgn.max_open` (4) caps GMGN positions per mode at once, counting pending + open + exiting. A 5th buy is refused before any spend ("4/4 GMGN positions already open (max 4 at once)"); check_caps enforces the same rule.
- min_trade_usd and max_open are cap keys: PROTECTED, settable only with force (CAP_KEYS).
- `cap_trade_usd` default is now 50. Set in desk.db with force and source 'chat: owner asked 8:33 PM CT': cap 50 (was 20), min 20, max_open 4.
- Pushes: "Buying X · $50.00 (0.69 SOL) · n/4 open" and "Bought X · $50.00 · n/4 open".
- The status line shows the size, the fallback, free SOL → next size, and n/4 open. The override ask shows "Size $50.00 ($20.00 if free SOL is short)" and the open count.
- `gmgn live` now checks that the $20 fallback fits.

## 45. Below $20, buy whatever free SOL covers, down to $1 (owner 2026-11-15 8:40 PM CT)
- pick_size order: $50 (`gmgn.cap_trade_usd`) if it fits → $20 (`gmgn.fallback_trade_usd`, new; keeps the 8:33 PM tier) → the largest whole-cent amount whose full cost fits free SOL, down to the floor `gmgn.min_trade_usd` ($1) → skip ("free X SOL can't cover even $1.00 …").
- Full cost = input + 1% GMGN fee on top + priority/tip + ~0.0021 SOL rent. Free SOL = wallet - 0.02 SOL reserve - in-flight buys. The size never exceeds $50 or the pool cap.
- The $1 floor keeps a buy from being dust that fees eat. All three sizes are cap keys (PROTECTED, force only), set with source 'chat: owner asked 8:40 PM CT'. max_open (4) is unchanged.

## 46. Re-entry after a LIVE GMGN exit (owner 2026-11-15 8:52 PM CT; bounce 7% at 8:53 PM CT)
"Make it to where coins like the cat one that just sold get a recheck and buy if they start coming back after sell."
- Every LIVE GMGN close (any exit reason, any coin) arms a `reentry_watch` row from the chain close (`check_closed`), for
  `gmgn.reentry_watch_min` (60) minutes from the sell. Post-sell low seeded from the last DexScreener price before the sell.
- `reentry.py` polls every `gmgn.reentry_poll_s` (15s), one batched DexScreener tokens call for all watches; tracks the low,
  price, liquidity, m5 buys/sells. Heartbeat "reentry".
- Trigger: price ≥ low × (1 + `gmgn.reentry_bounce_pct`/100) (7%) AND m5 buys > m5 sells.
- Drop: liquidity down > `gmgn.reentry_max_liq_drop_pct` (50%) since the sell, or under `scanner.min_liquidity_usd`;
  also when the desk already holds the mint. Expire at the window end.
- On a trigger: full Auditor (`reentry=True`, still refuses a coin that went red tonight), then the Analyst with current
  gates (`reentry=True` skips only the once-per-night analyses guard), then `gmgn.on_pass(..., reentry=info)`: normal
  adaptive size, max open, caps, same server-side exits. GMGN only; the FOMO executor is not involved.
- One trigger per watch (a reject ends it); `gmgn.reentry_max_per_coin` (1) re-entry orders per coin per night,
  counted from gmgn_orders reason 're-entry'. A closed re-entry doesn't arm another watch once the limit is used.
- Labels: stage pings "🔁 RE-ENTRY · …" (own dedupe stages reentry_trigger/reentry_auditor/reentry_analyst, no override
  buttons); GMGN pushes "GMGN · RE-ENTRY · Buying X · $50.00 (… SOL) · bounced +N% off post-sell low · n/4 open", and
  the Bought / sold / final result pushes for that order carry "RE-ENTRY · ".
- `gmgn.reentry_enabled` true (source 'chat: owner asked 8:52 PM CT'); `gmgn.reentry_bounce_pct` 7 (source 'chat: owner
  asked 8:53 PM CT'). None of these are protected.
- scanner.json gets `reentry` (watch list) and `limits.reentry`; data only, web/ unchanged.
- Seeded: ni hao cat (order 5, sold 8:36:51 PM CT, trailing stop), watched until 9:36:51 PM CT.

## 47. Phone app stuck on old data (2026-11-16 ~4:32 AM CT)
The Pages site kept serving state.json from 9:22 PM CT (2026-11-16T03:22:40Z) for 7+ hours while
the desk published a fresh gist every minute and the publish workflow reported success.
Root cause: `actions/deploy-pages` uses `GITHUB_SHA` as `pages_build_version`. Data changes live
in the gist, so the repo SHA did not change; every deploy reused the same version id
(`bbcb48e…`). After a mid-flight cancel or a failed deploy, GitHub Pages treated later
same-version deploys as already done and left the live files at 03:22. A later deploy briefly
updated the site at 10:33 UTC, then it stuck again. Unauthenticated gist fetches also returned
intermittent 403s.
Fix: the Pages API requires `pages_build_version` to be a git SHA that exists in the
repo (a gist revision 404s) and `actions/deploy-pages` always sends `github.sha`, so
data-only deploys reused one version. The workflow force-pushes a unique empty-tree
commit to the `pages-deploys` branch (no files, no secrets; that branch is allowed in
the github-pages environment) and deploys that SHA via the Pages API.
`cancel-in-progress` is false (Actions starter default) so a new run cannot cancel a Pages
deploy mid-flight; gist fetch retries plus a raw-URL fallback; the publisher skips dispatch
while a run is in progress (gist is still updated) and reads back the live state.json ts,
logging and Telegram-alerting when it is more than 15 minutes old (30 min alert cooldown).

## 48. 5-minute stop-loss grace on new GMGN LIVE buys (owner 2026-11-16 2:44 PM CT)
"Can we have the stop loss turned off for the first 5 minutes unless we see a steep drop in mc", then "sell early if it drops 30% from entry".
- New settings, not protected, hot-reloaded by Settings.sync: `risk.grace_min` (default 5, 0 = off, 0–60) and `risk.grace_stop_pct` (default 30, 1–90).
- On a new LIVE buy (including re-entry and owner override) with grace on, the swap's condition_orders are a server-side `loss_stop` at -grace_stop_pct and `profit_stop_trace` from +trail_start_pct. The drawdown trail (`loss_stop_trace`) is skipped during grace so a 10% peak-to-trough dip can't sell it early. PAPER simulates the same rules.
- `grace_until` and `exits_phase` ('grace' | 'normal') are stored on `gmgn_orders` at fill time (auto-migrated columns). After `grace_min` from the fill — or immediately on resume if the box was paused past the window — the desk replaces those exits with the normal set (`loss_stop` at -stop_pct, `loss_stop_trace`, `profit_stop_trace`) through GMGN's strategy API.
- Swap order: try to create the new strategy first, then cancel the old one, so a stop is always on. If GMGN rejects a second strategy, cancel then create with retries. If the old cancel fails but the new stop is on, both stay (the tighter one will fire first) and Telegram is alerted. If cancel succeeds and create cannot be attached (including a fallback limit_order stop at -stop_pct), the desk market-sells and alerts, same last resort as a missing-strategy repair. Alerts for a stuck cancel are not repeated every tick.
- If, at swap time, the price is already below -stop_pct, attaching the tighter stop may sell immediately. That's intended.
- Existing open positions with no `exits_phase` (The Great Blackout, id 16, and any other pre-change row) are left alone; their exits stay as they were at purchase.
- Telegram buy / bought pushes and the app (`exits.label`) say "stop -30% for first 5 min, then -15%". After the swap they show the normal stop and trail. App shell v17.
- A mid-grace repair (GMGN didn't attach the swap's conditions) uses a server-side limit stop at -grace_stop_pct, then the same promote path later.

## 49. Surge-top sell on open GMGN positions (owner 2026-11-16 4:26 PM CT)
"If it had bought a stock and the stock surges really fast then tops out it will sell."
- New settings, not protected, hot-reloaded: `risk.surge_sell_enabled` (true), `risk.surge_min_pct` (25), `risk.surge_window_s` (180), `risk.surge_top_drop_pct` (8).
- New desk job `gmgn_surge` (heartbeat `gmgn_surge`) in agents/gmgn_trader.py checks every open GMGN position about every 3s with ONE DexScreener `tokens/v1` call for all open mints (~20 calls/min, through the shared 250/min bucket; a 429 backs off `risk.backoff_429_s`). No call when nothing is open.
- Surge (arms): price is above entry AND it is up >= surge_min_pct from the lowest price seen in the last surge_window_s (the buy price counts as the starting low for the first window), OR up >= 20% from the lowest price in the last 60s (code constant). A bounce that stays under entry never arms.
- Top (sells): once armed, the desk tracks the post-surge peak; when price falls surge_top_drop_pct (8%) from that peak it market-sells 100% through the existing `safe_sell` path, exit_reason `surge top`, then cancels the position's server-side exits (known strategy id, then any other open strategy on that mint). The close is confirmed from the chain by the normal watcher (DECISION 42) on its next pass, which sends the usual "GMGN · <name> sold (surge top) · ±$ (±%)" ping. A "surged +X% then topped, market-selling" push goes out at sell time.
- Buy/sell counts were considered for the top signal; DexScreener only gives 5-minute txn buckets, so the top is price-only.
- Works during the 5-minute grace window (it is independent of exits_phase). It skips any order that a settle/grace-swap is touching (`_settling`), and any order not 'open' or with a sell already sent.
- No double sell: if the wallet already holds 0 tokens (a server-side exit fired), it does not sell and does not set exit_reason; it marks the row due so the watcher records the real server-side reason. If the market sell fails, server stops stay on, Telegram is alerted once, and it retries after 15s while still below the top line.
- PAPER positions use the same rule and close at the observed price.
- Surge state is in memory; after a restart it rebuilds from the next samples (the buy price is reused as the low only inside the first window).
- Limitation: DexScreener prices are cached upstream and can lag the chain by several seconds, so the actual sell can land below the -8% line on a fast dump.

## 50. Pinned requirements + venv self-heal (owner 2026-11-16 4:26 PM CT)
After a box restore dropped the gitignored .venv (desk down 3:23–4:16 PM CT), `requirements.txt` is pinned from the working venv's freeze (incl. pytest). `run_desk.sh` (also used by supervise_desk.py, main.py's respawn and desk-health) checks `.venv/bin/python` can import httpx/websockets/cryptography; if not, it rebuilds .venv with python3.13 and `pip install -r requirements.txt` under a flock (about 13s), then starts. Healthy path adds ~0.1s.

## 51. Scan coins beyond pump.fun (owner 2026-11-16 5:22 PM CT)
"Can we set it to look at not only pump fun coins."
- New setting `scanner.sources` (choice `all` | `pump`, default `all`, hot-reloaded, aliases "sources", "scan sources"). `pump` restores the old behaviour exactly (any non-`…pump` mint is killed "not a pump mint" at intake, in decide, and is never surge-watched).
- New setting `scanner.other_max_mc_usd` (default $10,000,000, 0 = off). Non-pump coins only: a coin above it is killed "too big: mc $X" (permanent, never re-checked). Pump mints are unaffected.
- Sources added (no new rate-limit families):
  1. PumpPortal `subscribeMigration` (same single socket): it already carried non-vanity pump.fun graduates (pool `pump-amm`, mint not ending in `pump`) and LetsBonk graduations (pool `raydium-cpmm`). These were killed before; now they go through the normal filters.
  2. GeckoTerminal `new_pools` (same pages the WS-gap backstop already fetches each poll, so zero extra calls): SOL-quoted non-pump pools on graduated DEXes (`sources.GRADUATED_DEX_GECKO`: PumpSwap, Raydium AMM/CPMM/CLMM, Meteora DAMM v1/v2/DLMM, Orca, …) with reserve >= $1,000 at intake. Bonding curves (pump-fun, meteora-dbc, raydium-launchlab, moonshot, boop) are skipped, same as pump.fun coins before graduation. Feed name `new_pools`, pool_label = Gecko dex id.
  3. DexScreener `token-profiles/latest/v1` + `token-boosts/latest/v1` (Solana, non-pump), once a minute, plus one `tokens/v1` lookup for unseen addresses: about 3 calls/min through the shared 250/min dex bucket. Admitted only with a graduated pool and an oldest pool younger than the watch window; migrated_at = oldest pool's pairCreatedAt. Feed name `dex_latest`, pool_label `dex:<dexId>`. Each address is looked up once per run.
- Pair choice (`pick_pair`): pump mints unchanged (PumpSwap only). Other mints: the most-liquid non-bonding pool, SOL-quoted first (a non-SOL best pool still gets the existing "not SOL-quoted" kill).
- Exclusions for non-pump coins: stablecoins, SOL and liquid-staked SOL, wrapped BTC/ETH, and big established tokens (JUP, BONK, WIF, RAY, PUMP) by mint list or stable symbol → "excluded: X" (permanent). Also "old token: first pool Nh ago" when the token's OLDEST DexScreener pool is older than the watch window (scanner.max_age_h, 24h horizon when off), so a brand-new pool on an old token never counts as a new coin.
- All existing filters, near-miss re-checks, age priority, surge watch, auditor and analyst are unchanged and apply to every source.
- Holder math (scanner top 10 + auditor layers 4/8): program-wide pool authorities (Raydium AMM v4, Raydium CPMM, Meteora DAMM v2) are excluded like the pool address, since non-pump pool vaults are owned by those, not by the pool. Both Token and Token-2022 accounts were already read.
- Dev check (auditor layer 3) for non-pump coins: creator comes from RugCheck `creator` (captured at intake, as for pump.fun migrations; DexScreener has no creator field). `creator_balance_raw` is the creator's balance when the desk first saw the coin (pool creation for new_pools, first sighting for dex_latest), instead of at pump.fun migration. No creator or no balance → "no data: dev" red, the same rule as today. The dev-exit timing check reads the mint's real token program (Token or Token-2022).
- Liquidity layer: RugCheck markets[].pubkey matches the DexScreener pairAddress for Raydium/Meteora/PumpSwap pools (verified live), so lp lock is read the same way; no matching market → "no data: liquidity" red as today.
- Telegram stage pings end with "· via <launchpad>" (pump.fun, letsbonk, meteora, raydium, pumpswap, …). Scanner log lines end with `src=<launchpad>`; the app's scanner check "Pump mint" becomes "Source (<launchpad>)" in `all` mode.
- GMGN buy/sell path has no pump-mint assumption (any SPL mint; token_balance reads Token and Token-2022 accounts via getTokenAccountsByOwner with a mint filter).

## 52. Desk-side trailing take-profit + USD limit stops (owner 2026-11-16 ~6:00 PM CT)
"Can we keep it the same but make the take profit work". The 5-minute grace setup is unchanged.
- Problem: after grace, GMGN rejects the smart_trade swap ("condition_orders must contain exactly one buy_low entry") for positions it didn't open through a swap with conditions, so promote fell back to a limit_order stop_loss only and no take-profit ran. Adopted app buys (#26/#27 Debu, #30 Mooncoin) were the same. The row's `conditions` still claimed the trails.
- Found while checking: `stop_strategy_body` sent limit-order `check_price` in SOL. GMGN compares limit-order check_price in USD (its own condition check_prices and an app-made take_profit are USD-scale), so every desk limit stop sat ~99% under entry and never fired. Debu #27 (-63%) and Mooncoin #30 (-69%) were sold from the GMGN app with the stops still unfired (both strategies show `canceled` at the sell second, not triggered). Fixed: check_price = entry_usd * (1 - stop_pct/100). Jensen Huang #28's stop d67d68e7 (5.50e-7) was replaced by 0a154b84 at 3.909e-5 USD (-20%), create-then-cancel, 6:10 PM CT.
- New job `gmgn_trail` (heartbeat `gmgn_trail`), every ~3s, one DexScreener call for the eligible mints: open LIVE orders whose live exit is a limit stop only (`limit_stop_only`: repair has "stop_loss strategy created" or `limit_stop`, or conditions without profit_stop_trace). Promote fallback now records `limit_stop` and stop-only conditions, and its push says the take-profit trail runs from the desk.
- Rule (`desk_trail_check`, settings read live every tick, nothing hardcoded): arm once peak >= entry * (1 + risk.trail_start_pct); armed and price <= peak * (1 - risk.trail_pct) -> market-sell 100% via `safe_sell`, exit_reason `desk trail`. Backup desk stop at -stop_pct (-grace_stop_pct inside an order's grace) -> `desk stop`. The GMGN limit stop stays on until our sell is sent, then the mint's open strategies (STMix and LimitOrder) are cancelled. Same guards as the surge sell: skips `_settling`, never a second sell, 0 held -> no sell and the watcher records the real reason; failed sell -> one alert, retry after 15s.
- First sight (new position, restart): the peak is seeded from the row's peak, but if price is already under that trail line the trail restarts from the current price, so a restart never sells on a stale peak.
- Applies regardless of `no_surge` (that flag is the surge rule only). Smart_trade positions with server-side trails are not touched. Never buys, never adds a buy_low. Trails run only while the desk is awake; the GMGN -stop_pct stop is server-side.
- `Gmgn.strategy_orders` takes `group_tag` ('STMix' default, 'LimitOrder'). Tests `tests/test_gmgn_desk_trail.py`; two older tests updated to the USD check_price.

## 53. Market-cap approval for GMGN LIVE buys (owner 2026-11-16 6:08 PM CT)
"Add a market-cap approval cap": above $300K, ask instead of auto-buying.
- New setting `gmgn.approve_above_mc_usd` (code default 0 = off, not protected, hot-reloaded); set to 300000 by the owner's request (change_log source 'chat: owner asked ...').
- In `GmgnTrader._on_pass`, LIVE only, after the open/once-per-night/halt/max-open checks and before sizing: if the coin's market cap (from the executor's DexScreener pair, else a fresh DexScreener read) is above the limit, or unknown, nothing is bought. A row goes into the new `gmgn_approvals` table and the owner chat (the notifier only sends to OWNER_CHAT_ID; callbacks and text from any other id are ignored) gets "GMGN · APPROVE? <coin>" with market cap, price, liquidity, score, the FOMO link from links.coin_url (r=savipww), and buttons Approve buy (`gmap:<id>`) / Skip (`gmsk:<id>`) / Open on FOMO. Text fallback: "approve <id>" / "skip <id>". At or under the limit, unchanged. PAPER never asks.
- Approve: the request is spent first (a second tap can't buy twice), then a fresh DexScreener check: GMGN must still be LIVE, liquidity >= scanner.min_liquidity_usd, price not down > 20% since the ask. Then the normal `on_pass` buy with approved=True: every cap, size, reserve, grace and exits apply unchanged.
- Expiry: 10 minutes. The GMGN watcher marks it expired and pushes "approval expired ... Nothing was bought." A late Approve is refused. A pending ask isn't repeated; after Skip / expiry the same coin isn't asked again that night (re-entries can ask again).
- Tests `tests/test_gmgn_mc_approval.py`.
- (52, follow-up 6:15 PM CT) The desk trail never sells when the wallet balance can't be read (retries in 15s) and skips rows that already have an exit_reason. When a LIVE close isn't explained by a fired STMix condition, the LimitOrder history decides: limit stop filled -> "server-side stop", limit stop cancelled -> "manual" (sold by the owner in the GMGN app). Jensen Huang #28, sold by the owner at ~6:11 PM CT, was marked exit_reason 'manual' before its close was recorded.

## 54. Strategy B · PAPER ONLY, built from the research Page (owner 2026-11-16 ~6:50 PM CT)
"Can you make a separate trading strategy that is completely different from the one we're using now based off of that research" (Page "What top memecoin traders do vs the desk"). Follow-up: "add a separate Strategy B tab" to the phone app.
- New module `strat_b.py` (`StratB`, job + heartbeat `strat_b`, in HB_JOBS). PAPER ONLY: it imports nothing from gmgn/gmgn_trader/solana/secrets_store, has no client, never signs or sends anything but Telegram pings to the owner chat through the normal notifier. `strat_b.mode` is a choice with ONE value (PAPER) and is PROTECTED; `tick()` refuses to run unless it is PAPER. A test checks the module's imports with `ast`.
- Strategy A (live GMGN) is unchanged: no A setting, mode, cap, table or code path changed. The only shared-code edits: the auditor also calls `ctx.strat_b.offer(scan, audit)` for coins it passes (after the analyst put; `offer` never raises), the Desk builds/runs StratB, `ctx.strat_b`, publisher adds `strat_b.json` + `state.stratB`, db adds two tables, config adds `strat_b.*` (no bare-name aliases, so "stop", "trail", "max open" still mean A's settings).
- Feed: same scanner + auditor feed as A (auditor-passed coins), its own decision logic. It does not use the analyst's gate or score.
- Entry (rules, value, source):
  - static, at audit pass: top 10 ≤ 20% (Fried Dev video, Parasol), ≥ 75 holders (Fried Dev "70–80+"), bundled wallets ≤ 5% of supply (pumpdata.fun: bundles graduate then fall; 5% is a judgment value, no source gives a number). Fail → `skipped`, never watched.
  - wait: no buy until 60 min after graduation (Hidden Hand: 87% of graduates are below half their graduation price an hour later). Watched up to 180 min after the audit, then `expired`.
  - zone: market cap $30K–$1M (swing holder Gake buys $30K–$1M; flippers' sub-$30K curve entries aren't copyable after slippage per KOL Brain), liquidity ≥ $10K and the $50 ≤ 2% of the pool, 24h volume ≥ 0.5x market cap (Parasol: under half is a walk-away).
  - trigger: a pullback of 15–40% from the highest price seen while watching, then a ≥ +5% bounce off the low since that high, with 5-min buys ≥ sells (Parasol: buy pullbacks, not vertical candles). A new high restarts the pullback. 15/40/5 are judgment values.
  - one buy per coin ever (Memecoin Guru: never average down; no re-entry).
- Size: $50 flat from a $1,000 simulated bankroll (5%, top of Trojan's 1–5%), max 5 open, no new buys after -10% of bankroll in a desk night (MoonHydra daily stop). No market-cap approval (paper).
- Exits, checked every 10s from one DexScreener `tokens/v1` call for open + watched mints (≤ 40, so ≤ 2 calls, ~6–12/min of the shared 250/min bucket; 429 → backoff risk.backoff_429_s):
  1. stop -35% from the entry fill, from the first second, no grace (guides -30% to -40%, three.ws -35%).
  2. take profit: sell 100% at +100% (2x) (guides take money at 2x; on-chain pros sell 90%+ in one sell on 71–87% of positions, almost never initials-only, so B sells all instead of half).
  3. spike: price ≥ +30% over the lowest price in the last 60s and above entry → sell 100% at once, no waiting for a top (pros sell into strength; 30%/60s is a judgment value).
  4. trail: arms at +50%, sells on a 20% drop from the high, so it can't sell under ~+20% (clusterz; SOLTidy arms at 1.5x).
  5. time stop: 24h (three.ws).
  6. liquidity under 20% of entry liquidity → sell ("liquidity pulled"). With no price it keeps holding until the time stop.
- Fills: DexScreener priceUsd ± (1% + 2 × trade / liquidity) slippage, 1% fee + $0.25 network fee per side (close to GMGN's 1% fee plus priority/tip). A gap through a stop fills at the observed price. A flat round trip costs ~4.5%.
- Tables `strat_b_watch` (one row per coin: watching|skipped|expired|bought) and `strat_b_orders`; log `logs/strat_b.log` (start/watch/skip/wait/expired/BUY/SELL/trail armed). Restart-safe: state is in the tables (spike history rebuilds from new samples).
- Telegram: buys and sells only, "STRATEGY B · PAPER · Bought …" / "STRATEGY B · PAPER · Sold … (reason) · ±$ (±%)", with an Open on FOMO button from links.coin_url (r=savipww).
- Results: app tab "Strat B" (strat_b.json: bankroll, P&L tonight/all, W/L, win rate, profit factor, open positions, recent trades, the rules); `state.json.stratB` summary; `.venv/bin/python strat_b_report.py` (or `--json`) prints the scorecard.
- Settings (hot-reloaded, not protected except mode): strat_b.enabled, bankroll_usd, trade_usd, max_open, daily_loss_stop_pct, min_age_min, watch_max_min, min_mc_usd, max_mc_usd, min_liq_usd, max_pool_pct, max_top10_pct, min_holders, max_bundle_pct, min_vol_mc, pullback_min_pct, pullback_max_pct, bounce_pct, stop_pct, take_profit_pct, trail_start_pct, trail_pct, spike_pct, spike_window_s, time_stop_h, slippage_pct, fee_pct, net_fee_usd, poll_s. enabled=false stops new watches/buys; open paper positions keep their exits.
- Not done: the "no single wallet over ~3.5%" check (the feed has no per-wallet holder list) and tracing where top wallets' SOL came from.
- Web: new bottom tab "Strat B" (#/stratb), read-only, PAPER badges; shell cache desk-shell-v19, assets ?v=19. Tests `tests/test_strat_b.py`.

## 56. Surge profit floor on GMGN positions (owner 2026-11-16 6:45 PM CT: "Lock in at least +5% after a surge")
- Why: Petryk The Hare (#32, LIVE) armed the surge rule at +20.2% (00:38:06Z), the desk saw an -10.4% drop from the 5.106e-05 peak at 00:41:37Z and market-sold, but it filled at 3.381e-05 = -20.7% vs entry (-$4.21).
- Rule (`surge_check`, `risk.surge_floor_pct`, default 5, 0 = off, not protected, change_log source 'chat: owner asked 2026-11-16 6:45 PM CT'): once a surge is armed, the desk market-sells 100% when price <= entry x (1 + floor%) OR price <= post-surge peak x (1 - surge_top_drop_pct) — whichever comes first. exit_reason `surge floor` (label "surge profit floor") for the floor, `surge top` for the 8% drop; same `safe_sell` + cancel-server-exits path as DECISION 49, PAPER closes at the tick price.
- If price is already under entry when either line would fire (missed both, e.g. a one-tick crash), the surge rule stands down ('missed', logged once as "surge missed") and the server-side stop / desk trail handle the coin, the same as a non-surged one. Before this change a surge top under entry would still market-sell.
- Per-order `no_surge` opt-out still skips the whole watcher, floor included. No other settings changed.
- Petryk slippage finding: on-chain trades (GeckoTerminal, pumpswap pool D27X3Bq2…) show the 8% line (4.698e-05) was crossed at 00:40:14Z (4.64e-05, still +8.9% vs entry) — 83 s before the desk fired. The desk's DexScreener price at 00:41:37Z (4.577e-05) was the on-chain price of ~00:40:49–00:41:15; a ~$900 dump at 00:41:15–16Z had already taken the pool to 3.66e-05, and our sell landed at 00:41:37Z at 3.438e-05 pool price. So the loss was feed lag (DexScreener ~20–80 s behind the pool on this coin), not the 3 s poll or GMGN execution (sent and filled in the same second). The floor alone would not have saved Petryk: the dump went from +7.5% to -14% in one second, so by the time any lagged feed saw it the coin was under entry. A fresher price (GMGN quote or pool reserves over RPC, only for armed positions) is the real fix for that; not built here — needs a check of GMGN quote rate limits first.
- Tests: `tests/test_gmgn_surge.py` (floor before top, top before floor, missed under entry, floor off, LIVE + PAPER floor sells, no_surge skips floor).

## 57. Real-time pool price for the GMGN exit watchers (owner 2026-11-16 6:52 PM CT: "Yes build that also")
- Why: on Petryk The Hare (DECISION 56) DexScreener ran 20–80 s behind the pool, so the surge sell fired 83 s late at -20.7% instead of ~+9%.
- New `poolprice.py` (parsers) + `fastprice.py` (feed). `gmgn_surge` and `gmgn_trail` now price open **LIVE** positions in this order: (1) `pool` — the pool's on-chain state over RPC (`desk.rpc_url`), ONE `getMultipleAccounts` (commitment confirmed, ≤100 accounts per call) for all open mints; (2) `gmgn_quote` — GMGN sell quote for ~$1 of the token (GET /v1/trade/quote, read-only), at most one call per 2 s, 30 s backoff on any error, honours the plan-block flag; (3) `dex` — DexScreener tokens/v1 as before, at most one call per 3 s. PAPER rows and tests that pass `pairs=` still use DexScreener.
- Supported pools: pump.fun bonding curve (virtual reserves; non-SOL-quoted curves refused), PumpSwap (vaults + signed i64 quote-reserve adjustment @245 — not in the old IDL; without it new pools read 40–94% low), Raydium AMM v4 (vaults − need_take_pnl), Raydium CPMM (vaults − protocol/fund/creator fees), Raydium CLMM + PancakeSwap CLMM fork, Orca Whirlpool, Meteora DAMM v2 (sqrt price), Meteora DLMM (active bin). Unsupported (→ fallback): Meteora DAMM v1, DBC, Fluxbeam, other programs, non-SOL/USDC/USDT quotes. USD = SOL price × GMGN's native_token_usd_price (cached 60 s).
- Trust check per mint at resolution: pool vs DexScreener within 35%, else pool vs Jupiter price API within 15% (DexScreener can trail a crash by more than 35%); otherwise that mint uses the fallbacks and is re-checked after 120 s. Logged to gmgn.log as "pool price check ok/FAILED". Each source change per mint is logged ("price source pool:pumpswap" / "gmgn_quote" / "dex" with the reason); surge armed/trigger/missed and desk-trail lines carry `src=`; heartbeats show `src=pool=N`.
- Rate limits: polls only when a LIVE position is open, every `risk.fast_poll_s` (new, default 1.5 s, 0.5–10, not protected); both watchers share one cached read, so ~1 RPC call per poll. 429 → exponential backoff 2→4→…→60 s, other RPC errors 5 s; fallbacks cover the gap. With nothing open: no calls; with the feed off the loops keep the old 3 s.
- New setting `risk.price_source` (`pool` | `dex`, default `pool`, alias "price source"; `set price source dex` restores the old feed exactly). No existing setting changed. Never buys, never signs, never adds a buy_low.
- Validation (2026-11-16 ~7:00 PM CT, live pools): on ~60 pools across all 8 pool types the pool price matched GeckoTerminal/DexScreener within ~0–4% where those were fresh (e.g. DAMM v2 +0.01%, Raydium v4 0.00% vs Gecko, CPMM −0.25%, CLMM −0.26/+0.15%, DLMM +0.20%, Whirlpool −0.05%, pump curve −0.23%); on six active PumpSwap pools it matched the latest on-chain swaps within −1.5…+3.9% (swap prices include fees/impact); Jupiter agreed within 1–4%.
- Latency (4-minute run, 5 active coins, real FastPrice + desk Net, 0 × 429, 126 RPC calls ≈ 1 per 1.9 s, p90 call 89 ms): the pool price changed every poll; DexScreener's price changed only 3–7 times in 4 min (~every 30 s) and best-fit lag was 31–53 s behind the pool. Two live crashes in that window: LUCIA (pumpswap) rugged −99% starting at t+154 s and DexScreener still showed the pre-rug high until t+212 s; JENSEN (pumpswap) fell −31% at t+92 s, DexScreener saw it at t+121 s, and a second −35% at t+168 s showed up at t+212 s.
- Tests: `tests/test_fastprice.py` (parsers for every pool type incl. the PumpSwap adjustment, one batched RPC call per tick, cache, 429 backoff → GMGN quote → DexScreener, quote backoff, sanity check with/without Jupiter, price_source=dex, surge and trail sell on the pool price while DexScreener lags, never a buy).

## 58. Original paper test restarted (owner 2026-11-16 6:52 PM CT: "go ahead and restart the original paper test")
- What it is: the FOMO PAPER test — desk.mode PAPER with the simulated $200 stack (`desk.paper_start_stack`) and FOMO's LIVE gate "1 full PAPER night + owner types LIVE" (DECISION 13/20). Not Strategy B (DECISION 54, separate tables) and not GMGN (gmgn.mode is LIVE; its 2h PAPER gate is unrelated).
- State before: 15 closed FOMO PAPER trades, −$170.93 (stack $29.07); 0 completed PAPER nights (only night 2026-11-14-1640, still open).
- Reset (new `paper_test.py restart`, history kept): meta `paper_base` = 200, `paper_base_ts` = `paper_test_since` = 2026-11-16 7:10:58 PM CT; change_log #75 key `desk.paper_test`, source 'chat: owner asked 2026-11-16 6:52 PM CT restart paper test after many changes'. Effects: PAPER balance back to $200 (ctx.balance counts PAPER closes from paper_base_ts); app Paper P&L / wins / losses and the PAPER chart count from the restart (publisher `_pnl_baseline` PAPER branch, chart filter); the LIVE gate counts only full PAPER nights that started at/after the restart (`telegram.paper_nights_done`). No trades, nights or reports deleted. desk.mode, gmgn.mode, caps and every setting unchanged.
- Found, not changed: the 8:00 AM night rollover (`main.night_loop`) only fires if the loop runs during the exact 08:00 minute; on Nov 15 and 16 the box was paused then (watchdog "event loop frozen" at 14:03Z), so the night has never rolled over since 2026-11-14 4:40 PM CT and no PAPER night can complete until that is fixed. A catch-up fix would also start a new night for GMGN's per-night ticket count, so it needs the owner's OK.
- Tests: `tests/test_paper_test_restart.py`.

## 59. Catch-up night rollover (owner approved 2026-11-16 7:21 PM CT, t269u)
- Problem (found in DECISION 58): `night_loop` only rolled when the 30 s tick landed inside the exact 08:00 minute. The box was paused at 8 AM on Nov 15 and 16, so night 2026-11-14-1640 never closed.
- Rule (`Desk.rollover_due` / `rollover_if_due`, main.py): roll when local time >= today's `desk.report_time` AND meta `last_rollover` != today. Checked on startup (before `night_start`) and on every 30 s tick. `last_rollover` is written first, so at most one roll per local day even if `close_night` throws (then `ended_at` is set directly and the error logged). Fresh start guard: if the current night already started today at/after report_time, it is marked as today's rollover and not rolled again.
- First catch-up ran on the 7:23:55 PM CT restart, as approved: closed 2026-11-14-1640 (Nov 14 4:40 PM → Nov 16 7:23 PM CT; `logs/reports/2026-11-14-1640.json`, 15 closed PAPER trades −$170.93, 1,285 coins, no errors) and started night **2026-11-16-1923** (7:23:55 PM CT). The new night also resets GMGN tickets-per-night and Strategy B's daily loss stop, and sent the usual once-per-night ticket push. No settings or modes changed.
- Morning report: the night-report routine runs `report.py` with no night id. It used to take the newest closed night, so if it ran before the desk rolled (8:00 race, or box paused at 8 AM) it would re-report an older night. `report.default_night` now reports the still-open current night when a rollover is due but hasn't happened (that night is "last night"), otherwise the newest closed night. After the roll it resolves to the same night, so the routine reports each night once. Sending once per day is still up to the routine (it writes `last_report_url`). The desk sends no report itself.
- Tests: `tests/test_night_rollover.py` (missed 8 AM rolls once, nothing before 08:00, late wake catches up, fresh start after 08:00 doesn't roll, close_night failure still rolls once, report default night across the 8 AM race).

## 60. Strategy B live view (owner 2026-11-16 7:32 PM CT, t270u: "a tab on the b strategy that also has all the live stuff for the scanners and filters")
- Strat B tab now has Overview | Live (`#/stratb`, `#/stratblive`; Live polls every 15s like A's Live). Read-only, PAPER-labelled. Shell desk-shell-v20, assets ?v=20.
- New publisher file `strat_b_live.json` (`strat_b.live_feed`): `items` = newest 100 Strategy B evaluations of the current night, newest first; `watchlist` = coins with status watching (time left, minutes since graduation, price/high/low, pass count, `away` = how far from the buy zone in words); `counts` (watching, bought/skipped/expired tonight, evaluations tonight).
- Every item and watch row carries all 10 B checks with pass/fail, the actual value and the bar: top 10 ≤ 20%, holders ≥ 75, bundled ≤ 5%, ≥ 60m since graduation, mc $30K–$1M, 24h vol/mc ≥ 0.5x, liquidity ≥ $10K (and $50 ≤ 2% of pool), dip 15–40% from the high, bounce ≥ +5%, 5m buys ≥ sells. Bars come from live strat_b.* settings. Before the first price the 6 market checks show "no price yet" (—).
- `strat_b.checks()` is display only; buying still goes through `entry_check` (unchanged). A test checks they agree.
- New table `strat_b_evals` (one row per offer, per change of B's verdict, per buy, per expiry; not per 10s tick) and `strat_b_watch.checks/eval_at/evals` (latest checks every tick). The `wait` lines in logs/strat_b.log now end with the compact checks (`top10=15%✓ … buys=10/5✓`).
- Strategy A, the shared scanner/auditor feed and all settings unchanged. Tests `tests/test_strat_b_live.py`.

## 61. Surge counts only at +25% above entry (owner 2026-11-16 7:38 PM CT: "Yes, only count a surge once it's up 25% from my buy price")
- Why: Share Bags #33 armed the surge rule on a +26.9% rise inside the window while only ~+8.9% above entry; the +5% floor (DECISION 56) then sold it at +1.3% (+$0.67) while the paper trade rode it to +49%.
- Rule (`surge_check`, new setting `risk.surge_min_entry_pct` = 25, 0 = off, not protected, change_log source 'chat: owner asked 2026-11-16 7:38 PM CT'): a surge arms only when BOTH (a) price >= entry x 1.25 and (b) the existing speed rule holds (+surge_min_pct 25% off the low in surge_window_s 180s, or +20% off the low in 60s). Once armed, nothing changed: sell at the +5% floor or an 8% drop from the post-surge peak, whichever first; under entry -> stand down to the normal stop/trail. `no_surge` opt-out unchanged. No other settings changed.
- Tests: `tests/test_gmgn_surge.py` (Share Bags shape doesn't arm, +24.9% doesn't arm / +25% does, slow grind past +25% never arms, gate 0 restores old arming); older floor/top unit tests now set the gate to 0 or use +30%+ peaks.

## 62. Live phone notifications in the app (owner 2026-11-16 8:17 PM CT: "Can you make my app give me live notifications because telegram kinda sucks to read"; pipeline added 8:17 PM CT)
- Supersedes the "no web push" part of DECISION 5. Telegram is unchanged and stays the backup: every Telegram push is mirrored (`Telegram.push` -> `webpush.mirror`) before it is sent, as a background task, so trading never waits on it.
- Desk -> phone: standard Web Push, no new library. `webpush.py` does VAPID (ES256 JWT, `.secrets/VAPID_PRIVATE_KEY`, generated today per SETUP.md, write-only) and RFC 8291 aes128gcm with `cryptography`; checked against `http_ece` both ways. 404/410 from the push service deletes the subscription. `Topic` header = tag so an offline phone only gets the newest per coin.
- Phone -> desk (Plan B has no endpoint): the app POSTs an AES-GCM envelope (same APP_TOKEN key as the data) to a private ntfy.sh topic named by a hash of APP_TOKEN; the desk streams it (`webpush` job, resumes from meta `webpush_ntfy_since`). Accepted only if it decrypts with APP_TOKEN, is < 15 min old, and the endpoint is https on a known push service (FCM/Mozilla/Windows/Apple). Messages: sub (with category prefs), test, unsub. The relay never carries trades.
- What buzzes (short title / body, e.g. "Sold The Nietzschean Gorilla +$22.97 (+45%)" / "trail · held 8s"): trades = GMGN buys, "Selling X" on desk-fired exits (surge/floor/trail), final sells with P&L and exit (trail, floor, surge top, stop, GMGN exit); approvals = $300K market-cap asks with Approve / Skip buttons (they open t.me/<bot>?start=gmap_<id>/gmsk_<id>, so the owner-chat lock still confirms it; tap = FOMO coin_url from links.py, r=savipww); alerts = gmgn_alert, auditor/risk alerts, refusals (e.g. out of SOL), stale app data; stratb = Strategy B paper buys/sells labelled "B PAPER"; paper = FOMO paper test (off by default). Not sent: "Buying…", grace-over, "confirming result", ticket bumps.
- Pipeline (owner add-on): one notification per coin (tag pipe-<mint>) updated in place: "Scanner passed X" -> "Auditor passed X" -> "X · Analyst score N: buy/skip". Only the buy verdict re-alerts; the other steps update silently. Works even when Telegram stage pings are off. Tonight's rate: ~4-6 coins/h (6 in the first hour of tonight's night, 24 in the last 6 h, 39 in 24 h; busiest clock hour 7).
- App (shell v21): big "Turn on notifications" card on Home until on; Account -> Notifications with per-category toggles (trades, approvals, alerts, pipeline [on, one tap off], Strategy B, FOMO paper test), Send a test, Turn off. `state.json.push` (public VAPID key + subscription ids) lets the app show "The desk has this phone ✓". The service worker shows pushes and handles taps/actions.
- No trading settings or modes changed. Tests `tests/test_webpush.py`.

## 63. Strategy B: coins up to 7 days old, its own older-coin feed, 15-minute wait tonight only (owner 2026-11-16 8:23 PM CT, t278u/t279u: "Yeah thats fine let's lower the wait to 15 mins for the night")
PAPER only. Strategy A, the shared scanner/auditor and every A setting are unchanged.
- No 3-hour cutoff: `strat_b.watch_max_min` 180 → 10080 (spec max raised to 10080) and new `strat_b.max_age_min` = 10080. A watched coin expires when it has been watched 7 days or when it turns 7 days old ("older than 7d"), whichever comes first. New `strat_b.max_watch` (90) caps the watchlist. Watched coins are re-priced every `strat_b.watch_poll_s` (30s) instead of every 10s, so 90 coins cost about 6 DexScreener calls/min (open paper positions still every 10s).
- B's own feed `strat_b_feed.py` (`BFeed`, job + heartbeat `strat_b_feed`): every `strat_b.feed_poll_s` (300s) it pulls GeckoTerminal Solana trending pools (1h p1–2, 6h p1–2, 24h p1–3; 7 calls 2.5s apart, about 1.4/min), keeps SOL-quoted graduated pools (sources.GRADUATED_DEX_GECKO) aged `strat_b.feed_min_age_min` (60m) to 7d, market cap $30K–$1M, reserve ≥ $10K, not excluded/known, and checks at most `strat_b.feed_checks_per_poll` (3) new coins per poll, read-only: getAccountInfo (mint and freeze authority must be revoked, supply), RugCheck report (bundled % via auditor.bundle_stats, AMM accounts), holders.fetch_accounts → top10_pct / holder_count. Then StratB.offer runs B's normal static checks. A coin whose RPC/RugCheck data can't be read is retried after 6h (in memory). The pool's creation time is used as "since graduation".
- Source: `strat_b_watch.source` / `strat_b_evals.source` ("desk scanner · pump.fun", "gecko trending 6h · 5.2d old", …). The Live view shows it without a web change: each reason starts "via <source> · …" and each row's check list starts with a "Via" chip. `strat_b_live.json` adds `source` per item/watch row, `counts.olderFeedWatching`, and `tonightOnly`.
- Tonight-only wait: `StratB.set_tonight()` set `strat_b.min_age_min` 60 → 15 (change_log source 'chat: owner asked 2026-11-16 8:23 PM CT') and stored meta `strat_b_tonight` = [{key, revert_to 60, night, until = Nov 17 8:00 AM CT}]. `StratB.apply_reverts()` runs at the start of every B tick (10s): once the desk night changes (8 AM rollover, or the catch-up rollover on a late start, DECISION 59) or the deadline passes, it sets the value back with change_log source 'auto revert at night rollover (chat: owner asked 2026-11-16 8:23 PM CT: tonight only)' and clears the meta.
- Tests `tests/test_strat_b_feed.py`.

## 64. Strategy A waits for a small dip before buying (owner 2026-11-16 8:40–8:42 PM CT, t284u/t285u/t286u/t287u: "wait for the coin to fall just a little bit before it buys"; then "buy instantly, no bounce"; then "if no dip in a few seconds, buy anyway")
Strategy B is unchanged. Modes, caps, exits, sizes and the $300K approval gate are unchanged. FOMO still has no order API: no FOMO order is ever placed; every FOMO link still goes through links.py with r=savipww.

- After the Analyst approves a coin, and after the existing >$300K LIVE approval gate if it applied, Strategy A no longer buys immediately. The coin goes into a pending dip-watch (`gmgn_dips`, job + heartbeat `gmgn_dip`). The high since approval is tracked off the same fast pool price source as the exit watchers (`risk.price_source` pool / `risk.fast_poll_s` 1.5s; DexScreener fallback).
- Buy instantly on the first tick that is >= `gmgn.dip_pct` (5) below that high. No bounce. If the coin has already gapped more than 10% below the high, it is still bought (logged as `dip_gap`, "still buying") — the 5–10% band is the target, not a skip.
- If no dip comes within `gmgn.dip_timeout_s` (30 seconds), buy at market anyway. The goal is only to try for a slightly lower entry. A dip on the timeout tick wins over the timeout. The only skip is a fresh safety problem (pool pulled, liquidity under the scanner floor or down more than `executor.recheck_liq_drop_pct`, or the coin is already open).
- Settings (not protected, hot-loaded; change_log source 'chat: owner asked 2026-11-16 8:42 PM CT'): `gmgn.dip_enabled` true, `gmgn.dip_pct` 5, `gmgn.dip_timeout_s` 30. There is no bounce setting.
- FOMO PAPER auto-fills wait for the same watch and fill at the same trigger price, so tonight's paper test stays comparable. Bypass/override paper fills and FOMO LIVE tap-to-trade buttons are unchanged (not automatic buys).
- Live view (shell desk-shell-v22, assets ?v=22): `feed.json` adds `dips` (coins currently watching) and `stage: dip` items (`watching` / `dip_buy` / `timeout_buy` / `skipped`). Pipeline push + Telegram (`stage_dip`, same `pipe-<mint>` tag) on enter-watch, dip-buy, timeout-buy, or skip. Outcomes are also in gmgn.log.
- Tests: `tests/test_gmgn_dip.py`.

## 65. Extend the GMGN grace stop while price is still below the normal stop (owner 2026-11-16 9:27 PM CT, t292u: "If the loss is below 20 percent but hasn't hit 50 yet during the grace period make the grace period last until after it comes back above 20")
The 5-minute grace window and the -50% steep stop are unchanged. Modes, caps, buy size, the $300K approval gate, Strategy B, the dip-buy, and FOMO are unchanged. FOMO still has no order API: no FOMO order is ever placed; every FOMO link still goes through links.py with r=savipww. The owner's declined manual-buy protection is left alone.

- When `risk.grace_min` ends, if the fast pool price (same feed as the exit watchers) is below entry × (1 − `risk.stop_pct`/100) but still above the grace stop, the desk does **not** swap to the normal -`risk.stop_pct` GMGN stop. The -`risk.grace_stop_pct` stop stays on (on GMGN and in the backup desk stop) until price recovers.
- Recovery, with hysteresis so a swap does not fire on the same dip: price must reach at least 1% above the normal stop (with stop 20%, that is ≥ entry × 0.81, i.e. -19%) for 2 consecutive ticks at least 1 second apart. A dip in between resets the count. If at the first check after the window the price is already at or above that level, the swap happens immediately (no extra wait). If there is no fresh price, grace stays on.
- If it never recovers, the -50% grace stop is still the protection. The desk will not attach a -20% stop that would sell immediately.
- Setting (not protected, hot-loaded; change_log source 'chat: owner asked 2026-11-16 9:27 PM CT'): `risk.grace_extend_below_stop` true. Applies to currently open desk positions still in the grace phase, and to PAPER so the paper test stays comparable.
- The 1.5s `gmgn_trail` loop runs the check (fast pool price); `watch_once` is the fallback / test path.
- App: open-position `exits.label` becomes `grace extended · stop -50% until back above -20%`, and `exits.graceExtended` is true. Logged once per order as `grace extended` in gmgn.log.
- Tests: `tests/test_gmgn_grace.py`.

## 66. Strategy B LIVE; Strategy A back to PAPER (owner 2026-11-17 8:13 AM CT, t304u: "Switch strat a to paper and strat b to live"; sizing 8:15 AM CT, t305u: "Make the trading amount rules the same as strat a until the wallet can cover the correct amount")
Replaces the PAPER-ONLY lock of DECISION 54 deliberately, by the owner's command.
- Modes: `gmgn.mode` LIVE → PAPER (force, by the main agent). `strat_b.mode` is now a PROTECTED choice PAPER|LIVE (force-only, like `gmgn.mode`), set to LIVE with change_log source 'chat: owner asked strat b live 2026-11-17 8:13 AM CT'. `desk.mode` (FOMO) stays PAPER; FOMO still has no order API and no FOMO order is ever placed; every FOMO link still goes through links.py with r=savipww. No cap, Strategy A or other setting changed.
- Telegram (owner chat only): `b status`, `b live` then `B LIVE` (capitals, within 5 minutes), `b paper`. `/halt` also stops B LIVE buys. `status` adds a Strategy B line.
- Entry unchanged: B's filters, triggers, watch logic and its older-coin feed are exactly as before (strat_b.py). Only the fill/exit path changes in LIVE, in the new module `strat_b_live.py` (the only B code that can place an order; strat_b.py still imports no GMGN / wallet / secrets code; `tests/test_strat_b.py` checks both).
- LIVE buy: signed GMGN swap from the main wallet through Strategy A's client (ctx.gmgn), body = A's `buy_body` with B's own `condition_orders` = one server-side `loss_stop` at -`strat_b.stop_pct` (35%), so the stop survives box sleep. After the fill, as in DECISION 39, the desk waits for GMGN's strategy; if none shows it creates a limit stop_loss at entry × 0.65 for the held amount, else market-sells and alerts.
- Sizing (t305u): `strat_b.trade_usd` ($50) when free SOL covers it with the 1% GMGN fee, priority/tip and token-account rent; otherwise Strategy A's own rule through the same function (`pick_size(..., top_usd=strat_b.trade_usd)`): A's fallback `gmgn.fallback_trade_usd` ($20), else the largest amount free SOL covers in whole cents, down to `gmgn.min_trade_usd` ($1); below that the buy is refused with the reason. Free SOL = wallet − `gmgn.sol_reserve` (0.02 SOL) − SOL in flight (A LIVE + B LIVE). Never above A's per-trade cap `gmgn.cap_trade_usd`; B's pool limit (`strat_b.max_pool_pct` of liquidity) also caps it.
- Caps are global across A LIVE and B LIVE (same wallet): `check_caps` (per-trade cap, `gmgn.max_open`, night tickets / spend) counts B LIVE rows, and A's open/committed/night counts include them. B's own `strat_b.max_open` also applies. A LIVE never buys a mint B LIVE holds and vice versa.
- Refusals: recorded as `strat_b_orders` rows (mode LIVE, status refused, reason in `note`), logged in strat_b.log, the coin keeps being watched and is retried at most every 10 min while it stays in the buy zone. Push "B LIVE · Skipped <coin>: <reason>" on each coin's first refusal tonight; wallet/cap refusals push at most once per 30 min.
- Exits: the desk runs B's other exits itself with real guarded market sells (never a 2nd sell, never with 0 held; GMGN strategies cancelled after our sell): take profit +100%, spike, trail (arms +50%, 20% drop), 24h time stop, liquidity pulled, and a backup -35% stop. Prices: A's real-time pool price feed (fastprice) in a new `strat_b_live` loop (heartbeat `strat_b_live`, ~1.5 s), plus B's DexScreener tick. Closes and P&L only from the chain (DECISION 42): SOL in (fees + rent) vs SOL out.
- Daily loss stop in LIVE uses real B LIVE P&L tonight only (limit unchanged: `strat_b.bankroll_usd` × `strat_b.daily_loss_stop_pct` = $100); paper losses don't count.
- Records: `strat_b_orders` gains `mode` (existing rows → PAPER), lamports, sol_usd, order ids, tx, sell tx, entry_src, note, confirm_push_at. Paper bankroll / P&L use mode='PAPER' only; the open paper position (#2 GTA) keeps running and closes as paper. `strat_b.json` v2: top-level paper fields unchanged (paper rows only), `mode`/`label` follow the current mode ("B LIVE"), real results under `live` (no order ids or tx). strat_b_report.py prints B LIVE first, then B PAPER. Pushes say "B LIVE · …" (webpush titles "B LIVE · …"); paper ones stay "B PAPER".
- App: Strat B tab shows a B LIVE card (real P&L, free SOL, last skip), B LIVE positions/trades, then the paper section; badges per row. Shell desk-shell-v23, assets ?v=23.
- Tests `tests/test_strat_b_live_mode.py` (13) + updated `tests/test_strat_b.py`.

## 67. App: Strategy B takes Home and Live; Strategy A moves to the old Strat B tab (owner 2026-11-17 9:21 AM CT, t308u: "can you make strat b appear where a is and swap a to b's spot")
- UI-only. No desk settings, modes, caps, or trading code changed. Data shapes unchanged. Secrets never in web/.
- Home (`#/home`) is now Strategy B · LIVE: the B LIVE card (real P&L, free SOL, last skip), B LIVE positions and trades, plus the shared GMGN wallet card. A Paper sub-tab (`#/bpaper`) holds B's simulated bankroll, paper positions/trades and rules.
- The bottom Live tab (`#/live`) is now B's live feed: watchlist and evaluations (what was `#/stratblive`).
- The old Strat B tab is now "Strat A" with a PAPER badge (`#/strata`). It holds Strategy A's previous Home views (Real money | Paper: GMGN / FOMO paper P&L, positions, tonight, agents) and A's Live | Scores | Scanner (`#/alive`, `#/scores`, `#/scanner`).
- Redirects so old hashes still open something sensible: `#/stratb` → `#/home`, `#/stratblive` → `#/live`, `#/paper` → `#/apaper` (A's paper, same content as before). Header badge shows "B LIVE" while Strategy B is live.
- Approval buttons and FOMO links are unchanged (still from published data / links.py with r=savipww). Shell desk-shell-v24, assets ?v=24.

## 68. Strategy A PAPER bankroll $2,500 with $500 buys (owner 2026-11-17 10:20 AM CT, t310u: "Give strat a a paper currency of 2500 with 500 dollar buys")
- Why: A is PAPER, but it was still sizing against the real wallet. After the wallet ran down, every A paper buy was refused ("free 0.0000 SOL can't cover even $1.00", orders 46–51). Paper trading should not depend on real SOL.
- New non-protected settings: `gmgn.paper_bankroll_usd` = 2500, `gmgn.paper_trade_usd` = 500 (source `chat: owner asked A paper $2500 bankroll $500 buys 2026-11-17 10:20 AM CT`). Not in PROTECTED / CAP_KEYS. `gmgn.cap_trade_usd` stays $50 for real money and Strategy B.
- Sizing (PAPER only): buy $500 when paper free covers it, else whatever is left down to `gmgn.min_trade_usd` ($1), else refuse with a paper-balance reason. Paper free = bankroll + realized A paper P&L since the reset − open A paper tickets. A thin pool can still cap the ticket (2% of liquidity), same idea as live, but never the $50 live cap.
- The paper bankroll starts fresh now (`meta gmgn_paper_bankroll_since`). Old PAPER rows from before the reset do not count against it.
- Caps: A paper uses `gmgn.max_open` (4) and `gmgn.max_tickets_night` (4) counted only on `gmgn_orders` mode=PAPER, so they do not take Strategy B LIVE slots or tickets, and B LIVE rows do not take A's paper slots. No per-trade $50 cap, no $200 night-spend cap, no wallet or reserve check on paper buys. Live / B sizing, modes, and cap values are unchanged.
- App: Strat A tab shows the paper bankroll, free cash, P&L, open amount and $500 buy size (shell desk-shell-v25, assets ?v=25). Same numbers in `state.json` / `account.json` under `gmgn.paper`, and in the Telegram GMGN status line when A is PAPER.
- Tests: `tests/test_gmgn_paper_bankroll.py`.

## 69. B LIVE daily loss $20, pickier A filters, B watch hygiene, A trail 10% (owner 2026-11-17 5:23 PM CT, t316u: "Let's do everything you said")
- Why: B LIVE was down about $28 of the ~$97 funded wallet. The daily loss stop was still $100 (10% of the $1,000 paper bankroll), more than the whole wallet. A's four paper buys were ~10-minute-old thin-pool coins (two rugged in 3 minutes). B's watchlist was holding dead coins and the feed hit GeckoTerminal 429s 192 times, which is part of why B only bought twice. A's trail is 20%, and the box-sleep problem is unchanged (Hetzner move still waiting).
- B LIVE daily loss: new non-protected setting `strat_b.live_daily_loss_usd` = 20 (source `chat: owner asked 2026-11-17 5:23 PM CT`). In LIVE mode the stop is that dollar amount, capped at the cached wallet USD when we have one, and it still counts only real (mode=LIVE) night P&L. PAPER is unchanged: `strat_b.bankroll_usd` × `strat_b.daily_loss_stop_pct` / 100 ($100 on the $1,000 paper bankroll). Modes and other caps were not changed.
- A scanner filters (the feed that supplies Strategy A): `scanner.min_liquidity_usd` 2500 → 25000, `scanner.min_pool_age_min` 10 → 60 (same source). `auditor.min_pool_age_min` and B's own filters are unchanged.
- B watchlist hygiene: a watch whose market cap stays under `strat_b.min_mc_usd` ($30K) for 10 straight evals or 15 minutes is expired; a watch with a price and $0 liquidity is expired immediately. Missing price / missing liquidity is not treated as $0. A recovered market cap resets the streak. Columns `strat_b_watch.under_floor_evals` / `under_floor_since`.
- B feed 429 backoff: exponential with ±25% jitter (60s, 120s, …, 15 min cap), honors `Retry-After` (delta-seconds or HTTP-date, up to 30 min), and does not poll GeckoTerminal during the backoff. A clean round resets the streak.
- A trailing sell: `risk.trail_pct` 20 → 10 (same source). Trail still starts at +20% (`risk.trail_start_pct` unchanged). Strategy B's own `strat_b.trail_pct` stays 20%.
- Tests: `tests/test_strat_b.py`, `tests/test_strat_b_live_mode.py`, `tests/test_strat_b_feed.py` (401 passing).

## 70. Desk moved to an always-on DigitalOcean droplet (owner 2026-11-17 5:41 PM CT, t324u: droplet 165.227.20.72 after asking to "move the desk to the server")
- Why: the box sleeps when idle, so only GMGN's server-side stop could fire overnight (DECISION 69 review). The droplet (Ubuntu 24.04, 1 GB RAM + 2 GB swap, SFO2, TZ America/Chicago) never sleeps.
- Layout: `/opt/desk`, Python 3.13 (deadsnakes) venv rebuilt from requirements.txt, `.secrets` copied as 700/600, gh auth copied to `/root/.config/gh` (600) for the publisher's gist + workflow dispatch. No inbound port: ufw allows OpenSSH only (the app is GitHub Pages; the publisher pushes out).
- Service: systemd `desk.service` (copy in `deploy/desk.service`) runs `desk_keepalive.sh`, which calls the idempotent `run_desk.sh` every 30s (starts main.py and supervise_desk.py if missing). Enabled at boot, Restart=always, `systemctl stop desk` SIGTERMs the whole cgroup. HOME/GH_CONFIG_DIR are set in the unit so gh finds its auth.
- Never two desks: `run_desk.sh` now takes `logs/start.lock` (flock, closed in children) so supervisor / respawn helper / keepalive can't race; and on the old box a `.moved_to_droplet` marker (gitignored, not copied) makes `run_desk.sh` refuse to start, so the box desk-health routine and leftover helpers can't start a second desk on the same wallet / Telegram poller. Rolling back = `systemctl disable --now desk` on the droplet, then `rm .moved_to_droplet && ./run_desk.sh` on the box.
- Cutover 5:48 PM CT: box supervisor and main.py stopped cleanly, desk.db snapshotted with sqlite backup (integrity ok, JALU open LIVE carried over), data/ and logs/ history copied, service started 5:48:57 PM CT. Modes, caps and settings unchanged.
- Box access: `./do_ssh.sh '<cmd>'` uses a dedicated key `~/.ssh/desk_do` (root password login left as the owner set it).

## 71. Strategy A and Strategy B both LIVE, wallet split 50/50; app shows LIVE only (owner 2026-11-17 5:59 PM CT, t328: "Split the wallet 50/50 and run both live"; app: "remove the paper trades entirely for now")
- Modes: `gmgn.mode` PAPER → LIVE (force, source `chat: owner asked 2026-11-17 5:59 PM CT`). `strat_b.mode` stays LIVE. `desk.mode` (FOMO) stays PAPER. The GMGN live gate (2h of GMGN PAPER, `gmgn.live_gate_h`) was checked: A had been PAPER since 8:13 AM CT. A's paper bankroll settings are kept for whenever A goes back to PAPER.
- New PROTECTED settings (force only, not hot-loaded; applied with a restart): `gmgn.live_wallet_share_pct` and `strat_b.live_wallet_share_pct` (default 100 = whole wallet, the old behaviour; set to 50 / 50), `strat_b.live_max_open` and `strat_b.live_max_tickets_night` (default 4 / 4).
- Budget formula (code: `budget_lamports` in agents/gmgn_trader.py), all in lamports:
  - free = wallet SOL − `gmgn.sol_reserve` − SOL promised to in-flight buys (A + B, submitting/pending)
  - open(X) = swap input of strategy X's LIVE positions still held or being bought/sold
  - total = free + open(A) + open(B)
  - budget(X) = share(X)% × total − open(X)
  - avail(X) = max(0, min(budget(X), free))
  - `pick_size` sizes against avail(X): $50 (A: `gmgn.cap_trade_usd`; B: `strat_b.trade_usd`, never over the cap), else $20, else whatever avail covers down to $1, else skip. `check_caps(..., strategy=X)` refuses a buy whose full cost (input + 1% GMGN fee + priority/tip + rent) is over avail(X). The old global wallet + reserve check still runs.
  - Cost basis, not market value: a losing position holds its slot of the share until it is sold. Sale proceeds go back to free SOL and are split again.
- Separate caps: A LIVE uses `gmgn.max_open` / `gmgn.max_tickets_night`, counting only `gmgn_orders`. B LIVE uses `strat_b.live_max_open` / `strat_b.live_max_tickets_night`, counting only `strat_b_orders` mode=LIVE. Both use the per-trade cap `gmgn.cap_trade_usd`. B's `can_buy` uses `strat_b.live_max_open` in LIVE mode (`strat_b.max_open` stays for B PAPER). Calls that don't pass `strategy` keep the old global behaviour.
- One coin, one strategy: both strategies sell 100% of the wallet's balance of a mint, so the same coin is blocked, not shared. A skips a coin that B LIVE holds (pending/open/exiting) and B refuses a coin A LIVE holds. Both re-check `mint_held_live` right before the order row is inserted, with no await in between, so a coin can't slip through while the other strategy's buy is in flight. Exit watchers stay separate: A's (watch/surge/trail/grace) read only `gmgn_orders`, and B's (`strat_b_live.once`) read only `strat_b_orders`. Tests cover both directions.
- App (shell desk-shell-v26, assets ?v=26): Home shows "LIVE · Strategy A + B": the shared wallet card (share split, reserve), an A LIVE card (P&L, share, budget, open count, positions, trades) and the B LIVE card (now with share and budget). The Strat A tab is A LIVE plus the evaluation feed, with a LIVE tab badge. The header badge reads "A+B LIVE". Paper views are hidden, and no data was deleted: the B Paper and A Paper toggles, paper bankroll cards, paper positions and trades, the FOMO practice balance, paper override buttons, the paper position on the coin page, and paper trades in Activity. Old routes `#/apaper`, `#/paper` → `#/strata`, and `#/bpaper`, `#/stratb` → `#/home`, `#/stratblive` → `#/live`. Publisher adds `gmgn.budgets` {A, B}, `gmgn.liveOrders`, `gmgn.bMode` and `strat_b live.budget`.
- Telegram: `gmgn` status shows A's share and free budget ("A's 50% share: … SOL ($…) free of … SOL → next $…"). `b status` shows B's share, budget and open count.
- Tests: `tests/test_wallet_split.py` (budget split, sizing, own caps, same-coin block both ways plus the last-line check, and sell isolation both ways). Updated `tests/test_strat_b_live_mode.py` so A's open slots no longer block B.

## 72. Profit reset for A and B; Wallet / Strat A / Strat B tabs with a feed per strategy (owner 2026-11-17 6:20 PM CT, t338u: "reset the profits for a and b and put the balances in separate tabs with the main wallet in its own tab and on the live feed stuff make it for each strategy")
- Profit reset is a display baseline only. New settings `gmgn.pnl_since` (A) and `strat_b.pnl_since` (B) hold a UTC ISO time ("" = none), validated as ISO and not protected. Both set to `2026-11-18T00:20:00Z` (= Nov 17 6:20 PM CT), source `chat: owner asked 2026-11-17 6:20 PM CT`. `pnl_since.py` holds the helpers.
- What counts: realized P&L, W/L, closed counts and "profit since" use only trades whose close time is ≥ the baseline. A position opened before the reset counts when it closes after it. Open positions don't count until they close. GrokChan (#59) sold at 6:19:59 PM CT (+$7.30), one second before the reset, so it isn't counted. It stays in history under "Before the reset".
- A: `gmgn.pnl_since`, when set, replaces the old meta baseline and manual seed (`gmgn_pnl_baseline_ts`, `gmgn_pnl_manual_usd` $64.54, `gmgn_pnl_manual_wins` 2). The meta rows are kept, not deleted. trades.json `real` (pnlAllUsd / pnlTonightUsd / wins / losses / closedTonight, plus a new `since`) and state.json `realPnl` follow it. B: `strat_b.live_card` stats (pnlAllUsd, pnlTonightUsd, closedAll, wins, losses, winRate, profitFactor) use only closes after the baseline. `closed` lists those trades, and the new `earlier` / `earlierClosed` / `earlierPnlUsd` fields cover trades before it.
- Untouched: daily-loss stops (`StratB.night_pnl`, A's night usage), caps, sizing, modes and every trading setting. No rows deleted.
- Telegram: `gmgn status` (LIVE) starts with "A LIVE profit since Nov 17 6:20 PM CT: $… · n closed · nW nL". `b status` says "B LIVE n closed since Nov 17 6:20 PM CT, P&L …".
- Publisher: new `strat_feed.json`: `pnl.A` / `pnl.B` (pnlUsd, pnlTonightUsd, closed, wins, losses, winRatePct, since, sinceLabel), plus `A` and `B` event lists covering the last 24 h, newest first, max 80. A's list has GMGN LIVE buys, sells/stops, refusals, failures and market-cap approvals. B's list has B LIVE buys, sells/stops, refusals and failures. There are no order ids, tx or bodies.
- App (shell desk-shell-v27, assets ?v=27): bottom tabs Wallet · Strat A · Strat B · Agents · Activity · Account. Home is folded into **Wallet**: main wallet SOL/USD, SOL price, reserve, the A/B split bar with each half's value, free budget and in-positions, and one card per strategy (profit since reset, free, open, W/L) linking to its tab. **Strat A**: Overview (profit since reset, share/free/open, positions, trades since reset, "Before the reset" collapsed) · Feed (A's buys/exits/skips/approvals, then the desk pipeline live feed, dips and tonight's counts) · Scores · Scanner. **Strat B**: Overview (same layout as A) · Feed (B's buys/exits/skips, watchlist, evaluations). Paper stays hidden. Old routes: `#/home` → `#/wallet`, `#/live` and `#/stratblive` → `#/bfeed`, `#/bpaper` → `#/stratb`, `#/paper` and `#/apaper` → `#/strata`. Pipeline push notifications open `#/alive` (A's feed). The default push route is `#/wallet`.
- Tests: `tests/test_pnl_since.py`.
- Follow-up (owner approved 2026-11-17 6:40 PM CT, t340u "Yeah thats cool"; shell v28, UI only): both strategy cards are labelled "Strategy A/B · real money · GMGN". Strat B gets three sub-tabs like A: Overview · Feed (B's buys, exits, skips) · Watchlist (`#/bwatch`: watchlist + evaluations, moved out of Feed). Old `#/live` and `#/stratblive` now open `#/bwatch` (the old Live tab was B's watchlist view).

## 73. Notifications say which strategy did what; wallet budgets follow the wallet (owner 2026-11-17 7:03 PM CT, t351u: "make the notifications from desk and telegram specify what strat is doing what"; 7:04 PM: the action goes in the headline; 7:06 PM: "make sure the strategies' usable amounts update as the wallet amount changes")
- `notif_label.py` (text only). Every Telegram push's first line, and every phone push title, is a headline naming who acted: "Strat A bought Sloth $50.00", "Strat B sold JALU −$9.85 (−19%) (stop)", "Strat A selling Sloth (trail)", "Strat A set a -50% stop on Foo", "Strat A · your manual buy Sloth is now protected", "Strat A skipped Rock", "Strat A · approve Sloth?", "Strat A ⚠️ alert · nema", "Strat A · Sloth passed the scanner". Pipeline stages (scanner / auditor / analyst / dip / re-entry) are A's. Everything else gets "Desk · …".
- The original message, minus its old "GMGN · " / "B LIVE · " / "PAPER · " tags, follows the headline on Telegram, so no details are lost. Applied once, in `Telegram.push`, and idempotent. The `pushes` table stores the labelled text.
- Phone: `webpush.labelled()` wraps `render()` and swaps in the headline title. Approvals keep their Approve/Skip buttons and the MC line moves to the body. Strategy messages `render()` used to drop now push too: adopted manual buys, "Buying…" and "sold, confirming result" (both quiet, same tag, so they're replaced in place), stops created, B's buy problems. Desk info that wasn't pushed before still isn't.
- Wallet freshness: any LIVE fill or exit (A or B, via `Ctx.event('fill'|'exit')`) marks the cached wallet stale. A publisher side task (`Publisher.wallet_loop`, checks every 5 s, runs during publishes too) re-reads the wallet and SOL price whenever the cache is stale or ≥ 30 s old (`Publisher.WALLET_REFRESH_S`), and publishes when either changed. Every publish also refreshes if ≥ 30 s old. Every LIVE buy was already sized from a fresh `/v1/user/info` read (unchanged). Budgets (DECISION 71) recompute from that read. App v29: Wallet / Strat A / Strat B poll every 15 s.
- Tests: `tests/test_notif_label.py`, `tests/test_wallet_refresh.py`.

## 74. Bypass layer 8 when getProgramAccounts is down (owner 2026-11-17 7:15 PM CT, t359u: "set it to where it bypasses layer 8 if it goes down")
- Why: the public Solana RPC rate-limits `getProgramAccounts` (429 / timeout). The auditor used that to mark every coin red at layer 8 ("no data: delegations") and sent "layer 8 down, every coin is red until rpc_url changes". Owner asked to keep buying when only that lookup is down.
- New non-protected setting `auditor.l8_bypass_on_down` = true (aliases "l8 bypass", "layer 8 bypass", "bypass layer 8"; source `chat: owner asked 2026-11-17 7:15 PM CT`). When layer 8 fails only because accounts are unavailable (RPC error / 429 / timeout, the "no data: delegations" path), it is a pass-with-warning: layer value `L8=skipped (rpc down)`, `l8_skipped=true` on the audit row, not counted in `layer8_kills`. A real delegation finding (max delegate ≥ `auditor.max_delegated_accounts`) still rejects. Layers 1–7, including layer 4's top-10 check, stay strict: a down RPC still fails layer 4 with "no data: top 10" (the same GPA fetch is cached and used by both layers, so a total outage still stops the coin at layer 4).
- Notifications: stage ping and buy pushes (Telegram + phone) append `⚠️ L8 skipped (rpc down)` on coins that passed with L8 skipped. `notif_label` puts it on the headline too ("Strat A · SkipCoin passed the audit · ⚠️ L8 skipped (rpc down)"). The app live feed shows the same note as the auditor-pass reason / layerName; Strat A's buy sub-line does too. The audit JSON records `l8_skipped` and the layer-8 value, so the audit page lists it.
- `gpa_down` alert: when the bypass is on, the text is "layer 8 is down and being skipped", not "every coin is red until rpc_url changes". The stale "PAPER · " prefix (from `ctx.prefix()` / FOMO `desk.mode`) is gone; `notif_label` headlines it "Desk · …".
- Retries: `Net.token_accounts_for_mint` retries getProgramAccounts 429s twice with 1s then 3s backoff (layer 4, layer 8, scanner top-10, B feed) before the caller declares no data. Other RPC errors are not retried here.
- Tests: `tests/test_auditor_l8_bypass.py`.

## 75. Live app data straight from the droplet, Pages kept as backup (owner 2026-11-17 7:23 PM CT, t363u: "Go ahead and set that up as long as it has no negative affects")
- Why: Pages data is 15–60 s+ behind (gist → Actions deploy → CDN). The owner asked for the fastest possible app updates.
- `live_api.py`: the desk keeps an in-memory snapshot of the app's core files (same `Builder` as the publisher: state, chart, activity, account, scores, feed, trades, scanner, strat_b, strat_b_live, strat_feed, agents/*; plus coin/ + audit/ for the coin page being viewed, max 2). Each file is encrypted exactly like the published files (crypto.py, APP_TOKEN-derived AES-GCM key), so the server only ever sends ciphertext.
- Cost guard: builds only while the app is polling (a request in the last 20 s; idle = zero work), every 2 s at most, and never more than 10 % of the loop's time (interval = max(2 s, 10 × build time)). One file per step with a yield to the event loop between files; hashing/encryption runs in a worker thread. Builder only reads the db and in-memory state: no new GMGN / RPC / Gecko / DexScreener calls. The 30 s wallet refresh (DECISION 73) is unchanged. Feed builder now reads each coin row once per build instead of once per event (same output, ~220 ms → a few ms).
- Server: stdlib `ThreadingHTTPServer` on 127.0.0.1:8787 in a daemon thread, supervised job `live_api` (no heartbeat, never restarts trading jobs; a bind failure retries every 60 s). One route: `GET /live/v1/bundle?since=<cursor>[&coin=<mint>]` → `{v, cursor, full, builtAt, builtAtMs, buildMs, files:{rel: envelope}}`, only files whose content changed since the cursor (top-level `ts` ignored for change detection; a cursor from another boot gets everything). Auth: `Authorization: Bearer sha256("grok-bot-desk/v1/live:" + APP_TOKEN)` hex, `hmac.compare_digest`; the raw token never leaves the phone. CORS only for `https://cdeason12341-cpu.github.io` (GET, Authorization header, preflight cached 2 h); other Origins 403. POST/PUT/DELETE/PATCH/HEAD 405; other paths 404; 120 requests/min per IP (429). No access log, `Cache-Control: no-store`. No order, settings or write endpoint exists.
- TLS: Caddy 2.6 (Ubuntu package) on the droplet, `deploy/Caddyfile`, hostname `165-227-20-72.sslip.io`, Let's Encrypt cert auto-renewed. Only `/live/v1/*` is proxied, everything else 404. systemd drop-in `deploy/caddy-limits.conf` (MemoryMax 150M, CPUWeight 20, Nice 5). ufw: OpenSSH + 80/tcp + 443/tcp only (80 is for ACME / redirect); 8787 is localhost-only.
- App (shell desk-shell-v30, assets ?v=30): polls the droplet every 2 s while visible (paused when hidden, resumes on return), decrypts only the changed files and re-renders only when something changed (open rows / details kept, bars don't re-animate). Any failure or 4 s timeout falls back to the existing Pages files (unchanged cadence 15/30 s) and retries the droplet after 30 s. Header pill: "● live 2s" (green, droplet) or "Pages 40s" (backup). Pages publishing is unchanged.
- Tests: `tests/test_live_api.py` (auth, raw-token-rejected, constant-time compare, CORS, read-only, rate limit, cursor diffs, envelopes only / no secrets, localhost bind, no access log).
