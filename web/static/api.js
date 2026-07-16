/* global window */
/* api.js — replaces the prototype's data.js.
 *
 * Instead of a hardcoded COVERS array, this loads the real library from the
 * FastAPI backend and normalizes each DB row into the shape the prototype
 * screens expect (title, author, pages, read, status, tags, genres, series,
 * color/kanji fallbacks, plus cover/page URL helpers).
 */

const API = "/api";

// ---- normalization ---------------------------------------------------------

// Status → accent color, from the design palette.
// MAL-matched status colors
const STATUS_COLOR = {
  Reading: "#2db039",
  Completed: "#26448f",
  "On Hold": "#f1c83b",
  Dropped: "#a12f31",
  "Not Started": "#c3c3c3",
  "Planned to Read": "#9b59b6",
};

// Deterministic hue from a string so fallback covers are stable per series.
function hashHue(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
  return h % 360;
}

// First 1–2 "word-ish" chars for the fallback glyph (works for latin or CJK).
function glyphFor(title) {
  const t = (title || "").trim();
  if (!t) return "無";
  // Prefer CJK characters if present.
  const cjk = t.match(/[　-鿿]/g);
  if (cjk) return cjk.slice(0, 2).join("");
  return t.slice(0, 1).toUpperCase();
}

// Turn a raw DB row (from /api/series) into the item shape screens consume.
function normalize(row) {
  const total = Number(row.total_pages ?? row.pages ?? 0);
  const read = Number(row.last_page ?? row.read ?? 0);
  const status = row.status || "Not Started";
  const hue = hashHue(String(row.title || row.id));
  return {
    id: row.id,
    library_id: row.library_id != null ? row.library_id : null,
    title: row.title || "Untitled",
    author: row.author || "",
    series: row.series_name || row.series || "",
    status,
    rating: row.rating || 0,
    favorite: !!row.favorite,
    // available !== 0 → present on disk; 0 → folder currently missing (soft-deleted).
    available: row.available === undefined ? true : !!row.available,
    tags: row.tags || [],
    genres: row.genres || [],
    parodies: row.parodies || [],
    notes: row.notes || "",
    language: row.language || "",
    folder_path: row.folder_path || "",
    last_chapter: row.last_chapter || "",
    date_added: row.date_added || "",
    last_read: row.last_read || "",
    pages: total,
    read: Math.min(read, total || read),
    // Chapter info for the card. Multi-chapter series show "chapters read / total";
    // single-chapter ones fall back to pages. May be undefined until the server's
    // background warm-up has scanned the folder (then it appears on the next load).
    chapter_count: row.chapter_count != null ? Number(row.chapter_count) : null,
    chapters_read: row.chapters_read != null ? Number(row.chapters_read) : null,
    // fallback-art fields the prototype CSS/components reference
    hue,
    color: STATUS_COLOR[status] || "#6aa6ff",
    kanji: glyphFor(row.title),
    // real cover from disk (cache-busted per id so it refreshes on rescan)
    coverUrl: `${API}/series/${row.id}/cover`,
    // Server-provided cover version (custom-cover mtime). Baked into the cover
    // URL so a new/removed custom cover defeats the week-long browser cache.
    cover_v: row.cover_v || 0,
    // chapters are loaded lazily on the detail screen
    chapters: row.chapters || null,
    total_pages: total,
    // where this series was imported from ({source,label,id,url}), or null
    origin: row.origin || null,
    // re-sync found chapters newer than a caught-up read position (drives the
    // Continue Reading priority + "NEW CH" badge; cleared on next progress save)
    fresh_chapters: !!row.fresh_chapters,
  };
}

// The auth token comes from one of three places, in order:
//  - window.MANGASHELF_TOKEN — injected by server.py when NO password is set.
//  - localStorage — "remember me" was checked at login: persists across browser
//    restarts on this device until you log out or clear it (trusted device).
//  - sessionStorage — "remember me" was NOT checked: lives only for this browser
//    session, so closing the browser logs the device out.
const _TOKEN_KEY = "mangashelf_token";
function authToken() {
  if (window.MANGASHELF_TOKEN) return window.MANGASHELF_TOKEN;
  try {
    return localStorage.getItem(_TOKEN_KEY)
      || sessionStorage.getItem(_TOKEN_KEY)
      || "";
  } catch (e) { return ""; }
}
// remember=true → trusted device (localStorage); false → this session only
// (sessionStorage). Always clears the other store so the two never disagree.
function setAuthToken(tok, remember) {
  window.MANGASHELF_TOKEN = tok || "";
  try {
    localStorage.removeItem(_TOKEN_KEY);
    sessionStorage.removeItem(_TOKEN_KEY);
    if (tok) (remember ? localStorage : sessionStorage).setItem(_TOKEN_KEY, tok);
  } catch (e) {}
}
function clearAuthToken() {
  window.MANGASHELF_TOKEN = "";
  try { localStorage.removeItem(_TOKEN_KEY); sessionStorage.removeItem(_TOKEN_KEY); } catch (e) {}
}

// <img> tags can't send headers, so image endpoints take the token as a query
// param. Returns "&token=…" or "?token=…" depending on whether the URL already
// has a query string. Empty when no token (localhost dev with auth disabled).
function tokenParam(hasQuery) {
  const tok = authToken();
  if (!tok) return "";
  return `${hasQuery ? "&" : "?"}token=${encodeURIComponent(tok)}`;
}

// Per-series cover cache-bust version. Bumped after a cover upload/remove so the
// <img> URL changes and the browser re-fetches instead of showing the stale one.
const _coverVersions = {};
function bumpCover(id) { _coverVersions[id] = Date.now(); }

// Cover URL helper kept for API-compat with prototype call sites.
function coverUrl(item) {
  const base = item.coverUrl || `${API}/series/${item.id}/cover`;
  let url = base + tokenParam(base.includes("?"));
  // Durable version from the server (custom-cover mtime) — changes the URL the
  // moment a custom cover is set/removed, so the week-long browser cache never
  // serves a stale cover after a change. Survives page reloads (unlike _coverVersions).
  if (item.cover_v) url += `&v=${item.cover_v}`;
  // In-session bump: forces an immediate refetch right after upload/remove,
  // before the series list is re-fetched with the new cover_v.
  const v = _coverVersions[item.id];
  if (v) url += `&cb=${v}`;
  return url;
}

// Page image URL for the reader. Pass thumb=true for a small, fast thumbnail
// (chapter-card grid) instead of the full multi-MB reading page.
function pageUrl(absPath, thumb) {
  return `${API}/page?path=${encodeURIComponent(absPath)}${thumb ? "&thumb=1" : ""}${tokenParam(true)}`;
}

// The progress label shown on a card. Rules (per user spec):
//  - Multi-chapter series (chapter_count > 1): "<read> / <total> ch", or just
//    "<total> ch" when not started. ("ch" everywhere for consistency.)
//  - Single-chapter series (or chapter_count == 1): fall back to pages —
//    "Pg. <read>/<total>", or "Pg. <total>" when not started but we know the count.
//  - Unknown yet (warm-up hasn't scanned): fall back to whatever page info we have.
function progressLabel(item) {
  const ch = item.chapter_count;
  if (ch != null && ch > 1) {
    const read = item.chapters_read || 0;
    if (read > 0) return `${read} / ${ch} ch`;
    return `${ch} ch`;
  }
  // Single chapter or chapter count not known yet → pages.
  if (item.pages) return `Pg. ${item.read}/${item.pages}`;
  if (item.read) return `Pg. ${item.read}`;
  return "";
}

// ---- fetch helpers ----------------------------------------------------------

// Per-install write-auth token, injected into the page <head> by server.py.
// Attached to mutating requests; mutating endpoints reject without it.
function authHeaders(base) {
  const h = base || {};
  const tok = authToken();
  if (tok) h["X-Mangashelf-Token"] = tok;
  return h;
}

// A 401 on an authenticated call means our stored token is stale/invalid (e.g. a
// password was set after we logged in, or the token rotated). Clear it and boot
// to the login screen instead of leaving the app half-broken (grid from cache but
// details failing to load). Guard so we only redirect once.
let _handling401 = false;
function handleAuthFailure() {
  if (_handling401) return;
  _handling401 = true;
  try { clearAuthToken(); } catch (e) {}
  // Reload — with no token + password required, the app boots to the login gate.
  try { window.location.reload(); } catch (e) {}
}

async function getJSON(url) {
  const res = await fetch(url, { headers: authHeaders() });
  if (res.status === 401) { handleAuthFailure(); throw new Error(`401 ${url}`); }
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (res.status === 401) { handleAuthFailure(); throw new Error("401"); }
  if (!res.ok) {
    // Surface the server's error detail (FastAPI puts it in {detail}) so the UI
    // can show "folder already exists" etc. instead of a bare status code.
    let detail = `${res.status}`;
    try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

// ---- active library + local settings -----------------------------------
// The active library scopes every series/facets fetch. Settings persist in
// localStorage (per device); library names/flags/default live server-side.
const SETTINGS_KEY = "mangashelf_settings";
function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? (JSON.parse(raw) || {}) : {};
  } catch (e) {
    console.warn("[mangashelf] could not read settings:", e);
    return {};
  }
}
function saveSettings(s) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  } catch (e) {
    // Surface it instead of silently dropping — this is the usual reason
    // "settings don't persist" (storage disabled/full/blocked).
    console.warn("[mangashelf] could not save settings:", e);
  }
}
// Settings live on the SERVER (durable + synced across devices); localStorage is
// just a fast cache so the first paint has the right values before the server
// responds. Seed from cache, then loadFromServer() overwrites with the truth.
const Settings = {
  startMode: loadSettings().startMode || "default",   // "default" | "last"
  confirmPrivate: loadSettings().confirmPrivate !== false,  // default on
  showSwitcher: loadSettings().showSwitcher !== false,      // default on
  hidePrivate: loadSettings().hidePrivate === true,         // default off
  deleteFromDisk: loadSettings().deleteFromDisk === true,   // default off
  lastLibraryId: loadSettings().lastLibraryId ?? null,

  _snapshot() {
    return {
      startMode: this.startMode,
      confirmPrivate: this.confirmPrivate,
      showSwitcher: this.showSwitcher,
      hidePrivate: this.hidePrivate,
      deleteFromDisk: this.deleteFromDisk,
      lastLibraryId: this.lastLibraryId,
    };
  },
  // Apply a patch locally, cache it, and persist to the server (fire-and-forget).
  set(patch) {
    Object.assign(this, patch);
    saveSettings(this._snapshot());          // local cache
    try {
      fetch(`${API}/settings`, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(patch),
      }).catch((e) => console.warn("[mangashelf] settings save failed:", e));
    } catch (e) { console.warn("[mangashelf] settings save failed:", e); }
  },
  // Pull authoritative settings from the server at startup.
  async loadFromServer() {
    try {
      const s = await getJSON(`${API}/settings`);
      if (s && typeof s === "object") {
        if (s.startMode != null) this.startMode = s.startMode;
        if (s.confirmPrivate != null) this.confirmPrivate = !!s.confirmPrivate;
        if (s.showSwitcher != null) this.showSwitcher = !!s.showSwitcher;
        if (s.hidePrivate != null) this.hidePrivate = !!s.hidePrivate;
        if (s.deleteFromDisk != null) this.deleteFromDisk = !!s.deleteFromDisk;
        if (s.lastLibraryId !== undefined) this.lastLibraryId = s.lastLibraryId;
        saveSettings(this._snapshot());       // refresh the local cache
      }
    } catch (e) {
      console.warn("[mangashelf] settings load failed (using cache):", e);
    }
  },
};

let _activeLibraryId = null;
function getActiveLibrary() { return _activeLibraryId; }
function setActiveLibrary(id) {
  _activeLibraryId = id == null ? null : Number(id);
  Settings.set({ lastLibraryId: _activeLibraryId });
}

const ApiClient = {
  // --- auth ---
  // Is a password gate configured? Callable before the user has any credential.
  authStatus() {
    return fetch(`${API}/auth-status`).then((r) => r.json());
  },
  // Exchange a password for the API token; stores it on success. remember=true
  // keeps this device trusted across browser restarts; false = this session only.
  async login(password, remember) {
    const res = await fetch(`${API}/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) {
      let detail = `${res.status}`;
      try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
      throw new Error(detail);
    }
    const j = await res.json();
    if (j && j.token) setAuthToken(j.token, !!remember);
    return j;
  },
  // Log out of THIS device: forget the stored token. The next load shows login.
  logout() { clearAuthToken(); },
  // Set / change / clear the app password (requires being logged in).
  setPassword(current, next) {
    return postJSON(`${API}/password`, { current: current || null, new: next || "" });
  },
  async listSeries(params = {}) {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v != null && v !== "") qs.set(k, v);
    });
    if (_activeLibraryId != null) qs.set("library", _activeLibraryId);
    const rows = await getJSON(`${API}/series?${qs.toString()}`);
    return rows.map(normalize);
  },
  async getSeries(id) {
    return normalize(await getJSON(`${API}/series/${id}`));
  },
  async getChapters(id) {
    return getJSON(`${API}/series/${id}/chapters`);
  },
  async getFacets() {
    const q = _activeLibraryId != null ? `?library=${_activeLibraryId}` : "";
    return getJSON(`${API}/facets${q}`);
  },
  // --- library management ---
  updateLibrary(id, fields) {
    return postJSON(`${API}/libraries/${id}`, fields);
  },
  setDefaultLibrary(id) {
    return postJSON(`${API}/libraries/${id}/default`, {});
  },
  // Remove a library from the app (DB records only — files on disk untouched).
  removeLibrary(id) {
    return fetch(`${API}/libraries/${id}`, { method: "DELETE", headers: authHeaders() })
      .then((r) => { if (!r.ok) return r.json().then((b) => { throw new Error(b.detail || r.status); }); return r.json(); });
  },
  saveProgress(id, chapterName, page) {
    return postJSON(`${API}/series/${id}/progress`, { chapter_name: chapterName, page });
  },
  setFavorite(id, favorite) {
    return postJSON(`${API}/series/${id}/favorite`, { favorite });
  },
  setStatus(id, status) {
    return postJSON(`${API}/series/${id}/status`, { status });
  },
  // Partial metadata update — pass only the fields you want to change.
  saveMetadata(id, fields) {
    return postJSON(`${API}/series/${id}/metadata`, fields);
  },
  splitChapterHere(id, page) {
    return postJSON(`${API}/series/${id}/split_chapter`, { page });
  },
  // Rename a series: renames the folder on disk + updates title/DB/sidecar.
  renameSeries(id, name) {
    return postJSON(`${API}/series/${id}/rename`, { name });
  },
  // Upload a custom cover image (multipart). Returns when the server has stored it.
  async uploadCover(id, file) {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`${API}/series/${id}/cover`, {
      method: "POST",
      headers: authHeaders(),   // do NOT set Content-Type; the browser sets the multipart boundary
      body: fd,
    });
    if (!res.ok) {
      let detail = `${res.status}`;
      try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
      throw new Error(detail);
    }
    bumpCover(id);   // force the <img> to refetch the new cover
    return res.json();
  },
  // Remove the custom cover, reverting to the auto-resolved one.
  removeCover(id) {
    return fetch(`${API}/series/${id}/cover`, { method: "DELETE", headers: authHeaders() }).then(async (r) => {
      if (!r.ok) throw new Error(`${r.status}`);
      bumpCover(id);
      return r.json();
    });
  },
  // Library management + disk rescan.
  listLibraries() {
    return getJSON(`${API}/libraries`);
  },
  browse(path) {
    return getJSON(`${API}/browse?path=${encodeURIComponent(path || "")}`);
  },
  // Rescan: pass libraryId to scan ONLY that library; addPath to add+scan a new
  // one; neither to scan all libraries.
  rescan(addPath, addName, libraryId) {
    return postJSON(`${API}/rescan`, {
      add_path: addPath || null,
      add_name: addName || null,
      library_id: libraryId != null ? libraryId : null,
    });
  },
  rescanStatus() {
    return getJSON(`${API}/rescan/status`);
  },
  // --- import a series from a link ---
  listSources() {
    return getJSON(`${API}/sources`);
  },
  // Search searchable sources (MangaDex, etc.) by title to find series not yet in
  // the library. Returns {results:[{title,author,cover_url,url,source_label,...}], searchable}.
  searchWeb(query) {
    return getJSON(`${API}/search-web?q=${encodeURIComponent(query)}`);
  },
  // Readable chapter count for one web-search result, fetched lazily per card so
  // the search grid isn't held up (see /api/chapter-count). Returns {count}.
  chapterCount(source, url) {
    return getJSON(`${API}/chapter-count?source=${encodeURIComponent(source)}&url=${encodeURIComponent(url)}`);
  },
  // --- read-before-import (preview) ---
  // Metadata + chapter list for a source URL, no download. For the preview detail.
  previewSeries(url) {
    return postJSON(`${API}/preview/series`, { url });
  },
  // Page image URLs for ONE chapter, fetched live (no download). Pass the source
  // name + chapter id from previewSeries.
  previewPages(source, chapterId, number, title) {
    return postJSON(`${API}/preview/pages`, { source, chapter_id: chapterId, number: number || "", title: title || "" });
  },
  // Reader src for a remote preview page — proxied through our server (Referer/CORS
  // handled server-side). `pageUrl` is a source CDN URL from previewPages.
  previewPageUrl(pageUrl, source) {
    return `${API}/preview/page?url=${encodeURIComponent(pageUrl)}&source=${encodeURIComponent(source || "")}${tokenParam(true)}`;
  },
  // --- Source extensions (user-installed declarative site manifests) ---
  listExtensions() {
    return getJSON(`${API}/sources/extensions`);
  },
  installExtension(manifest) {
    return postJSON(`${API}/sources/extensions`, { manifest });
  },
  // Ask the server to turn the AI's learned rules for a site into a manifest
  // (returns it for review; install separately). `url` = a series URL on that site.
  exportAiExtension(url, name) {
    return postJSON(`${API}/sources/extensions/from-ai`, { url, name: name || "" });
  },
  toggleExtension(id, enabled) {
    return postJSON(`${API}/sources/extensions/${encodeURIComponent(id)}/toggle`, { enabled });
  },
  removeExtension(id) {
    return fetch(`${API}/sources/extensions/${encodeURIComponent(id)}`, { method: "DELETE", headers: authHeaders() }).then(async (r) => {
      if (!r.ok) { let d = `${r.status}`; try { const j = await r.json(); if (j && j.detail) d = j.detail; } catch (e) {} throw new Error(d); }
      return r.json();
    });
  },
  // Resolve a URL to its metadata + chapter count (no download yet).
  scrapePreview(url) {
    return postJSON(`${API}/scrape/preview`, { url });
  },
  // Start the background import of `url` into `libraryId`. `title` (from the
  // preview) lets the queue show a real name before the job starts.
  scrapeStart(url, libraryId, title) {
    return postJSON(`${API}/scrape`, { url, library_id: libraryId, title: title || "" });
  },
  scrapeStatus() {
    return getJSON(`${API}/scrape/status`);
  },
  // Remove a still-queued import/re-sync job.
  cancelImport(id) {
    return fetch(`${API}/scrape/${id}`, { method: "DELETE", headers: authHeaders() }).then(async (r) => {
      if (!r.ok) { let d = `${r.status}`; try { const j = await r.json(); if (j && j.detail) d = j.detail; } catch (e) {} throw new Error(d); }
      return r.json();
    });
  },
  // Move a series to another library (physically moves its folder; background).
  moveSeries(id, libraryId) {
    return postJSON(`${API}/series/${id}/move`, { library_id: libraryId });
  },
  moveStatus() {
    return getJSON(`${API}/move/status`);
  },
  cancelMove(id) {
    return fetch(`${API}/move/${id}`, { method: "DELETE", headers: authHeaders() }).then(async (r) => {
      if (!r.ok) { let d = `${r.status}`; try { const j = await r.json(); if (j && j.detail) d = j.detail; } catch (e) {} throw new Error(d); }
      return r.json();
    });
  },
  // Re-sync a series from its origin — downloads any new chapters. Progress is
  // reported on the shared import status endpoint (scrapeStatus).
  resyncSeries(id) {
    return postJSON(`${API}/series/${id}/resync`, {});
  },
  // Queue a re-sync for EVERY imported series in the active library ("check all
  // for new chapters"). Same shared queue/status endpoint as single re-syncs.
  resyncAll() {
    const lib = window.getActiveLibrary ? window.getActiveLibrary() : null;
    return postJSON(`${API}/resync-all${lib != null ? `?library=${lib}` : ""}`, {});
  },
  // Rename a chapter folder (number + optional title combine into the name).
  renameChapter(id, oldName, number, title) {
    return postJSON(`${API}/series/${id}/rename_chapter`, { old_name: oldName, number, title });
  },
  // disk=true also sends the series folder to the RECYCLE BIN (server-validated
  // to live inside a library root; never a hard delete).
  deleteSeries(id, disk = false) {
    return fetch(`${API}/series/${id}${disk ? "?disk=true" : ""}`, { method: "DELETE", headers: authHeaders() }).then(async (r) => {
      if (!r.ok) {
        let detail = `${r.status}`;
        try { const j = await r.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
        throw new Error(detail);
      }
      return r.json();
    });
  },
};

// ---- shared in-memory store -------------------------------------------------
// Screens read window.STORE.items; App populates it once on load. Facets feed
// the filter dropdowns and search panel.
const STORE = {
  items: [],
  facets: { genres: [], tags: [], parodies: [], authors: [], series: [], languages: [], statuses: [], tag_counts: {}, genre_counts: {}, parody_counts: {}, author_counts: {}, series_counts: {}, language_counts: {} },
  loaded: false,
};

Object.assign(window, {
  STATUS_COLOR,
  coverUrl,
  pageUrl,
  progressLabel,
  ApiClient,
  STORE,
  normalize,
  Settings,
  getActiveLibrary,
  setActiveLibrary,
  bumpCover,
  authToken,
  setAuthToken,
  clearAuthToken,
});
