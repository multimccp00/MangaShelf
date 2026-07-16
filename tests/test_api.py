"""API-level regression tests: the /api/page path sandbox (directory-traversal
protection), the auth gate, and the combined-search relevance ranking.

web.api is imported lazily (inside fixtures/tests) so conftest's USERPROFILE
redirect is already active — its module-level Database() lands in the temp home.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def client_and_token(tmp_path_factory):
    from fastapi.testclient import TestClient
    import web.api as api

    client = TestClient(api.app)
    token = (Path.home() / ".mangashelf" / "web_token.txt").read_text().strip()

    # One on-disk library with a real page image, registered in the API's own DB.
    lib = tmp_path_factory.mktemp("library")
    page = lib / "Series" / "Chapter 0001" / "001.png"
    page.parent.mkdir(parents=True)
    page.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c626001000000ffff03000006000557bfabd4000000"
        "0049454e44ae426082"
    ))
    api._db.add_library(str(lib), name="TestLib")
    return client, token, page


# ------------------------------------------------------------------- auth --

def test_endpoints_require_token(client_and_token):
    client, _tok, _page = client_and_token
    assert client.get("/api/series").status_code == 401
    assert client.post("/api/resync-all").status_code == 401


def test_read_endpoint_accepts_token(client_and_token):
    client, tok, _page = client_and_token
    r = client.get("/api/series", headers={"X-Mangashelf-Token": tok})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ----------------------------------------------------------- path sandbox --

def test_page_serves_file_inside_library(client_and_token):
    client, tok, page = client_and_token
    r = client.get("/api/page", params={"path": str(page), "token": tok})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


@pytest.mark.parametrize("evil", [
    r"C:\Windows\System32\drivers\etc\hosts",
    r"C:\Windows\win.ini",
    "../../../../etc/passwd",
])
def test_page_refuses_paths_outside_libraries(client_and_token, evil):
    client, tok, _page = client_and_token
    r = client.get("/api/page", params={"path": evil, "token": tok})
    assert r.status_code in (400, 403, 404), f"sandbox let through: {evil} -> {r.status_code}"


def test_page_refuses_traversal_from_inside_library(client_and_token):
    """A path that STARTS inside a library but ..-escapes it must be rejected."""
    client, tok, page = client_and_token
    evil = str(page.parent) + r"\..\..\..\..\Windows\win.ini"
    r = client.get("/api/page", params={"path": evil, "token": tok})
    assert r.status_code in (400, 403, 404)


# ------------------------------------------------------ relevance ranking --

def test_relevance_exact_beats_prefix_beats_partial():
    from sources.registry import _relevance
    q = "my dress up darling"
    exact = _relevance(q, "My Dress-Up Darling", 0, 0)
    prefix = _relevance(q, "My Dress-Up Darling XOXO!", 0, 0)
    partial = _relevance(q, "My Darling Next Door", 0, 0)
    assert exact > prefix > partial


def test_relevance_priority_breaks_ties_but_never_jumps_tiers():
    from sources.registry import _relevance
    q = "berserk"
    low = _relevance(q, "Berserk", -5, 0)
    high = _relevance(q, "Berserk", 10, 0)
    weaker_high = _relevance(q, "Berserk of Gluttony", 10, 0)
    assert high > low                 # priority orders equals
    assert low > weaker_high          # but can't lift a weaker match over an exact one


def test_relevance_match_score_overrides_display_title():
    from sources.registry import _relevance
    # Romaji display title, but the source says it's a perfect match (alt title).
    scored = _relevance("my dress up darling", "Sono Bisque Doll wa Koi o Suru", 0, 0, match_score=1.0)
    unscored = _relevance("my dress up darling", "Sono Bisque Doll wa Koi o Suru", 0, 0)
    assert scored > unscored
    exact_text = _relevance("my dress up darling", "My Dress-Up Darling", 0, 0)
    assert abs(scored - exact_text) < 50   # same tier as a textual exact match


def test_norm_strips_punctuation_and_case():
    from sources.registry import _norm
    assert _norm("My Dress-Up Darling!") == "my dress up darling"
