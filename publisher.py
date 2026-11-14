"""Plan B writer: builds the app's JSON files, encrypts each (crypto.py), bundles them into the secret gist,
and dispatches the Pages workflow pinned to that gist revision. Debounced on events + heartbeat; never overlaps."""
from __future__ import annotations
import asyncio, json, os, time
from pathlib import Path
import config, crypto, links, secrets_store
from ctx import AGENTS, ROOT, iso

DATA = ROOT / "data"
REPO = "cdeason12341-cpu/desk"
DEBOUNCE_S = 20
MAX_COIN_FILES = 150


class Builder:
    """Pure-ish: reads db/ctx and returns {relpath: obj}. Shapes documented in web/DATA_SHAPES.md."""

    def __init__(self, desk):
        self.d, self.c = desk, desk.ctx

    def build(self) -> dict:
        files = {"state.json": self.state(), "chart.json": self.chart(), "activity.json": self.activity(),
                 "account.json": self.account()}
        for a in AGENTS:
            files[f"agents/{a}.json"] = self.agent(a)
        for m in self.coin_mints():
            files[f"coin/{m}.json"] = self.coin(m)
            au = self.audit(m)
            if au:
                files[f"audit/{m}.json"] = au
        return files

    # ---- helpers
    def hb(self):
        now = time.time()
        return {r["agent"]: {"ageS": round(now - r["ts"], 1), "note": r["note"]} for r in self.c.db.rows("SELECT * FROM heartbeats")}

    def night_counts(self):
        n, db = self.c.night, self.c.db
        one = lambda q, a=(): db.one(q, a)["n"]
        return {
            "pools": one("SELECT COUNT(*) n FROM coins WHERE night=?", (n,)),
            "inRoom": one("SELECT COUNT(*) n FROM coins WHERE status='room'"),
            "scannerPassed": one("SELECT COUNT(*) n FROM scans WHERE night=? AND verdict='pass'", (n,)),
            "scannerKilled": one("SELECT COUNT(*) n FROM scans WHERE night=? AND verdict='kill'", (n,)),
            "audited": one("SELECT COUNT(*) n FROM audits WHERE night=?", (n,)),
            "auditRejected": one("SELECT COUNT(*) n FROM audits WHERE night=? AND passed=0", (n,)),
            "scored": one("SELECT COUNT(*) n FROM analyses WHERE night=?", (n,)),
            "analystRejected": one("SELECT COUNT(*) n FROM analyses WHERE night=? AND passed=0", (n,)),
            "trades": one("SELECT COUNT(*) n FROM positions WHERE night=?", (n,)),
        }

    def position_view(self, p):
        from agents.risk import lines
        ln = lines(self.c.s, p)
        last = p["last_price"] or p["entry_price"]
        return {"id": p["id"], "mint": p["mint"], "name": p["name"], "symbol": p["symbol"], "mode": p["mode"],
                "status": p["status"], "entryPrice": p["entry_price"], "lastPrice": last, "peakPrice": p["peak"],
                "sizeUsd": p["size_usd"], "pct": round((last / p["entry_price"] - 1) * 100, 2),
                "stopPct": p["stop_pct"], "stopLine": ln["stop"], "trailActive": bool(p["trail_active"]),
                "trailLine": ln["trail"], "exitLine": ln["exit_line"], "openedAt": iso(p["opened_at"]),
                "exitReason": p["exit_reason"], "exitPrice": p["exit_price"], "pnlUsd": p["pnl_usd"],
                "link": links.coin_url(p["mint"])}

    def state(self):
        c, s = self.c, self.c.s
        pos = c.db.rows("SELECT * FROM positions WHERE status IN ('open','exiting') ORDER BY opened_at")
        tc = config.ticket_check(c.balance(), s["executor.stack_pct"], s["executor.min_ticket_usd"], s["executor.below_min"])
        recon = c.db.one("SELECT COUNT(*) n FROM ws_events WHERE kind='reconnect' AND ts>?", (time.time() - 3600,))["n"]
        hb = self.hb()
        return {"v": 1, "ts": iso(), "mode": c.mode(), "night": c.night,
                "balance": round(c.balance(), 2), "free": round(c.free(), 2), "inPositions": round(c.in_positions(), 2),
                "startBalance": (c.db.one("SELECT start_balance FROM nights WHERE id=?", (c.night,)) or {}).get("start_balance"),
                "positions": [self.position_view(p) for p in pos],
                "agents": {a: {"heartbeatAgeS": (hb.get(a) or {}).get("ageS"), "note": (hb.get(a) or {}).get("note"),
                               "paused": a in c.paused} for a in AGENTS},
                "counts": self.night_counts(), "ticket": tc, "wsReconnects1h": recon,
                "layer8Down": bool(self.d.auditor.gpa_down_alerted),
                "signupUrl": links.SIGNUP_URL}

    def chart(self):
        rows = self.c.db.rows("SELECT ts, balance FROM balance WHERE mode=? ORDER BY ts DESC LIMIT 500", (self.c.mode(),))[::-1]
        marks = self.c.db.rows("SELECT mint,name,opened_at,closed_at,exit_reason,pnl_usd,pct,stop_pct FROM positions WHERE mode=? ORDER BY opened_at DESC LIMIT 100", (self.c.mode(),))
        return {"v": 1, "ts": iso(), "mode": self.c.mode(),
                "points": [{"t": iso(r["ts"]), "balance": round(r["balance"], 2)} for r in rows]
                          + [{"t": iso(), "balance": round(self.c.balance(), 2)}],
                "trades": [{"mint": m["mint"], "name": m["name"], "openedAt": iso(m["opened_at"]),
                            "closedAt": iso(m["closed_at"]) if m["closed_at"] else None, "reason": m["exit_reason"],
                            "pnlUsd": m["pnl_usd"], "pct": m["pct"], "stopPct": m["stop_pct"],
                            "link": links.coin_url(m["mint"])} for m in marks]}

    def activity(self):
        rows = self.c.db.rows("SELECT * FROM activity ORDER BY id DESC LIMIT 200")
        return {"v": 1, "ts": iso(), "items": [
            {"id": r["id"], "t": iso(r["ts"]), "kind": r["kind"], "title": r["title"], "sub": r["sub"],
             "right1": r["right1"], "right2": r["right2"], "mint": r["mint"], "agent": r["agent"], "tone": r["tone"],
             "link": links.coin_url(r["mint"]) if r["mint"] else None} for r in rows]}

    def agent(self, a):
        c, s, n = self.c, self.c.s, self.c.night
        base = {"v": 1, "ts": iso(), "agent": a, "paused": a in c.paused,
                "heartbeatAgeS": (self.hb().get(a) or {}).get("ageS")}
        if a == "scanner":
            feed = c.db.rows("SELECT s.ts, s.mint, s.verdict, s.reason, s.json FROM scans s WHERE night=? ORDER BY id DESC LIMIT 60", (n,))
            room = c.db.rows("SELECT mint,name,symbol,price,migrated_at FROM coins WHERE status='room' ORDER BY migrated_at DESC LIMIT 60")
            return base | {"pools": self.night_counts()["pools"], "room": [
                {"mint": r["mint"], "name": r["name"], "symbol": r["symbol"], "priceUsd": r["price"],
                 "ageMin": int((time.time() - r["migrated_at"]) / 60), "link": links.coin_url(r["mint"])} for r in room],
                "feed": [dict(json.loads(f["json"]), link=links.coin_url(f["mint"])) for f in feed]}
        if a == "auditor":
            rows = c.db.rows("SELECT ts,mint,passed,reason,layer,json FROM audits WHERE night=? ORDER BY id DESC LIMIT 40", (n,))
            live = list(self.d.auditor.live.values())
            return base | {"takenApart": len(rows), "rejected": sum(1 for r in rows if not r["passed"]),
                           "running": [{"mint": l["mint"], "layers": [{k: x[k] for k in ("n", "name", "pass", "value")} for x in l["layers"]]} for l in live],
                           "recent": [dict(json.loads(r["json"]), link=links.coin_url(r["mint"])) for r in rows]}
        if a == "analyst":
            rows = c.db.rows("SELECT * FROM analyses WHERE night=? ORDER BY id DESC LIMIT 40", (n,))
            return base | {"mode": s["analyst.mode"], "scored": len(rows), "minScore": s["analyst.min_score"],
                           "inputTokens": sum(r["input_tokens"] or 0 for r in rows),
                           "outputTokens": sum(r["output_tokens"] or 0 for r in rows),
                           "scoring": list(self.d.analyst.live.keys()),
                           "recent": [dict(json.loads(r["json"]), link=links.coin_url(r["mint"])) for r in rows]}
        if a == "executor":
            rows = c.db.rows("SELECT * FROM executions WHERE night=? ORDER BY id DESC LIMIT 40", (n,))
            return base | {"trades": sum(1 for r in rows if r["status"] == "bought"), "stackPct": s["executor.stack_pct"],
                           "pending": list(self.d.executor.pending.keys()),
                           "recent": [json.loads(r["json"]) for r in rows]}
        if a == "risk":
            last = c.db.meta("risk_last_exit")
            pos = c.db.rows("SELECT * FROM positions WHERE status IN ('open','exiting')")
            return base | {"guarding": bool(pos), "stopPill": f"stop -{s['risk.stop_pct']:g}%",
                           "trailPill": f"trail {s['risk.trail_pct']:g}%", "lastExit": last,
                           "lastExitLabel": None if not last else ("Trailed out" if last["reason"] == "trail" else "Stopped" if last["reason"] == "stop" else last["reason"].title()),
                           "positions": [self.position_view(p) for p in pos]}
        return base

    def coin_mints(self):
        n = self.c.night
        rows = self.c.db.q("SELECT DISTINCT mint FROM (SELECT mint FROM audits WHERE night=? UNION SELECT mint FROM positions WHERE night=? OR status IN ('open','exiting'))",
                           (n, n))
        return [r[0] for r in rows][-MAX_COIN_FILES:]

    def candles(self, mint, bucket=60):
        rows = self.c.db.rows("SELECT ts, price, vol_m5 FROM prices WHERE mint=? ORDER BY ts", (mint,))
        out, cur = [], None
        for r in rows:
            b = int(r["ts"] // bucket * bucket)
            if not cur or cur["b"] != b:
                if cur:
                    out.append(cur)
                cur = {"b": b, "o": r["price"], "h": r["price"], "l": r["price"], "c": r["price"], "v": r["vol_m5"]}
            else:
                cur["h"] = max(cur["h"], r["price"]); cur["l"] = min(cur["l"], r["price"]); cur["c"] = r["price"]; cur["v"] = r["vol_m5"]
        if cur:
            out.append(cur)
        return [{"t": iso(x["b"]), "o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"], "volM5": x["v"]} for x in out[-300:]]

    def coin(self, mint):
        c = self.c
        co = c.coin(mint) or {}
        au = c.db.one("SELECT json FROM audits WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        an = c.db.one("SELECT json FROM analyses WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        ex = c.db.one("SELECT json FROM executions WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        ps = c.db.rows("SELECT * FROM positions WHERE mint=? ORDER BY id", (mint,))
        marks = []
        for p in ps:
            marks.append({"t": iso(p["opened_at"]), "price": p["entry_price"], "kind": "buy", "label": "Bought"})
            if p["exit_price"] is not None:
                lbl = {"stop": f"-{p['stop_pct']:g}%", "trail": "Trailed out", "timeout": "Timeout", "pulled": "Pulled", "manual": "Sold"}.get(p["exit_reason"], p["exit_reason"])
                marks.append({"t": iso(p["exit_pushed_at"] or p["closed_at"] or time.time()), "price": p["exit_price"], "kind": "sell", "label": lbl})
        openp = [p for p in ps if p["status"] in ("open", "exiting")]
        return {"v": 1, "ts": iso(), "mint": mint, "name": co.get("name"), "symbol": co.get("symbol"), "pool": co.get("pool"),
                "priceUsd": co.get("price"), "holders": co.get("holders"), "status": co.get("status"), "reason": co.get("reason"),
                "migratedAt": iso(co["migrated_at"]) if co.get("migrated_at") else None,
                "link": links.coin_url(mint), "candles": self.candles(mint),
                "audit": json.loads(au["json"]) if au else None, "analyst": json.loads(an["json"]) if an else None,
                "executor": json.loads(ex["json"]) if ex else None,
                "position": self.position_view(openp[-1]) if openp else (self.position_view(ps[-1]) if ps else None),
                "riskLine": ({"label": "Risk trailing stop", "price": self.position_view(openp[-1])["exitLine"], "style": "orange-dashed"} if openp else None),
                "marks": marks}

    def audit(self, mint):
        live = self.d.auditor.live.get(mint)
        if live:
            return {"v": 1, "ts": iso(), "mint": mint, "running": True,
                    "layers": [{k: x[k] for k in ("n", "name", "pass", "value", "source")} for x in live["layers"]],
                    "link": links.coin_url(mint)}
        r = self.c.db.one("SELECT json FROM audits WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if not r:
            return None
        return dict(json.loads(r["json"]), v=1, running=False, link=links.coin_url(mint))

    def account(self):
        c, s = self.c, self.c.s
        ch = c.db.rows("SELECT * FROM change_log ORDER BY id DESC LIMIT 50")
        return {"v": 1, "ts": iso(), "mode": c.mode(), "fomoHandle": s["desk.fomo_handle"],
                "profileUrl": links.profile_url(s["desk.fomo_handle"]), "signupUrl": links.SIGNUP_URL,
                "settings": c.s.specs_public(),
                "changeLog": [{"t": iso(r["ts"]), "key": r["key"], "old": json.loads(r["old"]), "new": json.loads(r["new"]), "source": r["source"]} for r in ch],
                "telegramBot": "@Denarpobot", "editVia": "telegram"}


class Publisher:
    def __init__(self, desk):
        self.d, self.c = desk, desk.ctx
        self.log = self.c.logs["publish"]
        self.builder = Builder(desk)
        self.dirty = asyncio.Event()
        self.last_publish = 0.0
        self.last_ok = None
        self.count = 0

    def mark(self):
        self.dirty.set()

    def write_local(self):
        token = secrets_store.get("APP_TOKEN")
        files = self.builder.build()
        bundle = {}
        for rel, obj in files.items():
            env = crypto.encrypt_obj(obj, token)
            bundle[rel] = env
            p = DATA / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(env))
            os.replace(tmp, p)
        (DATA / "_bundle.json").write_text(json.dumps({"v": 1, "files": bundle}))
        return len(files)

    async def sh(self, *args, timeout=120):
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(ROOT))
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            raise RuntimeError(f"timeout {args[:3]}")
        if p.returncode != 0:
            raise RuntimeError(secrets_store.redact(f"{' '.join(args[:3])} rc={p.returncode} {err.decode()[:300]}"))
        return out.decode()

    async def wait_idle(self, max_s=300):
        t0 = time.time()
        while time.time() - t0 < max_s:
            out = await self.sh("gh", "run", "list", "-R", REPO, "--workflow", "publish.yml", "--limit", "3", "--json", "status")
            if all(r["status"] == "completed" for r in json.loads(out)):
                return True
            await asyncio.sleep(5)
        return False

    async def publish_once(self):
        n = self.write_local()
        gid = secrets_store.get("GIST_ID")
        await self.wait_idle()                               # never overlapping runs
        payload = {"files": {"bundle.json": {"content": (DATA / "_bundle.json").read_text()}}}
        cur = json.loads(await self.sh("gh", "api", f"gists/{gid}", "--jq", "{files: (.files|keys)}"))
        for k in cur.get("files", []):
            if k != "bundle.json":
                payload["files"][k] = None                   # remove legacy loose files
        tmp = DATA / "_patch.json"
        tmp.write_text(json.dumps(payload))
        rev = (await self.sh("gh", "api", "-X", "PATCH", f"gists/{gid}", "--input", str(tmp), "--jq", ".history[0].version")).strip()
        tmp.unlink(missing_ok=True)
        await self.sh("gh", "workflow", "run", "publish.yml", "-R", REPO, "-f", f"rev={rev}")
        self.last_publish = time.time()
        self.count += 1
        self.log.info(f"{iso()}\tpublished\tfiles={n}\trev={rev[:7]}")
        return rev

    async def loop(self):
        hb = self.c.s["desk.publish_heartbeat_s"]
        while True:
            try:
                await asyncio.wait_for(self.dirty.wait(), timeout=max(1, hb - (time.time() - self.last_publish)))
                await asyncio.sleep(DEBOUNCE_S)              # debounce bursts of events
            except asyncio.TimeoutError:
                pass
            self.dirty.clear()
            try:
                await self.publish_once()
                self.last_ok = time.time()
            except Exception as e:
                self.c.error("publisher", "publish", str(e))
                self.log.info(f"{iso()}\tpublish failed\t{secrets_store.redact(str(e))[:200]}")
                await asyncio.sleep(30)
            self.c.hb("publisher", f"count={self.count}")
