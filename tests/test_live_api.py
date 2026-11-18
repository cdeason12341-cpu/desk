"""DECISION 75: live droplet endpoint. Auth (derived bearer, constant-time), CORS to the Pages origin only, GET only,
rate limit, diffs by cursor, encrypted envelopes only (no secrets / tokens in responses), localhost bind."""
import inspect, json, time, urllib.request, urllib.error
import crypto, live_api, main
from publisher import Builder
from tests.helpers import make_ctx, run
from tests.test_publisher_gmgn import _desk, _order, SECRET_NAMES

TOKEN = "test-app-token-0123456789abcdef"
GOOD = live_api.ORIGIN


def _api(rate=None):
    d, c, clock, t = _desk()
    _order(c)
    d.publisher = type("P", (), {"builder": Builder(d)})()
    api = live_api.LiveApi(d, host="127.0.0.1", port=0, token_fn=lambda: TOKEN)
    if rate:
        api.rate = live_api.RateLimit(rate)
    api.start_server()
    api.port = api.server.server_address[1]
    return api, d, c


def _req(api, method="GET", path=live_api.PATH, headers=None, query=""):
    url = f"http://127.0.0.1:{api.port}{path}{query}"
    r = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _auth(tok=TOKEN):
    return {"Authorization": "Bearer " + live_api.auth_token(tok)}


def _built(api):
    api.touch()
    run(api.build_once())


def test_binds_localhost_only_by_default_and_wired_in_main():
    assert live_api.HOST == "127.0.0.1"
    assert "live_api" in inspect.getsource(main.Desk.run)
    assert inspect.signature(live_api.LiveApi.__init__).parameters["host"].default == "127.0.0.1"


def test_auth_required_and_derived():
    api, d, c = _api()
    try:
        _built(api)
        assert _req(api)[0] == 401
        assert _req(api, headers={"Authorization": "Bearer nope"})[0] == 401
        assert _req(api, headers={"Authorization": "Bearer " + TOKEN})[0] == 401      # raw token is not the bearer
        assert _req(api, headers=_auth("other-token-xxxxxxxx"))[0] == 401
        code, h, body = _req(api, headers=_auth())
        assert code == 200 and json.loads(body)["files"]
    finally:
        api.server.shutdown()


def test_constant_time_compare_used():
    assert "compare_digest" in inspect.getsource(live_api.LiveApi.check_auth)


def test_no_token_configured_rejects_everything():
    api, d, c = _api()
    api.token_fn = lambda: None
    try:
        assert _req(api, headers=_auth())[0] == 401
    finally:
        api.server.shutdown()


def test_cors_only_pages_origin():
    api, d, c = _api()
    try:
        _built(api)
        code, h, _ = _req(api, "OPTIONS", headers={"Origin": GOOD, "Access-Control-Request-Method": "GET"})
        assert code == 204 and h["Access-Control-Allow-Origin"] == GOOD and "Authorization" in h["Access-Control-Allow-Headers"]
        assert h["Access-Control-Allow-Methods"] == "GET"
        assert _req(api, "OPTIONS", headers={"Origin": "https://evil.example"})[0] == 403
        code, h, _ = _req(api, headers={"Origin": "https://evil.example", **_auth()})
        assert code == 403 and "Access-Control-Allow-Origin" not in h
        code, h, _ = _req(api, headers={"Origin": GOOD, **_auth()})
        assert code == 200 and h["Access-Control-Allow-Origin"] == GOOD and h["Cache-Control"] == "no-store"
    finally:
        api.server.shutdown()


def test_read_only_and_unknown_paths():
    api, d, c = _api()
    try:
        _built(api)
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            assert _req(api, m, headers=_auth())[0] == 405
        assert _req(api, path="/live/v1/settings", headers=_auth())[0] == 404
        assert _req(api, path="/", headers=_auth())[0] == 404
    finally:
        api.server.shutdown()


def test_rate_limit():
    api, d, c = _api(rate=3)
    try:
        _built(api)
        codes = [_req(api, headers=_auth())[0] for _ in range(5)]
        assert codes[:3] == [200, 200, 200] and codes[3] == 429
    finally:
        api.server.shutdown()


def test_envelopes_decrypt_and_no_secrets_in_body():
    api, d, c = _api()
    try:
        _built(api)
        code, h, body = _req(api, headers=_auth())
        text = body.decode()
        assert TOKEN not in text and live_api.auth_token(TOKEN) not in text
        for name in SECRET_NAMES:
            assert name not in text
        out = json.loads(text)
        assert set(out) == {"v", "cursor", "full", "builtAt", "builtAtMs", "buildMs", "files"}
        assert out["full"] is True and "state.json" in out["files"] and "agents/risk.json" in out["files"]
        for rel, env in out["files"].items():
            assert set(env) == {"v", "iv", "ct"}
            obj = crypto.decrypt_obj(env, TOKEN)
            plain = json.dumps(obj)
            assert "SHOULD_NOT_PUBLISH" not in plain and "ord_secretish" not in plain
        assert crypto.decrypt_obj(out["files"]["state.json"], TOKEN)["gmgn"]["mode"] == "LIVE"
    finally:
        api.server.shutdown()


def test_cursor_diff_sends_only_changes():
    api, d, c = _api()
    try:
        _built(api)
        first = json.loads(_req(api, headers=_auth())[2])
        cur = first["cursor"]
        _built(api)                                              # nothing changed (only ts) -> nothing re-sent
        again = json.loads(_req(api, headers=_auth(), query=f"?since={cur}")[2])
        assert again["files"] == {} and again["full"] is False and again["builtAtMs"] >= first["builtAtMs"]
        c.db.x("INSERT INTO activity(ts,kind,title,sub,night) VALUES(?,?,?,?,?)", (c.clock(), "info", "hello", "x", c.night))
        _built(api)
        diff = json.loads(_req(api, headers=_auth(), query=f"?since={again['cursor']}")[2])
        assert "activity.json" in diff["files"] and "chart.json" not in diff["files"]
        stale = json.loads(_req(api, headers=_auth(), query="?since=deadbeef.5")[2])   # other boot -> full
        assert stale["full"] is True and "chart.json" in stale["files"]
    finally:
        api.server.shutdown()


def test_idle_when_nobody_polls():
    api, d, c = _api()
    try:
        assert api.active() is False
        assert _req(api, headers=_auth())[0] == 503        # first poll wakes it; nothing built yet
        assert api.active() is True
    finally:
        api.server.shutdown()


def test_bad_coin_param_ignored():
    api, d, c = _api()
    try:
        _built(api)
        code, _, body = _req(api, headers=_auth(), query="?coin=../../etc/passwd&coin=x")
        assert code == 200 and not any(k.startswith("coin/") for k in json.loads(body)["files"])
        assert api.coins == {}
    finally:
        api.server.shutdown()


def test_no_access_log():
    h = live_api.make_handler(object())
    assert h.log_message(None, "%s", "x") is None


def test_clock_only_changes_are_throttled():
    s = live_api.Snapshot()
    base = {"ts": "a", "agents": {"x": {"heartbeatAgeS": 1.0}}, "bal": 1}
    s.update({"state.json": base}, TOKEN, now=1000.0)
    c0 = s.cursor()
    s.update({"state.json": dict(base, ts="b", agents={"x": {"heartbeatAgeS": 3.0}})}, TOKEN, now=1002.0)
    assert s.cursor() == c0                                     # only clocks ticked: not re-sent yet
    s.update({"state.json": dict(base, ts="c", bal=2)}, TOKEN, now=1004.0)
    assert s.cursor() != c0                                     # real change: sent right away
    c1 = s.cursor()
    s.update({"state.json": dict(base, ts="d", bal=2)}, TOKEN, now=1004.0 + live_api.VOLATILE_RESEND_S + 1)
    assert s.cursor() != c1                                     # clock-only change re-sent after the window
    c2 = s.cursor()
    s.update({"state.json": dict(base, ts="d", bal=2)}, TOKEN, now=1100.0)
    assert s.cursor() == c2                                     # identical: never re-sent
