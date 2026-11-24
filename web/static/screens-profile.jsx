/* global window, React */
// Profile — the library's reading record.
//
// Composition follows the LANDING screen, not the library grid: a centred column
// on the same rail measure, headings flanked by hairlines in the display face,
// covers in .rail-grid. The status ledger leads at full width because that is
// what the screen is opened for; the record sentence sits above it and the
// per-library split below. Everything comes from /api/profile in one call.
const { useState: useStateProf, useEffect: useEffectProf, useRef: useRefProf } = React;

// Status colours come from STATUS_COLOR (api.js) — the same map the library
// cards and detail screen use. The --status-* CSS vars are a DIFFERENT palette
// (Reading is vermillion there, green here); taking them made this the one
// screen in the app where Reading was red.
function _profColour(status) {
  return (window.STATUS_COLOR || {})[status] || "var(--faint)";
}

function _profNum(n) {
  return Number(n || 0).toLocaleString();
}

// Estimated time for a pile of pages, as h/m/s. Zero-padded minutes and seconds
// so the column stays aligned down the ledger with tabular figures.
function _profHMS(pages, secsPerPage) {
  const total = Math.max(0, Math.round(Number(pages || 0) * Number(secsPerPage || 0)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  return `${h}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
}

// The landing screen's heading device: display face, letterspaced caps, gradient
// hairlines either side. Using the small left-aligned section head here is what
// made this screen read as an admin table rather than part of this app.
function ProfileHead({ title, kanji }) {
  return (
    <h2 className="landing-h prof-h">
      {title} <span className="kanji">{kanji}</span>
    </h2>
  );
}

// One ledger line. `barValue` lets the bar measure something other than the
// printed count — libraries rank by pages read while still showing series held.
function ProfileRow({ label, colour, value, pages, widest, index, locked, barValue, time }) {
  const bar = barValue == null ? value : barValue;
  const pct = widest > 0 ? Math.min(100, Math.round((bar / widest) * 100)) : 0;
  return (
    <div className={"prof-row" + (value ? "" : " is-empty")}>
      <span className="prof-dot" style={{ background: colour }} />
      <span className="prof-name" title={label}>
        {label}
        {locked ? <span className="prof-lock"><window.IconLock size={11} /></span> : null}
      </span>
      <span className="prof-measure">
        <span style={{ width: `${pct}%`, background: colour, animationDelay: `${index * 45}ms` }} />
      </span>
      <span className="prof-count">{_profNum(value)}</span>
      <span className="prof-pages">{pages ? `${_profNum(pages)} pages` : "—"}</span>
      {time == null ? null : <span className="prof-time">{pages ? time : "—"}</span>}
    </div>
  );
}

// A cover tile built from the app's OWN rail-card markup, so hover, shadow, the
// vermillion ring and the 1.05 image scale all come from existing CSS instead of
// a bespoke lift that matched nothing else in the app.
function ProfileCover({ item, onOpen, sub }) {
  const [ready, setReady] = useStateProf(false);
  const [failed, setFailed] = useStateProf(false);
  const imgRef = useRefProf(null);
  const colour = _profColour(item.status);
  const open = () => onOpen && onOpen(item);

  // A cached image can finish before React attaches onLoad, so the handler never
  // fires and .rail-card stays at opacity:0 — an invisible tile. CoverImg solves
  // this for normalized items; these rows carry only an id, so the same check
  // lives here. naturalWidth === 0 on a complete image means it failed to decode.
  useEffectProf(() => {
    const el = imgRef.current;
    if (!el) return;
    if (el.complete) {
      if (el.naturalWidth > 0) setReady(true);
      else setFailed(true);
    }
  }, []);
  return (
    <div
      className={"rail-card" + (ready || failed ? " rail-card-ready" : "")}
      onClick={open}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}
      role="button"
      tabIndex={0}
      aria-label={item.displayTitle || item.title}
    >
      <div className="rail-card-cover">
        {failed ? (
          <span className="prof-cover-fallback" aria-hidden="true">
            {(item.title || "?").trim().charAt(0)}
          </span>
        ) : (
          <img
            ref={imgRef}
            className="cover-img"
            src={window.coverUrl({ id: item.id, cover_v: item.cover_v })}
            alt=""
            loading="lazy"
            onLoad={() => setReady(true)}
            onError={() => setFailed(true)}
          />
        )}
        <div
          className="rail-card-overlay"
          style={{
            "--strip-color": colour,
            "--strip-bg": "linear-gradient(180deg, rgba(11,11,16,0.78), rgba(11,11,16,0.86))",
          }}
        >
          <div className="rail-card-title">{item.displayTitle || item.title}</div>
          <div className="rail-card-sub">{sub(item)}</div>
        </div>
      </div>
    </div>
  );
}

function ProfileShelf({ title, kanji, items, onOpen, sub }) {
  if (!items || !items.length) return null;
  return (
    <section className="landing-section">
      <ProfileHead title={title} kanji={kanji} />
      <div className="rail-grid">
        {items.map((it) => (
          <ProfileCover key={it.id} item={it} onOpen={onOpen} sub={sub} />
        ))}
      </div>
    </section>
  );
}

function Profile({ onOpenSeries }) {
  const [data, setData] = useStateProf(null);
  const [error, setError] = useStateProf("");

  useEffectProf(() => {
    let alive = true;
    window.ApiClient.profile()
      .then((d) => { if (alive) setData(d); })
      .catch((e) => { if (alive) setError(String((e && e.message) || e)); });
    return () => { alive = false; };
  }, []);

  if (error) {
    return (
      <div className="page prof-wrap">
        <ProfileHead title="Profile" kanji="記録" />
        <p className="prof-lede" style={{ color: "var(--vermillion)" }}>
          Couldn’t load your reading record. {error}
        </p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="page prof-wrap">
        <ProfileHead title="Profile" kanji="記録" />
      </div>
    );
  }

  const t = data.totals || {};
  const statuses = data.statuses || [];
  const libraries = data.libraries || [];
  // One clean rail row each, matching the landing's four-across rhythm.
  const readingNow = (data.reading || []).slice(0, 4);
  const finishedRecently = (data.finished || []).slice(0, 4);

  // Scale the status bars to the biggest status actually engaged with. "Not
  // Started" is everything imported and never opened — on a real library it
  // dwarfs the rest (3,446 against 37), which flattened every meaningful bar to
  // an invisible sliver. It keeps its row and its true count, just not the scale.
  const widestStatus = statuses.reduce(
    (m, s) => (s.status === "Not Started" ? m : Math.max(m, s.series || 0)), 0,
  );
  // Libraries rank by PAGES: a shelf of 3,496 unopened series has not been read
  // more than one of 14 you actually finished.
  const widestLib = libraries.reduce((m, l) => Math.max(m, l.pages || 0), 0);

  const pages = Number(t.pages_read || 0);
  const finishes = Number(t.finishes || 0);
  const completed = Number(t.completed || 0);
  const rereads = Math.max(0, finishes - completed);   // only ever from re-reading
  const secs = (data.time || {}).seconds_per_page || 0;

  return (
    <div className="page prof-wrap">
      <ProfileHead title="Profile" kanji="記録" />

      {pages > 0 ? (
        <p className="prof-lede">
          You’ve read <b>{_profNum(pages)}</b> pages —{" "}
          <b className="prof-hours">{_profHMS(pages, secs)}</b> — and finished{" "}
          <b>{_profNum(completed)}</b> series
          {rereads > 0 ? <>, <b>{_profNum(finishes)}</b> times over</> : null}.
        </p>
      ) : (
        <p className="prof-lede">
          Nothing read yet. Open any of your <b>{_profNum(t.series)}</b> series and
          your record starts here.
        </p>
      )}

      <ProfileHead title="By status" kanji="状態" />
      <div className="prof-ledger">
        {statuses.map((row, i) => (
          <ProfileRow
            key={row.status}
            label={row.status}
            colour={_profColour(row.status)}
            value={row.series}
            pages={row.pages}
            widest={widestStatus}
            index={i}
            time={_profHMS(row.pages, secs)}
          />
        ))}
      </div>

      <ProfileShelf
        title="Reading now"
        kanji="読書中"
        items={readingNow}
        onOpen={onOpenSeries}
        sub={(it) => `${_profNum(it.pages)} pages`}
      />
      <ProfileShelf
        title="Recently finished"
        kanji="読了"
        items={finishedRecently}
        onOpen={onOpenSeries}
        sub={(it) => (it.finishes > 1 ? `read ${_profNum(it.finishes)}×` : `${_profNum(it.pages)} pages`)}
      />

      <ProfileHead title="By library" kanji="書架" />
      <div className="prof-ledger prof-ledger-quiet">
        {libraries.length === 0 ? (
          <div className="prof-row is-empty">
            <span className="prof-dot" style={{ background: "var(--faint)" }} />
            <span className="prof-name">No libraries yet</span>
          </div>
        ) : libraries.map((lib, i) => (
          <ProfileRow
            key={lib.id}
            label={lib.name}
            colour="var(--sakura)"
            value={lib.series}
            pages={lib.pages}
            barValue={lib.pages}
            widest={widestLib}
            index={i}
            locked={lib.private}
          />
        ))}
      </div>

      <p className="prof-note">
        Time is estimated from pages read at ~{secs}s a page, not measured while you read.
        {data.hide_private ? " Private libraries are excluded." : ""}
      </p>
    </div>
  );
}

Object.assign(window, { Profile });
