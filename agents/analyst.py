"""AGENT 3 · ANALYST. Builds a numbers-only state, asks Jev 4 closed 'noul' questions; code applies the gate."""
from __future__ import annotations
import asyncio, hashlib, json, time
import fields, secrets_store
from ctx import iso
from net import RateLimited

QUESTIONS = {
    "alive_6h": {"type": "noul", "instructions": "This coin is still actively traded 6 hours from now"},
    "organic": {"type": "noul", "instructions": "Most buyers in the last 30 minutes are separate real wallets"},
    "flush": {"type": "noul", "instructions": "Price just flushed down and buyers are absorbing it"},
    "chase": {"type": "noul", "instructions": "This entry is chasing a green candle that already ran"},
}
NAMES = list(QUESTIONS)


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _n(v, fmt="{:,.0f}"):
    return "n/a" if v is None else fmt.format(float(v))


def build_state(scan: dict, audit: dict, pair: dict, max_chars: int = 3000) -> str:
    vals = {l["n"]: l["value"] for l in audit.get("layers", [])}
    dev = (vals.get(3) or "").split("%")[0] or "n/a"
    top10 = (vals.get(4) or "").rstrip("%") or "n/a"
    g = lambda p: fields.get(pair, p)
    st = (f"Coin {scan.get('name') or g('baseToken.name') or '?'} on Solana, graduated {scan.get('ageMin', 0)} min ago. "
          f"Liquidity ${_n(g('liquidity.usd'))}. Market cap ${_n(g('marketCap'))}.\n"
          f"Last 5 min: {_n(g('txns.m5.buys'))} buys, {_n(g('txns.m5.sells'))} sells, volume ${_n(g('volume.m5'))}, price {_n(g('priceChange.m5'), '{:+.1f}')}%.\n"
          f"Last 1h: {_n(g('txns.h1.buys'))} buys, {_n(g('txns.h1.sells'))} sells, volume ${_n(g('volume.h1'))}, price {_n(g('priceChange.h1'), '{:+.1f}')}%.\n"
          f"Top 10 hold {top10}%. Dev holds {dev}%, sold 0. LP {vals.get(5, 'n/a')}. Bundled {vals.get(7, 'n/a')}. Delegations {vals.get(8, 'n/a')}.")
    return st[:max_chars]


def rules_answers(s, pair: dict, bundled: int | None) -> dict:
    g = lambda p: fields.get(pair, p)
    alive = clamp(num(g("volume.h1")) / s["analyst.rules_alive_volume_h1"])
    organic = clamp(num(g("txns.h1.buys")) / s["analyst.rules_organic_buys_h1"])
    if bundled is not None and bundled >= s["analyst.rules_bundled_from"]:
        organic *= s["analyst.rules_bundled_penalty"]
    m5 = num(g("priceChange.m5"))
    if m5 <= -s["analyst.rules_flush_drop_m5_pct"] and num(g("txns.m5.buys")) >= num(g("txns.m5.sells")):
        flush = 1.0
    elif m5 < 0:
        flush = 0.5
    else:
        flush = 0.0
    chase = clamp(num(g("priceChange.h1")) / s["analyst.rules_chase_h1_pct"])
    return {"alive_6h": alive, "organic": organic, "flush": flush, "chase": chase}


def gate(s, a: dict) -> tuple[int, bool, list[str]]:
    failed = []
    if a["alive_6h"] < s["analyst.min_alive_6h"]:
        failed.append(f"alive_6h {a['alive_6h']:.2f}")
    if a["organic"] < s["analyst.min_organic"]:
        failed.append(f"organic {a['organic']:.2f}")
    if a["chase"] > s["analyst.max_chase"]:
        failed.append(f"chase {a['chase']:.2f}")
    score = round(100 * (a["alive_6h"] + a["organic"] + a["flush"] + (1 - a["chase"])) / 4 + 1e-9)
    if score < s["analyst.min_score"]:
        failed.append(f"score {score}")
    return score, not failed, failed


class Analyst:
    name = "analyst"

    def __init__(self, ctx):
        self.c = ctx
        self.log = ctx.logs["analyst"]
        self.live: dict[str, dict] = {}

    async def loop(self):
        while True:
            self.c.hb("analyst", f"queue={self.c.bus.analyst.qsize()}")
            try:
                item = await asyncio.wait_for(self.c.bus.analyst.get(), timeout=20)
            except asyncio.TimeoutError:
                continue
            while "analyst" in self.c.paused:
                self.c.hb("analyst", "paused")
                await asyncio.sleep(5)
            try:
                res = await self.run(item["scan"], item["audit"])
                if res and res["passed"] and self.c.bus:
                    self.c.bus.executor.put_nowait({"scan": item["scan"], "audit": item["audit"], "analyst": res})
            except Exception as e:
                self.c.error("analyst", "run", f"{item['scan'].get('mint')} {str(e)[:300]}")

    async def fresh_pair(self, mint):
        from agents.scanner import pick_pair
        return pick_pair(await self.c.net.dex_tokens([mint]), mint)

    async def run(self, scan, audit, pair=None):
        c, s = self.c, self.c.s
        mint = scan["mint"]
        if c.db.one("SELECT id FROM analyses WHERE mint=? AND night=?", (mint, c.night)):
            return None                                     # never re-asks the same coin
        if pair is None:
            try:
                pair = await self.fresh_pair(mint)
            except Exception:
                pair = None
        mode = s["analyst.mode"]
        model, itok, otok, raw = "rules", 0, 0, None
        state = build_state(scan, audit, pair or {}, int(s["analyst.state_max_chars"]))
        answers, fail_reason = None, None
        self.live[mint] = {"mint": mint, "running": True, "ts": iso()}
        if pair is None:
            fail_reason = "no price"
        elif mode == "rules":
            answers = rules_answers(s, pair, audit.get("bundled"))
        else:
            answers, model, itok, otok, raw, fail_reason = await self.ask_jev(state)
        if answers is not None:
            score, passed, failed = gate(s, answers)
        else:
            score, passed, failed = 0, False, [fail_reason]
        out = {"agent": "analyst", "mint": mint,
               "answers": {k: (round(v, 4) if isinstance(v, (int, float)) else v) for k, v in (answers or {}).items()},
               "score": score, "passed": passed, "failed": failed, "model": model, "inputTokens": itok}
        c.db.x("INSERT INTO analyses(ts,mint,passed,failed,score,model,input_tokens,output_tokens,json,night) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (c.clock(), mint, 1 if passed else 0, json.dumps(failed), score, model, itok, otok, json.dumps(out), c.night))
        h = hashlib.sha256(state.encode()).hexdigest()[:12]
        a = answers or {}
        self.log.info(f"{iso()}\t{mint}\tstate={h}\t" + "\t".join(f"{k}={a.get(k)}" for k in NAMES)
                      + f"\tscore={score}\t{'pass' if passed else 'reject'}\t{failed}\tin_tokens={itok}"
                      + (f"\traw={secrets_store.redact(json.dumps(raw))[:500]}" if raw is not None and answers is None else ""))
        if not passed:
            who = "Analyst (rules)" if model == "rules" else "Analyst"
            c.activity("reject", f"Rejected {c.label(mint)}", f"{who} · {failed[0]}", "Skipped", "", mint, "analyst", "red")
            c.skip(mint, "analyst", failed[0])
        self.live.pop(mint, None)
        c.event("analysis", mint)
        return out

    async def ask_jev(self, state):
        key = secrets_store.get("TYPESAFE_API_KEY")
        s = self.c.s
        if not key:
            return None, "jev", 0, 0, None, "no answer"
        t_first = time.time()
        attempt = 0
        while True:
            try:
                r = await self.c.net.jev(key, state, QUESTIONS, timeout=s["analyst.timeout_s"])
                break
            except RateLimited:
                if time.time() - t_first > s["analyst.queue_429_s"]:
                    return None, "jev", 0, 0, None, "no answer"
                await asyncio.sleep(5)
            except Exception:
                attempt += 1
                if attempt > 1:
                    return None, "jev", 0, 0, None, "no answer"
                await asyncio.sleep(2)
        model = r.get("model") or "jev"
        usage = r.get("usage") or {}
        itok, otok = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        ans = {}
        for k in NAMES:
            v = fields.get(r, f"answers.{k}.noul")
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 1):
                return None, model, itok, otok, r, "bad answer"
            ans[k] = float(v)
        return ans, model, itok, otok, r, None
