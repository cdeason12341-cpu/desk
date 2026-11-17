"""DECISION 59: catch-up night rollover (owner approved 2026-11-16 7:21 PM CT). The old loop only rolled during the
exact 08:00 minute, so a box paused at 8 AM never rolled (no rollover from 2026-11-14 4:40 PM CT to 11-16)."""
import datetime as dt, json, time
import pytest
import report
from main import Desk
from tests.helpers import run


def _desk(tmp_path):
    d = Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    d._allow_respawn = False
    return d


def _age_night(d, started: dt.datetime):
    """Make the current night an older one (own id, given start)."""
    nid = "night-" + started.strftime("%Y%m%d%H%M")
    d.db.x("UPDATE nights SET started_at=?, id=? WHERE id=?", (started.timestamp(), nid, d.ctx.night))
    d.db.set_meta("night", nid)
    d.ctx.night = nid


@pytest.fixture(autouse=True)
def _no_report_files(monkeypatch):
    """close_night writes logs/reports/<night>.json under the real desk dir: keep tests out of it."""
    def close(db, night):
        s = report.summary(db, night)
        db.x("UPDATE nights SET ended_at=? WHERE id=?", (time.time(), night))
        return s
    monkeypatch.setattr(report, "close_night", close)


def test_missed_8am_rolls_once_on_catch_up(tmp_path):
    d = _desk(tmp_path)
    now = dt.datetime.now().replace(hour=19, minute=25, second=0, microsecond=0)
    _age_night(d, now - dt.timedelta(days=2, hours=3))          # long night from two days ago (Nov 14 style)
    d.db.set_meta("last_rollover", (now - dt.timedelta(days=2)).date().isoformat())
    old = d.ctx.night
    assert d.rollover_due(now)
    assert run(d.rollover_if_due(now)) is True
    assert d.ctx.night != old and d.db.meta("night") == d.ctx.night
    assert d.db.one("SELECT ended_at FROM nights WHERE id=?", (old,))["ended_at"] is not None
    assert d.db.meta("last_rollover") == now.date().isoformat()
    new = d.ctx.night
    for k in range(5):                                          # every later tick today: no second roll
        assert run(d.rollover_if_due(now + dt.timedelta(minutes=k * 30))) is False
    assert d.ctx.night == new
    assert d.db.one("SELECT COUNT(*) n FROM nights")["n"] == 2   # history kept


def test_not_before_report_time_and_rolls_after(tmp_path):
    d = _desk(tmp_path)
    day = dt.datetime.now().replace(second=0, microsecond=0)
    _age_night(d, day.replace(hour=19, minute=0) - dt.timedelta(days=1))
    d.db.set_meta("last_rollover", (day - dt.timedelta(days=1)).date().isoformat())
    assert not d.rollover_due(day.replace(hour=7, minute=59))
    assert d.rollover_due(day.replace(hour=8, minute=0))
    assert d.rollover_due(day.replace(hour=11, minute=40))      # box woke late: still catches up


def test_fresh_start_after_report_time_does_not_roll(tmp_path):
    d = _desk(tmp_path)
    now = dt.datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    _age_night(d, now.replace(hour=9))                          # night began today after 08:00
    old = d.ctx.night
    assert run(d.rollover_if_due(now)) is False
    assert d.ctx.night == old and d.db.meta("last_rollover") == now.date().isoformat()


def test_close_night_failure_still_rolls_once(tmp_path, monkeypatch):
    d = _desk(tmp_path)
    now = dt.datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    _age_night(d, now - dt.timedelta(days=1))
    old = d.ctx.night
    monkeypatch.setattr(report, "close_night", lambda db, n: (_ for _ in ()).throw(RuntimeError("boom")))
    assert run(d.rollover_if_due(now)) is True
    assert d.db.one("SELECT ended_at FROM nights WHERE id=?", (old,))["ended_at"] is not None
    assert run(d.rollover_if_due(now + dt.timedelta(minutes=1))) is False


def test_report_default_night_handles_8am_race(tmp_path):
    d = _desk(tmp_path)
    s, db = d.settings, d.db
    now = dt.datetime.now().replace(hour=8, minute=3, second=0, microsecond=0)
    db.x("INSERT INTO nights(id,started_at,ended_at,mode,start_balance) VALUES('older',?,?,?,200)",
         ((now - dt.timedelta(days=3)).timestamp(), (now - dt.timedelta(days=1)).timestamp(), "PAPER"))
    _age_night(d, now - dt.timedelta(hours=13))                 # last night, still open: desk hasn't rolled yet
    db.set_meta("last_rollover", (now - dt.timedelta(days=1)).date().isoformat())
    assert report.default_night(db, s, now) == d.ctx.night      # not the older closed night (no re-report)
    last = d.ctx.night
    assert run(d.rollover_if_due(now)) is True
    assert report.default_night(db, s, now + dt.timedelta(minutes=2)) == last   # after the roll: same night
    assert report.default_night(db, s, now.replace(hour=7)) == last
