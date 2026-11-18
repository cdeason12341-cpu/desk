"""AGENT 2 · AUDITOR. 8 layers in order; one red layer and the coin is dead. No data is never a pass, except layer 8 when auditor.l8_bypass_on_down is on and getProgramAccounts is unavailable (DECISION 74)."""
from __future__ import annotations
import stage_pings
import asyncio, json, time
import devexit, fields, holders, solana
from ctx import iso

LAYERS = [(1, "Mint authority"), (2, "Freeze authority"), (3, "Dev wallet"), (4, "Top 10 holders"),
          (5, "Liquidity"), (6, "Pool age"), (7, "Bundled wallets"), (8, "Delegations")]


class Red(Exception):
    def __init__(self, value, source):
        super().__init__(value)
        self.value, self.source = value, source


def biggest_group(graph) -> int | None:
    """RugCheck insiders graph = list of networks {nodes:[{id,holdings}], links:[{source,target}]}.
    [] -> 0 bundled (DECISIONS #1). Not a list / a network without links|nodes -> None (no data)."""
    if not isinstance(graph, list):
        return None
    holding, edges = set(), []
    for net in graph:
        if not isinstance(net, dict) or "links" not in net or "nodes" not in net:
            return None
        for n in net.get("nodes") or []:
            if (n.get("holdings") or 0) > 0:          # keep only wallets that hold this mint
                holding.add(n.get("id"))
        edges += [(l.get("source"), l.get("target")) for l in net.get("links") or []]
    parent = {w: w for w in holding}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a, b in edges:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    sizes: dict = {}
    for w in holding:
        r = find(w)
        sizes[r] = sizes.get(r, 0) + 1
    big = max(sizes.values()) if sizes else 0
    return big if big >= 2 else 0


def bundle_stats(report, supply: int | None = None) -> tuple[float | None, int | None]:
    """% of supply held by all RugCheck insider networks, and size of the largest network.

    Source (verified 2026-11-14 on Nyajira 4qyk…pump and Fartcoin 9BB6…pump):
      report["insiderNetworks"][]["currentHolding"]  — raw amount still held
      report["insiderNetworks"][]["size"]            — wallets in that network
      report["token"]["supply"]                      — raw supply
    Sum currentHolding across every network ("held by bundles"), not just the biggest.
    insiderNetworks is null (or []) when RugCheck detected none — that is 0%, 0 linked,
    same convention as an empty insiders/graph (DECISIONS #1). A present but unreadable
    list, or networks with no usable supply, is no data (None, None).
    """
    if not isinstance(report, dict):
        return None, None
    nets = report.get("insiderNetworks")
    if nets is None:
        return 0.0, 0
    if not isinstance(nets, list):
        return None, None
    if not nets:
        return 0.0, 0
    held, linked = 0, 0
    for n in nets:
        if not isinstance(n, dict) or "currentHolding" not in n:
            return None, None
        try:
            held += int(n.get("currentHolding") or 0)
        except (TypeError, ValueError):
            return None, None
        if "size" in n:
            try:
                linked = max(linked, int(n["size"] or 0))
            except (TypeError, ValueError):
                pass
    if supply is None:
        try:
            supply = int(((report.get("token") or {}).get("supply")) or 0)
        except (TypeError, ValueError):
            supply = 0
    if not supply:
        return None, None
    return 100.0 * held / supply, linked


def bundle_layer(report, max_bundle_pct, supply: int | None = None) -> tuple[bool, str, float | None, int | None]:
    """Layer 7 verdict: (passed, value, pct, linked). Count is informational; only % rejects."""
    pct, linked = bundle_stats(report, supply)
    if pct is None:
        return False, "no data: bundles", None, None
    if pct > max_bundle_pct:
        return False, f"{pct:.0f}% held by bundles", pct, linked
    if (linked or 0) == 0 and pct == 0:
        return True, "None", pct, linked
    return True, f"{linked} linked, {pct:.1f}% of supply", pct, linked


def short(addr: str) -> str:
    return f"{addr[:4]}...{addr[-3:]}" if addr and len(addr) > 8 else (addr or "?")


L8_SKIP_VALUE = "L8=skipped (rpc down)"
L8_SKIP_NOTE = "⚠️ L8 skipped (rpc down)"


def layer8_verdict(accounts, max_delegated, *, error=None, bypass=False) -> tuple[bool, str, bool]:
    """Layer 8: (passed, value, skipped). error / missing accounts = no data (bypass → pass-with-warning)."""
    if accounts is None or error:
        if bypass:
            return True, L8_SKIP_VALUE, True
        return False, "no data: delegations", False
    groups = holders.delegate_groups(accounts)
    if groups and groups[0][1] >= max_delegated:
        return False, f"{groups[0][1]} wallets → {short(groups[0][0])}", False
    return True, "None" if not groups else f"max {groups[0][1]} → {short(groups[0][0])}", False


def audit_l8_skipped(audit) -> bool:
    if not isinstance(audit, dict):
        return False
    if audit.get("l8_skipped"):
        return True
    layers = audit.get("layers") or []
    l8 = next((L for L in layers if isinstance(L, dict) and L.get("n") == 8), None)
    return bool(l8 and L8_SKIP_VALUE in str((l8 or {}).get("value") or ""))


def mint_l8_skipped(ctx, mint) -> bool:
    try:
        r = ctx.db.one("SELECT json FROM audits WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
    except Exception:
        return False
    if not r:
        return False
    try:
        return audit_l8_skipped(json.loads(r["json"] or "{}"))
    except Exception:
        return False


def tag_l8_skip(text, skipped) -> str:
    """Append the L8-skipped warning to the first line of a notification, once."""
    if not skipped or not text or L8_SKIP_NOTE in text:
        return text
    first, sep, rest = text.partition("\n")
    return first + " · " + L8_SKIP_NOTE + ((sep + rest) if sep else "")


class Auditor:
    name = "auditor"

    def __init__(self, ctx):
        self.c = ctx
        self.log = ctx.logs["auditor"]
        self.live: dict[str, dict] = {}
        self.gpa_down_alerted = False

    async def loop(self):
        while True:
            self.c.hb("auditor", f"queue={self.c.bus.auditor.qsize()}")
            try:
                scan = await asyncio.wait_for(self.c.bus.auditor.get(), timeout=20)
            except asyncio.TimeoutError:
                continue
            while "auditor" in self.c.paused:
                self.c.hb("auditor", "paused")
                await asyncio.sleep(5)
            try:
                res = await self.run(scan["mint"], scan)
                if res and res["passed"] and self.c.bus:
                    self.c.bus.analyst.put_nowait({"scan": scan, "audit": res})
                sb = getattr(self.c, "strat_b", None)
                if res and res["passed"] and sb is not None:
                    sb.offer(scan, res)                      # Strategy B (paper) watches it too; never raises, never blocks
            except Exception as e:
                self.c.error("auditor", "run", f"{scan.get('mint')} {str(e)[:300]}")

    async def _src(self, fn, *a, **k):
        """retry [3] times, [2]s apart; still nothing -> raises (caller turns it red 'no data')."""
        tries, gap, last = int(self.c.s["auditor.retries"]), float(self.c.s["auditor.retry_gap_s"]), None
        for i in range(tries + 1):
            try:
                return await fn(*a, **k)
            except Exception as e:
                last = e
                if i < tries:
                    await asyncio.sleep(gap)
        raise last

    async def dev_exit(self, creator, mint, mi, raw_log) -> tuple[bool, str]:
        """Owner rule 2026-11-15 12:48 CT: a dev who is fully out, has been out > dev_exit_wait_s, and the price
        has not fallen more than dev_exit_max_drop_pct since that last sell, is not a red flag."""
        c = self.c
        program = fields.get(mi, "value.owner") or solana.TOKEN_PROGRAM
        try:
            sold_at, note = await self._src(devexit.last_dev_sell, c.net, creator, mint, program)
        except Exception as e:
            sold_at, note = None, f"rpc {str(e)[:80]}"
        if sold_at is None:
            raw_log.append(f"dev_exit unknown: {note}")
            return False, "dev sold (sell time unknown)"
        now = c.clock()
        since = now - sold_at
        rows = [(r["ts"], r["price"]) for r in c.db.rows(
            "SELECT ts, price FROM prices WHERE mint=? AND ts BETWEEN ? AND ?",
            (mint, sold_at - devexit.PRICE_WINDOW_S, sold_at + devexit.PRICE_WINDOW_S))]
        sell_price = devexit.price_near(rows, sold_at)
        now_price = m5 = h1 = None
        try:
            from agents.scanner import pick_pair
            p = pick_pair(await self._src(c.net.dex_tokens, [mint]), mint)
            if p:
                now_price = float(fields.get(p, "priceUsd") or 0) or None
                m5, h1 = fields.get(p, "priceChange.m5"), fields.get(p, "priceChange.h1")
        except Exception:
            pass
        ok, txt = devexit.judge(c.s, since, sell_price if now_price else None, now_price, m5, h1)
        raw_log.append(f"dev_exit sold_at={sold_at:.0f} since={since:.0f}s sell_px={sell_price} now_px={now_price} m5={m5} h1={h1} -> {ok} {txt}")
        return ok, txt

    async def run(self, mint: str, scan: dict | None, record: bool = True, on_layer=None, reentry: bool = False) -> dict | None:
        """scan=None means an on-demand audit (Telegram 'buy X' for a coin the desk never pushed)."""
        c, s = self.c, self.c.s
        if record:
            prev = c.db.one("SELECT passed FROM audits WHERE mint=? AND night=? ORDER BY id DESC LIMIT 1", (mint, c.night))
            if prev is not None and not prev["passed"]:
                return None
        coin = c.coin(mint) or {}
        if not s["auditor.enabled"]:
            return {"agent": "auditor", "mint": mint, "ts": iso(), "passed": True, "layersChecked": 0,
                    "layers": [], "holders": None, "reason": None, "bundled": None, "disabled": True}
        pool = (scan or {}).get("pool") or coin.get("pool")
        layers = [{"n": n, "name": nm, "pass": None, "value": "not checked", "source": None, "ms": None} for n, nm in LAYERS]
        state = {"agent": "auditor", "mint": mint, "ts": iso(), "passed": False, "layersChecked": 0,
                 "layers": layers, "holders": None, "reason": None, "running": True}
        self.live[mint] = state
        cache: dict = {}
        self._acct_cache = cache          # test hook: clear after layer 4 to exercise L8's own no-data path
        bundled = None
        bundle_pct = None
        raw_log = []

        async def report():
            if "report" not in cache:                     # each RugCheck endpoint once per audit
                cache["report"] = await self._src(c.net.rug_report, mint)
            return cache["report"]

        async def accounts():
            if "accts_err" in cache:
                raise RuntimeError(cache["accts_err"])
            if "accts" not in cache:
                try:
                    cache["accts"] = await self._src(holders.fetch_accounts, c.net, mint)
                except Exception as e:
                    cache["accts_err"] = str(e)
                    await self.gpa_down(e)
                    raise
            return cache["accts"]

        async def mint_info():
            if "mi" not in cache:
                cache["mi"] = await self._src(c.net.rpc, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            return cache["mi"]

        passed = True
        for i, (n, nm) in enumerate(LAYERS):
            t0 = time.time()
            L = layers[i]
            try:
                if n in (1, 2):
                    what = "mint" if n == 1 else "freeze"
                    try:
                        info = fields.get(await mint_info(), "value.data.parsed.info")
                    except Exception:
                        info = None
                    key = "mintAuthority" if n == 1 else "freezeAuthority"
                    if not isinstance(info, dict) or key not in info:
                        raise Red(f"no data: {what}", "rpc")
                    if info[key] is not None:
                        raise Red(f"{what} not revoked", "rpc")
                    L.update(value="Revoked", source="rpc")
                elif n == 3:
                    info = fields.get(await mint_info(), "value.data.parsed.info") or {}
                    try:
                        supply = int(info.get("supply") or 0)
                    except Exception:
                        supply = 0
                    if supply <= 0:
                        raise Red("no supply", "rpc")
                    cache["supply"] = supply
                    creator = coin.get("creator")
                    if not creator and scan is None:
                        try:
                            creator = fields.get(await report(), "creator")
                        except Exception:
                            creator = None
                    if not creator:
                        raise Red("no data: dev", "rugcheck")
                    from agents.scanner import creator_balance
                    now_bal = None
                    for _ in range(int(s["auditor.retries"]) + 1):
                        now_bal = await creator_balance(c.net, creator, mint)
                        if now_bal is not None:
                            break
                        await asyncio.sleep(s["auditor.retry_gap_s"])
                    if now_bal is None:
                        raise Red("no data: dev", "rpc")
                    at_mig = coin.get("creator_balance_raw")
                    pct = 100.0 * now_bal / supply
                    raw_log.append(f"dev={creator} now={now_bal} at_mig={at_mig} pct={pct:.3f}")
                    if pct >= s["auditor.max_dev_pct"]:
                        raise Red(f"dev holds {pct:.0f}%", "rpc")
                    if at_mig is None:
                        if scan is not None:
                            raise Red("no data: dev", "rpc")
                        sold_txt = "sold ? (not on desk)"
                    else:
                        if now_bal < int(at_mig):
                            if pct >= devexit.DEV_OUT_PCT:          # sold some, still holds: unchanged
                                raise Red("dev sold", "rpc")
                            ok, txt = await self.dev_exit(creator, mint, await mint_info(), raw_log)
                            if not ok:
                                raise Red(txt, "rpc")
                            sold_txt = txt
                        else:
                            sold_txt = "sold 0"
                    L.update(value=f"{pct:.1f}% · {sold_txt}", source="rpc")
                elif n == 4:
                    try:
                        acc = await accounts()
                    except Exception:
                        raise Red("no data: top 10", "rpc")
                    try:
                        amm = [k for k, v in (fields.get(await report(), "knownAccounts") or {}).items() if (v or {}).get("type") == "AMM"]
                    except Exception:
                        amm = []
                    pct = holders.top10_pct(acc, cache["supply"], holders.excluded_owners(pool, mint, amm))
                    if pct is None:
                        raise Red("no data: top 10", "rpc")
                    raw_log.append(f"top10={pct}")
                    if pct >= s["auditor.max_top10_pct"]:
                        raise Red(f"top 10 hold {pct:.0f}%", "rpc")
                    L.update(value=f"{pct:.1f}%", source="rpc")
                elif n == 5:
                    try:
                        rep = await report()
                    except Exception:
                        raise Red("no data: liquidity", "rugcheck")
                    mk = fields.select_market(rep, pool) if pool else None
                    locked = fields.get(mk, "lp.lpLockedPct") if mk else None
                    if locked is None:
                        raise Red("no data: liquidity", "rugcheck")
                    raw_log.append(f"lpLockedPct={locked}")
                    if float(locked) < s["auditor.min_lp_locked_pct"]:
                        raise Red("liquidity unlocked", "rugcheck")
                    base_liq = (scan or {}).get("liquidityUsd") or coin.get("liq_at_scan")
                    if base_liq:
                        from agents.scanner import pick_pair
                        try:
                            p = pick_pair(await self._src(c.net.dex_tokens, [mint]), mint)
                            now_liq = float(fields.get(p, "liquidity.usd") or 0) if p else None
                        except Exception:
                            now_liq = None
                        if now_liq is None:
                            raise Red("no data: liquidity", "dexscreener")
                        raw_log.append(f"liq scan={base_liq} now={now_liq}")
                        if now_liq < base_liq * (1 - s["auditor.max_liq_drop_pct"] / 100.0):
                            raise Red("liquidity pulled", "dexscreener")
                    L.update(value=f"{float(locked):.0f}% locked", source="rugcheck")
                elif n == 6:
                    mig = coin.get("migrated_at")
                    if not mig:
                        raise Red("no data: pool age", "rpc")
                    age = (c.clock() - mig) / 60.0
                    if age < s["auditor.min_pool_age_min"]:
                        raise Red("pool too young", "rpc")
                    L.update(value=f"{int(age)}m" if age < 120 else f"{age/60:.1f}h", source="rpc")
                elif n == 7:
                    try:
                        rep = await report()
                    except Exception:
                        raise Red("no data: bundles", "rugcheck")
                    ok, value, bundle_pct, bundled = bundle_layer(rep, s["auditor.max_bundle_pct"])
                    raw_log.append(f"bundled={bundled} bundle_pct={bundle_pct}")
                    if not ok:
                        raise Red(value, "rugcheck")
                    L.update(value=value, source="rugcheck")
                elif n == 8:
                    acc, err = None, None
                    try:
                        acc = await accounts()
                    except Exception as e:
                        err = e
                    ok, value, skipped = layer8_verdict(
                        acc, s["auditor.max_delegated_accounts"], error=err,
                        bypass=bool(s["auditor.l8_bypass_on_down"]))
                    if skipped:
                        raw_log.append(L8_SKIP_VALUE)
                        state["l8_skipped"] = True
                        L.update(value=value, source="rpc")
                    elif not ok:
                        raise Red(value, "rpc")
                    else:
                        groups = holders.delegate_groups(acc)
                        state["holders"] = holders.holder_count(acc)
                        raw_log.append(f"delegates={groups[:3]} holders={state['holders']}")
                        L.update(value=value, source="rpc")
                L["pass"] = True
            except Red as r:
                L.update(**{"pass": False, "value": r.value, "source": r.source})
                passed = False
            L["ms"] = int((time.time() - t0) * 1000)
            state["layersChecked"] = n
            if on_layer:
                await on_layer(dict(L))
            c.event("audit_layer", mint)
            if not passed:
                state["reason"] = L["value"]
                break
        state.update(passed=passed, running=False, ts=iso())
        out = {k: state[k] for k in ("agent", "mint", "ts", "passed", "layersChecked", "holders", "reason")}
        out["layers"] = [{k: l[k] for k in ("n", "name", "pass", "value", "source")} for l in layers]
        out["bundled"] = bundled
        out["bundle_pct"] = None if bundle_pct is None else round(bundle_pct, 4)
        out["l8_skipped"] = bool(state.get("l8_skipped"))
        self.log.info(f"{out['ts']}\t{mint}\t" + "\t".join(f"L{l['n']}={l['value']}({l['ms']}ms)" for l in layers if l["ms"] is not None)
                      + f"\t{' | '.join(raw_log)}\tverdict={'pass' if passed else 'red'}")
        if record:
            c.db.x("INSERT INTO audits(ts,mint,passed,reason,layer,json,night) VALUES(?,?,?,?,?,?,?)",
                   (c.clock(), mint, 1 if passed else 0, out["reason"], state["layersChecked"], json.dumps(out), c.night))
            if state["holders"] is not None:
                c.db.x("UPDATE coins SET holders=? WHERE mint=?", (state["holders"], mint))
            if passed:
                self.ping_pass(mint, scan, out, reentry=reentry)
            if not passed:
                c.activity("reject", f"Rejected {c.label(mint)}", f"Auditor · {out['reason']}", "Skipped", "", mint, "auditor", "red")
                c.skip(mint, "auditor", out["reason"])
                if out["reason"] == "no data: delegations":
                    k = "layer8_kills_" + c.night
                    c.db.set_meta(k, int(c.db.meta(k, 0)) + 1)
            c.event("audit", mint)
        self.live.pop(mint, None)
        return out

    def ping_pass(self, mint, scan, out, reentry=False):
        """Owner stage ping (DECISION 38). Notification only; the audit result is unchanged."""
        nm = (scan or {}).get("name") or self.c.label(mint)
        if reentry:                                      # DECISION 46: its own dedupe key, labelled
            return stage_pings.ping(self.c, "reentry_auditor", mint, "🔁 RE-ENTRY · " + stage_pings.auditor_text(nm, out), stage_pings.coin_button(mint))
        return stage_pings.ping(self.c, "auditor", mint, stage_pings.auditor_text(nm, out), stage_pings.coin_button(mint))

    async def gpa_down(self, e):
        self.c.error("auditor", "gpa_down", str(e)[:300])
        if not self.gpa_down_alerted:
            self.gpa_down_alerted = True
            # Do not use c.prefix() — that is FOMO desk.mode ("PAPER · "), not Strategy A's mode (DECISION 74).
            # notif_label will headline this as "Desk · …".
            if self.c.s["auditor.l8_bypass_on_down"]:
                text = "layer 8 is down and being skipped"
            else:
                text = "layer 8 down, every coin is red until rpc_url changes"
            await self.c.notifier.push(text, kind="alert")
