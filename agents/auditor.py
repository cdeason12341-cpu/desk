"""AGENT 2 · AUDITOR. 8 layers in order; one red layer and the coin is dead. No data is never a pass."""
from __future__ import annotations
import asyncio, json, time
import fields, holders
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


def short(addr: str) -> str:
    return f"{addr[:4]}...{addr[-3:]}" if addr and len(addr) > 8 else (addr or "?")


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

    async def run(self, mint: str, scan: dict | None, record: bool = True, on_layer=None) -> dict | None:
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
        bundled = None
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
                            raise Red("dev sold", "rpc")
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
                        g = await self._src(c.net.rug_graph, mint)
                    except Exception:
                        raise Red("no data: bundles", "rugcheck")
                    bundled = biggest_group(g)
                    if bundled is None:
                        raise Red("no data: bundles", "rugcheck")
                    raw_log.append(f"bundled={bundled}")
                    if bundled >= s["auditor.max_bundled"]:
                        raise Red(f"{bundled} bundled wallets", "rugcheck")
                    L.update(value="None" if bundled == 0 else f"{bundled} linked", source="rugcheck")
                elif n == 8:
                    try:
                        acc = await accounts()
                    except Exception:
                        raise Red("no data: delegations", "rpc")
                    groups = holders.delegate_groups(acc)
                    state["holders"] = holders.holder_count(acc)
                    raw_log.append(f"delegates={groups[:3]} holders={state['holders']}")
                    if groups and groups[0][1] >= s["auditor.max_delegated_accounts"]:
                        raise Red(f"{groups[0][1]} wallets → {short(groups[0][0])}", "rpc")
                    L.update(value="None" if not groups else f"max {groups[0][1]} → {short(groups[0][0])}", source="rpc")
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
        self.log.info(f"{out['ts']}\t{mint}\t" + "\t".join(f"L{l['n']}={l['value']}({l['ms']}ms)" for l in layers if l["ms"] is not None)
                      + f"\t{' | '.join(raw_log)}\tverdict={'pass' if passed else 'red'}")
        if record:
            c.db.x("INSERT INTO audits(ts,mint,passed,reason,layer,json,night) VALUES(?,?,?,?,?,?,?)",
                   (c.clock(), mint, 1 if passed else 0, out["reason"], state["layersChecked"], json.dumps(out), c.night))
            if state["holders"] is not None:
                c.db.x("UPDATE coins SET holders=? WHERE mint=?", (state["holders"], mint))
            if not passed:
                c.activity("reject", f"Rejected {c.label(mint)}", f"Auditor · {out['reason']}", "Skipped", "", mint, "auditor", "red")
                c.skip(mint, "auditor", out["reason"])
                if out["reason"] == "no data: delegations":
                    k = "layer8_kills_" + c.night
                    c.db.set_meta(k, int(c.db.meta(k, 0)) + 1)
            c.event("audit", mint)
        self.live.pop(mint, None)
        return out

    async def gpa_down(self, e):
        self.c.error("auditor", "gpa_down", str(e)[:300])
        if not self.gpa_down_alerted:
            self.gpa_down_alerted = True
            await self.c.notifier.push(f"{self.c.prefix()}layer 8 down, every coin is red until rpc_url changes", kind="alert")
