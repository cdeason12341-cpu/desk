"""Pages publish: unique deploy version, busy-skip, live-ts readback (DECISION 47)."""
import json, time
from pathlib import Path

from ctx import iso
from main import Desk
from publisher import ALERT_COOLDOWN_S, STALE_LIVE_S, Publisher
from tests.helpers import run


def _pub(tmp_path):
    d = Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    d._allow_respawn = False
    return d, Publisher(d)


def test_workflow_pins_build_version_to_gist_rev():
    text = Path(".github/workflows/publish.yml").read_text()
    assert "cancel-in-progress: false" in text
    assert "pages_build_version" in text
    assert "pages-deploys" in text
    assert "commit-tree" in text
    assert "contents: write" in text
    assert "actions/deploy-pages@" not in text
    assert "${{ secrets.DATA_PATH }}" in text
    assert "${{ secrets.GIST_ID }}" in text
    assert "gist.githubusercontent.com" in text
    for name in ("TYPESAFE_API_KEY", "TG_TOKEN", "VAPID_PRIVATE_KEY", "APP_TOKEN",
                 "GMGN_API_KEY", "GMGN_SIGNING_KEY"):
        assert f"{name}:" not in text


def test_parse_ts_and_fresh_verify(tmp_path):
    d, pub = _pub(tmp_path)
    ts = iso()
    pub.fetch_live_state = lambda: {"ts": ts}
    assert run(pub.verify_live()) is True
    assert pub.last_live_ts == ts
    assert d.ctx.notifier.sent == []


def test_stale_verify_alerts_once_then_cools_down(tmp_path):
    d, pub = _pub(tmp_path)
    stale = iso(time.time() - (STALE_LIVE_S + 5 * 60))
    pub.fetch_live_state = lambda: {"ts": stale}
    assert run(pub.verify_live()) is False
    assert pub.dirty.is_set()
    assert len(d.ctx.notifier.sent) == 1
    body = d.ctx.notifier.sent[0]["text"]
    assert "min old" in body
    assert stale in body
    pub.dirty.clear()
    assert run(pub.verify_live()) is False
    assert len(d.ctx.notifier.sent) == 1  # cooldown
    pub.last_stale_alert = time.time() - ALERT_COOLDOWN_S - 1
    assert run(pub.verify_live()) is False
    assert len(d.ctx.notifier.sent) == 2


def test_verify_failed_fetch_does_not_raise_or_alert(tmp_path):
    d, pub = _pub(tmp_path)
    pub.fetch_live_state = lambda: (_ for _ in ()).throw(RuntimeError("network down"))
    assert run(pub.verify_live()) is False
    assert d.ctx.notifier.sent == []


def test_publish_once_defers_dispatch_when_run_in_progress(tmp_path, monkeypatch):
    d, pub = _pub(tmp_path)
    calls = []
    monkeypatch.setattr("publisher.DATA", tmp_path)
    monkeypatch.setattr("publisher.WAIT_IDLE_S", 0)
    (tmp_path / "_bundle.json").write_text(json.dumps({"v": 1, "files": {}}))

    async def fake_refresh():
        return None

    async def fake_sh(*args, **k):
        calls.append(args)
        if len(args) >= 3 and args[0] == "gh" and args[1] == "run" and args[2] == "list":
            return json.dumps([{"status": "in_progress"}])
        if args[:2] == ("gh", "api") and "-X" not in args:
            return json.dumps({"files": ["bundle.json"]})
        if args[:2] == ("gh", "api") and "-X" in args:
            return "aaaabbbbccccddddeeeeffff0000111122223333\n"
        if args[:2] == ("gh", "workflow"):
            raise AssertionError("must not dispatch while a run is in progress")
        return ""

    monkeypatch.setattr("publisher.secrets_store.get", lambda k: "fake-gist-id")
    pub.refresh_gmgn = fake_refresh
    pub.write_local = lambda: 3
    pub.sh = fake_sh
    # wait_idle default max_s is bound at definition; pass 0 via patched method default
    orig = pub.wait_idle

    async def wait0(max_s=0):
        return await Publisher.wait_idle(pub, max_s=0)

    pub.wait_idle = wait0
    rev = run(pub.publish_once())
    assert rev.startswith("aaaa")
    assert pub.dirty.is_set()
    assert not any(c[:2] == ("gh", "workflow") for c in calls)
    assert any(c[:3] == ("gh", "run", "list") for c in calls)
