/* global window, React */
const { useState: useStateRdr, useEffect: useEffectRdr, useRef: useRefRdr, useMemo: useMemoRdr } = React;

// ====== Reader ======
// `item` carries real chapters (loaded by Detail). Pages are flattened across
// chapters into one continuous list so the slider/progress maps to a global page
// number — last_page is tracked per series.
// `previewSource` (a source name like "mangadex") switches the reader into
// remote/preview mode: page `abs` values are source CDN URLs streamed through the
// proxy, not local files. Progress/auto-status are disabled in preview mode.
function Reader({ item: itemProp, onClose, bg, startPage, endPage, resumePage, previewSource }) {
  // Default to single-page mode: page state is the single source of truth, so
  // navigation (buttons/arrows/input/slider) is reliable. Vertical (webtoon)
  // scroll mode is available via the toggle.
  const [mode, setMode] = useStateRdr(
    // Namespaced key (matches mangashelf_token/mangashelf_settings); falls back to
    // the old un-prefixed key so existing users keep their preference.
    () => localStorage.getItem("mangashelf_reader_mode") || localStorage.getItem("reader_mode") || "single"
  ); // single | vertical | double
  function changeMode(m) { setMode(m); localStorage.setItem("mangashelf_reader_mode", m); }
  const [chromeHidden, setChromeHidden] = useStateRdr(false);
  const canvasRef = useRefRdr(null);

  // The reader needs real chapters. Detail usually provides them, but if the
  // reader is opened with an item that hasn't loaded chapters yet, fetch them.
  const [item, setItem] = useStateRdr(itemProp);
  useEffectRdr(() => {
    if (item.chapters && item.chapters.length) return;
    if (!item.id) return;
    window.ApiClient.getSeries(item.id)
      .then((full) => setItem(full))
      .catch(() => {});
  }, [item.id]);

  // Flatten all chapter pages into [{abs, chapterName, pageInChapter, chapterIndex, globalPage}].
  const fullFlat = useMemoRdr(() => {
    const out = [];
    (item.chapters || []).forEach((ch, ci) => {
      (ch.pages || []).forEach((abs, pi) => {
        out.push({ abs, chapterName: ch.name, chapterIndex: ci, pageInChapter: pi + 1, globalPage: out.length + 1 });
      });
    });
    return out;
  }, [item.chapters]);

  // The visible slice (start/end global page indices) is STATE, not just props,
  // so we can switch chapters in-place without closing the reader. Seeded from
  // the props the reader was opened with. null end = "whole series, no slice".
  const [bounds, setBounds] = useStateRdr({ start: startPage || null, end: endPage || null });

  // Each chapter's global page range, for the prev/next-chapter controls and the
  // "which chapter am I in" lookup. startPage/endPage are 1-based inclusive.
  const chapterRanges = useMemoRdr(() => {
    const out = [];
    let running = 1;
    (item.chapters || []).forEach((ch, ci) => {
      const start = running;
      const end = running + (ch.page_count || (ch.pages || []).length) - 1;
      running = end + 1;
      out.push({ index: ci, name: ch.name, start, end });
    });
    return out;
  }, [item.chapters]);

  // If a chapter slice is active, the index of the chapter it corresponds to.
  const curChapterIdx = useMemoRdr(() => {
    if (bounds.end == null) return -1;
    return chapterRanges.findIndex((c) => c.start === bounds.start && c.end === bounds.end);
  }, [chapterRanges, bounds]);

  // Slice the flattened pages to the active chapter (or the whole series).
  const flat = useMemoRdr(() => {
    if (bounds.end == null || !fullFlat.length) return fullFlat;
    const s = Math.max(0, (bounds.start || 1) - 1);
    const e = Math.min(fullFlat.length, bounds.end);
    return fullFlat.slice(s, e);
  }, [fullFlat, bounds]);

  const total = flat.length;
  const [page, setPage] = useStateRdr(1);
  const [flipDir, setFlipDir] = useStateRdr(1);   // 1 = forward, -1 = back (for the page-turn animation)

  // Switch to an adjacent chapter without leaving the reader. dir = +1 next / -1 prev.
  function goChapter(dir) {
    if (curChapterIdx < 0) return;
    const next = chapterRanges[curChapterIdx + dir];
    if (!next) return;
    clampedRef.current = true;                 // we set the page ourselves below
    setBounds({ start: next.start, end: next.end });
    // Forward → start at page 1; backward → last page of the previous chapter
    // feels natural, but starting at page 1 is simpler and predictable. Use 1.
    setPage(1);
    setFlipDir(dir);
    if (canvasRef.current) canvasRef.current.scrollTo({ top: 0 });
  }
  const hasPrevChapter = curChapterIdx > 0;
  const hasNextChapter = curChapterIdx >= 0 && curChapterIdx < chapterRanges.length - 1;

  // Once pages are known, clamp the starting page into range (chapters may load
  // after the first render, so total starts at 0). Runs only while total changes.
  const clampedRef = useRefRdr(false);
  useEffectRdr(() => {
    if (!total || clampedRef.current) return;
    clampedRef.current = true;
    if (bounds.end == null) {
      // No chapter restriction: resume at the last-read page (global).
      setPage(Math.min(Math.max(1, bounds.start || item.read || 1), total));
    } else if (resumePage) {
      // Chapter-restricted Continue: resume at resumePage, translated from a
      // global index into this slice's local 1-based page.
      const local = resumePage - (bounds.start || 1) + 1;
      setPage(Math.min(Math.max(1, local), total));
    }
    // else: opened from a chapter card → start at page 1 of the chapter (default).
  }, [total]);

  // Auto-hide chrome after inactivity.
  // Chrome (top/bottom bars) stays visible by default — no aggressive auto-hide,
  // which previously made the page controls unclickable after 2.4s. Click the
  // page area to toggle it for distraction-free reading.

  // Auto-status: opening the reader marks the series
  // "Reading"; reaching the last page marks it "Completed". We patch the shared
  // store item so the library/landing reflect it, and persist via the API.
  const statusSetRef = useRefRdr({ reading: false, completed: false });
  function autoStatus(next) {
    if (!item.id) return;
    const store = (window.STORE.items || []).find((x) => x.id === item.id);
    if (store) store.status = next;
    item.status = next;
    window.ApiClient.setStatus(item.id, next).catch(() => {});
  }
  // Mark "Reading" once, on open (unless already Completed).
  useEffectRdr(() => {
    if (!item.id || !total) return;
    if (statusSetRef.current.reading) return;
    statusSetRef.current.reading = true;
    if (item.status !== "Completed") autoStatus("Reading");
  }, [item.id, total]);
  // Mark "Completed" only when the last page of the WHOLE series is reached,
  // not just the last page of a single chapter (endPage restricts the view).
  useEffectRdr(() => {
    if (!item.id || !fullFlat.length) return;
    const isWholeSeriesEnd = bounds.end == null || bounds.end >= fullFlat.length;
    if (isWholeSeriesEnd && page >= total && !statusSetRef.current.completed) {
      statusSetRef.current.completed = true;
      autoStatus("Completed");
    }
  }, [page, total, item.id, fullFlat.length, bounds]);

  // Persist progress whenever the current page changes (immediate, not debounced,
  // so closing the reader always reflects the last-seen page in the library).
  const saveRef = useRefRdr(null);
  useEffectRdr(() => {
    if (!total || !item.id) return;
    const cur = flat[page - 1];
    if (!cur) return;
    clearTimeout(saveRef.current);
    saveRef.current = setTimeout(() => {
      window.ApiClient.saveProgress(item.id, cur.chapterName, cur.globalPage || page).catch(() => {});
    }, 300);
    return () => clearTimeout(saveRef.current);
  }, [page, total, item.id, flat]);

  // Keyboard navigation. Escape closes via handleClose so the pending progress
  // save is flushed first (the app-level Escape used to bypass that, dropping the
  // last page turn and briefly showing stale progress).
  useEffectRdr(() => {
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); handleClose(); }
      else if (e.key === "ArrowRight" || e.key === " ") { e.preventDefault(); goToPage(page + 1); }
      else if (e.key === "ArrowLeft") { goToPage(page - 1); }
      else if (e.key === "1") changeMode("single");
      else if (e.key === "2") changeMode("vertical");
      else if (e.key === "3") changeMode("double");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [total, page, mode]);

  // When we programmatically scroll (button/slider jump), suppress the page
  // tracker briefly so the smooth-scroll doesn't bounce the page number back.
  const suppressScrollRef = useRefRdr(0);

  // Always hold the latest page so the deferred vertical-scroll timer below reads
  // the current value, not the one captured when mode changed.
  const pageRef = useRefRdr(page);
  pageRef.current = page;

  // When entering vertical mode, scroll to the current page so the view doesn't
  // jump to the top. Suppress the tracker briefly so it doesn't fight the scroll.
  useEffectRdr(() => {
    if (mode !== "vertical") return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const t = setTimeout(() => {
      suppressScrollRef.current = Date.now() + 400;
      const p = pageRef.current;  // latest page, not the mode-switch snapshot
      const el = canvas.querySelector(`[data-page="${p}"]`);
      if (p <= 1) canvas.scrollTo({ top: 0 });
      else if (el) el.scrollIntoView({ block: "start" });
    }, 50);
    return () => clearTimeout(t);
  }, [mode]); // only when the mode itself changes

  // In vertical mode, track the current page by measuring which page element
  // straddles the top of the viewport on each scroll-settle. The previous
  // approach kept a Set of "intersecting" pages and took Math.min — but with
  // fast/free-spin scrolling the set went stale (pages that scrolled off lingered
  // because their "not intersecting" callback was debounced away), so the page
  // number jumped erratically (e.g. ch 2 → ch 16). Measuring rects directly is
  // stable: the current page is simply the last one whose top is at/above a small
  // band below the viewport top.
  useEffectRdr(() => {
    if (mode !== "vertical") return;
    const canvas = canvasRef.current;
    if (!canvas) return;

    let rafId = null;
    function computeCurrent() {
      rafId = null;
      if (Date.now() < suppressScrollRef.current) return;
      // At (or within a few px of) the very bottom, the LAST page is current no
      // matter where its top sits relative to the band — otherwise a short final
      // page never crosses the band, so `page` sticks at total-1 and the series
      // never auto-completes / never saves the final page.
      if (canvas.scrollTop + canvas.clientHeight >= canvas.scrollHeight - 4) {
        setPage((prev) => (prev !== total ? total : prev));
        return;
      }
      const top = canvas.getBoundingClientRect().top;
      const band = top + canvas.clientHeight * 0.30;   // 30% down from the top
      let best = 1;
      let bestTop = -Infinity;
      canvas.querySelectorAll("[data-page]").forEach((el) => {
        const r = el.getBoundingClientRect();
        // The page whose top is the closest one at or above the band is "current".
        if (r.top <= band && r.top > bestTop) {
          bestTop = r.top;
          best = Number(el.getAttribute("data-page")) || best;
        }
      });
      setPage((prev) => (best !== prev ? best : prev));
    }
    function onScroll() {
      if (rafId == null) rafId = requestAnimationFrame(computeCurrent);
    }
    canvas.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      canvas.removeEventListener("scroll", onScroll);
      if (rafId != null) cancelAnimationFrame(rafId);
    };
  }, [mode, total]);

  // Jump to a page. In vertical mode, scroll to it; suppress the tracker for a
  // moment so the smooth-scroll doesn't immediately override the page we set.
  function goToPage(p) {
    // Past the end of a chapter slice → roll over into the next chapter (and
    // before the start → roll back into the previous one). Lets you read straight
    // through without touching the chapter buttons.
    if (bounds.end != null && curChapterIdx >= 0) {
      if (p > total && hasNextChapter) { goChapter(1); return; }
      if (p < 1 && hasPrevChapter) {
        const prev = chapterRanges[curChapterIdx - 1];
        clampedRef.current = true;
        setBounds({ start: prev.start, end: prev.end });
        setPage(prev.end - prev.start + 1);   // land on the last page of the prev chapter
        setFlipDir(-1);
        if (canvasRef.current) canvasRef.current.scrollTo({ top: 0 });
        return;
      }
    }
    const clamped = Math.max(1, Math.min(total || 1, p));
    // Remember the travel direction so the page-flip animation slides the right
    // way (forward = new page enters from the right, back = from the left).
    if (clamped !== page) setFlipDir(clamped > page ? 1 : -1);
    setPage(clamped);
    if (mode === "vertical" && canvasRef.current) {
      const canvas = canvasRef.current;
      suppressScrollRef.current = Date.now() + 600;
      if (clamped === 1) {
        canvas.scrollTo({ top: 0 });
      } else {
        const el = canvas.querySelector(`[data-page="${clamped}"]`);
        if (el) el.scrollIntoView({ block: "start" });
      }
    }
  }

  // Preload upcoming (and previous) pages so a page turn shows an already-decoded
  // image instead of flashing while it loads. In double mode we look two pages
  // ahead so the NEXT pair is ready, not just the next single page. Vertical mode
  // relies on native lazy-loading instead. Browser caches the request, so when the
  // <img> mounts it's already there.
  useEffectRdr(() => {
    if (mode === "vertical" || !total) return;
    const span = mode === "double" ? 2 : 1;
    const wanted = [];
    for (let d = 1; d <= span * 2; d++) {
      wanted.push(page + d);   // ahead (the common direction)
      wanted.push(page - d);   // behind (in case they turn back)
    }
    const imgs = [];
    wanted.forEach((p) => {
      const f = flat[p - 1];
      if (!f) return;
      const img = new Image();
      img.decoding = "async";
      img.src = previewSource ? window.ApiClient.previewPageUrl(f.abs, previewSource) : window.pageUrl(f.abs);
      imgs.push(img);
    });
    return () => { imgs.forEach((img) => { img.src = ""; }); };
  }, [page, mode, total, flat]);

  // --- Swipe-to-turn (single/double page mode) ---
  // Horizontal swipe on the page area changes pages, like a touch e-reader.
  // A barely-moved gesture is treated as a tap (toggles chrome) instead.
  const touchRef = useRefRdr(null);
  const touchHandledRef = useRefRdr(0);   // timestamp; suppresses the click that follows a touch
  function onTouchStart(e) {
    if (mode === "vertical") return;            // vertical mode scrolls naturally
    const t = e.touches[0];
    touchRef.current = { x: t.clientX, y: t.clientY };
  }
  function onTouchEnd(e) {
    if (mode === "vertical") return;
    const start = touchRef.current;
    touchRef.current = null;
    if (!start) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - start.x;
    const dy = t.clientY - start.y;
    const adx = Math.abs(dx), ady = Math.abs(dy);
    // Touch owns the tap/swipe; mark it so the synthesized click that follows is
    // ignored (React listeners are passive, so preventDefault isn't reliable).
    touchHandledRef.current = Date.now();
    if (adx < 10 && ady < 10) { setChromeHidden((h) => !h); return; }   // tap
    if (adx > 45 && adx > ady * 1.5) {                                  // horizontal swipe
      if (dx < 0) goToPage(page + 1);           // swipe left → next
      else goToPage(page - 1);                  // swipe right → previous
    }
  }
  function onCanvasClick() {
    // Ignore the click synthesized right after a touch gesture (handled above);
    // genuine mouse clicks (no recent touch) toggle the chrome.
    if (Date.now() - touchHandledRef.current < 600) return;
    setChromeHidden((h) => !h);
  }

  function handleClose() {
    // Flush any pending progress save before closing so the library reflects
    // the last-read page immediately when landing re-renders.
    clearTimeout(saveRef.current);
    const cur = flat[page - 1];
    if (cur && item.id) {
      window.ApiClient.saveProgress(item.id, cur.chapterName, cur.globalPage || page)
        .catch(() => {})
        .finally(() => onClose());
    } else {
      onClose();
    }
  }

  if (!item) return null;

  const cur = flat[page - 1] || {};
  const visible =
    mode === "vertical"
      ? flat.map((_, i) => i + 1)
      : mode === "single"
      ? [page]
      : [page, Math.min(page + 1, total)];

  function PageImg({ p }) {
    const f = flat[p - 1];
    if (!f) return null;
    const imgStyle = mode === "vertical"
      ? { background: "#1a1a22" }
      : {
          background: "#1a1a22",
          width: "100%",
          height: "100%",
          objectFit: "contain",
          display: "block",
        };
    return (
      <img
        data-page={p}
        className="page-img"
        src={previewSource ? window.ApiClient.previewPageUrl(f.abs, previewSource) : window.pageUrl(f.abs)}
        alt={`Page ${p}`}
        // Vertical/webtoon mode has hundreds of pages — lazy-load those. But in
        // single/double mode only 1–2 are shown, and lazy loading DELAYS the very
        // image we're turning to, causing the harsh flash. Load those eagerly.
        loading={mode === "vertical" ? "lazy" : "eager"}
        decoding="async"
        style={imgStyle}
      />
    );
  }

  return (
    <div
      className={`reader bg-${bg} reader-mode-${mode} ${chromeHidden ? "chrome-hidden" : ""}`}
    >
      {/* Top bar */}
      <div className="reader-top" onClick={(e) => e.stopPropagation()}>
        <button className="icon-btn" onClick={handleClose} title="Back"><window.IconArrowLeft size={18} /></button>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {previewSource && <span className="reader-preview-tag">PREVIEW</span>}
            {item.title}
          </div>
          <div style={{ fontSize: 11, color: "rgba(255,255,255,0.55)", fontFamily: "var(--font-mono)" }}>
            {cur.chapterName ? `${cur.chapterName} · ` : ""}Page {page}/{total}
          </div>
        </div>
        <div className="reader-controls">
          {/* Prev/next CHAPTER — switch chapters without leaving the reader. Shown
              only when reading a single chapter that has a neighbour. Hidden in
              preview mode (only one chapter is loaded there). */}
          {!previewSource && curChapterIdx >= 0 && (hasPrevChapter || hasNextChapter) && (
            <div className="reader-chapter-nav">
              <button
                className="btn sm"
                disabled={!hasPrevChapter}
                title="Previous chapter"
                onClick={() => goChapter(-1)}
              ><window.IconChevronLeft size={15} /> Prev ch</button>
              <button
                className="btn sm"
                disabled={!hasNextChapter}
                title="Next chapter"
                onClick={() => goChapter(1)}
              >Next ch <window.IconChevronRight size={15} /></button>
            </div>
          )}
          {/* Split (needs a real, imported series with a folder on disk) — hidden
              in preview mode where item.id is null. */}
          {!previewSource && (
          <button
            className="btn sm"
            style={{ fontSize: 11, opacity: 0.75 }}
            title="End chapter here — splits pages from this point into a new chapter"
            onClick={async () => {
              const ok = await window.confirmDialog({
                title: `Split chapter at page ${page}?`,
                message: "Pages from this point onward will move into a new chapter folder.",
                confirmLabel: "Split here",
                tone: "warn",
              });
              if (!ok) return;
              window.ApiClient.splitChapterHere(item.id, page)
                .then((r) => {
                  window.toast(`Created ${r.new_chapter} (${r.moved} pages moved).`, "ok");
                  window.ApiClient.getSeries(item.id).then((full) => setItem(full)).catch(() => {});
                })
                .catch((e) => window.toast(`Split failed: ${e.message || e}`, "error"));
            }}
          ><window.IconScissors size={13} /> End Chap Here</button>
          )}
          <div className="reader-mode-toggle">
            {[
              { id: "vertical", label: "縦 Vert" },
              { id: "single", label: "単 1up" },
              { id: "double", label: "双 2up" },
            ].map((m) => (
              <button key={m.id} className={mode === m.id ? "active" : ""} onClick={() => changeMode(m.id)}>
                {m.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Canvas — tap toggles the chrome; horizontal swipe turns the page (in
          single/double mode). Mouse click also toggles. Touch handlers own the
          tap/swipe and preventDefault the synthesized click so it doesn't
          double-fire. */}
      <div className="reader-canvas" ref={canvasRef}
        onClick={onCanvasClick}
        onTouchStart={onTouchStart}
        onTouchEnd={onTouchEnd}
        style={mode !== "vertical" ? {
          display: "flex", alignItems: "center", justifyContent: "center",
          overflow: "hidden", padding: 0, gap: 4,
          position: "absolute", inset: 0,
        } : undefined}
      >
        {total === 0 ? (
          <div className="empty" style={{ color: "rgba(255,255,255,0.6)" }}>
            <div className="glyph">無</div>
            <p>No pages found for this series.</p>
          </div>
        ) : mode === "vertical" ? (
          visible.map((p) => <PageImg key={p} p={p} />)
        ) : (
          // key={page} remounts the wrapper on every turn so the slide-in
          // animation replays; the direction class picks which side it enters from.
          <div
            key={page}
            className={"page-flip " + (flipDir >= 0 ? "flip-fwd" : "flip-back")}
            style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 4, width: "100%", height: "100%" }}
          >
            {visible.map((p) => <PageImg key={p} p={p} />)}
          </div>
        )}
      </div>

      {/* Bottom bar */}
      <div className="reader-bottom" onClick={(e) => e.stopPropagation()}>
        <div className="page-slider">
          <button className="icon-btn" onClick={() => goToPage(page - 1)} title="Previous page"><window.IconChevronLeft size={20} /></button>
          <input
            className="page-input"
            type="number"
            min={1}
            max={total || 1}
            value={page}
            onChange={(e) => {
              const v = parseInt(e.target.value, 10);
              if (!Number.isNaN(v) && v >= 1 && v <= total) goToPage(v);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                const v = parseInt(e.target.value, 10);
                if (!Number.isNaN(v)) goToPage(v);
                e.target.blur();
              }
            }}
            title="Jump to page"
          />
          <div
            className="slider-track"
            onClick={(e) => {
              const r = e.currentTarget.getBoundingClientRect();
              const pct = (e.clientX - r.left) / r.width;
              goToPage(Math.max(1, Math.round(pct * total)));
            }}
          >
            <div className="rail">
              <div className="fill" style={{ width: `${total ? (page / total) * 100 : 0}%` }} />
            </div>
            <div className="knob" style={{ left: `${total ? (page / total) * 100 : 0}%` }} />
          </div>
          <span>{String(total).padStart(3, "0")}</span>
          <button className="icon-btn" onClick={() => goToPage(page + 1)} title="Next page"><window.IconChevronRight size={20} /></button>
        </div>
        <div style={{ display: "flex", justifyContent: "center", fontSize: 11, color: "rgba(255,255,255,0.55)", fontFamily: "var(--font-mono)" }}>
          <span>Tap to {chromeHidden ? "show" : "hide"} chrome · Esc to exit · ←/→ pages</span>
        </div>
      </div>
    </div>
  );
}

// Web-result / preview cover with a graceful fallback tile (source CDNs sometimes
// hotlink-protect thumbnails → a broken <img> otherwise).
function WebCover({ url }) {
  const [err, setErr] = useStateRdr(false);
  if (!url || err) return <div className="web-result-noart">本</div>;
  return <img src={url} alt="" referrerPolicy="no-referrer" loading="lazy" onError={() => setErr(true)} />;
}

// ====== Search Panel ======
function SearchPanel({ onClose, onOpenDetail, onImportLink, onPreviewLink, initialQuery }) {
  const [q, setQ] = useStateRdr(initialQuery || "");
  const isUrl = /^https?:\/\//i.test(q.trim());
  // Debounced copy of the query drives the actual filtering, so typing fast in a
  // multi-thousand-series library doesn't re-scan the whole list on every
  // keystroke (the input stays responsive; results settle ~180ms after you stop).
  const [debouncedQ, setDebouncedQ] = useStateRdr(initialQuery || "");
  useEffectRdr(() => {
    const t = setTimeout(() => setDebouncedQ(q), 180);
    return () => clearTimeout(t);
  }, [q]);
  const [activeTags, setActiveTags] = useStateRdr(new Set());
  const [activeStatus, setActiveStatus] = useStateRdr(new Set());
  const [activeGenre, setActiveGenre] = useStateRdr(new Set());
  const [sort, setSort] = useStateRdr("random");
  // Web search (MangaDex etc.) — only run on demand, never automatically.
  const [webResults, setWebResults] = useStateRdr(null);   // null=not searched, []=no hits
  const [webBusy, setWebBusy] = useStateRdr(false);
  const [webErr, setWebErr] = useStateRdr("");

  const all = window.STORE.items;
  const facets = window.STORE.facets;

  // Reset web results whenever the query changes (they're for the old query).
  useEffectRdr(() => { setWebResults(null); setWebErr(""); }, [debouncedQ]);

  const webReqRef = useRefRdr(0);
  function searchWeb() {
    const query = q.trim();
    if (!query) return;
    const myReq = ++webReqRef.current;   // stale-guard: ignore superseded responses
    setWebBusy(true); setWebErr(""); setWebResults(null);
    window.ApiClient.searchWeb(query)
      .then((r) => {
        if (myReq !== webReqRef.current) return;
        const results = (r && r.results) || [];
        setWebResults(results);
        loadChapterCounts(results, myReq);   // fill "· N ch" lazily, per card
      })
      .catch((e) => {
        if (myReq !== webReqRef.current) return;
        // A raw "404 /api/…" means the server predates this feature — say so
        // plainly instead of showing the URL.
        const msg = String(e && e.message || e);
        setWebErr(/^404\b/.test(msg)
          ? "Web search needs the server restarted to enable it."
          : `Web search failed: ${msg}`);
      })
      .finally(() => { if (myReq === webReqRef.current) setWebBusy(false); });
  }

  // Resolve each result's readable chapter count separately (the search itself
  // doesn't, to stay fast) and patch it into state so "· N ch" appears as it
  // arrives. Keyed by url; guarded by the same request id so a stale search's
  // counts can't overwrite a newer one.
  function loadChapterCounts(results, myReq) {
    results.forEach((r) => {
      if (!r || !r.url || (typeof r.chapter_count === "number" && r.chapter_count >= 0)) return;
      window.ApiClient.chapterCount(r.source, r.url)
        .then((res) => {
          const count = res && typeof res.count === "number" ? res.count : -1;
          if (myReq !== webReqRef.current || count < 0) return;
          setWebResults((prev) =>
            Array.isArray(prev)
              ? prev.map((x) => (x.url === r.url ? { ...x, chapter_count: count } : x))
              : prev);
        })
        .catch(() => {});   // a failed count just leaves that card unlabelled
    });
  }

  // A stable random ordering of every series, computed once per panel-open.
  // (Recomputing on each render would reshuffle as you type.) Maps id -> rank.
  const randomRank = useMemoRdr(() => {
    const order = all.map((c) => c.id);
    for (let i = order.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [order[i], order[j]] = [order[j], order[i]];
    }
    const rank = {};
    order.forEach((id, i) => { rank[id] = i; });
    return rank;
  }, [all.length]);

  function toggle(set, setSet, key) {
    const next = new Set(set);
    next.has(key) ? next.delete(key) : next.add(key);
    setSet(next);
  }

  // Build one searchable haystack per item from every text field — title, author,
  // series, tags, genres, language, notes — so a query matches series even when
  // the term only appears in its metadata (not its folder name).
  // Filter + sort only when an actual input changes — memoized on the debounced
  // query and the active filters/sort, so re-renders from typing (before the
  // debounce fires) don't re-scan the whole library.
  const results = useMemoRdr(() => {
    const terms = debouncedQ.toLowerCase().split(/\s+/).filter(Boolean);
    let out = all.filter((c) => {
      if (terms.length) {
        const hay = [
          c.title, c.author, c.series, c.language, c.notes,
          ...(c.tags || []), ...(c.genres || []),
        ].join(" ").toLowerCase();
        // Every term must appear somewhere (AND) so multi-word queries narrow.
        if (!terms.every((t) => hay.includes(t))) return false;
      }
      if (activeTags.size && !c.tags.some((t) => activeTags.has(t))) return false;
      if (activeStatus.size && !activeStatus.has(c.status)) return false;
      if (activeGenre.size && !c.genres.some((g) => activeGenre.has(g))) return false;
      return true;
    });
    if (sort === "az") out = [...out].sort((a, b) => a.title.localeCompare(b.title));
    else if (sort === "progress") out = [...out].sort((a, b) => (b.read/(b.pages||1)) - (a.read/(a.pages||1)));
    else if (sort === "lastread") out = [...out].sort((a, b) => String(b.last_read||"").localeCompare(String(a.last_read||"")));
    else if (sort === "recent") out = [...out].sort((a, b) => {
      // Recently Added — by real date_added, newest first; fall back to id.
      const d = String(b.date_added||"").localeCompare(String(a.date_added||""));
      return d !== 0 ? d : b.id - a.id;
    });
    else out = [...out].sort((a, b) => randomRank[a.id] - randomRank[b.id]); // default: random
    return out;
  }, [all, debouncedQ, activeTags, activeStatus, activeGenre, sort, randomRank]);

  // Cap how many result cards we render — drawing thousands at once freezes the
  // browser. Show the first RESULT_CAP and tell the user to narrow the search.
  const RESULT_CAP = 48;
  const shown = results.slice(0, RESULT_CAP);

  return (
    <window.Modal onClose={onClose} panelClass="search-panel">
        <div className="search-panel-head">
          <div className="search-panel-input">
            <span style={{ display: "inline-flex", alignItems: "center", color: "var(--vermillion)" }}><window.IconSearch size={20} /></span>
            <input autoFocus placeholder="Search series, authors, tags… or paste a link to import"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && isUrl) { e.preventDefault(); onImportLink?.(q.trim()); } }} />
            {!isUrl && <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted)" }}>{results.length} results</span>}
            <button className="icon-btn" onClick={onClose} title="Close"><window.IconClose size={16} /></button>
          </div>
          {isUrl && (
            <button className="search-import-banner" onClick={() => onImportLink?.(q.trim())}>
              <window.IconPlus size={14} /> Import from this link
            </button>
          )}

          <div className="filter-group">
            <div className="filter-group-head"><span>Tags</span><div className="line" /></div>
            <div className="chip-list chip-list-capped">
              {((() => {
                // Re-rank tags by frequency within current results when a query/filter is active;
                // otherwise use global counts so the most-used tags always show first.
                const hasFilter = debouncedQ || activeGenre.size || activeStatus.size;
                if (!hasFilter) {
                  const counts = facets.tag_counts || {};
                  return Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([t]) => t);
                }
                const local = {};
                results.forEach((c) => c.tags.forEach((t) => { local[t] = (local[t] || 0) + 1; }));
                return Object.entries(local).sort((a, b) => b[1] - a[1]).map(([t]) => t);
              })()).map((t) => (
                <span key={t} className={"chip" + (activeTags.has(t) ? " active" : "")} onClick={() => toggle(activeTags, setActiveTags, t)}>
                  {t} {activeTags.has(t) && <span className="x"><window.IconClose size={11} /></span>}
                </span>
              ))}
            </div>
          </div>

          <div className="search-filter-grid">
            <div className="filter-group">
              <div className="filter-group-head"><span>Status</span><div className="line" /></div>
              <div className="chip-list">
                {facets.statuses.map((s) => (
                  <span key={s} className={"chip" + (activeStatus.has(s) ? " active" : "")} onClick={() => toggle(activeStatus, setActiveStatus, s)}>{s}</span>
                ))}
              </div>
            </div>
            <div className="filter-group">
              <div className="filter-group-head"><span>Genre</span><div className="line" /></div>
              <div className="chip-list chip-list-capped">
                {((() => {
                  const hasFilter = debouncedQ || activeTags.size || activeStatus.size;
                  if (!hasFilter) {
                    const counts = facets.genre_counts || {};
                    return Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([g]) => g);
                  }
                  const local = {};
                  results.forEach((c) => c.genres.forEach((g) => { local[g] = (local[g] || 0) + 1; }));
                  return Object.entries(local).sort((a, b) => b[1] - a[1]).map(([g]) => g);
                })()).map((g) => (
                  <span key={g} className={"chip" + (activeGenre.has(g) ? " active" : "")} onClick={() => toggle(activeGenre, setActiveGenre, g)}>{g}</span>
                ))}
              </div>
            </div>
            <div className="filter-group">
              <div className="filter-group-head"><span>Sort by</span><div className="line" /></div>
              <select className="select" value={sort} onChange={(e) => setSort(e.target.value)}>
                <option value="random">Random</option>
                <option value="recent">Recently Added</option>
                <option value="az">Title A–Z</option>
                <option value="progress">Progress</option>
              </select>
            </div>
          </div>
        </div>

        {(() => {
          // Do web results exist? If so, they become the PRIMARY content and the
          // local-library empty-state collapses to a single quiet line — instead
          // of a full-height 無 placeholder burying the results below the fold.
          const hasWeb = Array.isArray(webResults) && webResults.length > 0;
          // Are all web results from ONE source? Then show the source once in the
          // header, not a badge on every card.
          const webSources = hasWeb ? [...new Set(webResults.map((r) => r.source_label))] : [];
          const singleSource = webSources.length === 1;
          return (
        <div className="search-panel-body">
          {results.length > 0 ? (
            <>
              <div className="grid density-dense">
                {shown.map((c) => (
                  <window.LibraryCard key={c.id} item={c} compact onOpen={(it) => { onClose(); onOpenDetail(it); }} />
                ))}
              </div>
              {results.length > RESULT_CAP && (
                <div style={{ textAlign: "center", padding: "20px 0 4px", fontSize: 12, color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
                  Showing first {RESULT_CAP} of {results.length} — type to narrow your search.
                </div>
              )}
            </>
          ) : hasWeb ? (
            // No local matches but we have web results → just a quiet line, results below.
            <div className="search-nolocal">No matches in your library — showing web results below.</div>
          ) : (
            <div className="empty"><div className="glyph">無</div><p>Not in your library. Search the web to import it.</p></div>
          )}

          {/* Search the web (MangaDex etc.) — on-demand. Shown when there's a
              non-URL query, most useful when few/no local matches. */}
          {!isUrl && debouncedQ.trim() && (
            <div className="web-search-section">
              {webResults === null ? (
                <button className="web-search-btn" disabled={webBusy} onClick={searchWeb}>
                  <window.IconSearch size={15} />
                  <span className="web-search-label">
                    {webBusy ? "Searching the web…" : `Search the web for “${debouncedQ.trim()}”`}
                  </span>
                </button>
              ) : (
                <>
                  <div className="web-search-head">
                    <span>Web results{webResults.length > 0 ? ` · ${webResults.length}` : ""}{singleSource ? ` · ${webSources[0]}` : ""}</span>
                    <button className="btn ghost sm" onClick={searchWeb} disabled={webBusy}>
                      {webBusy ? "…" : "Search again"}
                    </button>
                  </div>
                  {webResults.length === 0 ? (
                    <div className="web-search-empty">No results found online for “{debouncedQ.trim()}”.</div>
                  ) : (
                    <div className="web-results">
                      {webResults.map((r, i) => (
                        <button key={r.url || i} className="web-result-card"
                          title={`Preview “${r.title}” from ${r.source_label}`}
                          onClick={() => { (onPreviewLink || onImportLink)?.(r.url); }}>
                          <div className="web-result-cover"><WebCover url={r.cover_url} /></div>
                          <div className="web-result-meta">
                            <div className="web-result-title">{r.title}</div>
                            <div className="web-result-sub">
                              <span className="web-result-author">{r.author || "Unknown"}</span>
                              {r.year ? <span className="web-result-year"> · {r.year}</span> : null}
                              {typeof r.chapter_count === "number" && r.chapter_count >= 0 ? (
                                <span className="web-result-chapters"> · {r.chapter_count} ch</span>
                              ) : null}
                            </div>
                            {/* Per-card badge only when results span MULTIPLE sources
                                (otherwise the source is in the header). */}
                            {!singleSource && <div className="web-result-badge">{r.source_label}</div>}
                          </div>
                        </button>
                      ))}
                    </div>
                  )}
                </>
              )}
              {webErr && <div className="web-search-empty" style={{ color: "var(--vermillion)" }}>{webErr}</div>}
            </div>
          )}
        </div>
          );
        })()}
    </window.Modal>
  );
}

Object.assign(window, { Reader, SearchPanel });
