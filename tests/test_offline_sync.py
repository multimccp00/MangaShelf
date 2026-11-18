"""Offline-queue replay rules: newest wins, finish still completes, order preserved."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _iso(delta_s: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


def _sid(db, tmp_path) -> int:
    lib = tmp_path / "lib"
    (lib / "S").mkdir(parents=True)
    return db.upsert_series(db.add_library(str(lib), name="L"), str(lib / "S"), "S")


def _row(db, sid):
    return dict(db.conn.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone())


def test_stale_progress_is_dropped_and_newer_applied(db, tmp_path):
    sid = _sid(db, tmp_path)
    assert db.update_last_read_progress(sid, "Ch1", 5, 10)           # online write, now
    # Offline write made an hour ago must not overwrite the newer position.
    assert db.update_last_read_progress(sid, "Ch1", 2, 10, _iso(-3600)) is False
    assert _row(db, sid)["last_page"] == 5
    # A genuinely newer offline write applies and records the device.
    assert db.update_last_read_progress(sid, "Ch2", 7, 10, _iso(+1), "Phone")
    r = _row(db, sid)
    assert (r["last_chapter"], r["last_page"], r["last_device"]) == ("Ch2", 7, "Phone")


def test_synced_finish_completes_and_reread_replays_in_order(db, tmp_path):
    sid = _sid(db, tmp_path)
    old = _iso(-7200)
    db.set_status(sid, "Reading", old)
    db.update_last_read_progress(sid, "Ch1", 10, 10, _iso(-7100))     # crosses last page
    assert _row(db, sid)["status"] == "Completed"
    # Re-read afterwards: back to page 3, replayed after the finish -> ends there.
    db.update_last_read_progress(sid, "Ch1", 3, 10, _iso(-7000))
    r = _row(db, sid)
    assert r["last_page"] == 3 and r["status"] == "Completed" and r["times_completed"] == 1


def test_stale_status_ignored(db, tmp_path):
    sid = _sid(db, tmp_path)
    db.update_last_read_progress(sid, "Ch1", 1, 10)
    assert db.set_status(sid, "Dropped", _iso(-3600)) is False
    assert _row(db, sid)["status"] == "Reading"


def test_future_timestamp_clamped(db, tmp_path):
    sid = _sid(db, tmp_path)
    db.update_last_read_progress(sid, "Ch1", 1, 10, _iso(+86400))
    # Clamped to ~now, so a normal write right after is not considered stale.
    assert db.update_last_read_progress(sid, "Ch1", 2, 10, _iso(+10)) is True
