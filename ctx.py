"""Shared desk context: db, settings, net, bus, notifier, loggers, night, balances."""
from __future__ import annotations
import datetime as dt, json, logging, time
from pathlib import Path
import links

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
AGENTS = ("scanner", "auditor", "analyst", "executor", "risk")


def iso(ts: float | None = None) -> str:
    return dt.datetime.fromtimestamp(ts if ts is not None else time.time(), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_logger(name: str, to_file: bool = True) -> logging.Logger:
    lg = logging.getLogger(f"desk.{name}")
    if not lg.handlers:
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if to_file:
            LOGS.mkdir(exist_ok=True)
            h = logging.FileHandler(LOGS / f"{name}.log")
        else:
            h = logging.NullHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(h)
    return lg


class NullNotifier:
    def __init__(self):
        self.sent = []

    async def push(self, text, buttons=None, kind="info", mint=None):
        self.sent.append({"text": text, "buttons": buttons or [], "kind": kind, "mint": mint})
        return True


class Ctx:
    def __init__(self, db, settings, net=None, bus=None, notifier=None, log_files=True, clock=time.time):
        self.db, self.s, self.net, self.bus = db, settings, net, bus
        self.notifier = notifier or NullNotifier()
        self.clock = clock
        self.logs = {n: make_logger(n, log_files) for n in AGENTS + ("desk", "telegram", "publish", "gmgn")}
        self.paused = set(db.meta("paused", []) or [])
        self.on_event = None
        self.gmgn = None            # GmgnTrader (hands-off mode), set by main.Desk
        self.night = db.meta("night") or self.new_night()

    # ---- nights
    def new_night(self) -> str:
        nid = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
        self.db.x("INSERT OR IGNORE INTO nights(id,started_at,mode,start_balance) VALUES(?,?,?,?)",
                  (nid, self.clock(), self.s["desk.mode"], self.balance()))
        self.db.set_meta("night", nid)
        self.night = nid
        return nid

    # ---- money
    def mode(self) -> str:
        return self.s["desk.mode"]

    def balance(self) -> float:
        """PAPER: simulated balance (start + closed PnL). LIVE: the stack set with 'stack N' + closed LIVE PnL."""
        if self.s["desk.mode"] == "PAPER":
            base = float(self.db.meta("paper_base", self.s["desk.paper_start_stack"]))
            since = float(self.db.meta("paper_base_ts", 0))
            r = self.db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM positions WHERE mode='PAPER' AND status='closed' AND closed_at>=?", (since,))
            return round(base + (r["p"] or 0), 6)
        base = float(self.s["desk.live_stack"])
        since = float(self.db.meta("live_stack_ts", 0))
        r = self.db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM positions WHERE mode='LIVE' AND status='closed' AND closed_at>=?", (since,))
        return round(base + (r["p"] or 0), 6)

    def in_positions(self) -> float:
        r = self.db.one("SELECT COALESCE(SUM(size_usd),0) s FROM positions WHERE mode=? AND status IN ('open','exiting')", (self.mode(),))
        return r["s"] or 0.0

    def free(self) -> float:
        return round(self.balance() - self.in_positions(), 6)

    def record_balance(self):
        self.db.x("INSERT INTO balance(ts,balance,mode,night) VALUES(?,?,?,?)", (self.clock(), self.balance(), self.mode(), self.night))

    # ---- activity / events
    def activity(self, kind, title, sub, right1="", right2="", mint=None, agent=None, tone="neutral"):
        self.db.x("INSERT INTO activity(ts,kind,title,sub,right1,right2,mint,agent,tone,night) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (self.clock(), kind, title, sub, right1, right2, mint, agent, tone, self.night))

    def event(self, kind, mint=None):
        if self.bus:
            self.bus.event(kind, mint)

    def skip(self, mint, agent, reason):
        self.db.x("INSERT INTO skips(ts,mint,agent,reason,night) VALUES(?,?,?,?,?)", (self.clock(), mint, agent, reason, self.night))

    def error(self, agent, kind, note):
        import secrets_store
        note = secrets_store.redact(str(note))
        self.db.x("INSERT INTO errors(ts,agent,kind,note,night) VALUES(?,?,?,?,?)", (self.clock(), agent, kind, note, self.night))

    def hb(self, agent, note=""):
        self.db.heartbeat(agent, note)

    def coin(self, mint):
        return self.db.one("SELECT * FROM coins WHERE mint=?", (mint,))

    def label(self, mint) -> str:
        c = self.coin(mint) or {}
        return c.get("name") or c.get("symbol") or (mint[:4] + "…" + mint[-4:])

    def prefix(self) -> str:
        return "PAPER · " if self.mode() == "PAPER" else ""

    def set_paused(self, agent, on: bool):
        if on:
            self.paused.add(agent)
        else:
            self.paused.discard(agent)
        self.db.set_meta("paused", sorted(self.paused))

    @staticmethod
    def coin_url(mint):
        return links.coin_url(mint, "solana")
