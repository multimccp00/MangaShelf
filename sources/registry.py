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
from pathlib import Path

from .base import MangaSource
from .mangadex import MangaDexSource

# Adapters that ship with the app (sources exposing an official public API).
_BUILTIN: list[type[MangaSource]] = [MangaDexSource]

# User-installed declarative Sources live here (NOT in the repo — per install,
# same app-data dir as covers/settings). One *.json manifest per site.
EXTENSIONS_DIR = Path.home() / ".mangashelf" / "extensions"

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
    if _sources is None:
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


def search_all(query: str, limit: int = 20) -> list[dict[str, object]]:
    """Search every source that supports title search (can_search), combine the
    results, and return them as plain dicts for the API. Sources that don't search
    (most of them) are skipped. A failing source is skipped, never fatal."""
    query = (query or "").strip()
    if not query:
        return []
    out: list[dict[str, object]] = []
    for s in _all():
        if not getattr(s, "can_search", False):
            continue
        try:
            for r in s.search(query, limit=limit):
                out.append({
                    "source": r.source, "source_label": r.source_label,
                    "title": r.title, "url": r.url, "author": r.author,
                    "cover_url": r.cover_url, "description": r.description,
                    "year": r.year,
                })
        except Exception as exc:  # noqa: BLE001
            print(f"[sources] search failed for {s.name!r}: {exc}")
    return out


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
