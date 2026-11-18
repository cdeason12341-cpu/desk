"""Telegram: long polling, owner only, pushes (PAPER prefix), inline buttons from links.py, callbacks, STEP 9 commands.
The bot token is read from .secrets and never logged (all errors are redacted)."""
from __future__ import annotations
import asyncio, html, json, re, time
import httpx
import config, links, secrets_store, solana
from ctx import iso

HELP = ("Commands:\n"
        "buy X · sell X · why X\n"
        "pause Scanner / resume Scanner (any agent; Risk not while a position is open)\n"
        "set stop to -8% (any setting by name or alias)\n"
        "scanner: set young first 60 · set surge 25% · set surge window 60 · set surge watch 150 · set surge poll 10\n"
        "stack 1240 · status · paper | live · invite · report\n"
        "GMGN hands-off: gmgn · gmgn live (then GMGN LIVE) · gmgn paper · /halt · /unhalt\n"
        "caps · cap trade 60 · cap tickets 6 (then CONFIRM CAP)\n"
        "Strategy B: b status · b live (then B LIVE) · b paper\n"
        "X = name, symbol or mint.")
AGENT_NAMES = {"scanner": "scanner", "auditor": "auditor", "analyst": "analyst", "executor": "executor", "risk": "risk"}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def tlocal(ts: float) -> str:
    return time.strftime("%-I:%M %p %Z", time.localtime(ts))



def paper_nights_done(db) -> int:
    """Full FOMO PAPER nights for the LIVE gate. After the owner restarts the paper test (meta paper_test_since,
    DECISION 58) only nights that started at or after the restart count; earlier nights stay in the history."""
    try:
        since = float(db.meta("paper_test_since") or 0)
    except (TypeError, ValueError):
        since = 0.0
    return db.one("SELECT COUNT(*) n FROM nights WHERE mode='PAPER' AND ended_at IS NOT NULL AND started_at>=?", (since,))["n"]

class Telegram:
    def __init__(self, desk):
        self.d = desk
        self.c = desk.ctx
        self.token = secrets_store.get("TG_TOKEN")
        self.owner = int(secrets_store.get("OWNER_CHAT_ID") or 0)
        self.http = httpx.AsyncClient(timeout=70)
        self.log = self.c.logs["telegram"]
        self.offset = int(self.c.db.meta("tg_offset", 0) or 0)
        self.awaiting_live = False
        self.from_owner = False          # True only while handling an owner's Telegram message
        self.pending_cap = None          # (key, value, expires_at)
        self.gmgn_live_asked = 0.0
        self.b_live_asked = 0.0          # 'b live' armed at (then type B LIVE within 5 min)

    async def api(self, method, **payload):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            r = await self.http.post(url, json=payload, timeout=payload.get("timeout", 20) + 15)
            d = r.json()
        except Exception as e:
            raise RuntimeError(secrets_store.redact(f"telegram {method}: {type(e).__name__} {e}"))
        if not d.get("ok"):
            raise RuntimeError(secrets_store.redact(f"telegram {method}: {d.get('description')}"))
        return d["result"]

    # ---------- notifier interface
    async def push(self, text, buttons=None, kind="info", mint=None, mirror=True):
        wp = getattr(self.c, "webpush", None)
        if wp is not None and mirror:
            wp.mirror(kind, text, mint)              # phone notification first (background task); Telegram is the backup
        ok = True
        try:
            await self.send(text, buttons, mentions=self.mentions([mint] if mint else []))
        except Exception as e:
            ok = False
            self.c.error("telegram", "push", str(e))
        self.c.db.x("INSERT INTO pushes(ts,mint,kind,text,ok,night) VALUES(?,?,?,?,?,?)",
                    (time.time(), mint, kind, text, 1 if ok else 0, self.c.night))
        self.log.info(f"{iso()}\tpush\t{kind}\t{'ok' if ok else 'FAIL'}\t{text[:160]}")
        return ok

    def mentions(self, mints):
        out = []
        for mt in mints or []:
            c = self.c.coin(mt)
            if c and (c.get("name") or c.get("symbol")):
                out.append((c.get("name") or c.get("symbol"), mt))
        return out

    async def send(self, text, buttons=None, chat_id=None, mentions=None):
        body = {"chat_id": chat_id or self.owner, "text": self.linkify(text, mentions), "parse_mode": "HTML",
                "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": buttons}
        return await self.api("sendMessage", **body)

    @staticmethod
    def linkify(text: str, mentioned=None) -> str:
        """HTML-escape, then link every known coin name we mention to its coin_url."""
        t = esc(text)
        if mentioned:
            for name, mint in mentioned:
                n = esc(name)
                if n and n in t:
                    t = t.replace(n, f'<a href="{esc(links.coin_url(mint))}">{n}</a>', 1)
        return t

    # ---------- polling
    async def bot_username(self):
        """@username from getMe, cached in meta for the app's override deep links. Never logs the token."""
        try:
            me = await self.api("getMe")
            if me.get("username"):
                self.c.db.set_meta("tg_username", me["username"])
        except Exception as e:
            self.c.error("telegram", "getMe", str(e))

    async def loop(self):
        await self.bot_username()
        while True:
            try:
                ups = await self.api("getUpdates", offset=self.offset, timeout=50,
                                     allowed_updates=["message", "callback_query"])
                for u in ups:
                    self.offset = u["update_id"] + 1
                    self.c.db.set_meta("tg_offset", self.offset)
                    try:
                        await self.handle(u)
                    except Exception as e:
                        self.c.error("telegram", "handle", str(e))
            except Exception as e:
                self.c.error("telegram", "poll", str(e))
                await asyncio.sleep(5)

    async def handle(self, u):
        if "callback_query" in u:
            q = u["callback_query"]
            if (q.get("from") or {}).get("id") != self.owner:
                return
            data = q.get("data") or ""
            kind, _, mint = data.partition(":")
            if kind in ("ovrc", "ovrx"):                 # owner override (real $) Confirm / Cancel, DECISION 36
                ov = self.d.overrides
                msg = await (ov.confirm_real(mint) if kind == "ovrc" else ov.cancel_real(mint))
            elif kind in ("gmap", "gmsk"):               # GMGN market-cap approval Approve / Skip, DECISION 53
                g = self.d.gmgn
                msg = await (g.approve(mint) if kind == "gmap" else g.skip_approval(mint))
                mint = (self.c.db.one("SELECT mint FROM gmgn_approvals WHERE id=?", (int(mint) if mint.isdigit() else -1,)) or {}).get("mint") or ""
            elif kind == "bought":
                msg = await self.d.executor.on_bought(mint)
            elif kind == "skip":
                msg = await self.d.executor.on_skip(mint)
            elif kind == "sold":
                msg = await self.d.risk.on_sold(mint)
            else:
                msg = "?"
            try:
                await self.api("answerCallbackQuery", callback_query_id=q["id"], text=msg[:190])
            except Exception:
                pass
            await self.reply(msg, mints=[mint])
            return
        m = u.get("message") or {}
        if (m.get("chat") or {}).get("id") != self.owner:
            self.log.info(f"{iso()}\tignored message from non-owner")
            return
        text = (m.get("text") or "").strip()
        self.log.info(f"{iso()}\tcmd\t{text[:80]}")
        self.from_owner = True
        try:
            out = await self.command(text)
        finally:
            self.from_owner = False
        if out:
            msg, buttons, mints = out
            await self.reply(msg, buttons, mints)

    async def reply(self, msg, buttons=None, mints=None):
        try:
            await self.send(msg, buttons, mentions=self.mentions(mints))
        except Exception as e:
            self.c.error("telegram", "reply", str(e))

    # ---------- commands (also used by the Grok Bot app path through Desk.command)
    def resolve(self, x: str):
        """-> ('one', mint) | ('many', [rows]) | ('unknown', x) | ('mint', x) for a valid unknown mint."""
        x = x.strip()
        if solana.is_pubkey(x):
            return ("one", x) if self.c.coin(x) else ("mint", x)
        rows = self.c.db.rows("SELECT mint,name,symbol FROM coins WHERE lower(name)=lower(?) OR lower(symbol)=lower(?) OR lower(symbol)=lower(?) ORDER BY first_seen DESC",
                              (x, x, x.lstrip("$")))
        if len(rows) == 1:
            return ("one", rows[0]["mint"])
        if len(rows) > 1:
            return ("many", rows)
        return ("unknown", x)

    def which(self, rows):
        lines = ["That matches more than one coin. Which one?"]
        for r in rows[:10]:
            lines.append(f"{r['name'] or r['symbol']} ({r['symbol'] or '?'}) · {links.coin_url(r['mint'])}")
        return "\n".join(lines), None, []

    async def command(self, text: str):
        c, s = self.c, self.c.s
        t = text.strip()
        low = t.lower().lstrip("/")
        m = re.match(r"^/?start\s+(ov[pr]_\S+)$", t)
        if m:
            return await self.cmd_override(m.group(1))
        m = re.match(r"^/?start\s+gm(ap|sk)_(\d+)$", t)        # phone notification Approve / Skip buttons (DECISION 62)
        if m:
            if not self.from_owner:
                return "Refused: approvals only work from the owner's Telegram chat.", None, []
            if getattr(self.d, "gmgn", None) is None:
                return "GMGN isn't running.", None, []
            g = self.d.gmgn
            return await (g.approve(m.group(2)) if m.group(1) == "ap" else g.skip_approval(m.group(2))), None, []
        if low in ("start", "help", "?", ""):
            return HELP, None, []
        m = re.match(r"^(approve|skip)\s+#?(\d+)$", low)  # text fallback for the GMGN MC approval buttons
        if m and getattr(self.d, "gmgn", None) is not None:
            g = self.d.gmgn
            msg = await (g.approve(m.group(2)) if m.group(1) == "approve" else g.skip_approval(m.group(2)))
            return msg, None, []
        b = await self.cmd_strat_b(t, low)
        if b is not None:
            return b
        g = await self.cmd_gmgn(t, low)
        if g is not None:
            return g
        m = re.match(r"^(buy|sell|why)\s+(.+)$", low)
        if m:
            verb, x = m.group(1), t.lstrip("/").split(None, 1)[1]
            kind, val = self.resolve(x)
            if kind == "many":
                return self.which(val)
            if kind == "unknown":
                return f"I don't know '{x}'. Send me its mint address.", None, []
            mint = val
            return await getattr(self, f"cmd_{verb}")(mint, known=(kind == "one"))
        m = re.match(r"^(pause|resume)\s+(\w+)$", low)
        if m:
            return self.cmd_pause(m.group(1), m.group(2))
        m = re.match(r"^set\s+(.+?)\s+(?:to\s+|=\s*)?(-?[\w$.,:%/+-]+)$", low)
        if m:
            try:
                msg = s.set(m.group(1), m.group(2))
            except config.SettingError as e:
                return f"Refused: {e}", None, []
            extra = self.d.after_setting(config.resolve(m.group(1)).key)
            return msg + (f"\n{extra}" if extra else ""), None, []
        m = re.match(r"^stack\s+\$?([\d,.]+)$", low)
        if m:
            try:
                msg = s.set("desk.live_stack", m.group(1))
            except config.SettingError as e:
                return f"Refused: {e}", None, []
            c.db.set_meta("live_stack_ts", time.time())
            extra = self.d.after_setting("desk.live_stack")
            return f"LIVE {msg}" + (f"\n{extra}" if extra else ""), None, []
        if low == "status":
            return self.cmd_status()
        if low == "paper":
            if s["desk.mode"] == "PAPER":
                return "Already PAPER.", None, []
            s.set("desk.mode", "PAPER", force=True)
            return "Mode LIVE -> PAPER.\n" + (self.d.ticket_msg() or ""), None, []
        if t == "LIVE":
            return self.cmd_live(confirm=True)
        if low == "live":
            return self.cmd_live(confirm=False)
        if low == "invite":
            return f"New to FOMO? {links.SIGNUP_URL}", None, []
        if low == "report":
            u = c.db.meta("last_report_url")
            return (f"Last morning Page: {u}" if u else "No morning Page yet. The first one comes after tonight."), None, []
        return HELP, None, []

    async def cmd_override(self, param):
        """Deep link from the app (t.me/<bot>?start=ovp_<mint> / ovr_<mint>). Owner chat only."""
        if not self.from_owner:
            self.log.info(f"{iso()}\toverride refused: not the owner chat")
            return "Refused: overrides only work from the owner's Telegram chat.", None, []
        mint = param.split("_", 1)[1] if "_" in param else None
        msg, buttons = await self.d.overrides.start(param)
        if msg is None:
            return None
        return msg, buttons, [mint] if mint else []

    async def cmd_buy(self, mint, known=True):
        c = self.c
        ex = c.db.one("SELECT * FROM executions WHERE mint=? AND night=? AND status IN ('sent','bought') ORDER BY id DESC LIMIT 1", (mint, c.night))
        if ex:
            j = json.loads(ex["json"])
            an = c.db.one("SELECT score FROM analyses WHERE mint=? AND night=?", (mint, c.night)) or {}
            from agents.executor import buy_text, buy_buttons
            txt = buy_text(c.prefix(), c.label(mint), j["sizeUsd"], j["stackPct"], an.get("score"),
                           c.s["risk.stop_pct"], c.s["risk.trail_pct"])
            return txt, buy_buttons(mint, j["mode"]), [mint]
        res = await self.d.auditor.run(mint, None, record=False)
        lines = [f"Auditor on {c.label(mint)}: {'8/8 clean' if res['passed'] else 'RED · ' + str(res['reason'])}"]
        for l in res["layers"]:
            mark = "✓" if l["pass"] else ("✗" if l["pass"] is False else "·")
            lines.append(f"{mark} {l['n']} {l['name']}: {l['value']}")
        lines.append("not sized, not on the desk")
        return "\n".join(lines), [[{"text": "Buy on FOMO", "url": links.coin_url(mint)}]], [mint]

    async def cmd_sell(self, mint, known=True):
        p = self.c.db.one("SELECT * FROM positions WHERE mint=? AND status IN ('open','exiting') ORDER BY id DESC LIMIT 1", (mint,))
        if not p:
            return f"No open position in {self.c.label(mint)}.", [[{"text": "Open on FOMO", "url": links.coin_url(mint)}]], [mint]
        from agents.risk import sell_buttons
        b = sell_buttons(mint)
        return f"{self.c.prefix() if p['mode']=='PAPER' else ''}Sell {p['name']} · ${p['size_usd']:,.2f} open", b, [mint]

    async def cmd_why(self, mint, known=True):
        c = self.c
        name = c.label(mint)
        if not known:
            return f"{name} never reached the desk tonight.", [[{"text": "Open on FOMO", "url": links.coin_url(mint)}]], [mint]
        ev = []
        for r in c.db.rows("SELECT ts, verdict, reason FROM scans WHERE mint=?", (mint,)):
            if r["verdict"] in ("kill", "recheck"):
                ev.append((r["ts"], f"Scanner {'stopped' if r['verdict'] == 'kill' else 're-checked'} {name} · {r['reason']}"))
        for r in c.db.rows("SELECT ts, passed, reason, layer FROM audits WHERE mint=?", (mint,)):
            if not r["passed"]:
                ev.append((r["ts"], f"Auditor stopped {name} · layer {r['layer']} · {r['reason']}"))
        for r in c.db.rows("SELECT ts, passed, failed, model, score FROM analyses WHERE mint=?", (mint,)):
            if not r["passed"]:
                f = json.loads(r["failed"] or "[]")
                who = "Analyst (rules)" if r["model"] == "rules" else "Analyst"
                ev.append((r["ts"], f"{who} stopped {name} · {f[0] if f else '?'} (score {r['score']})"))
        for r in c.db.rows("SELECT ts, status, reason FROM executions WHERE mint=?", (mint,)):
            if r["status"] in ("skipped", "missed"):
                ev.append((r["ts"], f"Executor {r['status']} {name} · {r['reason']}"))
            else:
                ev.append((r["ts"], f"Executor {r['status']} {name}"))
        coin = c.coin(mint) or {}
        if not ev:
            st = coin.get("status")
            msg = f"{name} is still in Scanner's waiting room." if st == "room" else f"{name}: {st or 'unknown'} {coin.get('reason') or ''}"
            return msg, None, [mint]
        ts, txt = sorted(ev)[-1]
        return f"{txt} · {tlocal(ts)}", None, [mint]

    def cmd_pause(self, verb, agent):
        a = AGENT_NAMES.get(agent.lower())
        if not a:
            return "Agents: Scanner, Auditor, Analyst, Executor, Risk.", None, []
        if verb == "pause":
            if a == "risk" and self.d.risk.open_positions():
                return "Risk can't be paused while a position is open.", None, []
            self.c.set_paused(a, True)
            return f"{a.title()} paused.", None, []
        self.c.set_paused(a, False)
        return f"{a.title()} resumed.", None, []

    def cmd_status(self):
        c = self.c
        lines = [f"Mode {c.mode()} · balance ${c.balance():,.2f} · free ${c.free():,.2f}"]
        if getattr(self.d, "gmgn", None) is not None:
            lines.append(self.d.gmgn.status_line())
        if getattr(self.d, "strat_b", None) is not None:
            try:
                lines.append(self.b_status())
            except Exception:
                pass
        if c.paused:
            lines.append("Paused: " + ", ".join(sorted(c.paused)))
        pos = self.d.risk.open_positions()
        buttons, mints = [], []
        if not pos:
            lines.append("No open positions.")
        for p in pos:
            ch = (p["last_price"] / p["entry_price"] - 1) * 100 if p["last_price"] else 0
            lines.append(f"{p['name']} · ${p['size_usd']:,.2f} · {ch:+.1f}% · stop -{p['stop_pct']:g}% · {p['status']}")
            buttons.append([{"text": f"Sell {p['name'][:20]} on FOMO", "url": links.coin_url(p["mint"])}])
            mints.append(p["mint"])
        return "\n".join(lines), buttons or None, mints

    def cmd_live(self, confirm):
        c, s = self.c, self.c.s
        if s["desk.mode"] == "LIVE":
            return "Already LIVE.", None, []
        full = paper_nights_done(c.db)
        if full < 1:
            return f"LIVE needs at least 1 full PAPER night first ({full} so far).", None, []
        if not confirm:
            return "Type LIVE (capitals) to switch. Money only moves when you tap Buy on FOMO and buy inside FOMO.", None, []
        if s["desk.live_stack"] <= 0:
            return "Set your LIVE stack first, e.g. 'stack 1240'.", None, []
        s.set("desk.mode", "LIVE", force=True)
        return "Mode PAPER -> LIVE.\n" + (self.d.ticket_msg() or ""), None, []

    # ---------- Strategy B PAPER / LIVE (owner Telegram messages only, DECISION 66; mirrors gmgn paper / gmgn live)
    def b_status(self):
        s = self.c.s
        sb = getattr(self.d, "strat_b", None)
        line = f"Strategy B {s['strat_b.mode']}{'' if s['strat_b.enabled'] else ' · off'} · Strategy A (GMGN) {s['gmgn.mode']}"
        if sb is not None:
            import strat_b
            lc = strat_b.live_card(self.c)
            line += (f" · B LIVE {lc['closedAll']} closed, P&L ${lc['pnlAllUsd']:+,.2f} (tonight ${lc['pnlTonightUsd']:+,.2f}), "
                     f"{len(lc['open'])} open · B PAPER bankroll ${sb.bankroll():,.2f}")
            if lc["freeSol"] is not None:
                line += f" · free {lc['freeSol']:.4f} SOL after the {s['gmgn.sol_reserve']:g} SOL reserve"
            bb = lc.get("budget") or {}
            if bb.get("budgetSol") is not None:                     # DECISION 71: B's share of the wallet
                usd = f" (${bb['budgetUsd']:,.2f})" if bb.get("budgetUsd") is not None else ""
                line += (f" · B's {bb['sharePct']:g}% share: {bb['budgetSol']:.4f} SOL{usd} free to buy, "
                         f"{bb['openCount']}/{bb['maxOpen']} open")
        return line

    async def cmd_strat_b(self, t, low):
        if t == "B LIVE" or low in ("b live", "b paper", "b status", "b mode", "strat b status"):
            if not self.from_owner:
                return "That command only works from the owner's Telegram chat.", None, []
        else:
            return None
        s = self.c.s
        if low in ("b status", "b mode", "strat b status"):
            return self.b_status(), None, []
        if low == "b paper":
            if s["strat_b.mode"] == "PAPER":
                return "Strategy B is already PAPER.", None, []
            s.set("strat_b.mode", "PAPER", source="telegram owner b paper", force=True)
            return ("Strategy B LIVE -> PAPER. New B buys are simulated. Open B LIVE positions keep their GMGN stop and "
                    "the desk keeps running their exits until they close."), None, []
        if t == "B LIVE":
            if time.time() - self.b_live_asked > 300:
                return "Send 'b live' first, then type B LIVE within 5 minutes.", None, []
            if getattr(self.d, "gmgn", None) is None or getattr(self.c, "gmgn", None) is None:
                return "GMGN isn't connected, so Strategy B can't trade LIVE.", None, []
            self.b_live_asked = 0.0
            s.set("strat_b.mode", "LIVE", source="telegram owner B LIVE", force=True)
            return (f"Strategy B PAPER -> LIVE. Real GMGN buys from your main wallet: ${s['strat_b.trade_usd']:,.2f} when free SOL "
                    f"covers it, else Strategy A's sizing (down to ${s['gmgn.min_trade_usd']:,.2f}), stop -{s['strat_b.stop_pct']:g}% "
                    f"on GMGN. /halt stops buys instantly."), None, []
        if low == "b live":
            if s["strat_b.mode"] == "LIVE":
                return "Strategy B is already LIVE.", None, []
            self.b_live_asked = time.time()
            return (f"Ready. Strategy B LIVE spends real SOL from your main wallet (${s['strat_b.trade_usd']:,.2f} per trade, "
                    f"A's caps apply). Type B LIVE (capitals) to switch."), None, []
        return None

    # ---------- GMGN hands-off mode (owner Telegram messages only)
    async def cmd_gmgn(self, t, low):
        g = getattr(self.d, "gmgn", None)
        if g is None:
            return None
        s = self.c.s
        if low in ("halt", "unhalt", "gmgn", "gmgn status", "caps", "gmgn live", "gmgn paper", "confirm cap") or t == "GMGN LIVE" \
                or re.match(r"^cap\s+(trade|tickets)\s+", low):
            if not self.from_owner:
                return "That command only works from the owner's Telegram chat.", None, []
        else:
            return None
        if low == "halt":
            g.halt(True, "telegram owner /halt")
            return "GMGN HALTED. No new GMGN buys. Server-side exits on open positions stay on. /unhalt to resume.", None, []
        if low == "unhalt":
            g.halt(False, "telegram owner /unhalt")
            return f"GMGN resumed ({g.mode()}).", None, []
        if low in ("gmgn", "gmgn status"):
            return g.status_line(), None, []
        if low == "caps":
            cap, n = s["gmgn.cap_trade_usd"], s["gmgn.max_tickets_night"]
            return f"GMGN caps: ${cap:,.2f} per trade · {n} tickets per night · ${cap * n:,.2f} per night", None, []
        m = re.match(r"^cap\s+(trade|tickets)\s+\$?([\d,.]+)$", low)
        if m:
            key = "gmgn.cap_trade_usd" if m.group(1) == "trade" else "gmgn.max_tickets_night"
            try:
                val = config.parse_value(config.BY_KEY[key], m.group(2))
            except config.SettingError as e:
                return f"Refused: {e}", None, []
            self.pending_cap = (key, val, time.time() + 120)
            cap = val if key == "gmgn.cap_trade_usd" else s["gmgn.cap_trade_usd"]
            n = val if key == "gmgn.max_tickets_night" else s["gmgn.max_tickets_night"]
            old = config.display(config.BY_KEY[key], s[key]); new = config.display(config.BY_KEY[key], val)
            return (f"{'Per-trade cap' if key.endswith('usd') else 'Tickets per night'} {old} -> {new} "
                    f"(night cap ${cap * n:,.2f}). Type CONFIRM CAP within 2 minutes to apply."), None, []
        if low == "confirm cap":
            pc, self.pending_cap = self.pending_cap, None
            if t != "CONFIRM CAP":
                self.pending_cap = pc
                return "Type CONFIRM CAP in capitals.", None, []
            if not pc or time.time() > pc[2]:
                return "Nothing to confirm (or it expired). Send the cap command again.", None, []
            msg = s.set(pc[0], pc[1], source="telegram owner CONFIRM CAP", force=True)
            return f"GMGN {msg}", None, []
        if low == "gmgn paper":
            if s["gmgn.mode"] == "PAPER":
                return "GMGN is already PAPER.", None, []
            s.set("gmgn.mode", "PAPER", source="telegram owner", force=True)
            self.c.db.set_meta("gmgn_paper_since", self.c.clock())
            return "GMGN LIVE -> PAPER. Open positions keep their server-side exits.", None, []
        if t == "GMGN LIVE":
            if time.time() - self.gmgn_live_asked > 300:
                return "Send 'gmgn live' first, then type GMGN LIVE within 5 minutes.", None, []
            why = await g.live_check()
            if why:
                return why, None, []
            self.gmgn_live_asked = 0.0
            s.set("gmgn.mode", "LIVE", source="telegram owner GMGN LIVE", force=True)
            cap, n = s["gmgn.cap_trade_usd"], s["gmgn.max_tickets_night"]
            return (f"GMGN PAPER -> LIVE. Real buys from your main wallet, ${cap:,.2f} per trade, max {n} per night "
                    f"(${cap * n:,.2f}). /halt stops buys instantly."), None, []
        if low == "gmgn live":
            why = await g.live_check()
            if why:
                return why, None, []
            self.gmgn_live_asked = time.time()
            cap, n = s["gmgn.cap_trade_usd"], s["gmgn.max_tickets_night"]
            return (f"Ready. GMGN LIVE spends real SOL from your main wallet: ${cap:,.2f} per trade, max {n} per night. "
                    f"Type GMGN LIVE (capitals) to switch."), None, []
        return None
