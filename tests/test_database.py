"""Database-layer regression tests for the code paths that can lose data.

Everything runs against a throwaway DB file (see conftest) — the real library
database is unreachable from here by construction.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def _mk_series(db, lib_id: int, folder: Path, title: str) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    sid = db.upsert_series(lib_id, str(folder), title)
    return sid


# --------------------------------------------------------------- libraries --

def test_remove_library_deletes_records_not_files(db, tmp_path):
    lib = tmp_path / "libA"
    lib_id = db.add_library(str(lib), name="A")
    s1 = _mk_series(db, lib_id, lib / "S1", "S1")
    s2 = _mk_series(db, lib_id, lib / "S2", "S2")
    (lib / "S1" / "page.png").write_bytes(b"x")
    db.set_tags(s1, ["tagA"]) if hasattr(db, "set_tags") else db.conn.execute(
        "INSERT INTO tags(series_id, tag) VALUES (?, 'tagA')", (s1,))
    db.conn.commit()

    removed = db.remove_library(lib_id)

    assert removed == 2
    assert db.conn.execute("SELECT COUNT(*) FROM series WHERE library_id=?", (lib_id,)).fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM libraries WHERE id=?", (lib_id,)).fetchone()[0] == 0
    # No orphaned children left behind.
    assert db.conn.execute(
        "SELECT COUNT(*) FROM tags WHERE series_id IN (?, ?)", (s1, s2)).fetchone()[0] == 0
    # Files on disk untouched.
    assert (lib / "S1" / "page.png").exists()


def test_remove_library_leaves_other_libraries_alone(db, tmp_path):
    a = db.add_library(str(tmp_path / "A"), name="A")
    b = db.add_library(str(tmp_path / "B"), name="B")
    sa = _mk_series(db, a, tmp_path / "A" / "S", "SA")
    sb = _mk_series(db, b, tmp_path / "B" / "S", "SB")

    db.remove_library(a)

    assert db.conn.execute("SELECT COUNT(*) FROM libraries").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM series WHERE id=?", (sb,)).fetchone()[0] == 1


# ------------------------------------------------------------- purge guard --

def test_purge_flags_missing_series_when_root_reachable(db, tmp_path):
    lib = tmp_path / "lib"
    lib_id = db.add_library(str(lib), name="L")
    gone = _mk_series(db, lib_id, lib / "Gone", "Gone")
    stays = _mk_series(db, lib_id, lib / "Stays", "Stays")
    shutil.rmtree(lib / "Gone")

    flagged = db.purge_missing_series()

    assert flagged == 1
    row = db.conn.execute("SELECT available FROM series WHERE id=?", (gone,)).fetchone()
    assert int(row["available"]) == 0
    # Soft delete only — the row (metadata) is still there.
    assert db.conn.execute("SELECT COUNT(*) FROM series WHERE id=?", (gone,)).fetchone()[0] == 1
    assert int(db.conn.execute("SELECT available FROM series WHERE id=?", (stays,)).fetchone()["available"]) == 1


def test_purge_never_flags_when_whole_drive_offline(db, tmp_path):
    """THE critical guard: an offline/unmounted library root must not flag its
    series. A regression here wipes availability for entire drives on a blip."""
    lib = tmp_path / "lib"
    lib_id = db.add_library(str(lib), name="L")
    sid = _mk_series(db, lib_id, lib / "S", "S")
    # Simulate the DRIVE going away: remove the library root entirely.
    shutil.rmtree(lib)

    flagged = db.purge_missing_series()

    assert flagged == 0
    assert int(db.conn.execute("SELECT available FROM series WHERE id=?", (sid,)).fetchone()["available"]) == 1


def test_purge_restores_availability_when_folder_returns(db, tmp_path):
    lib = tmp_path / "lib"
    lib_id = db.add_library(str(lib), name="L")
    sid = _mk_series(db, lib_id, lib / "S", "S")
    shutil.rmtree(lib / "S")
    db.purge_missing_series()
    assert int(db.conn.execute("SELECT available FROM series WHERE id=?", (sid,)).fetchone()["available"]) == 0

    (lib / "S").mkdir()
    db.purge_missing_series()
    assert int(db.conn.execute("SELECT available FROM series WHERE id=?", (sid,)).fetchone()["available"]) == 1


# ------------------------------------------------------------- fresh flag --

def test_fresh_chapters_set_and_cleared_by_progress(db, tmp_path):
    lib_id = db.add_library(str(tmp_path / "lib"), name="L")
    sid = _mk_series(db, lib_id, tmp_path / "lib" / "S", "S")

    db.set_fresh_chapters(sid, True)
    assert int(db.conn.execute("SELECT fresh_chapters FROM series WHERE id=?", (sid,)).fetchone()[0]) == 1

    # Any progress save serves the highlight.
    db.update_last_read_progress(sid, "Chapter 0001", 5)
    assert int(db.conn.execute("SELECT fresh_chapters FROM series WHERE id=?", (sid,)).fetchone()[0]) == 0


# ------------------------------------------------------------------ prune --

def test_prune_only_removes_rows_outside_valid_set(db, tmp_path):
    lib = tmp_path / "lib"
    lib_id = db.add_library(str(lib), name="L")
    keep = _mk_series(db, lib_id, lib / "Keep", "Keep")
    stale = _mk_series(db, lib_id, lib / "Stale", "Stale")

    removed = db.prune_stale_series(lib_id, {str(lib / "Keep")})

    assert removed == 1
    assert db.conn.execute("SELECT COUNT(*) FROM series WHERE id=?", (keep,)).fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM series WHERE id=?", (stale,)).fetchone()[0] == 0


# ------------------------------------------------------------ server search --

def test_search_matches_all_haystack_fields(db, tmp_path):
    lib = tmp_path / "lib"
    lib_id = db.add_library(str(lib), name="L")
    sid = _mk_series(db, lib_id, lib / "S", "Solo Leveling")
    db.conn.execute("UPDATE series SET author='Chugong', notes='great art', language='EN' WHERE id=?", (sid,))
    db.conn.execute("INSERT INTO tags(series_id, tag) VALUES (?, 'manhwa')", (sid,))
    db.conn.commit()
    other = _mk_series(db, lib_id, lib / "T", "Totally Different")

    def ids(q):
        return {r["id"] for r in db.get_series_list(search=q, library_id=lib_id)}

    assert ids("solo") == {sid}          # title
    assert ids("chugong") == {sid}       # author
    assert ids("manhwa") == {sid}        # tag
    assert ids("great art") == {sid}     # notes, multi-word AND
    assert ids("solo different") == set()  # AND across words narrows to nothing
    assert ids("") >= {sid, other}       # empty = everything
