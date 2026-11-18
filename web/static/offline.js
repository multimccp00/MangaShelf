/* global window, API, pageUrl, coverUrl, authHeaders */
/* offline.js — download series/chapters for offline reading.
 *
 * Downloaded content lives in Cache Storage ("ms-offline"): page images, the first
 * page's thumbnail per chapter, the cover, and the series JSON. sw.js serves them
 * back when there is no connection, so the reader/detail screens work unchanged.
 * What is saved is tracked in a small manifest in localStorage ("ms_offline"). */
(function () {
  const CACHE = "ms-offline";
  const MAN_KEY = "ms_offline";
  const PREF_KEY = "ms_offline_prefs";
  const CONCURRENCY = 3;

  const supported = !!(window.isSecureContext && "serviceWorker" in navigator && window.caches);

  // Same normalisation as sw.js: the cache key ignores token / width / cache-bust.
  function keyOf(url) {
    const u = new URL(url, window.location.href);
    for (const p of ["token", "w", "v"]) u.searchParams.delete(p);
    return u.href;
  }

  // ---- optional native storage (the Android wrapper app exposes window.MSNative) -----------
  // Page images / thumbnails / covers can live as plain files on another volume (the SD card)
  // instead of Cache Storage. The wrapper answers requests for them from those files, so the
  // reader works unchanged. Without MSNative (browser, PWA) everything stays in Cache Storage.
  const N = window.MSNative || null;
  // Same string the wrapper computes: path + "?" + decoded params sorted by name, minus token/w/v.
  function canon(url) {
    const u = new URL(url, window.location.href);
    const ps = [...u.searchParams].filter(([k]) => k !== "token" && k !== "w" && k !== "v")
      .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
    return u.pathname + "?" + ps.map(([k, v]) => `${k}=${v}`).join("&");
  }
  const nativeOn = () => !!N && !!N.location();
  const waiting = {}; let seq = 0;
  window.__msw = (id, ok) => { const f = waiting[id]; if (f) { delete waiting[id]; f(ok); } };
  const toB64 = (blob) => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1] || "");
    r.onerror = () => rej(r.error);
    r.readAsDataURL(blob);
  });
  async function hasImg(cache, url) {
    return (!!N && N.has(canon(url))) || !!(await cache.match(keyOf(url)));
  }
  async function putImg(cache, url, blob) {
    if (nativeOn()) {
      const ok = await new Promise((resolve) => {
        const id = ++seq; waiting[id] = resolve;
        toB64(blob).then((b64) => N.write(id, canon(url), blob.type || "image/jpeg", b64)).catch(() => { delete waiting[id]; resolve(false); });
      });
      if (ok) return true;                             // saved on the chosen volume
    }
    await cache.put(keyOf(url), new Response(blob, { headers: { "Content-Type": blob.type || "image/jpeg" } }));
    return false;
  }
  async function delImg(cache, url) {
    if (N) N.remove(canon(url));
    await cache.delete(keyOf(url));
  }

  const readJSON = (k, d) => { try { return JSON.parse(localStorage.getItem(k) || "") || d; } catch (e) { return d; } };
  const writeJSON = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };

  let man = readJSON(MAN_KEY, {});
  const saveMan = () => writeJSON(MAN_KEY, man);
  const emit = (sid) => window.dispatchEvent(new CustomEvent("ms-offline", { detail: { sid } }));

  let moving = null;   // { done, total } while saved files are being moved
  const ctl = {};   // seriesId -> AbortController of the running download

  const Offline = {
    supported,
    persisted: null,

    // ---- preferences (per device, so kept locally, not on the server) ---------
    prefs() { return { wifiOnly: false, autoDelete: false, width: 0, ...readJSON(PREF_KEY, {}) }; },
    setPrefs(p) { writeJSON(PREF_KEY, { ...this.prefs(), ...p }); emit(); },

    // ---- queries --------------------------------------------------------------
    // 'none' | 'partial' | 'done'
    chapterState(sid, idx) {
      const c = man[sid] && man[sid].chapters[idx];
      if (!c) return "none";
      return c.pages > 0 && c.done >= c.pages ? "done" : "partial";
    },
    seriesState(sid) {
      const m = man[sid];
      if (!m) return "none";
      const cs = Object.values(m.chapters);
      if (!cs.length) return "none";
      return cs.every((c) => c.pages > 0 && c.done >= c.pages) ? "done" : "partial";
    },
    isDownloading(sid) { return !!ctl[sid]; },
    progress(sid) {
      const cs = man[sid] ? Object.values(man[sid].chapters) : [];
      return { done: cs.reduce((a, c) => a + c.done, 0), total: cs.reduce((a, c) => a + c.pages, 0) };
    },
    keepN(sid) { return (man[sid] && man[sid].keepN) || 0; },
    list() {
      return Object.entries(man).map(([id, m]) => {
        const cs = Object.values(m.chapters);
        return {
          id: Number(id), title: m.title, cover_v: m.cover_v, chapters: cs.length,
          pages: cs.reduce((a, c) => a + (c.pages || 0), 0),
          done: cs.filter((c) => c.pages > 0 && c.done >= c.pages).length,
          bytes: cs.reduce((a, c) => a + (c.bytes || 0), 0), savedAt: m.savedAt, keepN: m.keepN || 0,
        };
      });
    },
    // Saved series as full rows (status, progress, tags… as of the download) for
    // building Home/Library offline. Falls back to the bare manifest entry.
    async rows() {
      const cache = await caches.open(CACHE);
      return Promise.all(this.list().filter((s) => s.chapters > 0).map(async (s) => {
        try {
          const hit = await cache.match(keyOf(`${API}/series/${s.id}`));
          if (hit) { const j = await hit.json(); return { ...j, id: s.id, chapters: undefined }; }
        } catch (e) {}
        return { id: s.id, title: s.title, cover_v: s.cover_v, total_pages: s.pages };
      }));
    },
    async usage() {
      let quota = 0;
      try { const e = await navigator.storage.estimate(); quota = e.quota || 0; } catch (e) {}
      return { used: this.list().reduce((a, s) => a + s.bytes, 0), quota, persisted: this.persisted };
    },
    async requestPersist() {
      try {
        if (navigator.storage && navigator.storage.persist) {
          this.persisted = (await navigator.storage.persisted()) || (await navigator.storage.persist());
        }
      } catch (e) { this.persisted = false; }
      return this.persisted;
    },
    // Where new downloads are written. Only in the Android wrapper (window.MSNative); null elsewhere.
    storage: N ? {
      volumes() { try { return JSON.parse(N.volumes()); } catch (e) { return []; } },
      location() { return N.location(); },            // "" = Cache Storage inside the app
      setLocation(p) { const ok = N.setLocation(p || ""); emit(); return ok; },
    } : null,
    // Drop manifest entries whose files are gone (browser storage can be cleared behind our back).
    async verify() {
      if (moving) return 0;
      if (N) {   // an SD card that's simply unplugged must not look like "files missing"
        const now = this.storage.volumes().map((v) => v.path);
        const seen = readJSON("ms_vols", []);
        writeJSON("ms_vols", [...new Set([...seen, ...now])]);
        if (seen.some((p) => !now.includes(p))) return 0;
      }
      const cache = await caches.open(CACHE);
      let lost = 0;
      for (const sid of Object.keys(man)) {
        const m = man[sid];
        if (ctl[sid] || !Object.keys(m.chapters).length) continue;
        const hit = await cache.match(keyOf(`${API}/series/${sid}`));
        const json = hit ? await hit.json().catch(() => null) : null;
        for (const idx of Object.keys(m.chapters)) {
          const ch = json && json.chapters.find((c) => String(c.index) === idx);
          let have = 0;
          if (ch) for (const abs of ch.pages) if (await hasImg(cache, pageUrl(abs))) have++;
          if (have === 0) { delete m.chapters[idx]; lost++; }
          else m.chapters[idx].done = Math.min(m.chapters[idx].done, have);
        }
        if (!Object.keys(m.chapters).length) { await cache.delete(keyOf(`${API}/series/${sid}`)); if (!m.keepN) delete man[sid]; }
      }
      if (lost) { saveMan(); emit(); if (window.toast) window.toast(`${lost} saved chapter${lost > 1 ? "s were" : " was"} missing from this device and removed from the list`, "error"); }
      return lost;
    },
    moving() { return moving; },
    // Move everything already saved to the current download location (Android app only).
    async move() {
      if (!N || moving) return { moved: 0, failed: 0 };
      const cache = await caches.open(CACHE);
      const urls = [];
      for (const sid of Object.keys(man)) {
        const hit = await cache.match(keyOf(`${API}/series/${sid}`));
        if (!hit) continue;
        const json = await hit.json();
        urls.push(coverUrl({ id: Number(sid) }));
        for (const ch of json.chapters) {
          if (!man[sid].chapters[ch.index]) continue;
          if (ch.pages[0]) urls.push(pageUrl(ch.pages[0], true));
          for (const abs of ch.pages) urls.push(pageUrl(abs));
        }
      }
      moving = { done: 0, total: urls.length }; emit();
      let failed = 0;
      for (const u of urls) {
        const key = canon(u);
        try {
          if (nativeOn()) {
            if (N.has(key)) {
              if (!(await new Promise((r) => { const id = ++seq; waiting[id] = r; N.moveTo(id, key); }))) failed++;
            } else {
              const hit = await cache.match(keyOf(u));
              if (hit && (await putImg(cache, u, await hit.blob()))) await cache.delete(keyOf(u));
              else if (hit) failed++;
            }
          } else if (N.has(key)) {          // target is "inside the app": bring the file back into Cache Storage
            const r = await fetch(u);
            if (!r.ok) throw new Error(`${r.status}`);
            await cache.put(keyOf(u), new Response(await r.blob(), { headers: { "Content-Type": r.headers.get("Content-Type") || "image/jpeg" } }));
            N.remove(key);
          }
        } catch (e) { failed++; }
        moving.done++;
        if (moving.done % 10 === 0) emit();
      }
      const moved = urls.length - failed;
      moving = null; emit();
      return { moved, failed };
    },
    blockedByWifiOnly() {
      const c = navigator.connection;
      return this.prefs().wifiOnly && !!c && c.type === "cellular";
    },

    // ---- download -------------------------------------------------------------
    // Fetch the series JSON (and cache it so the Detail screen opens offline).
    async _fetchSeries(sid, cache) {
      const url = `${API}/series/${sid}`;
      const res = await fetch(url, { headers: authHeaders() });
      if (!res.ok) throw new Error(`${res.status}`);
      await cache.put(keyOf(url), res.clone());
      return res.json();
    },

    // idxs: chapter indexes to save, or null for every chapter.
    async download(sid, idxs) {
      if (!supported || ctl[sid]) return;
      if (this.blockedByWifiOnly()) throw new Error("Wi-Fi only is on and you're on mobile data");
      const ac = (ctl[sid] = new AbortController());
      emit(sid);
      try {
        await this.requestPersist();
        const cache = await caches.open(CACHE);
        const json = await this._fetchSeries(sid, cache);
        const w = this.prefs().width;
        const m = (man[sid] = man[sid] || { chapters: {} });
        m.title = json.title; m.cover_v = json.cover_v; m.savedAt = Date.now();
        m.total = json.total_pages || 0;

        try {   // cover (best effort)
          const cu = coverUrl({ id: sid, cover_v: json.cover_v });
          const cr = await fetch(cu, { signal: ac.signal });
          if (cr.ok) await putImg(cache, cu, await cr.blob());
        } catch (e) { if (ac.signal.aborted) throw e; }

        // Global 1-based page range of each chapter (matches the reader / progress).
        let start = 1;
        const chapters = json.chapters.map((c) => {
          const r = { c, start, end: start + c.page_count - 1 };
          start = r.end + 1;
          return r;
        });
        const want = chapters.filter((r) => !idxs || idxs.includes(r.c.index));

        // Register every wanted chapter up front so progress() totals the whole
        // job (one steady 0-100%) instead of restarting for each chapter.
        for (const { c, start: s, end } of want) {
          const prev = m.chapters[c.index];
          m.chapters[c.index] = { name: c.name, pages: c.pages.length, done: 0, bytes: (prev && prev.bytes) || 0, start: s, end };
        }
        saveMan(); emit(sid);

        for (const { c } of want) {
          const entry = m.chapters[c.index];
          if (c.pages[0]) {   // chapter-card thumbnail
            try {
              const tu = pageUrl(c.pages[0], true);
              if (!(await hasImg(cache, tu))) {
                const tr = await fetch(tu, { signal: ac.signal });
                if (tr.ok) await putImg(cache, tu, await tr.blob());
              }
            } catch (e) { if (ac.signal.aborted) throw e; }
          }
          let next = 0;
          const worker = async () => {
            while (next < c.pages.length) {
              const abs = c.pages[next++];
              const url = pageUrl(abs) + (w ? `&w=${w}` : "");
              if (!(await hasImg(cache, url))) {
                const res = await fetch(url, { signal: ac.signal });
                if (!res.ok) throw new Error(`${res.status}`);
                const blob = await res.blob();
                await putImg(cache, url, blob);
                entry.bytes += blob.size;
              }
              entry.done++;
              if (entry.done % 5 === 0) { saveMan(); emit(sid); }
            }
          };
          await Promise.all(Array.from({ length: CONCURRENCY }, worker));
          saveMan(); emit(sid);
        }
      } finally {
        delete ctl[sid];
        saveMan(); emit(sid);
      }
    },
    pause(sid) { if (ctl[sid]) ctl[sid].abort(); },

    async remove(sid, idxs) {
      const m = man[sid];
      if (!m) return;
      this.pause(sid);
      const cache = await caches.open(CACHE);
      const sKey = keyOf(`${API}/series/${sid}`);
      const hit = await cache.match(sKey);
      const json = hit ? await hit.json() : null;
      const targets = idxs || Object.keys(m.chapters).map(Number);
      for (const idx of targets) {
        const ch = json && json.chapters.find((c) => c.index === idx);
        if (ch) {
          for (const abs of ch.pages) await delImg(cache, pageUrl(abs));
          if (ch.pages[0]) await delImg(cache, pageUrl(ch.pages[0], true));
        }
        delete m.chapters[idx];
      }
      if (!Object.keys(m.chapters).length) {
        await cache.delete(sKey);
        await delImg(cache, coverUrl({ id: sid }));
        if (!m.keepN) delete man[sid];
      }
      saveMan(); emit(sid);
    },

    setKeepN(sid, n) {
      man[sid] = man[sid] || { chapters: {}, title: "" };
      man[sid].keepN = n || 0;
      if (!man[sid].keepN && !Object.keys(man[sid].chapters).length) delete man[sid];
      saveMan(); emit(sid);
      if (n) this.maintain();
    },

    // ---- automatic behaviour ---------------------------------------------------
    // Called by ApiClient.saveProgress on every page turn: drop chapters you've
    // moved past when "auto-delete after reading" is on.
    onProgress(sid, _chapterName, page) {
      const m = man[sid];
      if (!m || !this.prefs().autoDelete) return;
      for (const [idx, c] of Object.entries(m.chapters)) {
        if (c.end && (page > c.end || (page >= c.end && c.end === m.total))) this.remove(sid, [Number(idx)]);
      }
    },

    // Top up "keep next N" series and resume interrupted downloads. Skipped while
    // offline, while reading progress is still waiting to sync (the plan would be
    // computed from a stale position), or on mobile data with Wi-Fi-only on.
    async maintain() {
      if (!supported || navigator.onLine === false || this.blockedByWifiOnly()) return;
      if (window.SyncQueue && window.SyncQueue.pending()) return;
      for (const sid of Object.keys(man)) {
        if (ctl[sid]) continue;
        const m = man[sid];
        const partial = Object.entries(m.chapters).filter(([, c]) => !(c.pages > 0 && c.done >= c.pages)).map(([i]) => Number(i));
        let want = partial;
        if (m.keepN) {
          try {
            const cache = await caches.open(CACHE);
            const json = await this._fetchSeries(sid, cache);
            let start = 1;
            const rows = json.chapters.map((c) => { const r = { idx: c.index, start, end: start + c.page_count - 1 }; start = r.end + 1; return r; });
            const cur = Math.max(1, json.last_page || 1);
            const at = Math.max(0, rows.findIndex((r) => cur >= r.start && cur <= r.end));
            const keep = rows.slice(at, at + 1 + m.keepN).map((r) => r.idx);
            want = [...new Set([...partial, ...keep.filter((i) => this.chapterState(sid, i) !== "done")])];
          } catch (e) { continue; }
        }
        if (want.length) { try { await this.download(Number(sid), want); } catch (e) { /* retry next time */ } }
      }
    },
  };

  // "Online" for our purposes means the SERVER answers — a phone on Wi-Fi with
  // Tailscale off reports navigator.onLine=true but can't reach the library.
  Offline.reachable = true;
  Offline.probe = async function () {
    let ok = false;
    try {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), 3000);
      ok = (await fetch(`${API}/health`, { cache: "no-store", signal: ac.signal })).ok;
      clearTimeout(t);
    } catch (e) { ok = false; }
    if (ok !== Offline.reachable) {
      Offline.reachable = ok;
      emit();
      if (ok) { window.SyncQueue && window.SyncQueue.flush(); setTimeout(() => Offline.maintain(), 1000); }
    }
    return ok;
  };
  setInterval(() => Offline.probe(), 15000);
  window.addEventListener("online", () => Offline.probe());
  window.addEventListener("offline", () => { Offline.reachable = false; emit(); });
  Offline.probe();

  window.Offline = Offline;

  if (supported) {
    const base = API.replace(/api$/, "");   // "/" at root, "/manga/" behind Tailscale
    navigator.serviceWorker.register(base + "sw.js", { scope: base }).catch((e) => console.warn("[offline] sw:", e));
    // The very first page load happens before the worker controls it, so the shell
    // (html/js/css) was never stored. Re-fetch it once the worker is ready; the
    // worker caches each response, making the app launchable with no connection.
    navigator.serviceWorker.ready.then(() => {
      const urls = [window.location.pathname, ...Array.from(document.querySelectorAll("script[src],link[rel=stylesheet],link[rel=manifest],link[rel~=icon]"))
        .map((el) => el.src || el.href)];
      urls.forEach((u) => {
        try { if (new URL(u, window.location.href).origin === window.location.origin) fetch(u).catch(() => {}); } catch (e) {}
      });
    });
    window.addEventListener("online", () => setTimeout(() => Offline.maintain(), 2000));
    window.addEventListener("ms-synced", () => Offline.maintain());
    setTimeout(() => Offline.verify().then(() => Offline.maintain()), 4000);
  }
})();
