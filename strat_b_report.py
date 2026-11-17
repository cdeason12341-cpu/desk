"""Print the Strategy B scorecard (B LIVE real money first, then B PAPER): .venv/bin/python strat_b_report.py [--json]. Read-only."""
import datetime as dt, json, sys, types
import config, strat_b
from ctx import ROOT
from db import DB


def ct(ts):
    if not ts:
        return "—"
    t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).astimezone()
    return t.strftime("%b %d %I:%M %p").replace(" 0", " ") + " CT"


def main():
    db = DB(str(ROOT / "desk.db"))
    c = types.SimpleNamespace(db=db, s=config.Settings(db), night=db.meta("night"))
    card = strat_b.scorecard(c, limit=1000)
    if "--json" in sys.argv:
        print(json.dumps(card, indent=1))
        return
    lv = card.get("live") or {}
    lwr = "—" if lv.get("winRatePct") is None else f"{lv['winRatePct']}%"
    print(f"Strategy B mode: {card['mode']}")
    print(f"B LIVE · real money · {'active' if lv.get('active') else 'not active'}")
    print(f"  P&L all time {lv.get('pnlAllUsd', 0):+.2f} · tonight {lv.get('pnlTonightUsd', 0):+.2f} ({lv.get('closedTonight', 0)} closed) · "
          f"{lv.get('wins', 0)}W {lv.get('losses', 0)}L · win rate {lwr} · refused tonight {lv.get('refusedTonight', 0)}")
    if lv.get("lastRefusal"):
        r = lv["lastRefusal"]
        print(f"  last refusal {ct(r['t'])} {r['name']}: {r['reason']}")
    for o in lv.get("open") or []:
        print(f"  open #{o['id']} {o['name']} ${o['sizeUsd']:.2f} {o['status']} in {ct(o['openedAt'])} · now {(o.get('pct') or 0):+.1f}% ({(o.get('pnlUsd') or 0):+.2f})")
    for o in lv.get("closed") or []:
        print(f"  closed #{o['id']} {o['name']} {o['pnlUsd']:+.2f} ({(o['pct'] or 0):+.1f}%) · {o['exitReason']} · {ct(o['openedAt'])} → {ct(o['closedAt'])}")
    print("")
    wr = "—" if card["winRatePct"] is None else f"{card['winRatePct']}%"
    pf = "—" if card["profitFactor"] is None else card["profitFactor"]
    print(f"B PAPER (STRATEGY B · PAPER) · enabled={card['enabled']} · not real money")
    print(f"Bankroll ${card['bankroll']:,.2f} (start ${card['bankrollStart']:,.2f}) · free ${card['free']:,.2f} · in positions ${card['inPositions']:,.2f} (unrealized {card['unrealizedUsd']:+.2f})")
    print(f"P&L all time {card['pnlAllUsd']:+.2f} · tonight {card['pnlTonightUsd']:+.2f} ({card['closedTonight']} closed)")
    print(f"Closed {card['closedAll']} · {card['wins']}W {card['losses']}L · win rate {wr} · profit factor {pf}")
    print(f"Watching {card['watching']} · skipped by filters {card['skipped']} · expired without entry {card['expired']}")
    print("\nOpen:")
    for o in card["open"] or []:
        print(f"  #{o['id']} {o['name']} ${o['sizeUsd']:.2f} in {ct(o['openedAt'])} · now {o['pct']:+.1f}% ({o['pnlUsd']:+.2f}) · trail {'armed' if o['trailArmed'] else 'not armed'}")
    if not card["open"]:
        print("  none")
    print("\nClosed (newest first):")
    for o in card["closed"] or []:
        print(f"  #{o['id']} {o['name']} {o['pnlUsd']:+.2f} ({(o['pct'] or 0):+.1f}%) · {o['exitReason']} · {ct(o['openedAt'])} → {ct(o['closedAt'])}")
    if not card["closed"]:
        print("  none yet")
    print("\nRules:")
    for r in card["rules"]:
        print(f"  {r['k']}: {r['v']}  ({r['why']})")


if __name__ == "__main__":
    main()
