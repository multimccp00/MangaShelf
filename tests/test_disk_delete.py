"""Delete-series with ?disk=true: recycle-bin deletion, sandbox enforcement.

send2trash is monkeypatched — tests must not fill the real recycle bin.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def client_and_token():
    from fastapi.testclient import TestClient
    import api
    client = TestClient(api.app)
    token = (Path.home() / ".mangashelf" / "web_token.txt").read_text().strip()
    return client, token, api


def _mk_series(api, root: Path, name: str) -> int:
    lib_id = api._db.add_library(str(root), name=root.name)
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "001.png").write_bytes(b"x")
    return api._db.upsert_series(lib_id, str(folder), name)


def test_delete_without_disk_keeps_files(client_and_token, tmp_path, monkeypatch):
    client, tok, api = client_and_token
    sid = _mk_series(api, tmp_path / "libK", "Keep")
    r = client.delete(f"/api/series/{sid}", headers={"X-Mangashelf-Token": tok})
    assert r.status_code == 200 and r.json()["disk"] == "kept"
    assert (tmp_path / "libK" / "Keep" / "001.png").exists()


def test_delete_with_disk_recycles_folder(client_and_token, tmp_path, monkeypatch):
    client, tok, api = client_and_token
    sid = _mk_series(api, tmp_path / "libR", "Recycled")
    sent = []
    import send2trash
    monkeypatch.setattr(send2trash, "send2trash", lambda p: sent.append(p))

    r = client.delete(f"/api/series/{sid}", params={"disk": "true"},
                      headers={"X-Mangashelf-Token": tok})

    assert r.status_code == 200 and r.json()["disk"] == "recycled"
    assert sent == [str((tmp_path / "libR" / "Recycled").resolve())]
    # DB record gone.
    assert api._db.get_series_by_id(sid) is None


def test_delete_with_disk_refuses_folder_outside_libraries(client_and_token, tmp_path, monkeypatch):
    """A corrupted/tampered folder_path outside every library root must be
    refused — same sandbox rule as /api/page. The DB row must survive too."""
    client, tok, api = client_and_token
    lib_id = api._db.add_library(str(tmp_path / "libS"), name="libS")
    (tmp_path / "libS").mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "Evil"
    outside.mkdir(parents=True)
    sid = api._db.upsert_series(lib_id, str(outside), "Evil")
    called = []
    import send2trash
    monkeypatch.setattr(send2trash, "send2trash", lambda p: called.append(p))

    r = client.delete(f"/api/series/{sid}", params={"disk": "true"},
                      headers={"X-Mangashelf-Token": tok})

    assert r.status_code == 400
    assert called == []
    assert api._db.get_series_by_id(sid) is not None   # row untouched on refusal


def test_delete_with_disk_when_folder_already_gone(client_and_token, tmp_path):
    client, tok, api = client_and_token
    root = tmp_path / "libG"
    sid = _mk_series(api, root, "Ghost")
    import shutil
    shutil.rmtree(root / "Ghost")

    r = client.delete(f"/api/series/{sid}", params={"disk": "true"},
                      headers={"X-Mangashelf-Token": tok})

    assert r.status_code == 200 and r.json()["disk"] == "already-gone"
