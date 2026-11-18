"""Plan B writer: builds the app's JSON files, encrypts each (crypto.py), bundles them into the secret gist,
and dispatches the Pages workflow pinned to that gist revision. Debounced on events + heartbeat; never overlaps."""
from __future__ import annotations
import asyncio, json, os, sys, time, urllib.parse, urllib.request
import datetime as dt
from pathlib import Path
import config, crypto, links, secrets_store
from ctx import AGENTS, ROOT, iso

DATA = ROOT / "data"
REPO = "cdeason12341-cpu/desk"
DEBOUNCE_S = 20
MAX_COIN_FILES = 150
PUBLISH_TIMEOUT_S = 90
SH_TIMEOUT_S = 45
WAIT_IDLE_S = 45
PAGES_ORIGIN = "https://cdeason12341-cpu.github.io/desk"
STALE_LIVE_S = 15 * 60
VERIFY_AFTER_DISPATCH_S = 60
VERIFY_OK_EVERY_S = 300
ALERT_COOLDOWN_S = 30 * 60


class Builder:
    """Pure-ish: reads db/ctx and returns {relpath: obj}. Shapes documented in web/DATA_SHAPES.md."""

    def __init__(self, desk):
        self.d, self.c = desk, desk.ctx

    def build(self) -> dict:
        files = {"state.json": self.state(), "chart.json": self.chart(), "activity.json": self.activity(),
                 "account.json": self.account(), "scores.json": self.scores(), "feed.json": self.feed(),
                 "trades.json": self.trades(), "scanner.json": self.scanner_feed()}
        sb = self.strat_b()
        if sb is not None:
            files["strat_b.json"] = sb
        sl = self.strat_b_live()
        if sl is not None:
            files["strat_b_live.json"] = sl
        files["strat_feed.json"] = self.strat_feed()
        for a in AGENTS:
            files[f"agents/{a}.json"] = self.agent(a)
        for m in self.coin_mints():
            files[f"coin/{m}.json"] = self.coin(m)
            au = self.audit(m)
            if au:
                files[f"audit/{m}.json"] = au
        return files

    def strat_b(self):
        """Strategy B scorecard: its own file, never mixed into trades.json real/paper (DECISION 54). Paper fields at the
        top level, real-money B LIVE results under 'live' (DECISION 66)."""
        try:
            import strat_b
            return strat_b.scorecard(self.c)
        except Exception as e:
            self.c.error("publisher", "strat_b", str(e)[:200])
            return None

    def strat_b_live(self):
        """Strategy B live view: newest evaluations tonight + watchlist (DECISION 60)."""
        try:
            import strat_b
            return strat_b.live_feed(self.c)
        except Exception as e:
            self.c.error("publisher", "strat_b_live", str(e)[:200])
            return None

    # ---- DECISION 72: per-strategy tabs. Profit since each strategy's reset + each strategy's own event feed
    # (A: GMGN LIVE buys / exits / refusals / market-cap approvals; B: B LIVE buys / exits / refusals). No ids, no tx.
    FEED_WINDOW_S = 24 * 3600

    def strat_feed(self):
        import pnl_since
        c, s = self.c, self.c.s
        since_t = time.time() - self.FEED_WINDOW_S
        out = {"v": 1, "ts": iso(), "pnl": {}, "A": [], "B": []}
        try:
            out["pnl"]["A"] = pnl_since.a_live(c.db, s, c.night)
        except Exception as e:
            c.error("publisher", "strat_feed A pnl", str(e)[:200])
        try:
            out["pnl"]["B"] = pnl_since.b_live(c.db, s, c.night)
        except Exception as e:
            c.error("publisher", "strat_feed B pnl", str(e)[:200])

        def ev(t, kind, title, sub, mint, name=None, symbol=None, pnl=None, pct=None, size=None, tone=None):
            return {"t": iso(t) if t else None, "kind": kind, "title": title, "sub": sub, "mint": mint, "name": name,
                    "symbol": symbol, "pnlUsd": None if pnl is None else round(pnl, 2), "pct": pct,
                    "sizeUsd": None if size is None else round(size, 2), "tone": tone,
                    "link": links.coin_url(mint) if mint else None}

        def l8_note(mint):
            from agents.auditor import mint_l8_skipped, L8_SKIP_NOTE
            return f" · {L8_SKIP_NOTE}" if mint and mint_l8_skipped(c, mint) else ""

        A = out["A"]
        for o in c.db.rows("SELECT * FROM gmgn_orders WHERE mode='LIVE' AND (ts>=? OR closed_at>=?) ORDER BY id DESC LIMIT 80",
                           (since_t, since_t)):
            nm = o["name"] or o["symbol"] or (o["mint"] or "")[:6]
            if o["status"] == "refused":
                A.append(ev(o["ts"], "skip", f"Skipped {nm}", o["reason"] or "refused", o["mint"], o["name"], o["symbol"], tone="red"))
                continue
            if o["status"] == "failed":
                A.append(ev(o["ts"], "fail", f"Buy failed · {nm}", o["reason"] or "failed", o["mint"], o["name"], o["symbol"], tone="red"))
                continue
            A.append(ev(o["ts"], "buy", f"Bought {nm}", f"${(o['size_usd'] or 0):,.2f}" + (f" · {o['reason']}" if o["reason"] else "") + l8_note(o["mint"]),
                        o["mint"], o["name"], o["symbol"], size=o["size_usd"], tone="cyan"))
            if o["closed_at"]:
                stop = "stop" in (o["exit_reason"] or "") and "time" not in (o["exit_reason"] or "")
                A.append(ev(o["closed_at"], "stop" if stop else "sell", f"{'Stopped' if stop else 'Sold'} {nm}",
                            (o["exit_reason"] or "closed").replace("_", " "), o["mint"], o["name"], o["symbol"],
                            pnl=o["pnl_usd"], pct=o["pct"], tone="green" if (o["pnl_usd"] or 0) > 0 else "red"))
        try:
            for a in c.db.rows("SELECT * FROM gmgn_approvals WHERE ts>=? ORDER BY id DESC LIMIT 30", (since_t,)):
                nm = a["name"] or a["symbol"] or (a["mint"] or "")[:6]
                A.append(ev(a["ts"], "approval", f"Approval {a['status']} · {nm}",
                            f"market cap ${(a['mc'] or 0):,.0f}" + (f" · score {a['score']}" if a["score"] is not None else ""),
                            a["mint"], a["name"], a["symbol"], tone="amber"))
        except Exception:
            pass
        B = out["B"]
        try:
            rows = c.db.rows("SELECT * FROM strat_b_orders WHERE mode='LIVE' AND (ts>=? OR closed_at>=?) ORDER BY id DESC LIMIT 80",
                             (since_t, since_t))
        except Exception:
            rows = []
        for o in rows:
            nm = o["name"] or o["symbol"] or (o["mint"] or "")[:6]
            t0 = o.get("opened_at") or o.get("ts")
            if o["status"] == "refused":
                B.append(ev(o.get("ts") or t0, "skip", f"Skipped {nm}", o.get("note") or "refused", o["mint"], o["name"], o["symbol"], tone="red"))
                continue
            if o["status"] in ("failed", "unknown"):
                B.append(ev(o.get("ts") or t0, "fail", f"Buy {o['status']} · {nm}", (o.get("note") or "")[:160], o["mint"], o["name"], o["symbol"], tone="red"))
                continue
            B.append(ev(t0, "buy", f"Bought {nm}", f"${(o['size_usd'] or 0):,.2f}", o["mint"], o["name"], o["symbol"],
                        size=o["size_usd"], tone="cyan"))
            if o["status"] == "closed" and o.get("closed_at"):
                stop = "stop" in (o.get("exit_reason") or "") and "time" not in (o.get("exit_reason") or "")
                B.append(ev(o["closed_at"], "stop" if stop else "sell", f"{'Stopped' if stop else 'Sold'} {nm}",
                            (o.get("exit_reason") or "closed").replace("_", " "), o["mint"], o["name"], o["symbol"],
                            pnl=o["pnl_usd"], pct=o["pct"], tone="green" if (o["pnl_usd"] or 0) > 0 else "red"))
        for k in ("A", "B"):
            out[k] = sorted(out[k], key=lambda e: e["t"] or "", reverse=True)[:80]
        return out

    def strat_b_summary(self):
        sb = self.strat_b()
        if sb is None:
            return None
        lv = sb.get("live") or {}
        return {k: sb[k] for k in ("label", "mode", "enabled", "bankroll", "pnlAllUsd", "pnlTonightUsd", "closedAll",
                                   "wins", "losses", "winRatePct", "watching")} | {"openCount": len(sb["open"])} | \
            {"live": {k: lv.get(k) for k in ("label", "active", "pnlAllUsd", "pnlTonightUsd", "closedAll", "wins", "losses",
                                             "winRatePct", "freeSol", "refusedTonight")} | {"openCount": len(lv.get("open") or [])}}

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
                "signupUrl": links.SIGNUP_URL,
                "gmgn": self.gmgn(),
                "realPnl": (lambda side: {"pnlTonightUsd": side["pnlTonightUsd"], "pnlAllUsd": side["pnlAllUsd"],
                                          "closedTonight": side["closedTonight"], "wins": side["wins"],
                                          "losses": side["losses"]})(self.trades()["real"]),
                "stratB": self.strat_b_summary(),
                "push": self.push_status()}

    def push_status(self):
        wp = getattr(self.d, "webpush", None)
        try:
            return wp.status() if wp is not None else None
        except Exception as e:
            self.c.error("publish", "push_status", str(e)[:200])
            return None

    def chart(self):
        since = 0.0
        if self.c.mode() == "PAPER":                    # paper test restarted (DECISION 58): chart starts there, history kept in the DB
            try:
                since = float(self.c.db.meta("paper_test_since") or 0)
            except (TypeError, ValueError):
                since = 0.0
        rows = self.c.db.rows("SELECT ts, balance FROM balance WHERE mode=? AND ts>=? ORDER BY ts DESC LIMIT 500", (self.c.mode(), since))[::-1]
        marks = self.c.db.rows("SELECT mint,name,opened_at,closed_at,exit_reason,pnl_usd,pct,stop_pct FROM positions WHERE mode=? AND opened_at>=? ORDER BY opened_at DESC LIMIT 100", (self.c.mode(), since))
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
            rows = c.db.rows("SELECT * FROM analyses WHERE night=? ORDER BY ts DESC, id DESC LIMIT 50", (n,))
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

    TG_BOT_DEFAULT = "Denarpobot"

    def tg_bot(self):
        """Desk bot @username for the app's override deep links (getMe, cached in meta). Not a secret."""
        return self.c.db.meta("tg_username") or self.TG_BOT_DEFAULT

    def _ov_ctx(self):
        """Inputs for the per-button override flags (DECISION 36/37).
        hard: mints where BOTH buttons hide: any GMGN order tonight not refused/skipped, any FOMO buy tonight that did not
              come from the analyst paper bypass, or an owner override already used.
        paper: mints with an analyst-bypass PAPER buy tonight: only the Paper button hides."""
        n, db = self.c.night, self.c.db
        bypass = {r["mint"] for r in db.rows("SELECT mint FROM signals WHERE night=? AND reason LIKE 'analyst bypass%'", (n,))}
        bypass |= {r["mint"] for r in db.rows("SELECT mint FROM executions WHERE night=? AND reason LIKE 'analyst bypass%'", (n,))}
        bypass |= {r["mint"] for r in db.rows("SELECT mint FROM positions WHERE night=? AND name LIKE 'BYPASS · %'", (n,))}
        fomo = {r["mint"] for r in db.rows("SELECT mint FROM positions WHERE night=? UNION "
                                            "SELECT mint FROM executions WHERE night=? AND status='bought'", (n, n))}
        gm = {r["mint"] for r in db.rows("SELECT mint FROM gmgn_orders WHERE night=? AND status NOT IN ('refused','skipped')", (n,))}
        used = self.d.overrides.used_mints() if getattr(self.d, "overrides", None) else set()
        hard = gm | used | (fomo - bypass)
        latest = {r["mint"]: r["t"] for r in db.rows("SELECT mint, MAX(ts) t FROM analyses WHERE night=? GROUP BY mint", (n,))}
        return {"hard": hard, "paper": bypass, "latest": latest}

    def _ov_flags(self, mint, passed, ts, ov):
        """{'overridable','overridablePaper','overridableReal'}: only on the newest Analyst decision for a coin when it
        was a reject, at most 30 min old, and not blocked (see _ov_ctx)."""
        from overrides import REJECT_MAX_AGE_S
        base = (not passed and ov["latest"].get(mint) == ts and (self.c.clock() - ts) <= REJECT_MAX_AGE_S
                and mint not in ov["hard"])
        real = bool(base)
        paper = bool(base and mint not in ov["paper"])
        return {"overridable": real or paper, "overridablePaper": paper, "overridableReal": real}

    def scores(self):
        """Last ~50 analyst decisions tonight, for the phone app's Analyst scores list."""
        c, s, n = self.c, self.c.s, self.c.night
        rows = c.db.rows("SELECT * FROM analyses WHERE night=? ORDER BY ts DESC, id DESC LIMIT 50", (n,))
        bought = {r["mint"] for r in c.db.rows(
            "SELECT mint FROM positions WHERE night=? UNION "
            "SELECT mint FROM executions WHERE night=? AND status='bought' UNION "
            "SELECT mint FROM gmgn_orders WHERE night=? AND status NOT IN ('refused','skipped')",
            (n, n, n))}
        ov = self._ov_ctx()
        items = []
        for r in rows:
            try:
                body = json.loads(r["json"] or "{}")
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            mint = r["mint"]
            co = c.coin(mint) or {}
            failed = body.get("failed")
            if failed is None:
                try:
                    failed = json.loads(r["failed"]) if r["failed"] else []
                except Exception:
                    failed = []
            if not isinstance(failed, list):
                failed = []
            ans = body.get("answers") if isinstance(body.get("answers"), dict) else {}
            items.append({
                "t": iso(r["ts"]), "mint": mint,
                "mintShort": (mint[:4] + "…" + mint[-4:]) if mint and len(mint) >= 8 else (mint or ""),
                "name": co.get("name") or body.get("name"),
                "symbol": co.get("symbol") or body.get("symbol"),
                "score": r["score"] if r["score"] is not None else body.get("score"),
                "answers": {"alive_6h": ans.get("alive_6h"), "organic": ans.get("organic"),
                            "flush": ans.get("flush"), "chase": ans.get("chase")},
                "passed": bool(r["passed"]), "failed": failed,
                "bought": mint in bought, "link": links.coin_url(mint),
                **self._ov_flags(mint, bool(r["passed"]), r["ts"], ov),
            })
        return {"v": 1, "ts": iso(), "night": n, "minScore": s["analyst.min_score"], "tgBot": self.tg_bot(), "items": items}

    def feed(self):
        """Newest-first live evaluation stream for the phone app, last ~100 events tonight."""
        import re
        c, s, n = self.c, self.c.s, self.c.night
        bars = {"alive_6h": s["analyst.min_alive_6h"], "organic": s["analyst.min_organic"],
                "chase": s["analyst.max_chase"]}

        def short(mint):
            return (mint[:4] + "…" + mint[-4:]) if mint and len(mint) >= 8 else (mint or "")

        def names(mint, body=None):
            co = c.coin(mint) or {}
            body = body if isinstance(body, dict) else {}
            return co.get("name") or body.get("name"), co.get("symbol") or body.get("symbol")

        def body_of(row):
            try:
                b = json.loads(row["json"] or "{}")
            except Exception:
                b = {}
            return b if isinstance(b, dict) else {}

        def recheck_n(reason, body):
            if body.get("recheck") is not None:
                try:
                    return int(body["recheck"])
                except (TypeError, ValueError):
                    pass
            m = re.match(r"recheck\s+(\d+)", reason or "")
            return int(m.group(1)) if m else None

        def item(ts, mint, body, **kw):
            name, symbol = names(mint, body)
            out = {"t": iso(ts), "mint": mint, "mintShort": short(mint), "name": name, "symbol": symbol,
                   "stage": None, "outcome": None, "reason": None, "recheckN": None,
                   "layer": None, "layerName": None, "score": None, "answers": None,
                   "passed": None, "failed": [], "link": links.coin_url(mint), "_ts": ts,
                   "liq": body.get("liquidityUsd") if isinstance(body, dict) else None,
                   "mc": body.get("marketCapUsd") if isinstance(body, dict) else None}
            out.update(kw)
            return out

        items = []
        # entered the waiting room (first seen tonight)
        for r in c.db.rows("SELECT mint,name,symbol,first_seen FROM coins WHERE night=? AND first_seen IS NOT NULL", (n,)):
            items.append(item(r["first_seen"], r["mint"], {"name": r["name"], "symbol": r["symbol"]},
                              stage="room", outcome="waiting", reason="waiting room"))
        for r in c.db.rows("SELECT ts,mint,verdict,reason,json FROM scans WHERE night=?", (n,)):
            b = body_of(r)
            reason = r["reason"] or b.get("reason")
            nchk = recheck_n(reason, b)
            verdict = r["verdict"]
            if verdict == "kill":
                items.append(item(r["ts"], r["mint"], b, stage="scanner", outcome="kill", reason=reason, recheckN=nchk))
            elif verdict == "pass":
                stage = "recheck" if nchk else "scanner"
                items.append(item(r["ts"], r["mint"], b, stage=stage, outcome="pass", reason=reason, recheckN=nchk))
            elif verdict == "recheck":
                waiting = reason and "waiting room" in reason
                items.append(item(r["ts"], r["mint"], b,
                                  stage="room" if waiting else "recheck",
                                  outcome="waiting" if waiting else "recheck",
                                  reason=reason, recheckN=nchk))
        for r in c.db.rows("SELECT ts,mint,passed,reason,layer,json FROM audits WHERE night=?", (n,)):
            b = body_of(r)
            layers = b.get("layers") if isinstance(b.get("layers"), list) else []
            fail = next((L for L in layers if isinstance(L, dict) and L.get("pass") is False), None)
            if r["passed"]:
                l8_skip = bool(b.get("l8_skipped"))
                items.append(item(r["ts"], r["mint"], b, stage="auditor", outcome="pass",
                                  reason=("⚠️ L8 skipped (rpc down)" if l8_skip else None),
                                  layer=r["layer"] or 8,
                                  layerName="8/8 · L8 skipped (rpc down)" if l8_skip else "8/8 clean",
                                  passed=True, l8Skipped=l8_skip))
            else:
                layer = (fail or {}).get("n") or r["layer"]
                layer_name = (fail or {}).get("name")
                items.append(item(r["ts"], r["mint"], b, stage="auditor", outcome="fail",
                                  reason=r["reason"] or (fail or {}).get("value"),
                                  layer=layer, layerName=layer_name, passed=False))
        ov = self._ov_ctx()
        for r in c.db.rows("SELECT * FROM analyses WHERE night=?", (n,)):
            b = body_of(r)
            ans = b.get("answers") if isinstance(b.get("answers"), dict) else {}
            failed = b.get("failed")
            if failed is None:
                try:
                    failed = json.loads(r["failed"]) if r["failed"] else []
                except Exception:
                    failed = []
            if not isinstance(failed, list):
                failed = []
            passed = bool(r["passed"])
            score = r["score"] if r["score"] is not None else b.get("score")
            items.append(item(r["ts"], r["mint"], b, stage="analyst",
                              outcome="buy" if passed else "no_buy",
                              reason=None, score=score,
                              answers={"alive_6h": ans.get("alive_6h"), "organic": ans.get("organic"),
                                       "flush": ans.get("flush"), "chase": ans.get("chase")},
                              passed=passed, failed=failed,
                              **self._ov_flags(r["mint"], passed, r["ts"], ov)))
        for r in c.db.rows("SELECT * FROM gmgn_dips WHERE night=?", (n,)):
            drop = None
            if r["high"] and r["last_price"]:
                drop = round((1 - r["last_price"] / r["high"]) * 100, 1)
            if r["status"] in ("watching", "buying"):
                outcome, why = "watching", (f"need -{s['gmgn.dip_pct']:g}% from high · buy anyway in "
                                            f"{max(0, int((r['expires'] or 0) - c.clock()))}s")
            elif r["status"] == "skipped":
                outcome, why = "skipped", r["reason"] or r["result"]
            elif r["reason"] == "timeout":
                outcome, why = "timeout_buy", r["result"] or "no dip · bought at market"
            else:
                outcome, why = "dip_buy", r["result"] or (f"down {drop:g}% from the high" if drop is not None else "bought the dip")
            items.append(item(r["updated"] or r["ts"], r["mint"], {"name": r["name"], "symbol": r["symbol"]},
                              stage="dip", outcome=outcome, reason=why,
                              high=r["high"], lastPrice=r["last_price"], dropPct=drop,
                              expiresAt=iso(r["expires"]) if r["expires"] else None))
        dips = []
        for r in c.db.rows("SELECT * FROM gmgn_dips WHERE status IN ('watching','buying') ORDER BY ts DESC"):
            drop = None
            if r["high"] and r["last_price"]:
                drop = round((1 - r["last_price"] / r["high"]) * 100, 1)
            dips.append({"mint": r["mint"], "name": r["name"], "symbol": r["symbol"],
                         "high": r["high"], "low": r["low"], "lastPrice": r["last_price"],
                         "startPrice": r["start_price"], "dropPct": drop, "needPct": s["gmgn.dip_pct"],
                         "timeoutS": s["gmgn.dip_timeout_s"],
                         "expiresAt": iso(r["expires"]) if r["expires"] else None,
                         "since": iso(r["ts"]) if r["ts"] else None,
                         "link": links.coin_url(r["mint"])})
        items.sort(key=lambda x: (x["_ts"] is not None, x["_ts"] or 0, x.get("stage") or ""), reverse=True)
        out_items = []
        for it in items[:100]:
            it.pop("_ts", None)
            out_items.append(it)
        return {"v": 1, "ts": iso(), "night": n, "minScore": s["analyst.min_score"], "bars": bars,
                "tgBot": self.tg_bot(), "items": out_items, "dips": dips}

    def scanner_feed(self):
        """Live Scanner view (owner request 2026-11-15 6:25 PM CT): newest ~100 scanner decisions tonight, each check's
        value vs its bar (bars read from settings), last-hour counts, current limits. No secrets."""
        c, s, n = self.c, self.c.s, self.c.night
        now = c.clock()
        liq_bar = float(s["scanner.min_liquidity_usd"]); mc_bar = float(s["scanner.min_market_cap_usd"] or 0)
        top_bar = float(s["scanner.max_top10_pct"]); h1_bar = float(s["scanner.max_h1_change_pct"])

        def chk(key, label, value, bar, op, fmt):
            if bar is None:
                ok = None if value is None else bool(value)
                return {"key": key, "label": label, "value": value, "bar": None, "op": op, "pass": ok, "fmt": fmt}
            if value is None:
                ok = None
            else:
                ok = value >= bar if op == ">=" else value <= bar
            return {"key": key, "label": label, "value": value, "bar": bar, "op": op, "pass": ok, "fmt": fmt}

        import sources

        def src_chk(mint, rs, co):
            """Pump-only: 'Pump mint' (old). All sources: 'Source (<launchpad>)', fails only on an excluded / pump-only kill."""
            if sources.pump_only(s):
                ok = mint.endswith("pump")
                return {"key": "pump", "label": "Pump mint", "value": ok, "bar": None, "op": "is", "pass": ok, "fmt": "bool"}
            ok = not (rs.startswith("excluded") or rs.startswith("not a pump") or rs.startswith("too big") or rs.startswith("old token"))
            lbl = sources.launchpad(mint, co.get("pool_label"))
            return {"key": "pump", "label": f"Source ({lbl})", "value": ok, "bar": None, "op": "is", "pass": ok, "fmt": "bool", "source": lbl}

        items = []
        for r in c.db.rows("SELECT ts, mint, verdict, reason, json FROM scans WHERE night=? ORDER BY ts DESC, id DESC LIMIT 100", (n,)):
            try:
                b = json.loads(r["json"] or "{}")
            except Exception:
                b = {}
            if not isinstance(b, dict):
                b = {}
            mint, reason = r["mint"], r["reason"] or b.get("reason")
            co = c.coin(mint) or {}
            rs = (reason or "").lower()
            sol_q = False if rs.startswith("not sol-quoted") else (None if b.get("liquidityUsd") is None and not b.get("pool") else True)
            num = lambda k: float(b[k]) if isinstance(b.get(k), (int, float)) else None
            checks = [
                chk("liq", "Liquidity", num("liquidityUsd"), liq_bar, ">=", "usd"),
                chk("mc", "Market cap", num("marketCapUsd"), mc_bar if mc_bar > 0 else None, ">=", "usd") if mc_bar > 0
                else {"key": "mc", "label": "Market cap", "value": num("marketCapUsd"), "bar": None, "op": "off", "pass": None, "fmt": "usd"},
                chk("top10", "Top 10 hold", num("top10Pct"), top_bar, "<=", "pct"),
                chk("h1", "1h change", num("h1ChangePct"), h1_bar, "<=", "pct"),
                src_chk(mint, rs, co),
                {"key": "sol", "label": "SOL-quoted", "value": sol_q, "bar": None, "op": "is", "pass": sol_q, "fmt": "bool"},
            ]
            v = r["verdict"]
            nchk = b.get("recheck")
            nxt = b.get("recheckAt")
            if v == "pass":
                pill = "pass"
            elif "waiting room" in rs:
                pill = "waiting"
            elif nxt:
                pill = "recheck"
            else:
                pill = "kill"
            items.append({"t": iso(r["ts"]), "mint": mint, "mintShort": f"{mint[:4]}…{mint[-4:]}",
                          "name": b.get("name") or co.get("name"), "symbol": b.get("symbol") or co.get("symbol"),
                          "pill": pill, "verdict": v, "recheckN": nchk, "nextRecheckN": (nchk or 0) + 1 if nxt else None,
                          "nextAt": nxt, "final": bool(nchk) and not nxt and v != "pass",
                          "reason": None if v == "pass" else reason, "ageMin": b.get("ageMin"),
                          "checks": checks, "link": links.coin_url(mint),
                          "surge": b.get("surge") if isinstance(b.get("surge"), dict) else None})
        hr = now - 3600
        one = lambda q, a: c.db.one(q, a)["n"]
        counts = {
            "seen": one("SELECT COUNT(DISTINCT mint) n FROM scans WHERE ts>=?", (hr,)),
            "passed": one("SELECT COUNT(*) n FROM scans WHERE ts>=? AND verdict='pass'", (hr,)),
            "killed": one("SELECT COUNT(*) n FROM scans WHERE ts>=? AND verdict='kill'", (hr,)),
            "rechecking": c.db.one("SELECT COUNT(*) n FROM coins WHERE status='recheck'")["n"],     # queued now (as the heartbeat)
            "surges": one("SELECT COUNT(*) n FROM surges WHERE ts>=?", (hr,)),
        }
        limits = {"minLiquidityUsd": liq_bar, "minMarketCapUsd": mc_bar, "maxTop10Pct": top_bar, "maxH1ChangePct": h1_bar,
                  "minPoolAgeMin": s["scanner.min_pool_age_min"], "maxAgeH": s["scanner.max_age_h"],
                  "recheckMinutes": s["scanner.recheck_minutes"],
                  "nearMiss": {"minLiqUsd": s["scanner.recheck_min_liq_usd"], "minMcUsd": s["scanner.recheck_min_mc_usd"],
                               "maxTop10Pct": s["scanner.recheck_max_top10_pct"], "maxH1Pct": s["scanner.recheck_max_h1_pct"]},
                  "youngPriorityMin": s["scanner.young_priority_min"],
                  "surge": {"pct": s["scanner.surge_pct"], "windowS": s["scanner.surge_window_s"],
                            "watchMax": s["scanner.surge_watch_max"], "pollS": s["scanner.surge_poll_s"]}}
        re = getattr(self.d, "reentry", None)
        reentry = re.feed() if re is not None else []
        limits["reentry"] = {"enabled": s["gmgn.reentry_enabled"], "watchMin": s["gmgn.reentry_watch_min"],
                             "bouncePct": s["gmgn.reentry_bounce_pct"], "maxPerCoin": s["gmgn.reentry_max_per_coin"],
                             "pollS": s["gmgn.reentry_poll_s"], "maxLiqDropPct": s["gmgn.reentry_max_liq_drop_pct"]}
        return {"v": 1, "ts": iso(), "night": n, "counts1h": counts, "limits": limits, "items": items, "reentry": reentry}

    def coin_mints(self):
        n = self.c.night
        rows = self.c.db.q("SELECT DISTINCT mint FROM (SELECT mint FROM audits WHERE night=? UNION SELECT mint FROM positions WHERE night=? OR status IN ('open','exiting') UNION SELECT mint FROM gmgn_orders WHERE night=? OR status IN ('open','exiting','pending','submitting') UNION SELECT mint FROM gmgn_dips WHERE night=? OR status IN ('watching','buying'))",
                           (n, n, n, n))
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
                "priceUsd": (c.db.one("SELECT price FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)) or {"price": co.get("price")})["price"], "holders": co.get("holders"), "status": co.get("status"), "reason": co.get("reason"),
                "migratedAt": iso(co["migrated_at"]) if co.get("migrated_at") else None,
                "coin_url": links.coin_url(mint, "solana"), "link": links.coin_url(mint, "solana"), "candles": self.candles(mint),
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
        return dict(json.loads(r["json"]), v=1, running=False, link=links.coin_url(mint), coin_url=links.coin_url(mint))

    def account(self):
        c, s = self.c, self.c.s
        ch = c.db.rows("SELECT * FROM change_log ORDER BY id DESC LIMIT 50")
        return {"v": 1, "ts": iso(), "mode": c.mode(), "stack": round(c.balance(), 2),
                "profile_url": links.profile_url(s["desk.fomo_handle"]), "SIGNUP_URL": links.SIGNUP_URL,
                "fomoHandle": s["desk.fomo_handle"],
                "profileUrl": links.profile_url(s["desk.fomo_handle"]), "signupUrl": links.SIGNUP_URL,
                "settings": c.s.specs_public(),
                "changeLog": [{"t": iso(r["ts"]), "key": r["key"], "old": json.loads(r["old"]), "new": json.loads(r["new"]), "source": r["source"]} for r in ch],
                "telegramBot": "@Denarpobot", "editVia": "telegram",
                "gmgn": self.gmgn()}


    def gmgn_position(self, o):
        s = self.c.s
        entry, last = o["entry_price"], o["last_price"] or o["entry_price"]
        size = o["size_usd"] or 0
        pnl = pct = None
        if entry and last and size:
            pnl = round(size * (last / entry - 1), 2)
            pct = round((last / entry - 1) * 100, 2)
        attached = bool(o.get("strategy_order_id")) or o.get("mode") == "PAPER"
        from agents.gmgn_trader import exits_label, grace_is_extended, grace_phase
        now = self.c.clock()
        phase = grace_phase(s, o)
        grace_on = phase == "grace"
        extended = grace_is_extended(s, o, now)
        return {"id": o["id"], "mint": o["mint"], "name": o["name"], "symbol": o["symbol"],
                "mode": o["mode"], "status": o["status"], "entryPrice": entry, "lastPrice": last,
                "peakPrice": o["peak"], "sizeUsd": size, "pct": pct, "pnlUsd": pnl,
                "openedAt": iso(o["ts"]) if o["ts"] else None,
                "exits": {"stopPct": s["risk.grace_stop_pct"] if grace_on else s["risk.stop_pct"],
                          "trailPct": s["risk.trail_pct"], "trailStartPct": s["risk.trail_start_pct"],
                          "attached": bool(attached), "repair": o.get("repair") or None,
                          "phase": phase, "graceMin": int(s["risk.grace_min"]) if grace_on else 0,
                          "graceStopPct": s["risk.grace_stop_pct"], "normalStopPct": s["risk.stop_pct"],
                          "graceUntil": iso(o["grace_until"]) if o.get("grace_until") else None,
                          "graceExtended": extended,
                          "label": exits_label(s, phase, o=o, now=now)},
                "link": links.coin_url(o["mint"]) if o["mint"] else None}

    def gmgn_order(self, o):
        """Public trade row. Never includes quote, body, conditions, tx, or ids from the broker."""
        return {"id": o["id"], "t": iso(o["ts"]) if o["ts"] else None, "mint": o["mint"],
                "name": o["name"], "symbol": o["symbol"], "mode": o["mode"], "status": o["status"],
                "reason": o["reason"], "sizeUsd": o["size_usd"], "entryPrice": o["entry_price"],
                "lastPrice": o["last_price"] or o["entry_price"], "exitPrice": o["exit_price"],
                "exitReason": o["exit_reason"], "pnlUsd": o["pnl_usd"], "pct": o["pct"],
                "openedAt": iso(o["ts"]) if o["ts"] else None,
                "closedAt": iso(o["closed_at"]) if o["closed_at"] else None,
                "link": links.coin_url(o["mint"]) if o["mint"] else None}

    def gmgn(self):
        """Read-only GMGN wallet + trades for the phone app. No secrets, no order bodies."""
        from agents.gmgn_trader import night_usage, ACTIVE, paper_snapshot, budget_view
        from gmgn import MAIN_WALLET
        c, s = self.c, self.c.s
        g = getattr(self.d, "gmgn", None)
        snap = g.wallet_snapshot() if g and hasattr(g, "wallet_snapshot") else {
            "sol": None, "usd": None, "solUsd": None, "updatedAt": None}
        mode = s["gmgn.mode"]
        n, used = night_usage(c.db, c.night, mode, "A")
        cap = float(s["gmgn.cap_trade_usd"])
        tickets = int(s["gmgn.max_tickets_night"])
        placeholders = ",".join("?" * len(ACTIVE))
        open_rows = c.db.rows(f"SELECT * FROM gmgn_orders WHERE status IN ({placeholders}) ORDER BY ts", ACTIVE)
        recent = c.db.rows("SELECT * FROM gmgn_orders ORDER BY id DESC LIMIT 20")
        live_recent = c.db.rows("SELECT * FROM gmgn_orders WHERE mode='LIVE' AND status NOT IN ('refused') ORDER BY id DESC LIMIT 20")
        lam = getattr(g, "_bal", (0, None))[1] if g is not None else None
        budgets = {k: budget_view(s, c.db, k, lam, snap.get("solUsd")) for k in ("A", "B")}      # DECISION 71
        ps = paper_snapshot(s, c.db)
        paper = {k: ps[k] for k in ("bankrollUsd", "tradeUsd", "pnlUsd", "inPositionsUsd", "freeUsd")}
        paper["since"] = iso(ps["since"]) if ps["since"] else None
        return {"mode": mode, "enabled": bool(s["gmgn.enabled"]),
                "halted": bool(c.db.meta("gmgn_halted", False)),
                "wallet": MAIN_WALLET, "walletShort": f"{MAIN_WALLET[:4]}…{MAIN_WALLET[-4:]}",
                "sol": snap.get("sol"), "usd": snap.get("usd"), "solUsd": snap.get("solUsd"),
                "updatedAt": snap.get("updatedAt"),
                "capTradeUsd": cap, "ticketsUsed": n, "ticketsMax": tickets,
                "nightSpendUsd": round(used, 2), "nightCapUsd": round(cap * tickets, 2),
                "reserveSol": s["gmgn.sol_reserve"],
                "paper": paper,
                "bMode": s["strat_b.mode"], "budgets": budgets,
                "open": [self.gmgn_position(r) for r in open_rows],
                "liveOrders": [self.gmgn_order(r) for r in live_recent],
                "orders": [self.gmgn_order(r) for r in recent]}

    BYPASS_PREFIX = "BYPASS · "
    TAG_PREFIXES = ("BYPASS · ", "OVERRIDE · ")

    def _bypass_map(self):
        """(mint, night) -> (tag, analyst score) for FOMO PAPER fills made through analyst.paper_bypass (DECISION 35,
        tag BYPASS) or an owner paper override (DECISION 36, tag OVERRIDE)."""
        rows = self.c.db.rows("SELECT s.mint, s.night, s.reason, (SELECT a.score FROM analyses a WHERE a.mint=s.mint AND a.night=s.night"
                              " ORDER BY a.id DESC LIMIT 1) score FROM signals s WHERE s.reason LIKE 'analyst bypass%' OR s.reason LIKE 'owner override%'")
        return {(r["mint"], r["night"]): ("OVERRIDE" if (r["reason"] or "").startswith("owner override") else "BYPASS", r["score"]) for r in rows}


    # Home-screen Real P&L can be reset to a timestamp + optional manual seed (meta
    # gmgn_pnl_baseline_ts / gmgn_pnl_manual_usd) without deleting trade history.
    def _pnl_baseline(self, mode):
        """(since_ts, manual_usd, manual_wins, seed_tonight) for LIVE home totals. PAPER: (paper_test_since, 0, 0, 0)
        once the owner restarted the paper test (DECISION 58), else (None, 0, 0, 0)."""
        if mode == "PAPER":
            try:
                since = float(self.c.db.meta("paper_test_since"))
            except (TypeError, ValueError):
                return None, 0.0, 0, 0.0
            return since, 0.0, 0, 0.0
        if mode != "LIVE":
            return None, 0.0, 0, 0.0
        import pnl_since                                     # DECISION 72: owner reset of A's profit; replaces the old meta seed
        ts72 = pnl_since.since(self.c.s, "A")
        if ts72 is not None:
            return ts72, 0.0, 0, 0.0
        raw = self.c.db.meta("gmgn_pnl_baseline_ts")
        if raw is None:
            return None, 0.0, 0, 0.0
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            return None, 0.0, 0, 0.0
        try:
            adj = float(self.c.db.meta("gmgn_pnl_manual_usd", 0) or 0)
        except (TypeError, ValueError):
            adj = 0.0
        try:
            wins = int(self.c.db.meta("gmgn_pnl_manual_wins", 0) or 0)
        except (TypeError, ValueError):
            wins = 0
        # Seed counts toward "tonight" only while the night that was open at the reset is still current.
        night_start = (self.c.db.one("SELECT started_at FROM nights WHERE id=?", (self.c.night,)) or {}).get("started_at")
        seed_tonight = adj if night_start is None or float(night_start) <= ts else 0.0
        return ts, adj, wins, seed_tonight

    def trades(self):

        """Real | Paper split for the app. Real = GMGN LIVE orders + LIVE positions (real money). Paper = every PAPER
        trade: FOMO paper positions (bypass ones flagged with their analyst score) and GMGN PAPER orders. No secrets."""
        from agents.gmgn_trader import ACTIVE
        c = self.c
        bmap = self._bypass_map()

        def pos_row(p):
            v = self.position_view(p)
            v.update(source="FOMO", closedAt=iso(p["closed_at"]) if p["closed_at"] else None, pctClosed=p["pct"],
                     open=p["status"] in ("open", "exiting"), bypass=False, score=None, tag=None)
            nm = v["name"] or ""
            pre = next((x for x in self.TAG_PREFIXES if nm.startswith(x)), None)
            if (p["mint"], p["night"]) in bmap or pre:
                tag, score = bmap.get((p["mint"], p["night"]), (pre.split(" ")[0] if pre else "BYPASS", None))
                v.update(bypass=True, tag=tag, score=score)
                if pre:
                    v["name"] = nm[len(pre):]
            if not v["open"] and p["pct"] is not None:
                v["pct"] = p["pct"]
            return v

        def gm_row(o):
            v = self.gmgn_order(o) if o["status"] not in ACTIVE else dict(self.gmgn_order(o), **self.gmgn_position(o))
            ov = (o["reason"] or "").startswith("owner override")
            v.update(source="GMGN", open=o["status"] in ACTIVE, bypass=ov, score=None, tag="OVERRIDE" if ov else None)
            if ov:
                a = c.db.one("SELECT score FROM analyses WHERE mint=? ORDER BY id DESC LIMIT 1", (o["mint"],))
                v["score"] = a["score"] if a else None
            return v

        def side(mode):
            pos = c.db.rows("SELECT * FROM positions WHERE mode=? ORDER BY opened_at DESC LIMIT 60", (mode,))
            gm = c.db.rows("SELECT * FROM gmgn_orders WHERE mode=? AND status NOT IN ('refused','failed') ORDER BY id DESC LIMIT 60", (mode,))
            rows = [pos_row(p) for p in pos] + [gm_row(o) for o in gm]
            rows.sort(key=lambda r: r.get("openedAt") or r.get("t") or "", reverse=True)
            closed = [r for r in rows if not r["open"] and r.get("pnlUsd") is not None]
            since, manual, manual_wins, seed_tonight = self._pnl_baseline(mode)
            if since is None:
                tonight_pos = c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p, COUNT(*) n FROM positions WHERE mode=? AND night=? AND status='closed'", (mode, c.night))
                tonight_gm = c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p, COUNT(*) n FROM gmgn_orders WHERE mode=? AND night=? AND closed_at IS NOT NULL", (mode, c.night))
                all_pos = c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM positions WHERE mode=? AND status='closed'", (mode,))
                all_gm = c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM gmgn_orders WHERE mode=? AND closed_at IS NOT NULL", (mode,))
                wins = sum(1 for r in closed if (r["pnlUsd"] or 0) > 0)
                losses = sum(1 for r in closed if (r["pnlUsd"] or 0) <= 0)
            else:
                tonight_pos = c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p, COUNT(*) n FROM positions WHERE mode=? AND night=? AND status='closed' AND closed_at>=?", (mode, c.night, since))
                tonight_gm = c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p, COUNT(*) n FROM gmgn_orders WHERE mode=? AND night=? AND closed_at IS NOT NULL AND closed_at>=?", (mode, c.night, since))
                all_pos = c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM positions WHERE mode=? AND status='closed' AND closed_at>=?", (mode, since))
                all_gm = c.db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM gmgn_orders WHERE mode=? AND closed_at IS NOT NULL AND closed_at>=?", (mode, since))
                wl = c.db.one("SELECT COALESCE(SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END),0) w, "
                              "COALESCE(SUM(CASE WHEN pnl_usd<=0 THEN 1 ELSE 0 END),0) l FROM ("
                              "SELECT pnl_usd FROM positions WHERE mode=? AND status='closed' AND closed_at>=? "
                              "UNION ALL SELECT pnl_usd FROM gmgn_orders WHERE mode=? AND closed_at IS NOT NULL AND closed_at>=?)",
                              (mode, since, mode, since))
                wins = int(wl["w"] or 0) + int(manual_wins or 0)
                losses = int(wl["l"] or 0)
            return {"open": [r for r in rows if r["open"]], "closed": [r for r in rows if not r["open"]][:40],
                    "pnlTonightUsd": round(seed_tonight + (tonight_pos["p"] or 0) + (tonight_gm["p"] or 0), 2),
                    "pnlAllUsd": round(manual + (all_pos["p"] or 0) + (all_gm["p"] or 0), 2),
                    "closedTonight": (tonight_pos["n"] or 0) + (tonight_gm["n"] or 0),
                    "wins": wins, "losses": losses,
                    "since": (__import__("pnl_since").iso(since) if (mode == "LIVE" and since is not None) else None)}

        real, paper = side("LIVE"), side("PAPER")
        paper["bypassOn"] = bool(c.s["analyst.paper_bypass"])
        paper["bypassCount"] = sum(1 for r in paper["open"] + paper["closed"] if r["tag"] == "BYPASS")
        paper["overrideCount"] = sum(1 for r in paper["open"] + paper["closed"] if r["tag"] == "OVERRIDE")
        real["overrideCount"] = sum(1 for r in real["open"] + real["closed"] if r["tag"] == "OVERRIDE")
        return {"v": 1, "ts": iso(), "deskMode": c.mode(), "gmgnMode": c.s["gmgn.mode"], "real": real, "paper": paper}

class Publisher:
    def __init__(self, desk):
        self.d, self.c = desk, desk.ctx
        self.log = self.c.logs["publish"]
        self.builder = Builder(desk)
        self.dirty = asyncio.Event()
        self.last_publish = 0.0
        self.last_ok = None
        self.count = 0
        self.next_verify_at = 0.0
        self.last_stale_alert = 0.0
        self.last_live_ts = None

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

    async def _kill_proc(self, p):
        if p is None or p.returncode is not None:
            return
        try:
            p.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(p.wait(), 5)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass

    async def sh(self, *args, timeout=SH_TIMEOUT_S):
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(ROOT))
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            await self._kill_proc(p)
            raise RuntimeError(f"timeout {args[:3]}")
        except asyncio.CancelledError:
            await self._kill_proc(p)
            raise
        if p.returncode != 0:
            raise RuntimeError(secrets_store.redact(f"{' '.join(args[:3])} rc={p.returncode} {err.decode()[:300]}"))
        return out.decode()

    async def wait_idle(self, max_s=WAIT_IDLE_S):
        t0 = time.time()
        while True:
            self.c.hb("publisher", f"wait_idle count={self.count}")
            out = await self.sh("gh", "run", "list", "-R", REPO, "--workflow", "publish.yml", "--limit", "3", "--json", "status")
            if all(r["status"] == "completed" for r in json.loads(out)):
                return True
            if time.time() - t0 >= max_s:
                return False
            await asyncio.sleep(5)

    WALLET_REFRESH_S = 30            # DECISION 73: wallet + SOL price re-read at least this often, and right after any fill / exit

    async def refresh_gmgn(self):
        g = getattr(self.d, "gmgn", None)
        if g is None or not hasattr(g, "refresh_wallet"):
            return
        try:
            await asyncio.wait_for(g.refresh_wallet(min_age=self.WALLET_REFRESH_S), timeout=25)
        except Exception as e:
            self.c.error("publisher", "gmgn", str(e)[:200])

    async def idle_wallet(self):
        """While idle: refresh the wallet when it's stale (>= WALLET_REFRESH_S or after a fill / exit) and publish when
        the balance or SOL price changed, so the Wallet / Strat cards follow the real wallet."""
        g = getattr(self.d, "gmgn", None)
        if g is None or not hasattr(g, "refresh_wallet") or getattr(g, "client", None) is None:
            return False
        ts, before = getattr(g, "_bal", (0.0, None))
        sol_before = getattr(g, "_sol", (0.0, None))[1]
        if not getattr(g, "_bal_stale", False) and before is not None and time.time() - ts < self.WALLET_REFRESH_S:
            return False
        await self.refresh_gmgn()
        after, sol_after = g._bal[1], g._sol[1]
        changed = after != before or (sol_before and sol_after and abs(sol_after / sol_before - 1) > 0.002)
        if changed:
            self.dirty.set()
        return bool(changed)

    def _parse_ts(self, ts):
        if not ts:
            return None
        try:
            return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
        except Exception:
            return None

    def fetch_live_state(self):
        """Decrypt the live Pages state.json. Never log the URL (it contains DATA_PATH)."""
        token, dpath = secrets_store.get("APP_TOKEN"), secrets_store.get("DATA_PATH")
        url = f"{PAGES_ORIGIN}/d/{urllib.parse.quote(dpath)}/state.json?t={time.time()}"
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.load(r)
        return crypto.decrypt_obj(raw, token)

    async def _alert_stale(self, ts, age):
        now = time.time()
        if self.last_stale_alert and now - self.last_stale_alert < ALERT_COOLDOWN_S:
            return
        self.last_stale_alert = now
        mins = max(1, int(age // 60))
        msg = (f"Phone app data is {mins} min old"
               + (f" (live ts {ts})" if ts else "")
               + ". The desk is still publishing; I'm retrying the deploy.")
        await self.c.notifier.push(msg, kind="info")

    async def verify_live(self):
        """Read back deployed state.json. Log and alert if it's more than 15 minutes old."""
        try:
            obj = await asyncio.to_thread(self.fetch_live_state)
            ts = obj.get("ts") if isinstance(obj, dict) else None
            parsed = self._parse_ts(ts)
            age = (time.time() - parsed) if parsed is not None else 10**9
            self.last_live_ts = ts
            if age <= STALE_LIVE_S:
                self.log.info(f"{iso()}\tdeploy ok\tlive_ts={ts}\tageS={int(age)}")
                return True
            self.log.info(f"{iso()}\tdeploy stale\tlive_ts={ts}\tageS={int(age)}")
            await self._alert_stale(ts, age)
            self.dirty.set()
            return False
        except Exception as e:
            self.c.error("publisher", "verify", str(e))
            self.log.info(f"{iso()}\tdeploy verify failed\t{secrets_store.redact(str(e))[:200]}")
            return False

    async def maybe_verify(self):
        if not self.count or time.time() < self.next_verify_at:
            return
        ok = await self.verify_live()
        self.next_verify_at = time.time() + (60 if not ok else VERIFY_OK_EVERY_S)

    async def publish_once(self):
        await self.refresh_gmgn()
        n = self.write_local()
        gid = secrets_store.get("GIST_ID")
        idle = await self.wait_idle()                        # never overlapping runs
        payload = {"files": {"bundle.json": {"content": (DATA / "_bundle.json").read_text()}}}
        cur = json.loads(await self.sh("gh", "api", f"gists/{gid}", "--jq", "{files: (.files|keys)}"))
        for k in cur.get("files", []):
            if k != "bundle.json":
                payload["files"][k] = None                   # remove legacy loose files
        tmp = DATA / "_patch.json"
        tmp.write_text(json.dumps(payload))
        rev = (await self.sh("gh", "api", "-X", "PATCH", f"gists/{gid}", "--input", str(tmp), "--jq", ".history[0].version")).strip()
        tmp.unlink(missing_ok=True)
        if not idle:
            self.dirty.set()
            self.last_publish = time.time()
            self.count += 1
            self.log.info(f"{iso()}\tgist updated, deploy busy, deferred\tfiles={n}\trev={rev[:7]}")
            return rev
        await self.sh("gh", "workflow", "run", "publish.yml", "-R", REPO, "-f", f"rev={rev}")
        self.last_publish = time.time()
        self.count += 1
        self.next_verify_at = time.time() + VERIFY_AFTER_DISPATCH_S
        self.log.info(f"{iso()}\tpublished\tfiles={n}\trev={rev[:7]}")
        return rev

    async def wallet_loop(self):
        """DECISION 73: runs beside the publish loop (also while a publish is in flight): wallet re-read every
        WALLET_REFRESH_S, or within 5 s after a fill / exit; a change triggers a publish."""
        while True:
            try:
                await self.idle_wallet()
            except Exception as e:
                self.c.error("publisher", "wallet_loop", str(e)[:200])
            await asyncio.sleep(5)

    async def loop(self):
        hb = self.c.s["desk.publish_heartbeat_s"]
        if getattr(self, "_wallet_task", None) is None:
            self._wallet_task = asyncio.create_task(self.wallet_loop())
        while True:
            try:
                deadline = time.time() + max(1, hb - (time.time() - self.last_publish))
                while time.time() < deadline and not self.dirty.is_set():
                    self.c.hb("publisher", f"idle count={self.count}")
                    await self.maybe_verify()
                    try:
                        await asyncio.wait_for(self.dirty.wait(), timeout=min(15, max(0.1, deadline - time.time())))
                    except asyncio.TimeoutError:
                        continue
                if self.dirty.is_set():
                    await asyncio.sleep(DEBOUNCE_S)          # debounce bursts of events
            except asyncio.TimeoutError:
                pass
            self.dirty.clear()
            try:
                self.c.hb("publisher", f"starting count={self.count}")
                await asyncio.wait_for(self.publish_once(), timeout=PUBLISH_TIMEOUT_S)
                self.last_ok = time.time()
            except Exception as e:
                self.c.error("publisher", "publish", str(e))
                self.log.info(f"{iso()}\tpublish failed\t{secrets_store.redact(str(e))[:200]}")
                await asyncio.sleep(30)
            self.c.hb("publisher", f"count={self.count}")
            await self.maybe_verify()
