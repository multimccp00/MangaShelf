/* global window, React */
const { useState, useRef, useEffect } = React;

// Custom dropdown — replaces native <select> so both the control and the popup
// list are fully themed (native option popups can't be styled cross-browser).
// Fixed width + truncation keeps the filter bar compact regardless of option text.
function Dropdown({ value, options, onChange, width = 130 }) {
  const [open, setOpen] = useState(false);
  const [rect, setRect] = useState(null);   // button position, for the fixed-position menu
  const [activeIdx, setActiveIdx] = useState(-1);   // keyboard-highlighted option
  const rootRef = useRef(null);
  const btnRef = useRef(null);
  const menuRef = useRef(null);

  // The menu is rendered with position:fixed (see below) so it escapes the
  // filter bar — on mobile the bar is overflow-x:auto, which CLIPS a normally-
  // positioned dropdown; and each card's `perspective` makes its own stacking
  // context that would otherwise paint over an absolutely-positioned menu.
  // A fixed menu anchored to the button's measured rect dodges both problems.
  function measure() {
    if (btnRef.current) setRect(btnRef.current.getBoundingClientRect());
  }

  function openMenu() {
    // Highlight the current value when opening so arrow keys start from there.
    setActiveIdx(Math.max(0, options.indexOf(value)));
    setOpen(true);
  }
  function choose(opt) { onChange(opt); setOpen(false); btnRef.current?.focus(); }

  useEffect(() => {
    if (!open) return;
    measure();
    function onDoc(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    }
    // Reposition (or close) the menu if the user scrolls/resizes while it's open.
    function onScroll() { measure(); }
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("resize", onScroll);
    // capture:true so we also catch scrolls inside the filter bar / page panes.
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("resize", onScroll);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  // Keep the highlighted option scrolled into view as arrow keys move it.
  useEffect(() => {
    if (!open || activeIdx < 0 || !menuRef.current) return;
    const el = menuRef.current.children[activeIdx];
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  }, [activeIdx, open]);

  // Full keyboard model on the trigger: Enter/Space/Down opens; when open,
  // Up/Down move, Enter/Space select, Escape closes, Home/End jump.
  function onBtnKey(e) {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") { e.preventDefault(); openMenu(); }
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); setOpen(false); btnRef.current?.focus(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setActiveIdx((i) => Math.min(options.length - 1, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActiveIdx((i) => Math.max(0, i - 1)); }
    else if (e.key === "Home") { e.preventDefault(); setActiveIdx(0); }
    else if (e.key === "End") { e.preventDefault(); setActiveIdx(options.length - 1); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (activeIdx >= 0) choose(options[activeIdx]); }
  }

  const menuStyle = rect
    ? { position: "fixed", top: rect.bottom + 4, left: rect.left, width: rect.width }
    : null;
  const listId = "dd-list";

  return (
    <div className="dd" ref={rootRef} style={{ width }}>
      <button
        ref={btnRef}
        type="button"
        className={"dd-btn" + (open ? " open" : "")}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={onBtnKey}
        title={value}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
      >
        <span className="dd-value">{value}</span>
        <span className="dd-caret"><IconChevronDown size={13} /></span>
      </button>
      {open && menuStyle && (
        <div className="dd-menu dd-menu-fixed" style={menuStyle} role="listbox" id={listId} ref={menuRef}>
          {options.map((opt, i) => (
            <button
              type="button"
              key={opt}
              role="option"
              aria-selected={opt === value}
              className={"dd-item" + (opt === value ? " active" : "") + (i === activeIdx ? " kb-active" : "")}
              title={opt}
              onMouseEnter={() => setActiveIdx(i)}
              onClick={() => choose(opt)}
            >
              {opt}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function CoverImg({ item, onLoad }) {
  const [err, setErr] = useState(false);
  const imgRef = useRef(null);

  // Cached images can finish loading before React attaches onLoad, so the
  // handler never fires and the card stays at opacity:0 (a blank grid gap).
  // Catch that case: if the <img> is already complete on mount, fire onLoad
  // ourselves. naturalWidth === 0 on a complete img means it failed to decode.
  useEffect(() => {
    const el = imgRef.current;
    if (!el) return;
    if (el.complete) {
      if (el.naturalWidth > 0) onLoad?.();
      else setErr(true);
    }
  }, []);

  // Fallback tile so a failed cover never leaves an invisible/blank card —
  // the parent still gets onLoad so the card animates in.
  if (err || !item.coverUrl) {
    return (
      <div
        className="img cover-fallback"
        ref={() => onLoad?.()}
        style={{
          display: "flex", alignItems: "center", justifyContent: "center",
          background: `linear-gradient(155deg, hsl(${item.hue || 220} 32% 24%), hsl(${item.hue || 220} 30% 14%))`,
          color: "rgba(255,255,255,0.85)", textAlign: "center", padding: "16px",
        }}
      >
        <span style={{ fontFamily: "var(--font-serif-jp), serif", fontSize: 40, opacity: 0.85 }}>
          {item.kanji || "本"}
        </span>
      </div>
    );
  }

  return (
    <img
      ref={imgRef}
      className="img cover-img"
      src={window.coverUrl(item)}
      alt={item.title}
      // Lazy-load + low priority: a big library renders 60 cards at once. Without
      // this the browser fires 60 cover requests immediately, saturating its ~6
      // connections/host — so a getSeries() API call (opening a detail) QUEUES
      // behind them and appears to "load forever". Lazy loading only fetches
      // covers as they scroll into view, keeping the connection pool free for
      // navigation/API calls.
      loading="lazy"
      decoding="async"
      fetchpriority="low"
      onLoad={onLoad}
      onError={() => setErr(true)}
    />
  );
}

function ProgressRing({ pct, size = 36, color }) {
  const r = size / 2 - 3;
  const c = 2 * Math.PI * r;
  const off = c - (c * pct) / 100;
  const style = { width: size, height: size };
  if (color) style["--ring-color"] = color;
  return (
    <div className="progress-ring" style={style}>
      <svg viewBox={`0 0 ${size} ${size}`}>
        <circle className="track" cx={size / 2} cy={size / 2} r={r} />
        <circle className="bar" cx={size / 2} cy={size / 2} r={r}
          strokeDasharray={c} strokeDashoffset={off} />
      </svg>
      <span className="num">{pct}%</span>
    </div>
  );
}

// Order defines the click-cycle on cards (and the hover "next status" preview):
// a natural reading lifecycle — Planned comes right after Not Started.
const STATUSES = ["Not Started", "Planned to Read", "Reading", "Completed", "On Hold", "Dropped"];

// Shared card behavior: favorite toggle, status cycling, and delete — all with
// optimistic updates that revert (and alert) on failure. Keeps the local fav/
// status in sync when the underlying item is swapped by a reload(), and patches
// the shared STORE item so other screens reflect the change. Used by every card
// variant so this logic lives in exactly one place.
function useSeriesActions(item, { onDeleted } = {}) {
  const [fav, setFav] = useState(!!item.favorite);
  const [status, setStatus] = useState(item.status || "Not Started");

  // Resync when reload() swaps STORE.items without unmounting (key is stable by id).
  useEffect(() => {
    setFav(!!item.favorite);
    setStatus(item.status || "Not Started");
  }, [item.id, item.favorite, item.status]);

  // Mirror a change into the matching STORE item so the library/landing update too.
  function patchStore(patch) {
    const s = (window.STORE.items || []).find((x) => x.id === item.id);
    if (s) Object.assign(s, patch);
  }

  function toggleFav(e) {
    if (e) e.stopPropagation();
    const next = !fav;
    setFav(next);
    item.favorite = next;
    patchStore({ favorite: next });
    window.ApiClient.setFavorite(item.id, next).catch((err) => {
      setFav(!next);
      item.favorite = !next;
      patchStore({ favorite: !next });
      window.toast(`Favorite update failed: ${err.message || err}`, "error");
    });
  }

  function setStatusTo(next) {
    const prev = status;
    setStatus(next);
    item.status = next;
    patchStore({ status: next });
    window.ApiClient.setStatus(item.id, next).catch((err) => {
      setStatus(prev);
      item.status = prev;
      patchStore({ status: prev });
      window.toast(`Status update failed: ${err.message || err}`, "error");
    });
  }

  function cycleStatus(e) {
    if (e) e.stopPropagation();
    setStatusTo(STATUSES[(STATUSES.indexOf(status) + 1) % STATUSES.length]);
  }

  async function deleteCard(e) {
    if (e) e.stopPropagation();
    // Doujinshi titles run to 100+ chars; an untruncated one turns the dialog
    // title into a paragraph. The full name stays visible on the card behind it.
    const shortTitle = item.title.length > 58 ? item.title.slice(0, 58).trimEnd() + "…" : item.title;
    const res = await window.confirmDialog({
      title: `Remove “${shortTitle}”?`,
      message: "Removes it from the library.",
      confirmLabel: "Remove",
      tone: "danger",
      checkboxes: [
        {
          id: "disk",
          danger: true,
          label: "Also move the folder to the recycle bin",
          hint: "Recoverable from the bin; leave unchecked to keep the files.",
          // The default follows the "deleting also deletes files" setting, but
          // the box is always visible so every delete is an explicit choice.
          checked: !!window.Settings.deleteFromDisk,
        },
        { id: "remember", label: "Remember this choice as my default", checked: false },
      ],
    });
    if (!res || !res.ok) return;
    const disk = !!res.checks.disk;
    if (res.checks.remember && disk !== !!window.Settings.deleteFromDisk) {
      window.Settings.set({ deleteFromDisk: disk });
    }
    window.ApiClient.deleteSeries(item.id, disk)
      .then((r) => {
        window.STORE.items = (window.STORE.items || []).filter((x) => x.id !== item.id);
        onDeleted?.(item.id);
        if (disk) window.toast(r.disk === "recycled" ? "Folder moved to the recycle bin." : "Removed (folder was already gone).", "ok");
      })
      .catch((err) => window.toast(`Delete failed: ${err.message || err}`, "error"));
  }

  return { fav, status, toggleFav, cycleStatus, setStatusTo, deleteCard };
}

// Inline SVG icons
function IconStar({ size = 16 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
    </svg>
  );
}
function IconCycle({ size = 14 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
      <path d="M3 3v5h5"/>
    </svg>
  );
}
function IconTrash({ size = 13 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6"/>
      <path d="M19 6l-1 14H6L5 6"/>
      <path d="M10 11v6M14 11v6"/>
      <path d="M9 6V4h6v2"/>
    </svg>
  );
}
function IconBookmark({ size = 14 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
    </svg>
  );
}
function IconStarOutline({ size = 16 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
      <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
    </svg>
  );
}
function IconArrowLeft({ size = 16 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="19" y1="12" x2="5" y2="12"/>
      <polyline points="12 19 5 12 12 5"/>
    </svg>
  );
}
function IconArrowRight({ size = 16 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="5" y1="12" x2="19" y2="12"/>
      <polyline points="12 5 19 12 12 19"/>
    </svg>
  );
}
function IconChevronLeft({ size = 22 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="15 18 9 12 15 6"/>
    </svg>
  );
}
function IconChevronRight({ size = 22 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="9 18 15 12 9 6"/>
    </svg>
  );
}
function IconChevronDown({ size = 14 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="6 9 12 15 18 9"/>
    </svg>
  );
}
function IconClose({ size = 16 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18"/>
      <line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
  );
}
function IconPlus({ size = 14 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="5" x2="12" y2="19"/>
      <line x1="5" y1="12" x2="19" y2="12"/>
    </svg>
  );
}
function IconSearch({ size = 18 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="11" cy="11" r="7"/>
      <line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
  );
}
function IconPlay({ size = 14 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M8 5v14l11-7z"/>
    </svg>
  );
}
function IconScissors({ size = 14 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="3"/>
      <circle cx="6" cy="18" r="3"/>
      <line x1="20" y1="4" x2="8.12" y2="15.88"/>
      <line x1="14.47" y1="14.48" x2="20" y2="20"/>
      <line x1="8.12" y1="8.12" x2="12" y2="12"/>
    </svg>
  );
}
function IconCheck({ size = 14 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  );
}
function IconDot({ size = 12 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <circle cx="12" cy="12" r="6"/>
    </svg>
  );
}
function IconPencil({ size = 13 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 20h9"/>
      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>
    </svg>
  );
}
function IconGear({ size = 16 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </svg>
  );
}
function IconLibrary({ size = 18 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
    </svg>
  );
}
function IconLock({ size = 12 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="11" width="18" height="11" rx="2"/>
      <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
    </svg>
  );
}
function IconEye({ size = 16 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/>
      <circle cx="12" cy="12" r="3"/>
    </svg>
  );
}
function IconEyeOff({ size = 16 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9.9 5.2A9.5 9.5 0 0 1 12 5c6.5 0 10 7 10 7a16.8 16.8 0 0 1-3.2 4.1M6.3 6.3A16.7 16.7 0 0 0 2 12s3.5 7 10 7a9.3 9.3 0 0 0 4.7-1.3"/>
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>
      <path d="M3 3l18 18"/>
    </svg>
  );
}

// Password input with a built-in show/hide eye toggle. Drop-in for a plain
// <input type="password">: pass className, value, onChange, placeholder, etc.
function PasswordInput({ className, value, onChange, placeholder, autoComplete, inputRef }) {
  const [show, setShow] = useState(false);
  return (
    <div className="pw-field">
      <input
        ref={inputRef}
        className={(className || "") + " pw-field-input"}
        type={show ? "text" : "password"}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        autoComplete={autoComplete}
      />
      <button
        type="button"
        className="pw-field-toggle"
        onClick={() => setShow((s) => !s)}
        title={show ? "Hide password" : "Show password"}
        aria-label={show ? "Hide password" : "Show password"}
        tabIndex={-1}
      >
        {show ? <IconEyeOff size={16} /> : <IconEye size={16} />}
      </button>
    </div>
  );
}
// Density toggle glyphs — increasing grid tightness, drawn as SVG grids.
function IconDensity({ cols = 2, size = 16 }) {
  const gap = 2.5;
  const pad = 2.5;
  const span = 24 - pad * 2;
  const cell = (span - gap * (cols - 1)) / cols;
  const rects = [];
  for (let r = 0; r < cols; r++) {
    for (let c = 0; c < cols; c++) {
      rects.push(
        <rect key={`${r}-${c}`}
          x={pad + c * (cell + gap)} y={pad + r * (cell + gap)}
          width={cell} height={cell} rx={cols >= 4 ? 0.5 : 1} />
      );
    }
  }
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      {rects}
    </svg>
  );
}

function LibraryCard({ item, onOpen, compact, onDeleted }) {
  const ref = useRef(null);
  const [imgReady, setImgReady] = useState(false);
  const { fav, status, toggleFav, cycleStatus, deleteCard } = useSeriesActions(item, { onDeleted });

  // Safety net: a card must never stay invisible (opacity:0) just because an
  // image load event was missed — reveal it after a short grace period.
  useEffect(() => {
    const t = setTimeout(() => setImgReady(true), 1500);
    return () => clearTimeout(t);
  }, []);

  const pct = item.pages ? Math.round((item.read / item.pages) * 100) : 0;
  const unread = Math.max(0, item.pages - item.read);
  const showUnread = unread > 0 && item.read > 0;
  const statusColor = window.STATUS_COLOR[status] || "#c3c3c3";
  // The status this bubble will switch to next, and its colour — previewed on
  // hover so you can see where a click will take it.
  const nextStatus = STATUSES[(STATUSES.indexOf(status) + 1) % STATUSES.length];
  const nextStatusColor = window.STATUS_COLOR[nextStatus] || "#c3c3c3";
  const [statusHover, setStatusHover] = useState(false);

  const stripStyle = {
    "--strip-color": statusColor,
    "--strip-bg": `linear-gradient(180deg, rgba(11,11,16,0.78), rgba(11,11,16,0.86))`,
  };

  return (
    <div
      className={"card" + (compact ? " card-compact" : "") + (imgReady ? " card-ready" : "") + (item.available === false ? " card-missing" : "")}
      onClick={() => onOpen?.(item)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen?.(item); } }}
      role="button"
      tabIndex={0}
      aria-label={item.title}
    >
      <div className="card-cover" ref={ref}>
        <CoverImg item={item} onLoad={() => setImgReady(true)} />
        {item.available === false && (
          <div className="missing-badge" title="This folder wasn't found on disk at the last scan">Folder missing</div>
        )}
        <div className="cover-title-overlay" style={stripStyle}>
          <h3 className="cover-title">{item.title}</h3>
          {!compact ? (
            <div className="cover-meta">
              <span className="cover-meta-chapters">{window.progressLabel(item)}</span>
              {item.author && window.progressLabel(item) ? <span className="sep" /> : null}
              {item.author && <span className="cover-meta-author">{item.author}</span>}
            </div>
          ) : item.author ? (
            // Compact (search) cards: show just the author on one truncated line.
            <div className="cover-meta">
              <span className="cover-meta-author">{item.author}</span>
            </div>
          ) : null}
        </div>
        {!compact && (
          <div className={"card-hover-actions" + (fav ? " fav-pinned" : "")} onClick={(e) => e.stopPropagation()}>
            <button className={"card-action-btn fav-bubble" + (fav ? " on" : "")} title={fav ? "Unfavorite" : "Favorite"} onClick={toggleFav}>
              <IconStar size={17} />
            </button>
            <button className="card-action-btn status-bubble"
              title={`Status: ${status} — click to set ${nextStatus}`}
              onClick={cycleStatus}
              onMouseEnter={() => setStatusHover(true)}
              onMouseLeave={() => setStatusHover(false)}
              style={statusHover
                ? { color: nextStatusColor, borderColor: `${nextStatusColor}99` }
                : { color: statusColor, borderColor: `${statusColor}55` }}>
              <IconCycle size={14} />
            </button>
            <button className="card-action-btn delete-bubble" title="Remove from library" onClick={deleteCard}>
              <IconTrash size={13} />
            </button>
          </div>
        )}
        <div className="badge-row">
          {showUnread ? <div className="unread-badge">{unread} NEW</div>
            : item.read === 0 ? <div className="unread-badge" style={{background:"rgba(11,11,16,0.7)",color:"var(--text-dim)"}}>NEW</div>
            : <span />}
        </div>
        {!compact && (
          <div className="status-tag" style={{ borderColor: `${statusColor}66` }}>
            <span className="dot" style={{ background: statusColor }} />
            {status}
          </div>
        )}
      </div>
    </div>
  );
}

// Continue Reading rail card — same 3 hover bubbles as LibraryCard.
function RailCard({ item, onOpen, onDeleted }) {
  const [imgReady, setImgReady] = useState(false);
  const { fav, status, toggleFav, cycleStatus, deleteCard } = useSeriesActions(item, { onDeleted });
  const statusColor = window.STATUS_COLOR[status] || "#c3c3c3";
  const nextStatus = STATUSES[(STATUSES.indexOf(status) + 1) % STATUSES.length];
  const nextStatusColor = window.STATUS_COLOR[nextStatus] || "#c3c3c3";
  const [statusHover, setStatusHover] = useState(false);
  const stripStyle = {
    "--strip-color": statusColor,
    "--strip-bg": `linear-gradient(180deg, rgba(11,11,16,0.78), rgba(11,11,16,0.86))`,
  };

  return (
    <div
      className={"rail-card" + (imgReady ? " rail-card-ready" : "")}
      onClick={() => onOpen?.(item)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen?.(item); } }}
      role="button"
      tabIndex={0}
      aria-label={item.title}
    >
      <div className="rail-card-cover">
        <CoverImg item={item} onLoad={() => setImgReady(true)} />
        {/* Re-sync found chapters newer than the caught-up read position. */}
        {!!item.fresh_chapters && <span className="rail-card-new">NEW CH</span>}
        <div className="rail-card-overlay" style={stripStyle}>
          <div className="rail-card-title">{item.title}</div>
          <div className="rail-card-sub cover-meta">
            <span className="cover-meta-chapters">{window.progressLabel(item)}</span>
            {item.author && window.progressLabel(item) ? <span className="sep" /> : null}
            {item.author && <span className="cover-meta-author">{item.author}</span>}
          </div>
        </div>
        {item.read > 0 && item.read < item.pages && (
          <div className="rail-card-bar rail-card-bar-progress"><span style={{width: `${(item.read/item.pages)*100}%`, background: statusColor}} /></div>
        )}
        <div className={"rail-hover-actions" + (fav ? " fav-pinned" : "")} onClick={(e) => e.stopPropagation()}>
          <button className={"card-action-btn fav-bubble" + (fav ? " on" : "")} title={fav ? "Unfavorite" : "Favorite"} onClick={toggleFav}>
            <IconStar size={17} />
          </button>
          <button className="card-action-btn status-bubble"
            title={`Status: ${status} — click to set ${nextStatus}`}
            onClick={cycleStatus}
            onMouseEnter={() => setStatusHover(true)}
            onMouseLeave={() => setStatusHover(false)}
            style={statusHover
              ? { color: nextStatusColor, borderColor: `${nextStatusColor}99` }
              : { color: statusColor, borderColor: `${statusColor}55` }}>
            <IconCycle size={14} />
          </button>
          <button className="card-action-btn delete-bubble" title="Remove from library" onClick={deleteCard}>
            <IconTrash size={13} />
          </button>
        </div>
      </div>
    </div>
  );
}

// Rail card for the New Reads section — hovering shows a "Plan to Read" bubble.
function NewReadsRailCard({ item, onOpen, onStatusChange }) {
  const [imgReady, setImgReady] = useState(false);
  const { status, setStatusTo } = useSeriesActions(item);
  const statusColor = window.STATUS_COLOR[status] || "#c3c3c3";
  const stripStyle = {
    "--strip-color": statusColor,
    "--strip-bg": `linear-gradient(180deg, rgba(11,11,16,0.78), rgba(11,11,16,0.86))`,
  };

  const isPlanned = status === "Planned to Read";
  function markPlanned(e) {
    e.stopPropagation();
    // Toggle: click again to clear back to Not Started. Toast so the action is
    // visibly confirmed (the card doesn't move, so without this it looked dead).
    const next = isPlanned ? "Not Started" : "Planned to Read";
    setStatusTo(next);
    onStatusChange?.(item.id, next);
    window.toast(isPlanned ? "Removed from Planned to Read." : `“${item.title}” → Planned to Read.`, "ok");
  }

  return (
    <div
      className={"rail-card" + (imgReady ? " rail-card-ready" : "")}
      onClick={() => onOpen?.(item)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen?.(item); } }}
      role="button"
      tabIndex={0}
      aria-label={item.title}
    >
      <div className="rail-card-cover">
        <CoverImg item={item} onLoad={() => setImgReady(true)} />
        <div className="rail-card-overlay" style={stripStyle}>
          <div className="rail-card-title">{item.title}</div>
          <div className="rail-card-sub cover-meta">
            <span className="cover-meta-chapters">{window.progressLabel(item) || status}</span>
            {item.author && (window.progressLabel(item) || status) ? <span className="sep" /> : null}
            {item.author && <span className="cover-meta-author">{item.author}</span>}
          </div>
        </div>
        <div className={"rail-hover-actions" + (isPlanned ? " fav-pinned" : "")} onClick={(e) => e.stopPropagation()}>
          <button className={"card-action-btn plan-bubble" + (isPlanned ? " on" : "")}
                  title={isPlanned ? "Planned to Read — click to clear" : "Mark as Planned to Read"} onClick={markPlanned}>
            <IconBookmark size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

// A stack of open modals so that when several are nested (e.g. a preview opened
// ON TOP of the search panel), only the TOPMOST responds to Escape and gets the
// higher z-index — instead of Escape closing both at once and the two overlays
// sharing a stacking level.
const _MODAL_STACK = [];

// ---------------------------------------------------------------------------
// Accessible modal shell: overlay + dialog with a focus trap, focus restore on
// close, Escape-to-dismiss, and the proper dialog ARIA. Content-agnostic — pass
// the modal's inner markup as children and its own className via `panelClass`.
// `onClose` is called on Escape, backdrop click, or a trapped Tab escaping.
function Modal({ onClose, panelClass, labelledBy, children, dismissable = true }) {
  const panelRef = useRef(null);
  const restoreRef = useRef(null);
  const idRef = useRef(null);
  // Depth in the modal stack when this one mounted → a z-index above any modal
  // opened before it, so a nested modal always paints over its parent.
  const [depth, setDepth] = useState(0);

  useEffect(() => {
    // Register on the modal stack; the last-registered is the "topmost".
    const id = {};
    idRef.current = id;
    _MODAL_STACK.push(id);
    setDepth(_MODAL_STACK.length - 1);
    // Remember what was focused so we can restore it when the modal closes.
    restoreRef.current = document.activeElement;
    const panel = panelRef.current;
    // Focus the first focusable element (or the panel itself) on open.
    const focusables = () =>
      panel ? panel.querySelectorAll(
        'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
      ) : [];
    const first = focusables()[0];
    (first || panel)?.focus();

    function isTopmost() {
      return _MODAL_STACK[_MODAL_STACK.length - 1] === idRef.current;
    }

    function onKey(e) {
      // Only the topmost modal reacts to Escape, so a nested preview closes without
      // also dismissing the search panel underneath it.
      if (e.key === "Escape" && dismissable && isTopmost()) { e.preventDefault(); onClose?.(); return; }
      if (e.key !== "Tab") return;
      // Trap Tab within the panel.
      const els = focusables();
      if (!els.length) return;
      const firstEl = els[0];
      const lastEl = els[els.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault(); lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault(); firstEl.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      // Unregister from the modal stack (remove THIS one, wherever it sits).
      const i = _MODAL_STACK.indexOf(idRef.current);
      if (i !== -1) _MODAL_STACK.splice(i, 1);
      // Restore focus to the trigger when the modal unmounts.
      if (restoreRef.current && restoreRef.current.focus) restoreRef.current.focus();
    };
  }, []);

  // Lift each stacked modal above the one below it. Base 40 matches the overlay's
  // CSS z-index; +1 per depth keeps a nested modal (and its backdrop) on top.
  const overlayStyle = depth > 0 ? { zIndex: 40 + depth } : undefined;

  // PORTAL to <body>: a modal opened from deep inside a screen (e.g. Move on the
  // detail page) would otherwise live inside whatever stacking context its parent
  // chain creates — trapping the overlay's z-index below the topbar, which then
  // paints over the dialog. From <body>, z-index 40 always beats the chrome.
  return ReactDOM.createPortal(
    <div className="search-overlay" style={overlayStyle} onClick={dismissable ? onClose : undefined}>
      <div
        className={panelClass}
        ref={panelRef}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        tabIndex={-1}
      >
        {children}
      </div>
    </div>,
    document.body
  );
}

// ---------------------------------------------------------------------------
// Themed confirmation dialog — replaces the browser's native window.confirm()
// (the ugly "<host>:<port> says…" box). Used imperatively:
//
//   const ok = await window.confirmDialog({
//     title: "Switch library?",
//     message: "…",
//     confirmLabel: "Switch",
//     tone: "danger" | "warn" | "default",
//   });
//
// Resolves true on confirm, false on cancel / Escape / backdrop click.
// With `opts.checkboxes` ([{id, label, hint?, checked?}]) it renders opt-in
// checkboxes and resolves an OBJECT instead: { ok, checks: {id: bool} } — so
// destructive extras (e.g. "also delete files") are explicit per action.
function ConfirmDialog({ opts, onResolve }) {
  const confirmRef = useRef(null);
  const [checks, setChecks] = useState(() => {
    const init = {};
    (opts.checkboxes || []).forEach((c) => { init[c.id] = !!c.checked; });
    return init;
  });
  const hasChecks = (opts.checkboxes || []).length > 0;
  const resolve = (ok) => onResolve(hasChecks ? { ok, checks } : ok);

  useEffect(() => {
    // Focus the confirm button so Enter confirms and the dialog is keyboard-first.
    if (confirmRef.current) confirmRef.current.focus();
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); resolve(false); }
      else if (e.key === "Enter") { e.preventDefault(); resolve(true); }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [checks]);

  const tone = opts.tone || "default";
  return (
    <div className="confirm-overlay" onClick={() => resolve(false)}>
      <div className="confirm-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        {opts.title && (
          <div className={"confirm-title tone-" + tone}>
            {tone === "danger" && <IconTrash size={16} />}
            {tone === "warn" && <IconLock size={14} />}
            <span>{opts.title}</span>
          </div>
        )}
        {opts.message && <div className="confirm-message">{opts.message}</div>}
        {hasChecks && (
          <div className="confirm-checks">
            {opts.checkboxes.map((c) => (
              <label key={c.id} className={"confirm-check" + (c.danger ? " is-danger" : "")}>
                <input
                  type="checkbox"
                  checked={!!checks[c.id]}
                  onChange={(e) => setChecks((prev) => ({ ...prev, [c.id]: e.target.checked }))}
                />
                <span>
                  {c.label}
                  {c.hint && <span className="confirm-check-hint">{c.hint}</span>}
                </span>
              </label>
            ))}
          </div>
        )}
        <div className="confirm-actions">
          <button className="confirm-btn ghost" onClick={() => resolve(false)}>
            {opts.cancelLabel || "Cancel"}
          </button>
          <button
            ref={confirmRef}
            className={"confirm-btn primary tone-" + tone}
            onClick={() => resolve(true)}
          >
            {opts.confirmLabel || "Confirm"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Imperative entry point. Mounts the dialog into its own root so it can be
// called from anywhere (event handlers, non-React code) and returns a Promise.
let _confirmRoot = null;
function confirmDialog(opts) {
  return new Promise((resolve) => {
    let host = document.getElementById("confirm-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "confirm-host";
      document.body.appendChild(host);
    }
    if (!_confirmRoot) _confirmRoot = ReactDOM.createRoot(host);
    function finish(result) {
      _confirmRoot.render(null);     // unmount the dialog
      resolve(result);
    }
    _confirmRoot.render(<ConfirmDialog opts={opts || {}} onResolve={finish} />);
  });
}

// ---------------------------------------------------------------------------
// Themed toast notifications — replace window.alert() (the ugly native
// "<host>:<port> says…" box) for transient feedback. Imperative, mounts
// into its own root so it's callable from anywhere:
//
//   window.toast("Cover upload failed: …", "error");   // "error" | "warn" | "ok"
//   window.toast("Created Chapter 12 (18 pages moved).");   // default = "ok"
//
// Auto-dismisses; errors linger longer than successes. Stacks bottom-center.
let _toastRoot = null;
let _toasts = [];
let _toastSeq = 1;
function ToastStack({ items, onExpire }) {
  useEffect(() => {
    const timers = items.map((t) =>
      setTimeout(() => onExpire(t.id), t.tone === "error" ? 6000 : t.tone === "warn" ? 4500 : 3000)
    );
    return () => timers.forEach(clearTimeout);
  }, [items]);
  if (!items.length) return null;
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={"toast toast-" + (t.tone || "ok")}>
          <span className="toast-icon">
            {t.tone === "error" ? "!" : t.tone === "warn" ? "!" : <IconCheck size={13} />}
          </span>
          <span className="toast-msg">{t.msg}</span>
          <button className="toast-x" title="Dismiss" onClick={() => onExpire(t.id)}><IconClose size={12} /></button>
        </div>
      ))}
    </div>
  );
}
function _renderToasts() {
  if (!_toastRoot) {
    let host = document.getElementById("toast-host");
    if (!host) { host = document.createElement("div"); host.id = "toast-host"; document.body.appendChild(host); }
    _toastRoot = ReactDOM.createRoot(host);
  }
  const expire = (id) => { _toasts = _toasts.filter((x) => x.id !== id); _renderToasts(); };
  _toastRoot.render(<ToastStack items={_toasts} onExpire={expire} />);
}
function toast(msg, tone = "ok") {
  _toasts = [..._toasts, { id: _toastSeq++, msg: String(msg), tone }];
  _renderToasts();
}

Object.assign(window, {
  toast,
  CoverImg, ProgressRing, LibraryCard, RailCard, NewReadsRailCard, Dropdown,
  IconStar, IconStarOutline, IconCycle, IconTrash, IconBookmark,
  IconArrowLeft, IconArrowRight, IconChevronLeft, IconChevronRight, IconChevronDown,
  IconClose, IconSearch, IconPlus, IconPlay, IconScissors, IconCheck, IconDot,
  IconPencil, IconDensity, IconGear, IconLibrary, IconLock,
  IconEye, IconEyeOff, PasswordInput,
  ConfirmDialog, confirmDialog, Modal, useSeriesActions,
});
