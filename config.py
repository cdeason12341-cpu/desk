"""Settings registry (STEP 10): defaults, min/max, aliases, display, and change_log.
Every agent reads settings through Settings.get(); nothing else changes them."""
from __future__ import annotations
import json, re, time
from dataclasses import dataclass, field


@dataclass
class Spec:
    key: str
    default: object
    lo: float | None = None
    hi: float | None = None
    kind: str = "float"            # float | int | bool | str | choice | time
    choices: tuple = ()
    aliases: tuple = ()
    fmt: str = "{v}"               # display format
    neg: bool = False              # user may type "-8%" for a positive pct (stop)


def _s(key, default, lo=None, hi=None, kind=None, choices=(), aliases=(), fmt="{v}", neg=False):
    if kind is None:
        kind = "bool" if isinstance(default, bool) else "int" if isinstance(default, int) else \
               "float" if isinstance(default, float) else "str"
    return Spec(key, default, lo, hi, kind, tuple(choices), tuple(aliases), fmt, neg)


SPECS: list[Spec] = [
    # scanner
    _s("scanner.min_liquidity_usd", 40000, 1000, 10_000_000, aliases=("min liquidity", "liquidity", "min liq"), fmt="${v:,.0f}"),
    _s("scanner.max_top10_pct", 25, 1, 100, aliases=("scanner top10", "top10", "top 10"), fmt="{v:g}%"),
    _s("scanner.max_h1_change_pct", 80, 1, 10000, aliases=("max h1", "h1 change", "max pump"), fmt="+{v:g}%"),
    _s("scanner.poll_s", 60, 15, 600, aliases=("scanner poll",), fmt="{v}s"),
    _s("scanner.min_pool_age_min", 10, 0, 240, aliases=("scanner pool age",), fmt="{v}m"),
    _s("scanner.max_wait_min", 60, 5, 720, aliases=("max wait", "waiting room"), fmt="{v}m"),
    _s("scanner.runner_h24_pct", 300, 10, 100000, aliases=("runner",), fmt="+{v:g}%"),
    _s("scanner.max_dex_rpm", 250, 10, 290, aliases=("dex rpm",), fmt="{v}/min"),
    _s("scanner.ws_silent_s", 120, 30, 900, aliases=("ws silent",), fmt="{v}s"),
    _s("scanner.backoff_429_s", 30, 5, 300, aliases=("scanner 429",), fmt="{v}s"),
    # auditor
    _s("auditor.enabled", True, aliases=("auditor",)),
    _s("auditor.max_dev_pct", 5, 0, 100, aliases=("dev", "max dev"), fmt="{v:g}%"),
    _s("auditor.max_top10_pct", 25, 1, 100, aliases=("auditor top10",), fmt="{v:g}%"),
    _s("auditor.min_lp_locked_pct", 99, 0, 100, aliases=("lp locked", "lp"), fmt="{v:g}%"),
    _s("auditor.max_liq_drop_pct", 50, 1, 100, aliases=("liq pulled",), fmt="{v:g}%"),
    _s("auditor.min_pool_age_min", 10, 0, 240, aliases=("pool age",), fmt="{v}m"),
    _s("auditor.max_bundled", 5, 1, 1000, aliases=("bundled", "bundles"), fmt="{v}"),
    _s("auditor.max_delegated_accounts", 10, 1, 10000, aliases=("delegations", "delegated"), fmt="{v}"),
    _s("auditor.retries", 3, 0, 10, aliases=("retries",), fmt="{v}"),
    _s("auditor.retry_gap_s", 2, 0, 30, aliases=("retry gap",), fmt="{v}s"),
    # analyst
    _s("analyst.mode", "jev", kind="choice", choices=("jev", "rules"), aliases=("analyst mode", "analyst")),
    _s("analyst.min_alive_6h", 0.70, 0, 1, aliases=("alive", "min alive"), fmt="{v:.2f}"),
    _s("analyst.min_organic", 0.65, 0, 1, aliases=("organic", "min organic"), fmt="{v:.2f}"),
    _s("analyst.max_chase", 0.30, 0, 1, aliases=("chase", "max chase"), fmt="{v:.2f}"),
    _s("analyst.min_score", 70, 0, 100, aliases=("score", "min score"), fmt="{v}"),
    _s("analyst.timeout_s", 10, 2, 60, aliases=("jev timeout",), fmt="{v}s"),
    _s("analyst.state_max_chars", 3000, 500, 3000, aliases=("state chars",), fmt="{v}"),
    _s("analyst.queue_429_s", 60, 5, 600, aliases=("jev 429",), fmt="{v}s"),
    _s("analyst.rules_alive_volume_h1", 25000, 100, 10_000_000, aliases=("alive volume",), fmt="${v:,.0f}"),
    _s("analyst.rules_organic_buys_h1", 150, 1, 100000, aliases=("organic buys",), fmt="{v}"),
    _s("analyst.rules_bundled_penalty", 0.5, 0, 1, aliases=("bundled penalty",), fmt="x{v:g}"),
    _s("analyst.rules_bundled_from", 3, 1, 1000, aliases=("bundled from",), fmt="{v}"),
    _s("analyst.rules_flush_drop_m5_pct", 5, 0.1, 100, aliases=("flush drop",), fmt="-{v:g}%", neg=True),
    _s("analyst.rules_chase_h1_pct", 80, 1, 10000, aliases=("chase h1",), fmt="+{v:g}%"),
    # executor
    _s("executor.stack_pct", 8, 0.5, 100, aliases=("stack pct", "ticket", "size"), fmt="{v:g}%"),
    _s("executor.max_pool_pct", 2, 0.1, 20, aliases=("pool pct", "max pool"), fmt="{v:g}%"),
    _s("executor.min_ticket_usd", 47.50, 5, 10000, aliases=("min ticket",), fmt="${v:,.2f}"),
    _s("executor.below_min", "bump", kind="choice", choices=("bump", "skip"), aliases=("below min",)),
    _s("executor.recheck_liq_drop_pct", 20, 1, 100, aliases=("recheck liq",), fmt="{v:g}%"),
    _s("executor.recheck_price_up_pct", 15, 1, 1000, aliases=("recheck price",), fmt="{v:g}%"),
    _s("executor.confirm_window_min", 5, 1, 60, aliases=("buy window",), fmt="{v}m"),
    # risk
    _s("risk.poll_s", 15, 5, 120, aliases=("risk poll",), fmt="{v}s"),
    _s("risk.stop_pct", 6, 1, 50, aliases=("stop", "stop loss", "sl"), fmt="-{v:g}%", neg=True),
    _s("risk.trail_start_pct", 20, 1, 1000, aliases=("trail start",), fmt="+{v:g}%"),
    _s("risk.trail_pct", 12, 1, 90, aliases=("trail", "trailing"), fmt="{v:g}%"),
    _s("risk.timeout_h", 3, 0.25, 48, aliases=("timeout",), fmt="{v:g}h"),
    _s("risk.timeout_min_gain_pct", 5, 0, 1000, aliases=("timeout gain",), fmt="+{v:g}%"),
    _s("risk.confirm_window_min", 2, 1, 60, aliases=("sell window",), fmt="{v}m"),
    _s("risk.no_price_polls", 4, 1, 100, aliases=("no price polls",), fmt="{v}"),
    _s("risk.backoff_429_s", 30, 5, 300, aliases=("risk 429",), fmt="{v}s"),
    # desk
    _s("desk.mode", "PAPER", kind="choice", choices=("PAPER", "LIVE"), aliases=("mode",)),
    _s("desk.paper_start_stack", 200.0, 10, 1_000_000, aliases=("paper stack", "paper start"), fmt="${v:,.2f}"),
    _s("desk.live_stack", 0.0, 0, 10_000_000, aliases=("stack", "live stack"), fmt="${v:,.2f}"),
    _s("desk.report_time", "08:00", kind="time", aliases=("report time", "report")),
    _s("desk.notifier", "telegram", kind="choice", choices=("telegram",), aliases=("notifier",)),
    _s("desk.rpc_url", "https://api.mainnet-beta.solana.com", kind="str", aliases=("rpc", "rpc url")),
    _s("desk.push_rejects", False, aliases=("push rejects",)),
    _s("desk.fees_ref_discount", 0.10, 0, 0.5, aliases=("ref discount",), fmt="{v:.0%}"),
    _s("desk.fomo_handle", "cd12345", kind="str", aliases=("fomo handle", "handle")),
    _s("desk.publish_heartbeat_s", 180, 60, 3600, aliases=("publish heartbeat",), fmt="{v}s"),
]
BY_KEY = {s.key: s for s in SPECS}
ALIASES: dict[str, str] = {}
for _sp in SPECS:
    ALIASES[_sp.key] = _sp.key
    ALIASES[_sp.key.split(".", 1)[1].replace("_", " ")] = _sp.key
    ALIASES[_sp.key.split(".", 1)[1]] = _sp.key
    for a in _sp.aliases:
        ALIASES[a] = _sp.key


class SettingError(ValueError):
    pass


def resolve(name: str) -> Spec:
    n = re.sub(r"\s+", " ", name.strip().lower())
    key = ALIASES.get(n) or ALIASES.get(n.replace("-", " "))
    if not key:
        raise SettingError(f"unknown setting '{name}'")
    return BY_KEY[key]


def display(spec: Spec, v) -> str:
    try:
        return spec.fmt.format(v=v)
    except Exception:
        return str(v)


def parse_value(spec: Spec, raw) -> object:
    if not isinstance(raw, str):
        val = raw
    else:
        r = raw.strip().replace(",", "").replace("$", "").rstrip("%").strip()
        if spec.kind == "bool":
            if r.lower() in ("true", "on", "yes", "1"): return True
            if r.lower() in ("false", "off", "no", "0"): return False
            raise SettingError(f"{spec.key} must be on or off")
        if spec.kind == "choice":
            for c in spec.choices:
                if r.lower() == c.lower():
                    return c
            raise SettingError(f"{spec.key} must be one of {', '.join(spec.choices)}")
        if spec.kind == "time":
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", r):
                raise SettingError(f"{spec.key} must be HH:MM (00:00 to 23:59)")
            return r
        if spec.kind == "str":
            if not r:
                raise SettingError(f"{spec.key} can't be empty")
            if spec.key == "desk.rpc_url" and not r.startswith("https://"):
                raise SettingError("rpc_url must start with https://")
            return raw.strip()
        try:
            val = float(r)
        except ValueError:
            raise SettingError(f"{spec.key} needs a number")
    if spec.kind in ("float", "int"):
        val = float(val)
        if spec.neg and val < 0:
            val = -val
        if (spec.lo is not None and val < spec.lo) or (spec.hi is not None and val > spec.hi):
            lo, hi = display(spec, spec.lo), display(spec, spec.hi)
            raise SettingError(f"{spec.key.split('.',1)[1]} must be between {lo} and {hi}")
        if spec.kind == "int":
            if val != int(val):
                raise SettingError(f"{spec.key} must be a whole number")
            val = int(val)
    return val


class Settings:
    """Backed by db.settings; change_log keeps every change."""

    def __init__(self, db):
        self.db = db
        self._cache: dict[str, object] = {}
        self.listeners = []
        self.reload()

    def reload(self):
        rows = dict(self.db.q("SELECT key, value FROM settings"))
        for s in SPECS:
            self._cache[s.key] = json.loads(rows[s.key]) if s.key in rows else s.default

    def get(self, key: str):
        return self._cache[key]

    def __getitem__(self, key):
        return self._cache[key]

    def set(self, name: str, raw, source: str = "telegram", force: bool = False) -> str:
        spec = resolve(name)
        new = parse_value(spec, raw)
        old = self._cache[spec.key]
        if spec.key == "desk.mode" and not force:
            raise SettingError("use 'paper' or 'live' to switch mode")
        self.db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (spec.key, json.dumps(new)))
        self.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
                  (time.time(), spec.key, json.dumps(old), json.dumps(new), source))
        self._cache[spec.key] = new
        label = spec.aliases[0] if spec.aliases else spec.key.split(".", 1)[1]
        msg = f"{label} {display(spec, old)} -> {display(spec, new)}"
        for fn in self.listeners:
            try:
                fn(spec.key, old, new)
            except Exception:
                pass
        return msg

    def as_dict(self):
        return dict(self._cache)

    def specs_public(self):
        return [{"key": s.key, "value": self._cache[s.key], "default": s.default, "min": s.lo, "max": s.hi,
                 "kind": s.kind, "choices": list(s.choices), "display": display(s, self._cache[s.key]),
                 "aliases": list(s.aliases)} for s in SPECS]


def ticket_check(stack: float, stack_pct: float, min_ticket: float, below_min: str) -> dict:
    """STEP 10 ticket check. Returns {'ok':bool,'kind':'ok|bump|skip','msg':str,'min_stack':float}."""
    min_stack = min_ticket / (stack_pct / 100.0)
    if stack >= min_stack:
        return {"ok": True, "kind": "ok", "msg": "", "min_stack": round(min_stack, 2)}
    if below_min == "bump":
        p = min_ticket / stack * 100.0 if stack > 0 else 100.0
        return {"ok": True, "kind": "bump", "min_stack": round(min_stack, 2),
                "msg": f"tickets bumped to ${min_ticket:,.2f} = {p:.2f}% of stack"}
    return {"ok": False, "kind": "skip", "min_stack": round(min_stack, 2),
            "msg": f"every coin will be skipped, stack under ${min_stack:,.2f}. "
                   f"Switch to bump (set below min to bump) or raise the stack."}
