"""SQLite (WAL) store for the desk. One connection, used from the single asyncio loop."""
import json, sqlite3, time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS change_log(id INTEGER PRIMARY KEY, ts REAL, key TEXT, old TEXT, new TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS nights(id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, mode TEXT, start_balance REAL);
CREATE TABLE IF NOT EXISTS coins(
  mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, pool TEXT, pool_label TEXT, signature TEXT,
  creator TEXT, creator_balance_raw TEXT, creator_known INTEGER DEFAULT 0, migrated_at REAL, first_seen REAL,
  status TEXT, reason TEXT, liq_at_scan REAL, top10 REAL, h1 REAL, h24 REAL, runner INTEGER DEFAULT 0,
  price REAL, quote TEXT, holders INTEGER, sent INTEGER DEFAULT 0, night TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, verdict TEXT, reason TEXT, json TEXT, night TEXT);
CREATE TABLE IF NOT EXISTS audits(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, passed INTEGER, reason TEXT, layer INTEGER, json TEXT, night TEXT);
CREATE TABLE IF NOT EXISTS analyses(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, passed INTEGER, failed TEXT, score INTEGER,
  model TEXT, input_tokens INTEGER, output_tokens INTEGER, json TEXT, night TEXT);
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, price REAL, size_usd REAL, stack_pct REAL, mode TEXT, status TEXT, reason TEXT, night TEXT);
CREATE TABLE IF NOT EXISTS executions(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, status TEXT, reason TEXT, size_usd REAL, json TEXT, night TEXT);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY, mint TEXT, name TEXT, symbol TEXT, mode TEXT, entry_price REAL, size_usd REAL, peak REAL,
  stop_pct REAL, trail_active INTEGER DEFAULT 0, opened_at REAL, status TEXT, exit_price REAL, exit_reason TEXT,
  closed_at REAL, pnl_usd REAL, pct REAL, fees_usd REAL, last_price REAL, last_liq REAL, no_price INTEGER DEFAULT 0,
  exit_pushed_at REAL, repushed INTEGER DEFAULT 0, night TEXT);
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, title TEXT, sub TEXT, right1 TEXT, right2 TEXT,
  mint TEXT, agent TEXT, tone TEXT, night TEXT);
CREATE TABLE IF NOT EXISTS prices(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, price REAL, vol_m5 REAL, src TEXT);
CREATE INDEX IF NOT EXISTS prices_mint ON prices(mint, ts);
CREATE TABLE IF NOT EXISTS skips(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, agent TEXT, reason TEXT, night TEXT);
CREATE TABLE IF NOT EXISTS heartbeats(agent TEXT PRIMARY KEY, ts REAL, note TEXT);
CREATE TABLE IF NOT EXISTS pushes(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, kind TEXT, text TEXT, ok INTEGER, night TEXT);
CREATE TABLE IF NOT EXISTS ws_events(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS balance(id INTEGER PRIMARY KEY, ts REAL, balance REAL, mode TEXT, night TEXT);
CREATE TABLE IF NOT EXISTS gmgn_orders(
  id INTEGER PRIMARY KEY, ts REAL, night TEXT, mode TEXT, mint TEXT, name TEXT, symbol TEXT, status TEXT, reason TEXT,
  size_usd REAL, sol_usd REAL, lamports INTEGER, entry_price REAL, peak REAL, last_price REAL, last_liq REAL,
  order_id TEXT, strategy_order_id TEXT, repair TEXT, tx TEXT, exit_reason TEXT, exit_price REAL, pnl_usd REAL, pct REAL,
  closed_at REAL, quote TEXT, body TEXT, conditions TEXT, last_check REAL, sell_order_id TEXT, no_surge INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS stage_pings(night TEXT, stage TEXT, mint TEXT, ts REAL, PRIMARY KEY(night, stage, mint));
CREATE TABLE IF NOT EXISTS overrides(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, kind TEXT, status TEXT, score INTEGER, reject_ts REAL,
  reject_price REAL, price REAL, liq REAL, mc REAL, size_usd REAL, expires REAL, result TEXT, night TEXT);
CREATE TABLE IF NOT EXISTS surges(id INTEGER PRIMARY KEY, ts REAL, mint TEXT, pct REAL, mc_from REAL, mc_to REAL, secs REAL, window_s REAL, status TEXT, night TEXT, scanned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS rechecks(mint TEXT PRIMARY KEY, n INTEGER DEFAULT 0, next_at REAL, first_reason TEXT, last_reason TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS gmgn_approvals(id INTEGER PRIMARY KEY, ts REAL, night TEXT, mint TEXT, name TEXT, symbol TEXT,
  mc REAL, price REAL, liq REAL, score INTEGER, reentry TEXT, status TEXT, expires REAL, result TEXT);
CREATE TABLE IF NOT EXISTS strat_b_watch(mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, added_at REAL, grad_at REAL,
  top10 REAL, holders INTEGER, bundle_pct REAL, high REAL, low REAL, last_price REAL, status TEXT, reason TEXT, updated REAL, night TEXT);
CREATE TABLE IF NOT EXISTS strat_b_evals(id INTEGER PRIMARY KEY, ts REAL, night TEXT, mint TEXT, name TEXT, symbol TEXT, kind TEXT,
  status TEXT, reason TEXT, checks TEXT, price REAL);
CREATE INDEX IF NOT EXISTS strat_b_evals_night ON strat_b_evals(night, id);
CREATE TABLE IF NOT EXISTS strat_b_orders(id INTEGER PRIMARY KEY, ts REAL, night TEXT, mint TEXT, name TEXT, symbol TEXT, status TEXT,
  size_usd REAL, signal_price REAL, entry_price REAL, tokens REAL, buy_fees_usd REAL, peak REAL, last_price REAL, last_liq REAL,
  entry_liq REAL, mc_entry REAL, trail_armed INTEGER DEFAULT 0, opened_at REAL, closed_at REAL, exit_reason TEXT,
  exit_signal_price REAL, exit_price REAL, sell_fees_usd REAL, pnl_usd REAL, pct REAL, why TEXT, last_seen REAL);
CREATE TABLE IF NOT EXISTS gmgn_dips(
  id INTEGER PRIMARY KEY, ts REAL, night TEXT, mint TEXT, name TEXT, symbol TEXT,
  score INTEGER, reentry TEXT, high REAL, low REAL, last_price REAL, last_liq REAL, last_mc REAL,
  start_price REAL, start_liq REAL, status TEXT, reason TEXT, expires REAL, updated REAL,
  want_gmgn INTEGER DEFAULT 0, fomo_item TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, ts REAL, agent TEXT, kind TEXT, note TEXT, night TEXT);
"""


class DB:
    def __init__(self, path=":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript(SCHEMA)
        self._migrate()

    MIGRATIONS = (("gmgn_orders", "sell_tx", "TEXT"), ("gmgn_orders", "entry_src", "TEXT"),
                  ("gmgn_orders", "confirm_push_at", "REAL"), ("gmgn_orders", "audit", "TEXT"),
                  ("gmgn_orders", "grace_until", "REAL"), ("gmgn_orders", "exits_phase", "TEXT"),
                  ("gmgn_orders", "no_surge", "INTEGER"),
                  ("strat_b_watch", "checks", "TEXT"), ("strat_b_watch", "eval_at", "REAL"), ("strat_b_watch", "evals", "INTEGER"),
                  ("strat_b_watch", "source", "TEXT"), ("strat_b_evals", "source", "TEXT"),
                  # Strategy B LIVE (DECISION 66): real GMGN orders share strat_b_orders, told apart by mode
                  ("strat_b_orders", "mode", "TEXT"), ("strat_b_orders", "lamports", "INTEGER"), ("strat_b_orders", "sol_usd", "REAL"),
                  ("strat_b_orders", "order_id", "TEXT"), ("strat_b_orders", "tx", "TEXT"), ("strat_b_orders", "strategy_order_id", "TEXT"),
                  ("strat_b_orders", "sell_order_id", "TEXT"), ("strat_b_orders", "sell_tx", "TEXT"), ("strat_b_orders", "entry_src", "TEXT"),
                  ("strat_b_orders", "note", "TEXT"), ("strat_b_orders", "confirm_push_at", "REAL"))

    def _migrate(self):
        for table, col, typ in self.MIGRATIONS:
            cols = {r[1] for r in self.c.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.c.execute("UPDATE strat_b_orders SET mode='PAPER' WHERE mode IS NULL")      # rows from before DECISION 66

    def x(self, sql, args=()):
        return self.c.execute(sql, args)

    def q(self, sql, args=()):
        return [tuple(r) for r in self.c.execute(sql, args).fetchall()]

    def rows(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.c.execute(sql, args).fetchone()
        return dict(r) if r else None

    def meta(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_meta(self, key, value):
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    def heartbeat(self, agent, note=""):
        self.x("INSERT INTO heartbeats(agent,ts,note) VALUES(?,?,?) ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts, note=excluded.note",
               (agent, time.time(), note))
