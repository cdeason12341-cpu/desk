"""Out-of-process desk supervisor.

Checks logs/heartbeats.json every 30s and restarts the desk with SIGKILL +
run_desk.sh if it is genuinely dead or wedged. This lives in a separate
process so a stuck asyncio loop or a GIL-holding C call in main.py cannot
silence it.

It cannot run while the whole VM/box is paused (the supervisor is paused
too). After a resume, wall-clock file_age looks huge while time.monotonic
barely advanced — that is a clock jump, not a wedged loop, and must not
trigger a kill. See DECISIONS.
"""
from __future__ import annotations
import datetime as dt, json, os, signal, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
HB_PATH = LOGS / "heartbeats.json"
PID_PATH = LOGS / "desk.pid"
SUP_PID = LOGS / "supervise.pid"
SUP_LOG = LOGS / "supervise.log"
STALE_S = 180
INTERVAL_S = 30
JUMP_S = 60
GRACE_S = 90


def iso(ts: float | None = None) -> str:
    return dt.datetime.fromtimestamp(ts if ts is not None else time.time(), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str, path: Path = SUP_LOG):
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{iso()}\tsupervise\t{msg}\n"
    with path.open("a") as f:
        f.write(line)


def clock_skew(prev_wall: float, prev_mono: float, now_wall: float, now_mono: float) -> float:
    """How much wall time ran ahead of monotonic (seconds). Large after a VM pause."""
    return (now_wall - prev_wall) - (now_mono - prev_mono)


def parse_hb(path: Path, now_wall: float | None = None):
    now_wall = time.time() if now_wall is None else now_wall
    if not path.exists():
        return {}, None, now_wall, None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}, None, now_wall, None
    file_age, written = now_wall, None
    ts = data.get("ts")
    if ts:
        try:
            written = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
            file_age = max(0.0, now_wall - written)
        except Exception:
            written = None
    return data, written, file_age, data.get("pid")


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def decide(prev_wall, prev_mono, now_wall, now_mono, file_age, pid_is_alive,
           stale_s=STALE_S, jump_s=JUMP_S, grace_s=GRACE_S, grace_until_mono=0.0):
    """Pure decision. Returns (action, reason, grace_until_mono).

    action: ok | clock_jump | grace | start | kill_restart
    """
    skew = 0.0 if prev_wall is None else clock_skew(prev_wall, prev_mono, now_wall, now_mono)
    if prev_wall is not None and skew > jump_s:
        return "clock_jump", f"box paused/resumed wall-mono skew={skew:.0f}s file_age={file_age:.0f}s", now_mono + grace_s
    if now_mono < grace_until_mono:
        return "grace", f"post-resume grace {grace_until_mono - now_mono:.0f}s left file_age={file_age:.0f}s", grace_until_mono
    if not pid_is_alive:
        return "start", "desk pid is not running", grace_until_mono
    # After a pause, file_age is huge even if the desk just woke; subtract the
    # skew already handled above. Here skew is small, so wall file_age is real.
    if file_age is not None and file_age > stale_s:
        return "kill_restart", f"heartbeats stale file_age={file_age:.0f}s", grace_until_mono
    return "ok", f"file_age={file_age:.0f}s", grace_until_mono


def kill_and_restart(pid, restart_cmd, cwd=None, out=None):
    if pid and pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
        for _ in range(20):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
    cwd = cwd or str(ROOT)
    dest = out if out is not None else open(LOGS / "desk.out", "a")
    subprocess.Popen(restart_cmd, cwd=cwd, start_new_session=True,
                     stdout=dest, stderr=subprocess.STDOUT)


def tick(state, now_wall=None, now_mono=None, hb_path=HB_PATH, pid_path=PID_PATH,
         stale_s=STALE_S, jump_s=JUMP_S, grace_s=GRACE_S, restart_cmd=None, do_act=True,
         log_fn=None, restart_out=None):
    """One supervisor check. state is a mutable dict. Returns (action, reason)."""
    now_wall = time.time() if now_wall is None else now_wall
    now_mono = time.monotonic() if now_mono is None else now_mono
    data, written, file_age, hb_pid = parse_hb(hb_path, now_wall)
    file_pid = read_pid(pid_path)
    pid = file_pid or hb_pid
    alive = pid_alive(pid)
    action, reason, grace = decide(
        state.get("prev_wall"), state.get("prev_mono"),
        now_wall, now_mono, file_age, alive,
        stale_s=stale_s, jump_s=jump_s, grace_s=grace_s,
        grace_until_mono=state.get("grace_until_mono") or 0.0,
    )
    state["prev_wall"], state["prev_mono"] = now_wall, now_mono
    state["grace_until_mono"] = grace
    if do_act and action in ("start", "kill_restart"):
        cmd = restart_cmd or [str(ROOT / "run_desk.sh")]
        (log_fn or log)(f"{action}\t{reason}\tpid={pid}")
        if action == "kill_restart":
            kill_and_restart(pid, cmd, out=restart_out)
        else:
            dest = restart_out if restart_out is not None else open(LOGS / "desk.out", "a")
            subprocess.Popen(cmd, cwd=str(ROOT), start_new_session=True,
                             stdout=dest, stderr=subprocess.STDOUT)
    return action, reason


def main():
    LOGS.mkdir(exist_ok=True)
    SUP_PID.write_text(str(os.getpid()))
    log(f"start pid={os.getpid()} stale={STALE_S}s interval={INTERVAL_S}s")
    state = {}
    # first pass just seeds prev_* so a long first sleep isn't a 'jump'
    tick(state, do_act=False)
    while True:
        time.sleep(INTERVAL_S)
        try:
            action, reason = tick(state)
            if action not in ("ok", "grace"):
                log(f"{action}\t{reason}")
        except Exception as e:
            log(f"error\t{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
