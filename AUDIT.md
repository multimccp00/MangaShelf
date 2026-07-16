# MangaShelf — Engineering Audit

**Date:** 2026-07-11
**Reviewer:** Senior engineer pass over the full codebase (~10k LOC)
**Scope:** Backend (FastAPI/SQLite/scanner/downloader/sources), frontend (React/esbuild bundle), auth, data-integrity, concurrency, and packaging.

---

## 1. Executive Summary

MangaShelf is a **well-built, thoughtfully-engineered** self-hosted manga library manager. The code quality is notably above average for a personal project: atomic file writes, drive-aware soft-deletes, rollback on partial disk operations, a per-install auth token with optional password gate, and careful cache invalidation are all present and correct. The comments are excellent — they explain *why*, not just *what*, and capture hard-won lessons (Windows case-rename quirks, venv shim behavior, phone keep-alive shutdown hangs).

As a **portfolio piece**, this is strong. The architecture is clean: a pluggable source-adapter system, a shared download pipeline, sidecar metadata that survives rescans, and an AI-assisted fallback adapter that is genuinely novel.

The findings below are **refinements**, not alarms. There is **one confirmed data-integrity bug** (orphaned `parodies` rows), a handful of **medium** robustness gaps, and several **low** polish items. Nothing here is a showstopper.

**Overall grade: B+ / A-.** Fix the handful of medium items and it's solidly an A-.

---

## 2. Severity Legend

| Level | Meaning |
|-------|---------|
| 🔴 High | Data loss, security hole, or crash affecting normal use |
| 🟠 Medium | Correctness/robustness gap under realistic conditions |
| 🟡 Low | Polish, maintainability, or edge-case only |
| 🟢 Note | Observation / commendation, no action required |

---

## 3. Findings

### 🔴 / 🟠 3.1 — `delete_series` leaks `parodies` rows  *(Medium; data hygiene)*

**File:** [database.py:667-672](database.py#L667-L672)

```python
def delete_series(self, series_id: int) -> None:
    self.conn.execute("DELETE FROM series_genres WHERE series_id=?", (series_id,))
    self.conn.execute("DELETE FROM tags WHERE series_id=?", (series_id,))
    self.conn.execute("DELETE FROM series WHERE id=?", (series_id,))
    self.conn.commit()
```

`series_genres` and `tags` are cleaned up, but the `parodies` table is **not**. Every deleted series leaves orphaned parody rows behind. Because `parodies.series_id` is a plain column with no `ON DELETE CASCADE` and IDs are reused via AUTOINCREMENT reset only on table drop, a *new* series that happens to reuse a freed `series_id` would inherit the stale parodies.

**Also affects** `prune_stale_series` → `delete_series` (same path) and the soft-delete/purge flows indirectly.

**Fix:** add `self.conn.execute("DELETE FROM parodies WHERE series_id=?", (series_id,))` to `delete_series`. Consider also declaring the FKs with `ON DELETE CASCADE` (the schema already runs `PRAGMA foreign_keys=ON`), which would make all three child-table cleanups automatic and future-proof.

---

### 🟠 3.2 — SQLite single connection shared across threads under a global lock  *(Medium; scalability ceiling)*

**File:** [web/api.py:63-85](web/api.py#L63-L85)

The whole app funnels every DB call through one `sqlite3` connection guarded by a single `RLock` (`_LockedDB`). This is **correct and safe**, but it fully serializes all DB access — a long `get_series_list()` on a large library blocks *every* other request (covers, pages, progress saves) for its duration. WAL mode helps reads-vs-writes at the SQLite level, but the Python-side lock negates that by serializing in the app.

For a single-user self-hosted app this is **acceptable** and probably invisible. Flagging it as the main scalability ceiling if the library grows to tens of thousands of series or multiple simultaneous readers.

**Options (only if it ever bites):** a small connection pool with per-thread connections, or `threading.local()` connections. Not worth doing pre-emptively.

---

### 🟠 3.3 — Import/move/rescan job state is in-memory only  *(Medium; durability)*

**Files:** [web/api.py:1717](web/api.py#L1717) (`_JOBS`), [web/api.py:1905](web/api.py#L1905) (`_MOVE_JOBS`), [web/api.py:1603](web/api.py#L1603) (`_RESCAN_STATE`)

Queues and job status live in module-level lists. A server restart mid-import **loses all queue state** and the UI's job indicator. The *download itself* is resumable (files-on-disk skip), and a move is transactional (copy→verify→commit→delete, original left intact on failure) — so **no data is lost**, which is the important part. But a queued-but-not-started import silently vanishes on restart, and an in-flight import's progress indicator disappears.

**Fix (optional):** persist the queue to a small JSON file (same atomic-write pattern already used for page counts/settings) and rehydrate on startup. Low priority given the underlying operations are already crash-safe.

---

### 🟠 3.4 — AI adapter: no image-download `Referer`/headers; brittle on hotlink-protected CDNs  *(Medium; feature robustness)*

**File:** [web/sources/ai.py:180-288](web/sources/ai.py)

`GenericAISource` does not override `image_headers()`, so downloads go out with only the default UA. Many manga CDNs 403 without a `Referer` matching the origin site. The dedicated adapters handle this (e.g. nhentai sets `Referer`), but the AI fallback — which targets *arbitrary* sites — is the one most likely to need it and doesn't have it.

**Fix:** cache the series domain in the per-domain rules and return `{"Referer": "https://<domain>/"}` from `image_headers()`. This is already noted in the roadmap's "remaining polish."

**Secondary:** the LLM endpoint discovery (`_discover_llm`) caches `_llm_checked=True` on first probe; if LM Studio starts *after* the server, the adapter stays unavailable until restart. Consider re-probing on a TTL rather than a one-shot flag.

---

### 🟠 3.5 — Path containment check is resolve-based but `browse` endpoint is unsandboxed  *(Medium; intentional but worth documenting)*

**File:** [web/api.py:1561-1598](web/api.py#L1561-L1598)

`/api/browse` lets an authenticated caller enumerate **any** directory on the host (it's the folder picker for adding a library). It's token-gated and read-only (folder names only, never file contents), so the exposure is limited — but it does leak the host's full directory structure to anyone with the token. This is a deliberate design choice for the folder picker, and acceptable for a single-user tool. Worth a one-line comment noting it's intentionally un-sandboxed, since it's the one endpoint that steps outside library roots by design.

The page/cover endpoints (`_is_inside_library`) **are** correctly sandboxed via `resolve()` + `relative_to()`. Good.

---

### 🟡 3.6 — `_ext_from_url` whitelist drops `.gif`  *(Low)*

**File:** [web/sources/downloader.py:55-57](web/sources/downloader.py#L55-L57)

The extension whitelist is `(.jpg, .jpeg, .png, .webp)` and everything else (including `.gif`) falls back to `.jpg`. A GIF saved as `.jpg` still decodes in most viewers via content-sniffing, but the scanner's `IMAGE_EXTENSIONS` *does* include `.gif`, so this is an inconsistency. Minor; add `.gif` to the whitelist.

---

### 🟡 3.7 — Reader `localStorage` mode key is un-namespaced  *(Low)*

**File:** [web/static/screens-reader.jsx:13-15](web/static/screens-reader.jsx#L13-L15)

`reader_mode` is stored under a bare key, while everything else uses the `mangashelf_` prefix (`mangashelf_token`, `mangashelf_settings`). Cosmetic collision risk if the app ever shares an origin. Rename to `mangashelf_reader_mode` for consistency.

---

### 🟡 3.8 — `login` reveals whether a password is set + no rate limiting  *(Low; threat model dependent)*

**File:** [web/api.py:2119-2130](web/api.py#L2119-L2130)

`/api/auth-status` openly reports `password_set`, and `/api/login` has no attempt throttling or lockout. For a tailnet-scoped personal app this is fine (the network is the primary boundary and PBKDF2 with 200k rounds makes offline-style guessing slow). If ever exposed to the public internet, add rate limiting / exponential backoff on failed logins. The comment already claims "a small artificial check keeps timing roughly constant" but there is no such delay in the code — either add it or drop the comment.

---

### 🟡 3.9 — Bare `except Exception: pass` swallows errors in many spots  *(Low; observability)*

**Files:** throughout `web/api.py` (cover save, sidecar mirror, settings write, etc.)

Many best-effort operations silently swallow all exceptions. This is *intentional* (metadata mirroring shouldn't fail a rename) and mostly correct, but it makes field debugging harder. Where already present, the code often `print()`s — good. Consider routing these through `logging` at `DEBUG`/`WARNING` so they're capturable without changing behavior.

---

### 🟡 3.10 — `_page_num` / chapter-number parsing grabs the *first* number  *(Low; AI adapter edge case)*

**File:** [web/sources/ai.py:229](web/sources/ai.py#L229)

Chapter number extraction uses the first `\d+` found in the link text or href. On sites where the URL contains a series-id number before the chapter number, this could mis-order chapters. The heuristic works on the verified sites (mangapill) but is inherently fragile for arbitrary sites — which is the nature of the AI-fallback feature. Acceptable given the feature's "best effort on unknown sites" framing; worth a comment.

---

## 4. What's Done Well (🟢)

These are worth calling out because they're the parts that make this a portfolio-grade codebase:

- **Atomic writes everywhere** — page counts, settings, sidecars, covers all use temp-file + `os.replace()`. Crash-safe. ([api.py:264](web/api.py#L264), [sidecar.py:66](sidecar.py#L66))
- **Drive-aware soft-delete** ([database.py:705-754](database.py#L705-L754)) — `purge_missing_series` explicitly refuses to wipe a library when its root drive is merely offline. This is exactly the bug that destroys lesser library managers, and it's handled correctly with a clear explanation. *(This matches a known project risk — see the standing note on rescan-while-offline data loss.)*
- **Transactional move** ([api.py:1939-1966](web/api.py#L1939-L1966)) — copy → verify count+bytes → commit DB → delete source. A failed move is a true no-op with the original intact.
- **Rollback on rename/split** ([api.py:1304-1333](web/api.py#L1304-L1333), [api.py:1395-1421](web/api.py#L1395-L1421)) — partial disk operations roll back so disk and DB never diverge.
- **Auth design** — per-install token + optional PBKDF2 password gate, with the clever detail that read endpoints accept `?token=` (for `<img>` tags that can't send headers) while mutations require the header (kept out of logs/referrers). ([api.py:680-695](web/api.py#L680-L695))
- **Cover versioning via mtime** ([api.py:344-356](web/api.py#L344-L356)) — a week-long browser cache that stays *correct* because the URL changes the instant a cover changes.
- **Independent job channels** ([app.jsx:7-47](web/static/app.jsx#L7-L47)) — import and move indicators track separately; finishing one never clears the other.
- **No XSS surface** — no `dangerouslySetInnerHTML`/`innerHTML`/`eval` anywhere in the frontend.
- **Portfolio hygiene** — piracy/adult adapters live in a git-ignored `sources/local/`; the AI rule cache is now git-ignored too. Committed code ships only the API-based MangaDex adapter.

---

## 5. Recommended Priority Order

1. **Fix `delete_series` parodies leak** (§3.1) — 1-line fix, real data hygiene issue. Add `ON DELETE CASCADE` while you're there.
2. **AI adapter `Referer` headers** (§3.4) — makes the flagship feature work on more sites.
3. **Re-probe LLM availability on a TTL** (§3.4 secondary) — removes the "start LM Studio before the server" footgun.
4. **Reconcile the login timing comment** (§3.8) — either implement the constant-time delay or remove the comment claiming it exists.
5. Everything else is optional polish.

---

## 6. Testing Gap (🟢 Note)

No automated test suite was found in the tree. For a portfolio piece, a small `pytest` suite around the pure-logic functions — `_chapters_read`, `_chapter_page_starts`, `natural_key`, `split_page_token`, `_safe_name`, `discover_series_dirs` on a temp fixture tree — would be high-value and cheap, and demonstrates testing discipline to reviewers. The disk-mutating operations (move/rename/split) are the highest-risk code and would benefit most from fixture-based tests.

---

*End of engineering audit.*

<br>

---
---

# MangaShelf — Front-End Audit

**Date:** 2026-07-11
**Reviewer:** Senior front-end engineer pass over the entire client (`web/static/`)
**Scope:** React app (app.jsx, components.jsx, screens-library/reader/detail.jsx, api.js, tweaks-shim.jsx), the esbuild bundle pipeline (build.js), index.html shell, and styles.css (~4.2k lines). Focused on component architecture, state management, rendering performance, accessibility, responsive/mobile behavior, and UX robustness.

---

## F1. Executive Summary

The front-end is **genuinely impressive for a hand-rolled, no-framework-tooling app**. It runs on global-scope React (classic runtime, no JSX imports) concatenated by a custom esbuild step — an unusual choice that's well-reasoned and cleanly executed. The UX craftsmanship is the standout: optimistic updates with revert-on-failure *everywhere*, a proper focus-trapping modal, a themed confirm dialog replacing `window.confirm`, keyboard navigation throughout the reader, swipe-to-turn on touch, scroll-position restoration on back-navigation, and cover/thumbnail fallback chains that never show a broken image.

The comments are, again, excellent — nearly every non-obvious effect explains the bug it fixes (missed `onLoad` on cached images, stale vertical-scroll page tracking, the touch-then-synthetic-click double-fire).

**Where it's weakest** is the deliberate architectural shortcuts that come with the no-build-tool approach: a mutable global `window.STORE` that React can't observe (worked around with manual `tick`/`delTick` counters), a few `window.alert()` calls that clash with the otherwise-polished custom dialogs, and some accessibility gaps on custom controls. None are severe; all are the kind of thing a senior reviewer flags for a v2 hardening pass.

**Front-end grade: B+.** The interaction design punches well above the architecture. Tightening state management and closing the a11y gaps would take it to A-.

---

## F2. Findings

### 🟠 F2.1 — Global mutable `STORE` + manual re-render counters is the core architectural smell  *(Medium; maintainability)*

**Files:** [api.js:463](web/static/api.js#L463), [components.jsx:161-164](web/static/components.jsx#L161-L164), [screens-library.jsx:149](web/static/screens-library.jsx#L149), [app.jsx:82](web/static/app.jsx#L82)

`window.STORE.items` is a shared array that components **mutate in place** (`Object.assign(s, patch)`, `STORE.items = STORE.items.filter(...)`). Because React can't see these mutations, the code compensates with a scattering of force-render counters: `tick`, `delTick`, `setDelTick((n)=>n+1)`, and `patchStore` helpers duplicated across `useSeriesActions`, `Detail`, and `Reader`.

This works today but is the most likely source of *future* "the UI didn't update" bugs — every new mutation site has to remember to bump the right counter, and cross-screen consistency depends on all of them mutating the same object references. Example latent risk: a card deleted in the search overlay mutates `STORE.items`, but the underlying Library screen only re-renders if *its* `delTick` fires — the two screens don't share a counter.

**Recommendation (v2):** lift library state into a React context or a tiny external store with a subscribe primitive (`useSyncExternalStore` is built for exactly this and is available in the vendored React 18). Not urgent; it's the single highest-leverage refactor for long-term maintainability.

---

### 🟠 F2.2 — `window.alert()` used for error surfacing throughout  *(Medium; UX consistency)*

**Files:** [components.jsx:176,189,212](web/static/components.jsx), [screens-detail.jsx:312,327,350,438](web/static/screens-detail.jsx), [app.jsx:379,384](web/static/app.jsx), [screens-reader.jsx:437,440](web/static/screens-reader.jsx)

The app ships a beautiful custom `confirmDialog` (explicitly built to replace the ugly native `"<host>:<port> says…"` box) — but failures still fall back to `window.alert()`, which shows exactly that ugly native box the confirm dialog was made to avoid. It's inconsistent, and on the split-chapter success path ([screens-reader.jsx:437](web/static/screens-reader.jsx#L437)) `window.alert` is even used for a *success* message, which is jarring.

**Fix:** add a lightweight toast/notification component (same imperative-mount pattern as `confirmDialog`) and route these through it. Medium priority — it's the most visible polish gap in an otherwise refined UI.

---

### 🟠 F2.3 — `useJobChannel` effect depends only on `[active]`, capturing a stale `onComplete`  *(Medium; correctness)*

**File:** [app.jsx:19-44](web/static/app.jsx#L19-L44)

The polling effect closes over `onComplete` (which is `() => { refreshLibraries(); reload(); }`) but its dependency array is `[active]` only. `reload`/`refreshLibraries` are stable function declarations here so it works in practice, but this is a fragile pattern: if those handlers ever start closing over changing state, the poller will call a stale version. The lint-correct fix is to store `onComplete` in a ref updated each render, or include it in deps with a stable identity (`useCallback`).

Related: the poller uses fixed `setTimeout(tick, 1000)` polling. Fine for a self-hosted app, but there's no backoff and no `visibilitychange` pause — it keeps polling every second even when the tab is backgrounded. Minor battery/CPU cost on mobile.

---

### 🟡 F2.4 — Custom `Dropdown` is not keyboard-accessible  *(Low-Medium; a11y)*

**File:** [components.jsx:7-73](web/static/components.jsx#L7-L73)

The custom `Dropdown` replaces `<select>` for theming (a reasonable call, well-justified in the comment), but it loses the native keyboard model: no `role="listbox"`/`role="option"`, no arrow-key navigation between options, no `aria-expanded`/`aria-activedescendant`, and no type-ahead. A keyboard or screen-reader user can open it (it's a `<button>`) but can't navigate the options with arrows. Since these dropdowns *are* the primary library filter controls, this is the most impactful a11y gap.

**Fix:** add `role="listbox"` + `aria-expanded` on the button, `role="option"` + `aria-selected` on items, and Up/Down/Enter/Escape handling. The `SearchPanel` and reader mode-toggle, notably, correctly use a native `<select>` and native buttons — so the pattern is already in the codebase to follow.

---

### 🟡 F2.5 — Cards use `role="button"` but nest interactive buttons inside  *(Low; a11y/semantics)*

**Files:** [components.jsx:479-536](web/static/components.jsx#L479-L536) (LibraryCard), RailCard, ChapterCard

Each card is a `role="button" tabIndex=0` div that contains *other* buttons (favorite, status, delete). Nested interactive elements inside a `role=button` is invalid ARIA and confuses screen readers about what's clickable. It works visually because the inner buttons `stopPropagation`, but assistive tech sees a button containing buttons. Consider making the card a non-button container with a dedicated "open" affordance, or move the hover-action buttons out of the button's accessible subtree.

---

### 🟡 F2.6 — Image-heavy grids don't cap decode pressure; no `width`/`height` on `<img>`  *(Low; performance/CLS)*

**Files:** [components.jsx:113-122](web/static/components.jsx) (CoverImg), [screens-detail.jsx:35](web/static/screens-detail.jsx#L35) (ChapterThumb), reader `PageImg`

Covers and chapter thumbs render `<img>` without intrinsic `width`/`height` attributes, relying on CSS `aspect-ratio` (which *is* set on the containers — good, so layout shift is largely avoided). But without dimensions the browser can't reserve exact space before CSS applies, and there's no `fetchpriority` hint to deprioritize offscreen covers. The paging (PAGE_SIZE=60, RESULT_CAP=48, chapter row-paging) is the real defense here and is well done. Low priority; add explicit dimensions and `fetchpriority="low"` on grid covers for a marginal win.

The reader's manual preload via `new Image()` ([screens-reader.jsx:282-300](web/static/screens-reader.jsx#L282-L300)) is a nice touch and correctly scoped to single/double mode.

---

### 🟡 F2.7 — `randomRank`/`newReads` shuffle keyed on `.length` re-rolls unexpectedly  *(Low; UX)*

**Files:** [screens-reader.jsx:560-569](web/static/screens-reader.jsx#L560-L569), [screens-library.jsx:50-57](web/static/screens-library.jsx#L50-L57)

Both random orderings are memoized on `items.length`. If a series is added *and* one removed between loads (same length), the memo won't re-roll; conversely a single add/delete reshuffles the entire "New Reads" rail and search random-sort, which can feel like content jumping around. Minor; keying on a stable load-token instead of `.length` would be more predictable.

---

### 🟡 F2.8 — Reader number input allows out-of-range typing to silently no-op  *(Low; UX)*

**File:** [screens-reader.jsx:495-513](web/static/screens-reader.jsx#L495-L513)

The page `<input type="number">` only calls `goToPage` when `v >= 1 && v <= total`. Typing a value above `total` does nothing and doesn't clamp or show feedback — the field just displays the invalid number until blur. Clamping on change (or on Enter, which it partially does) would be clearer. Very minor.

---

### 🟡 F2.9 — External font dependency breaks the "works offline" claim  *(Low; robustness)*

**File:** [index.html:11-13](web/static/index.html#L11-L13)

React is correctly vendored locally "so the app works offline / on the phone without internet" — but the page still loads Inter/JetBrains Mono/Noto Serif JP/Zen Tokyo Zoo from `fonts.googleapis.com`. On a truly offline device (or one that can only reach the Tailnet, not the public internet), fonts fall back to system defaults and the CJK serif glyphs used in fallback cover tiles (本/無/縦/単/双) may not render as designed. For consistency with the offline goal, consider self-hosting the fonts (or at least the JP serif) as woff2.

---

### 🟡 F2.10 — `preview.cover_url` loaded cross-origin directly in the import modal  *(Low; consistency)*

**File:** [app.jsx:955-957](web/static/app.jsx#L955-L957)

The import preview renders the source's cover straight from its origin CDN (`<img src={preview.cover_url} referrerPolicy="no-referrer">`). This is the one spot the browser fetches an image from outside the app's own origin. It's harmless (and `no-referrer` is a thoughtful touch), but it can show a broken image if the CDN hotlink-protects — with no fallback tile like `CoverImg` has. Consider proxying preview covers through the backend (which already downloads the real cover post-import anyway) for a consistent, always-rendering preview.

---

## F3. What's Done Well (🟢)

- **Optimistic-update-with-revert is universal and correct** — favorite, status, metadata, rename, cover all update instantly then roll back and alert on failure. This is the hallmark of a considered UI. ([components.jsx:166-213](web/static/components.jsx), [screens-detail.jsx:303-352](web/static/screens-detail.jsx))
- **The `Modal` component is a proper implementation** — focus trap, focus restore on close, Escape-to-dismiss, `role="dialog"`/`aria-modal`, `aria-labelledby`. Most hand-rolled modals get this wrong; this one doesn't. ([components.jsx:639-692](web/static/components.jsx))
- **Robust image fallback chains** — `CoverImg` handles the cached-image missed-`onLoad` race *and* decode failure; `ChapterThumb` advances through corrupt pages before giving up. Users never see a broken `<img>`. ([components.jsx:76-123](web/static/components.jsx), [screens-detail.jsx:7-36](web/static/screens-detail.jsx))
- **Reader vertical-scroll page tracking** — the rewrite from an IntersectionObserver Set to direct rect measurement (documented at [screens-reader.jsx:201-240](web/static/screens-reader.jsx#L201-L240)) is exactly the right call and the comment explains precisely why the old approach failed.
- **Touch UX** — swipe-to-turn with tap-vs-swipe discrimination, and suppression of the synthetic click that follows a touch. Long-press on the library FAB opens settings. Genuinely thoughtful mobile design. ([screens-reader.jsx:302-335](web/static/screens-reader.jsx), [app.jsx:527-542](web/static/app.jsx))
- **Scroll restoration on back-navigation** — re-applies the saved scrollTop over ~30 frames so late-loading covers don't bump the position. A detail most apps skip. ([app.jsx:214-229](web/static/app.jsx))
- **Responsive + reduced-motion + focus-visible** — the CSS has `prefers-reduced-motion` handling, `:focus-visible` rings, `@media (hover: none)` touch adaptations, and breakpoints down to 380px. Solid accessibility baseline in the stylesheet.
- **Debounced search + result capping** — the search filters on a 180ms-debounced query and caps rendered results at 48 with a "narrow your search" hint, so a multi-thousand-series library stays responsive. ([screens-reader.jsx:544-614](web/static/screens-reader.jsx))
- **Build pipeline** — wrapping each source file in its own IIFE to preserve the original per-`<script>` scoping is a clever, minimal way to ship one minified bundle without rewriting the globals-based source. ([build.js:42-61](web/build.js))
- **No XSS surface** — confirmed again from the client side: zero `dangerouslySetInnerHTML`, all interpolation goes through React's escaping.

---

## F4. Recommended Priority Order (Front-End)

1. **Replace `window.alert()` with a toast** (F2.2) — highest visible-polish return; the confirm dialog already shows the pattern.
2. **Make `Dropdown` keyboard-accessible** (F2.4) — biggest a11y gap, and these are the primary filter controls.
3. **Move `useJobChannel`'s `onComplete` into a ref** (F2.3) — cheap correctness hardening.
4. **Plan the `STORE` → `useSyncExternalStore` refactor** (F2.1) — not urgent, but the top long-term maintainability win; do it before the state-sync counters multiply further.
5. **Self-host fonts** (F2.9) — small change, makes the offline story true.
6. Remaining items are minor polish.

---

## F5. Testing Gap (🟢 Note)

As with the backend, there are no front-end tests. The pure functions are the cheap, high-value targets: `parseChapterName` ([screens-detail.jsx:42](web/static/screens-detail.jsx#L42)), `progressLabel`/`normalize`/`glyphFor`/`hashHue` ([api.js](web/static/api.js)), and the reader's chapter-range/flatten math. A couple of React Testing Library smoke tests around the reader's page-navigation and the optimistic-revert flows would cover the highest-risk interactions. The reader (716 lines, the most stateful component) would benefit most.

---

*End of front-end audit.*

<br>

---
---

# MangaShelf — Back-End Audit

**Date:** 2026-07-11
**Reviewer:** Senior back-end engineer pass over the entire server (`web/api.py`, `database.py`, `scanner.py`, `sidecar.py`, `lists.py`, `config.py`, `pdf_support.py`, `web/server.py`, and `web/sources/*`)
**Scope:** Deep dive on the concerns a backend engineer owns — transaction integrity, the SQLite threading/WAL model, concurrency correctness, resource lifecycle (DB connections, Playwright browsers, thread pools), migration safety, external-I/O failure handling, the download pipeline's atomicity, and the security boundary of file-serving. This goes *below* the general engineering audit's §3 findings; where a point was raised there, this expands the mechanism and consequences.

---

## B1. Executive Summary

The backend is **solid, deliberate, and unusually crash-conscious** for a personal project. The author clearly internalized the failure modes that destroy library managers — offline-drive wipes, half-written metadata, disk/DB divergence on rename — and defended against each with real mechanisms (drive-aware soft-delete, atomic temp-file writes, copy→verify→commit→delete moves, rollback on rename/split). The SQLite concurrency model (`check_same_thread=False` + a serializing lock proxy + WAL) is correctly reasoned and safe.

The findings cluster in three areas: **(1) transaction granularity** — multi-statement DB mutations commit without wrapping in explicit transactions, so a mid-operation failure can leave a partially-applied write; **(2) resource lifecycle** — the shared Playwright browser and the SQLite connection are never closed, and there's no cap on concurrent PDF renders; and **(3) external-I/O trust** — scraped URLs are fetched/rendered with generous timeouts but no total-size limits or SSRF guards. For a single-user self-hosted app on a trusted tailnet, none are critical, but they're the real backend hardening backlog.

**Back-end grade: A- / B+.** The data-safety fundamentals are genuinely strong; the gaps are transaction rigor and resource hygiene, not architecture.

---

## B2. Findings

### 🔴 B2.1 — Multi-statement DB mutations aren't wrapped in explicit transactions  *(High-ish; data integrity)*

**Files:** [database.py:313-371](database.py#L313-L371) (`update_series_metadata`), [database.py:571-648](database.py#L571-L648) (`sync_series_from_sidecar`), [database.py:667-672](database.py#L667-L672) (`delete_series`)

Several methods issue a *sequence* of `DELETE` + `INSERT` statements and then a single `commit()`. Example — `update_series_metadata` does:

```python
UPDATE series SET ...            # (1)
DELETE FROM series_genres ...    # (2)
INSERT ... genres ...            # (3) loop
DELETE FROM tags ...             # (4)
INSERT ... tags ...              # (5) loop
DELETE FROM parodies ...         # (6, conditional)
INSERT ... parodies ...          # (7) loop
self.conn.commit()               # only here
```

Python's `sqlite3` in its default (legacy) mode opens an implicit transaction on the first DML and holds it until `commit()`, so in the *happy path* this is atomic. **But** if any statement in the middle raises (e.g. a genre insert hits an unexpected constraint, or the process is killed between the `DELETE FROM tags` and its re-`INSERT`), there is no `try/except` that issues a `rollback()`. The connection is shared and long-lived, so the next call that reaches `commit()` will **commit the half-applied state** — the series' tags could be permanently wiped with only some re-inserted. This is the classic "delete-then-reinsert without a transaction guard" hazard.

**Fix:** wrap each of these methods in an explicit transaction with rollback on failure:

```python
try:
    with self.conn:      # commits on success, rolls back on exception
        ... statements ...
except Exception:
    raise
```

`with self.conn:` is the idiomatic sqlite3 transaction context and is the smallest correct change. This matters most for `update_series_metadata` and `sync_series_from_sidecar`, which both delete-then-reinsert child rows.

---

### 🟠 B2.2 — The shared Playwright browser is never closed (process/handle leak)  *(Medium; resource lifecycle)*

**Files:** [web/sources/ai.py:88-98](web/sources/ai.py#L88-L98), [web/sources/local/mangahome.py:49-59](web/sources/local/mangahome.py#L49-L59)

Both the AI adapter and MangaHome launch a module-level Chromium via `sync_playwright().start()` + `chromium.launch()` and cache it in a dict, but there is **no shutdown path** — no `atexit` handler, no `.close()`, nothing on server stop. Each launched browser is a real child process holding a few hundred MB. On a normal run only one is created and it dies with the parent, so impact is bounded — but:

- If the parent is force-killed (the `os._exit(0)` hard-shutdown path in [server.py:117](web/server.py#L117)), the Chromium child can be **orphaned** and linger.
- Per-context/per-page objects ARE closed correctly (`finally: page.close(); ctx.close()`), which is the important part for leaks *during* a run — good. It's only the top-level browser that dangles.

**Fix:** register an `atexit`/signal cleanup that calls `browser.close()` + `pw.stop()` if started, and null the cache. Low effort, removes the orphan-process risk.

---

### 🟠 B2.3 — No bound on concurrent PDF renders or thumbnail generation (memory spike)  *(Medium; resource exhaustion)*

**Files:** [web/api.py:437-506](web/api.py#L437-L506) (PDF render/cache), [web/api.py:406-434](web/api.py#L406-L434) (thumbnails)

PDF page rendering (PyMuPDF at 150 DPI) and Pillow thumbnailing run **inline on the FastAPI threadpool with no semaphore**. A reader flipping fast through a PDF chapter, or several devices hitting the grid at once, can trigger many simultaneous full-page renders — each decoding a multi-MB page into memory. There's no concurrency cap and no per-render memory ceiling. The disk cache mitigates *repeat* loads (keyed by path+mtime+size+page), but the *first* load of N distinct pages fans out unbounded.

The startup thumbnail prewarm *is* correctly bounded (`ThreadPoolExecutor(max_workers=6)` at [api.py:573](web/api.py#L573)) — but the on-demand request path isn't.

**Fix:** gate expensive renders behind a `threading.Semaphore(N)` (e.g. 4) so bursts queue instead of all decoding at once. Prevents a memory spike from a fast reader or multiple clients.

---

### 🟠 B2.4 — Scraper/downloader external I/O has no total-size cap and reads whole responses into memory  *(Medium; robustness / DoS-by-upstream)*

**Files:** [web/sources/downloader.py:60-74](web/sources/downloader.py#L60-L74) (`_download`), [web/api.py:358-374](web/api.py#L358-L374) (`_save_source_cover`), [web/sources/ai.py:65-85](web/sources/ai.py#L65-L85) (LLM call)

Every remote fetch does `r.read()` with **no maximum size** — a page image, a cover, or an LLM response is read fully into memory. A misbehaving or malicious upstream (or an accidental link to a huge file) could stream gigabytes and OOM the process. The upload path *does* cap size (25 MB at [api.py:853](web/api.py#L853)) — good — but the download/scrape paths don't. There's also no `Content-Length` pre-check.

**Fix:** read with a cap (`r.read(MAX_BYTES + 1)` and reject if it overflows) on the download, cover, and LLM paths. Even a generous 100 MB ceiling per page turns an unbounded risk into a bounded one.

---

### 🟠 B2.5 — SSRF: the AI/import path fetches arbitrary user-supplied URLs server-side  *(Medium; security — threat-model dependent)*

**Files:** [web/api.py:1678-1708](web/api.py#L1678-L1708) (`scrape_preview`), [web/sources/ai.py:185-188](web/sources/ai.py#L185-L188) (`matches` = any `http` URL)

The AI adapter's `matches()` returns true for **any** `http(s)` URL, so an authenticated user can make the server fetch/headless-render an arbitrary address — including internal ones (`http://169.254.169.254/…` cloud metadata, `http://localhost:…` other services on the host, RFC-1918 addresses on the LAN). This is a textbook SSRF surface. It's gated behind the auth token and this is a single-user app, so the practical risk is low — the attacker would be the user themselves — but if the app is ever shared or exposed, this is the sharpest edge.

**Fix (if the threat model ever widens):** resolve the target host and reject private/loopback/link-local ranges before fetching or rendering. At minimum, document that the AI adapter will fetch whatever URL it's given.

---

### 🟠 B2.6 — The SQLite connection is never closed; `_LockedDB` proxy silently un-serializes attribute access  *(Medium; correctness nuance)*

**Files:** [web/api.py:46-85](web/api.py#L46-L85)

Two related points:

1. **No close on shutdown.** The web `Database` connection (opened in `_make_web_db`) is never `close()`d. With WAL this is usually fine (WAL checkpoints on clean close, but SQLite recovers on next open), yet the hard `os._exit(0)` shutdown path skips any checkpoint — leaving a `-wal` file to be recovered next boot. Harmless in practice but worth a graceful `conn.close()` on the normal shutdown path.

2. **The lock proxy only serializes *callables*.** `_LockedDB.__getattr__` returns non-callable attributes *unlocked* (`if not callable(attr): return attr`). Today every DB access goes through methods, so this is safe — but it's a latent trap: if any code ever reads `_db.conn` or another attribute and uses it directly, it bypasses the lock entirely. Consider documenting that only method calls are serialized, or blocking raw-attribute access.

Also note (raised in the general audit, restated for the mechanism): because *all* DB access serializes through one `RLock`, WAL's read/write concurrency is negated at the Python layer — a long `get_series_list()` blocks every cover/page/progress request. Acceptable for single-user; it's the throughput ceiling.

---

### 🟡 B2.7 — `scan_and_sync` re-imports the sidecar module inside a hot loop; per-series work isn't batched  *(Low; performance)*

**File:** [scanner.py:270-290](scanner.py#L270-L290)

Inside the per-series loop, `from sidecar import read_sidecar` runs on every iteration (import caching makes this cheap but it's noise), and each series triggers its own `upsert_series` + `sync_series_from_sidecar`, each committing separately. For a large library rescan that's thousands of individual transactions. Batching the writes (one transaction per library, or per N series) would cut rescan time materially on big libraries. Hoist the import out of the loop too.

---

### 🟡 B2.8 — `purge_missing_series` and `prune_stale_series` do full-table scans with per-row commits/deletes  *(Low; performance + the known data-loss guard)*

**Files:** [database.py:674-703](database.py#L674-L703) (`prune_stale_series`), [database.py:705-754](database.py#L705-L754) (`purge_missing_series`)

Both walk every series row and `Path(...).exists()` each folder — a `stat()` per series, which on a network drive is slow and serialized under the DB lock (so it blocks all other requests for the duration of a rescan). Correctness is good (the drive-aware guard is the right design and is the single most important safety property in the app — *do not weaken it*), but the I/O could move off the lock: collect the paths first, stat them *outside* the lock, then apply the availability updates in one transaction. This also shrinks the window where a rescan starves reads.

*(Cross-ref: this is the mechanism behind the standing project risk that a rescan while a drive is offline could wipe that drive's library — the guard here is what prevents it. Any change to these two methods must preserve the "library root reachable but folder missing" precondition.)*

---

### 🟡 B2.9 — Migration path has no version marker; relies on `PRAGMA table_info` diffing every boot  *(Low; maintainability)*

**File:** [database.py:119-194](database.py#L119-L194)

Schema migration is done by introspecting columns and conditionally `ALTER TABLE`-ing. It's idempotent and works, but there's no `user_version`/schema-version pragma, so every startup re-runs the full introspection and the logic grows a new `if "col" not in columns` branch per change. For a portfolio piece this is fine; at scale a `PRAGMA user_version` gate with numbered migration steps is cleaner and lets you reason about upgrade order. The legacy `nhentai_id → external_id` rename is handled correctly (with value preservation) — good.

---

### 🟡 B2.10 — Background threads swallow all exceptions; failures are invisible  *(Low; observability)*

**Files:** [web/api.py:308-320](web/api.py#L308-L320) (`_warmup_worker`), [web/api.py:1800-1824](web/api.py#L1800-L1824) (`_job_worker`), [web/api.py:1969-1990](web/api.py#L1969-L1990) (`_move_worker`)

The warmup worker's `except Exception: pass` means a systematically-failing folder scan silently never populates its page count and the user just sees "Pg 0/0" forever with no signal why. The job/move workers do capture errors into job state (good), but the warmup path is a black hole. Route these through `logging` so a persistent failure is at least discoverable in the server log. (This is the backend half of the general audit's §3.9.)

---

### 🟡 B2.11 — `_download` retry sleeps synchronously inside the threadpool; no jitter/backoff cap  *(Low)*

**File:** [web/sources/downloader.py:60-74](web/sources/downloader.py#L60-L74)

Retries sleep `1.0 * (attempt+1)` seconds (1s, 2s, 3s) with no jitter. With `_CONCURRENCY=4` workers all hitting the same host, synchronized retries can thundering-herd a struggling CDN. Minor; add small random jitter. The atomic `.part` → `replace` write is correct and good.

---

## B3. What's Done Well (🟢)

- **Atomic file writes are consistent and correct** — page counts, chapter meta, settings, password, sidecars, covers, and downloaded pages all use temp-file + `os.replace()`. This is the single most important durability pattern and it's applied everywhere. ([api.py:264-274](web/api.py#L264-L274), [sidecar.py:66-75](sidecar.py#L66-L75), [downloader.py:67-69](web/sources/downloader.py#L67-L69))
- **The move is a real transactional saga** — manifest → copy → verify (file count AND byte total) → commit DB → delete source, with partial-copy cleanup if the commit hasn't happened. A failed move is a true no-op. ([api.py:1939-1966](web/api.py#L1939-L1966))
- **Rename/split roll back disk operations** — every moved file is tracked and reversed on failure, and the new directory is removed, so disk and DB never diverge. ([api.py:1304-1333](web/api.py#L1304-L1333))
- **Drive-aware soft-delete** — the crown jewel: `purge_missing_series` refuses to flag anything when the library root is unreachable, and soft-deletes (available=0) rather than dropping rows. This correctly prevents the catastrophic "offline drive → wiped library" bug. ([database.py:705-754](database.py#L705-L754))
- **SQLite concurrency is correctly reasoned** — `check_same_thread=False` paired with a serializing lock proxy is the right way to share one connection across FastAPI's threadpool, and the WAL/NORMAL/cache pragmas are sensible defaults. ([api.py:46-85](web/api.py#L46-L85))
- **Path containment is enforced with `resolve()` + `relative_to()`** on every page/cover serve, correctly sandboxing arbitrary `?path=` to library roots (PDF page tokens are stripped before the check). ([api.py:101-114](web/api.py#L101-L114), [api.py:971-994](web/api.py#L971-L994))
- **The download pipeline is polite and resumable** — bounded concurrency, inter-chapter delay, per-page skip-if-exists, retry with atomic writes, and failure counts surfaced to the UI rather than swallowed. ([downloader.py:77-149](web/sources/downloader.py#L77-L149))
- **Auth is well-constructed** — per-install token via `secrets.token_urlsafe`, constant-time compare (`compare_digest`), PBKDF2-SHA256 at 200k rounds for the password, atomic password-file writes, and the header-for-writes / query-param-for-reads split is a genuinely clever solution to the `<img>`-can't-send-headers problem. ([api.py:589-695](web/api.py#L589-L695))
- **Cache invalidation is disciplined** — every disk-mutating operation (rename/split/move/rescan/import) clears the right caches (chapters, cover paths, page counts), and the invalidation is centralized. ([api.py:138-145](web/api.py#L138-L145) and its call sites)
- **Adapter abstraction is clean** — the three-method `MangaSource` interface + shared downloader means a new source is ~100 lines and never touches ingest/DB logic. The registry's git-ignored `local/` loading keeps piracy/adult adapters out of the repo while staying pluggable. ([base.py](web/sources/base.py), [registry.py](web/sources/registry.py))

---

## B4. Recommended Priority Order (Back-End)

1. **Wrap delete-then-reinsert methods in `with self.conn:` transactions** (B2.1) — the one finding with real corruption potential. Small, high-value.
2. **Cap remote read sizes** in the downloader/cover/LLM paths (B2.4) — turns an unbounded-memory risk into a bounded one.
3. **Bound concurrent PDF/thumbnail renders with a semaphore** (B2.3) — prevents a memory spike from a fast reader or multiple clients.
4. **Add Playwright browser cleanup on shutdown** (B2.2) — removes the orphan-process risk.
5. **Add SSRF range-blocking** *if* the app will ever be shared beyond the single owner (B2.5).
6. **Move rescan `stat()` I/O off the DB lock + batch writes** (B2.7, B2.8) — throughput on large/network libraries.
7. Remaining items are observability and polish.

---

## B5. Cross-Cutting Note: Transaction + Resource Discipline

The backend's *disk*-side integrity is excellent (atomic writes, sagas, rollbacks). The gap is that its *database*-side integrity relies on sqlite3's implicit-transaction happy path rather than explicit transaction boundaries, and its *resource* lifecycle (browser, DB connection, render concurrency) is mostly "created lazily, never torn down." Both are survivable for a single-user tool, and neither undermines the strong data-safety story — but closing B2.1 (explicit transactions) and B2.2–B2.4 (resource bounds) is what would take this from "carefully written" to "production-hardened."

---

*End of back-end audit.*

<br>

---
---

# MangaShelf — UI/UX Design Audit

**Date:** 2026-07-11
**Reviewer:** Senior UI/UX designer pass over the live app (desktop 1440×900 + mobile 390×844) plus the design system in `styles.css`
**Method:** Reviewed the *rendered* product — captured and inspected the landing, library grid, series detail, search overlay, and mobile equivalents — then cross-referenced against the design tokens, spacing scale, type system, and responsive rules. This is a **visual craft + interaction design + user-flow** audit, distinct from the front-end *engineering* audit above; where a point overlaps, this one is about the user's experience, not the code.

---

## D1. Executive Summary

Visually, this is a **genuinely handsome app** with a coherent, confident aesthetic — the "Cyber-Shrine" dark theme (deep ink canvas, vermillion accent, subtle sakura/gold/jade status hues, procedural paper grain, JP display type) is well-conceived and consistently applied. The design *system* underneath is real: a proper spacing scale, radii scale, elevation tokens, and a status-color palette that maps meaning to hue. The card treatment (2:3 cover, gradient title overlay, hover-reveal action bubbles, status tag, NEW badge) is polished and reads instantly. The search overlay and mobile landing are the standout screens — beautifully composed, correctly prioritized.

**But the product has two categories of problem the screenshots make obvious:**

1. **Layout containment on desktop** — the main content areas have **no max-width**, so on a wide screen a handful of series sit jammed against the far-left edge with ~60% of the viewport as dead black space, and the filter bar **overflows off the right edge** (the "Language" dropdown is visually clipped). This is the single most damaging issue: it makes a beautiful app look broken/unfinished on the exact screens (desktop) where it has the most room to shine.

2. **Empty-state & loading polish** — the detail screen flashes "**0 chapters / 0% COMPLETE / 0 chapters**" and a disabled "Loading…" button while data loads, which reads as "this manga is empty" for a beat before the real numbers appear. First impressions of a series page shouldn't be a zeroed-out skeleton with real-looking (wrong) numbers.

Neither undermines the strong aesthetic foundation — they're finish-work. Close them and this jumps from "clearly talented but rough in spots" to "portfolio centerpiece."

**Design grade: B.** The visual language is A-level; layout containment and loading/empty states are what hold it back.

---

## D2. Findings

### 🔴 D2.1 — No max-width on content: wide screens leave the library jammed left with huge dead space  *(High; layout / visual balance)*

**Observed:** [library screenshot] — on 1440px wide, 4 series cards occupy the left ~40% and the right ~60% is empty black. The grid (`grid-template-columns: repeat(var(--grid-cols,6), minmax(0,1fr))`, [styles.css:754](web/static/styles.css#L754)) fills its container, but the container (`.content`/`.page`) has no `max-width` and the cards are `minmax(0,1fr)` — so with few items they stay small and hard-left instead of centering or growing.

**Why it matters:** it's the first thing a desktop user sees, and it reads as a broken/unfinished layout. A gorgeous card design is undercut by the composition around it.

**Fix options (pick one):**
- Give `.content`/`.page` a `max-width` (e.g. 1600px) and `margin-inline: auto` so the whole workspace centers on ultra-wide screens; **and/or**
- Let cards grow to a comfortable max (e.g. `minmax(180px, 220px)` with `justify-content: center` on the grid) so a sparse library centers rather than clinging to the edge.

The mobile layout does *not* have this problem (it correctly fills the narrow viewport) — this is desktop-specific.

---

### 🔴 D2.2 — Desktop filter bar overflows the viewport; the last control is clipped  *(High; usability)*

**Observed:** [library screenshot] — the filter row (Sort · Status · Series · Genre · Author · Tag · Parody · Language) runs off the right edge; "All Languages" is cut off by the viewport with no visible affordance to reach it. On desktop the bar is `flex-wrap: wrap` ([styles.css:1001](web/static/styles.css)) so it *should* wrap — but with 8 fixed-width dropdowns + labels it neither wraps cleanly nor scrolls on desktop, so a control is simply unreachable at this width.

**Why it matters:** a primary filter is inaccessible without zooming out. Filters are the core "find something in a big library" tool.

**Fix:** either (a) apply the same horizontal-scroll treatment used on mobile ([styles.css:3660](web/static/styles.css#L3660)) to desktop when the bar exceeds width, (b) collapse secondary filters (Parody/Language/Author) into a single "More filters" popover, or (c) ensure the wrap actually engages by not letting the row exceed 100%. Option (b) is the most designerly — 8 always-visible dropdowns is a lot of persistent chrome; most are rarely used.

---

### 🟠 D2.3 — Detail screen shows a zeroed-out "empty" state while chapters load  *(Medium; loading UX / first impression)*

**Observed:** [detail screenshot] — while chapters load, the page shows "11875 pages · **0 chapters**", "**0% COMPLETE** / **0 chapters**", and a disabled "Loading…" button — but the progress *also* shows "8 / 11875 pages". So the header confidently states real-looking but wrong facts (0 chapters, 0%) for a beat. It reads as a broken/empty series.

**Why it matters:** the series page is the app's second-most-important screen; leading with wrong zeros is a poor first impression and can make a user think an import failed.

**Fix:** show a skeleton/placeholder for chapter-derived numbers *until they resolve* (e.g. "— chapters", a shimmer on the chapter grid) rather than a concrete `0`. Don't render `0 chapters` / `0% complete` when `loading === true`; render a neutral loading affordance instead. The chapter grid already has a "Loading chapters…" empty — extend that treatment to the header stats.

---

### 🟠 D2.4 — Information hierarchy on detail: the metadata table dominates; chapters (the point) are below the fold  *(Medium; IA)*

**Observed:** [detail screenshot] — the top of the detail screen is a large cover + a metadata table (Parodies / Tags / Genres / Artists / Languages / Pages), most rows showing just an empty "+ Add" affordance. The **chapters grid** — the reason you open a series — is pushed below all of that, off-screen at this fold. For a series with mostly-empty metadata, the screen is dominated by empty editors.

**Why it matters:** the primary task (pick a chapter / continue reading) is de-prioritized behind secondary editing affordances that are mostly empty.

**Fix:** consider (a) collapsing empty metadata rows into a single "+ Add details" affordance until populated, or (b) moving the chapter grid up / making the cover pane and chapter list a two-column layout so chapters are visible without scrolling. The "Continue / Start Reading" CTA is well-placed and prominent — good — but the chapters themselves should be reachable at the same fold.

---

### 🟠 D2.5 — Empty "+ Add" editors everywhere make a populated-looking page feel unfinished  *(Medium; visual density)*

**Observed:** [detail screenshot] — Parodies "+ parody", Tags "+ Add", Genres "+ Add", Artists "+ artist", Languages "+ language" all render as empty ghost buttons. Five rows of "add me" with no values looks like a form waiting to be filled, not a library entry. For imported series that *have* metadata this is fine; for scanned local folders with none, it's five empty prompts.

**Fix:** when a field is empty, render it muted/inline (e.g. a single greyed "Add tags, genres…" line) and only expand to the full labelled-row treatment once there's a value or the user chooses to edit. Reduces visual noise and makes populated series look richer by contrast.

---

### 🟡 D2.6 — Landing "Continue Reading" shows only 2 cards at huge size; rail feels under-filled  *(Low-Medium; composition)*

**Observed:** [landing screenshot] — two very large cards fill the Continue Reading rail with lots of gap to the right; New Reads below is cut off at the fold. The cards are gorgeous but oversized for a "rail" — a rail usually implies a scannable row of several. Two big cards + empty right side echoes the containment issue (D2.1).

**Fix:** either show more cards per rail (4–6 smaller) so the rail reads as a rail, or constrain the rail width and center it. The mobile version (2×2 grid) actually composes *better* than desktop here.

---

### 🟡 D2.7 — Two different search entry points with different affordances  *(Low; consistency)*

**Observed:** the landing has a centered pill search with a "Search" button; the topbar (other screens) has a read-only search box that opens the full overlay; the overlay itself has a third search input. Three search UIs with slightly different behavior (landing submits/navigates, topbar opens overlay, overlay filters live). It works, but the mental model isn't uniform — a user learns the landing search behaves differently from the "same-looking" box elsewhere.

**Fix:** unify on the overlay as the single search surface (the landing pill could open the same overlay pre-focused), so search always feels like the same tool.

---

### 🟡 D2.8 — Wordmark/type: the "MANGA SHELF" display face is decorative but low-legibility at the brand size  *(Low; type)*

**Observed:** the Zen Tokyo Zoo display font on "MANGA SHELF" ([landing]) and the outlined detail H1 ("FAIRY-TAIL") are stylish but thin/outlined enough to reduce legibility, especially the detail H1 which renders as a hairline outline face. It's a strong aesthetic choice; just verify it holds up for long titles and at mobile sizes (a 30-character title in an outlined display face can get hard to parse).

**Fix (optional):** keep the display face for the brand, but consider a slightly heavier/solid weight for the series H1 so long titles stay readable. Low priority — it's a taste call, and the current look is on-brand.

---

### 🟡 D2.9 — Status vocabulary shown as ALL-CAPS badges is slightly shouty; color-only meaning has a contrast risk  *(Low; a11y/legibility)*

**Observed:** [library] status tags ("READING", "NOT STARTED") are uppercase mono on translucent dark chips with a colored dot. Readable, but several sit *over* busy cover art (e.g. the "NOT STARTED" over High-School DxD) where the translucent background doesn't fully separate text from the image. Status is also conveyed largely by hue (green/grey/gold) — fine, but the dot is small.

**Fix:** ensure the chip background is opaque enough (or add a subtle text-shadow) so status text always clears busy covers; the dot + text combo already avoids color-only reliance, which is good — just tighten the contrast over imagery.

---

### 🟢 D2.10 — Reader was not re-captured here, but prior review confirms it's the strongest interaction surface

The reader (single/double/vertical modes, swipe-to-turn, tap-to-toggle-chrome, 44px touch targets, page-flip animation) is the most considered interaction in the app per the front-end audit. No new design issues to add beyond what's noted there; the mode-toggle labels (縦/単/双) are a lovely on-brand touch, though a first-time user may not know what they mean — a tooltip (present) covers it.

---

## D3. What's Done Well (🟢)

- **Coherent, confident visual identity** — the Cyber-Shrine palette (ink scale + vermillion + sakura/gold/jade + grain) is genuinely well-designed and applied consistently across every surface. The app has a *point of view*, which most hobby projects lack.
- **A real design system** — tokenized spacing (`--s-1..9`), radii (`--r-sm..xl`), elevation shadows, and a semantic status-color map. This is how you keep 4,200 lines of CSS coherent. ([styles.css:3-64](web/static/styles.css#L3-L64))
- **The card is excellent** — 2:3 cover, gradient-to-dark title overlay for legibility over any art, hover-reveal action bubbles, status tag with dot, NEW/unread badge, hover zoom + 3D perspective. It reads instantly and looks premium. ([styles.css:760-945](web/static/styles.css))
- **The search overlay is the best screen** — sticky search input, faceted Tags/Status/Genre chips, sort control, live result count, capped results. Composed, prioritized, and fast-feeling. [search screenshot]
- **Mobile is a genuine adaptation, not an afterthought** — the landing (2×2 rails), library (3-col reflow), horizontally-scrolling filter bar, 44px reader targets, and hover→always-visible action bubbles on touch devices are all deliberate and correct. The `@media (hover: none)` handling is textbook. ([styles.css:3609-3785](web/static/styles.css))
- **Accessibility baseline in the CSS** — `:focus-visible` rings, `prefers-reduced-motion` disabling animation, safe-area insets for notches. Above average for a personal project. ([styles.css:66-82](web/static/styles.css))
- **Thoughtful micro-details** — procedural SVG grain (no asset), status-colored progress rings/bars, the vermillion selection color, cover fallback tiles with JP glyphs so a missing cover still looks intentional. These are the touches that signal craft.
- **Empty states have personality** — the 無 ("nothing") glyph on empty results/readers is on-brand and warmer than a generic "no results."

---

## D4. Recommended Priority Order (Design)

1. **Add max-width + centering to the content area** (D2.1) — the highest-impact fix; it's what makes the desktop app look finished. One CSS change.
2. **Fix the filter-bar overflow** (D2.2) — a primary control is currently unreachable on desktop. Consider a "More filters" popover to also reduce chrome.
3. **Replace the zeroed detail loading state with a skeleton** (D2.3) — stop showing "0 chapters / 0%" before data loads.
4. **Rebalance the detail hierarchy** (D2.4) + **collapse empty metadata editors** (D2.5) — get chapters above the fold and quiet the empty "+ Add" rows.
5. **Fill out the Continue Reading rail** (D2.6) and **unify search entry points** (D2.7).
6. Type, status-chip contrast, and reader label discoverability are polish.

---

## D5. One-Paragraph Verdict

The *design language* is portfolio-grade — a distinctive, consistent, well-systematized aesthetic that most self-taught projects never reach. What separates it from truly finished work is **layout composition on desktop** (unbounded width, overflowing filters) and **loading/empty-state polish** (zeroed detail stats, empty editor rows). These are finish-work, not foundational flaws: a day of layout containment and skeleton states would lift the whole product a full grade. Fix D2.1–D2.3 first; they're cheap and they're what a visitor notices in the first ten seconds.

---

*End of UI/UX design audit.*

<br>

---
---

# MangaShelf — Audit V (new features: Extensions, Web Search, Preview-Reader)

**Date:** 2026-07-15
**Reviewer:** Senior engineer pass over everything built AFTER the first four audits — the declarative Source-extension system, web title-search, the read-before-import preview/streaming reader, dev auto-reload, and the session's data/UX fixes.
**Scope:** New backend (`sources/declarative.py`, `sources/mangadex.py` search, the `/api/preview/*` + `/api/search-web` endpoints, `server.py` reload), and the corresponding frontend (`PreviewModal`, reader remote-page mode, web-search UI). Prior-audit findings are not repeated; this covers new surface only.

---

## V1. Executive Summary

The features added since the last audits are **well-architected and consistent with the codebase's strong patterns** — the extension engine reuses the AI adapter's audited SSRF/size-cap/render primitives rather than duplicating them, the search capability is a clean optional method on the source interface (so it fans out automatically), and the preview-reader correctly proxies remote pages so the existing reader "just works" with streamed content. The safety-first framing of the extension system ("a manifest is data, never code") is genuinely the right call for a multi-user app and is upheld in the implementation.

The findings are concentrated in **three areas**: (1) the **web-search + preview endpoints do blocking network I/O on the request thread with no timeout ceiling**, which — combined with the global DB lock from the original audit — is the main new performance/availability risk; (2) a couple of **correctness edges in the declarative search/JSON paths** (unbounded variant fan-out, JSONPath escaping); and (3) **the preview page-proxy is an open image relay** that, while token-gated and SSRF-guarded, deserves tightening. Nothing here is a data-loss or RCE bug — the dangerous class (code execution via extensions) was correctly designed out.

**Grade for the new work: A- / B+.** Architecture is A; the gaps are I/O concurrency discipline and a few validation edges.

---

## V2. Findings

### 🟠 V2.1 — Search + preview endpoints block the request thread on slow upstreams with no total-timeout  *(Medium; availability)*

**Files:** [api.py:1829](web/api.py#L1829) (`preview_series`), [api.py:1865](web/api.py#L1865) (`preview_pages`), [registry.py:search_all], [mangadex.py:search]

`search_all()` calls each searchable source's `search()` **serially**, and MangaDex's search now issues **up to 3 sequential HTTP calls** (the spelling variants) each with a 30s socket timeout — so a slow MangaDex can tie up a worker for ~90s. `preview_series` calls `fetch_series` (which for MangaDex pages the *entire* chapter feed — 404 calls for Berserk, each rate-limited at 0.25s ≈ 100s). These run on FastAPI's threadpool, and per the original audit **every DB access serializes through one lock** — a few concurrent slow previews can starve the whole app.

**Mechanism:** each `urlopen` has a *socket* timeout, but there's no *total* operation timeout, and the variant/feed loops multiply it. A handful of clients each opening a big-series preview could exhaust the threadpool.

**Fix:** (a) cap the total work — e.g. run `search_all`'s sources concurrently with a `ThreadPoolExecutor` + an overall deadline; (b) for `preview_series`, the preview doesn't need the full chapter feed's page counts — consider a lighter "chapters list only" path, or a hard cap on feed pages fetched. At minimum, document that preview of a huge series is slow and bound it.

---

### 🟠 V2.2 — Declarative search fans out ALL variants with no per-variant cap, then de-dupes  *(Medium; efficiency — mirrors a bug already hit)*

**File:** [mangadex.py](web/sources/mangadex.py) `search` (variant gather) and [declarative.py:227](web/sources/declarative.py#L227)

MangaDex `search()` gathers results from every spelling variant (up to 3 full API calls) and ranks. That's correct and was the fix for the "high school of the dead" relevance bug — but it's **3× the API calls per search**, serial, with the rate-limit pause between each. On a busy instance that's a lot of upstream traffic. The declarative `search()` is single-fetch (good). Consider: stop early if the first variant already yields a strong exact match (score 1.0), avoiding the extra 2 calls in the common case.

---

### 🟠 V2.3 — Preview page-proxy is an open, authenticated image relay  *(Medium; abuse surface — threat-model dependent)*

**File:** [api.py:1887](web/api.py#L1887) (`preview_page`)

`/api/preview/page?url=…` fetches **any** URL the caller supplies and streams it back. It's **token-gated** (good) and **SSRF-guarded** via `_is_public_url` (good) and **size-capped** (good). But: (a) it's a `require_token_read` route, so the token can travel in the query string (needed for `<img>`), meaning a leaked page URL leaks the token — consistent with the rest of the app but worth noting the relay makes it a general-purpose proxy; (b) there's **no allowlist of hosts** — a holder of the token can proxy arbitrary public images through the server (bandwidth/abuse). For a single-user tool this is fine; if shared, restrict the proxy to hosts of *loaded sources*, or sign the URL when `preview/pages` issues it so only app-generated URLs are proxied.

**Also:** the SSRF-guard `try/except` at [api.py:1895-1903](web/api.py#L1895-L1903) is convoluted — if the `_is_public_url` import fails, it falls through to only checking `url.startswith("http")`, silently dropping the SSRF guard. Make the guard failure fail-closed (reject) rather than degrade.

---

### 🟠 V2.4 — Preview reading has no rate-limit / politeness toward the source  *(Medium; gets you blocked)*

**Files:** [api.py:1865](web/api.py#L1865) (`preview_pages`), the reader's eager preload

The download pipeline is deliberately polite (inter-chapter delay, bounded concurrency). The **preview path isn't** — opening a chapter fetches its page list, and the reader then loads pages (and preloads neighbors) as fast as the browser will, all proxied through the server hitting the source CDN. Reading several chapters quickly could trip a source's rate limiting / get the server IP throttled. Consider a small per-source throttle on the proxy, or reuse the downloader's politeness for preview fetches.

---

### 🟡 V2.5 — `_jpath` / `_interp` JSONPath subset: `{...}` interpolation can misfire on braces in data  *(Low; JSON manifests)*

**File:** [declarative.py:187-195](web/sources/declarative.py#L187-L195)

`_interp` uses `re.sub(r"\{([^}]+)\}", …)` to fill templates. If a resolved JSON value itself contains `{` or `}` (rare but possible in a filename/title), or a template legitimately needs a literal brace, there's no escaping. Low likelihood for image templates, but a malformed manifest could produce surprising URLs. Consider a stricter placeholder syntax or escaping. Also `_jpath` silently returns `""` for missing keys — good for robustness, but a typo'd path fails silently with no diagnostic; a validate-time warning would help authors.

---

### 🟡 V2.6 — Declarative `search()` title fallback can produce ugly/wrong titles  *(Low; UX)*

**File:** [declarative.py:261-262](web/sources/declarative.py#L261-L262)

When neither a capture group nor `title_regex` yields a title, it derives one from the URL slug (`"berserk-1989".replace("-", " ").title()` → "Berserk 1989"). That's a reasonable fallback but can look odd (numbers, ids in slugs). Fine as a last resort; just flagging that extension authors should provide a title source. A `search` result with no title at all might be better hidden than shown with a mangled slug.

---

### 🟡 V2.7 — `server.py` reload mode diverges from the hardened shutdown path  *(Low; dev-only)*

**File:** [server.py](web/server.py) reload branch

The new `MANGASHELF_RELOAD=1` path uses `uvicorn.run("api:app", reload=True, …)` and `return`s **before** installing the custom SIGTERM force-quit handler and the reliable-shutdown config. That's acceptable (reload is dev-only, and uvicorn's reloader has its own signal handling), but it means the "Ctrl+C sometimes hangs on a phone keep-alive" fix the non-reload path carefully implements doesn't apply in reload mode. Since reload is local-dev only, low impact — worth a one-line comment noting the divergence is intentional.

---

### 🟡 V2.8 — `preview_series` refetches the full chapter feed just to count/list chapters  *(Low; ties into V2.1)*

**File:** [api.py:1829](web/api.py#L1829)

For MangaDex, `fetch_series` fetches the entire paginated chapter feed (to build the chapter list) — necessary for the preview's clickable list, but it's the same heavy call whether the user reads chapter 1 or 400. That's inherent to showing a full chapter list; noting it as the cost of the feature. If preview latency on huge series bothers users, lazy-load the chapter list (first N, "load more") — the reader already has that pattern.

---

## V3. What's Done Well (🟢)

- **The extension system's core safety property holds** — manifests are interpreted, never executed. No `eval`, no dynamic import of manifest content, regexes compiled from data only. This is the correct architecture for multi-user extensibility and it's implemented faithfully. ([declarative.py](web/sources/declarative.py))
- **Primitive reuse over duplication** — `declarative.py` imports the SSRF guard, browser render, UA, and page-number helpers from the (already-audited) AI adapter instead of re-implementing them, so the safety surface stays in one place. ([declarative.py:28](web/sources/declarative.py#L28))
- **Search as an optional interface method** — `can_search` + `search()` on the base class means any source (built-in, extension) joins the web-search fan-out with zero changes to the search plumbing. Clean open/closed design. ([base.py], [registry.py:search_all])
- **Preview proxies remote pages so the reader is unchanged** — the reader gained one `previewSource` prop; remote pages flow through the same `<img>`/lazy-load path as local ones. Minimal, correct integration. ([screens-reader.jsx])
- **Every new fetch is SSRF-guarded + size-capped** — `_fetch_html`, `_fetch_json`, `preview_page`, MangaDex search all bound their reads and reject non-public hosts. Consistent with the hardening from the back-end audit.
- **Validation is thorough and fail-safe at load** — `validate_manifest` checks every field/regex up front; a bad extension is skipped with a logged reason, never crashing discovery. The new `search` block is validated the same way. ([declarative.py:58](web/sources/declarative.py#L58))
- **Search relevance ranking** — gathering spelling variants + ranking by normalized title-closeness is a genuinely good solve for MangaDex's literal matching (the "Highschool" vs "high school" problem). ([mangadex.py] search)
- **Session data-safety** — the tags-as-genres migration was done with a DB backup + single transaction, and verified non-destructive against the backup. Correct handling of a risky data operation.

---

## V4. Recommended Priority Order

1. **Bound the search/preview I/O** (V2.1) — concurrent fan-out with an overall deadline; it's the main new availability risk given the global DB lock.
2. **Fail-closed the preview-page SSRF guard + consider host allowlist / signed URLs** (V2.3) — small change, closes the open-relay edge.
3. **Early-exit MangaDex search on a strong exact match** (V2.2) — cuts upstream calls 3×→1× in the common case.
4. **Add light politeness to preview fetching** (V2.4) — avoid getting the server IP throttled by sources.
5. Remaining items are low-severity polish.

---

## V5. Testing Gap (🟢 Note — still open across all audits)

Five audits in, there is still **no automated test suite**. The new pure-logic surface is especially test-worthy and cheap: `validate_manifest` (accept/reject cases), `_jpath`/`_interp` (the JSONPath subset), `_spelling_variants` + the search ranking, and `DeclarativeSource.search()` against fixture HTML (which I exercised manually during the build — those ad-hoc checks want to be committed pytest cases). The extension engine is exactly the kind of input-driven, edge-case-heavy code where a test suite pays for itself immediately, and it would let future manifest-format changes be made with confidence.

---

*End of Audit V.*

<br>

---
---

# MangaShelf — Front-End Audit II (new UI: Preview, Web Search, Remote Reader)

**Date:** 2026-07-15
**Reviewer:** Senior front-end engineer pass over the client-side work added this session — `PreviewModal`, the web-search section in `SearchPanel`, the reader's remote/preview mode, the metadata-editor rewrite (always-visible rows, Series field, facet refresh), and the extensions-manager UI. Covers React patterns, interaction/loading states, accessibility, and UX consistency for the *new* surface only (the first front-end audit's findings still stand).

---

## FE2.1 Executive Summary

The new UI is **consistent with the app's established, polished patterns** — it reuses the `Modal` (focus trap), the `toast` system, the `Dropdown`, and the existing `Reader` rather than reinventing them, so the new features feel native. The read-before-import preview is a genuinely nice piece of interaction design: a clear "nothing is downloaded until you import" hint, per-chapter loading state, and a clean hand-off into the existing reader via a single `previewSource` prop. The web-search-on-demand (button only when few/no local results) respects the user's bandwidth and is the right trigger model.

The findings are **mostly Medium/Low interaction-polish and a few real correctness edges**: the remote reader inherits chapter-navigation UI that doesn't work in single-chapter preview mode; the preview modal's chapter list has the same accessibility gap as the rest of the app (custom controls); and there's no cancellation/stale-guard on the async preview/search fetches, so fast interactions can show wrong results. None are severe. The biggest *architectural* smell remains the one from audit #1 — the mutable global `STORE` — which the new facet-refresh code has to work around with a manual re-render tick.

**Front-end grade for the new work: B+.** Solid, native-feeling features; the gaps are async-lifecycle hygiene and a couple of preview-mode edge cases.

---

## FE2.2 Findings

### 🟠 FE2.1 — Remote/preview reader shows chapter-nav + split/progress UI that can't work  *(Medium; correctness/UX)*

**Files:** [screens-reader.jsx](web/static/screens-reader.jsx) (Reader), [app.jsx:64](web/static/app.jsx#L64) (PreviewModal builds a 1-chapter item)

In preview mode the reader is opened with an item containing **exactly one chapter** (the one you clicked), and `id: null`. But the reader's chrome still renders controls that assume a full, imported series:
- **Prev/next-chapter buttons** — there's only one chapter, so they're dead (or, worse, `goChapter` indexes out of the single-element `chapterRanges`).
- **"End Chap Here" (split)** — calls `splitChapterHere(item.id, …)` with `item.id === null` → a failed API call if clicked.
- **Auto-status / progress** — correctly no-ops on `item.id === null` (verified in `handleClose`), which is right, but the UI still *implies* progress is tracked.

**Fix:** pass a `preview`/`previewSource` flag down and hide the chapter-nav, split, and any progress affordance in preview mode. The reader already receives `previewSource` — gate those controls on it. Right now it's inconsistent: reading works, but the surrounding controls are misleading or broken.

---

### 🟠 FE2.2 — No stale-response guard on preview/search async fetches  *(Medium; correctness)*

**Files:** [app.jsx](web/static/app.jsx) `openPreview`, [screens-reader.jsx](web/static/screens-reader.jsx) `searchWeb`, PreviewModal `readChapter`

The async fetches don't guard against being superseded. Examples:
- `openPreview(url)` sets `preview` from whatever resolves; click two results quickly and the **slower** response can overwrite the one you're looking at.
- `searchWeb` sets `webResults` on resolve with no check that the query still matches — a slow earlier search can clobber a newer one's results.
- `readChapter` — clicking chapter A then B: both fetch; whichever resolves last opens. `loadingCh` blocks a *second concurrent* click (good), but not a changed-mind sequence after the first resolves.

**Fix:** the standard `alive`/request-id pattern the codebase already uses elsewhere (e.g. Detail's `let alive = true` effect) — capture a token per request and ignore stale resolves. Low effort, prevents "I clicked X but got Y."

---

### 🟠 FE2.3 — Preview page-proxy `<img>` URLs put the auth token in every image request  *(Medium; consistency/leak surface)*

**File:** [api.js](web/static/api.js) `previewPageUrl`, reader `PageImg`

`previewPageUrl` appends `?token=…` (via `tokenParam`) to every proxied page URL so the `<img>` authenticates — consistent with how local covers/pages work. But in preview mode a chapter can be 90+ pages, so the token is now sprayed across dozens of remote-proxy URLs held in the DOM, browser history of image requests, and any error logging. Same pattern as the rest of the app (noted in front-end audit #1), just amplified by preview volume. Not a new *class* of issue, but the preview reader is the highest-volume token-in-URL surface. Mitigation would be app-wide (a short-lived image cookie), so flagging rather than fixing piecemeal.

---

### 🟡 FE2.4 — Preview chapter list: custom `<button>` rows, no keyboard list semantics; whole modal is one long scroll of 400 buttons  *(Low-Medium; a11y + perf)*

**File:** [app.jsx:117-131](web/static/app.jsx#L117-L131)

The chapter list renders **every** chapter as a `<button>` (Berserk = 404 buttons) with no virtualization and no `role="list"`/`listitem` grouping. For huge series that's a lot of DOM nodes in the modal at once (each with an icon SVG). Also there's no chapter search/filter or number-jump, so finding chapter 250 means scrolling. Consider: (a) virtualize or paginate ("Load more", which the detail screen already does), and (b) a quick chapter-number filter. A11y-wise the buttons are individually fine (real `<button>`s, focusable), but a 400-item flat list is hard to navigate by keyboard.

---

### 🟡 FE2.5 — Facet refresh after metadata save relies on mutating global `STORE` + a manual tick  *(Low-Medium; the recurring architecture smell)*

**File:** [screens-detail.jsx](web/static/screens-detail.jsx) `saveField` → `setFacetTick`

The fix that made added series/tags show their usage count works by: mutate `window.STORE.facets` in place, then bump a `facetTick` state to force a re-render. It's correct and I verified it, but it's another instance of the core issue from front-end audit #1 (F2.1): React can't see `STORE` mutations, so every cross-cutting update needs a manual re-render trigger. The `facetTick` is a fine local patch; the systemic fix (`useSyncExternalStore` or a context store) is still the top long-term refactor. Each new feature that touches shared state adds another manual-sync site.

---

### 🟡 FE2.6 — Web-result & preview covers load cross-origin with no fallback tile  *(Low; robustness)*

**Files:** [screens-reader.jsx](web/static/screens-reader.jsx) web-result cards, [app.jsx:92](web/static/app.jsx#L92) preview cover

Web-search result covers and the preview cover load **directly from the source CDN** (`<img src={r.cover_url} referrerPolicy="no-referrer">`) — unlike the library's `CoverImg`, which has a decode-failure fallback tile. If a source hotlink-protects its cover thumbnails (the reason the app proxies *pages*), these show a broken-image icon with no graceful fallback. Consider routing preview/search covers through the same proxy (or giving them an `onError` fallback to the 本 glyph like `CoverImg`). The 256px MangaDex thumbnails work today; other sources may not.

---

### 🟡 FE2.7 — "Search the web" only appears after clicking, and its results don't survive a re-open  *(Low; UX)*

**File:** [screens-reader.jsx](web/static/screens-reader.jsx) `searchWeb` + the `useEffect` resetting on `debouncedQ`

Web results reset whenever the debounced query changes (correct — they're for the old query), but they also vanish if you tweak a filter chip (which changes local `results` but not the query). Minor: a user who searched the web, then toggled a status filter, loses their web results and must "Search again." Scope the web-results reset to genuine query changes only (it mostly is, via `debouncedQ`) and confirm filter toggles don't clear them.

---

### 🟢 FE2.8 — Preview mode correctly disables progress persistence

Verified: the reader's `handleClose`, auto-status, and progress-save effects all guard on `item.id`, and a preview item has `id: null` — so reading a preview chapter never writes bogus progress/status to the DB. This is the right call and was implemented correctly (not just left to chance).

---

## FE2.3 What's Done Well (🟢)

- **Reuses the design system** — `Modal` (focus trap + Escape + ARIA), `toast` for errors, `Dropdown`, and the existing `Reader` are all reused, so the preview/search features feel native, not bolted-on.
- **On-demand web search** — the "Search the web for '…'" button (only shown for a non-URL query, most useful when local results are thin) is the correct trigger: no surprise network calls, user stays in control. ([screens-reader.jsx])
- **Clear preview affordances** — the "nothing is downloaded until you import" hint, per-chapter "loading…" state, `disabled` while a fetch is in flight, and the source badge set honest expectations. The preview modal reads as a considered feature. ([app.jsx:64-133](web/static/app.jsx#L64-L133))
- **Minimal, correct reader integration** — remote reading was added with a single `previewSource` prop threaded through `PageImg` + the preload; the reader's hundreds of lines of navigation/gesture logic are untouched and "just work" with proxied URLs.
- **Metadata editor fixes are real improvements** — always-visible rows (you can actually add tags now), inputs sized to match pills, no empty-pills-row gap, facet counts refresh on save. This closed a self-inflicted regression cleanly.
- **Error handling routes through toasts** — the new async paths (`openPreview`, `searchWeb`, `readChapter`) all surface failures via the themed toast, with a friendly "server needs restart" message for the 404-before-restart case. Consistent with the app.
- **No XSS surface added** — all new interpolation goes through React's escaping; no `dangerouslySetInnerHTML` introduced.

---

## FE2.4 Recommended Priority Order (Front-End)

1. **Gate reader chapter-nav/split/progress UI on preview mode** (FE2.1) — the one place preview is actively misleading/broken.
2. **Add stale-response guards to preview/search fetches** (FE2.2) — cheap, prevents "clicked X, got Y."
3. **Fallback tile for preview/web covers** (FE2.6) — or proxy them; avoids broken-image icons on hotlink-protected sources.
4. **Paginate/virtualize + filter the preview chapter list** (FE2.4) — for 400-chapter series.
5. The `STORE` refactor (FE2.5) remains the top *systemic* item, unchanged from audit #1.

---

*End of front-end audit II.*

<br>

---
---

# MangaShelf — Back-End Audit II (Extensions engine, Search, Preview I/O)

**Date:** 2026-07-15
**Reviewer:** Senior back-end engineer pass over the server-side work added this session — the declarative extension engine (`sources/declarative.py`), MangaDex title-search, the extension install/reload lifecycle (`registry.py`), the `/api/preview/*` + `/api/search-web` endpoints, and the `server.py` auto-reload path. Focus: I/O bounds & timeouts, concurrency/thread-safety, resource lifecycle, transaction/state integrity, and the security boundary of the new fetch surfaces. Prior back-end-audit findings are not repeated.

---

## BE2.1 Executive Summary

The new server code **upholds the codebase's strong safety discipline** — the extension engine never executes manifest content (the critical RCE-avoidance property), reuses the audited SSRF/render primitives, validates every manifest field at load, and writes extension files atomically (temp + `os.replace`). The `reload()`-based hot-swap of the source list is a clean way to pick up installed extensions without a restart.

The findings cluster tightly around **outbound I/O discipline**: several new network paths read responses **without a size cap**, none has a **total-operation timeout** (only per-socket), and the request-thread work is **serial and unbounded** — which, layered on the pre-existing global DB lock, is the real availability risk. There is also a **thread-safety gap** in the `reload()`/`_all()` global-source-list swap, and a couple of resource/lifecycle notes. Nothing is a data-corruption or code-execution bug; the concerns are "a slow or hostile upstream, or concurrent installs, can degrade the server."

**Back-end grade for the new work: A- / B+.** Safety architecture is A; I/O bounding and one concurrency gap are the work items.

---

## BE2.2 Findings

### 🔴 / 🟠 BE2.1 — MangaDex `_get` reads responses with NO size cap  *(Medium-High; unbounded memory — regression vs. the hardening standard)*

**File:** [mangadex.py:26-31](web/sources/mangadex.py#L26-L31)

```python
def _get(url):
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())        # ← unbounded read
    time.sleep(_RATE)
    return data
```

Back-end audit #1 explicitly added size caps to the downloader, cover, and LLM read paths (`r.read(MAX + 1)`). But `_get` — used by **every** MangaDex call including the new `search()` (3× per query) and `preview_series` (the full chapter feed, hundreds of calls) — still does an **unbounded `r.read()`**. A malicious/misbehaving response (or MangaDex returning something huge) streams unbounded into memory. This is the same class the earlier audit fixed elsewhere, left unfixed on the busiest path.

**Fix:** cap it: `raw = r.read(MAX_JSON + 1); if len(raw) > MAX_JSON: raise …`. One-line change, matches the established pattern. Same for `declarative._fetch_json`/`_fetch_html` — those *do* cap (good), so this is specifically `_get`.

---

### 🟠 BE2.2 — No total-operation timeout anywhere; serial fan-out multiplies latency on the request thread  *(Medium; availability)*

**Files:** [mangadex.py:50](web/sources/mangadex.py#L50) (`search` — 3 variants × 30s socket + 0.25s rate each), [api.py:1829](web/api.py#L1829) (`preview_series` → full feed), [registry.py] (`search_all` serial)

Every `urlopen` has a **socket** timeout but there is **no total-operation deadline**, and the work is serial:
- `search()` makes up to **3 sequential** MangaDex calls (spelling variants), each `time.sleep(0.25)` after — so a healthy search is ~1s of forced pauses; a degraded MangaDex is up to ~90s on one worker.
- `search_all()` calls each searchable source **serially**, so N sources = sum of their latencies.
- `preview_series` pages the **entire** chapter feed (Berserk = 404 chapters ≈ hundreds of `_get` calls at 0.25s each ≈ tens of seconds), all on the request thread.

Layered on the global DB `RLock` from audit #1, a few concurrent slow searches/previews can exhaust the FastAPI threadpool and stall unrelated requests (covers, progress saves).

**Fix:** (a) run `search_all` sources concurrently (`ThreadPoolExecutor`) with an overall deadline; (b) short-circuit MangaDex `search` when variant #1 already returns an exact match (avoids 2 calls in the common case — see BE2.3); (c) bound `preview_series`'s feed paging or lazy-load the chapter list.

---

### 🟠 BE2.3 — Spelling-variant fan-out always makes 3 calls even when the first is exact  *(Medium; efficiency)*

**File:** [mangadex.py:62-88](web/sources/mangadex.py#L62-L88)

`search()` always gathers from **all** variants before ranking, so a query like "berserk" (which variant #1 nails at score 1.0) still fires the collapsed + joined-word variants — 3 upstream calls + 3 rate pauses for a result that was decided on call #1. Correctness is fine; it's 3× the traffic and latency for the common exact-match case.

**Fix:** after each variant, check whether an exact (score 1.0) match is already in hand; if so, stop. Keeps the relevance win for the "high school of the dead" case while making the common case one call.

---

### 🟠 BE2.4 — `reload()` swaps the global source list with no lock; concurrent request can iterate a torn/None list  *(Medium; thread-safety)*

**Files:** [registry.py:103-107](web/sources/registry.py#L103-L107) (`reload`), [registry.py:_all], and every `for s in _all()` iterator

`reload()` sets the module global `_sources = None`; the next `_all()` rebuilds it. But this runs under **no lock**, while FastAPI serves requests across a threadpool. Sequence: thread A is mid-`search_all` iterating `_all()`; thread B (an extension install) calls `reload()` → `_sources = None`, then rebuilds. Thread A already holds its list reference so it's *mostly* safe (Python list iteration over a rebound name is fine), **but** `_all()` itself has a check-then-build (`if _sources is None: … _sources = srcs`) that isn't atomic — two threads hitting it during a reload can both rebuild (duplicate work, and `_load_extensions` runs twice concurrently, racing on `_extension_errors`). Low probability, but it's an unsynchronized global-state mutation.

**Fix:** guard `_all()` + `reload()` with a small lock (the app already uses `threading.RLock` liberally), so rebuild is atomic and reload can't interleave with a rebuild.

---

### 🟠 BE2.5 — Preview page-proxy: SSRF guard degrades open on import failure; no host allowlist  *(Medium; security — restated from Audit V with the mechanism)*

**File:** [api.py:1887-1917](web/api.py#L1887-L1917) (`preview_page`)

The proxy is token-gated, SSRF-guarded, and size-capped (good). Two backend concerns:
1. **Fail-open guard:** the SSRF check is wrapped so that if `from sources.ai import _is_public_url` *raises*, it falls through to only `url.startswith("http")` — dropping the private-range check. A guard that can silently disable itself should **fail closed** (reject on guard-unavailable).
2. **Open relay:** any token-holder can proxy any public image URL through the server. For single-user this is fine; if shared, sign the URL when `preview/pages` issues it (HMAC over url+source) and verify in `preview_page`, so only app-generated URLs are proxied.

---

### 🟡 BE2.6 — Preview fetching bypasses the download pipeline's politeness  *(Low-Medium; gets the server IP throttled)*

**Files:** [api.py:1865](web/api.py#L1865) (`preview_pages`), [api.py:1887](web/api.py#L1887) (`preview_page`)

The downloader is deliberately polite (inter-chapter delay, bounded concurrency, retry backoff). The preview path is not: `preview_pages` fetches a chapter's page list, then the reader hammers `preview_page` for each page + preloads. Rapidly previewing chapters could trip a source's rate limiting and get the **server's IP** throttled/blocked — affecting real imports too. Consider a small per-source token-bucket on the proxy, or routing preview fetches through the same politeness the downloader uses.

---

### 🟡 BE2.7 — `preview_page` sets a long browser cache on content it doesn't own  *(Low)*

**File:** [api.py:1917](web/api.py#L1917)

The proxy returns `Cache-Control: public, max-age=3600`. For ephemeral preview pages (URLs that expire — MangaDex `at-home` tokens rotate) a stale cached image could 404 on re-fetch. Minor; a shorter/no cache, or keying off the source's own cache headers, would be safer. Also `public` on an authenticated proxy response is technically incorrect (a shared cache could serve it to another user) — use `private`.

---

### 🟡 BE2.8 — `server.py` reload path skips the hardened shutdown handling  *(Low; dev-only)*

**File:** [server.py](web/server.py) reload branch

`MANGASHELF_RELOAD=1` `return`s before installing the custom SIGTERM force-quit + `timeout_graceful_shutdown` config that the non-reload path carefully set up (to avoid the phone-keep-alive shutdown hang). Reload is local-dev only and uvicorn's reloader manages its own signals, so impact is low — but the divergence should be an explicit comment so nobody assumes the shutdown hardening applies in reload mode. Also `reload_dirs` watches the whole project root, which will trigger reloads on unrelated file writes (e.g. the DB/cache files under the tree if any) — scope it to source dirs.

---

## BE2.3 What's Done Well (🟢)

- **Extensions never execute code** — the single most important backend property for multi-user extensibility. Manifests are validated data; regexes compiled from strings; no `eval`/dynamic import. ([declarative.py])
- **Atomic extension writes** — `install_extension` writes to `.json.tmp` then `os.replace`, and validates *before* writing (a bad manifest leaves nothing on disk). Enable/disable via a `.disabled` marker preserves the file. Consistent with the app's atomic-write discipline. ([registry.py:182](web/sources/registry.py#L182))
- **No path traversal on extension id** — `id` is regex-validated to `[a-z0-9_-]{1,64}` at load, so `_ext_path(id)` can't escape the extensions dir. Correct.
- **SSRF + size caps on the declarative fetches** — `_fetch_html`/`_fetch_json` both guard public-host + cap reads. (The gap is `_get` in mangadex.py, BE2.1 — not the extension engine.)
- **Resilient discovery** — a malformed/invalid manifest is skipped with a recorded error, never crashing source loading; `search_all`/`find_source` swallow per-source exceptions so one bad source can't break the fan-out.
- **Search as an interface method** — `can_search` + `search()` on the base class means the fan-out (`search_all`) and the UI light up for any source automatically. Clean extension point.
- **Preview proxy fundamentals** — token-gated, SSRF-guarded, size-capped, streams rather than buffering to disk. The bones are right; the refinements (fail-closed, politeness, cache-control) are BE2.5–BE2.7.
- **Auto-reload is opt-in and non-invasive** — gated behind an env var, leaving the production/remote run on the hardened path. The right way to add a dev convenience.

---

## BE2.4 Recommended Priority Order

1. **Cap `_get`'s read** (BE2.1) — one line, closes an unbounded-memory hole on the busiest upstream path; matches the standard the rest of the app already meets.
2. **Bound the search/preview I/O** (BE2.2) — concurrent `search_all` + overall deadline; the main availability risk given the global DB lock.
3. **Lock `_all()`/`reload()`** (BE2.4) — small, removes the unsynchronized global-state race.
4. **Fail-closed the proxy SSRF guard** (BE2.5) — cheap; sign URLs / host-allowlist only if the app will be shared.
5. **Short-circuit MangaDex search on exact match** (BE2.3) + **preview politeness** (BE2.6) — cut upstream load / avoid throttling.
6. Cache-control + reload-dir scoping are low-severity polish.

---

## BE2.5 Cross-Cutting Note

Back-end audit #1's theme was *transaction + resource discipline*; this pass's theme is **outbound-I/O discipline**. The new features moved the app from mostly-local I/O (disk, one SQLite file) to **heavy outbound HTTP on the request thread** — search fan-out, full chapter feeds, per-page proxying. That's a different failure surface: not corruption, but **latency amplification and unbounded reads** that a slow/hostile upstream can weaponize, made worse by the single serializing DB lock. Bounding these (size caps everywhere, total deadlines, concurrent-but-capped fan-out, off-thread heavy fetches) is the work that would take the new features from "works great on a healthy network" to "degrades gracefully."

---

*End of back-end audit II.*

<br>

---
---

# MangaShelf — UI/UX Design Audit II (Web Search, Preview, Metadata editor)

**Date:** 2026-07-15
**Reviewer:** Senior UI/UX designer pass over the *rendered* new features — captured live screenshots of the web-search results, the read-before-import preview modal, and the reworked metadata editor. Focus: information hierarchy, visual balance, interaction affordances, and consistency with the app's established "Cyber-Shrine" language. This covers new surfaces only; the first design audit's findings (desktop max-width, filter overflow) were separately addressed during the session.

---

## D2.1 Executive Summary

The new features **inherit the app's strong visual identity cleanly** — the preview modal, web-result cards, and search section all use the established palette, type, badges, and card treatments, so nothing looks bolted-on. The read-before-import preview is a legitimately good *concept* rendered with care (source badge, cover, description, honest "streams live, nothing downloaded" hint). The metadata editor rework is a clear win: all fields visible, properly sized, with usage counts.

But the *rendered* screenshots surface **one repeated composition problem that hurts every new screen: priority-inversion via empty space.** In the search panel, an empty-state placeholder (the 無 glyph + "Not in your library") **dominates the center of the screen while the actual web results are buried at the bottom, below the fold.** On the detail page, the metadata sits in a narrow left band while the entire right two-thirds is empty void. The content the user wants is consistently de-emphasized relative to whitespace and placeholders. This is the same *class* of issue flagged in design audit #1 (D2.1 dead-space, D2.4 hierarchy) — the new screens reproduce it.

**Design grade for the new work: B.** The components are on-brand and well-crafted individually; the layout composition (what gets visual priority) is where the new screens lose points.

---

## D2.2 Findings

### 🔴 D2.1 — Search panel: empty-state dominates, web results buried below the fold  *(High; information hierarchy)*

**Observed:** [web-search screenshot] Searching "berserk" with no local match shows the large 無 glyph + "Not in your library. Search the web to import it." filling the **center** of the panel, while "WEB RESULTS · 20" — the 20 actual importable results the user is looking for — sit **crammed at the very bottom, the cards clipped by the panel edge.** The eye lands on an empty placeholder; the payload requires scrolling to even see.

**Why it matters:** the entire point of this flow is "find something to import." The results are the content; the empty-state is scaffolding. Right now the scaffolding wins the visual hierarchy. A first-time user sees "無 / Not in your library" and may think the feature failed, not realizing 20 results are below.

**Fix:** once web results exist, they should take the **primary vertical space** — the "no local matches" empty-state should collapse to a single quiet line ("No matches in your library — showing web results below") or disappear entirely, and the results grid should start near the top of the body. Don't render the full-height 無 empty-state when web results are present or loading.

---

### 🟠 D2.2 — Detail metadata: content in a narrow left band, right two-thirds is empty void  *(Medium; visual balance — recurring)*

**Observed:** [metadata screenshot] The metadata rows (Series/Parodies/Tags/…/Pages) occupy a ~360px-wide column on the left of the right pane; everything to the right of the values — roughly **60% of the page width** — is empty black. The chapters grid (the other main content) is far below the fold under the tall cover column.

**Why it matters:** this is the same dead-space/hierarchy issue from design audit #1 (D2.4), still present on the most-used screen. The page reads as sparse/unfinished, and the two primary contents (metadata to edit, chapters to read) are split far apart with a void between.

**Fix (still open from audit #1):** use the empty right space — e.g. flow the chapter grid into the right column beside/below the metadata so it's above the fold, or widen the metadata values / add a second column (tags could wrap wider). The metadata editor itself is now good; it's the *placement* that wastes the canvas.

---

### 🟠 D2.3 — Web-result & preview cards mix line-clamped titles with monospace metadata inconsistently  *(Medium; polish/legibility)*

**Observed:** [web-search + preview screenshots] Web-result cards show a 2-line-clamped title + a monospace "author · year" sub with a green MANGADEX badge. It reads well, but: the **badge repeats on every card** (redundant when all results are one source), and the author/year line **truncates awkwardly** ("Miura Kentarou · 1…" — the year cut mid-digit). The preview modal's chapter rows are cleaner.

**Fix:** (a) when all web results share one source, show the source **once** in the "WEB RESULTS" header rather than a badge per card (reintroduce per-card badges only when results span multiple sources — which the architecture now supports); (b) let the year have room or drop it before truncating the author — a half-year ("1…") reads as broken.

---

### 🟡 D2.4 — Preview modal: 400-chapter flat list with no jump/filter, and the "click to read" intent isn't obvious enough  *(Low-Medium; usability at scale)*

**Observed:** [preview modal, earlier full screenshot] A chapter-rich series (Berserk = 404 rows) is a very long flat scroll. The "Chapters — click to read" header states intent, and each row has a ▸ play glyph, which is good — but there's no way to jump to a chapter number, and the rows are visually uniform so scanning 400 is tedious.

**Fix:** add a chapter-number filter/jump at the top of the list (even a simple "go to #" input), and consider a compact multi-column grid for the chapter list on wide screens (the detail screen already uses a chapter grid). Reinforce the click affordance — the whole row highlighting on hover already helps; a subtle "Read →" on hover would make it unmistakable.

---

### 🟡 D2.5 — "Preview" vs "Import" mental model could be clearer up front  *(Low; UX clarity)*

**Observed:** clicking a web result opens the preview modal (good), but the *first* time, a user might expect clicking a search result to import directly (the tooltip says "Preview…"). The distinction "browse/read first, import when ready" is communicated well *inside* the modal (the hint line), but the entry point (a plain result card) doesn't signal "this opens a preview, not an import."

**Fix:** minor — the result card could carry a tiny "preview" affordance or the empty-state line could say "search the web to preview & import." The in-modal hint already does the heavy lifting; this is just smoothing the first-run expectation.

---

### 🟢 D2.6 — Preview modal composition (header) is well-balanced

The preview modal's **header** (cover left, title/author/genres/description/actions right, source badge, close button top-right) is genuinely well-composed — clear hierarchy, the primary "Import to library" action is prominent, "Open source ↗" is appropriately secondary, and the description clamps cleanly (a fix made this session). This is the app's card/detail language applied correctly. The *body* (chapter list) is where D2.4 applies.

---

## D2.3 What's Done Well (🟢)

- **On-brand from day one** — every new surface uses the ink palette, vermillion/jade accents, mono metadata, badge pills, and card radii already established. The features look like they were always part of the app.
- **Honest, well-placed microcopy** — "Reading a chapter streams it live — nothing is downloaded until you import" sets the right expectation exactly where the user needs it. The source badge + "preview" label are honest about provenance.
- **Metadata editor is fixed and clean** — [screenshot] all fields visible with "+ Add" affordances, inputs sized to match pills, usage count on the Series value ("Fairy Tail · 1"), no wasted row height. This closed a self-inflicted regression well and now reads as a proper editable form.
- **Preview modal header hierarchy** — cover/title/actions are correctly prioritized; import is the clear primary CTA (D2.6).
- **On-demand web search respects the user** — no results shown until asked; the "Search again" affordance and result count are clear.
- **Consistent empty/loading states** — the 無 glyph, "loading…" per chapter, and the toast error path all match the app's existing vocabulary (even where D2.1 argues the empty-state is over-weighted, it's the *right* visual element, just mis-sized).

---

## D2.4 Recommended Priority Order (Design)

1. **Fix the search-panel hierarchy** (D2.1) — when web results exist, they get primary space; the empty-state collapses to a line. Highest-impact: it's what a user sees when the feature does its main job.
2. **Use the detail page's empty right space** (D2.2) — recurring from audit #1; get chapters and metadata to share the canvas instead of a void.
3. **De-duplicate the per-card source badge + fix year truncation** (D2.3).
4. **Chapter-number jump/filter + grid for long preview lists** (D2.4).
5. Preview entry-point signaling (D2.5) is minor polish.

---

## D2.5 One-Paragraph Verdict

The new features are **well-crafted at the component level and fully on-brand** — the preview modal header and the fixed metadata editor are genuinely nice. What holds the new work at a B is **composition**: the rendered screens repeatedly give visual priority to empty space and placeholders over the actual content (web results buried under a full-height empty-state; metadata marooned beside a 60%-empty pane). This is the same hierarchy/dead-space theme from the first design audit, reproduced on the new screens. The fix is layout, not restyling — the pieces are already beautiful; they just need to occupy the canvas in priority order. Fix D2.1 first: it's the moment the search feature proves its value, and right now that moment shows an empty glyph instead of 20 results.

---

*End of UI/UX design audit II.*
