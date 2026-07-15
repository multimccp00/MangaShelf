"""Adapter discovery + URL routing.

Ships with the API-based adapters (MangaDex) and additionally loads:
  - user-supplied Python adapters from sources/local/ (git-ignored; TRUSTED, for
    the machine owner only — arbitrary code), and
  - user-installed **declarative Sources**: JSON manifests in
    ~/.mangashelf/extensions/, interpreted by the safe DeclarativeSource engine
    (no code execution — safe for a multi-user app; see EXTENSIONS_DESIGN.md).
A missing/empty folder in either case is fine.
"""
from __future__ import annotations

import importlib
import json
import pkgutil
import threading
from pathlib import Path

from .anilist import AniListSource
from .base import MangaSource
from .mangadex import MangaDexSource

# Adapters that ship with the app (sources exposing an official public API).
# MangaDex reads pages; AniList adds English-first search + metadata and hands off
# to MangaDex for the readable chapter feed. AniList is listed FIRST so that when
# the same series is found on both, its English-titled result is the one shown
# (search_all de-dupes by title, first source wins). URL routing (find_source) is
# unaffected — each adapter only matches its own domain.
_BUILTIN: list[type[MangaSource]] = [AniListSource, MangaDexSource]

# User-installed declarative Sources live here (NOT in the repo — per install,
# same app-data dir as covers/settings). One *.json manifest per site.
EXTENSIONS_DIR = Path.home() / ".mangashelf" / "extensions"

# Guards the cached source list: FastAPI serves across a threadpool, so a request
# iterating _all() must not race an extension install/reload rebuilding it.
_sources_lock = threading.RLock()
_sources: list[MangaSource] | None = None
# Reasons manifests were skipped at last load, surfaced to the manage UI:
#   [{"file": "...", "error": "..."}]
_extension_errors: list[dict[str, str]] = []


def _load_local(into: list[MangaSource]) -> None:
    """Instantiate every MangaSource subclass found in sources/local/*.py.
    Silently no-ops if the folder is absent (the public checkout)."""
    try:
        from . import local  # noqa: WPS433 — optional, git-ignored package
    except Exception:
        return
    for info in pkgutil.iter_modules(local.__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{local.__name__}.{info.name}")
        except Exception as exc:  # a broken local adapter must not kill the app
            print(f"[sources] skipped local adapter {info.name!r}: {exc}")
            continue
        for obj in vars(mod).values():
            if isinstance(obj, type) and issubclass(obj, MangaSource) and obj is not MangaSource:
                into.append(obj())


def _is_disabled(manifest_path: Path) -> bool:
    """A manifest is disabled (kept installed but inactive) by a sibling
    <id>.json.disabled marker — so toggling doesn't lose the file."""
    return manifest_path.with_suffix(".json.disabled").exists()


def _load_extensions(into: list[MangaSource]) -> None:
    """Load every valid, enabled manifest from EXTENSIONS_DIR as a DeclarativeSource.
    A malformed/invalid/disabled manifest is skipped (recorded in _extension_errors),
    never crashing discovery — same resilience as _load_local."""
    global _extension_errors
    _extension_errors = []
    try:
        from .declarative import DeclarativeSource, ManifestError
    except Exception as exc:  # engine import failed — no extensions this run
        _extension_errors.append({"file": "<engine>", "error": str(exc)})
        return
    if not EXTENSIONS_DIR.exists():
        return
    for path in sorted(EXTENSIONS_DIR.glob("*.json")):
        if _is_disabled(path):
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            into.append(DeclarativeSource(manifest))
        except (ManifestError, ValueError, OSError) as exc:
            _extension_errors.append({"file": path.name, "error": str(exc)})
            print(f"[sources] skipped extension {path.name!r}: {exc}")


def _all() -> list[MangaSource]:
    global _sources
    # Double-checked under the lock so two threads can't both rebuild (which would
    # run _load_extensions twice, racing on _extension_errors).
    if _sources is not None:
        return _sources
    with _sources_lock:
        if _sources is not None:
            return _sources
        srcs: list[MangaSource] = [cls() for cls in _BUILTIN]
        # Declarative user extensions (safe, multi-user) come after built-ins.
        _load_extensions(srcs)
        # Private Python adapters (trusted, machine-owner only) after that.
        _load_local(srcs)
        # AI fallback goes LAST so dedicated adapters/extensions always win; it
        # only matches when a local LLM is running.
        try:
            from .ai import GenericAISource
            srcs.append(GenericAISource())
        except Exception as exc:  # noqa: BLE001
            print(f"[sources] AI adapter unavailable: {exc}")
        _sources = srcs
        return _sources


def reload() -> None:
    """Drop the cached source list so the next call rebuilds it — picks up newly
    installed/removed/toggled extensions WITHOUT a server restart."""
    global _sources
    with _sources_lock:
        _sources = None


def find_source(url: str) -> MangaSource | None:
    """Return the adapter that handles `url`, or None if none matches."""
    url = (url or "").strip()
    for s in _all():
        try:
            if s.matches(url):
                return s
        except Exception:
            continue
    return None


def list_sources() -> list[dict[str, str]]:
    """Names + labels + example URLs of all loaded adapters, for the UI.
    The AI fallback is listed only when a local LLM is actually available."""
    out = []
    for s in _all():
        if s.name == "ai":
            try:
                from .ai import llm_available
                if not llm_available():
                    continue
            except Exception:
                continue
        out.append({"name": s.name, "label": s.label, "example": getattr(s, "example", "")})
    return out


def find_by_name(name: str) -> MangaSource | None:
    for s in _all():
        if s.name == name:
            return s
    return None


_SEARCH_DEADLINE = 25   # seconds — overall cap for the whole web search

# Non-alphanumeric chars ignored when comparing a title to the query, so
# "My Dress-Up Darling" matches "my dress up darling".
_NORM_RE = None  # lazily compiled in _norm (avoids an import-time re dependency here)


def _norm(s: str) -> str:
    """Lowercase and collapse to space-separated alphanumeric words, so hyphens,
    punctuation and casing don't affect title matching."""
    global _NORM_RE
    if _NORM_RE is None:
        import re
        _NORM_RE = re.compile(r"[^a-z0-9]+")
    return _NORM_RE.sub(" ", (s or "").lower()).strip()


def _relevance(query: str, title: str, priority: int, rank: int,
               match_score: float = -1.0) -> float:
    """Score how well a hit answers `query`, used to sort combined web results
    best-first across all sources. Higher is better.

    If the source supplied `match_score` (0.0–1.0 — it ranked the hit against titles
    the display `title` may not show, e.g. an English alt title on a romaji-titled
    series), that decides the tier: it's the authoritative relevance and can't be
    second-guessed by comparing our query to a romaji string we don't understand.
    Otherwise we fall back to text tiers on the display title: exact == query, then
    prefix, then all query words present, then partial word overlap.

    `priority` (source weight) and `rank` (position in the source's own best-match
    list) only fine-tune WITHIN a tier, so a genuinely better match from a
    low-priority source still beats a weak match from a high-priority one."""
    if match_score is not None and match_score >= 0.0:
        # Map the source's 0–1 score onto the same tier scale as the text path so
        # the two ranking paths are comparable: 1.0 → ~exact (1000), scaling down.
        base = 1000.0 * match_score
    else:
        q, t = _norm(query), _norm(title)
        if not q or not t:
            base = 0.0
        elif t == q:
            base = 1000.0
        elif t.startswith(q):
            base = 800.0
        else:
            qw = q.split()
            tw = set(t.split())
            hit = sum(1 for w in qw if w in tw)
            if hit == len(qw):
                base = 600.0            # every query word present (any order)
            else:
                base = 300.0 * (hit / len(qw))   # partial overlap, scaled 0–300
    # priority nudges within a tier (bounded so it can't jump tiers); a higher rank
    # in the source's own list (rank 0 = its top hit) is a small further tie-break.
    return base + max(-20, min(20, priority)) - rank * 0.1


def search_all(query: str, limit: int = 20) -> list[dict[str, object]]:
    """Search every source that supports title search (can_search) CONCURRENTLY,
    combine the results, and return them as plain dicts for the API. Sources that
    don't search are skipped; a failing/slow one is skipped, never fatal. The whole
    fan-out is bounded by an overall deadline so one slow source can't stall the
    request thread (which serializes with the DB lock)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    query = (query or "").strip()
    if not query:
        return []
    searchable = [s for s in _all() if getattr(s, "can_search", False)]
    if not searchable:
        return []

    def _one(src):
        # rank = this hit's position in the source's own best-match-first list, so
        # we can reward a source's top results even after interleaving sources.
        return [({
            "source": r.source, "source_label": r.source_label,
            "title": r.title, "url": r.url, "author": r.author,
            "cover_url": r.cover_url, "description": r.description, "year": r.year,
            "chapter_count": getattr(r, "chapter_count", -1),
            # Kept out of the API payload (popped before return) — used only for
            # ranking; a source's own alt-title-aware score beats display-title text.
            "_match_score": getattr(r, "match_score", -1.0),
        }, rank) for rank, r in enumerate(src.search(query, limit=limit))]

    # Collect every source's hits, tagged with the source object (for its priority)
    # and the hit's in-source rank, so the final ordering can be by relevance across
    # sources rather than simply grouping one source after another.
    scored: list[tuple[float, int, dict[str, object]]] = []
    seen_urls: set[str] = set()
    with ThreadPoolExecutor(max_workers=max(1, len(searchable))) as ex:
        futs = {ex.submit(_one, s): s for s in searchable}
        try:
            for fut in as_completed(futs, timeout=_SEARCH_DEADLINE):
                s = futs[fut]
                try:
                    hits = fut.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[sources] search failed for {s.name!r}: {exc}")
                    continue
                prio = getattr(s, "priority", 0)
                for r, rank in hits:
                    # Do NOT de-dupe across sources by title: different sources carry
                    # genuinely DIFFERENT content for the "same" series — MangaDex may
                    # have 1 readable English chapter where Weeb Central has 124.
                    # Collapsing them would hide the source the user actually wants.
                    # Only drop exact-URL duplicates (a source listing a series twice).
                    u = str(r.get("url", ""))
                    if u and u in seen_urls:
                        continue
                    if u:
                        seen_urls.add(u)
                    ms = float(r.pop("_match_score", -1.0))
                    scored.append((_relevance(query, str(r.get("title", "")), prio, rank, ms), rank, r))
        except TimeoutError:
            print("[sources] web search hit the overall deadline; returning partial results")

    # Interleave all sources by relevance: the closest title match to the query
    # floats to the top no matter which source it came from, with source priority
    # only breaking ties. Higher score first; stable within equal scores. Every hit
    # keeps its per-card source badge so the user still sees (and picks) the source.
    scored.sort(key=lambda t: t[0], reverse=True)
    out: list[dict[str, object]] = [r for _score, _rank, r in scored]

    # NOTE: we do NOT fetch chapter counts here — resolving them for every result
    # is slow (MangaDex rate-limits the per-result feed calls, adding several
    # seconds), and blocking the search on it makes the whole UI feel sluggish.
    # Results carry whatever count their source supplied for free (>= 0); the rest
    # stay -1 and the frontend fills them in LAZILY via /api/chapter-count so the
    # grid appears instantly and each "· N ch" pops in as it resolves.
    return out


def chapter_count_for(source: str, url: str) -> int:
    """Readable chapter count for one result, resolved on demand (backs the lazy
    /api/chapter-count endpoint). -1 if unknown/unsupported."""
    src = find_by_name((source or "").strip())
    if not src or not (url or "").strip():
        return -1
    try:
        n = src.chapter_count(url)
    except Exception:
        return -1
    return n if isinstance(n, int) else -1


def any_searchable() -> bool:
    """True if at least one loaded source supports title search."""
    return any(getattr(s, "can_search", False) for s in _all())


# ---------------------------------------------------- extension management --
# These back the /api/sources/extensions endpoints. All validate before touching
# disk, write atomically, and reload() so changes take effect immediately.

def _ext_path(ext_id: str) -> Path:
    return EXTENSIONS_DIR / f"{ext_id}.json"


def install_extension(manifest: dict) -> dict[str, str]:
    """Validate a manifest and save it (atomically) as an installed extension.
    Returns {id, name, version}. Raises ManifestError/ValueError on a bad manifest
    so the caller can 400 with the reason. Overwrites an existing id (an update)."""
    import os
    from .declarative import validate_manifest
    m = validate_manifest(manifest)   # raises on anything invalid — nothing is written
    EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _ext_path(m["id"])
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, dest)
    reload()
    return {"id": m["id"], "name": m.get("name", m["id"]), "version": m.get("version", "")}


def remove_extension(ext_id: str) -> bool:
    """Delete an installed extension (and any disabled marker). True if removed."""
    p = _ext_path(ext_id)
    existed = p.exists()
    try:
        p.unlink(missing_ok=True)
        p.with_suffix(".json.disabled").unlink(missing_ok=True)
    except OSError:
        return False
    if existed:
        reload()
    return existed


def set_extension_enabled(ext_id: str, enabled: bool) -> bool:
    """Enable/disable without deleting the file (via a .disabled marker).
    Returns True if the extension exists."""
    p = _ext_path(ext_id)
    if not p.exists():
        return False
    marker = p.with_suffix(".json.disabled")
    try:
        if enabled:
            marker.unlink(missing_ok=True)
        elif not marker.exists():
            marker.write_text("", encoding="utf-8")
    except OSError:
        return False
    reload()
    return True


def list_extensions() -> dict[str, object]:
    """Every installed manifest (enabled or not), for the manage UI, plus the
    load errors from the last discovery pass."""
    out = []
    if EXTENSIONS_DIR.exists():
        for path in sorted(EXTENSIONS_DIR.glob("*.json")):
            enabled = not _is_disabled(path)
            info = {"file": path.name, "enabled": enabled, "valid": True, "error": ""}
            try:
                m = json.loads(path.read_text(encoding="utf-8"))
                from .declarative import validate_manifest
                validate_manifest(m)
                info.update(id=m.get("id"), name=m.get("name"), version=m.get("version"),
                            author=m.get("author", ""),
                            host=(m.get("match", {}) or {}).get("host", ""))
            except Exception as exc:  # noqa: BLE001
                info.update(valid=False, error=str(exc), id=path.stem)
            out.append(info)
    return {"extensions": out, "errors": _extension_errors}


def source_info(external_id: str) -> dict[str, str] | None:
    """Turn a stored 'external_id' ("<source>:<id>") into display info:
    {source, label, id, url}. None if it isn't a recognizable origin."""
    if not external_id or ":" not in str(external_id):
        return None
    name, _, ident = str(external_id).partition(":")
    src = find_by_name(name)
    if not src:
        # Adapter not loaded (e.g. a private one on another machine): still show it.
        return {"source": name, "label": name, "id": ident, "url": ""}
    return {"source": name, "label": src.label, "id": ident, "url": src.url_for(ident)}
