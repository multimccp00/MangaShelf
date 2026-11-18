/* global window, React */
const { useState: useStateOff, useEffect: useEffectOff } = React;

// Re-render on any offline-manager / connectivity / sync change. Returns whether
// the device currently has a connection.
function useOfflineTick() {
  const [, setTick] = useStateOff(0);
  const [online, setOnline] = useStateOff(navigator.onLine !== false);
  useEffectOff(() => {
    const bump = () => setTick((t) => t + 1);
    const on = () => { setOnline(true); bump(); };
    const off = () => { setOnline(false); bump(); };
    window.addEventListener("ms-offline", bump);
    window.addEventListener("ms-sync", bump);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("ms-offline", bump);
      window.removeEventListener("ms-sync", bump);
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);
  return online && (!window.Offline || window.Offline.reachable !== false);
}

function IconDown({ size = 13 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 4v11" /><polyline points="7 11 12 16 17 11" /><path d="M5 20h14" />
    </svg>
  );
}

const fmtBytes = (b) => b >= 1073741824 ? (b / 1073741824).toFixed(1) + " GB" : b >= 1048576 ? Math.round(b / 1048576) + " MB" : Math.round(b / 1024) + " KB";

// Run a download, surfacing failures as a toast (offline mid-way just pauses it —
// what's already saved is kept and resumes automatically later).
async function runDownload(seriesId, idxs) {
  try { await window.Offline.download(seriesId, idxs); }
  catch (e) {
    if (e && e.name === "AbortError") return;
    window.toast(`Download stopped: ${e.message || e}`, "warn");
  }
}

// Small per-chapter save/saved button on a chapter card.
function ChapterOfflineBtn({ seriesId, ch }) {
  const O = window.Offline;
  if (!O || !O.supported) return null;
  const st = O.chapterState(seriesId, ch.index);
  const busy = O.isDownloading(seriesId);
  const title = st === "done" ? "Saved offline — tap to remove" : st === "partial" ? "Partly saved — tap to finish" : "Save for offline reading";
  function click(e) {
    e.stopPropagation();
    if (st === "done") O.remove(seriesId, [ch.index]);
    else if (!busy) runDownload(seriesId, [ch.index]);
  }
  return (
    <button className={"off-btn " + st} title={title} aria-label={title} onClick={click}>
      {st === "done" ? <window.IconCheck size={12} /> : <IconDown size={12} />}
    </button>
  );
}

// Drawn minus (the app's icon set has a plus but no minus).
function IconMinus({ size = 14 }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

// − 3 + counter used by the "latest N" / "keep next N" rows.
function Stepper({ value, onChange, label }) {
  const set = (v) => onChange(Math.max(1, Math.min(99, v)));
  return (
    <div className="off-stepper" role="group" aria-label={label}>
      <button type="button" aria-label="Fewer" onClick={() => set(value - 1)} disabled={value <= 1}><IconMinus size={13} /></button>
      <input type="number" inputMode="numeric" min="1" max="99" value={value} aria-label={label}
        onChange={(e) => set(parseInt(e.target.value, 10) || 1)} />
      <button type="button" aria-label="More" onClick={() => set(value + 1)}><window.IconPlus size={13} /></button>
    </div>
  );
}

// "Save offline" button + the panel of choices under it.
function OfflineMenu({ seriesId, chapterRows }) {
  const online = useOfflineTick();
  const O = window.Offline;
  const [open, setOpen] = useStateOff(false);
  const [nLast, setNLast] = useStateOff(3);
  const [nKeep, setNKeep] = useStateOff(3);
  if (!O) return null;
  if (!O.supported) {
    return (
      <button className="btn lg" onClick={() => window.toast(
        "Offline saving needs a secure address (https://…). Open the app through your Tailscale https link, not http://100.x.x.x.", "warn")}>
        <IconDown size={15} /> Save offline
      </button>
    );
  }

  const dl = O.isDownloading(seriesId);
  const prog = O.progress(seriesId);
  const state = O.seriesState(seriesId);
  const keep = O.keepN(seriesId);
  const unread = chapterRows.filter((c) => !c.isRead);
  const pagesOf = (rows) => rows.reduce((a, c) => a + c.page_count, 0);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  async function start(rows) {
    if (!rows.length) { window.toast("Nothing to save", "info"); return; }
    setOpen(false);
    const pages = pagesOf(rows);
    if (pages > 2000) {
      const ok = await window.confirmDialog({
        title: "Large download",
        message: `${plural(rows.length, "chapter")}, ${pages} pages. This can use several GB on this device.`,
        confirmLabel: "Download", tone: "warn",
      });
      if (!ok) return;
    }
    runDownload(seriesId, rows.map((c) => c.index));
  }

  if (dl) {
    const pct = prog.total ? Math.round((prog.done / prog.total) * 100) : 0;
    return (
      <button className="btn lg" onClick={() => O.pause(seriesId)} title="Pause (what's saved is kept)">
        <IconDown size={15} /> Saving… {pct}% · Pause
      </button>
    );
  }

  return (
    <div className="off-menu-wrap">
      <button className={"btn lg" + (state === "done" ? " off-done" : "") + (open ? " open" : "")}
        aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <IconDown size={15} /> {state === "done" ? "Saved offline" : state === "partial" ? "Partly saved" : "Save offline"}
        <span className="off-caret"><window.IconChevronDown size={14} /></span>
      </button>
      {open && (
        <div className="off-menu" role="menu">
          {!online && <div className="off-note">You're offline — downloads need a connection.</div>}
          <button className="btn off-opt" role="menuitem" onClick={() => start(chapterRows)}>
            <b>All chapters</b>
            <span>{plural(chapterRows.length, "chapter")} · {pagesOf(chapterRows)} pages</span>
          </button>
          <button className="btn off-opt" role="menuitem" disabled={!unread.length} onClick={() => start(unread)}>
            <b>Unread chapters</b>
            <span>{unread.length ? `${plural(unread.length, "chapter")} · ${pagesOf(unread)} pages` : "You're all caught up"}</span>
          </button>
          <div className="off-opt-row">
            <Stepper value={nLast} onChange={setNLast} label="Number of latest chapters" />
            <button className="btn off-opt grow" role="menuitem" onClick={() => start(chapterRows.slice(-nLast))}>
              <b>Latest {plural(Math.min(nLast, chapterRows.length), "chapter")}</b>
              <span>The newest ones</span>
            </button>
          </div>
          {keep ? (
            <button className="btn off-opt" role="menuitem" onClick={() => { O.setKeepN(seriesId, 0); setOpen(false); }}>
              <b>Stop keeping chapters ahead</b>
              <span>Currently keeping the next {keep}</span>
            </button>
          ) : (
            <div className="off-opt-row">
              <Stepper value={nKeep} onChange={setNKeep} label="Chapters to keep ahead" />
              <button className="btn off-opt grow" role="menuitem" onClick={() => { O.setKeepN(seriesId, nKeep); setOpen(false); }}>
                <b>Keep next {nKeep} ready</b>
                <span>Tops up as you read</span>
              </button>
            </div>
          )}
          {state !== "none" && (
            <button className="btn off-opt off-danger" role="menuitem" onClick={() => { O.remove(seriesId); setOpen(false); }}>
              <b>Remove offline copy</b>
              <span>Deletes it from this device only</span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// Slim status strip at the top of the workspace (in the layout flow, so it never
// covers the buttons below it): offline state + changes waiting to sync.
function OfflinePills() {
  const online = useOfflineTick();
  const pending = window.SyncQueue ? window.SyncQueue.pending() : 0;
  if (online && !pending) return null;
  return (
    <div className={"off-strip" + (online ? "" : " warn")} role="status">
      {!online && <span>Offline — showing what's saved on this device</span>}
      {pending > 0 && (
        <button className="btn ghost sm" title="Reading progress made offline; it syncs when you're connected"
          onClick={() => window.SyncQueue.flush()}>{pending} to sync</button>
      )}
    </div>
  );
}

// Downloaded library: works with no connection (covers/Detail come from cache).
function Downloaded({ onOpenSeries, onBack }) {
  useOfflineTick();
  const list = window.Offline ? window.Offline.list().filter((s) => s.chapters > 0) : [];
  return (
    <div className="dl-screen">
      <div className="dl-head">
        {onBack && <button className="btn ghost sm" onClick={onBack}>← Back</button>}
        <h2>Downloaded</h2>
      </div>
      {list.length === 0 ? (
        <div className="empty"><p>Nothing saved yet. Open a series and use “Save offline”.</p></div>
      ) : (
        <div className="dl-grid">
          {list.map((s) => (
            <button key={s.id} className="dl-card" onClick={() => onOpenSeries(window.normalize({ id: s.id, title: s.title, cover_v: s.cover_v }))}>
              <img src={window.coverUrl({ id: s.id, cover_v: s.cover_v })} alt="" loading="lazy" />
              <div className="dl-meta">
                <b>{s.title}</b>
                <span>{s.done}/{s.chapters} ch · {fmtBytes(s.bytes)}</span>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Storage dashboard + offline preferences, shown inside Settings. Reuses the
// Settings modal's own rows (toggle rows, library-style list) so it matches.
const QUALITY = [["Original", 0], ["Reduced (1600 px)", 1600], ["Small (1080 px)", 1080]];

function OfflineSettings() {
  useOfflineTick();
  const O = window.Offline;
  const [usage, setUsage] = useStateOff(null);
  const [name, setName] = useStateOff(window.SyncQueue ? window.SyncQueue.device() : "");
  const list = O ? O.list() : [];
  const sig = list.map((s) => s.bytes).join(",");
  useEffectOff(() => { if (O) O.usage().then(setUsage); }, [sig]);
  if (!O) return null;
  if (!O.supported) {
    return <div className="settings-section-hint">Offline reading needs a secure address (https://… or localhost). It isn't available on this one.</div>;
  }
  const p = O.prefs();
  const pct = usage && usage.quota ? Math.min(100, (usage.used / usage.quota) * 100) : 0;
  const Toggle = window.Toggle;
  // "Download location" — only in the Android app and only when a removable card is mounted.
  const st = O.storage;
  const cards = st ? st.volumes().filter((v) => v.removable) : [];
  const loc = st ? st.location() : "";
  const IN_APP = "Inside the app";
  const cardLabel = (v) => `${v.label} · ${fmtBytes(v.free)} free`;
  const curCard = cards.find((v) => v.path === loc);
  return (
    <>
      {usage && (
        <>
          <div className="settings-section-hint">
            {fmtBytes(usage.used)} saved on this device{usage.quota ? ` · ${fmtBytes(usage.quota)} available` : ""}.{" "}
            {usage.persisted ? "Protected from automatic cleanup." : "The browser may clear it if space runs low — installing the app helps."}
          </div>
          {usage.quota > 0 && <div className="off-meter"><span style={{ width: `${Math.max(pct, usage.used ? 1 : 0)}%` }} /></div>}
        </>
      )}
      <div className="settings-toggle-row">
        <div className="settings-toggle-label">
          <div>Download on Wi-Fi only</div>
          <div className="settings-toggle-hint">Keeps big downloads off mobile data. Works on Android; iPhone can't tell the difference.</div>
        </div>
        <Toggle checked={p.wifiOnly} onChange={(v) => O.setPrefs({ wifiOnly: v })} />
      </div>
      <div className="settings-toggle-row">
        <div className="settings-toggle-label">
          <div>Delete chapters after I read them</div>
          <div className="settings-toggle-hint">Frees space automatically once you've moved past a downloaded chapter.</div>
        </div>
        <Toggle checked={p.autoDelete} onChange={(v) => O.setPrefs({ autoDelete: v })} />
      </div>
      <div className="settings-toggle-row">
        <div className="settings-toggle-label">
          <div>Image quality</div>
          <div className="settings-toggle-hint">Smaller pages use less space and download faster. Applies to new downloads.</div>
        </div>
        <window.Dropdown width={170} value={(QUALITY.find((q) => q[1] === p.width) || QUALITY[0])[0]}
          options={QUALITY.map((q) => q[0])}
          onChange={(label) => O.setPrefs({ width: QUALITY.find((q) => q[0] === label)[1] })} />
      </div>
      {cards.length > 0 && (
        <div className="settings-toggle-row">
          <div className="settings-toggle-label">
            <div>Download location</div>
            <div className="settings-toggle-hint">Where new chapters are saved. Chapters you've already saved stay where they are until you move them.</div>
          </div>
          <window.Dropdown width={230} value={curCard ? cardLabel(curCard) : IN_APP}
            options={[IN_APP, ...cards.map(cardLabel)]}
            onChange={(label) => { const v = cards.find((c) => cardLabel(c) === label); st.setLocation(v ? v.path : ""); }} />
        </div>
      )}
      {cards.length > 0 && list.length > 0 && (
        <div className="settings-toggle-row">
          <div className="settings-toggle-label">
            <div>Move saved chapters</div>
            <div className="settings-toggle-hint">Moves everything you've already saved to {curCard ? curCard.label : "the app's own storage"}. Keep the app open until it finishes.</div>
          </div>
          <button className="btn ghost sm" disabled={!!O.moving()} onClick={async () => {
            const r = await O.move();
            if (window.toast) window.toast(r.failed ? `Moved ${r.moved} files, ${r.failed} failed` : `Moved ${r.moved} files`);
          }}>{O.moving() ? `Moving ${O.moving().done}/${O.moving().total}` : "Move now"}</button>
        </div>
      )}
      <div className="settings-toggle-row">
        <div className="settings-toggle-label">
          <div>This device's name</div>
          <div className="settings-toggle-hint">Shown as "last read on…" so you can tell your devices apart.</div>
        </div>
        <input className="settings-pw-input off-name-input" value={name} maxLength={30}
          onChange={(e) => { setName(e.target.value); try { localStorage.setItem("ms_device", e.target.value); } catch (er) {} }} />
      </div>
      {list.length > 0 && (
        <div className="settings-lib-list off-saved-list">
          {list.map((s) => (
            <div className="settings-lib-row" key={s.id}>
              <div className="settings-lib-main">
                <div className="settings-lib-name"><span className="settings-lib-name-text">{s.title || `Series ${s.id}`}</span></div>
                <div className="settings-lib-path">{s.done}/{s.chapters} chapters · {fmtBytes(s.bytes)}{s.keepN ? ` · keeping next ${s.keepN}` : ""}</div>
              </div>
              <div className="settings-lib-actions">
                <button className="btn ghost sm" onClick={() => O.remove(s.id)}>Remove</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// Round download button for the library/rail cards (sits with fav / status / remove).
function OfflineBubble({ item }) {
  useOfflineTick();
  const O = window.Offline;
  if (!O || !O.supported || !item || item.id == null) return null;
  const dl = O.isDownloading(item.id);
  const state = O.seriesState(item.id);
  const prog = O.progress(item.id);
  const pct = prog.total ? Math.round((prog.done / prog.total) * 100) : 0;
  const title = dl ? `Saving… ${pct}% — click to pause`
    : state === "done" ? "Saved offline — click to remove"
    : state === "partial" ? "Partly saved — click to finish" : "Save whole series offline";
  async function click(e) {
    e.stopPropagation();
    if (dl) return O.pause(item.id);
    if (state === "done") {
      const ok = await window.confirmDialog({ title: "Remove offline copy?", message: `“${item.title}” will be deleted from this device only.`, confirmLabel: "Remove", tone: "danger" });
      if (ok) O.remove(item.id);
      return;
    }
    if ((item.pages || 0) > 2000) {
      const ok = await window.confirmDialog({ title: "Large download", message: `${item.pages} pages — this can use several GB on this device.`, confirmLabel: "Download", tone: "warn" });
      if (!ok) return;
    }
    runDownload(item.id, null);
  }
  return (
    <button className={"card-action-btn download-bubble" + (state === "done" ? " on" : "") + (dl ? " busy" : "")} title={title} aria-label={title} onClick={click}>
      {dl ? <span className="dl-pct">{pct}</span> : state === "done" ? <window.IconCheck size={14} /> : <IconDown size={14} />}
    </button>
  );
}

Object.assign(window, { OfflineBubble, useOfflineTick, ChapterOfflineBtn, OfflineMenu, OfflinePills, Downloaded, OfflineSettings });
