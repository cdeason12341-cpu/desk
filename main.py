"""The desk: ONE asyncio loop running Scanner, Auditor, Analyst, Executor, Risk, Telegram and the publisher.
Agents are a process, not routines. Heartbeats in db.heartbeats + logs/heartbeats.json."""
from __future__ import annotations
import asyncio, datetime as dt, json, os, signal, sys, time
import config, secrets_store
from bus import Bus
from ctx import AGENTS, Ctx, LOGS, ROOT, iso
from db import DB
from net import Net
from agents.scanner import Scanner
from agents.auditor import Auditor
from agents.analyst import Analyst
from agents.executor import Executor
from agents.risk import Risk

TICKET_KEYS = {"executor.stack_pct", "executor.below_min", "executor.min_ticket_usd", "desk.live_stack", "desk.paper_start_stack"}


class Desk:
    def __init__(self, db_path=str(ROOT / "desk.db"), notifier=None, log_files=True, with_telegram=True):
        self.db = DB(db_path)
        self.settings = config.Settings(self.db)
        self.bus = Bus()
        self.ctx = Ctx(self.db, self.settings, None, self.bus, notifier, log_files)
        self.ctx.net = Net(self.settings)
        self.scanner, self.auditor, self.analyst = Scanner(self.ctx), Auditor(self.ctx), Analyst(self.ctx)
        self.executor, self.risk = Executor(self.ctx), Risk(self.ctx)
        from agents.gmgn_trader import GmgnTrader
        import gmgn as gmgn_api
        client = gmgn_api.Gmgn() if with_telegram else None        # tests build the Desk without network clients
        self.gmgn = GmgnTrader(self.ctx, client if (client and client.configured) else None)
        self.ctx.gmgn = self.gmgn if self.gmgn.client else None
        self.gmgn.record_gate_rule()
        self.tg = None
        if with_telegram:
            from telegram import Telegram
            self.tg = Telegram(self)
            self.ctx.notifier = self.tg
        from publisher import Publisher
        self.publisher = Publisher(self)
        self.log = self.ctx.logs["desk"]

    # ---- ticket check (STEP 10)
    def ticket(self):
        s = self.settings
        return config.ticket_check(self.ctx.balance(), s["executor.stack_pct"], s["executor.min_ticket_usd"], s["executor.below_min"])

    def ticket_msg(self):
        t = self.ticket()
        return (self.ctx.prefix() + t["msg"]) if t["kind"] != "ok" else None

    def after_setting(self, key):
        return self.ticket_msg() if key in TICKET_KEYS else None

    async def night_start(self, force=False):
        msg = self.ticket_msg()
        if msg and (force or self.db.meta("ticket_msg_night") != self.ctx.night):   # once per night, not per restart
            await self.ctx.notifier.push(msg, kind="ticket")
            self.db.set_meta("ticket_msg_night", self.ctx.night)
        self.log.info(f"{iso()}\tnight\t{self.ctx.night}\tticket={self.ticket()}")

    # ---- loops
    async def events_loop(self):
        while True:
            kind, mint = await self.bus.events.get()
            if kind in ("signal", "fill", "exit", "audit", "analysis"):
                self.publisher.mark()

    async def heartbeat_loop(self):
        while True:
            now = time.time()
            hb = {r["agent"]: round(now - r["ts"], 1) for r in self.db.rows("SELECT agent, ts FROM heartbeats")}
            out = {"ts": iso(), "pid": os.getpid(), "ageS": hb, "mode": self.ctx.mode(), "night": self.ctx.night,
                   "wsLastMsgAgeS": round(now - self.scanner.ws_last, 1) if self.scanner.ws_last else None}
            (LOGS / "heartbeats.json").write_text(json.dumps(out))
            self.ctx.hb("desk", "")
            await asyncio.sleep(15)

    async def night_loop(self):
        """At settings.report_time: close the night (report.py builds its summary), start the next one."""
        while True:
            await asyncio.sleep(30)
            hhmm = dt.datetime.now().strftime("%H:%M")
            if hhmm == self.settings["desk.report_time"] and self.db.meta("last_rollover") != dt.date.today().isoformat():
                import report
                report.close_night(self.db, self.ctx.night)
                self.db.set_meta("last_rollover", dt.date.today().isoformat())
                self.ctx.new_night()
                await self.night_start()

    async def supervised(self, name, fn):
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ctx.error(name, "crash", f"{type(e).__name__}: {e}")
                self.log.info(f"{iso()}\t{name}\tcrashed, restarting in 5s\t{secrets_store.redact(str(e))[:200]}")
                await asyncio.sleep(5)

    async def run(self):
        self.log.info(f"{iso()}\tdesk start\tpid={os.getpid()}\tmode={self.ctx.mode()}\tgmgn={self.settings['gmgn.mode']}"
                      f"{'' if self.ctx.gmgn else ' (no client)'}\tgmgn_paper_since={iso(self.gmgn.paper_since())}")
        self.ctx.record_balance()
        await self.night_start()
        jobs = {"ws": self.scanner.ws_loop, "scanner": self.scanner.poll_loop, "auditor": self.auditor.loop,
                "analyst": self.analyst.loop, "executor": self.executor.loop, "risk": self.risk.loop,
                "events": self.events_loop, "heartbeat": self.heartbeat_loop, "night": self.night_loop,
                "publisher": self.publisher.loop}
        if self.ctx.gmgn:
            jobs["gmgn"] = self.gmgn.loop
        if self.tg:
            jobs["telegram"] = self.tg.loop
        await asyncio.gather(*(self.supervised(n, f) for n, f in jobs.items()))


def main():
    LOGS.mkdir(exist_ok=True)
    (LOGS / "desk.pid").write_text(str(os.getpid()))
    desk = Desk()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(desk.run())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
