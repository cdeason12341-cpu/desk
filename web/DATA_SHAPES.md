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
 "capTradeUsd":65.0,"ticketsUsed":0,"ticketsMax":4,"nightSpendUsd":0.0,"nightCapUsd":260.0,
 "reserveSol":0.02,
 "open":[{"id":1,"mint","name","symbol","mode":"LIVE","status":"open",
          "entryPrice":0.1,"lastPrice":0.11,"peakPrice":0.12,"sizeUsd":65.0,
          "pct":10.0,"pnlUsd":6.5,"openedAt":"…Z",
          "exits":{"stopPct":6,"trailPct":12,"trailStartPct":20,"attached":true,"repair":null},
          "link":"https://fomo.family/coin?address=…&chainId=1399811149&r=savipww&source=share_link"}],
 "orders":[{"id":1,"t":"…Z","mint","name","symbol","mode":"LIVE","status":"open|closed|…",
            "reason":null,"sizeUsd":65.0,"entryPrice":0.1,"lastPrice":0.11,"exitPrice":null,
            "exitReason":null,"pnlUsd":null,"pct":null,"openedAt":"…Z","closedAt":null,"link":"…"}]}
```
Balance and SOL price are cached on the desk and refreshed at most every 60s. `sol`/`usd` may be
null if the desk has not yet read the wallet. The Home card shows this as "Real wallet (GMGN LIVE)";
the FOMO $200 PAPER stack is labeled "FOMO practice balance (PAPER, not real money)".


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
   {"t":"…Z","stage":"auditor","outcome":"fail","layer":3,"layerName":"Dev wallet","reason":"dev sold","passed":false}
 ]}
```
`stage` is `scanner`, `recheck`, `room`, `auditor`, or `analyst`.
`outcome` is `kill`, `pass`, `fail`, `recheck`, `waiting`, `buy`, or `no_buy`.
`bars` are the current Analyst alive / organic / chase thresholds (chase is a max).
`minScore` is the current `analyst.min_score` buy bar. Analyst rows in the app highlight
green when `score >= minScore`. BUY means the Analyst passed every bar; NO BUY otherwise.
FOMO `link` comes only from links.py (`r=savipww`). Secrets and broker fields are never included.
The app shows this as the Live feed at the top of the Scores / Live tab and polls it every 15 s while that tab is open.
