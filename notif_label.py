"""DECISION 73 (owner 2026-11-17 7:03 / 7:04 PM CT): every Telegram message and phone push says which strategy is acting,
with the action in the headline: "Strat A bought Sloth", "Strat B sold JALU -$9.85 (-19%) (stop)",
"Strat A · Sloth passed the scanner", "Desk · …" for system messages. Text only: nothing here changes trading.

headline(kind, text) -> (who, title) where who is "Strat A" | "Strat B" | "Desk".
telegram(kind, text) -> the Telegram text: headline on line 1, the original message (tags stripped) below it.
"""
from __future__ import annotations
import re

MONEY = r"([+-])\$([\d,]+(?:\.\d+)?) \(([+-]?[\d.]+)%\)"
LABELS = ("Strat A", "Strat B", "Desk")


def who(kind: str, text: str) -> str:
    kind = kind or "info"
    first = (text or "").split("\n", 1)[0]
    if kind == "strat_b" or first.startswith(("B LIVE · ", "STRATEGY B · ", "B PAPER · ")):
        return "Strat B"
    if kind.startswith(("gmgn", "stage_")) or first.startswith(("GMGN · ", "PAPER · GMGN · ")):
        return "Strat A"
    return "Desk"


_TAGS = ("STRATEGY B · PAPER · ", "B LIVE · ", "B PAPER · ", "PAPER · GMGN · ", "GMGN · ", "PAPER · ", "RE-ENTRY · ",
         "🔁 RE-ENTRY · ", "OVERRIDE · ", "BYPASS · ")


def strip_tags(first: str) -> tuple[str, list[str]]:
    """Remove the old routing tags from a first line; returns (core, flags) with flags like 'paper', 're-entry'."""
    flags, t, changed = [], first.strip(), True
    while changed:
        changed = False
        for tg in _TAGS:
            if t.startswith(tg):
                t = t[len(tg):]
                changed = True
                if "PAPER" in tg:
                    flags.append("paper")
                if "RE-ENTRY" in tg:
                    flags.append("re-entry")
    return t.strip(), flags


def _money(sign, amt, pct):
    p = float(pct)
    ps = f"{p:+.0f}%" if abs(p) >= 10 else f"{p:+.1f}%"
    return f"{'−' if sign == '-' else '+'}${amt} ({ps})".replace("(-", "(−")


def _short(why):
    w = (why or "").strip()
    return {"trailing take-profit (desk)": "trail", "trailing take-profit": "trail", "server-side exit": "GMGN exit",
            "hard stop": "stop", "server-side stop": "stop", "stop (GMGN server-side)": "stop"}.get(w, w)


def _reason(r: str) -> str:
    return re.split(r" · | \(|, ", r or "")[0].strip()


def action(core: str) -> str | None:
    """Plain action phrase for a stripped first line, or None when no pattern fits."""
    t = core
    surge = ""
    m = re.match(r"^⚡ Surge \+?([\d.]+)% in (\w+) · (.*)$", t)
    if m:
        t, surge = m.group(3), f" ⚡+{m.group(1)}%"
    pats = [
        (r"^✅ Scanner pass · (.+?) · ", lambda m: f"· {m.group(1)} passed the scanner{surge}"),
        (r"^🛡️ Audit pass · (.+?)(?: · |$)", lambda m: f"· {m.group(1)} passed the audit{surge}"),
        (r"^🎯 Analyst pass · score (\d+) · (.+?)(?: · via .*)?$", lambda m: f"· {m.group(2)} passed the analyst (score {m.group(1)})"),
        (r"^Analyst · score (\d+) \(min (\d+)\) · (.+?)(?: · via .*)?$", lambda m: f"· {m.group(3)} scored {m.group(1)}, no buy"),
        (r"^⏳ Dip watch · (.+)$", lambda m: f"· waiting for a dip on {m.group(1)}"),
        (r"^🎯 Dip buy · (.+)$", lambda m: f"· dip reached on {m.group(1)}, buying"),
        (r"^🎯 Dip timeout · (.+)$", lambda m: f"· no dip on {m.group(1)}, buying at market"),
        (r"^Dip skipped · (.+)$", lambda m: f"skipped {m.group(1)} (dip)"),
        (r"^(?:🔁 )?RE-ENTRY watch · (.+?) bounced", lambda m: f"· {m.group(1)} bounced, re-entry check"),
        (r"^APPROVE\? (.+?)(?: \(([^)]*)\))?( · re-entry)?$", lambda m: f"· approve {m.group(1)}?"),
        (r"^(.+?): approval expired", lambda m: f"· {m.group(1)} approval expired, nothing bought"),
        (r"^(.+?): your manual buy is now managed", lambda m: f"· your manual buy {m.group(1)} is now protected"),
        (r"^ALERT (.+?): (.+)$", lambda m: f"⚠️ alert · {m.group(1)}"),
        (r"^(?:Buy|Buying) (.+?) (failed|unknown|error)\b", lambda m: f"⚠️ buy {m.group(2)} · {m.group(1)}"),
        (r"^Buying (.+?) · \$([\d,.]+)", lambda m: f"buying {m.group(1)} ${m.group(2)}"),
        (r"^(?:Bought|Buy) (.+?) · \$([\d,.]+)", lambda m: f"bought {m.group(1)} ${m.group(2)}"),
        (rf"^Sold (.+?) \((.+)\) · {MONEY}", lambda m: f"sold {m.group(1)} {_money(m.group(3), m.group(4), m.group(5))} ({_short(m.group(2))})"),
        (rf"^Sold (.+?) · (.+?) · {MONEY}", lambda m: f"sold {m.group(1)} {_money(m.group(3), m.group(4), m.group(5))} ({_short(m.group(2))})"),
        (rf"^(.+?) (?:sold|final result) \((.+)\) · {MONEY}", lambda m: f"sold {m.group(1)} {_money(m.group(3), m.group(4), m.group(5))} ({_short(m.group(2))})"),
        (r"^(.+?) sold, confirming result", lambda m: f"sold {m.group(1)}, confirming result"),
        (r"^(.+?): exits weren't attached, so I created a server-side (-[\d.]+%) stop", lambda m: f"set a {m.group(2)} stop on {m.group(1)}"),
        (r"^(.+?): .*?stop (-[\d.]+%) live on GMGN", lambda m: f"set a {m.group(2)} stop on {m.group(1)}"),
        (r"^(.+?): (.+?),? market-selling", lambda m: f"selling {m.group(1)} ({_short(_reason(m.group(2)))})"),
        (r"^Skipped (.+?): (.+)$", lambda m: f"skipped {m.group(1)} ({m.group(2)})"),
        (r"^Refused (.+?): (.+)$", lambda m: f"refused {m.group(1)}"),
        (r"^(.+?) hit the (-[\d.]+%) stop", lambda m: f"stopped {m.group(1)}"),
    ]
    for p, f in pats:
        m = re.match(p, t)
        if m:
            return f(m)
    return None


def headline(kind: str, text: str) -> tuple[str, str]:
    first = (text or "").split("\n", 1)[0]
    if (kind or "").startswith("gmgn_copy"):            # DECISION 95: "🔁 Copied buy · A" etc. is already the headline
        return "Strat A", first.strip()
    w = who(kind, text)
    core, flags = strip_tags(first)
    act = action(core) if w != "Desk" or kind in ("buy", "sell", "risk") else None
    if act is None:
        title = f"{w} · {core}"
    elif act.startswith("· "):
        title = f"{w} {act}"
    else:
        title = f"{w} {act}"
    if "paper" in flags:
        title += " · paper"
    if "re-entry" in flags and "re-entry" not in title:
        title += " · re-entry"
    if "L8 skipped" in (text or "") and "L8 skipped" not in title:
        title += " · ⚠️ L8 skipped (rpc down)"
    return w, title


def is_labelled(text: str) -> bool:
    return (text or "").startswith(tuple(f"{l} " for l in LABELS))


def telegram(kind: str, text: str) -> str:
    """Headline first, then the original message with its old routing tags removed. Idempotent."""
    if not text or is_labelled(text):
        return text
    _, title = headline(kind, text)
    first, _, rest = text.partition("\n")
    core, _ = strip_tags(first)
    body = core if core and not title.endswith(core) else ""
    return "\n".join(x for x in (title, body, rest) if x)
