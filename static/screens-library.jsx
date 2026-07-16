/* global window, React */
const { useState: useStateScr, useMemo: useMemoScr } = React;

// Library view state (sort/filters/paging) persisted OUTSIDE the component so it
// survives the unmount that happens when you open a manga's detail screen and
// come back — without this, every "back" reset the filters to defaults. Scoped
// per active library so switching libraries starts fresh.
const _libView = {
  libId: undefined,
  sort: "Title A-Z",
  genre: "All Genres",
  author: "All Authors",
  seriesF: "All Series",
  tag: "All Tags",
  status: "All",
  language: "All Languages",
  parody: "All Parodies",
  favOnly: false,
  limit: 0,   // how many cards were expanded via "Load more" — restored so the
              // page is tall enough to return to the same scroll position
};
function _libViewFor(libId) {
  // Reset remembered filters when the active library changes.
  if (_libView.libId !== libId) {
    Object.assign(_libView, {
      libId, sort: "Title A-Z", genre: "All Genres", author: "All Authors",
      seriesF: "All Series", tag: "All Tags", status: "All",
      language: "All Languages", parody: "All Parodies", favOnly: false, limit: 0,
    });
  }
  return _libView;
}

// Landing — centered search, Continue Reading rail, New Reads rail.
function Landing({ onOpenDetail, onOpenSearch, onImportLink, onBrowse, density }) {
  const [q, setQ] = useStateScr("");
  const [delTick, setDelTick] = useStateScr(0);   // bump to re-render after a delete
  const items = window.STORE.items;

  // Continue Reading — series you're actively reading or plan to read, PLUS any
  // series that just re-synced new chapters while you were caught up
  // (fresh_chapters — those may sit at "Completed" and would otherwise be
  // filtered out; they're exactly the ones you want to jump back into). Fresh
  // ones lead the rail; then Reading before Planned, most recent first.
  const continueRail = items
    .filter((c) => c.fresh_chapters || c.status === "Reading" || c.status === "Planned to Read")
    .sort((a, b) => {
      if (!!b.fresh_chapters !== !!a.fresh_chapters) return b.fresh_chapters ? 1 : -1;
      const rank = (s) => (s === "Reading" ? 0 : 1);
      if (rank(a.status) !== rank(b.status)) return rank(a.status) - rank(b.status);
      return String(b.last_read || "").localeCompare(String(a.last_read || ""));
    })
    .slice(0, 4);

  // New Reads — 4 random series you've never started (Not Started, never opened),
  // so it surfaces things to pick up. Shuffled; keyed on library size so it
  // re-rolls when data loads/refreshes, not on every keystroke.
  const newReads = useMemoScr(() => {
    const pool = items.filter((c) => c.read === 0 && c.status === "Not Started");
    for (let i = pool.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [pool[i], pool[j]] = [pool[j], pool[i]];
    }
    return pool.slice(0, 4);
  }, [items.length]);

  function submit(e) {
    e.preventDefault();
    const v = q.trim();
    // Pasting a link imports it instead of searching.
    if (/^https?:\/\//i.test(v)) { onImportLink?.(v); return; }
    onOpenSearch?.(q);
  }

  return (
    <div className="landing-simple">
      <form className="landing-search" onSubmit={submit}>
        <input
          placeholder="Search series… or paste a link to import"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button className="btn primary" type="submit">Search</button>
      </form>

      <section className="landing-section">
        <h2 className="landing-h">Continue Reading</h2>
        <div className="rail-grid">
          {continueRail.length === 0
            ? <div className="rail-card-sub" style={{ gridColumn: "1/-1", textAlign: "center" }}>Nothing in progress yet.</div>
            : continueRail.map((c) => <window.RailCard key={c.id} item={c} onOpen={onOpenDetail} onDeleted={() => setDelTick((n) => n + 1)} />)}
        </div>
      </section>

      <section className="landing-section">
        <h2 className="landing-h">New Reads</h2>
        <div className="rail-grid">
          {newReads.map((c) => <window.NewReadsRailCard key={c.id} item={c} onOpen={onOpenDetail} onDeleted={() => setDelTick((n) => n + 1)} />)}
        </div>
      </section>

      <div style={{ display: "flex", justifyContent: "center" }}>
        <button className="btn" onClick={onBrowse}>Browse full library <window.IconArrowRight size={15} /></button>
      </div>
    </div>
  );
}

// How many cards to render at a time. Rendering thousands of parallax-tilt cards
// (each lazy-loading a cover) freezes the browser, so we page them in.
const PAGE_SIZE = 60;

// Library — filter/sort over the real library.
function Library({ onOpenDetail, onOpenSearch, density, setDensity }) {
  // Seed filters from the persisted view (kept across detail open/close), scoped
  // to the active library. Each setter mirrors its value back into _libView so
  // the next remount restores it.
  const v = _libViewFor(window.getActiveLibrary ? window.getActiveLibrary() : null);
  const mk = (key, setRaw) => (val) => {
    const next = typeof val === "function" ? val(_libView[key]) : val;
    _libView[key] = next;
    setRaw(next);
  };
  const [sort, setSortRaw] = useStateScr(v.sort);
  const [genre, setGenreRaw] = useStateScr(v.genre);
  const [author, setAuthorRaw] = useStateScr(v.author);
  const [seriesF, setSeriesFRaw] = useStateScr(v.seriesF);
  const [tag, setTagRaw] = useStateScr(v.tag);
  const [status, setStatusRaw] = useStateScr(v.status);
  const [language, setLanguageRaw] = useStateScr(v.language);
  const [parody, setParodyRaw] = useStateScr(v.parody);
  const setSort = mk("sort", setSortRaw);
  const setGenre = mk("genre", setGenreRaw);
  const setAuthor = mk("author", setAuthorRaw);
  const setSeriesF = mk("seriesF", setSeriesFRaw);
  const setTag = mk("tag", setTagRaw);
  const setStatus = mk("status", setStatusRaw);
  const setLanguage = mk("language", setLanguageRaw);
  const setParody = mk("parody", setParodyRaw);
  // Restore how far the list was expanded (for scroll restore on "back").
  const [limit, setLimitRaw] = useStateScr(v.limit && v.limit > PAGE_SIZE ? v.limit : PAGE_SIZE);
  const setLimit = mk("limit", setLimitRaw);
  // Favorites-only view. The nav "Favorites" button sets window.STORE._favOnly;
  // if that flag is set we force it on (and clear the flag); otherwise restore
  // the remembered value. Persisted like the other filters.
  const [favOnly, setFavOnlyRaw] = useStateScr(() => {
    if (window.STORE._favOnly) { window.STORE._favOnly = false; _libView.favOnly = true; return true; }
    return v.favOnly;
  });
  const setFavOnly = mk("favOnly", setFavOnlyRaw);

  const all = window.STORE.items;
  const facets = window.STORE.facets;

  // Bumped when a card is deleted so the list re-renders (the card mutates
  // STORE.items in place, which React can't observe on its own).
  const [delTick, setDelTick] = useStateScr(0);

  // Reset paging whenever filters/sort CHANGE — but not on the initial mount,
  // which would clobber the restored `limit` when returning from a detail.
  const firstRun = React.useRef(true);
  React.useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return; }
    setLimit(PAGE_SIZE);
  }, [sort, genre, status, author, seriesF, tag, language, parody, favOnly]);

  const items = useMemoScr(() => {
    let xs = [...all];
    if (favOnly) xs = xs.filter((x) => x.favorite);
    // "None" selects series that are MISSING that field (empty string / empty list).
    if (genre === "None") xs = xs.filter((x) => !x.genres || x.genres.length === 0);
    else if (genre !== "All Genres") xs = xs.filter((x) => x.genres.includes(genre));
    if (status !== "All") xs = xs.filter((x) => x.status === status);
    if (author === "None") xs = xs.filter((x) => !x.author);
    else if (author !== "All Authors") xs = xs.filter((x) => x.author === author);
    if (seriesF === "None") xs = xs.filter((x) => !x.series);
    else if (seriesF !== "All Series") xs = xs.filter((x) => x.series === seriesF);
    if (tag === "None") xs = xs.filter((x) => !x.tags || x.tags.length === 0);
    else if (tag !== "All Tags") xs = xs.filter((x) => x.tags.includes(tag));
    if (parody === "None") xs = xs.filter((x) => !x.parodies || x.parodies.length === 0);
    else if (parody !== "All Parodies") xs = xs.filter((x) => (x.parodies || []).includes(parody));
    if (language === "None") xs = xs.filter((x) => !x.language);
    else if (language !== "All Languages") xs = xs.filter((x) => x.language === language);
    if (sort === "Title A-Z") xs.sort((a, b) => a.title.localeCompare(b.title));
    if (sort === "Title Z-A") xs.sort((a, b) => b.title.localeCompare(a.title));
    if (sort === "Last Read") xs.sort((a, b) => String(b.last_read || "").localeCompare(String(a.last_read || "")));
    if (sort === "Date Added") xs.sort((a, b) => {
      // By real date_added (newest first); fall back to id when it's missing.
      const d = String(b.date_added || "").localeCompare(String(a.date_added || ""));
      return d !== 0 ? d : b.id - a.id;
    });
    if (sort === "Rating") xs.sort((a, b) => (b.rating||0) - (a.rating||0));
    return xs;
  }, [all, sort, genre, status, author, seriesF, tag, language, parody, favOnly, delTick]);

  return (
    <div className="page">
      <div className="filter-bar">
        {favOnly && (
          <button className="fav-filter-chip" onClick={() => setFavOnly(false)} title="Showing favorites only — click to clear">
            <window.IconStar size={13} /> Favorites
            <window.IconClose size={12} />
          </button>
        )}
        <div className="group">
          <span className="label">Sort</span>
          <window.Dropdown value={sort} onChange={setSort}
            options={["Title A-Z","Title Z-A","Last Read","Date Added","Rating"]} />
        </div>
        <div className="group">
          <span className="label">Status</span>
          <window.Dropdown value={status} onChange={setStatus}
            options={["All", ...facets.statuses]} />
        </div>
        <div className="group">
          <span className="label">Series</span>
          <window.Dropdown value={seriesF} onChange={setSeriesF}
            options={["All Series", "None", ...facets.series]} />
        </div>
        <div className="group">
          <span className="label">Genre</span>
          <window.Dropdown value={genre} onChange={setGenre}
            options={["All Genres", "None", ...facets.genres]} />
        </div>
        <div className="group">
          <span className="label">Author</span>
          <window.Dropdown value={author} onChange={setAuthor}
            options={["All Authors", "None", ...facets.authors]} />
        </div>
        <div className="group">
          <span className="label">Tag</span>
          <window.Dropdown value={tag} onChange={setTag}
            options={["All Tags", "None", ...facets.tags]} />
        </div>
        <div className="group">
          <span className="label">Parody</span>
          <window.Dropdown value={parody} onChange={setParody}
            options={["All Parodies", "None", ...(facets.parodies || [])]} />
        </div>
        <div className="group">
          <span className="label">Language</span>
          <window.Dropdown value={language} onChange={setLanguage}
            options={["All Languages", "None", ...(facets.languages || [])]} />
        </div>
        <div className="group density-group">
          <span className="label">Density</span>
          {/* Single cycling button (like the card status bubble) — clicking steps
              through the density levels, so the control takes one slot instead of
              four and the whole filter bar fits on one line. */}
          {(() => {
            const levels = ["spacious", "balanced", "dense", "tight"];
            const colsFor = (d) => (d === "spacious" ? 1 : d === "balanced" ? 2 : d === "dense" ? 3 : 4);
            const next = levels[(levels.indexOf(density) + 1) % levels.length];
            return (
              <button className="density-cycle" title={`Density: ${density} — click for ${next}`}
                      onClick={() => setDensity(next)}>
                <window.IconDensity cols={colsFor(density)} size={15} />
                <span className="density-cycle-label">{density}</span>
              </button>
            );
          })()}
        </div>
      </div>

      <div className={`grid density-${density} library-grid`}>
        {items.slice(0, limit).map((c) => <window.LibraryCard key={c.id} item={c} onOpen={onOpenDetail} onDeleted={() => setDelTick((n) => n + 1)} />)}
      </div>

      {items.length > limit && (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8, padding: "32px 0" }}>
          <button className="btn primary" onClick={() => setLimit((n) => n + PAGE_SIZE)}>
            Load more
          </button>
          <span style={{ fontSize: 12, color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
            Showing {Math.min(limit, items.length)} of {items.length}
          </span>
        </div>
      )}

      {items.length === 0 && (
        <div className="empty">
          <div className="glyph">無</div>
          <p>No series match the current filters.</p>
        </div>
      )}
    </div>
  );
}

Object.assign(window, { Landing, Library });
