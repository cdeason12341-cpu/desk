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
    _s("scanner.max_age_h", 5, 0, 24, aliases=("max age", "age limit"), fmt="{v:g}h"),   # 0 = off (no age limit)
    _s("scanner.min_market_cap_usd", 30000, 0, 1_000_000_000, aliases=("min mc", "market cap", "min market cap", "mc"), fmt="${v:,.0f}"),   # 0 = off
    _s("scanner.runner_h24_pct", 300, 10, 100000, aliases=("runner",), fmt="+{v:g}%"),
    _s("scanner.max_dex_rpm", 250, 10, 290, aliases=("dex rpm",), fmt="{v}/min"),
    _s("scanner.ws_silent_s", 120, 30, 900, aliases=("ws silent",), fmt="{v}s"),
    _s("scanner.recheck_minutes", "5,10,20,30,45,60", kind="str", aliases=("recheck", "recheck minutes")),
    # near-miss gates (0 = off): a first-scan fixable kill is re-checked only if every failing value is within these
    _s("scanner.recheck_min_liq_usd", 5000, 0, 10_000_000, aliases=("recheck min liq", "near miss liq"), fmt="${v:,.0f}"),
    _s("scanner.recheck_min_mc_usd", 20000, 0, 1_000_000_000, aliases=("recheck min mc", "near miss mc"), fmt="${v:,.0f}"),
    _s("scanner.recheck_max_top10_pct", 40, 0, 100, aliases=("recheck top10", "recheck top 10", "near miss top10", "near miss top 10"), fmt="{v:g}%"),
    _s("scanner.recheck_max_h1_pct", 150, 0, 100000, aliases=("recheck h1", "recheck pump", "near miss h1", "near miss pump"), fmt="+{v:g}%"),
    _s("scanner.recheck_max_queue", 300, 0, 5000, aliases=("recheck queue",), fmt="{v}"),
    _s("scanner.recheck_per_poll", 60, 1, 300, aliases=("recheck per poll",), fmt="{v}"),
    _s("scanner.young_priority_min", 60, 0, 1440, aliases=("young priority", "young first", "young", "young min"), fmt="{v}m"),   # 0 = off (plain order)
    _s("scanner.surge_pct", 25, 1, 1000, aliases=("surge pct", "surge", "surge percent"), fmt="+{v:g}%"),
    _s("scanner.surge_window_s", 60, 10, 900, aliases=("surge window", "surge secs"), fmt="{v}s"),
    _s("scanner.surge_watch_max", 150, 0, 300, aliases=("surge watch", "surge max", "surge cap"), fmt="{v}"),   # 0 = off
    _s("scanner.surge_poll_s", 10, 5, 60, aliases=("surge poll",), fmt="{v}s"),
    _s("scanner.backoff_429_s", 30, 5, 300, aliases=("scanner 429",), fmt="{v}s"),
    # owner 2026-11-16 5:22 PM CT "look at not only pump fun coins" (DECISION 51): all = pump.fun + other launchpads/DEXes, pump = pump mints only (old)
    _s("scanner.sources", "all", kind="choice", choices=("all", "pump"), aliases=("sources", "scan sources", "scanner sources", "launchpads")),
    _s("scanner.other_max_mc_usd", 10_000_000, 0, 1_000_000_000, aliases=("other max mc", "max mc other", "non pump max mc"), fmt="${v:,.0f}"),   # 0 = off; non-pump coins only
    # auditor
    _s("auditor.enabled", True, aliases=("auditor",)),
    _s("auditor.max_dev_pct", 5, 0, 100, aliases=("dev", "max dev"), fmt="{v:g}%"),
    _s("auditor.max_top10_pct", 25, 1, 100, aliases=("auditor top10",), fmt="{v:g}%"),
    _s("auditor.min_lp_locked_pct", 99, 0, 100, aliases=("lp locked", "lp"), fmt="{v:g}%"),
    _s("auditor.max_liq_drop_pct", 50, 1, 100, aliases=("liq pulled",), fmt="{v:g}%"),
    _s("auditor.min_pool_age_min", 10, 0, 240, aliases=("pool age",), fmt="{v}m"),
    _s("auditor.dev_exit_wait_s", 60, 0, 3600, aliases=("dev exit wait", "dev out wait"), fmt="{v}s"),
    _s("auditor.dev_exit_max_drop_pct", 20, 1, 100, aliases=("dev exit drop", "dev out drop"), fmt="-{v:g}%", neg=True),
    _s("auditor.max_bundled", 1000, 1, 1000, aliases=("bundled", "bundles"), fmt="{v}"),
    _s("auditor.max_bundle_pct", 20, 1, 100, aliases=("bundle pct", "bundle percent"), fmt="{v:g}%"),
    _s("auditor.max_delegated_accounts", 10, 1, 10000, aliases=("delegations", "delegated"), fmt="{v}"),
    _s("auditor.retries", 3, 0, 10, aliases=("retries",), fmt="{v}"),
    _s("auditor.retry_gap_s", 2, 0, 30, aliases=("retry gap",), fmt="{v}s"),
    # analyst
    _s("analyst.mode", "jev", kind="choice", choices=("jev", "rules"), aliases=("analyst mode", "analyst")),
    _s("analyst.min_alive_6h", 0.70, 0, 1, aliases=("alive", "min alive"), fmt="{v:.2f}"),
    _s("analyst.min_organic", 0.65, 0, 1, aliases=("organic", "min organic"), fmt="{v:.2f}"),
    _s("analyst.max_chase", 0.30, 0, 1, aliases=("chase", "max chase"), fmt="{v:.2f}"),
    _s("analyst.min_score", 70, 0, 100, aliases=("score", "min score"), fmt="{v}"),
    _s("analyst.paper_bypass", False, aliases=("paper bypass", "analyst bypass")),   # PAPER only: failed coins still paper-fill; never GMGN
    _s("analyst.timeout_s", 10, 2, 60, aliases=("jev timeout",), fmt="{v}s"),
    _s("analyst.state_max_chars", 3000, 500, 3000, aliases=("state chars",), fmt="{v}"),
    _s("analyst.queue_429_s", 60, 5, 600, aliases=("jev 429",), fmt="{v}s"),
    _s("analyst.rules_alive_volume_h1", 25000, 100, 10_000_000, aliases=("alive volume",), fmt="${v:,.0f}"),
    _s("analyst.rules_organic_buys_h1", 150, 1, 100000, aliases=("organic buys",), fmt="{v}"),
    _s("analyst.rules_bundled_penalty", 0.5, 0, 1, aliases=("bundled penalty",), fmt="x{v:g}"),
    _s("analyst.rules_bundled_from", 5, 1, 1000, aliases=("bundled from",), fmt="{v}"),
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
    # 5-min stop-loss grace on new GMGN LIVE buys (owner 2026-11-16 2:44 PM CT, DECISION 48). 0 min = off.
    _s("risk.grace_min", 5, 0, 60, aliases=("grace", "grace min", "stop grace"), fmt="{v}m"),
    _s("risk.grace_stop_pct", 30, 1, 90, aliases=("grace stop", "steep drop", "grace stop loss"), fmt="-{v:g}%", neg=True),
    # keep the grace stop after the window if price is still below the normal stop (owner 2026-11-16 9:27 PM CT, DECISION 65)
    _s("risk.grace_extend_below_stop", True, aliases=("grace extend", "extend grace", "grace extend below stop")),
    # surge-top sell on open GMGN positions (owner 2026-11-16 4:26 PM CT, DECISION 49)
    _s("risk.surge_sell_enabled", True, aliases=("surge sell", "sell surge", "surge top sell")),
    _s("risk.surge_min_pct", 25.0, 1, 1000, aliases=("surge sell min", "surge sell pct"), fmt="+{v:g}%"),
    _s("risk.surge_window_s", 180, 10, 3600, aliases=("surge sell window",), fmt="{v}s"),
    _s("risk.surge_top_drop_pct", 8.0, 1, 90, aliases=("surge top drop", "top drop"), fmt="{v:g}%"),
    # surge profit floor (owner 2026-11-16 6:45 PM CT, DECISION 56): once armed, also sell at entry x (1 + this %). 0 = off.
    _s("risk.surge_floor_pct", 5.0, 0, 100, aliases=("surge floor", "profit floor", "surge lock"), fmt="+{v:g}%"),
    # a surge counts only once price is this far above entry, on top of the fast-rise rule (owner 2026-11-16 7:38 PM CT, DECISION 61). 0 = off.
    _s("risk.surge_min_entry_pct", 25.0, 0, 1000, aliases=("surge min entry", "surge entry min", "surge above entry"), fmt="+{v:g}%"),
    # real-time exit price for open LIVE GMGN positions (owner 2026-11-16 6:52 PM CT, DECISION 57): pool = pool reserves over
    # RPC -> GMGN sell quote -> DexScreener; dex = DexScreener only (the old feed). Poll used while a LIVE position is open.
    _s("risk.price_source", "pool", kind="choice", choices=("pool", "dex"), aliases=("price source", "exit price source")),
    _s("risk.fast_poll_s", 1.5, 0.5, 10, aliases=("fast poll", "exit poll", "price poll"), fmt="{v:g}s"),
    # desk
    _s("desk.mode", "PAPER", kind="choice", choices=("PAPER", "LIVE"), aliases=("mode",)),
    _s("desk.paper_start_stack", 200.0, 10, 1_000_000, aliases=("paper stack", "paper start"), fmt="${v:,.2f}"),
    _s("desk.live_stack", 0.0, 0, 10_000_000, aliases=("stack", "live stack"), fmt="${v:,.2f}"),
    _s("desk.report_time", "08:00", kind="time", aliases=("report time", "report")),
    _s("desk.notifier", "telegram", kind="choice", choices=("telegram",), aliases=("notifier",)),
    _s("desk.rpc_url", "https://api.mainnet-beta.solana.com", kind="str", aliases=("rpc", "rpc url")),
    _s("desk.push_rejects", False, aliases=("push rejects",)),
    _s("alerts.stage_pings", True, aliases=("stage pings", "pings")),   # owner ping on each stage pass (DECISION 38)
    _s("desk.fees_ref_discount", 0.10, 0, 0.5, aliases=("ref discount",), fmt="{v:.0%}"),
    _s("desk.fomo_handle", "cd12345", kind="str", aliases=("fomo handle", "handle")),
    _s("desk.publish_heartbeat_s", 180, 60, 3600, aliases=("publish heartbeat",), fmt="{v}s"),
    # gmgn hands-off mode (GMGN Agent API, main wallet only). mode/caps/gate are PROTECTED: see PROTECTED below.
    _s("gmgn.enabled", True, aliases=("gmgn", "hands off")),
    _s("gmgn.mode", "PAPER", kind="choice", choices=("PAPER", "LIVE"), aliases=("gmgn mode",)),
    _s("gmgn.cap_trade_usd", 50.0, 5, 10000, aliases=("gmgn cap", "cap trade"), fmt="${v:,.2f}"),
    _s("gmgn.max_tickets_night", 4, 1, 50, aliases=("gmgn tickets", "cap tickets"), fmt="{v}"),
    _s("gmgn.fallback_trade_usd", 20.0, 1, 10000, aliases=("gmgn fallback", "fallback trade"), fmt="${v:,.2f}"),   # 2nd size when the top size doesn't fit
    _s("gmgn.min_trade_usd", 1.0, 0.5, 10000, aliases=("gmgn min trade", "min trade"), fmt="${v:,.2f}"),          # floor: below the fallback, buy what free SOL covers down to this
    # DECISION 68 (owner 2026-11-17 10:20 AM CT): Strategy A PAPER bankroll. Not protected; not used for LIVE or Strategy B.
    _s("gmgn.paper_bankroll_usd", 2500.0, 1, 1_000_000, aliases=("a paper bankroll", "paper bankroll"), fmt="${v:,.2f}"),
    _s("gmgn.paper_trade_usd", 500.0, 1, 10000, aliases=("a paper trade", "paper trade size"), fmt="${v:,.2f}"),
    _s("gmgn.max_open", 4, 1, 20, aliases=("gmgn max open", "max open"), fmt="{v}"),                     # LIVE positions at once (pending+open+exiting)
    # DECISION 71: share of the one GMGN wallet each strategy may use while LIVE (100 = whole wallet, old behaviour)
    _s("gmgn.live_wallet_share_pct", 100.0, 0, 100, aliases=("a wallet share", "a share"), fmt="{v:g}%"),
    _s("gmgn.live_gate_h", 2.0, 0.5, 48, aliases=("gmgn gate",), fmt="{v:g}h"),
    _s("gmgn.slippage_pct", 15, 1, 50, aliases=("gmgn slippage", "slippage"), fmt="{v:g}%"),
    _s("gmgn.priority_fee_sol", 0.0002, 0, 0.01, aliases=("gmgn priority fee", "priority fee"), fmt="{v:g} SOL"),
    _s("gmgn.tip_fee_sol", 0.001, 0, 0.01, aliases=("gmgn tip", "tip fee"), fmt="{v:g} SOL"),
    _s("gmgn.sol_reserve", 0.02, 0.005, 1, aliases=("gmgn reserve", "sol reserve"), fmt="{v:g} SOL"),
    _s("gmgn.fee_pct", 1.0, 0, 5, aliases=("gmgn fee",), fmt="{v:g}%"),
    _s("gmgn.watch_s", 60, 15, 600, aliases=("gmgn watch",), fmt="{v}s"),
    # LIVE buys above this market cap wait for the owner's Approve on Telegram (DECISION 53). 0 = off.
    _s("gmgn.approve_above_mc_usd", 0, 0, 1e12, aliases=("approve above mc", "approval mc", "mc approval"), fmt="${v:,.0f}"),
    # DECISION 64 (owner 2026-11-16 8:40–8:42 PM CT): wait for a small dip after approval, then buy instantly.
    # Timeout buys at market (does not skip). No bounce. 0 dip_pct = buy on the first tick.
    _s("gmgn.dip_enabled", True, aliases=("dip", "dip buy", "wait for dip")),
    _s("gmgn.dip_pct", 5.0, 0, 50, aliases=("dip pct", "dip percent"), fmt="{v:g}%"),
    _s("gmgn.dip_timeout_s", 30, 1, 600, aliases=("dip timeout", "dip wait"), fmt="{v:g}s"),
    # re-entry after a LIVE GMGN exit (owner 2026-11-15 8:52 PM CT, DECISION 46)
    _s("gmgn.reentry_enabled", True, aliases=("reentry", "re-entry", "re entry")),
    _s("gmgn.reentry_watch_min", 60, 5, 720, aliases=("reentry watch", "re-entry watch"), fmt="{v}m"),
    _s("gmgn.reentry_bounce_pct", 7.0, 1, 500, aliases=("reentry bounce", "re-entry bounce", "bounce"), fmt="+{v:g}%"),
    _s("gmgn.reentry_max_per_coin", 1, 0, 5, aliases=("reentry max", "re-entry max"), fmt="{v}"),
    _s("gmgn.reentry_poll_s", 15, 5, 120, aliases=("reentry poll", "re-entry poll"), fmt="{v}s"),
    _s("gmgn.reentry_max_liq_drop_pct", 50, 1, 100, aliases=("reentry liq drop", "re-entry liq drop"), fmt="{v:g}%"),
    # STRATEGY B (owner 2026-11-16 6:50 PM CT, DECISION 54; LIVE allowed 2026-11-17 8:13 AM CT, DECISION 66). A separate
    # strategy built from the research Page "What top memecoin traders do vs the desk". strat_b.mode PAPER|LIVE, force-only.
    _s("strat_b.enabled", True, aliases=("strategy b", "strat b")),
    _s("strat_b.mode", "PAPER", kind="choice", choices=("PAPER", "LIVE"), aliases=("strategy b mode", "strat b mode")),
    _s("strat_b.bankroll_usd", 1000.0, 100, 1_000_000, aliases=("b bankroll",), fmt="${v:,.2f}"),
    _s("strat_b.trade_usd", 50.0, 1, 10000, aliases=("b trade", "b size"), fmt="${v:,.2f}"),
    _s("strat_b.max_open", 5, 1, 50, aliases=("b max open",), fmt="{v}"),
    _s("strat_b.daily_loss_stop_pct", 10.0, 1, 100, aliases=("b daily loss", "b loss stop"), fmt="-{v:g}%", neg=True),
    # DECISION 69 (owner 2026-11-17 5:23 PM CT): LIVE daily loss stop is real money, not the paper bankroll.
    # DECISION 71: B LIVE's own wallet share and its own LIVE slots / tickets (no longer shared with Strategy A)
    _s("strat_b.live_wallet_share_pct", 100.0, 0, 100, aliases=("b wallet share", "b share"), fmt="{v:g}%"),
    _s("strat_b.live_max_open", 4, 1, 20, aliases=("b live max open",), fmt="{v}"),
    _s("strat_b.live_max_tickets_night", 4, 1, 50, aliases=("b live tickets",), fmt="{v}"),
    _s("strat_b.live_daily_loss_usd", 20.0, 1, 1_000_000, aliases=("b live daily loss", "b live loss"), fmt="${v:,.2f}"),
    _s("strat_b.min_age_min", 60, 0, 1440, aliases=("b min age", "b wait"), fmt="{v}m"),
    _s("strat_b.watch_max_min", 180, 10, 10080, aliases=("b watch",), fmt="{v}m"),
    # DECISION 63 (owner 2026-11-16 8:23 PM CT): coins up to 7 days old, and B's own feed of older coins (GeckoTerminal trending)
    _s("strat_b.max_age_min", 10080, 60, 43200, aliases=("b max age",), fmt="{v}m"),
    _s("strat_b.max_watch", 90, 5, 300, aliases=("b max watch",), fmt="{v}"),
    _s("strat_b.watch_poll_s", 30, 10, 600, aliases=("b watch poll",), fmt="{v}s"),
    _s("strat_b.feed_enabled", True, aliases=("b feed",)),
    _s("strat_b.feed_poll_s", 300, 60, 3600, aliases=("b feed poll",), fmt="{v}s"),
    _s("strat_b.feed_min_age_min", 60, 0, 43200, aliases=("b feed min age",), fmt="{v}m"),
    _s("strat_b.feed_checks_per_poll", 3, 1, 20, aliases=("b feed checks",), fmt="{v}"),
    _s("strat_b.min_mc_usd", 30000, 0, 1e12, aliases=("b min mc",), fmt="${v:,.0f}"),
    _s("strat_b.max_mc_usd", 1_000_000, 1000, 1e12, aliases=("b max mc",), fmt="${v:,.0f}"),
    _s("strat_b.min_liq_usd", 10000, 0, 1e9, aliases=("b min liq",), fmt="${v:,.0f}"),
    _s("strat_b.max_pool_pct", 2.0, 0.1, 50, aliases=("b pool pct",), fmt="{v:g}%"),
    _s("strat_b.max_top10_pct", 20.0, 1, 100, aliases=("b top10", "b top 10"), fmt="{v:g}%"),
    _s("strat_b.min_holders", 75, 0, 100000, aliases=("b holders",), fmt="{v}"),
    _s("strat_b.max_bundle_pct", 5.0, 0, 100, aliases=("b bundle",), fmt="{v:g}%"),
    _s("strat_b.min_vol_mc", 0.5, 0, 100, aliases=("b volume", "b vol mc"), fmt="{v:g}x"),
    _s("strat_b.pullback_min_pct", 15.0, 1, 90, aliases=("b pullback min",), fmt="-{v:g}%", neg=True),
    _s("strat_b.pullback_max_pct", 40.0, 1, 95, aliases=("b pullback max",), fmt="-{v:g}%", neg=True),
    _s("strat_b.bounce_pct", 5.0, 0, 100, aliases=("b bounce",), fmt="+{v:g}%"),
    _s("strat_b.stop_pct", 35.0, 1, 90, aliases=("b stop",), fmt="-{v:g}%", neg=True),
    _s("strat_b.take_profit_pct", 100.0, 5, 10000, aliases=("b take profit", "b tp"), fmt="+{v:g}%"),
    _s("strat_b.trail_start_pct", 50.0, 1, 10000, aliases=("b trail start",), fmt="+{v:g}%"),
    _s("strat_b.trail_pct", 20.0, 1, 90, aliases=("b trail",), fmt="{v:g}%"),
    _s("strat_b.spike_pct", 30.0, 1, 1000, aliases=("b spike",), fmt="+{v:g}%"),
    _s("strat_b.spike_window_s", 60, 10, 3600, aliases=("b spike window",), fmt="{v}s"),
    _s("strat_b.time_stop_h", 24.0, 0.25, 168, aliases=("b time stop",), fmt="{v:g}h"),
    _s("strat_b.slippage_pct", 1.0, 0, 50, aliases=("b slippage",), fmt="{v:g}%"),
    _s("strat_b.fee_pct", 1.0, 0, 10, aliases=("b fee",), fmt="{v:g}%"),
    _s("strat_b.net_fee_usd", 0.25, 0, 10, aliases=("b network fee",), fmt="${v:,.2f}"),
    _s("strat_b.poll_s", 10, 3, 120, aliases=("b poll",), fmt="{v}s"),
]
# Only changeable with force=True: desk.mode/gmgn.mode via paper|live commands, caps via the owner's
# Telegram 'cap ...' + 'CONFIRM CAP', gate only by a recorded owner rule change.
CAP_KEYS = ("gmgn.cap_trade_usd", "gmgn.max_tickets_night", "gmgn.fallback_trade_usd", "gmgn.min_trade_usd", "gmgn.max_open")
PROTECTED = {"desk.mode", "gmgn.mode", "gmgn.live_gate_h", "strat_b.mode", *CAP_KEYS,
             # DECISION 71: wallet split + B's own LIVE caps are money limits too (owner command, force=True)
             "gmgn.live_wallet_share_pct", "strat_b.live_wallet_share_pct", "strat_b.live_max_open", "strat_b.live_max_tickets_night"}   # strat_b.mode: PAPER|LIVE, owner command only (DECISION 66)
BY_KEY = {s.key: s for s in SPECS}
ALIASES: dict[str, str] = {}
for _sp in SPECS:
    ALIASES[_sp.key] = _sp.key
    if _sp.key.startswith("strat_b."):          # Strategy B never takes a bare name ("stop pct" stays risk.stop_pct)
        ALIASES.setdefault("strat b " + _sp.key.split(".", 1)[1].replace("_", " "), _sp.key)
        for a in _sp.aliases:
            ALIASES.setdefault(a, _sp.key)
        continue
    _put = ALIASES.setdefault if _sp.key.startswith("gmgn.") else ALIASES.__setitem__   # gmgn never steals a bare name
    _put(_sp.key.split(".", 1)[1].replace("_", " "), _sp.key)
    _put(_sp.key.split(".", 1)[1], _sp.key)
    for a in _sp.aliases:
        ALIASES[a] = _sp.key


class SettingError(ValueError):
    pass


def normalize_recheck_minutes(raw) -> str:
    """'5, 10,20' -> '5,10,20'; 'off' disables. Ages in minutes, 1..1440, strictly increasing."""
    r = str(raw).strip().lower()
    if r in ("off", "none", "0"):
        return "off"
    try:
        vals = [float(x) for x in r.replace(" ", "").split(",") if x]
    except ValueError:
        raise SettingError("scanner.recheck_minutes must be minutes like 5,10,20,30,45,60 or off")
    if not vals or any(v < 1 or v > 1440 for v in vals) or any(b <= a for a, b in zip(vals, vals[1:])):
        raise SettingError("scanner.recheck_minutes must be increasing minutes between 1 and 1440, or off")
    return ",".join(f"{v:g}" for v in vals)


def resolve(name: str) -> Spec:
    n = re.sub(r"\s+", " ", name.strip().lower())
    key = ALIASES.get(n) or ALIASES.get(n.replace("-", " "))
    if not key:
        raise SettingError(f"unknown setting '{name}'")
    return BY_KEY[key]


OFF_ZERO = {"scanner.max_age_h", "scanner.min_market_cap_usd", "scanner.recheck_min_liq_usd",
            "scanner.recheck_min_mc_usd", "scanner.recheck_max_top10_pct", "scanner.recheck_max_h1_pct",
            "scanner.young_priority_min", "scanner.surge_watch_max", "scanner.other_max_mc_usd"}   # 0 means off for these


def display(spec: Spec, v) -> str:
    if spec.key in OFF_ZERO and v is not None and float(v) == 0:
        return "off"
    try:
        return spec.fmt.format(v=v)
    except Exception:
        return str(v)


def parse_value(spec: Spec, raw) -> object:
    if spec.key in OFF_ZERO and isinstance(raw, str) and raw.strip().lower() in ("off", "none", "no limit"):
        raw = "0"
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
            if spec.key == "scanner.recheck_minutes":
                return normalize_recheck_minutes(raw)
            return raw.strip()
        mult = 1.0
        if spec.key.endswith("_usd") and r[-1:].lower() in ("k", "m"):   # "30k" / "1.5m"
            mult, r = (1e3 if r[-1].lower() == "k" else 1e6), r[:-1]
        try:
            val = float(r) * mult
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
        self._seen_log_id = self._log_id()

    def reload(self):
        rows = dict(self.db.q("SELECT key, value FROM settings"))
        for s in SPECS:
            self._cache[s.key] = json.loads(rows[s.key]) if s.key in rows else s.default

    def _log_id(self) -> int:
        r = self.db.q("SELECT COALESCE(MAX(id), 0) FROM change_log")
        return int(r[0][0]) if r else 0

    def sync(self) -> list[str]:
        """Pick up settings written to the DB outside this process (logged in change_log) without a restart.
        PROTECTED keys (modes, GMGN caps, gate) are never hot-loaded: they change only via their Telegram flows or a restart."""
        last = self._log_id()
        if last == getattr(self, "_seen_log_id", None):
            return []
        self._seen_log_id = last
        rows = dict(self.db.q("SELECT key, value FROM settings"))
        changed = []
        for sp in SPECS:
            if sp.key in PROTECTED:
                continue
            new = json.loads(rows[sp.key]) if sp.key in rows else sp.default
            old = self._cache.get(sp.key)
            if new != old:
                self._cache[sp.key] = new
                changed.append(sp.key)
                for fn in self.listeners:
                    try:
                        fn(sp.key, old, new)
                    except Exception:
                        pass
        return changed

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
        if spec.key in CAP_KEYS and not force:
            raise SettingError("caps change only with 'cap trade N' / 'cap tickets N' + CONFIRM CAP in Telegram")
        if spec.key in PROTECTED and not force:
            raise SettingError(f"{spec.key} can't be changed with set")
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
