"""DECISION 72 (owner 2026-11-17 6:20 PM CT: "reset the profits for a and b"). Display-only per-strategy P&L baseline.

gmgn.pnl_since (Strategy A LIVE) and strat_b.pnl_since (Strategy B LIVE) hold a UTC ISO time ("" = no baseline).
Realized P&L, wins / losses, closed counts and "total profit" in the app and in Telegram status count only trades
whose close time is at or after the baseline. A position opened before the baseline counts when it closes after it.
Nothing is deleted. Risk code (daily loss stops, night P&L, caps, sizing) never calls this module.
"""
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KEYS = {"A": "gmgn.pnl_since", "B": "strat_b.pnl_since"}
CT = ZoneInfo("America/Chicago")


def parse(raw) -> float | None:
    """UTC ISO string -> epoch seconds, else None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def since(s, strategy: str) -> float | None:
    try:
        return parse(s[KEYS[strategy]])
    except KeyError:
        return None


def label(ts: float | None) -> str | None:
    """'Nov 17 6:20 PM CT' for app / Telegram text."""
    if ts is None:
        return None
    d = datetime.fromtimestamp(ts, CT)
    return d.strftime("%b %-d %-I:%M %p CT")


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize(pnls: list[float], tonight: list[float]) -> dict:
    wins = [p for p in pnls if (p or 0) > 0]
    return {"pnlUsd": round(sum(p or 0 for p in pnls), 2), "pnlTonightUsd": round(sum(p or 0 for p in tonight), 2),
            "closed": len(pnls), "closedTonight": len(tonight), "wins": len(wins), "losses": len(pnls) - len(wins),
            "winRatePct": round(100 * len(wins) / len(pnls), 1) if pnls else None}


def a_live(db, s, night: str) -> dict:
    """Strategy A LIVE realized results since its baseline: GMGN LIVE orders (+ FOMO LIVE positions, same as the
    app's real side). No baseline = all time."""
    ts = since(s, "A")
    t0 = ts if ts is not None else 0.0
    gm = db.rows("SELECT pnl_usd, night FROM gmgn_orders WHERE mode='LIVE' AND closed_at IS NOT NULL AND closed_at>=?", (t0,))
    pos = db.rows("SELECT pnl_usd, night FROM positions WHERE mode='LIVE' AND status='closed' AND closed_at>=?", (t0,))
    rows = list(gm) + list(pos)
    out = summarize([r["pnl_usd"] or 0 for r in rows], [r["pnl_usd"] or 0 for r in rows if r["night"] == night])
    out.update(strategy="A", since=iso(ts), sinceLabel=label(ts))
    return out


def b_live(db, s, night: str) -> dict:
    ts = since(s, "B")
    t0 = ts if ts is not None else 0.0
    try:
        rows = db.rows("SELECT pnl_usd, night FROM strat_b_orders WHERE mode='LIVE' AND status='closed' AND closed_at>=?", (t0,))
    except Exception:
        rows = []
    out = summarize([r["pnl_usd"] or 0 for r in rows], [r["pnl_usd"] or 0 for r in rows if r["night"] == night])
    out.update(strategy="B", since=iso(ts), sinceLabel=label(ts))
    return out


def telegram_line(d: dict, who: str) -> str:
    when = f"since {d['sinceLabel']}" if d.get("sinceLabel") else "all time"
    return (f"{who} LIVE profit {when}: ${d['pnlUsd']:+,.2f} · {d['closed']} closed · {d['wins']}W {d['losses']}L")
