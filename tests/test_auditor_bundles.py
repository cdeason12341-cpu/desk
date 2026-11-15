"""Auditor layer 7: reject when bundles hold more than 20% of supply.
Count of linked wallets is shown but is not a reject rule."""
import json
import config
from agents.auditor import bundle_stats, bundle_layer, biggest_group
from tests.helpers import make_ctx


def _rep(supply, networks):
    return {"token": {"supply": supply}, "insiderNetworks": networks}


def _net(size, holding):
    return {"id": f"n{size}-{holding}", "size": size, "type": "transfer",
            "tokenAmount": holding, "currentHolding": holding, "activeAccounts": size}


def test_under_20_passes():
    # 19.9% across two networks, 6 linked in the biggest — under the cap, count does not reject
    ok, value, pct, linked = bundle_layer(_rep(1000, [_net(6, 150), _net(3, 49)]), 20)
    assert ok and abs(pct - 19.9) < 1e-9 and linked == 6
    assert value == "6 linked, 19.9% of supply"


def test_over_20_rejects():
    ok, value, pct, linked = bundle_layer(_rep(1000, [_net(3, 320)]), 20)
    assert not ok and abs(pct - 32) < 1e-9 and linked == 3
    assert value == "32% held by bundles"


def test_equal_to_20_passes():
    # "more than 20%" — exactly 20% is a pass
    ok, value, pct, linked = bundle_layer(_rep(500, [_net(4, 100)]), 20)
    assert ok and pct == 20 and linked == 4
    assert value == "4 linked, 20.0% of supply"


def test_missing_data_is_red():
    # malformed list / missing currentHolding / networks but no supply
    for rep in (
        {"token": {"supply": 1000}, "insiderNetworks": "oops"},
        {"token": {"supply": 1000}, "insiderNetworks": [{"size": 3}]},
        {"token": {"supply": 0}, "insiderNetworks": [_net(2, 10)]},
        {"token": {}, "insiderNetworks": [_net(2, 10)]},
        "not-a-dict",
    ):
        ok, value, pct, linked = bundle_layer(rep, 20)
        assert ok is False and value == "no data: bundles" and pct is None and linked is None, rep


def test_no_networks_is_zero_not_missing():
    # Same convention as an empty insiders/graph: no bundles detected → 0%, pass
    for rep in (
        {"token": {"supply": 1000}, "insiderNetworks": None},
        {"token": {"supply": 1000}, "insiderNetworks": []},
        {"insiderNetworks": None},
    ):
        ok, value, pct, linked = bundle_layer(rep, 20)
        assert ok and pct == 0 and linked == 0 and value == "None", rep


def test_count_does_not_reject():
    # 8 linked wallets holding 10% — would have failed the old max_bundled=5 rule
    ok, value, pct, linked = bundle_layer(_rep(1000, [_net(8, 100)]), 20)
    assert ok and linked == 8 and pct == 10 and "8 linked" in value


def test_real_nyajira_shape():
    # Live RugCheck report 2026-11-14, mint 4qyk1uJexNRNUwfx9J4KKoJ1v5icmZNci42yDzgEpump
    rep = {"token": {"supply": 961589058081961}, "insiderNetworks": [
        {"id": "proud-ebony-orangutan", "size": 4, "type": "transfer",
         "tokenAmount": 36747808633751, "currentHolding": 36747808633751, "activeAccounts": 4},
        {"id": "loud-cerise-kiwi", "size": 4, "type": "transfer",
         "tokenAmount": 2818343100000, "currentHolding": 2818343100000, "activeAccounts": 4},
    ]}
    pct, linked = bundle_stats(rep)
    ok, value, _, _ = bundle_layer(rep, 20)
    assert ok and linked == 4 and abs(pct - 4.114663265061672) < 1e-9
    assert value == "4 linked, 4.1% of supply"


def test_setting_alias_and_range():
    c, _ = make_ctx()
    assert c.s["auditor.max_bundle_pct"] == 20
    assert c.s["auditor.max_bundled"] == 1000
    assert c.s["analyst.rules_bundled_from"] == 5
    assert config.resolve("bundle pct").key == "auditor.max_bundle_pct"
    assert config.resolve("bundle percent").key == "auditor.max_bundle_pct"
    msg = c.s.set("bundle pct", 25, source="telegram")
    assert "20%" in msg and "25%" in msg
    assert c.s["auditor.max_bundle_pct"] == 25
    row = c.db.one("SELECT key, old, new, source FROM change_log WHERE key='auditor.max_bundle_pct' ORDER BY id DESC")
    assert row["source"] == "telegram"
    assert json.loads(row["old"]) == 20 and json.loads(row["new"]) == 25
    for bad in (0, 101, 12.5, "abc"):
        try:
            c.s.set("bundle pct", bad, source="test")
            raise AssertionError(f"accepted {bad}")
        except config.SettingError:
            pass
    assert c.s["auditor.max_bundle_pct"] == 25


def test_biggest_group_still_counts_graph():
    # kept for the graph helper; layer 7 no longer rejects on this count
    g = [{"net_id": "a", "network_type": "transfer", "relatedMint": None,
          "nodes": [{"id": "A", "participant": True, "holdings": 10},
                    {"id": "B", "participant": True, "holdings": 10},
                    {"id": "C", "participant": True, "holdings": 10}],
          "links": [{"source": "A", "target": "B"}, {"source": "B", "target": "C"}]}]
    assert biggest_group(g) == 3
    assert biggest_group([]) == 0
