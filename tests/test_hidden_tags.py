"""Hidden-tag filtering: series carrying a user-chosen tag/genre drop out of every
listing, while the rest are untouched. Uses neutral dummy tag names only."""
from __future__ import annotations


def _mk(db, lib_id, tmp_path, name, genres, tags):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    sid = db.upsert_series(lib_id, str(folder), name)
    db.update_series_metadata(sid, name, "", "", "Reading", 0, False, "", genres, tags)
    return sid


def test_exclude_terms_hide_by_tag_or_genre_case_insensitive(db, tmp_path):
    lib = db.add_library(str(tmp_path), name="L")
    a = _mk(db, lib, tmp_path, "A", ["GenreX"], ["plain"])
    b = _mk(db, lib, tmp_path, "B", [], ["Hide-Me"])
    c = _mk(db, lib, tmp_path, "C", ["hide-genre"], [])

    ids = lambda rows: {r["id"] for r in rows}
    assert ids(db.get_series_list()) == {a, b, c}
    assert ids(db.get_series_list(exclude_terms=["hide-me"])) == {a, c}              # tag, any case
    assert ids(db.get_series_list(exclude_terms=["HIDE-GENRE"])) == {a, b}           # genre
    assert ids(db.get_series_list(exclude_terms=["hide-me", "hide-genre"])) == {a}
    assert ids(db.get_series_list(exclude_terms=["", "  "])) == {a, b, c}            # blanks ignored


def test_hidden_series_leave_profile_shelf_and_counts(db, tmp_path):
    lib = db.add_library(str(tmp_path), name="L")
    _mk(db, lib, tmp_path, "A", [], ["plain"])
    _mk(db, lib, tmp_path, "B", [], ["hide-me"])

    assert {r["title"] for r in db.recent_series(["Reading"], 10)} == {"A", "B"}
    assert {r["title"] for r in db.recent_series(["Reading"], 10, hide_terms=["hide-me"])} == {"A"}
    assert db.reading_stats(hide_terms=["hide-me"])[0]["series"] == 1
    assert db.reading_stats_by_library(hide_terms=["hide-me"])[0]["series"] == 1
