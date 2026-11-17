"""Timeouts and the in-process watchdog that break the hour-long event-loop stalls."""
import asyncio, json, time
from pathlib import Path

import net as netmod
from main import Desk
from publisher import PUBLISH_TIMEOUT_S, Publisher
from tests.helpers import make_ctx, run


def test_net_hard_timeout_raises_source_error():
    c, _ = make_ctx()
    n = netmod.Net(c.s)

    class Slow:
        async def get(self, *a, **k):
            await asyncio.sleep(5)
            raise AssertionError("httpx should have been cancelled")

    n.http = Slow()
    t0 = time.monotonic()
    try:
        run(n._send(n.http.get("https://example.invalid"), 0.15, "dexscreener"))
        assert False, "expected SourceError"
    except netmod.SourceError as e:
        assert "timeout" in str(e)
    assert time.monotonic() - t0 < 1.0


def test_sh_timeout_kills_child(tmp_path):
    d = Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    d._allow_respawn = False
    pub = Publisher(d)
    t0 = time.monotonic()
    try:
        run(pub.sh("sleep", "30", timeout=0.3))
        assert False, "expected timeout"
    except RuntimeError as e:
        assert "timeout" in str(e)
    assert time.monotonic() - t0 < 2.0


def test_sh_cancel_kills_child(tmp_path):
    d = Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    d._allow_respawn = False
    pub = Publisher(d)

    async def body():
        task = asyncio.create_task(pub.sh("sleep", "30", timeout=10))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
            assert False, "expected CancelledError"
        except asyncio.CancelledError:
            pass

    t0 = time.monotonic()
    run(body())
    assert time.monotonic() - t0 < 2.0


def test_publish_once_overall_timeout(tmp_path, monkeypatch):
    d = Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    d._allow_respawn = False
    pub = Publisher(d)

    async def hang(*a, **k):
        await asyncio.sleep(5)

    pub.refresh_gmgn = hang
    t0 = time.monotonic()

    async def body():
        try:
            await asyncio.wait_for(pub.publish_once(), timeout=0.2)
            assert False
        except asyncio.TimeoutError:
            pass

    run(body())
    assert time.monotonic() - t0 < 1.5
    assert PUBLISH_TIMEOUT_S <= 120


def test_watchdog_dumps_and_restarts_stale_agent(tmp_path):
    d = Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    d._allow_respawn = False
    d.stale_s = 0.4
    d.hb_path = tmp_path / "heartbeats.json"
    d.watchdog_log = tmp_path / "watchdog.log"
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hung():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def body():
        d._loop = asyncio.get_running_loop()
        d._loop_tick = time.monotonic()
        d._jobs = {"scanner": hung}
        sup = asyncio.create_task(d.supervised("scanner", hung), name="sup-scanner")
        await started.wait()
        now = time.time()
        # ts slightly in the future so second-resolution file_age is 0 (not "loop frozen")
        d.hb_path.write_text(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 2)),
            "pid": 0, "ageS": {"scanner": 5.0}, "wsPongAgeS": 0.1,
        }))
        actions = d.check_watchdog(respawn=False)
        assert any(a[0] == "restart" and a[1] == "scanner" for a in actions)
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        # supervised should spawn a new inner task
        await asyncio.sleep(0.05)
        assert d._inner.get("scanner") is not None
        d._shutdown = True
        sup.cancel()
        try:
            await sup
        except asyncio.CancelledError:
            pass

    run(body())
    text = d.watchdog_log.read_text()
    assert "scanner heartbeat stale" in text
    assert "Thread" in text or "faulthandler" in text or "Task" in text or "watchdog" in text


def test_watchdog_does_not_respawn_when_disabled(tmp_path):
    d = Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    d._allow_respawn = False
    d.stale_s = 0.2
    d.hb_path = tmp_path / "heartbeats.json"
    d.watchdog_log = tmp_path / "watchdog.log"
    d._loop_tick = time.monotonic() - 30  # frozen
    actions = d.check_watchdog(respawn=False)
    assert actions and actions[0][0] == "frozen"
    assert "event loop frozen" in actions[0][1]
    # process must still be us
    import os
    assert os.path.exists("/proc/%d" % os.getpid())


def test_watchdog_clock_jump_is_not_a_wedge(tmp_path):
    d = Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    d._allow_respawn = False
    d.stale_s = 0.2
    d.hb_path = tmp_path / "heartbeats.json"
    d.watchdog_log = tmp_path / "watchdog.log"
    now = time.time()
    d._loop_tick = time.monotonic()  # loop is fresh
    d._prev_wall = now - 26 * 60
    d._prev_mono = time.monotonic() - 15
    d.hb_path.write_text(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 26 * 60)),
        "pid": 0, "ageS": {"scanner": 26 * 60, "publisher": 26 * 60},
    }))
    actions = d.check_watchdog(respawn=True)  # even if respawn were allowed
    assert actions and actions[0][0] == "clock_jump"
    assert "paused/resumed" in actions[0][1]
