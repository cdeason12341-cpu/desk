"""Out-of-process supervisor: clock-jump vs real stale heartbeats, and a blocked-loop kill."""
import json, os, signal, subprocess, sys, textwrap, time
from pathlib import Path

from supervise_desk import clock_skew, decide, tick, STALE_S


def test_clock_skew_detects_pause_not_normal_time():
    assert abs(clock_skew(1000, 10, 1010, 20) - 0) < 1e-6
    # 40 min wall passed, 15s monotonic — VM pause
    assert clock_skew(1000, 10, 1000 + 40 * 60, 25) == 40 * 60 - 15


def test_decide_clock_jump_does_not_kill():
    action, reason, grace = decide(
        prev_wall=1_000, prev_mono=10,
        now_wall=1_000 + 26 * 60, now_mono=25,
        file_age=26 * 60, pid_is_alive=True, stale_s=180, jump_s=60, grace_s=90,
    )
    assert action == "clock_jump"
    assert "paused/resumed" in reason
    action2, _, _ = decide(
        prev_wall=1_000 + 26 * 60, prev_mono=25,
        now_wall=1_000 + 26 * 60 + 5, now_mono=30,
        file_age=26 * 60 + 5, pid_is_alive=True, stale_s=180,
        grace_until_mono=grace,
    )
    assert action2 == "grace"


def test_decide_stale_heartbeats_kills_when_no_jump():
    action, reason, _ = decide(
        prev_wall=1_000, prev_mono=10,
        now_wall=1_200, now_mono=210,
        file_age=200, pid_is_alive=True, stale_s=180, jump_s=60,
    )
    assert action == "kill_restart"
    assert "stale" in reason


def test_decide_dead_pid_starts():
    action, _, _ = decide(None, None, 10, 10, 0, False, stale_s=180)
    assert action == "start"


def test_tick_clock_jump_then_fresh_hb_is_ok(tmp_path):
    hb = tmp_path / "heartbeats.json"
    pidp = tmp_path / "desk.pid"
    pidp.write_text(str(os.getpid()))
    now = time.time()
    hb.write_text(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 26 * 60)),
                              "pid": os.getpid(), "ageS": {"desk": 0}}))
    state = {"prev_wall": now - 26 * 60, "prev_mono": time.monotonic() - 15}
    action, reason = tick(state, hb_path=hb, pid_path=pidp, stale_s=180, do_act=False)
    assert action == "clock_jump", (action, reason)
    # after resume, a fresh heartbeat should be ok even if grace is set
    hb.write_text(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 2)),
                              "pid": os.getpid(), "ageS": {"desk": 0}}))
    action, reason = tick(state, hb_path=hb, pid_path=pidp, stale_s=180, grace_s=90, do_act=False)
    assert action in ("grace", "ok"), (action, reason)


def test_supervisor_kills_blocked_loop_and_restarts(tmp_path):
    """Deliberate blocked-loop hook: a child writes one heartbeat then sleeps forever
    (simulating a sync call that stalls the asyncio loop and stops heartbeats.json)."""
    hb = tmp_path / "heartbeats.json"
    pidp = tmp_path / "desk.pid"
    restarted = tmp_path / "restarted"
    child_src = textwrap.dedent(f"""
        import json, os, time, sys
        from pathlib import Path
        hb, pidp = Path({str(hb)!r}), Path({str(pidp)!r})
        pidp.write_text(str(os.getpid()))
        def write():
            hb.write_text(json.dumps({{
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "pid": os.getpid(), "ageS": {{"desk": 0, "scanner": 0}},
            }}))
        write()
        time.sleep(600)
    """)
    script = tmp_path / "blocked.py"
    script.write_text(child_src)
    proc = subprocess.Popen([sys.executable, str(script)], cwd=str(tmp_path))
    try:
        for _ in range(20):
            if pidp.exists() and hb.exists():
                break
            time.sleep(0.05)
        assert proc.poll() is None
        # make the heartbeat look 5s stale; supervisor stale_s is 0.8s
        state = {}
        tick(state, hb_path=hb, pid_path=pidp, stale_s=0.8, do_act=False)
        # Force a stale wall timestamp without a monotonic jump (wedged loop).
        hb.write_text(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5)),
            "pid": proc.pid, "ageS": {"desk": 5, "scanner": 5},
        }))
        action, reason = tick(
            state, hb_path=hb, pid_path=pidp, stale_s=0.8, jump_s=60,
            restart_cmd=["bash", "-c", f"echo ok > {restarted}"], do_act=True,
            log_fn=lambda m: None, restart_out=subprocess.DEVNULL,
        )
        assert action == "kill_restart", (action, reason)
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None, "blocked child should have been SIGKILL'd"
        for _ in range(40):
            if restarted.exists():
                break
            time.sleep(0.05)
        assert restarted.exists()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=2)
