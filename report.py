"""Night summary data for the morning Page (the Page itself is written by the night-report routine).
CLI:  python report.py [night_id] [--clear-skips]  -> prints JSON summary."""
from __future__ import annotations
import json, sys, time
import links
from ctx import ROOT, iso
from db import DB


def summary(db: DB, night: str) -> dict:
    pos = db.rows("SELECT * FROM positions WHERE night=? ORDER BY opened_at", (night,))
    fmt = lambda p: {"mint": p["mint"], "name": p["name"], "link": links.coin_url(p["mint"]), "mode": p["mode"],
                     "entry": p["entry_price"], "exit": p["exit_price"], "pnlUsd": p["pnl_usd"], "pct": p["pct"],
                     "rule": p["exit_reason"], "status": p["status"], "feesUsd": p["fees_usd"],
                     "openedAt": iso(p["opened_at"]), "closedAt": iso(p["closed_at"]) if p["closed_at"] else None}
    closed = [p for p in pos if p["status"] == "closed"]
    red = [fmt(p) for p in closed if (p["pnl_usd"] or 0) < 0]
    green = [fmt(p) for p in closed if (p["pnl_usd"] or 0) >= 0]
    kills = {}
    for r in db.rows("SELECT reason FROM scans WHERE night=? AND verdict='kill'", (night,)):
        k = (r["reason"] or "").split(" ")[0] if (r["reason"] or "").startswith(("liq", "+")) else (r["reason"] or "")
        k = "liq" if k.startswith("liq") else ("h1 pump" if k.startswith("+") else ("copycat" if k.startswith("copycat") else k))
        kills[k] = kills.get(k, 0) + 1
    drops = db.one("SELECT COUNT(*) n FROM coins WHERE night=? AND status='dropped'", (night,))["n"]
    if drops:
        kills["never qualified"] = drops
    aud = [{"mint": r["mint"], "link": links.coin_url(r["mint"]), "layer": r["layer"], "reason": r["reason"], "t": iso(r["ts"])}
           for r in db.rows("SELECT * FROM audits WHERE night=? AND passed=0 ORDER BY ts", (night,))]
    by_layer = {}
    for a in aud:
        by_layer.setdefault(a["layer"], []).append(a)
    an = db.rows("SELECT * FROM analyses WHERE night=? ORDER BY ts", (night,))
    rej = [{"mint": r["mint"], "link": links.coin_url(r["mint"]), "failed": json.loads(r["failed"] or "[]")[:1],
            "score": r["score"], "model": r["model"], "t": iso(r["ts"])} for r in an if not r["passed"]]
    return {"night": night, "generatedAt": iso(),
            "layer8Kills": int(db.meta("layer8_kills_" + night, 0) or 0),
            "redTrades": red, "greenTrades": green,
            "openTrades": [fmt(p) for p in pos if p["status"] != "closed"],
            "coinsSeen": db.one("SELECT COUNT(*) n FROM coins WHERE night=?", (night,))["n"],
            "scannerKills": kills, "auditorRefusals": {str(k): v for k, v in sorted(by_layer.items())},
            "analystRejects": rej,
            "jev": {"calls": sum(1 for r in an if r["model"] != "rules"),
                    "inputTokens": sum(r["input_tokens"] or 0 for r in an),
                    "outputTokens": sum(r["output_tokens"] or 0 for r in an),
                    "costUsd": None, "costNote": "Jev price per token not known to the desk"},
            "feesUsd": round(sum(p["fees_usd"] or 0 for p in closed), 4),
            "pnlUsd": round(sum(p["pnl_usd"] or 0 for p in closed), 2),
            "skips": db.rows("SELECT ts, mint, agent, reason FROM skips WHERE night=?", (night,)),
            "footer": "New to FOMO? " + links.SIGNUP_URL}


def cutoff(settings, now):
    """Today's report_time as a local datetime."""
    import datetime as dt
    hh, mm = (int(x) for x in str(settings["desk.report_time"]).split(":"))
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def default_night(db: DB, settings=None, now=None) -> str | None:
    """Night the morning report should cover (DECISION 59). Normally the most recently closed night. If the desk
    hasn't rolled yet today although report_time has passed (routine ran first, or the box was paused at 8 AM),
    the still-open current night is last night, so report that one instead of re-reporting an older night."""
    import datetime as dt
    last = (db.one("SELECT id FROM nights WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1") or {}).get("id")
    cur = db.meta("night")
    if settings is not None and cur:
        now = now or dt.datetime.now()
        r = db.one("SELECT started_at, ended_at FROM nights WHERE id=?", (cur,))
        due = db.meta("last_rollover") != now.date().isoformat() and now >= cutoff(settings, now)
        if due and r and r["ended_at"] is None and dt.datetime.fromtimestamp(float(r["started_at"])) < cutoff(settings, now):
            return cur
    return last or cur


def close_night(db: DB, night: str):
    s = summary(db, night)
    (ROOT / "logs" / "reports").mkdir(parents=True, exist_ok=True)
    (ROOT / "logs" / "reports" / f"{night}.json").write_text(json.dumps(s, indent=1))
    db.x("UPDATE nights SET ended_at=? WHERE id=?", (time.time(), night))
    return s


def clear_skips(db: DB, night: str | None = None):
    if night:
        db.x("DELETE FROM skips WHERE night=?", (night,))
    else:
        db.x("DELETE FROM skips")


if __name__ == "__main__":
    db = DB(str(ROOT / "desk.db"))
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    import config
    night = args[0] if args else default_night(db, config.Settings(db))
    print(json.dumps(summary(db, night), indent=1, default=str))
    if "--clear-skips" in sys.argv:
        clear_skips(db, night)
