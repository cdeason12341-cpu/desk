"""Restart the original FOMO PAPER test (DECISION 58) without deleting any history.

The FOMO PAPER test = desk.mode PAPER with its simulated stack (desk.paper_start_stack) and the LIVE gate
"1 full PAPER night + the owner types LIVE" (telegram.cmd_live). A restart:
  - meta paper_base = desk.paper_start_stack, paper_base_ts = now  -> PAPER balance starts again from the stack
    (ctx.balance only adds PAPER positions closed at/after paper_base_ts)
  - meta paper_test_since = now -> the app's Paper P&L / wins / losses / chart and the LIVE gate's full PAPER night
    count only start from now (telegram.paper_nights_done counts nights started at/after it)
  - one change_log row (key 'desk.paper_test', not a setting, so Settings.sync ignores it)
It never touches desk.mode, gmgn.mode, caps, Strategy B or any setting. Trades, nights, and reports stay in the DB.

CLI (desk can stay up; meta is read live):  .venv/bin/python paper_test.py "<source text>"   """
from __future__ import annotations
import json, sys, time


def restart(db, settings, source: str, now: float | None = None) -> dict:
    now = time.time() if now is None else float(now)
    old = {"paper_base": db.meta("paper_base"), "paper_base_ts": db.meta("paper_base_ts"),
           "paper_test_since": db.meta("paper_test_since")}
    stack = float(settings["desk.paper_start_stack"])
    db.set_meta("paper_base", stack)
    db.set_meta("paper_base_ts", now)
    db.set_meta("paper_test_since", now)
    db.set_meta("paper_test_note", source)
    new = {"paper_base": stack, "paper_base_ts": now, "paper_test_since": now}
    db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
         (now, "desk.paper_test", json.dumps(old), json.dumps(new), source))
    return new


if __name__ == "__main__":
    from pathlib import Path
    import config
    from db import DB
    root = Path(__file__).resolve().parent
    db = DB(str(root / "desk.db"))
    s = config.Settings(db)
    src = sys.argv[1] if len(sys.argv) > 1 else "manual paper test restart"
    print(json.dumps(restart(db, s, src)))
