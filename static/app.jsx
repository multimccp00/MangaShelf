/* global window, React, ReactDOM */
const { useState, useEffect, useRef } = React;

// Bump on every frontend release; shown tiny in the corner so we can confirm which
// bundle a browser is actually running (ends cache-vs-code guessing).
const BUILD_VERSION = "web84";

// One background-job channel: polls a status endpoint while a job is active and
// exposes start/dismiss. Used once per concurrent job type (import, move) so they
// track independently — finishing a move never clears the import's indicator.
function useJobChannel(fetchStatus, onComplete) {
  const [active, setActive] = useState(null);   // { kind, title } | null
  const [status, setStatus] = useState(null);
  const titles = useRef({});                    // job-id -> title (for the queue popover)

  // Keep the latest fetchStatus/onComplete in refs so the polling effect (which
  // depends only on [active]) always calls the CURRENT versions, never a stale
  // closure captured when the job started.
  const fetchRef = useRef(fetchStatus); fetchRef.current = fetchStatus;
  const doneRef = useRef(onComplete); doneRef.current = onComplete;

  function start(kind, title, id) {
    if (id != null && title) titles.current[id] = title;
    setStatus({ running: true, message: "Starting…" });
    setActive({ kind, title });
  }
  function dismiss() { setActive(null); setStatus(null); }

  useEffect(() => {
    if (!active) return;
    let alive = true, timer = null, lastId;
    const tick = () => {
      fetchRef.current().then((s) => {
        if (!alive) return;
        setStatus(s);
        if (s.running) {
          // A queued job just became active → the previous one finished → refresh.
          if (s.id != null && lastId !== undefined && s.id !== lastId) doneRef.current();
          lastId = s.id;
          timer = setTimeout(tick, 1000);
          return;
        }
        if (!s.error) doneRef.current();
        timer = setTimeout(() => { if (alive) { setActive(null); setStatus(null); } },
                           s.error ? 9000 : 3500);
      }).catch((e) => {
        if (!alive) return;
        setStatus({ error: e.message || String(e) });
        timer = setTimeout(() => { if (alive) { setActive(null); setStatus(null); } }, 7000);
      });
    };
    tick();
    return () => { alive = false; clearTimeout(timer); };
  }, [active]);

  return { active, status, titles, start, dismiss };
}

// ---------------------------------------------------------------------------
// Read-before-import preview. Shows a source series' cover/description/chapter
// list (fetched with no download). Click a chapter → it streams live into the
// reader (proxied). "Import" hands off to the normal import flow. Nothing is
// saved to disk unless the user imports.
// Shown the instant a preview is requested, while the (potentially slow) chapter
// feed is fetched from the source. A skeleton in the same shape as PreviewModal so
// the swap to real content isn't jarring.
function PreviewLoading({ onClose }) {
  return (
    <window.Modal onClose={onClose} panelClass="preview-modal preview-modal-loading" labelledBy="preview-loading-title">
      <div className="preview-head">
        <div className="preview-cover"><div className="preview-skel preview-skel-cover" /></div>
        <div className="preview-info">
          <div className="preview-badge">Fetching preview…</div>
          <h2 id="preview-loading-title" className="preview-title">
            <span className="preview-spinner" aria-hidden="true" /> Loading chapters…
          </h2>
          <div className="preview-skel preview-skel-line" style={{ width: "60%" }} />
          <div className="preview-skel preview-skel-line" style={{ width: "90%" }} />
          <div className="preview-skel preview-skel-line" style={{ width: "80%" }} />
          <div className="preview-hint">Pulling the chapter list from the source — this can take a few seconds.</div>
        </div>
        <button className="icon-btn preview-close" onClick={onClose} title="Close"><window.IconClose size={16} /></button>
      </div>
      <div className="preview-chapters-head"><span>Chapters</span></div>
      <div className="preview-chapters">
        {[0, 1, 2, 3, 4].map((i) => <div key={i} className="preview-skel preview-skel-chapter" />)}
      </div>
    </window.Modal>
  );
}

function PreviewModal({ data, onClose, onReadChapter, onImport }) {
  const [loadingCh, setLoadingCh] = useState(null);   // chapter index being fetched
  const [coverErr, setCoverErr] = useState(false);
  const [chFilter, setChFilter] = useState("");       // chapter-list filter (D2.4)
  const chReqRef = useRef(0);                          // stale-guard for chapter loads
  const allChapters = data.chapters || [];
  // Filter the chapter list by number/title so a 400-chapter series is navigable.
  const chapters = chFilter.trim()
    ? allChapters.filter((ch) => {
        const q = chFilter.trim().toLowerCase();
        return String(ch.number || "").toLowerCase().includes(q)
          || String(ch.title || "").toLowerCase().includes(q);
      })
    : allChapters;

  function readChapter(ch) {
    if (loadingCh != null) return;
    const myReq = ++chReqRef.current;
    setLoadingCh(ch.index);
    window.ApiClient.previewPages(data.source, ch.id, ch.number, ch.title)
      .then((r) => {
        if (myReq !== chReqRef.current) return;   // superseded by a newer click
        const pages = (r && r.pages) || [];
        if (!pages.length) { window.toast("This chapter has no readable pages.", "error"); return; }
        // Build a reader item with ONE chapter whose pages are the remote URLs.
        const item = {
          id: null, title: data.title,
          chapters: [{ name: ch.number ? `Chapter ${ch.number}` : (ch.title || "Chapter"),
                       page_count: pages.length, pages }],
          read: 0, pages: pages.length,
        };
        onReadChapter(item, 1, pages.length);
      })
      .catch((e) => window.toast(`Couldn't load chapter: ${e.message || e}`, "error"))
      .finally(() => setLoadingCh(null));
  }

  return (
    <window.Modal onClose={onClose} panelClass="preview-modal" labelledBy="preview-title">
      <div className="preview-head">
        <div className="preview-cover">
          {data.cover_url && !coverErr
            ? <img src={data.cover_url} alt="" referrerPolicy="no-referrer" onError={() => setCoverErr(true)} />
            : <div className="preview-noart">本</div>}
        </div>
        <div className="preview-info">
          <div className="preview-badge">{data.source_label} · preview</div>
          <h2 id="preview-title" className="preview-title">{data.title}</h2>
          <div className="preview-sub">
            {data.author || "Unknown"} · {chapters.length} chapters
          </div>
          {data.genres && data.genres.length > 0 && (
            <div className="preview-genres">{data.genres.slice(0, 8).join(" · ")}</div>
          )}
          {data.description && <p className="preview-desc">{data.description}</p>}
          <div className="preview-actions">
            <button className="btn primary" onClick={() => onImport(data.url)}>
              <window.IconPlus size={14} /> {data.already ? "Already in library — import again" : "Import to library"}
            </button>
            <a className="btn ghost sm" href={data.url} target="_blank" rel="noreferrer">Open source ↗</a>
          </div>
          <div className="preview-hint">Reading a chapter streams it live — nothing is downloaded until you import.</div>
        </div>
        <button className="icon-btn preview-close" onClick={onClose} title="Close"><window.IconClose size={16} /></button>
      </div>

      <div className="preview-chapters-head">
        <span>Chapters — click to read</span>
        {allChapters.length > 12 && (
          <input className="preview-ch-filter" placeholder="Filter #/title…"
            value={chFilter} onChange={(e) => setChFilter(e.target.value)} />
        )}
      </div>
      <div className="preview-chapters">
        {allChapters.length === 0 && <div className="preview-empty">No chapters found.</div>}
        {allChapters.length > 0 && chapters.length === 0 && <div className="preview-empty">No chapters match “{chFilter}”.</div>}
        {chapters.map((ch) => (
          <button key={ch.index} className="preview-chapter" disabled={loadingCh != null}
            onClick={() => readChapter(ch)}>
            <window.IconPlay size={11} />
            <span className="preview-chapter-name">
              {ch.number ? `Chapter ${ch.number}` : (ch.title || `Chapter ${ch.index + 1}`)}
              {ch.title && ch.number ? ` · ${ch.title}` : ""}
            </span>
            {loadingCh === ch.index && <span className="preview-chapter-loading">loading…</span>}
          </button>
        ))}
      </div>
    </window.Modal>
  );
}

function App() {
  // useTweaks is shimmed (tweaks-shim.jsx) — holds local UI prefs only.
  const [t, setTweak] = window.useTweaks({
    accent: "#ef3b3b",
    readerBg: "dark",
    density: "balanced",
    grain: true,
  });

  const [screen, setScreen] = useState("landing");
  const [reader, setReader] = useState(null);     // { item, startPage }
  const [detail, setDetail] = useState(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [rescanOpen, setRescanOpen] = useState(false);
  const [addLinkOpen, setAddLinkOpen] = useState(false);
  const [addLinkUrl, setAddLinkUrl] = useState("");   // prefill when opened from a pasted link
  const [preview, setPreview] = useState(null);       // {source,title,chapters,...} for read-before-import
  const [previewLoading, setPreviewLoading] = useState(false);  // fetch in flight (slow — MangaDex feed)

  // Open a read-before-import preview for a source URL: fetch its chapter list
  // (no download) and show the preview modal. The fetch can be slow (it pulls the
  // whole chapter feed from the source), so we show a loading modal IMMEDIATELY on
  // click for instant feedback. Errors surface as a toast. A request id guards
  // against a slower earlier preview clobbering a newer one.
  const previewReqRef = useRef(0);
  function openPreview(url) {
    // Keep the search panel MOUNTED underneath the preview (don't setSearchOpen(false))
    // so its web-search results survive — closing the preview drops you back onto the
    // same results instead of the library, giving a natural "back to search".
    const myReq = ++previewReqRef.current;
    setPreview(null);
    setPreviewLoading(true);
    window.ApiClient.previewSeries(url)
      .then((d) => { if (myReq === previewReqRef.current) { setPreview(d); setPreviewLoading(false); } })
      .catch((e) => {
        if (myReq !== previewReqRef.current) return;
        setPreviewLoading(false);
        window.toast(`Couldn't preview: ${e.message || e}`, "error");
      });
  }
  function closePreview() { previewReqRef.current++; setPreview(null); setPreviewLoading(false); }
  // Open the import dialog, optionally pre-filled with a pasted URL. Closes the
  // search overlay if it was open (so pasting a link in search jumps to import).
  function openAddLink(url) {
    setAddLinkUrl(url || "");
    setSearchOpen(false);
    setAddLinkOpen(true);
  }

  // Two independent background-job channels so an import (queue) and a move can
  // run — and be shown — at the same time. Each polls its own status endpoint.
  // (refreshLibraries/reload are hoisted function declarations, safe to reference.)
  const importCh = useJobChannel(() => window.ApiClient.scrapeStatus(), () => { refreshLibraries(); reload(); });
  const moveCh = useJobChannel(() => window.ApiClient.moveStatus(), () => { refreshLibraries(); reload(); });

  // Data load: fetch the real library + facets into the shared STORE, then
  // bump a counter to re-render screens that read STORE synchronously.
  const [tick, setTick] = useState(0);
  const [loadError, setLoadError] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [libraries, setLibraries] = useState([]);
  const [activeLib, setActiveLib] = useState(null);   // active library id
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [showSwitcher, setShowSwitcher] = useState(window.Settings.showSwitcher);
  // Auth gate: when the server requires a password, the page boots WITHOUT a
  // token. We show a login screen until the user logs in (or a previously stored
  // token still works). needsLogin === null means "still deciding".
  const [needsLogin, setNeedsLogin] = useState(null);

  // Scroll container for the main content area (declared early so the
  // scroll-to-top-on-detail effect below can reference it).
  const contentRef = useRef(null);
  function scrollTop() { if (contentRef.current) contentRef.current.scrollTop = 0; }

  // Fetch the data for whatever library is currently active.
  async function reload() {
    try {
      const [items, facets] = await Promise.all([
        window.ApiClient.listSeries(),
        window.ApiClient.getFacets(),
      ]);
      window.STORE.items = items;
      window.STORE.facets = facets;
      window.STORE.loaded = true;
      setLoaded(true);
      setTick((n) => n + 1);
    } catch (e) {
      setLoadError(String(e));
    }
  }

  // Switch the active library: set it, persist, then reload the data for it.
  async function switchLibrary(id) {
    window.setActiveLibrary(id);
    setActiveLib(id);
    setScreen((s) => (s === "detail" ? "library" : s));   // leave any open detail
    await reload();
  }

  // Resolve which library to open on first load: the server default, or the
  // last-used one (per the startMode setting). Then load its data.
  async function init() {
    try {
      // Pull authoritative settings from the server first so the startup choice
      // (default vs last-used) reflects saved preferences, not stale cache.
      await window.Settings.loadFromServer();
      setShowSwitcher(window.Settings.showSwitcher);
      const libs = await window.ApiClient.listLibraries();
      setLibraries(libs);
      let target = null;
      if (window.Settings.startMode === "last" && window.Settings.lastLibraryId != null) {
        target = libs.find((l) => l.id === window.Settings.lastLibraryId);
      }
      if (!target) target = libs.find((l) => l.is_default) || libs[0];
      if (target) {
        window.setActiveLibrary(target.id);
        setActiveLib(target.id);
      }
      await reload();
    } catch (e) {
      setLoadError(String(e));
    }
  }

  // Re-fetch the library list (after rename/flag/default/rescan changes).
  async function refreshLibraries() {
    try { setLibraries(await window.ApiClient.listLibraries()); } catch (e) {}
  }

  // Poll the active background job's status (import/re-sync via scrapeStatus,
  // move via moveStatus) at the app level, so the modal can close and you keep
  // browsing while it runs. On completion, refresh the library.
  // Adopt jobs running SERVER-SIDE so their indicator shows on THIS device — no
  // matter when the import started (another device, before this tab opened) or how
  // many times the page was refreshed. Imports run on the server, independent of any
  // browser tab; this keeps a *continuous* low-frequency watch (every 4s while the
  // channel is idle) so the indicator reappears after a refresh/close/reopen and
  // cross-device (start on phone → see it on PC). The channel's own 1s poll takes
  // over once adopted; this watcher only fires when nothing is currently shown.
  useEffect(() => {
    if (!loaded) return;
    let alive = true;
    const tick = () => {
      // Only look for a job to adopt when the indicator isn't already showing one
      // (avoids fighting the channel's own poll).
      if (!importCh.active) {
        window.ApiClient.scrapeStatus()
          .then((s) => { if (alive && s && s.running) importCh.start(s.kind || "import", s.title); })
          .catch(() => {});
      }
      if (!moveCh.active) {
        window.ApiClient.moveStatus()
          .then((s) => { if (alive && s && s.running) moveCh.start("move", s.title); })
          .catch(() => {});
      }
    };
    tick();                                  // immediately on load
    const id = setInterval(tick, 4000);      // then keep watching
    return () => { alive = false; clearInterval(id); };
  }, [loaded, importCh.active, moveCh.active]);

  // Decide whether to show the login gate, then init. If the server says a
  // password is required AND we don't already hold a working token, gate. If we
  // hold a token (from a prior login), verify it by attempting init — a 401 will
  // surface as a load error, but we optimistically proceed since a stored token
  // is almost always valid.
  useEffect(() => {
    (async () => {
      const passwordRequired = !!window.MANGASHELF_PASSWORD_REQUIRED;
      const haveToken = !!window.authToken();
      if (passwordRequired && !haveToken) {
        setNeedsLogin(true);     // show the login screen
        return;
      }
      setNeedsLogin(false);
      init();
    })();
  }, []);

  // Called by the login screen once a password is accepted (token now stored).
  function onLoggedIn() {
    setNeedsLogin(false);
    init();
  }

  // Deep-link helpers: #library / #search open those views directly (handy for
  // sharing a link straight to the library, and for testing).
  useEffect(() => {
    if (!loaded) return;
    if (location.hash === "#library") setScreen("library");
    if (location.hash === "#search") setSearchOpen(true);
  }, [loaded]);

  useEffect(() => {
    document.documentElement.style.setProperty("--vermillion", t.accent);
    document.documentElement.style.setProperty("--vermillion-glow", t.accent + "73");
  }, [t.accent]);

  // Whenever a NEW detail opens, scroll the content area to the top — otherwise it
  // inherits the library's scroll position and opens already scrolled down. Runs
  // after the detail content mounts, so it wins over any layout shift.
  useEffect(() => {
    if (screen === "detail" && contentRef.current) contentRef.current.scrollTop = 0;
  }, [detail, screen]);

  // Returning to the list from a detail: restore the scroll position we left.
  // Cards/covers load async and change the content height, so a single set isn't
  // enough — we re-apply over a few animation frames until it sticks (or briefly).
  useEffect(() => {
    if (screen === "detail" || !restoreScrollRef.current) return;
    restoreScrollRef.current = false;
    const target = savedScrollRef.current;
    if (!contentRef.current || target <= 0) return;
    let frames = 0;
    const apply = () => {
      const el = contentRef.current;
      if (!el) return;
      el.scrollTop = target;
      // Keep re-applying for ~0.5s so late-loading cover images that grow the
      // page don't bump us off the saved position.
      if (frames++ < 30 && Math.abs(el.scrollTop - target) > 2) requestAnimationFrame(apply);
    };
    requestAnimationFrame(apply);
  }, [screen, tick]);

  useEffect(() => {
    function onKey(e) {
      if (e.key === "Escape") {
        // The Reader handles its own Escape (so it can flush the pending progress
        // save before closing); here we only close the search overlay.
        if (!reader && searchOpen) setSearchOpen(false);
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setSearchOpen(true);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [reader, searchOpen]);

  // Remember which screen a detail was opened FROM so the back button returns
  // there (landing vs library) instead of always dumping to the library.
  const [detailOrigin, setDetailOrigin] = useState("library");
  // Scroll position of the list screen we left, to restore on "back".
  const savedScrollRef = useRef(0);
  const restoreScrollRef = useRef(false);   // true → next detail-exit should restore, not reset
  function openDetail(it) {
    // If opened from the search overlay, fall back to the screen underneath it.
    setDetailOrigin(screen === "detail" ? detailOrigin : screen);
    // Remember where we were in the list so "back" returns to the same spot
    // (only capture when coming FROM a list screen, not detail→detail).
    if (screen !== "detail" && contentRef.current) {
      savedScrollRef.current = contentRef.current.scrollTop;
      restoreScrollRef.current = true;
    }
    setDetail(it);
    setScreen("detail");
    // Start the detail at the top — the scroll container otherwise keeps the
    // library's scroll position, opening the manga already scrolled down.
    scrollTop();
  }
  function openSearch(q) { setSearchQuery(typeof q === "string" ? q : ""); setSearchOpen(true); }
  // Returning from detail: bump the tick so the library re-renders with any status/
  // favorite changes the detail screen made to the shared STORE items, then
  // restore the scroll position we left from (handled by the effect below).
  function goBackToLibrary() {
    setTick((n) => n + 1);
    setScreen(detailOrigin === "landing" ? "landing" : "library");
    // Don't scrollTop() — the restore effect puts us back where we were.
  }

  // Auth gate. While deciding, render nothing (avoids a flash of the app). When a
  // password is required and we're not logged in, show the login screen instead.
  if (needsLogin === null) {
    return <div className="app" />;
  }
  if (needsLogin) {
    return <LoginScreen onLoggedIn={onLoggedIn} />;
  }

  return (
    <div className={"app" + (t.grain ? " grain" : "")}>
      <main className="workspace">
        {/* The landing screen has its own centered search and nav, so the topbar
            is redundant there — only show it on the other screens. */}
        {screen !== "landing" && (
          <header className="topbar">
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <button className="btn ghost sm" style={screen === "landing" ? { background: "var(--ink-2)", color: "white" } : {}} onClick={() => setScreen("landing")}>Home</button>
              <button className="btn ghost sm" style={screen === "library" ? { background: "var(--ink-2)", color: "white" } : {}} onClick={() => setScreen("library")}>Library</button>
              <button className="btn ghost sm" onClick={() => { window.STORE._favOnly = true; setScreen("library"); }}>Favorites</button>
            </div>

            {/* Flexible spacers center the search bar (stretch on both sides). */}
            <div className="topbar-spacer" style={{ flex: 1 }} />
            <div className="search" onClick={() => openSearch("")} style={{ cursor: "pointer", flex: "0 1 520px" }}>
              <span className="search-icon"><window.IconSearch size={17} /></span>
              <input placeholder="Search series, authors, tags…" readOnly style={{ cursor: "pointer" }}
                onKeyDown={(e) => { if (e.key === "Enter") openSearch(""); }} />
              <kbd>⌘K</kbd>
            </div>
            <div className="topbar-spacer" style={{ flex: 1 }} />

            <button className="btn ghost sm refresh-btn" title="Reload from database" onClick={reload}><window.IconCycle size={13} /> Refresh</button>
            <button className="btn ghost sm rescan-btn" title="Rescan folders on disk / add a library" onClick={() => setRescanOpen(true)}><window.IconSearch size={13} /> Rescan</button>
            <button className="btn ghost sm addlink-btn" title="Import a series from a link" onClick={() => openAddLink("")}><window.IconPlus size={13} /> From link</button>
          </header>
        )}

        <div className="content" ref={contentRef}>
          {loadError && (
            <div className="empty"><div className="glyph">!</div><p>Couldn't load library: {loadError}</p></div>
          )}
          {!loaded && !loadError && (
            <div className="empty"><p>Loading library…</p></div>
          )}
          {loaded && screen === "landing" && (
            <window.Landing tick={tick} onOpenDetail={openDetail} onOpenSearch={openSearch} onImportLink={openAddLink} onBrowse={() => { setScreen("library"); scrollTop(); }} density={t.density} />
          )}
          {loaded && screen === "library" && (
            <window.Library onOpenDetail={openDetail} onOpenSearch={openSearch} density={t.density} setDensity={(d) => setTweak("density", d)} />
          )}
          {loaded && screen === "detail" && detail && (
            <window.Detail
              item={detail}
              libraries={libraries}
              refreshSignal={tick}
              onBack={goBackToLibrary}
              onMoveStarted={(title) => { goBackToLibrary(); moveCh.start("move", title); }}
              onOpenReader={(item, page, endPage, resumePage) => setReader({ item, startPage: page, endPage, resumePage })}
            />
          )}
        </div>
      </main>

      {searchOpen && (
        <window.SearchPanel
          onClose={() => setSearchOpen(false)}
          onOpenDetail={openDetail}
          onImportLink={openAddLink}
          onPreviewLink={openPreview}
          initialQuery={searchQuery}
        />
      )}
      {reader && (
        <window.Reader
          item={reader.item}
          startPage={reader.startPage}
          endPage={reader.endPage}
          resumePage={reader.resumePage}
          previewSource={reader.previewSource}
          bg={t.readerBg}
          onClose={() => { setReader(null); if (!reader.previewSource) setTimeout(reload, 350); }}
        />
      )}
      {previewLoading && !preview && <PreviewLoading onClose={closePreview} />}
      {preview && (
        <PreviewModal
          data={preview}
          onClose={closePreview}
          onReadChapter={(item, startPage, endPage) =>
            setReader({ item, startPage, endPage, previewSource: preview.source })}
          onImport={(url) => { closePreview(); openAddLink(url); }}
        />
      )}
      {rescanOpen && (
        <RescanModal
          onClose={() => setRescanOpen(false)}
          onDone={() => { setRescanOpen(false); refreshLibraries(); reload(); }}
        />
      )}
      {addLinkOpen && (
        <AddLinkModal
          libraries={libraries}
          activeLib={activeLib}
          initialUrl={addLinkUrl}
          onClose={() => { setAddLinkOpen(false); setAddLinkUrl(""); }}
          onStarted={(title, id) => { setAddLinkOpen(false); setAddLinkUrl(""); importCh.start("import", title, id); }}
        />
      )}
      {(importCh.active || moveCh.active) && (
        <div className="bg-job-stack">
          {moveCh.active && moveCh.status && (
            <BackgroundJob kind={moveCh.active.kind} title={moveCh.active.title} status={moveCh.status}
              titlesById={moveCh.titles.current} onDismiss={moveCh.dismiss}
              onCancel={(id) => window.ApiClient.cancelMove(id).catch((e) => window.toast(`Couldn't cancel: ${e.message || e}`, "error"))} />
          )}
          {importCh.active && importCh.status && (
            <BackgroundJob kind={importCh.active.kind} title={importCh.active.title} status={importCh.status}
              titlesById={importCh.titles.current} onDismiss={importCh.dismiss}
              onCancel={(id) => window.ApiClient.cancelImport(id).catch((e) => window.toast(`Couldn't cancel: ${e.message || e}`, "error"))} />
          )}
        </div>
      )}
      {settingsOpen && (
        <SettingsModal
          libraries={libraries}
          activeLib={activeLib}
          onClose={() => setSettingsOpen(false)}
          onAddLibrary={() => { setSettingsOpen(false); setRescanOpen(true); }}
          onChanged={refreshLibraries}
          onSwitch={(id) => { setSettingsOpen(false); switchLibrary(id); }}
          onShowSwitcher={setShowSwitcher}
        />
      )}
      {/* Floating quick-switch button (bottom-right). Hidden inside the reader so
          it doesn't cover pages. The cycle FAB itself is toggleable via the
          showSwitcher setting — but since the topbar no longer has a gear, we
          ALWAYS render at least the gear-only variant when the switcher is off,
          so Settings stays reachable everywhere. */}
      {loaded && !reader && (
        <LibrarySwitcher
          libraries={libraries}
          activeLib={activeLib}
          onSwitch={switchLibrary}
          onOpenSettings={() => setSettingsOpen(true)}
          gearOnly={!showSwitcher}
        />
      )}
      {/* Tiny build tag so we can confirm which bundle a browser is running. */}
      <div className="build-tag" title="App version">{BUILD_VERSION}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Login gate. Shown when the server requires a password and we have no valid
// token yet. On success the token is stored (api.js) and onLoggedIn() proceeds.
function LoginScreen({ onLoggedIn }) {
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [remember, setRemember] = useState(true);   // trust this device by default
  const inputRef = useRef(null);

  useEffect(() => { if (inputRef.current) inputRef.current.focus(); }, []);

  async function submit(e) {
    if (e) e.preventDefault();
    if (busy) return;
    setErr("");
    setBusy(true);
    try {
      await window.ApiClient.login(pw, remember);
      onLoggedIn();
    } catch (ex) {
      setErr(ex.message === "401" || /incorrect/i.test(ex.message) ? "Incorrect password" : `Login failed: ${ex.message || ex}`);
      setPw("");
      if (inputRef.current) inputRef.current.focus();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">MANGA SHELF</div>
        <div className="login-sub">This library is password-protected.</div>
        <window.PasswordInput
          inputRef={inputRef}
          className="login-input"
          placeholder="Password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          autoComplete="current-password"
        />
        <label className="login-remember">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
          <span>Remember this device</span>
        </label>
        {err && <div className="login-error">{err}</div>}
        <button className="btn primary login-btn" type="submit" disabled={busy || !pw}>
          {busy ? "Checking…" : "Unlock"}
        </button>
        <div className="login-remember-hint">
          {remember
            ? "You'll stay logged in on this device until you log out."
            : "You'll be asked again next time you open the browser."}
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Floating bottom-right library switcher. The main FAB CYCLES to the next
// library on tap (no menu); rotating INTO a private library asks for a confirm
// (per setting). A companion gear opens Settings — it fades in on hover on
// desktop, and is reachable via a long-press on the FAB on touch devices.
function LibrarySwitcher({ libraries, activeLib, onSwitch, onOpenSettings, gearOnly }) {
  const active = libraries.find((l) => l.id === activeLib);
  const [spin, setSpin] = useState(0);          // bump to replay the rotate animation
  const longPressRef = useRef(null);            // timer id for touch long-press
  const longFiredRef = useRef(false);           // true once long-press opened settings

  // Switcher disabled in settings, but we're on landing (no topbar gear): show
  // just a Settings gear, nothing to cycle.
  if (gearOnly) {
    return (
      <div className="lib-switcher">
        <button className="lib-switcher-gear always" title="Settings & libraries" onClick={onOpenSettings}>
          <window.IconGear size={16} />
        </button>
      </div>
    );
  }

  // The next library in the list, wrapping around. Returns null if there's
  // 0 or 1 library (nothing to cycle to).
  function nextLibrary() {
    if (!libraries.length) return null;
    const i = libraries.findIndex((l) => l.id === activeLib);
    if (i === -1) return libraries[0];
    if (libraries.length < 2) return null;
    return libraries[(i + 1) % libraries.length];
  }

  async function cycle() {
    const next = nextLibrary();
    if (!next) return;                            // only one library — nothing to do
    if (next.private && window.Settings.confirmPrivate) {
      const ok = await window.confirmDialog({
        title: `Switch to “${next.name}”?`,
        message: "This is a private library — its content will be shown.",
        confirmLabel: "Switch",
        tone: "warn",
      });
      if (!ok) return;
    }
    setSpin((n) => n + 1);                         // replay the rotate animation
    onSwitch(next.id);
  }

  // Touch long-press → Settings (mirrors the desktop hover-to-reveal gear).
  function onTouchStart() {
    longFiredRef.current = false;
    longPressRef.current = setTimeout(() => {
      longFiredRef.current = true;
      onOpenSettings();
    }, 500);
  }
  function clearLongPress() {
    if (longPressRef.current) { clearTimeout(longPressRef.current); longPressRef.current = null; }
  }
  function onClick() {
    // A long-press already opened Settings — swallow the trailing click so we
    // don't ALSO cycle the library.
    if (longFiredRef.current) { longFiredRef.current = false; return; }
    cycle();
  }

  const only = libraries.length < 2;              // single library: FAB can't cycle

  return (
    <div className="lib-switcher">
      {/* Companion gear: hover-reveal on desktop (handled in CSS); always shown
          when the cycle FAB can't do anything (single library). */}
      <button
        className={"lib-switcher-gear" + (only ? " always" : "")}
        title="Settings & libraries"
        onClick={onOpenSettings}
      >
        <window.IconGear size={16} />
      </button>
      <button
        className={"lib-switcher-fab" + (active && active.private ? " private" : "") + (spin ? " spin" : "")}
        key={"fab-" + spin}
        title={
          only
            ? (active ? `Library: ${active.name}` : "Library")
            : (active ? `Library: ${active.name} — tap to switch, long-press for settings` : "Switch library")
        }
        onClick={onClick}
        onContextMenu={(e) => { e.preventDefault(); onOpenSettings(); }}
        onTouchStart={onTouchStart}
        onTouchEnd={clearLongPress}
        onTouchMove={clearLongPress}
        onTouchCancel={clearLongPress}
      >
        <window.IconLibrary size={20} />
        {active && active.private && <span className="lib-switcher-fab-lock"><window.IconLock size={11} /></span>}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// A small iOS-style toggle switch.
function Toggle({ checked, onChange }) {
  return (
    <button
      type="button"
      className={"toggle" + (checked ? " on" : "")}
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
    >
      <span className="toggle-knob" />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Floating progress indicator for a background job (import / re-sync / move).
// Persists across navigation (rendered at the app root), so you can keep browsing
// while a long download or move runs. Dismissing only hides it — the server keeps
// working — but we keep it up until the job finishes so you don't lose track.
function BackgroundJob({ kind, title, status, titlesById, onDismiss, onCancel }) {
  const label = kind === "move" ? "Moving" : kind === "resync" ? "Updating" : "Importing";
  const error = status && status.error;
  const warning = status && status.warning;   // finished, but some pages failed
  const total = status ? (status.total || 0) : 0;
  const cur = status ? (kind === "move" ? (status.copied || 0) : (status.chapter || 0)) : 0;
  const pct = total ? Math.round((cur / total) * 100) : 0;
  const finished = !status.running;
  const queuedList = (status && status.queued) || [];
  const queued = status ? (status.queued_count || queuedList.length || 0) : 0;
  // Name a queued job from the server title, else the title we remembered at
  // enqueue time (the server only learns a title once the job starts running).
  const nameFor = (q) => (q.title && q.title !== "Queued" ? q.title
    : (titlesById && titlesById[q.id]) || "Queued import");
  // Prefer the LIVE server title (the currently-active job) over the title the
  // channel was started with — otherwise the label stays stuck on the first job
  // as the queue advances.
  const activeName = (status && status.title) || title || "";

  return (
    <div className={"bg-job" + (error ? " error" : finished ? (warning ? " warn" : " done") : "") + (queued > 0 ? " has-queue" : "")}
         role="status" aria-live="polite">
      {queued > 0 && (
        <div className="bg-job-queuelist">
          <div className="bg-job-queuelist-head">
            <span>{kind === "move" ? "Move queue" : "Import queue"}</span><span className="bg-job-queuelist-count">{queued + 1}</span>
          </div>
          <div className="bg-job-qitem active">
            <span className="bg-job-qnum">1</span>
            <span className="bg-job-qname">{activeName || label}</span>
            <span className="bg-job-qtag">now</span>
          </div>
          {queuedList.map((q, i) => (
            <div className="bg-job-qitem" key={q.id != null ? q.id : i}>
              <span className="bg-job-qnum">{i + 2}</span>
              <span className="bg-job-qname">{nameFor(q)}</span>
              {onCancel && q.id != null && (
                <button className="bg-job-qcancel" title="Remove from queue"
                  onClick={() => onCancel(q.id)}><window.IconClose size={12} /></button>
              )}
            </div>
          ))}
        </div>
      )}
      <div className="bg-job-head">
        <span className="bg-job-spinner">{finished ? (error ? "!" : warning ? "!" : "✓") : <span className="rescan-spinner" />}</span>
        <div className="bg-job-text">
          <div className="bg-job-title">{error ? "Failed" : finished ? (warning ? "Done, with issues" : "Done") : label}{activeName ? ` · ${activeName}` : ""}</div>
          <div className="bg-job-msg">{error ? error : (status.message || "")}</div>
        </div>
        {queued > 0 && <span className="bg-job-queue" title={`${queued} waiting — hover to see the queue`}>+{queued}</span>}
        <button className="icon-btn sm" onClick={onDismiss} title="Hide"><window.IconClose size={14} /></button>
      </div>
      {!finished && total > 0 && (
        <div className="bg-job-bar"><div className="fill" style={{ width: `${pct}%` }} /></div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Source extensions manager. Install/enable/disable/remove declarative site
// manifests (JSON). Manifests are DATA, not code — safe to install/share.
function SourceExtensionsModal({ onClose }) {
  const [items, setItems] = useState([]);
  const [errors, setErrors] = useState([]);
  const [loading, setLoading] = useState(true);
  const [json, setJson] = useState("");
  const [busy, setBusy] = useState(false);
  const [aiUrl, setAiUrl] = useState("");
  const [aiBusy, setAiBusy] = useState(false);

  function refresh() {
    setLoading(true);
    window.ApiClient.listExtensions()
      .then((r) => { setItems(r.extensions || []); setErrors(r.errors || []); })
      .catch((e) => window.toast(`Couldn't load extensions: ${e.message || e}`, "error"))
      .finally(() => setLoading(false));
  }
  useEffect(() => { refresh(); }, []);

  function install() {
    let manifest;
    try { manifest = JSON.parse(json); }
    catch (e) { window.toast("That isn't valid JSON.", "error"); return; }
    setBusy(true);
    window.ApiClient.installExtension(manifest)
      .then((r) => { window.toast(`Installed “${r.name}” v${r.version}.`, "ok"); setJson(""); refresh(); })
      .catch((e) => window.toast(`${e.message || e}`, "error"))
      .finally(() => setBusy(false));
  }
  function toggle(it) {
    window.ApiClient.toggleExtension(it.id, !it.enabled)
      .then(refresh).catch((e) => window.toast(`${e.message || e}`, "error"));
  }
  async function remove(it) {
    const ok = await window.confirmDialog({
      title: `Remove “${it.name || it.id}”?`, message: "This deletes the extension file.",
      confirmLabel: "Remove", tone: "danger",
    });
    if (!ok) return;
    window.ApiClient.removeExtension(it.id)
      .then(() => { window.toast("Extension removed.", "ok"); refresh(); })
      .catch((e) => window.toast(`${e.message || e}`, "error"));
  }
  // Generate a manifest from the AI's learned rules for a site, then drop it into
  // the install box for review (the user installs it as usual).
  function generateFromAi() {
    if (!aiUrl.trim()) return;
    setAiBusy(true);
    window.ApiClient.exportAiExtension(aiUrl.trim())
      .then((r) => {
        setJson(JSON.stringify(r.manifest, null, 2));
        window.toast("Generated a manifest from AI rules — review and install below.", "ok");
      })
      .catch((e) => window.toast(`${e.message || e}`, "error"))
      .finally(() => setAiBusy(false));
  }

  return (
    <window.Modal onClose={onClose} panelClass="settings-modal" labelledBy="ext-title">
      <div className="settings-head">
        <h2 id="ext-title"><window.IconLibrary size={20} /> Source extensions</h2>
        <button className="icon-btn" onClick={onClose} title="Close"><window.IconClose size={16} /></button>
      </div>
      <div className="settings-body">
        <div className="settings-section">
          <div className="settings-section-hint">
            Add support for a new site by installing its extension (a small JSON file).
            Extensions are data, not code — but only install ones you trust: an extension
            can fetch from the site it names.
          </div>
          {loading ? <div className="settings-section-hint">Loading…</div> : (
            <div className="ext-list">
              {items.length === 0 && <div className="settings-section-hint">No extensions installed yet.</div>}
              {items.map((it) => (
                <div key={it.file} className={"ext-row" + (it.valid ? "" : " invalid")}>
                  <span className={"settings-lib-dot" + (it.enabled && it.valid ? " on" : "")} />
                  <div className="ext-main">
                    <div className="ext-name">
                      {it.name || it.id}
                      {it.version && <span className="ext-ver">v{it.version}</span>}
                      {!it.valid && <span className="ext-badge-bad" title={it.error}>invalid</span>}
                    </div>
                    <div className="ext-sub">{it.host || it.file}{it.author ? ` · ${it.author}` : ""}{!it.valid && it.error ? ` · ${it.error}` : ""}</div>
                  </div>
                  <div className="ext-actions">
                    {it.valid && (
                      <button className={"icon-btn sm" + (it.enabled ? " on" : "")} title={it.enabled ? "Disable" : "Enable"} onClick={() => toggle(it)}>
                        {it.enabled ? <window.IconEye size={14} /> : <window.IconEyeOff size={14} />}
                      </button>
                    )}
                    <button className="icon-btn sm" title="Remove" onClick={() => remove(it)}><window.IconTrash size={13} /></button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="settings-section">
          <div className="settings-section-title">Generate from AI</div>
          <div className="settings-section-hint">
            Already imported a site with the AI source? Turn what the AI learned into a
            shareable extension (that then works without an LLM). Paste a series URL from that site.
          </div>
          <div className="addlink-url-row">
            <input
              className="addlink-input"
              placeholder="https://a-site-you-imported-with-AI.com/manga/…"
              value={aiUrl}
              onChange={(e) => setAiUrl(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && aiUrl.trim()) generateFromAi(); }}
            />
            <button className="btn sm" disabled={aiBusy || !aiUrl.trim()} onClick={generateFromAi}>
              {aiBusy ? "Generating…" : "Generate"}
            </button>
          </div>
        </div>

        <div className="settings-section">
          <div className="settings-section-title">Install an extension</div>
          <div className="settings-section-hint">Paste an extension manifest (JSON) below, or generate one above.</div>
          <textarea
            className="ext-textarea"
            placeholder='{ "manifest_version": 1, "id": "...", "name": "...", "type": "html", ... }'
            value={json}
            onChange={(e) => setJson(e.target.value)}
            spellCheck={false}
          />
          <div className="settings-pw-actions">
            <button className="btn primary sm" disabled={busy || !json.trim()} onClick={install}>
              {busy ? "Installing…" : "Install"}
            </button>
          </div>
        </div>
      </div>
    </window.Modal>
  );
}

// ---------------------------------------------------------------------------
// Settings + library management modal.
function SettingsModal({ libraries, activeLib, onClose, onAddLibrary, onChanged, onSwitch, onShowSwitcher }) {
  const [startMode, setStartMode] = useState(window.Settings.startMode);
  const [confirmPrivate, setConfirmPrivate] = useState(window.Settings.confirmPrivate);
  const [showSwitcher, setShowSwitcher] = useState(window.Settings.showSwitcher);
  const [busy, setBusy] = useState(false);
  const [rescanningId, setRescanningId] = useState(null);   // library id currently rescanning
  const [extOpen, setExtOpen] = useState(false);            // Source extensions manager open?

  // Password gate state.
  const [pwSet, setPwSet] = useState(false);
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwMsg, setPwMsg] = useState("");
  useEffect(() => {
    window.ApiClient.authStatus().then((s) => setPwSet(!!(s && s.password_set))).catch(() => {});
  }, []);

  function setStart(mode) { setStartMode(mode); window.Settings.set({ startMode: mode }); }
  function setConfirm(v) { setConfirmPrivate(v); window.Settings.set({ confirmPrivate: v }); }
  function setSwitcher(v) { setShowSwitcher(v); window.Settings.set({ showSwitcher: v }); onShowSwitcher(v); }

  async function savePassword(clear) {
    setPwMsg("");
    setPwBusy(true);
    try {
      const next = clear ? "" : pwNew;
      const res = await window.ApiClient.setPassword(pwCurrent, next);
      setPwSet(!!(res && res.password_set));
      setPwCurrent(""); setPwNew("");
      setPwMsg(clear ? "Password removed." : "Password saved.");
    } catch (ex) {
      setPwMsg(ex.message || String(ex));
    } finally {
      setPwBusy(false);
    }
  }

  function rename(lib) {
    const name = window.prompt("Library name:", lib.name);
    if (name == null || !name.trim() || name.trim() === lib.name) return;
    setBusy(true);
    window.ApiClient.updateLibrary(lib.id, { name: name.trim() })
      .then(onChanged).finally(() => setBusy(false));
  }
  function togglePrivate(lib) {
    setBusy(true);
    window.ApiClient.updateLibrary(lib.id, { private: !lib.private })
      .then(onChanged).finally(() => setBusy(false));
  }
  function makeDefault(lib) {
    setBusy(true);
    window.ApiClient.setDefaultLibrary(lib.id).then(onChanged).finally(() => setBusy(false));
  }
  // Rescan just one library, polling the background scan until it finishes.
  function rescanOne(lib) {
    if (rescanningId) return;
    setRescanningId(lib.id);
    window.ApiClient.rescan(null, null, lib.id)
      .then(() => {
        const poll = () => window.ApiClient.rescanStatus().then((s) => {
          if (s.running) { setTimeout(poll, 800); return; }
          setRescanningId(null);
          if (s.error) window.toast(`Rescan failed: ${s.error}`, "error");
          onChanged();   // refresh counts
        }).catch(() => setRescanningId(null));
        poll();
      })
      .catch((e) => { setRescanningId(null); window.toast(`Rescan failed: ${e.message || e}`, "error"); });
  }

  return (
    <window.Modal onClose={onClose} panelClass="settings-modal" labelledBy="settings-title">
        <div className="settings-head">
          <h2 id="settings-title"><window.IconGear size={20} /> Settings</h2>
          <button className="icon-btn" onClick={onClose} title="Close"><window.IconClose size={16} /></button>
        </div>

        <div className="settings-body">
          <div className="settings-section">
            <div className="settings-section-title">Libraries</div>
            <div className="settings-section-hint">Each library is a separate folder. Switch between them with the floating button.</div>
            <div className="settings-lib-list">
              {libraries.map((l) => (
                <div key={l.id} className={"settings-lib-row" + (l.id === activeLib ? " active" : "")}>
                  <span className={"settings-lib-dot" + (l.online ? " on" : "")} title={l.online ? "Online" : "Folder offline"} />
                  <div className="settings-lib-main">
                    <div className="settings-lib-name">
                      <span className="settings-lib-name-text">{l.name}</span>
                      {l.id === activeLib && <span className="settings-lib-badge active" title="Active"><span className="badge-dot" /><span className="badge-text">active</span></span>}
                      {l.private && <span className="settings-lib-badge private" title="Private"><window.IconLock size={9} /><span className="badge-text">private</span></span>}
                      {l.is_default && <span className="settings-lib-badge def" title="Default"><span className="badge-glyph">★</span><span className="badge-text">default</span></span>}
                    </div>
                    <div className="settings-lib-path" title={l.path}>{l.path} · {l.count} series</div>
                  </div>
                  <div className="settings-lib-actions">
                    {l.id !== activeLib && <button className="btn ghost sm" disabled={busy || !!rescanningId} onClick={() => onSwitch(l.id)}>Open</button>}
                    <button className={"icon-btn sm" + (rescanningId === l.id ? " spinning" : "")} disabled={busy || !!rescanningId} onClick={() => rescanOne(l)} title="Rescan this library"><window.IconCycle size={13} /></button>
                    <button className="icon-btn sm" disabled={busy || !!rescanningId} onClick={() => rename(l)} title="Rename"><window.IconPencil size={13} /></button>
                    <button className={"icon-btn sm" + (l.private ? " on" : "")} disabled={busy || !!rescanningId} onClick={() => togglePrivate(l)} title={l.private ? "Mark public" : "Mark private"}><window.IconLock size={13} /></button>
                    {!l.is_default && <button className="icon-btn sm" disabled={busy || !!rescanningId} onClick={() => makeDefault(l)} title="Set as default">★</button>}
                  </div>
                </div>
              ))}
            </div>
            <button className="btn sm settings-add-btn" onClick={onAddLibrary}>+ Add a library folder</button>
          </div>

          <div className="settings-section">
            <div className="settings-section-title">Import sources</div>
            <div className="settings-section-hint">Add support for more manga sites by installing Source extensions.</div>
            <button className="btn sm settings-add-btn" onClick={() => setExtOpen(true)}>
              <window.IconLibrary size={13} /> Manage Source extensions
            </button>
          </div>
          {extOpen && <SourceExtensionsModal onClose={() => setExtOpen(false)} />}

          <div className="settings-section">
            <div className="settings-section-title">On startup, open</div>
            <label className="settings-radio">
              <input type="radio" name="startmode" checked={startMode === "default"} onChange={() => setStart("default")} />
              <span><b>The default library</b><br /><span className="settings-radio-hint">Safe — never opens private content when you open the app in public.</span></span>
            </label>
            <label className="settings-radio">
              <input type="radio" name="startmode" checked={startMode === "last"} onChange={() => setStart("last")} />
              <span><b>The last library I used</b><br /><span className="settings-radio-hint">Convenient, but may reopen private content where you left off.</span></span>
            </label>
          </div>

          <div className="settings-section">
            <div className="settings-section-title">Behaviour</div>
            <div className="settings-toggle-row">
              <div className="settings-toggle-label">
                <div>Show the floating switch button</div>
                <div className="settings-toggle-hint">A round button in the corner to quick-switch libraries.</div>
              </div>
              <Toggle checked={showSwitcher} onChange={setSwitcher} />
            </div>
            <div className="settings-toggle-row">
              <div className="settings-toggle-label">
                <div>Confirm before opening a private library</div>
                <div className="settings-toggle-hint">Asks "are you sure?" before switching into a library marked private.</div>
              </div>
              <Toggle checked={confirmPrivate} onChange={setConfirm} />
            </div>
          </div>

          <div className="settings-section">
            <div className="settings-section-title">Security</div>
            <div className="settings-section-hint">
              {pwSet
                ? "A password is required to open the app on any device — even on your Tailnet."
                : "Set a password so only people who know it can open the app, even if they can reach this device on your Tailnet."}
            </div>
            {pwSet && (
              <window.PasswordInput
                className="settings-pw-input"
                placeholder="Current password"
                value={pwCurrent}
                onChange={(e) => setPwCurrent(e.target.value)}
                autoComplete="current-password"
              />
            )}
            <window.PasswordInput
              className="settings-pw-input"
              placeholder={pwSet ? "New password (leave blank to keep)" : "Choose a password"}
              value={pwNew}
              onChange={(e) => setPwNew(e.target.value)}
              autoComplete="new-password"
            />
            {pwMsg && <div className="settings-pw-msg">{pwMsg}</div>}
            <div className="settings-pw-actions">
              <button
                className="btn primary sm"
                disabled={pwBusy || !pwNew || (pwSet && !pwCurrent)}
                onClick={() => savePassword(false)}
              >
                {pwBusy ? "Saving…" : pwSet ? "Change password" : "Set password"}
              </button>
              {pwSet && (
                <button
                  className="btn ghost sm"
                  disabled={pwBusy || !pwCurrent}
                  onClick={() => savePassword(true)}
                  title="Remove the password (app opens for anyone who can reach it)"
                >
                  Remove password
                </button>
              )}
            </div>

            {pwSet && (
              <div className="settings-logout-row">
                <div className="settings-toggle-hint">
                  This device is remembered. Log out to require the password here again.
                </div>
                <button
                  className="btn ghost sm"
                  onClick={async () => {
                    const ok = await window.confirmDialog({
                      title: "Log out this device?",
                      message: "You'll need to enter the password again to open the app on this device.",
                      confirmLabel: "Log out",
                    });
                    if (!ok) return;
                    window.ApiClient.logout();
                    location.reload();
                  }}
                >
                  Log out this device
                </button>
              </div>
            )}
          </div>
        </div>
    </window.Modal>
  );
}

// Import-from-link modal. Paste a series URL → preview its metadata → pick a
// target library → download in the background (polling scrape status) → the
// server rescans and the new series appears.
function AddLinkModal({ libraries, activeLib, initialUrl, onClose, onStarted }) {
  const [url, setUrl] = useState(initialUrl || "");
  const [preview, setPreview] = useState(null);   // {source,title,author,chapters,...}
  const [previewing, setPreviewing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState("");
  const [libId, setLibId] = useState(activeLib || (libraries[0] && libraries[0].id) || null);
  const [sources, setSources] = useState([]);   // supported sources for the hint

  // Auto-preview when opened from a pasted link, so it's one step, not two.
  useEffect(() => { if (initialUrl && initialUrl.trim()) doPreview(); }, []);
  useEffect(() => { window.ApiClient.listSources().then(setSources).catch(() => {}); }, []);

  async function doPreview() {
    setErr(""); setPreview(null); setPreviewing(true);
    try {
      setPreview(await window.ApiClient.scrapePreview(url.trim()));
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setPreviewing(false);
    }
  }

  // Kick off the import, then hand progress to the app-level indicator and close
  // — so you can keep browsing while it downloads.
  function startImport() {
    if (libId == null) { setErr("Pick a target library."); return; }
    setErr(""); setStarting(true);
    window.ApiClient.scrapeStart(url.trim(), libId, preview.title)
      .then((res) => onStarted?.(preview.title, res && res.id))
      .catch((e) => { setStarting(false); setErr(e.message || String(e)); });
  }

  // For an already-imported series: sync new chapters into the existing copy
  // instead of downloading a fresh one. (Re-sync runs on the import queue.)
  function startSync() {
    setErr(""); setStarting(true);
    window.ApiClient.resyncSeries(preview.already.id)
      .then((res) => onStarted?.(preview.title, res && res.id))
      .catch((e) => { setStarting(false); setErr(e.message || String(e)); });
  }

  return (
    <window.Modal onClose={onClose} panelClass="rescan-modal" labelledBy="addlink-title">
      <div className="rescan-head">
        <h2 id="addlink-title">Import from a link</h2>
        <button className="icon-btn" onClick={onClose} title="Close"><window.IconClose size={16} /></button>
      </div>

      <div className="addlink-body">
        <div className="addlink-group">
          <span className="rescan-label">Series URL</span>
          <div className="addlink-url-row">
            <input
              className="addlink-input"
              placeholder="https://mangadex.org/title/…"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && url.trim()) doPreview(); }}
            />
            <button className="btn sm" disabled={!url.trim() || previewing} onClick={doPreview}>
              {previewing ? "Reading…" : "Preview"}
            </button>
          </div>
          {!preview && sources.length > 0 && (
            <div className="addlink-sources">
              <span className="addlink-sources-label">Supported sources</span>
              {sources.map((s) => (
                <div className="addlink-src-row" key={s.name}>
                  <span className="addlink-src-name">{s.label}</span>
                  {s.example && <span className="addlink-src-ex">{s.example}</span>}
                </div>
              ))}
            </div>
          )}
        </div>

        {preview && (
          <div className="addlink-preview">
            <div className="addlink-preview-cover">
              {preview.cover_url
                ? <img src={preview.cover_url} alt="" referrerPolicy="no-referrer" />
                : <div className="addlink-preview-noart">本</div>}
            </div>
            <div className="addlink-preview-meta">
              <div className="addlink-preview-title">{preview.title}</div>
              <div className="addlink-preview-sub">
                {preview.source}{preview.author ? ` · ${preview.author}` : ""} · {preview.chapters} chapters
              </div>
              {preview.genres && preview.genres.length > 0 && (
                <div className="addlink-preview-genres">{preview.genres.slice(0, 6).join(" · ")}</div>
              )}
            </div>
          </div>
        )}

        {preview && (
          <div className="addlink-group">
            <span className="rescan-label">Add to library</span>
            <window.Dropdown
              value={(libraries.find((l) => l.id === libId) || {}).name || ""}
              options={libraries.map((l) => l.name)}
              onChange={(name) => {
                const l = libraries.find((x) => x.name === name);
                if (l) setLibId(l.id);
              }}
              width={220}
            />
          </div>
        )}

        {preview && preview.already && (
          <div className="addlink-dup">
            <window.IconCheck size={13} /> Already in your library{preview.already.library ? ` · ${preview.already.library}` : ""}.
          </div>
        )}

        {preview && (preview.already ? (
          <div className="addlink-actions">
            <button className="btn primary addlink-import" disabled={starting} onClick={startSync}>
              {starting ? "Starting…" : "Sync new chapters"}
            </button>
            <button className="btn ghost sm" disabled={starting} onClick={startImport}>Import a fresh copy</button>
          </div>
        ) : (
          <button className="btn primary addlink-import" disabled={starting} onClick={startImport}>
            {starting ? "Starting…" : `Import ${preview.chapters} chapters`}
          </button>
        ))}
        {preview && (
          <div className="addlink-hint">Runs in the background — you can keep using the app.</div>
        )}

        {err && <div className="rescan-err">{err}</div>}
      </div>
    </window.Modal>
  );
}

// Folder-browser + rescan modal. Browse the server's disk to pick a library
// folder (or rescan existing ones), then kick off a background scan and poll
// until it finishes.
function RescanModal({ onClose, onDone }) {
  const [path, setPath] = useState("");        // "" = drive roots
  const [parent, setParent] = useState(null);
  const [dirs, setDirs] = useState([]);
  const [browseErr, setBrowseErr] = useState(null);
  const [libs, setLibs] = useState([]);
  const [phase, setPhase] = useState("idle");  // idle | scanning | done | error
  const [msg, setMsg] = useState("");
  const pollRef = useRef(null);

  function load(p) {
    setBrowseErr(null);
    window.ApiClient.browse(p)
      .then((r) => { setPath(r.path); setParent(r.parent); setDirs(r.dirs || []); })
      .catch((e) => setBrowseErr(e.message || String(e)));
  }

  useEffect(() => {
    load("");
    window.ApiClient.listLibraries().then(setLibs).catch(() => {});
    return () => clearTimeout(pollRef.current);
  }, []);

  function poll() {
    window.ApiClient.rescanStatus().then((s) => {
      if (s.running) {
        setMsg("Scanning folders on disk…");
        pollRef.current = setTimeout(poll, 800);
      } else if (s.error) {
        setPhase("error"); setMsg(s.error);
      } else {
        setPhase("done"); setMsg(`Done — ${s.series} series found.`);
        setTimeout(onDone, 700);
      }
    }).catch((e) => { setPhase("error"); setMsg(e.message || String(e)); });
  }

  function startScan(addPath) {
    setPhase("scanning"); setMsg("Starting…");
    window.ApiClient.rescan(addPath)
      .then(() => poll())
      .catch((e) => { setPhase("error"); setMsg(e.message || String(e)); });
  }

  const busy = phase === "scanning";

  return (
    <window.Modal onClose={onClose} panelClass="rescan-modal" labelledBy="rescan-title" dismissable={!busy}>
        <div className="rescan-head">
          <h2 id="rescan-title">Rescan library</h2>
          <button className="icon-btn" onClick={onClose} disabled={busy} title="Close"><window.IconClose size={16} /></button>
        </div>

        {libs.length > 0 && (
          <div className="rescan-libs">
            <span className="rescan-label">Current libraries</span>
            {libs.map((l) => (
              <div key={l.id} className="rescan-lib-row">
                <span className={"rescan-dot" + (l.online ? " on" : "")} />
                <span className="rescan-lib-path" title={l.path}>{l.path}</span>
                <span className="rescan-lib-state">{l.online ? "online" : "offline"}</span>
              </div>
            ))}
            <button className="btn sm" disabled={busy} onClick={() => startScan(null)}>
              <window.IconCycle size={13} /> Rescan all
            </button>
          </div>
        )}

        <div className="rescan-browser">
          <span className="rescan-label">Add / scan a folder</span>
          <div className="rescan-crumbs">
            <button className="rescan-crumb" disabled={busy} onClick={() => load("")}>Drives</button>
            {path && <span className="rescan-cur" title={path}>{path}</span>}
          </div>
          {browseErr && <div className="rescan-err">{browseErr}</div>}
          <div className="rescan-dirlist">
            {parent !== null && (
              <button className="rescan-diritem up" disabled={busy} onClick={() => load(parent)}>
                <window.IconArrowLeft size={13} /> ..
              </button>
            )}
            {dirs.length === 0 && <div className="rescan-empty">No subfolders.</div>}
            {dirs.map((d) => (
              <button key={d.path} className="rescan-diritem" disabled={busy} onClick={() => load(d.path)} title={d.path}>
                {d.name}
              </button>
            ))}
          </div>
          {path && (
            <button className="btn primary sm" disabled={busy} onClick={() => startScan(path)}>
              Add &amp; scan “{path}”
            </button>
          )}
        </div>

        {phase !== "idle" && (
          <div className={"rescan-status " + phase}>
            {busy && <span className="rescan-spinner" />}
            {msg}
          </div>
        )}
    </window.Modal>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
