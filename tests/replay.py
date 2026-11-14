"""STEP 13.6 test_replay harness: run N hours of LIVE migrations through a PAPER desk on a separate db,
no Telegram (pushes captured), no publishing. Then print the Activity.
Usage: .venv/bin/python -m tests.replay --hours 2 [--db logs/replay.db]"""
import argparse, asyncio, json, sys, time
sys.path.insert(0, ".")
import main
from ctx import NullNotifier


async def go(hours, db):
    d = main.Desk(db_path=db, notifier=NullNotifier(), with_telegram=False)
    d.settings.set("desk.mode", "PAPER", source="replay", force=True)
    jobs = [d.scanner.ws_loop(), d.scanner.poll_loop(), d.auditor.loop(), d.analyst.loop(), d.executor.loop(), d.risk.loop()]
    tasks = [asyncio.create_task(j) for j in jobs]
    try:
        await asyncio.sleep(hours * 3600)
    finally:
        for t in tasks:
            t.cancel()
    rows = d.db.rows("SELECT ts,title,sub,right1,right2 FROM activity ORDER BY id")
    for r in rows:
        print(time.strftime("%H:%M", time.localtime(r["ts"])), "|", r["title"], "|", r["sub"], "|", r["right1"], r["right2"])
    print(json.dumps({"pushes": d.ctx.notifier.sent[-20:]}, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2)
    ap.add_argument("--db", default="logs/replay.db")
    a = ap.parse_args()
    asyncio.run(go(a.hours, a.db))
