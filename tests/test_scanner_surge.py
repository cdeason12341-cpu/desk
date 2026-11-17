"""DECISION 41 (owner 2026-11-15 6:52 PM CT): young pools first (not exclusive) and MC-surge re-scans.
A surge only re-queues a coin with priority; every scanner/auditor/analyst check still applies."""
import asyncio, json
import config, stage_pings
from agents.scanner import Scanner, SurgeWatch, surge_eligible, prioritize, base_reason
from publisher import Builder
from tests.helpers import make_ctx, pair, run
from tests.test_publisher_gmgn import DeskStub

NOW = 1_800_000_000.0
A = "Srga111111111111111111111111111111111pump"
B = "Srgb111111111111111111111111111111111pump"
C = "Srgc111111111111111111111111111111111pump"
D = "Srgd111111111111111111111111111111111pump"
E = "Srge111111111111111111111111111111111pump"


def _ctx(**kw):
    base = {"scanner.min_liquidity_usd": 2500, "scanner.min_market_cap_usd": 25000, "scanner.max_top10_pct": 30,
            "scanner.recheck_minutes": "off"}
    base.update(kw)
    c, clock = make_ctx(**base)
    clock.t = NOW
    sc = Scanner(c)

    async def fake_top10(mint, pool):
        return 12.0
    sc.top10 = fake_top10
    return c, clock, sc


def _add(c, mint, age_min, status="room", reason=None):
    ts = NOW - age_min * 60.0
    c.db.x("INSERT INTO coins(mint,migrated_at,first_seen,status,reason,night) VALUES(?,?,?,?,?,?)",
           (mint, ts, ts, status, reason, c.night))


def _pair(mint, mc, liq=50000):
    p = pair(mint, liq=liq)
    p["marketCap"] = mc
    return p


def test_settings_defaults_aliases_and_bounds():
    c, _ = make_ctx()
    assert c.s["scanner.young_priority_min"] == 60 and c.s["scanner.surge_pct"] == 25
    assert c.s["scanner.surge_window_s"] == 60 and c.s["scanner.surge_watch_max"] == 150 and c.s["scanner.surge_poll_s"] == 10
    for alias, key in (("young first", "scanner.young_priority_min"), ("young priority", "scanner.young_priority_min"),
                       ("surge", "scanner.surge_pct"), ("surge window", "scanner.surge_window_s"),
                       ("surge watch", "scanner.surge_watch_max"), ("surge poll", "scanner.surge_poll_s")):
        assert config.resolve(alias).key == key
    assert "+40%" in c.s.set("surge", "40%")
    c.s.set("young first", "off")
    assert c.s["scanner.young_priority_min"] == 0
    for k, bad in (("surge watch", "301"), ("surge window", "5"), ("surge poll", "2")):
        try:
            c.s.set(k, bad)
            raise AssertionError(f"{k} accepted {bad}")
        except config.SettingError:
            pass


def test_surge_eligibility_keeps_safety_kills_dead():
    assert surge_eligible("room", None)
    for r in ("liq $1.2K", "mc $18.0K", "+140% in 1h", "liq $300 · too far for recheck", "recheck 2: mc $26.7K · next at 45m"):
        assert surge_eligible("killed", r) and surge_eligible("recheck", r), r
    for r in ("not a pump mint", "not SOL-quoted (USDC)", "top 10 hold 41%", "recheck 1: top 10 hold 39% · next at 45m",
              "copycat of X", "never qualified", None):
        assert not surge_eligible("killed", r) and not surge_eligible("recheck", r), r
    assert not surge_eligible("passed", None) and not surge_eligible("dropped", "liq $1K")
    assert base_reason("recheck 2: mc $26.7K · next at 10m") == "mc $26.7K"


def test_young_first_but_old_still_scanned():
    c, clock, sc = _ctx()
    coins = [{"mint": "old2", "migrated_at": NOW - 300 * 60}, {"mint": "y30", "migrated_at": NOW - 30 * 60},
             {"mint": "old1", "migrated_at": NOW - 90 * 60}, {"mint": "y5", "migrated_at": NOW - 5 * 60},
             {"mint": "sg", "migrated_at": NOW - 200 * 60, "surge": {"pct": 30}}]
    assert [x["mint"] for x in prioritize(c.s, coins, NOW)] == ["sg", "y5", "y30", "old2", "old1"]
    c.s.set("young first", "0")
    assert [x["mint"] for x in prioritize(c.s, coins, NOW)] == ["sg", "old2", "y30", "old1", "y5"]
    # end to end: an old coin in the room is still scanned in the same poll
    c.s.set("young first", "60")
    _add(c, A, 120); _add(c, B, 15)
    c.net.pairs[A] = _pair(A, 40000); c.net.pairs[B] = _pair(B, 40000)
    run(sc.poll_once())
    order = [r["mint"] for r in c.db.rows("SELECT mint FROM scans ORDER BY id")]
    assert order == [B, A]


def test_due_rechecks_young_first_under_per_poll_limit():
    c, clock, sc = _ctx(**{"scanner.recheck_per_poll": 1})
    _add(c, A, 200, "recheck", "liq $2.0K"); _add(c, B, 20, "recheck", "liq $2.0K")
    for m in (A, B):
        c.db.x("INSERT INTO rechecks(mint,n,next_at,updated) VALUES(?,0,?,?)", (m, NOW - (100 if m == A else 10), NOW))
    assert [r["mint"] for r in sc.due_rechecks(NOW)] == [B]           # younger wins the single slot
    assert [r["mint"] for r in sc.due_rechecks(NOW)] == [A]           # older is next, not dropped


def test_surge_watch_window_threshold_and_cooldown():
    w = SurgeWatch()
    assert w.observe(A, 20000, NOW, 25, 60) is None
    assert w.observe(A, 24000, NOW + 20, 25, 60) is None               # +20%
    hit = w.observe(A, 26000, NOW + 40, 25, 60)
    assert hit and hit["pct"] == 30.0 and hit["secs"] == 40 and hit["mcFrom"] == 20000
    assert w.observe(A, 40000, NOW + 50, 25, 60) is None               # cooldown
    w2 = SurgeWatch()
    w2.observe(B, 20000, NOW, 25, 60)
    assert w2.observe(B, 30000, NOW + 90, 25, 60) is None              # low is outside the 60s window
    assert w2.observe(B, None, NOW + 95, 25, 60) is None
    w3 = SurgeWatch()                                                  # dust pool: +100% on $14 MC is noise
    w3.observe(C, 14, NOW, 25, 60)
    assert w3.observe(C, 28, NOW + 10, 25, 60) is None


def test_surge_requeues_killed_coin_first_with_full_checks_and_tags():
    c, clock, sc = _ctx()
    _add(c, A, 20, "killed", "mc $20.0K")                              # surges, then passes the full scan
    _add(c, B, 20, "killed", "not SOL-quoted (USDC)")                  # safety: never watched
    _add(c, C, 20, "killed", "top 10 hold 45%")                        # top-10 kill: never watched
    _add(c, D, 20, "killed", "mc $10.0K")                              # surges but liq fails: stays killed
    _add(c, E, 50, "room")
    for m in (A, B, C): c.net.pairs[m] = _pair(m, 20000)
    c.net.pairs[D] = _pair(D, 10000, liq=1000)
    c.net.pairs[E] = _pair(E, 40000)
    assert run(sc.surge_once()) == 3                                   # A, D, E watched
    clock.t = NOW + 10
    for m in (A, B, C): c.net.pairs[m] = _pair(m, 26000)
    c.net.pairs[D] = _pair(D, 14000, liq=1000)
    run(sc.surge_once())
    assert set(sc.surge_queue) == {A, D}
    assert {r["mint"] for r in c.db.rows("SELECT mint FROM surges")} == {A, D}
    run(sc.poll_once())
    scans = c.db.rows("SELECT mint, verdict, reason, json FROM scans ORDER BY id")
    assert [s["mint"] for s in scans][:2] in ([A, D], [D, A])          # surges first, before the room coin
    assert scans[-1]["mint"] == E
    by = {s["mint"]: s for s in scans}
    assert by[A]["verdict"] == "pass" and by[A]["reason"] == "⚡ Surge +30% in 1m"
    assert json.loads(by[A]["json"])["surge"]["pct"] == 30.0
    assert c.coin(A)["sent"] == 1 and c.bus.auditor.get_nowait()["surge"]["pct"] == 30.0
    assert by[D]["verdict"] == "kill" and by[D]["reason"].startswith("⚡ Surge +40% in 1m · liq ")
    assert c.coin(D)["status"] == "killed" and c.coin(D)["sent"] == 0 and c.coin(D)["reason"].startswith("liq ")
    assert c.db.one("SELECT COUNT(*) n FROM surges WHERE scanned=1")["n"] == 2


def test_surge_too_young_goes_back_to_waiting_room():
    c, clock, sc = _ctx()
    _add(c, A, 3, "killed", "mc $20.0K")                              # under min pool age (10m): can't pass yet
    c.net.pairs[A] = _pair(A, 20000)
    run(sc.surge_once()); clock.t = NOW + 10; c.net.pairs[A] = _pair(A, 30000); run(sc.surge_once())
    run(sc.poll_once())
    assert c.coin(A)["status"] == "room" and c.coin(A)["sent"] == 0
    s = c.db.rows("SELECT verdict, reason, json FROM scans WHERE mint=?", (A,))[-1]
    assert s["reason"] == "surge: clears filters, back in waiting room" and json.loads(s["json"])["surge"]


def test_watch_cap_youngest_first_and_off():
    c, clock, sc = _ctx(**{"scanner.surge_watch_max": 2})
    _add(c, A, 100, "killed", "liq $1K"); _add(c, B, 10, "killed", "liq $1K"); _add(c, C, 50, "room")
    assert [r["mint"] for r in sc.surge_candidates(NOW)] == [B, C]
    c.s.set("surge watch", "0")
    assert sc.surge_candidates(NOW) == [] and run(sc.surge_once()) == 0


def test_dex_budget_guard_skips_sweep():
    c, clock, sc = _ctx()
    from net import MinuteBucket
    import time as _t
    c.net.dex_bucket = MinuteBucket(10)
    c.net.dex_bucket.stamps = [_t.time()] * 6                         # 60% already used
    _add(c, A, 10, "room")
    called = []
    orig = c.net.dex_tokens
    async def spy(m):
        called.append(m); return await orig(m)
    c.net.dex_tokens = spy
    assert run(sc.surge_once()) == 1 and called == []


def test_stage_pings_carry_surge_tag():
    c, clock = make_ctx()
    out = {"mint": A, "name": "Zap", "liquidityUsd": 12345, "marketCapUsd": 40000, "surge": {"pct": 31.6, "windowS": 60}}
    assert stage_pings.scanner_text(out) == "⚡ Surge +32% in 1m · ✅ Scanner pass · Zap · liq $12.3K · MC $40.0K"
    c.db.x("INSERT INTO surges(ts,mint,pct,window_s,night) VALUES(?,?,?,?,?)", (clock.t, A, 31.6, 60, c.night))

    async def go():
        stage_pings.ping(c, "auditor", A, "🛡️ Audit pass · Zap · top10 12%")
        stage_pings.ping(c, "auditor", B, "🛡️ Audit pass · Other · top10 12%")
        for t in list(getattr(c, "_ping_tasks", ())):
            await t
    asyncio.run(go())
    txt = {m["mint"]: m["text"] for m in c.notifier.sent}
    assert txt[A] == "⚡ Surge +32% in 1m · 🛡️ Audit pass · Zap · top10 12%"
    assert txt[B] == "🛡️ Audit pass · Other · top10 12%"


def test_scanner_feed_surge_pill_and_limits():
    c, clock = make_ctx()
    body = {"agent": "scanner", "mint": A, "verdict": "pass", "liquidityUsd": 9000, "marketCapUsd": 40000,
            "surge": {"pct": 30.0, "windowS": 60, "secs": 40, "mcFrom": 20000, "mcTo": 26000}}
    c.db.x("INSERT INTO scans(ts,mint,verdict,reason,json,night) VALUES(?,?,?,?,?,?)",
           (clock.t - 5, A, "pass", "⚡ Surge +30% in 1m", json.dumps(body), c.night))
    c.db.x("INSERT INTO surges(ts,mint,pct,window_s,night) VALUES(?,?,?,?,?)", (clock.t - 10, A, 30.0, 60, c.night))
    f = Builder(DeskStub(c)).build()["scanner.json"]
    assert f["items"][0]["surge"]["pct"] == 30.0 and f["items"][0]["pill"] == "pass"
    assert f["counts1h"]["surges"] == 1
    L = f["limits"]
    assert L["youngPriorityMin"] == 60 and L["surge"] == {"pct": 25, "windowS": 60, "watchMax": 150, "pollS": 10}
