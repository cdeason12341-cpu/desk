# Desk data files (Plan B) — contract for the iPhone app

The app is static (GitHub Pages). The desk process on the box writes these files, encrypts each one, and publishes
them under `https://cdeason12341-cpu.github.io/desk/d/{DATA_PATH}/`. The app never talks to the box.

## Opening the app
`https://cdeason12341-cpu.github.io/desk/#t={APP_TOKEN}&d={DATA_PATH}`
On first open, app.js reads `t` and `d` from `location.hash`, saves them in `localStorage`
(`desk.t`, `desk.d`), then clears the fragment with `history.replaceState`. Neither value is ever sent anywhere
except `d` as part of the file path. No token = show the "open the link from Telegram" empty state.

## Encryption (every file)
Envelope: `{"v":1,"iv":"<base64 12 bytes>","ct":"<base64 ciphertext+16-byte GCM tag>"}`
Key: `PBKDF2-SHA256(password = APP_TOKEN (utf-8), salt = utf-8 "grok-bot-desk/v1/salt", iterations = 200000, length = 256 bits)`
Cipher: AES-256-GCM, no additional data. Plaintext = UTF-8 JSON.
WebCrypto: `importKey("raw", enc(token), "PBKDF2")` -> `deriveKey({name:"PBKDF2",salt,iterations:200000,hash:"SHA-256"}, k, {name:"AES-GCM",length:256})`
-> `decrypt({name:"AES-GCM", iv}, key, ct)`. Derive once per session.
Wrong token -> decrypt throws -> error state "can't read the desk (link changed?)".

## Fetching
`fetch(base + "state.json?t=" + Date.now(), {cache:"no-store"})` (Pages sends max-age=600).
404 for a file = not published yet (empty state). A publish happens on events (signal, fill, exit, audit, analysis, scan),
debounced 20 s, plus a heartbeat every 180 s; a publish is capped at 90 s and never overlaps
a previous Pages run. Typical time from a coin event to the app is about 40–70 s
(20 s debounce + ~20–25 s Pages deploy + 0–15 s poll on the Live view). `state.ts` tells how fresh the data is.
Poll `state.json` every 30 s while visible; fetch other files when their screen is open.

All timestamps are ISO-8601 UTC (`...Z`); show them in the phone's local time. All `link` fields are FOMO URLs
from links.py and already carry `r=savipww`; never build FOMO URLs in the app.

## state.json (Home)
```json
{"v":1,"ts":"2026-11-14T22:40:00Z","mode":"PAPER","night":"2026-11-14-1640",
 "balance":200.0,"free":152.5,"inPositions":47.5,"startBalance":200.0,
 "positions":[Position],
 "agents":{"scanner":{"heartbeatAgeS":12.0,"note":"room=3","paused":false}, "auditor":{...}, "analyst":{...}, "executor":{...}, "risk":{...}},
 "counts":{"pools":41,"inRoom":3,"scannerPassed":2,"scannerKilled":36,"audited":2,"auditRejected":1,"scored":1,"analystRejected":0,"trades":1},
 "ticket":{"ok":true,"kind":"bump","msg":"tickets bumped to $47.50 = 23.75% of stack","min_stack":593.75},
 "wsReconnects1h":0,"layer8Down":false,"signupUrl":"https://fomo.family/r/savipww"}
```
Position:
```json
{"id":1,"mint":"…pump","name":"USPR","symbol":"USPR","mode":"PAPER","status":"open|exiting|closed",
 "entryPrice":0.1,"lastPrice":0.105,"peakPrice":0.11,"sizeUsd":47.5,"pct":5.0,"stopPct":6,"stopLine":0.094,
 "trailActive":false,"trailLine":null,"exitLine":0.094,"openedAt":"…Z","exitReason":null,"exitPrice":null,"pnlUsd":null,
 "link":"https://fomo.family/coin?address=…&chainId=1399811149&r=savipww&source=share_link"}
```
A heartbeat older than 120 s = agent shown as down.

## chart.json (Home balance chart)
`{"v":1,"ts":"…","mode":"PAPER","points":[{"t":"…Z","balance":200.0}],"trades":[{"mint","name","openedAt","closedAt","reason":"stop|trail|timeout|pulled|manual|null","pnlUsd","pct","stopPct","link"}]}`

## activity.json (Activity list, newest first, max 200)
`{"v":1,"ts":"…","items":[{"id":12,"t":"…Z","kind":"buy|sell|stop|reject","title":"Bought USPR","sub":"Executor · 16:41","right1":"$47.50","right2":"23.75% of stack","mint":"…","agent":"executor","tone":"cyan|green|red|neutral","link":"…"}]}`
Rows as the spec words them: `Rejected X / Auditor · {reason} / Skipped`, `Rejected X / Analyst (rules) · chase 0.31 / Skipped`,
`Bought X / Executor · {time} / ${size} · {pct}% of stack`, `Sold X / Risk · trailing stop / +$N / +P%`, `Stopped X / Risk · -6% stop / -$N`.

## agents/{name}.json (Agents tab cards)
Common: `{"v":1,"ts","agent","paused":false,"heartbeatAgeS":12.0}` plus:
- scanner: `"pools":41, "room":[{"mint","name","symbol","priceUsd","ageMin","link"}], "feed":[ScannerJSON + "link"]`
  ScannerJSON = `{"agent":"scanner","mint","name","symbol","pool","liquidityUsd","top10Pct","h1ChangePct","ageMin","verdict":"pass|kill","reason","ts"}`.
  Passed row: `{name} · liq ${liq} · → auditor`; killed: grey, red strike, reason right.
- auditor: `"takenApart":N, "rejected":N, "running":[{"mint","layers":[Layer]}], "recent":[AuditJSON + "link"]`
- analyst: `"mode":"jev|rules","scored":N,"minScore":70,"inputTokens":N,"outputTokens":N,"scoring":[mint],"recent":[AnalystJSON + "link"]`
  AnalystJSON = `{"agent":"analyst","mint","answers":{"alive_6h","organic","flush","chase"},"score","passed","failed":["chase 0.31"],"model","inputTokens"}`; ring green from minScore.
- executor: `"trades":N,"stackPct":8,"pending":[mint],"recent":[ExecutorJSON]`
  ExecutorJSON = `{"agent":"executor","mint","signalPrice","sizeUsd","stackPct","link","mode","status":"sent|bought|skipped|missed","reason"}`
- risk: `"guarding":bool,"stopPill":"stop -6%","trailPill":"trail 12%","lastExit":RiskJSON|null,"lastExitLabel":"Trailed out|Stopped|…","positions":[Position]`

## audit/{mint}.json (Auditor screen, disc animation)
AuditJSON: `{"v":1,"agent":"auditor","mint","ts","passed":false,"running":false,"layersChecked":2,"holders":null,"reason":"freeze not revoked","bundled":null,"link",
 "layers":[{"n":1,"name":"Mint authority","pass":true,"value":"Revoked","source":"rpc"}, {"n":2,…,"pass":false,"value":"freeze not revoked"}, {"n":3,…,"pass":null,"value":"not checked","source":null}, …]}`
`pass`: true = green check, false = red, null = grey "not checked". While `running` is true the app animates the
cyan beam on the first layer with `pass:null`. (Plan B: no SSE; layers arrive per publish, so the app replays them.)

## coin/{mint}.json (Coin screen)
`{"v":1,"ts","mint","name","symbol","pool","priceUsd","holders","status":"room|passed|killed|dropped","reason","migratedAt","coin_url","link",
 "candles":[{"t","o","h","l","c","volM5"}] (1-minute buckets from Scanner 60 s / Risk 15 s polls),
 "audit":AuditJSON|null,"analyst":AnalystJSON|null,"executor":ExecutorJSON|null,"position":Position|null,
 "riskLine":{"label":"Risk trailing stop","price":0.094,"style":"orange-dashed"}|null,
 "marks":[{"t","price","kind":"buy|sell","label":"Bought|-8%|Trailed out|Timeout|Pulled|Sold"}]}`

## account.json (Account tab; = spec GET /api/account: mode, stack, profile_url, SIGNUP_URL)
`{"v":1,"ts","mode","stack":200.0,"profile_url":"https://fomo.family/profile/cd12345?source=share_link&r=savipww","SIGNUP_URL":"https://fomo.family/r/savipww","fomoHandle":"cd12345","profileUrl":"https://fomo.family/profile/cd12345?source=share_link&r=savipww",
 "signupUrl":"https://fomo.family/r/savipww","telegramBot":"@Denarpobot","editVia":"telegram",
 "settings":[{"key":"risk.stop_pct","value":6,"default":6,"min":1,"max":50,"kind":"float|int|bool|choice|time|str","choices":[],"display":"-6%","aliases":["stop","stop loss","sl"]}],
 "changeLog":[{"t","key","old","new","source"}]}`
Plan B: the Account tab shows sliders + exact number fields read-only with each setting's range, and a
"change in Telegram" hint that copies e.g. `set stop to -8%`. The app can't write to the desk.

## gmgn (in state.json and account.json)
Read-only snapshot of the owner's main GMGN wallet and hands-off trades. Never contains API keys,
signing keys, swap bodies, quotes, tx hashes, or broker order ids.

```json
{"mode":"LIVE","enabled":true,"halted":false,
 "wallet":"JAP5hC5dxCz8Q9kKXMshRKEwoN7fXX3JFpkKQ7Mo5zC2","walletShort":"JAP5…5zC2",
 "sol":1.02,"usd":71.40,"solUsd":70.0,"updatedAt":"2026-11-15T02:40:00Z",
 "capTradeUsd":50.0,"stepTradeUsd":0.0,"fallbackTradeUsd":20.0,"minTradeUsd":1.0,
 "sizeLabel":"$50, then $20",
 "ticketsUsed":0,"ticketsMax":4,"nightSpendUsd":0.0,"nightCapUsd":300.0,
 "reserveSol":0.02,
 "paper":{"bankrollUsd":2500.0,"tradeUsd":500.0,"pnlUsd":0.0,"inPositionsUsd":0.0,"freeUsd":2500.0,"since":"2026-11-17T16:20:00Z"},
 "open":[{"id":1,"mint","name","symbol","mode":"LIVE","status":"open",
          "entryPrice":0.1,"lastPrice":0.11,"peakPrice":0.12,"sizeUsd":65.0,
          "pct":10.0,"pnlUsd":6.5,"openedAt":"…Z",
          "exits":{"stopPct":6,"trailPct":12,"trailStartPct":20,"attached":true,"repair":null,
                   "phase":"normal","graceMin":0,"graceStopPct":30,"normalStopPct":6,"graceUntil":null,
                   "label":"stop -6% · trail 12%"},
          "link":"https://fomo.family/coin?address=…&chainId=1399811149&r=savipww&source=share_link"}],
 "orders":[{"id":1,"t":"…Z","mint","name","symbol","mode":"LIVE","status":"open|closed|…",
            "reason":null,"sizeUsd":65.0,"entryPrice":0.1,"lastPrice":0.11,"exitPrice":null,
            "exitReason":null,"pnlUsd":null,"pct":null,"openedAt":"…Z","closedAt":null,"link":"…"}]}
```
Balance and SOL price are cached on the desk and refreshed at most every 60s. `sol`/`usd` may be
null if the desk has not yet read the wallet. The Home card shows this as "Real wallet (GMGN LIVE)";
the FOMO $200 PAPER stack is labeled "FOMO practice balance (PAPER, not real money)".
`paper` is Strategy A's simulated bankroll (DECISION 68): start amount, trade size, realized P&L
since the bankroll reset, cash in open paper tickets, and free cash. It is not real money and is
not limited by the wallet or `capTradeUsd`. `since` is an ISO timestamp or null.
`stepTradeUsd` is the optional middle size between the top cap and the fallback (0 = off).
`sizeLabel` is the public size text shown in the app and Telegram, e.g. `$50, then $20` (DECISION 85; a middle `stepTradeUsd` is included only when it sits between the cap and the fallback).


## scores.json (Analyst scores list, newest first, max 50 tonight)
```json
{"v":1,"ts":"…Z","night":"2026-11-14-1640","minScore":60,
 "items":[{"t":"…Z","mint":"…pump","mintShort":"fQKg…pump","name":"Nyajira","symbol":"NYA",
           "score":59,"answers":{"alive_6h":0.54,"organic":0.53,"flush":0.27,"chase":0.68},
           "passed":false,"failed":["alive_6h 0.54","organic 0.53","chase 0.68","score 59"],
           "bought":false,
           "link":"https://fomo.family/coin?address=…&chainId=1399811149&r=savipww&source=share_link"}]}
```
`minScore` is the current `analyst.min_score` bar (the number a coin must reach to buy).
`bought` is true if that mint has a FOMO position, a bought execution, or a GMGN order tonight
(other than refused/skipped). FOMO `link` comes only from links.py. Secrets and broker fields
are never included. The app shows this on the Scores tab and as a Home card; tapping a row
reveals the four answers and the failed reasons.

## feed.json (Live evaluation feed, newest first, max 100 tonight)
```json
{"v":1,"ts":"…Z","night":"2026-11-14-1640","minScore":60,
 "bars":{"alive_6h":0.70,"organic":0.65,"chase":0.30},
 "items":[
   {"t":"…Z","mint":"…pump","mintShort":"fQKg…pump","name":"Nyajira","symbol":"NYA",
    "stage":"analyst","outcome":"no_buy","reason":null,"recheckN":null,
    "layer":null,"layerName":null,"score":59,
    "answers":{"alive_6h":0.54,"organic":0.53,"flush":0.27,"chase":0.68},
    "passed":false,"failed":["alive_6h 0.54","organic 0.53","chase 0.68","score 59"],
    "link":"https://fomo.family/coin?address=…&chainId=1399811149&r=savipww&source=share_link"},
   {"t":"…Z","mint":"…","mintShort":"…","name":"DeadCoin","symbol":"DEAD",
    "stage":"scanner","outcome":"kill","reason":"liq $0.0K","recheckN":null,
    "layer":null,"layerName":null,"score":null,"answers":null,"passed":null,"failed":[],
    "link":"…"},
   {"t":"…Z","stage":"recheck","outcome":"recheck","recheckN":2,"reason":"recheck 2: liq $5.4K · next at 30m"},
   {"t":"…Z","stage":"room","outcome":"waiting","reason":"waiting room"},
   {"t":"…Z","stage":"auditor","outcome":"fail","layer":3,"layerName":"Dev wallet","reason":"dev sold","passed":false},
   {"t":"…Z","stage":"dip","outcome":"watching","reason":"need -5% from high · buy anyway in 18s","high":0.0012,"lastPrice":0.00118,"dropPct":1.7}
 ],
 "dips":[{"mint","name","symbol","high","low","lastPrice","startPrice","dropPct","needPct":5,"timeoutS":30,
          "expiresAt":"…Z","since":"…Z","link":"…r=savipww…"}]}
```
Every item also carries `"liq"` and `"mc"` (USD numbers from the scan JSON `liquidityUsd` /
`marketCapUsd`, or null). Scanner / recheck / room rows show them as `MC $31.2K · liq $12.0K`.
`stage` is `scanner`, `recheck`, `room`, `auditor`, `analyst`, or `dip` (DECISION 64).
`outcome` is `kill`, `pass`, `fail`, `recheck`, `waiting`, `buy`, `no_buy`, or for dip rows `watching`, `dip_buy`, `timeout_buy`, `skipped`.
`dips` is the coins currently waiting for a 5% dip (or a 30s market buy if none comes).
`bars` are the current Analyst alive / organic / chase thresholds (chase is a max).
`minScore` is the current `analyst.min_score` buy bar. Analyst rows in the app highlight
green when `score >= minScore`. BUY means the Analyst passed every bar; NO BUY otherwise.
FOMO `link` comes only from links.py (`r=savipww`). Secrets and broker fields are never included.
The app shows this as the Live feed on the Live tab and polls it every 15 s while that tab is open. A Live | Scores toggle at the top of the tab switches to the Analyst scores list (scores.json). Tapping an Analyst row (on Live or Scores) opens a detail sheet: each answer vs its bar, PASS/FAIL, and BUY or “missed {minScore} by N”.

## trades.json (Real | Paper toggle on Home)
`{v, ts, deskMode, gmgnMode, real: Side, paper: Side & {bypassOn, bypassCount}}`
Side: `{open: [Row], closed: [Row], pnlTonightUsd, pnlAllUsd, closedTonight, wins, losses}`.
Real = GMGN LIVE orders + LIVE positions. Paper = PAPER positions (FOMO) + GMGN PAPER orders.
Row: position_view / gmgn_order fields plus `source` ("FOMO"|"GMGN"), `open`, `bypass` (bool), `score` (analyst score for
bypass rows), `closedAt`. No broker ids, quotes, bodies or tx.

## strat_b.json (Strat B tab) — Strategy B (DECISION 54; B LIVE since DECISION 66)
Simulated strategy, separate from GMGN and from trades.json. Never real money; the app shows it with a PAPER badge.
```json
{"v":1,"ts":"…Z","label":"STRATEGY B · PAPER","mode":"PAPER","enabled":true,"night":"…",
 "bankrollStart":1000.0,"bankroll":1012.4,"free":962.4,"inPositions":50.0,"unrealizedUsd":-3.1,
 "pnlAllUsd":12.4,"pnlTonightUsd":12.4,"closedTonight":2,"closedAll":2,"wins":1,"losses":1,"winRatePct":50.0,"profitFactor":1.8,
 "watching":7,"skipped":12,"expired":3,
 "tradeUsd":50.0,"fallbackTradeUsd":20.0,"minTradeUsd":1.0,"sizeLabel":"$50, then $20",
 "open":[{"id":3,"mint","name","symbol","mode":"PAPER","status":"open","sizeUsd":50.0,"entryPrice":0.0001,"signalPrice":0.0000988,
          "lastPrice":0.000097,"peakPrice":0.0001,"openedAt":"…Z","mcEntry":120000,"why":"pullback -18% then +6% bounce, 74m after graduation",
          "pnlUsd":-3.1,"pct":-6.2,"trailArmed":false,"stopLine":0.000065,"tpLine":0.0002,"trailLine":null,"timeStopAt":"…Z","link":"…r=savipww…"}],
 "closed":[{… "status":"closed","pnlUsd":20.1,"pct":40.2,"exitReason":"stop|take profit 2x|spike|trail|time stop|liquidity pulled","exitPrice":…,"closedAt":"…Z"}],
 "rules":[{"k":"Stop","v":"-50% for the first 5 min, then -35% from entry","why":"…"},
          {"k":"Trail","v":"arms at +20%, sells on a 20% drop from the high, only when expected net after fees is at least +5%","why":"…"}]}
```
`state.json` also carries `stratB`: `{label, mode, enabled, bankroll, pnlAllUsd, pnlTonightUsd, closedAll, wins, losses, winRatePct, watching, openCount, live:{label, active, pnlAllUsd, pnlTonightUsd, closedAll, wins, losses, winRatePct, freeSol, refusedTonight, openCount}}`.

**B LIVE (DECISION 66, `v`:2).** Top-level paper fields above stay paper-only (`mode='PAPER'` rows); `mode`/`label` are the
current Strategy B mode (`"LIVE"` → `"B LIVE"`). Real-money results sit under `live`, never mixed with the paper bankroll:
```json
"live":{"label":"B LIVE","mode":"LIVE","active":true,"pnlAllUsd":-4.2,"pnlTonightUsd":-4.2,"closedTonight":1,"closedAll":1,
        "wins":0,"losses":1,"winRatePct":0.0,"profitFactor":null,"unrealizedUsd":0,"inPositions":0,
        "wallet":{"sol":0.0012,"usd":0.08,"solUsd":70.1,"updatedAt":"…Z"},"freeSol":0.0,"reserveSol":0.02,
        "refusedTonight":3,"lastRefusal":{"t":"…Z","name":"Cat","reason":"free 0.0000 SOL can't cover even $1.00 …"},
        "open":[{… "mode":"LIVE","label":"B LIVE","status":"open|submitting|pending|exiting","sol":0.71,"stopOnGmgn":true,"note":null}],
        "closed":[{… "mode":"LIVE","label":"B LIVE","pnlUsd":-4.2,"pct":-35.4,"exitReason":"stop (GMGN server-side)|take profit 2x|…"}],
        "failed":[…]}
```
Every order row also has `mode` ("PAPER"|"LIVE") and `label` ("B PAPER"|"B LIVE"). No order ids or tx signatures.

## strat_b_live.json (Strat B → Live) — DECISION 60 (`mode`/`label` follow strat_b.mode; items may have status `refused` in LIVE)
```json
{"v":1,"ts":"…Z","label":"STRATEGY B · PAPER","mode":"PAPER","night":"…","enabled":true,
 "counts":{"watching":3,"skippedTonight":9,"boughtTonight":1,"expiredTonight":2,"evaluationsTonight":41},
 "items":[{"id":41,"t":"…Z","mint","name","symbol","kind":"offer|wait|buy|expired","status":"watching|skipped|bought|expired",
           "reason":"top 10 hold 24.5% > 20%","price":0.0001,"link":"…r=savipww…",
           "checks":[{"k":"top10","label":"Top 10","pass":false,"value":"24.5%","bar":"≤ 20%"}, … 10 checks …]}],
 "watchlist":[{"mint","name","symbol","addedAt","expiresAt","minLeft":142,"ageMin":51,"price","high","low",
               "checks":[…],"passCount":7,"checkCount":10,"away":["9m left of the wait","needs 6.2% more dip"],"evalAt","link"}]}
```

## strat_feed.json (Wallet / Strat A / Strat B tabs) — DECISION 72
```
{v, ts,
 pnl: {A: {strategy, since, sinceLabel, pnlUsd, pnlTonightUsd, closed, closedTonight, wins, losses, winRatePct, winsTonight, lossesTonight, winRateTonightPct}, B: {...same}},
 A: [{t, kind: buy|sell|stop|skip|fail|approval, title, sub, mint, name, symbol, pnlUsd, pct, sizeUsd, tone, link}],   // last 24h, newest first, max 80
 B: [...same, no approvals]}
```
The app's main profit figure is `pnlTonightUsd`: LIVE closes on the current 8 AM CT night (the desk's `night` id, e.g. `2026-11-18-0800`). It resets automatically at each 8 AM CT rollover. The small-print "Total" next to it is `pnlUsd`: LIVE closes at/after `since` (settings `gmgn.pnl_since` / `strat_b.pnl_since`, the 2026-11-17 6:20 PM CT reset). Display only. `winsTonight` / `lossesTonight` / `winRateTonightPct` / `closedTonight` are the current night; `wins` / `losses` / `winRatePct` / `closed` are the total since `since`. trades.json `real.since` and strat_b.json `live.since/sinceLabel/earlier/earlierClosed/earlierPnlUsd` follow the same baseline. Both the live droplet bundle and the Pages bundle carry these fields from the publisher.

## push (in state.json) — phone notifications (DECISION 62)
`state.json.push` = `{vapid, subs: [{id, cats, device, lastOk, fails}], cats, last: {t, title} | null}` or `null`.
`vapid` is the PUBLIC VAPID key (base64url, 65-byte P-256 point) for `pushManager.subscribe`. `subs[].id` = first 16 hex of
sha256(endpoint), so the app can tell whether the desk has this phone. Categories: trades, approvals, alerts, pipeline, stratb, paper.
The phone reaches the desk by POSTing a `{"v":1,"iv","ct"}` envelope (same key as the data) to `https://ntfy.sh/gbdesk-<sha256("grok-bot-desk/v1/push:"+token)[:40]>`:
`{type:"sub", sub, cats, device, ts}` · `{type:"test", endpoint, ts}` · `{type:"unsub", endpoint, ts}` (ts in ms, rejected after 15 min).
Push payload (JSON, aes128gcm): `{cat, title, body, tag, url, renotify, ts, actions?, aid?, bot?, requireInteraction?}`.
