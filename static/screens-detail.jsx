/* global window, React */
const { useState: useStateDtl, useEffect: useEffectDtl, useRef: useRefDtl } = React;

// Chapter cover thumb. A chapter's first page is sometimes corrupt/undecodable,
// so on a load error we advance to the next page (up to a few tries) before
// giving up and showing a placeholder tile — never a broken image.
function ChapterThumb({ pages, hue, kanji }) {
  const list = pages || [];
  const [idx, setIdx] = useStateDtl(0);
  const [failed, setFailed] = useStateDtl(false);
  const ref = useRefDtl(null);
  const MAX_TRIES = 4;

  function advance() {
    if (idx + 1 < list.length && idx + 1 < MAX_TRIES) setIdx(idx + 1);
    else setFailed(true);
  }

  useEffectDtl(() => {
    const el = ref.current;
    if (el && el.complete && el.naturalWidth === 0) advance();
  }, [idx]);

  if (list.length === 0 || failed) {
    return (
      <div className="img" style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        background: `linear-gradient(155deg, hsl(${hue || 220} 30% 22%), hsl(${hue || 220} 28% 13%))`,
        color: "rgba(255,255,255,0.7)",
      }}>
        <span style={{ fontFamily: "var(--font-serif-jp), serif", fontSize: 30, opacity: 0.8 }}>{kanji || "本"}</span>
      </div>
    );
  }
  return <img key={idx} ref={ref} className="img" src={window.pageUrl(list[idx], true)} alt="" loading="lazy" onError={advance} />;
}

// Parse a chapter folder name into { number, title }.
//   "Chap 003 - The Reveal" -> { number: 3, title: "The Reveal" }
//   "Chapter 5"             -> { number: 5, title: "" }
//   "Extras"                -> { number: null, title: "Extras" }
function parseChapterName(name) {
  const s = String(name || "").trim();
  const m = s.match(/^(?:chap(?:ter)?|ch|vol(?:ume)?|ep(?:isode)?)[\s._-]*0*(\d+)\s*(?:[-:—.]\s*)?(.*)$/i);
  if (m) return { number: parseInt(m[1], 10), title: (m[2] || "").trim() };
  const num = s.match(/^0*(\d+)$/);
  if (num) return { number: parseInt(num[1], 10), title: "" };
  return { number: null, title: s };
}

// One chapter card: cover, badges, label, and a hover edit button that opens an
// inline editor for the chapter number + title (renames the folder on disk).
function ChapterCard({ ch, displayIndex, item, read, stColor, canEdit, onOpen, onRename }) {
  const parsed = parseChapterName(ch.name);
  const [editing, setEditing] = useStateDtl(false);
  const [num, setNum] = useStateDtl(parsed.number == null ? "" : String(parsed.number));
  const [title, setTitle] = useStateDtl(parsed.title);
  const [busy, setBusy] = useStateDtl(false);

  function open(e) { e.stopPropagation(); setNum(parsed.number == null ? "" : String(parsed.number)); setTitle(parsed.title); setEditing(true); }
  function save(e) {
    e.stopPropagation();
    setBusy(true);
    const n = num.trim() === "" ? null : parseInt(num, 10);
    onRename(ch.name, Number.isNaN(n) ? null : n, title.trim())
      .then((ok) => { setBusy(false); if (ok) setEditing(false); });
  }

  return (
    <div
      className={"chapter-card" + (ch.isRead ? " done" : "") + (ch.inProgress ? " in-progress" : "")}
      onClick={() => !editing && onOpen()}
      onKeyDown={(e) => { if (!editing && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); onOpen(); } }}
      role="button"
      tabIndex={editing ? -1 : 0}
      aria-label={`Read ${ch.name}`}
    >
      <div className="chapter-thumb">
        <ChapterThumb pages={ch.pages} hue={item.hue} kanji={item.kanji} />
        <div className="chapter-overlay">
          {ch.isRead && <div className="chapter-badge done"><window.IconCheck size={13} /></div>}
          {ch.inProgress && <div className="chapter-badge progress"><window.IconDot size={10} /></div>}
        </div>
        {canEdit && !editing && (
          <button className="chapter-edit-btn" title="Rename chapter" onClick={open}>
            <window.IconPencil size={13} />
          </button>
        )}
        {ch.inProgress && (
          <div className="chapter-progress">
            <span style={{ width: `${Math.min(100, ((read - ch.startPage + 1) / ch.page_count) * 100)}%`, background: stColor }} />
          </div>
        )}
        {editing && (
          <div className="chapter-edit-panel" onClick={(e) => e.stopPropagation()}>
            <label>Number</label>
            <input type="number" value={num} placeholder="—" onChange={(e) => setNum(e.target.value)} autoFocus />
            <label>Title</label>
            <input type="text" value={title} placeholder="(optional)" onChange={(e) => setTitle(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") save(e); if (e.key === "Escape") setEditing(false); }} />
            <div className="chapter-edit-actions">
              <button className="btn ghost sm" disabled={busy} onClick={(e) => { e.stopPropagation(); setEditing(false); }}>Cancel</button>
              <button className="btn primary sm" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
            </div>
          </div>
        )}
      </div>
      <div className="chapter-meta">
        <span className="chapter-num">{ch.name.length > 14 ? `CH ${displayIndex + 1}` : ch.name}</span>
        <span className="chapter-pages">{ch.page_count}p</span>
      </div>
    </div>
  );
}

// Editable 1–5 star rating. Click a star to set; click the same star again to
// clear (back to 0). Hovering previews the value.
function StarRating({ value, onChange }) {
  const [hover, setHover] = useStateDtl(0);
  const shown = hover || value || 0;
  return (
    <div className="detail-rating" onMouseLeave={() => setHover(0)}>
      <span className="detail-rating-label">Rating</span>
      <div className="detail-rating-stars">
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            className={"detail-star" + (n <= shown ? " on" : "")}
            title={`${n} star${n > 1 ? "s" : ""}`}
            onMouseEnter={() => setHover(n)}
            onClick={() => onChange(n === value ? 0 : n)}
          >
            {n <= shown ? <window.IconStar size={20} /> : <window.IconStarOutline size={20} />}
          </button>
        ))}
        {value > 0 && <span className="detail-rating-num">{value}/5</span>}
      </div>
    </div>
  );
}

// The series title H1 — click the pencil (or the title) to rename. Renaming
// renames the folder on disk, so onSave is async and may fail/revert.
function EditableTitle({ title, onSave }) {
  const [editing, setEditing] = useStateDtl(false);
  const [draft, setDraft] = useStateDtl(title);
  useEffectDtl(() => { setDraft(title); }, [title]);

  function commit() {
    setEditing(false);
    const v = draft.trim();
    if (v && v !== title) onSave(v);
  }

  if (editing) {
    return (
      <input
        className="detail-h1-input" autoFocus value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") commit(); if (e.key === "Escape") { setDraft(title); setEditing(false); } }}
      />
    );
  }
  return (
    <h1 className="detail-h1 editable" onClick={() => setEditing(true)} title="Click to rename (renames the folder on disk)">
      {title}
      <button className="detail-h1-edit" onClick={(e) => { e.stopPropagation(); setEditing(true); }} title="Rename series">
        <window.IconPencil size={15} />
      </button>
    </h1>
  );
}

// Move a series to another library. Physically moves its folder on disk
// (copy → verify → delete original). Kicks off the move, then hands progress to
// the app-level indicator and closes, so you can keep browsing while it runs.
function MoveModal({ item, libraries, onClose, onStarted }) {
  const [libId, setLibId] = useStateDtl(libraries[0] ? libraries[0].id : null);
  const [starting, setStarting] = useStateDtl(false);
  const [err, setErr] = useStateDtl("");

  async function start() {
    if (libId == null) return;
    const target = libraries.find((l) => l.id === libId);
    const ok = await window.confirmDialog({
      title: `Move “${item.title}”?`,
      message: `This moves the series' files on disk into “${target ? target.name : "the selected library"}”. Large series can take a while; the original is kept until the copy is verified. You can keep using the app while it runs.`,
      confirmLabel: "Move",
      tone: "warn",
    });
    if (!ok) return;
    setErr(""); setStarting(true);
    window.ApiClient.moveSeries(item.id, libId)
      .then(() => onStarted?.(item.title))
      .catch((e) => { setStarting(false); setErr(e.message || String(e)); });
  }

  return (
    <window.Modal onClose={onClose} panelClass="rescan-modal" labelledBy="move-title">
      <div className="rescan-head">
        <h2 id="move-title">Move to another library</h2>
        <button className="icon-btn" onClick={onClose} title="Close"><window.IconClose size={16} /></button>
      </div>
      <div className="addlink-body">
        <div className="addlink-group">
          <span className="rescan-label">Move to</span>
          <window.Dropdown
            value={(libraries.find((l) => l.id === libId) || {}).name || ""}
            options={libraries.map((l) => l.name)}
            onChange={(name) => { const l = libraries.find((x) => x.name === name); if (l) setLibId(l.id); }}
            width={220}
          />
        </div>
        <button className="btn primary addlink-import" disabled={starting || libId == null} onClick={start}>
          {starting ? "Starting…" : "Move"}
        </button>
        <div className="addlink-hint">Runs in the background — you can keep using the app.</div>
        {err && <div className="rescan-err">{err}</div>}
      </div>
    </window.Modal>
  );
}

function Detail({ item: initialItem, libraries = [], refreshSignal, onBack, onMoveStarted, onOpenReader }) {
  // Load the full series record (with real chapters) on mount.
  const [item, setItem] = useStateDtl(initialItem);
  const [moveOpen, setMoveOpen] = useStateDtl(false);
  const [resync, setResync] = useStateDtl(null);   // {message,error,done} while syncing
  const [loading, setLoading] = useStateDtl(!initialItem.chapters);
  const [tab, setTab] = useStateDtl("chapters");
  const [fav, setFav] = useStateDtl(!!initialItem.favorite);
  const [status, setStatus] = useStateDtl(initialItem.status || "Not Started");
  // Bumped when facets refresh after a metadata save, forcing a re-render so the
  // pill usage-counts (e.g. how many series use "Fairy Tail") reflect the new value.
  const [facetTick, setFacetTick] = useStateDtl(0);
  // Render chapters in chunks of exactly 4 rows. Each card has a thumbnail (a PDF
  // page render for PDF chapters), so mounting many at once lags — but the column
  // count differs per breakpoint (8 desktop → 2 tiny phone), so a fixed number
  // would leave the grid half-empty on wide screens. We measure the live column
  // count from the grid and use cols × ROWS_PER_PAGE so 4 rows fill on every device.
  const ROWS_PER_PAGE = 4;
  // Column count used to page chapters in whole rows. The live grid is the single
  // source of truth: we read its actual rendered column count from
  // getComputedStyle so a change to the .chapter-grid CSS breakpoints can never
  // desync the pagination math. `colsForWidth` is only a fallback seed for the
  // first render (before the grid exists) and if the measurement isn't available.
  const chapterGridRef = useRefDtl(null);
  function colsForWidth(w) {
    if (w <= 380) return 2;
    if (w <= 640) return 3;
    if (w <= 1024) return 4;
    if (w <= 1280) return 6;
    return 8;
  }
  // Measure the grid's real column count from its computed template; 0 if not
  // measurable yet (grid absent/empty), so callers can fall back to width.
  function measureCols() {
    const el = chapterGridRef.current;
    if (!el) return 0;
    const tmpl = getComputedStyle(el).gridTemplateColumns || "";
    const n = tmpl.split(" ").filter((t) => t && t !== "0px").length;
    return n > 0 ? n : 0;
  }
  const [cols, setCols] = useStateDtl(() => colsForWidth(window.innerWidth));
  const [chapLimit, setChapLimit] = useStateDtl(() => colsForWidth(window.innerWidth) * ROWS_PER_PAGE);

  // Keep `cols` synced to the live grid. A ResizeObserver on the grid fires on
  // width changes AND once it first has layout, so we don't need a separate
  // resize listener or the width→breakpoint mirror.
  useEffectDtl(() => {
    function sync() {
      const n = measureCols() || colsForWidth(window.innerWidth);
      setCols((prev) => {
        if (n === prev) return prev;
        // Round the visible count up to whole rows for the new column count.
        setChapLimit((cur) => Math.max(n * ROWS_PER_PAGE, Math.ceil(cur / n) * n));
        return n;
      });
    }
    sync();
    const el = chapterGridRef.current;
    let ro = null;
    if (el && typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(sync);
      ro.observe(el);
    } else {
      window.addEventListener("resize", sync);
    }
    return () => {
      if (ro) ro.disconnect();
      else window.removeEventListener("resize", sync);
    };
  }, [loading]);   // re-run once chapters load and the grid actually renders

  // Patch the matching object in the shared library store so the library/landing
  // reflect changes (the STORE is an intentional shared cache). We do NOT touch
  // the component's own `item` here — local rendering goes through setItem so
  // React stays the source of truth and we never mutate state in place.
  function patchStoreItem(patch) {
    const it = (window.STORE.items || []).find((x) => x.id === item.id);
    if (it) Object.assign(it, patch);
  }

  // Optimistic status change with revert on failure.
  function changeStatus(next) {
    const prev = status;
    setStatus(next);
    setItem((p) => ({ ...p, status: next }));
    patchStoreItem({ status: next });
    window.ApiClient.setStatus(item.id, next).catch((e) => {
      setStatus(prev);
      setItem((p) => ({ ...p, status: prev }));
      patchStoreItem({ status: prev });
      window.toast(`Status update failed: ${e.message || e}`, "error");
    });
  }

  // Save a metadata field (tags/genres/author/series/language). Optimistic: UI
  // updates immediately, then reverts (and alerts) if the server rejects it.
  function saveField(field, value) {
    const prev = field === "series" ? item.series : item[field];
    const apply = (v) => {
      setItem((p) => ({ ...p, [field]: v, ...(field === "series" ? { series: v } : {}) }));
      patchStoreItem(field === "series" ? { series: v, series_name: v } : { [field]: v });
    };
    apply(value);
    window.ApiClient.saveMetadata(item.id, { [field]: value })
      .then(() => {
        // Refresh the facets so a newly-added series/tag/genre shows up in the
        // filter dropdowns + autocomplete right away (otherwise it only appears
        // after a full reload). Fire-and-forget; the value is already applied.
        if (window.ApiClient.getFacets) {
          window.ApiClient.getFacets()
            .then((f) => { if (f) { window.STORE.facets = f; setFacetTick((n) => n + 1); } })
            .catch(() => {});
        }
      })
      .catch((e) => {
        apply(prev);
        window.toast(`Save failed: ${e.message || e}`, "error");
      });
  }

  // Rename the series. This renames the folder on disk, so we wait for the server
  // and refresh from its response (folder_path changes too). Reverts on failure.
  function renameTitle(newName) {
    const name = (newName || "").trim();
    if (!name || name === item.title) return;
    const prevTitle = item.title;
    setItem((p) => ({ ...p, title: name }));         // optimistic
    patchStoreItem({ title: name });
    window.ApiClient.renameSeries(item.id, name)
      .then((r) => {
        if (r && r.series && window.normalize) {
          const full = window.normalize(r.series);
          setItem((p) => ({ ...p, ...full }));
          patchStoreItem({ title: full.title, folder_path: full.folder_path });
        }
      })
      .catch((e) => {
        setItem((p) => ({ ...p, title: prevTitle }));  // revert
        patchStoreItem({ title: prevTitle });
        window.toast(`Rename failed: ${e.message || e}`, "error");
      });
  }

  // Rename a chapter folder (number + title). The rename and the refresh are
  // handled separately: if the rename succeeds we report success even if the
  // follow-up refresh fails (a transient refresh error must NOT be reported as a
  // rename failure — the folder already changed on disk).
  async function renameChapter(oldName, number, title) {
    try {
      await window.ApiClient.renameChapter(item.id, oldName, number, title);
    } catch (e) {
      window.toast(`Chapter rename failed: ${e.message || e}`, "error");
      return false;
    }
    try {
      const full = await window.ApiClient.getSeries(item.id);
      setItem(full);
    } catch (e) {
      // Rename succeeded; refresh didn't. Keep the success result.
    }
    return true;
  }

  // Re-fetch the authoritative series record on mount AND whenever refreshSignal
  // changes — the parent bumps it after the reader closes, so returning to a
  // still-mounted Detail picks up the new read-progress (chapter read/in-progress
  // marks and the % bar) live, instead of staying stale until you leave and reopen.
  useEffectDtl(() => {
    let alive = true;
    window.ApiClient.getSeries(initialItem.id)
      .then((full) => {
        if (!alive) return;
        setItem(full);
        setLoading(false);
        setStatus(full.status || "Not Started");
        setFav(!!full.favorite);  // resync fav from the authoritative server record
      })
      .catch(() => setLoading(false));
    return () => { alive = false; };
  }, [initialItem.id, refreshSignal]);

  const stColor = window.STATUS_COLOR[status];
  const chapters = item.chapters || [];
  const totalPages = item.total_pages || item.pages || 0;
  const read = Math.min(item.read || 0, totalPages || item.read || 0);
  const pct = totalPages ? Math.round((read / totalPages) * 100) : 0;
  // While chapters are still loading, show a neutral "—" instead of a concrete
  // "0 chapters" / "0%" — the zeros read as "this series is empty/broken" for a
  // beat before the real numbers resolve. Placeholders make it read as "loading".
  const chapCountText = loading ? "—" : `${chapters.length}`;
  const pctText = loading ? "—" : `${pct}%`;

  // Compute each chapter's global start page (1-based) across the flattened series.
  let runningStart = 1;
  const chapterRows = chapters.map((ch) => {
    const startPage = runningStart;
    const endPage = runningStart + ch.page_count - 1;
    runningStart = endPage + 1;
    const isRead = endPage <= read && read > 0;
    const inProgress = !isRead && startPage <= read && read > 0;
    return { ...ch, startPage, endPage, isRead, inProgress };
  });

  // The chapter the reader should open to when "Continue" is pressed: the one
  // containing the last-read page (or the first chapter if nothing read yet).
  // Continue must open a SINGLE chapter — opening with no endPage exposed the
  // whole flattened series, which is not how chapters should read.
  const continueChapter =
    chapterRows.find((c) => read >= c.startPage && read <= c.endPage) || chapterRows[0];

  function toggleFav() {
    const next = !fav;
    setFav(next);
    setItem((p) => ({ ...p, favorite: next }));
    patchStoreItem({ favorite: next });
    window.ApiClient.setFavorite(item.id, next).catch((e) => {
      setFav(!next);
      setItem((p) => ({ ...p, favorite: !next }));
      patchStoreItem({ favorite: !next });
      window.toast(`Favorite update failed: ${e.message || e}`, "error");
    });
  }

  // Custom cover upload. Uploads the chosen image, then forces every <img> for
  // this series to refetch by bumping a cache-bust version (coverBust state).
  const coverInputRef = useRefDtl(null);
  const [coverBusy, setCoverBusy] = useStateDtl(false);
  const [coverBust, setCoverBust] = useStateDtl(0);
  function pickCover() { if (coverInputRef.current) coverInputRef.current.click(); }
  function onCoverChosen(e) {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";   // allow re-picking the same file later
    if (!file) return;
    setCoverBusy(true);
    window.ApiClient.uploadCover(item.id, file)
      .then(() => { window.bumpCover(item.id); setCoverBust((n) => n + 1); })
      .catch((err) => window.toast(`Cover upload failed: ${err.message || err}`, "error"))
      .finally(() => setCoverBusy(false));
  }
  async function resetCover() {
    const ok = await window.confirmDialog({
      title: "Revert cover?",
      message: "Remove the custom cover and go back to the auto one.",
      confirmLabel: "Revert",
    });
    if (!ok) return;
    setCoverBusy(true);
    window.ApiClient.removeCover(item.id)
      .then(() => { window.bumpCover(item.id); setCoverBust((n) => n + 1); })
      .catch((err) => window.toast(`Could not remove cover: ${err.message || err}`, "error"))
      .finally(() => setCoverBusy(false));
  }

  // Check the origin for new chapters and download them (progress on the shared
  // import status endpoint). On success, reload the series so new chapters show.
  function doResync() {
    if (resync && resync.running) return;
    setResync({ running: true, message: "Checking for new chapters…" });
    const poll = () => window.ApiClient.scrapeStatus().then((s) => {
      if (s.running) { setResync({ running: true, message: s.message }); setTimeout(poll, 1000); return; }
      if (s.error) { setResync({ running: false, error: s.error }); return; }
      setResync({ running: false, message: "Up to date." });
      window.ApiClient.getSeries(item.id).then(setItem).catch(() => {});
      setTimeout(() => setResync(null), 2500);
    }).catch((e) => setResync({ running: false, error: e.message || String(e) }));
    window.ApiClient.resyncSeries(item.id)
      .then(() => poll())
      .catch((e) => setResync({ running: false, error: e.message || String(e) }));
  }

  const origin = item.origin;
  const otherLibs = libraries.filter((l) => l.id !== item.library_id);

  const continueLabel =
    read > 0 && read < totalPages ? <><window.IconPlay size={13} /> Continue · Pg. {read}</>
    : read >= totalPages && totalPages > 0 ? <><window.IconCycle size={14} /> Re-read from start</>
    : <><window.IconPlay size={13} /> Start Reading</>;

  return (
    <div className="page detail-page" style={{ "--statusColor": stColor }}>
      <div className="detail-topbar">
        <button className="btn ghost sm" onClick={onBack}><window.IconArrowLeft size={15} /> Library</button>
        <div className="detail-breadcrumb">
          <span className="muted">Library</span><span className="sep">/</span>
          {item.series && <><span className="muted">{item.series}</span><span className="sep">/</span></>}
          <span>{item.title}</span>
        </div>
        <div style={{ flex: 1 }} />
        <div className="group" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="label" style={{ fontSize: 10, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.12em", fontWeight: 600 }}>Status</span>
          <window.Dropdown
            value={status}
            onChange={changeStatus}
            width={140}
            options={["Not Started", "Reading", "Completed", "On Hold", "Dropped", "Planned to Read"]}
          />
        </div>
        <button className={"btn sm" + (fav ? " primary" : "")} onClick={toggleFav}>{fav ? <><window.IconStar size={14} /> Favorited</> : <><window.IconStarOutline size={14} /> Favorite</>}</button>
        {otherLibs.length > 0 && (
          <button className="btn ghost sm" title="Move this series to another library" onClick={() => setMoveOpen(true)}>
            <window.IconLibrary size={14} /> Move
          </button>
        )}
      </div>
      {moveOpen && (
        <MoveModal
          item={item}
          libraries={otherLibs}
          onClose={() => setMoveOpen(false)}
          onStarted={(title) => { setMoveOpen(false); onMoveStarted?.(title); }}
        />
      )}

      <div className="detail-grid">
        <div className="detail-cover-pane">
          <div className="detail-cover">
            {/* key includes coverBust so the image remounts + refetches after an upload/reset */}
            <window.CoverImg key={"cover-" + coverBust} item={item} w={500} h={700} />
            <div className="detail-cover-status" style={{ color: stColor }}>
              <span className="dot" style={{ background: stColor }} />
              <span style={{ color: "white" }}>{status}</span>
            </div>
            <input ref={coverInputRef} type="file" accept="image/*" style={{ display: "none" }} onChange={onCoverChosen} />
            <div className="detail-cover-actions" onClick={(e) => e.stopPropagation()}>
              <button className="detail-cover-btn" disabled={coverBusy} onClick={pickCover}>
                <window.IconPencil size={13} /> {coverBusy ? "Uploading…" : "Change cover"}
              </button>
              <button className="detail-cover-btn ghost" disabled={coverBusy} onClick={resetCover} title="Revert to auto cover">
                <window.IconCycle size={13} />
              </button>
            </div>
          </div>

          <div className="detail-progress-block">
            <div className="detail-progress-row">
              <span className="detail-progress-label">Progress</span>
              <span className="detail-progress-value"><span className="num">{read}</span><span className="of">/ {totalPages} pages</span></span>
            </div>
            <div className="detail-progress-bar">
              <div className="fill" style={{ width: `${pct}%`, background: `linear-gradient(90deg, ${stColor}, ${stColor}cc)`, color: stColor }} />
            </div>
            <div className="detail-progress-row">
              <span className="detail-progress-label">{pctText} complete</span>
              <span className="detail-progress-value" style={{ fontSize: 11 }}>{chapCountText} chapters</span>
            </div>
          </div>

          <div className="detail-actions">
            <button className="btn lg primary" onClick={() => {
              // Open the chapter that contains the last-read page, resuming AT that
              // page — NOT the whole series. startPage/endPage scope the reader to
              // the chapter; resumePage (4th arg) is where inside it to start.
              const c = continueChapter;
              if (c) onOpenReader(item, c.startPage, c.endPage, Math.max(c.startPage, Math.min(read || c.startPage, c.endPage)));
              else onOpenReader(item, read || 1);
            }} disabled={loading || totalPages === 0}>
              {loading ? "Loading…" : totalPages === 0 ? "No pages" : continueLabel}
            </button>
          </div>

          <StarRating value={item.rating || 0} onChange={(n) => saveField("rating", n)} />

          {item.genres.length > 0 && (
            <div>
              <div className="detail-progress-label" style={{ marginBottom: 8 }}>Genres</div>
              <div className="detail-genre-tags">
                {item.genres.map((g) => (
                  <span key={g} className="genre-pill" style={{ borderColor: `${stColor}55`, color: stColor }}>
                    {g}
                    {(window.STORE.facets.genre_counts || {})[g] != null && (
                      <span className="token-count">{window.STORE.facets.genre_counts[g]}</span>
                    )}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="detail-meta-pane">
          <div className="detail-meta-head">
            <EditableTitle title={item.title} onSave={renameTitle} />
            <div className="detail-h-sub">
              <span className="kanji-tag" style={{ color: stColor }}>{item.kanji}</span>
              {item.author && <><span className="dotsep" /><span>{item.author}</span></>}
              {item.series && <><span className="dotsep" /><span className="muted">{item.series}</span></>}
              <span className="dotsep" />
              <span className="muted">{totalPages} pages · {chapCountText} chapters</span>
            </div>
          </div>

          {/* Grouped metadata table: a label per row, grey pills with
              usage counts. Single-value rows (Parodies/Artists/Languages) edit in
              place; multi-value rows (Tags/Genres) add/remove with autocomplete. */}
          {(() => {
            // Always show every metadata row (with empty "+ Add" fields when a
            // field has no value yet) so you can always add tags/genres/etc.
            // (No collapse — hiding empty fields meant you couldn't add to them.)
            const vis = () => true;
            return (
          <div className="info-table">
            {/* Series — groups multiple entries under one series name (e.g. all
                "Fairy Tail" doujinshi). Single value; type or pick an existing
                series name. This is the "add series to mangas" feature. */}
            {vis("Series") && (
            <InfoRow label="Series">
              <TokenEditor
                single addLabel="series"
                values={item.series ? [item.series] : []}
                counts={window.STORE.facets.series_counts || {}}
                suggestions={window.STORE.facets.series || []}
                onChange={(arr) => saveField("series", arr[0] || "")}
              />
            </InfoRow>
            )}

            {/* Parodies — multi-value: a work can parody/collaborate on several
                source series, so this is a full add/remove list with autocomplete. */}
            {vis("Parodies") && (
            <InfoRow label="Parodies">
              <TokenEditor
                addLabel="parody"
                values={item.parodies || []}
                counts={window.STORE.facets.parody_counts || {}}
                suggestions={window.STORE.facets.parodies || []}
                onChange={(arr) => saveField("parodies", arr)}
              />
            </InfoRow>
            )}

            {/* Tags */}
            {vis("Tags") && (
            <InfoRow label="Tags">
              <TokenEditor
                values={item.tags}
                counts={window.STORE.facets.tag_counts || {}}
                suggestions={window.STORE.facets.tags || []}
                onChange={(arr) => saveField("tags", arr)}
              />
            </InfoRow>
            )}

            {/* Genres (you keep these separately from tags) */}
            {vis("Genres") && (
            <InfoRow label="Genres">
              <TokenEditor
                values={item.genres}
                counts={window.STORE.facets.genre_counts || {}}
                suggestions={window.STORE.facets.genres || []}
                onChange={(arr) => saveField("genres", arr)}
              />
            </InfoRow>
            )}

            {/* Artists = author (single value; × on hover, + Add to set) */}
            {vis("Artists") && (
            <InfoRow label="Artists">
              <TokenEditor
                single addLabel="artist"
                values={item.author ? [item.author] : []}
                counts={window.STORE.facets.author_counts || {}}
                suggestions={window.STORE.facets.authors || []}
                onChange={(arr) => saveField("author", arr[0] || "")}
              />
            </InfoRow>
            )}

            {/* Languages = language (single value; × on hover, + Add to set) */}
            {vis("Languages") && (
            <InfoRow label="Languages">
              <TokenEditor
                single addLabel="language"
                values={item.language ? [item.language] : []}
                counts={window.STORE.facets.language_counts || {}}
                suggestions={window.STORE.facets.languages || []}
                onChange={(arr) => saveField("language", arr[0] || "")}
              />
            </InfoRow>
            )}

            {/* Pages */}
            <InfoRow label="Pages">
              <span className="token-pill"><span className="token-name">{totalPages}</span></span>
            </InfoRow>

            {/* Source — where this series was imported from + a "check for new
                chapters" action. Only shown for imported series. */}
            {origin && (
              <InfoRow label="Source">
                <div className="detail-source">
                  {origin.url
                    ? <a className="detail-source-link" href={origin.url} target="_blank" rel="noreferrer">{origin.label}</a>
                    : <span className="detail-source-link">{origin.label}</span>}
                  <button className="btn ghost sm" disabled={resync && resync.running} onClick={doResync} title="Download any new chapters from the source">
                    <window.IconCycle size={12} /> {resync && resync.running ? "Checking…" : "Check for updates"}
                  </button>
                  {resync && (
                    <span className={"detail-source-status" + (resync.error ? " error" : "")}>
                      {resync.error ? resync.error : resync.message}
                    </span>
                  )}
                </div>
              </InfoRow>
            )}

            {item.notes && (
              <InfoRow label="Notes"><div className="detail-field-value multiline" style={{flex:1}}>{item.notes}</div></InfoRow>
            )}
          </div>
            );
          })()}
        </div>
      </div>

      <div className="detail-chapters">
        <div className="detail-chapters-head">
          <h2 className="detail-chapters-title"><span className="kanji" style={{ color: stColor, textShadow: `0 0 12px ${stColor}77` }}>章</span> Chapters &amp; Pages</h2>
          <span className="detail-chapters-meta">{chapCountText} chapters · {totalPages} pages</span>
          <div style={{ flex: 1 }} />
        </div>

        {loading ? (
          <div className="empty"><p>Loading chapters…</p></div>
        ) : (
          <>
            <div className="chapter-grid" ref={chapterGridRef}>
              {chapterRows.slice(0, chapLimit).map((ch, i) => (
                <ChapterCard
                  key={ch.index}
                  ch={ch}
                  displayIndex={i}
                  item={item}
                  read={read}
                  stColor={stColor}
                  canEdit={chapters.length > 1 || (ch.path && ch.path !== item.folder_path)}
                  onOpen={() => onOpenReader(item, ch.startPage, ch.endPage)}
                  onRename={renameChapter}
                />
              ))}
            </div>
            {chapterRows.length > chapLimit && (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8, padding: "24px 0" }}>
                <button className="btn primary" onClick={() => setChapLimit((n) => n + cols * ROWS_PER_PAGE)}>
                  Load more chapters
                </button>
                <span style={{ fontSize: 12, color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
                  Showing {Math.min(chapLimit, chapterRows.length)} of {chapterRows.length}
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// One row of the info table: "Label: <pills>".
function InfoRow({ label, children }) {
  return (
    <div className="info-row">
      <div className="info-label">{label}:</div>
      <div className="info-vals">{children}</div>
    </div>
  );
}

// Editable list of values: grey pills (name + usage count, × on hover) + Add box
// with type-to-search autocomplete (suggestions show their counts).
// `single` mode = at most one value (used for Parodies/Artists).
function TokenEditor({ values, counts, suggestions, color, onChange, single, addLabel }) {
  const [adding, setAdding] = useStateDtl(false);
  const [q, setQ] = useStateDtl("");

  const lower = values.map((v) => v.toLowerCase());
  const matches = (suggestions || [])
    .filter((s) => !lower.includes(s.toLowerCase()) && (!q || s.toLowerCase().includes(q.toLowerCase())))
    .sort((a, b) => (counts[b] || 0) - (counts[a] || 0))
    .slice(0, 8);

  function add(val) {
    const v = (val || "").trim();
    if (!v) return;
    if (values.some((x) => x.toLowerCase() === v.toLowerCase())) { setQ(""); setAdding(false); return; }
    onChange(single ? [v] : [...values, v]);  // single: replace
    setQ("");
    if (single) setAdding(false);
  }
  function remove(val) {
    onChange(values.filter((x) => x !== val));
  }

  const pillStyle = color ? { borderColor: `${color}55`, color } : undefined;
  const showAdd = !adding && !(single && values.length >= 1);

  return (
    <div className="token-editor">
      {/* Only render the pills row when it has content (values or the +Add button).
          When editing an EMPTY field, this row would otherwise be an empty box that
          adds height + a gap above the input — the "space is too big" issue. */}
      {(values.length > 0 || showAdd) && (
      <div className="token-pills">
        {values.map((v) => (
          <span key={v} className="token-pill" style={pillStyle}>
            <span className="token-name">{v}</span><span className="token-slot"><span className="token-count">{counts[v] != null ? counts[v] : ""}</span><button className="token-x" title="Remove" onClick={() => remove(v)}><window.IconClose size={13} /></button></span>
          </span>
        ))}
        {showAdd && (
          <button className="token-add" onClick={() => setAdding(true)}>+ {addLabel || "Add"}</button>
        )}
      </div>
      )}
      {adding && (
        <div className="token-add-box">
          <input
            className="detail-field-input"
            autoFocus
            value={q}
            placeholder="Type to search or add new…"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") add(q); if (e.key === "Escape") { setQ(""); setAdding(false); } }}
            onBlur={() => setTimeout(() => setAdding(false), 150)}
          />
          {(matches.length > 0 || q.trim()) && (
            <div className="token-suggest">
              {q.trim() && !matches.some((m) => m.toLowerCase() === q.trim().toLowerCase()) && (
                <button className="token-suggest-item new" onMouseDown={(e) => { e.preventDefault(); add(q); }}>
                  + Create “{q.trim()}”
                </button>
              )}
              {matches.map((m) => (
                <button key={m} className="token-suggest-item" onMouseDown={(e) => { e.preventDefault(); add(m); }}>
                  <span>{m}</span><span className="token-count">{counts[m] || 0}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

Object.assign(window, { Detail });
