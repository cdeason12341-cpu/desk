"""Settings written to the DB outside the running desk (logged in change_log) hot-load; protected keys never do."""
import json, time
import config
from tests.helpers import make_ctx


def _external_set(db, key, old, new):
    db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(new)))
    db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)", (time.time(), key, json.dumps(old), json.dumps(new), "chat"))


def test_external_change_hot_loads_and_protected_does_not():
    c, _ = make_ctx()
    s = c.s
    assert s.sync() == []
    seen = []
    s.listeners.append(lambda k, o, n: seen.append((k, o, n)))
    _external_set(c.db, "analyst.min_alive_6h", 0.7, 0.0)
    _external_set(c.db, "analyst.min_organic", 0.65, 0.0)
    _external_set(c.db, "analyst.max_chase", 0.3, 1.0)
    _external_set(c.db, "gmgn.cap_trade_usd", 65, 500)
    assert sorted(s.sync()) == ["analyst.max_chase", "analyst.min_alive_6h", "analyst.min_organic"]
    assert s["analyst.min_alive_6h"] == 0.0 and s["analyst.min_organic"] == 0.0 and s["analyst.max_chase"] == 1.0
    assert s["gmgn.cap_trade_usd"] != 500                       # caps only via CONFIRM CAP
    assert ("analyst.max_chase", 0.3, 1.0) in seen
    assert s.sync() == []                                       # nothing new
    s.set("min organic", "0.5", source="test")                  # in-process change: sync is a no-op
    assert s.sync() == [] and s["analyst.min_organic"] == 0.5
