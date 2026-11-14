"""STEP 13.2 test_gate: each just-below value rejects; all at threshold pass. Same in rules mode."""
import json
from agents.analyst import Analyst, gate, rules_answers
from tests.helpers import make_ctx, pair, run

MINT = "Gate1111111111111111111111111111111111pump"
OK = {"alive_6h": 0.70, "organic": 0.65, "flush": 0.75, "chase": 0.30}   # score = 70


def test_threshold_passes():
    c, _ = make_ctx()
    score, passed, failed = gate(c.s, OK)
    assert (score, passed, failed) == (70, True, [])


def test_each_rejects():
    c, _ = make_ctx()
    for k, v, label in (("alive_6h", 0.69, "alive_6h 0.69"), ("organic", 0.64, "organic 0.64"), ("chase", 0.31, "chase 0.31")):
        a = dict(OK, **{k: v})
        score, passed, failed = gate(c.s, a)
        assert not passed and failed[0] == label, (k, failed)
    a = dict(OK, flush=0.71)              # alive/organic/chase at threshold, score 69
    score, passed, failed = gate(c.s, a)
    assert score == 69 and not passed and failed == ["score 69"]


def _jev(a):
    return {"model": "jev-1.14.0", "answers": {k: {"type": "noul", "noul": v} for k, v in a.items()},
            "usage": {"input_tokens": 300, "output_tokens": 20}}


def test_jev_mode_end_to_end(monkeypatch):
    import secrets_store
    monkeypatch.setattr(secrets_store, "get", lambda n, d=None: "k" if n == "TYPESAFE_API_KEY" else d)
    c, _ = make_ctx(**{"analyst.mode": "jev"})
    an = Analyst(c)
    p = pair(MINT)
    c.net.jev_resp = _jev(OK)
    r = run(an.run({"mint": MINT, "name": "Gate", "ageMin": 12}, {"layers": []}, pair=p))
    assert r["passed"] and r["score"] == 70 and r["model"] == "jev-1.14.0" and r["inputTokens"] == 300
    c.net.jev_resp = _jev(dict(OK, chase=0.31))
    r = run(an.run({"mint": MINT[:-5] + "Bpump", "name": "Gate2", "ageMin": 12}, {"layers": []}, pair=p))
    assert not r["passed"] and r["failed"][0] == "chase 0.31"
    act = c.db.one("SELECT sub FROM activity ORDER BY id DESC LIMIT 1")["sub"]
    assert act == "Analyst · chase 0.31"
    c.net.jev_resp = {"model": "jev", "answers": {"alive_6h": {"noul": 1.4}}}
    r = run(an.run({"mint": MINT[:-5] + "Cpump", "name": "Gate3", "ageMin": 12}, {"layers": []}, pair=p))
    assert r["failed"] == ["bad answer"]


def test_rules_mode():
    c, _ = make_ctx(**{"analyst.mode": "rules"})
    an = Analyst(c)
    # threshold: alive = 17500/25000 = .70, organic = 97.5/150 = .65, chase = 24/80 = .30, flush 1 -> score 76
    p = pair(MINT, vol_h1=17500, buys_h1=97.5, h1=24, m5=-5, buys_m5=10, sells_m5=5)
    a = rules_answers(c.s, p, 0)
    assert abs(a["alive_6h"] - 0.70) < 1e-9 and abs(a["organic"] - 0.65) < 1e-9 and abs(a["chase"] - 0.30) < 1e-9 and a["flush"] == 1
    r = run(an.run({"mint": MINT, "name": "Rule", "ageMin": 12}, {"layers": []}, pair=p))
    assert r["passed"] and r["model"] == "rules" and r["inputTokens"] == 0
    cases = [(dict(vol_h1=17250), "alive_6h 0.69"), (dict(buys_h1=96), "organic 0.64"), (dict(h1=24.8), "chase 0.31")]
    for i, (kw, label) in enumerate(cases):
        base = dict(vol_h1=17500, buys_h1=97.5, h1=24, m5=-5, buys_m5=10, sells_m5=5)
        base.update(kw)
        m = MINT[:-6] + f"R{i}pump"
        r = run(an.run({"mint": m, "name": f"Rule{i}", "ageMin": 12}, {"layers": []}, pair=pair(m, **base)))
        assert not r["passed"] and r["failed"][0] == label, (label, r["failed"])
        act = c.db.one("SELECT sub FROM activity WHERE mint=?", (m,))["sub"]
        assert act == f"Analyst (rules) · {label}", act
    # bundled penalty: 3+ bundled wallets halves organic
    a2 = rules_answers(c.s, p, 3)
    assert abs(a2["organic"] - 0.325) < 1e-9
