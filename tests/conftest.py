"""Shared test fixtures.

CRITICAL ISOLATION RULE: the app derives every data path (DB, settings, tokens,
job queue, extensions) from Path.home()/.mangashelf. On Windows Path.home() reads
USERPROFILE, so we point USERPROFILE at a temp dir BEFORE any app module is
imported — every test runs against a throwaway data dir and can never touch the
real library DB. Do not import app modules at the top of test files; use the
fixtures (or import inside tests) so the redirect is already in place.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# --- USERPROFILE redirect: happens at conftest import time, before app imports --
_FAKE_HOME = Path(tempfile.mkdtemp(prefix="mangashelf-test-home-"))
os.environ["USERPROFILE"] = str(_FAKE_HOME)
os.environ["HOME"] = str(_FAKE_HOME)          # POSIX fallback, harmless on Windows
(_FAKE_HOME / ".mangashelf").mkdir(parents=True, exist_ok=True)

# Make the project root + web/ importable, mirroring how server.py sets it up.
_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT), str(_ROOT / "web")):
    if p not in sys.path:
        sys.path.insert(0, p)

# Sanity guard: refuse to run if the redirect didn't take (protects the real DB).
assert str(Path.home()).startswith(str(_FAKE_HOME)), "USERPROFILE redirect failed"


@pytest.fixture()
def db():
    """A fresh Database on a throwaway file (new one per test)."""
    import database as database_mod
    dbfile = _FAKE_HOME / ".mangashelf" / f"test-{os.urandom(4).hex()}.db"
    orig = database_mod.DB_PATH
    database_mod.DB_PATH = dbfile
    d = database_mod.Database()
    yield d
    d.conn.close()
    database_mod.DB_PATH = orig


@pytest.fixture()
def tmp_library(tmp_path):
    """An on-disk library folder with two small fake series."""
    lib = tmp_path / "library"
    for series, chapters in (("Alpha", 2), ("Beta", 1)):
        for ch in range(1, chapters + 1):
            d = lib / series / f"Chapter {ch:04d}"
            d.mkdir(parents=True)
            # 1x1 PNG (smallest valid) — scanner only needs image files to exist.
            (d / "001.png").write_bytes(bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                "890000000d49444154789c626001000000ffff03000006000557bfabd4000000"
                "0049454e44ae426082"
            ))
    return lib
