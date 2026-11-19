"""The desk: ONE asyncio loop running Scanner, Auditor, Analyst, Executor, Risk, Telegram and the publisher.
Agents are a process, not routines. Heartbeats in db.heartbeats + logs/heartbeats.json."""
from __future__ import annotations
import asyncio, datetime as dt, faulthandler, json, os, signal, sys, threading, time, traceback
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
STALE_S = 180
WATCHDOG_EVERY_S = 15
# heartbeat name -> job name(s) in run()
HB_JOBS = {
    "scanner": ("scanner",),
    "surge": ("surge",),
    "reentry": ("reentry",),
    "auditor": ("auditor",),
    "analyst": ("analyst",),
    "executor": ("executor",),
    "risk": ("risk",),
    "publisher": ("publisher",),
    "gmgn": ("gmgn",),
    "gmgn_surge": ("gmgn_surge",),
    "gmgn_trail": ("gmgn_trail",),
    "gmgn_dip": ("gmgn_dip",),
    "strat_b": ("strat_b",),
    "strat_b_live": ("strat_b_live",),
    "strat_b_feed": ("strat_b_feed",),
}


class Desk:
    def __init__(self, db_path=str(ROOT / "desk.db"), notifier=None, log_files=True, with_telegram=True):
        self.db = DB(db_path)
        self.settings = config.Settings(self.db)
        self.settings.record_default_source(
            "auditor.l8_bypass_on_down", "chat: owner asked 2026-11-17 7:15 PM CT")
        self.bus = Bus()
        self.ctx = Ctx(self.db, self.settings, None, self.bus, notifier, log_files)
        self.ctx.net = Net(self.settings)
        self.scanner, self.auditor, self.analyst = Scanner(self.ctx), Auditor(self.ctx), Analyst(self.ctx)
        self.executor, self.risk = Executor(self.ctx), Risk(self.ctx)
        from agents.gmgn_trader import GmgnTrader
        import gmgn as gmgn_api
        if with_telegram:                                           # DECISION 88: a GMGN ban survives a restart
            gmgn_api.GATE.configure(str(ROOT / "data" / "gmgn_gate.json"))
        client = gmgn_api.Gmgn() if with_telegram else None        # tests build the Desk without network clients
        self.gmgn = GmgnTrader(self.ctx, client if (client and client.configured) else None)
        self.ctx.gmgn = self.gmgn if self.gmgn.client else None
        self.gmgn.record_gate_rule()
        if with_telegram:                                           # real run: pool-reserve exit prices (DECISION 57)
            from fastprice import FastPrice
            self.gmgn.fast = FastPrice(self.ctx, self.gmgn)
        self.tg = None
        if with_telegram:
            from telegram import Telegram
            self.tg = Telegram(self)
            self.ctx.notifier = self.tg
        from strat_b import StratB                                  # STRATEGY B (DECISION 54); LIVE via strat_b_live + ctx.gmgn (DECISION 66)
        self.strat_b = StratB(self.ctx, log_files=log_files)
        self.ctx.strat_b = self.strat_b
        from strat_b_feed import BFeed                              # B's own older-coin feed (DECISION 63), paper only
        self.strat_b_feed = BFeed(self.strat_b)
        from overrides import Overrides
        self.overrides = Overrides(self)
        from reentry import ReEntry
        self.reentry = ReEntry(self)
        self.ctx.reentry = self.reentry
        self.webpush = None                                         # phone notifications (DECISION 62); Telegram stays the backup
        if with_telegram:
            from webpush import WebPush
            self.webpush = WebPush(self.ctx)
        self.ctx.webpush = self.webpush
        from dip import DipWatch                                  # Strategy A dip-watch (DECISION 64)
        self.dip = DipWatch(self.ctx, trader=self.gmgn, executor=self.executor)
        self.ctx.dip = self.dip
        self.ctx.executor = self.executor
        from publisher import Publisher
        self.publisher = Publisher(self)
        self.live = None                                            # DECISION 75: live app data from this server (read-only)
        if with_telegram:
            from live_api import LiveApi
            self.live = LiveApi(self)
        self.log = self.ctx.logs["desk"]
        self.hb_path = LOGS / "heartbeats.json"
        self.watchdog_log = LOGS / "watchdog.log"
        self.stale_s = STALE_S
        self._shutdown = False
        self._loop = None
        self._loop_tick = 0.0
        self._jobs = {}
        self._inner = {}
        self._allow_respawn = False  # process restarts belong to supervise_desk.py
        self._prev_wall = None
        self._prev_mono = None
        self._resume_grace_until = 0.0

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
            if kind in ("signal", "fill", "exit", "audit", "analysis", "scan", "webpush"):
                self.publisher.mark()

    async def heartbeat_loop(self):
        while True:
            now = time.time()
            self._loop_tick = time.monotonic()
            hb = {r["agent"]: round(now - r["ts"], 1) for r in self.db.rows("SELECT agent, ts FROM heartbeats")}
            out = {"ts": iso(), "pid": os.getpid(), "ageS": hb, "mode": self.ctx.mode(), "night": self.ctx.night,
                   "wsLastMsgAgeS": round(now - self.scanner.ws_last, 1) if self.scanner.ws_last else None,
                   "wsPongAgeS": round(now - self.scanner.ws_pong, 1) if self.scanner.ws_pong else None}
            self.hb_path.write_text(json.dumps(out))
            self.ctx.hb("desk", "")
            for _ in range(3):                           # settings written outside this process apply within ~5s
                try:
                    ch = self.settings.sync()
                    if ch:
                        self.log.info(f"{iso()}\tsettings\thot-loaded {', '.join(ch)}")
                except Exception as e:
                    self.ctx.error("desk", "settings_sync", str(e)[:200])
                await asyncio.sleep(5)

    def rollover_due(self, now: dt.datetime | None = None) -> bool:
        """DECISION 59: catch-up rollover. Due when local time is at/after desk.report_time and today's rollover
        hasn't happened (meta last_rollover != today). A night that already started today at/after report_time
        (fresh start after 8 AM) counts as today's rollover: it is marked done, never rolled again."""
        import report
        now = now or dt.datetime.now()
        today = now.date().isoformat()
        if self.db.meta("last_rollover") == today:
            return False
        cutoff = report.cutoff(self.settings, now)
        if now < cutoff:
            return False
        r = self.db.one("SELECT started_at FROM nights WHERE id=?", (self.ctx.night,))
        if r and r["started_at"] and dt.datetime.fromtimestamp(float(r["started_at"])) >= cutoff:
            self.db.set_meta("last_rollover", today)
            return False
        return True

    async def rollover_if_due(self, now: dt.datetime | None = None) -> bool:
        """Close the current night (report.py summary), start the next one. At most once per local day."""
        now = now or dt.datetime.now()
        if not self.rollover_due(now):
            return False
        import report
        old = self.ctx.night
        self.db.set_meta("last_rollover", now.date().isoformat())     # first: a failure below can't cause a 2nd roll
        try:
            report.close_night(self.db, old)
        except Exception as e:
            self.ctx.error("desk", "close_night", str(e)[:200])
            self.db.x("UPDATE nights SET ended_at=? WHERE id=? AND ended_at IS NULL", (time.time(), old))
        self.ctx.new_night()
        self.log.info(f"{iso()}	night rollover	closed={old}	new={self.ctx.night}")
        try:
            import strat_b_gta as G
            await G.on_night_rollover(self.ctx, log=self.log)
        except Exception as e:
            self.ctx.error("desk", "gta_baseline", str(e)[:200])
        await self.night_start()
        return True

    async def night_loop(self):
        """At/after settings.report_time: close the night and start the next one (catch-up, DECISION 59)."""
        while True:
            await asyncio.sleep(30)
            try:
                await self.rollover_if_due()
            except Exception as e:
                self.ctx.error("desk", "rollover", str(e)[:200])

    async def supervised(self, name, fn):
        while not self._shutdown:
            task = asyncio.create_task(fn(), name=name)
            self._inner[name] = task
            try:
                await task
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                if self._shutdown or asyncio.current_task().cancelling():
                    raise
                self.log.info(f"{iso()}\t{name}\twatchdog cancelled, restarting")
                continue
            except Exception as e:
                self.ctx.error(name, "crash", f"{type(e).__name__}: {e}")
                self.log.info(f"{iso()}\t{name}\tcrashed, restarting in 5s\t{secrets_store.redact(str(e))[:200]}")
                await asyncio.sleep(5)

    def restart_job(self, name):
        """Cancel the inner task so supervised() restarts it. Safe on the event loop thread."""
        task = self._inner.get(name)
        if task is None or task.done():
            return False
        self.log.info(f"{iso()}\t{name}\twatchdog restart")
        task.cancel()
        return True

    def _dump_stacks(self, why):
        LOGS.mkdir(exist_ok=True)
        chunks = [f"{iso()}\twatchdog\t{why}\n"]
        try:
            with self.watchdog_log.open("a") as fh:
                fh.write(chunks[0])
                fh.flush()
                faulthandler.dump_traceback(file=fh, all_threads=True)
            chunks = []  # header already on disk; keep extra for task dump
        except Exception as e:
            chunks.append(f"faulthandler failed: {type(e).__name__}\n")
        def _write(extra=""):
            try:
                with self.watchdog_log.open("a") as f:
                    f.write(secrets_store.redact("".join(chunks) + extra) + "\n")
            except Exception:
                pass
        def _task_dump():
            parts = []
            try:
                for task in asyncio.all_tasks(self._loop):
                    parts.append(f"# Task {task.get_name()} {task}\n")
                    stack = task.get_stack(limit=20)
                    if stack:
                        for fr in stack:
                            parts.extend(traceback.format_stack(fr))
            except Exception as e:
                parts.append(f"task dump failed: {type(e).__name__}\n")
            _write("".join(parts))
        if self._loop is not None and self._loop.is_running():
            try:
                if threading.current_thread() is threading.main_thread():
                    _task_dump()
                else:
                    _write()
                    self.log.info(f"{iso()}\twatchdog\t{why}\tdump={self.watchdog_log}")
                    self._loop.call_soon_threadsafe(_task_dump)
                    return
            except Exception:
                pass
        _write()
        self.log.info(f"{iso()}\twatchdog\t{why}\tdump={self.watchdog_log}")

    def _loop_frozen(self):
        if not self._loop_tick:
            return False  # not started yet
        return (time.monotonic() - self._loop_tick) > self.stale_s

    def _hb_snapshot(self):
        """Real ages from heartbeats.json plus file lag. Never touches sqlite from the watchdog thread."""
        now = time.time()
        ages, file_age, ws_pong = {}, 0.0, None
        try:
            raw = self.hb_path.read_text() if self.hb_path.exists() else ""
            data = json.loads(raw) if raw else {}
            ts = data.get("ts")
            if ts:
                written = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
                file_age = max(0.0, now - written)
            ages = {k: float(v) + file_age for k, v in (data.get("ageS") or {}).items()}
            wp = data.get("wsPongAgeS")
            ws_pong = None if wp is None else float(wp) + file_age
        except Exception:
            pass
        return ages, file_age, ws_pong

    def check_watchdog(self, respawn=None):
        """Inspect heartbeats. Restart stale *tasks* if the loop is alive.

        Process-level restarts are supervise_desk.py's job. A wall-clock gap
        with a fresh monotonic loop_tick is a VM pause/resume, not a wedge.
        """
        if self._shutdown:
            return []
        if respawn is None:
            respawn = self._allow_respawn
        actions = []
        ages, file_age, ws_pong = self._hb_snapshot()
        now_wall, now_mono = time.time(), time.monotonic()
        skew = 0.0
        if self._prev_wall is not None:
            from supervise_desk import clock_skew
            skew = clock_skew(self._prev_wall, self._prev_mono, now_wall, now_mono)
        self._prev_wall, self._prev_mono = now_wall, now_mono
        if skew > 60:
            why = (f"box paused/resumed wall-mono skew={skew:.0f}s "
                   f"file_age={file_age:.0f}s loop_tick={now_mono - (self._loop_tick or now_mono):.0f}s")
            self.log.info(f"{iso()}\twatchdog\t{why}")
            self._resume_grace_until = now_mono + 90
            actions.append(("clock_jump", why))
            return actions
        if now_mono < self._resume_grace_until:
            actions.append(("grace", "post-resume"))
            return actions
        # Real wedge: monotonic advanced but the loop did not tick. Do not
        # treat wall-clock file_age alone as a wedge (that's a pause).
        if self._loop_frozen():
            why = f"event loop frozen file_age={file_age:.0f}s loop_tick={now_mono - self._loop_tick:.0f}s"
            self._dump_stacks(why)
            actions.append(("frozen", why))
            if respawn:
                self._respawn_process()
            return actions
        stale = []
        for agent, jobs in HB_JOBS.items():
            age = ages.get(agent)
            if age is None:
                continue  # not started (e.g. no gmgn client)
            if age > self.stale_s:
                stale.extend(jobs)
        if ws_pong is not None and ws_pong > self.stale_s:
            stale.append("ws")
        seen = []
        for name in stale:
            if name in seen:
                continue
            seen.append(name)
            why = f"{name} heartbeat stale > {self.stale_s:.0f}s"
            self._dump_stacks(why)
            actions.append(("restart", name))
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self.restart_job, name)
            else:
                self.restart_job(name)
        return actions

    def _respawn_process(self):
        """SIGTERM (SIGKILL if the loop can't handle it) and start a new desk via run_desk.sh."""
        if self._shutdown:
            return
        self._shutdown = True
        helper = (
            f"old={os.getpid()}; "
            f"while kill -0 $old 2>/dev/null; do sleep 1; done; "
            f"cd {ROOT!s} && ./run_desk.sh"
        )
        try:
            import subprocess
            subprocess.Popen(["bash", "-lc", helper], cwd=str(ROOT), start_new_session=True,
                             stdout=open(LOGS / "desk.out", "a"), stderr=subprocess.STDOUT)
        except Exception as e:
            self.log.info(f"{iso()}\twatchdog\trespawn helper failed\t{type(e).__name__}")
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            pass
        # if the event loop is wedged, SIGTERM is queued and never handled
        def _kill_if_still_up():
            time.sleep(8)
            try:
                os.kill(os.getpid(), signal.SIGKILL)
            except Exception:
                pass
        threading.Thread(target=_kill_if_still_up, name="desk-kill", daemon=True).start()

    def _watchdog_thread(self):
        # first check after one interval so startup heartbeats can land
        while not self._shutdown:
            time.sleep(WATCHDOG_EVERY_S)
            if self._shutdown:
                break
            try:
                self.check_watchdog()
            except Exception:
                try:
                    self._dump_stacks("watchdog error")
                except Exception:
                    pass

    async def run(self, with_watchdog=True):
        self.log.info(f"{iso()}\tdesk start\tpid={os.getpid()}\tmode={self.ctx.mode()}\tgmgn={self.settings['gmgn.mode']}"
                      f"{'' if self.ctx.gmgn else ' (no client)'}\tgmgn_paper_since={iso(self.gmgn.paper_since())}")
        self.ctx.record_balance()
        try:
            rolled = await self.rollover_if_due()             # catch up a rollover missed while down/paused (calls night_start)
        except Exception as e:
            self.ctx.error("desk", "rollover", str(e)[:200]); rolled = False
        if not rolled:
            await self.night_start()
        self._shutdown = False
        self._loop = asyncio.get_running_loop()
        self._loop_tick = time.monotonic()
        jobs = {"ws": self.scanner.ws_loop, "scanner": self.scanner.poll_loop,
                "surge": self.scanner.surge_loop, "reentry": self.reentry.loop, "auditor": self.auditor.loop,
                "analyst": self.analyst.loop, "executor": self.executor.loop, "risk": self.risk.loop,
                "events": self.events_loop, "heartbeat": self.heartbeat_loop, "night": self.night_loop,
                "publisher": self.publisher.loop, "strat_b": self.strat_b.loop, "strat_b_live": self.strat_b.live.loop,
                "strat_b_feed": self.strat_b_feed.loop, "gmgn_dip": self.dip.loop}
        if self.ctx.gmgn:
            jobs["gmgn"] = self.gmgn.loop
            jobs["gmgn_surge"] = self.gmgn.surge_loop
            jobs["gmgn_trail"] = self.gmgn.trail_loop
        if self.tg:
            jobs["telegram"] = self.tg.loop
        if self.webpush:
            jobs["webpush"] = self.webpush.loop
        if self.live:
            jobs["live_api"] = self.live.loop
        self._jobs = jobs
        wd = None
        if with_watchdog:
            wd = threading.Thread(target=self._watchdog_thread, name="desk-watchdog", daemon=True)
            wd.start()
        # Debug-only: DESK_BLOCK_LOOP_S makes this task block the asyncio loop
        # with a sync sleep so the out-of-process supervisor can be proven to fire.
        # run_desk.sh never sets this.
        block = float(os.environ.get("DESK_BLOCK_LOOP_S") or 0)
        if block > 0:
            self.log.info(f"{iso()}\tdebug\tblocking event loop for {block}s")
            time.sleep(block)
        try:
            await asyncio.gather(*(self.supervised(n, f) for n, f in jobs.items()))
        finally:
            self._shutdown = True


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
