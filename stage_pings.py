"""Owner Telegram pings when a coin passes each stage (owner request 2026-11-15 4:48 PM CT, DECISION 38).
One message per (night, stage, mint), deduped in the `stage_pings` table so restarts and re-checks never repeat it.
Pushes go through ctx.notifier (Telegram -> owner chat only) as background tasks: trading never waits on Telegram,
except the Analyst pass ping, which waits up to 3s so it lands before the buy push. Off with `set stage pings off`."""
from __future__ import annotations
import asyncio
import links

STAGES = ("scanner", "auditor", "analyst")
WAIT_S = 3.0


def _usd(v):
    if v is None:
        return "n/a"
    v = float(v)
    return f"${v / 1e6:,.2f}M" if v >= 1e6 else f"${v / 1e3:,.1f}K" if v >= 1e3 else f"${v:,.0f}"


def _tasks(ctx) -> set:
    if not hasattr(ctx, "_ping_tasks"):
        ctx._ping_tasks = set()
    return ctx._ping_tasks


def ping(ctx, stage, mint, text, buttons=None):
    """Schedule one owner push for this stage+mint tonight. Returns the task, or None (off / duplicate / no loop)."""
    tg_on = bool(ctx.s["alerts.stage_pings"])
    wp = getattr(ctx, "webpush", None)
    if not mint or ctx.notifier is None or not (tg_on or wp is not None):
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if stage != "scanner" and " · via " not in text:
        src = source_tag(ctx, mint)
        if src:
            first, _, rest = text.partition("\n")
            text = f"{first} · via {src}" + (f"\n{rest}" if rest else "")
    if stage != "scanner":
        tag = surge_tag(ctx, mint)
        if tag and "⚡" not in text:
            text = f"{tag} · {text}"
    cur = ctx.db.x("INSERT OR IGNORE INTO stage_pings(night,stage,mint,ts) VALUES(?,?,?,?)", (ctx.night, stage, mint, ctx.clock()))
    if not cur.rowcount:
        return None                                     # already pinged this coin at this stage tonight
    if not tg_on:                                       # Telegram stage pings off: the phone app still gets its pipeline note
        return wp.mirror(f"stage_{stage}", text, mint)
    t = loop.create_task(_send(ctx, stage, mint, text, buttons))
    ts = _tasks(ctx); ts.add(t); t.add_done_callback(ts.discard)
    return t


async def _send(ctx, stage, mint, text, buttons):
    try:
        await ctx.notifier.push(text, buttons, kind=f"stage_{stage}", mint=mint)
    except Exception as e:
        ctx.error("telegram", "stage_ping", f"{stage} {mint} {str(e)[:200]}")


def coin_button(mint):
    return [[{"text": "Open on FOMO", "url": links.coin_url(mint, "solana")}]]


def bot_username(ctx):
    return ctx.db.meta("tg_username") or "Denarpobot"


SURGE_TAG_MAX_AGE_S = 30 * 60


def window_label(secs) -> str:
    secs = int(secs or 60)
    return f"{secs // 60}m" if secs % 60 == 0 else f"{secs}s"


def surge_label(surge) -> str:
    """'⚡ Surge +32% in 1m' from a scanner surge dict {pct, windowS}."""
    if not surge:
        return ""
    return f"⚡ Surge +{float(surge.get('pct') or 0):.0f}% in {window_label(surge.get('windowS'))}"


def surge_tag(ctx, mint):
    """Surge label for a coin flagged tonight in the last 30 min, else ''. Auditor/Analyst pings carry it."""
    try:
        r = ctx.db.one("SELECT pct, window_s FROM surges WHERE mint=? AND night=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                       (mint, ctx.night, ctx.clock() - SURGE_TAG_MAX_AGE_S))
    except Exception:
        return ""
    return surge_label({"pct": r["pct"], "windowS": r["window_s"]}) if r else ""


def source_tag(ctx, mint) -> str:
    """Launchpad / source label of a coin the desk saw (DECISION 51), '' if unknown."""
    try:
        import sources
        row = ctx.coin(mint)
        return sources.launchpad(mint, row.get("pool_label")) if row else ""
    except Exception:
        return ""


# ---- message builders (pure, tested)
def scanner_text(out):
    name = out.get("name") or out.get("symbol") or (out["mint"][:4] + "…" + out["mint"][-4:])
    t = f"✅ Scanner pass · {name} · liq {_usd(out.get('liquidityUsd'))} · MC {_usd(out.get('marketCapUsd'))}"
    if out.get("source"):
        t += f" · via {out['source']}"
    if out.get("recheck"):
        t += f" · recheck {out['recheck']}"
    if out.get("surge"):
        t = f"{surge_label(out['surge'])} · {t}"
    return t


def auditor_text(name, audit):
    top10 = next((l.get("value") for l in audit.get("layers", []) if l.get("n") == 4), None)
    top = str(top10 or "n/a").split(" ")[0]
    if top != "n/a" and not top.endswith("%"):
        top += "%"
    return f"🛡️ Audit pass · {name} · top10 {top}"


def analyst_pass_text(name, score):
    return f"🎯 Analyst pass · score {score} · {name}\nBuy flow next."


def analyst_reject(ctx, mint, name, score, failed, bypass_will_buy):
    s = ctx.s
    why = ", ".join(str(f) for f in (failed or [])[:3]) or "rejected"
    t = f"Analyst · score {score} (min {s['analyst.min_score']}) · {name}\nNo buy: {why}."
    bot = bot_username(ctx)
    real = {"text": "Override · Real $", "url": f"https://t.me/{bot}?start=ovr_{mint}"}
    paper = {"text": "Override · Paper", "url": f"https://t.me/{bot}?start=ovp_{mint}"}
    if bypass_will_buy:
        t += "\nPaper bypass is on, so it goes on paper anyway. Real-money override: button below or in the app (Live tab), 30 min."
        rows = [[real]]
    else:
        t += "\nOverride buttons are below and in the app (Live tab), for 30 min."
        rows = [[paper, real]]
    rows.append(coin_button(mint)[0])
    return t, rows
