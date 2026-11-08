/* AirStation Studio (admin) */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const S = { view: "dashboard", page: 0, playlists: [], selected: new Set(), timer: null };

  async function api(path, method = "GET", body) {
    const res = await fetch("/api/admin" + path, {
      method, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "air-admin" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch { /* */ }
    if (res.status === 401 && path !== "/login") { showLogin(); throw new Error("Please log in."); }
    if (res.status === 402) { refreshLicence(); if (S.view !== "licence") show("licence"); }
    if (!res.ok) throw new Error(data.error || `Error ${res.status}`);
    return data;
  }

  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) n.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
    return n;
  }

  function toast(msg, err = false) {
    const t = $("[data-toast]");
    t.textContent = msg; t.classList.toggle("err", err); t.classList.add("show");
    clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 3800);
  }
  const guard = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

  const gb = b => (b / 1073741824).toFixed(2) + " GiB";
  const mmss = s => s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "–";
  const when = ts => ts ? new Date(ts * 1000).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "never";

  /* ------------------------------------------------------------ views */
  const OPEN_WHEN_LOCKED = new Set(["dashboard", "licence"]);
  function show(view) {
    if (S.licence && S.licence.locked && !OPEN_WHEN_LOCKED.has(view)) {
      toast("The trial has ended: enter a licence key to use this page. The station keeps broadcasting.", true);
      view = "licence";
      history.replaceState(null, "", "#licence");
    }
    S.view = view;
    $$("[data-view]").forEach(v => v.hidden = v.dataset.view !== view);
    $$("[data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === view));
    clearInterval(S.timer);
    ({ dashboard: loadDashboard, library: loadLibrary, playlists: loadPlaylists,
       schedule: loadSchedule, settings: loadSettings, connect: loadConnect, duplicates: loadDuplicates,
       appearance: loadAppearance, licence: loadLicence, listeners: () => loadListeners().catch(e => toast(e.message, true)) })[view]?.();
    if (view === "dashboard") S.timer = setInterval(loadDashboard, 5000);
    if (view === "listeners") S.timer = setInterval(() => loadListeners().catch(() => {}), 60000);
  }
  function showLogin() {
    clearInterval(S.timer);
    $$("[data-view]").forEach(v => v.hidden = v.dataset.view !== "login");
    $("[data-tabs]").hidden = true; $("[data-logout]").hidden = true;
  }
  async function loggedIn() {
    $("[data-tabs]").hidden = false; $("[data-logout]").hidden = false;
    await refreshLicence();
    show(location.hash.slice(1) || "dashboard");
  }
  $$("[data-tab]").forEach(b => b.addEventListener("click", () => { location.hash = b.dataset.tab; show(b.dataset.tab); }));
  $("[data-login-form]").addEventListener("submit", guard(async e => {
    e.preventDefault();
    await api("/login", "POST", { password: e.target.password.value });
    e.target.reset(); loggedIn();
  }));
  $("[data-logout]").addEventListener("click", guard(async () => { await api("/logout", "POST", {}); showLogin(); $("[data-licbar]").hidden = true; }));

  /* ---------------------------------------------------------- licence */
  async function refreshLicence() {
    try { S.licence = (await api("/me")).licence || null; } catch { return; }
    licenceBar(S.licence);
  }
  function licenceBar(l) {
    const bar = $("[data-licbar]");
    const showBar = l && (l.status === "expired" || (l.status === "trial" && l.days_left <= 7));
    bar.hidden = !showBar;
    if (showBar) {
      bar.classList.toggle("expired", l.status === "expired");
      $("[data-licbar-text]").textContent = l.message;
    }
    $$("[data-tab]").forEach(b => b.classList.toggle("locked", !!(l && l.locked) && !OPEN_WHEN_LOCKED.has(b.dataset.tab)));
  }
  $("[data-licbar-go]").addEventListener("click", () => { location.hash = "licence"; show("licence"); });

  const LSTATUS = { licensed: ["Licensed", "ok"], trial: ["Trial", "warn"], expired: ["Trial ended", "off"], developer: ["Developer build", "dim"] };
  function renderLicence(l) {
    S.licence = { status: l.status, locked: l.locked, days_left: l.days_left, message: l.message };
    licenceBar(S.licence);
    const [label, cls] = LSTATUS[l.status] || [l.status, "dim"];
    const pill = $("[data-lic-status]");
    pill.textContent = label; pill.className = "pill " + cls;
    $("[data-lic-message]").textContent = l.message;
    const copy = v => el("button", { class: "btn small", type: "button", onclick: () => navigator.clipboard.writeText(v).then(() => toast("Copied.")) }, "Copy");
    const rows = [];
    if (l.licence) {
      rows.push(["Licensed to", l.licence.name || "–", ""], ["Licence", `${l.licence.id} (key …${l.licence.hint})`, ""],
        ["Activation", l.licence.kind === "offline" ? "Offline certificate" : "Online", ""]);
      if (l.checked_at) rows.push(["Last checked", when(l.checked_at), ""]);
    } else if (l.status === "trial" || l.status === "expired") {
      rows.push(["Trial ends", new Date(l.trial_ends * 1000).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" }), ""]);
    }
    rows.push(["Station domain", l.domain, l.domain], ["Install id", l.install_id, l.install_id], ["Version", l.version, ""]);
    if (l.last_error) rows.push(["Last message", l.last_error, ""]);
    $("[data-lic-details]").replaceChildren(...rows.flatMap(([k, v, c]) => [el("dt", {}, k), el("dd", {}, v), c ? copy(c) : el("span")]));
    const licensed = l.status === "licensed";
    $("[data-lic-activate]").hidden = licensed || l.status === "developer";
    $("[data-lic-check]").hidden = !licensed || l.licence.kind === "offline" || !l.server;
    $("[data-lic-deactivate]").hidden = !licensed;
    $("[data-lic-deactivate-hint]").hidden = !licensed;
    const buy = $("[data-lic-buy]");
    buy.hidden = !l.purchase_url;
    if (l.purchase_url) buy.href = l.purchase_url;
  }
  async function loadLicence() { renderLicence(await api("/licence")); }
  $("[data-lic-form]").addEventListener("submit", guard(async e => {
    e.preventDefault();
    const btn = e.target.querySelector("button"); btn.disabled = true;
    try {
      renderLicence(await api("/licence/activate", "POST", { key: e.target.key.value.trim() }));
      e.target.reset(); toast("Licence activated. Thank you!");
    } finally { btn.disabled = false; }
  }));
  $("[data-lic-certform]").addEventListener("submit", guard(async e => {
    e.preventDefault();
    renderLicence(await api("/licence/activate", "POST", { certificate: e.target.certificate.value }));
    e.target.reset(); toast("Certificate accepted. Thank you!");
  }));
  $("[data-lic-check]").addEventListener("click", guard(async () => {
    const r = await api("/licence/check", "POST", {});
    renderLicence(r); toast(r.result === "refreshed" ? "Licence confirmed." : `Check: ${r.result}`);
  }));
  $("[data-lic-deactivate]").addEventListener("click", guard(async () => {
    if (!confirm("Deactivate the licence on this server? Studio goes back to the trial (or locks if the trial is over) until you activate again.")) return;
    renderLicence(await api("/licence/deactivate", "POST", {})); toast("Deactivated. You can now activate the key on your new server.");
  }));

  /* -------------------------------------------------------- dashboard */
  async function loadDashboard() {
    const d = await api("/status").catch(e => { toast(e.message, true); return null; });
    if (!d) return;
    const np = d.nowplaying || {}, n = np.now || {};
    const src = $("[data-d-source]");
    src.textContent = d.liquidsoap ? (np.source || "autodj") : "offline";
    src.className = "pill" + (np.is_live ? " live" : d.liquidsoap ? "" : " off");
    $("[data-d-title]").textContent = n.title || (np.is_live ? "Live (no title sent)" : "–");
    $("[data-d-artist]").textContent = n.artist || "";
    const show = d.show_now ? `Show: ${d.show_now.name}` : "No show scheduled: default rotation";
    $("[data-d-show]").textContent = `${show} · Playlist: ${d.playlist_now ? d.playlist_now.name : "none"} · ${d.listeners.total} listeners`;
    const liq = d.liquidsoap;
    $("[data-live-toggle]").checked = !!d.live_enabled;
    $("[data-record]").value = d.record_mode || "off";
    $("[data-d-liq]").textContent = liq
      ? `Mixxx ${liq.live_connected ? "connected" : "not connected"} · OBS ${liq.obs_connected ? "connected" : "not connected"} · recording: ${liq.record} · takeover: ${liq.live_switch === "immediate" ? "immediate" : "after current song"}`
      : `Liquidsoap not reachable: ${d.liquidsoap_error || "starting?"}`;
    const waiting = $("[data-d-waiting]");
    waiting.hidden = !(liq && liq.live_waiting);
    $("[data-skip]").textContent = liq && liq.live_waiting ? "Go live now" : "Skip track";
    const lib = d.library, sync = d.sync || {};
    const stats = [["Library", lib.total], ["Cached", lib.cached], ["Missing", lib.missing],
      ["Unplayable", lib.bad], ["Emergency files", d.emergency ? d.emergency.files : "?"], ["Disk free", d.disk.free ? gb(d.disk.free) : "?"]];
    $("[data-d-stats]").replaceChildren(...stats.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, String(v ?? "–")))));
    const pct = Math.min(100, d.cached_bytes / d.cache_max_bytes * 100);
    $("[data-d-cachebar]").style.width = pct + "%";
    $("[data-d-cache]").textContent = `Cache ${gb(d.cached_bytes)} of ${gb(d.cache_max_bytes)} (plays from here when home is offline)`;
    const em = d.emergency || {};
    const emEl = $("[data-d-emergency]");
    if (em.unreadable) {
      emEl.hidden = false;
      emEl.textContent = `Emergency folder: ${em.unreadable} file(s) the radio can't read (e.g. ${em.unreadable_examples.join(", ")}). Fix on the VPS: chmod -R a+rX /opt/air-station/data/emergency`;
    } else if (!em.files) {
      emEl.hidden = false;
      emEl.textContent = "Emergency folder is empty: upload MP3s to /opt/air-station/data/emergency (sub-folders are fine).";
    } else emEl.hidden = true;
    const syncEl = $("[data-d-sync]");
    syncEl.replaceChildren(
      el("b", { class: sync.nas_ok ? "ok" : sync.at ? "bad" : "warn" }, sync.at ? (sync.nas_ok ? "NAS reachable" : "NAS unreachable") : "NAS sync has not run yet"),
      sync.at ? ` · last run ${when(sync.at)} · ${sync.message || ""} · last full scan ${when(sync.last_index_at)}` : " · see docs/DEPLOY.md step 8",
      d.sync_request && d.sync_request.index ? " · rescan requested" : "");
    const q = await api("/queue");
    $("[data-d-queue]").replaceChildren(...(q.queue.length ? q.queue.slice(0, 15).map(i => el("li", {},
      el("span", { class: "tag " + i.reason }, i.reason), el("span", { class: "tag " + (i.cached ? "cached" : "") }, i.cached ? "ready" : "fetching"),
      el("span", { class: "t" }, `${i.artist} – ${i.title}`), el("small", {}, mmss(i.duration)),
      el("button", { class: "x", title: "Remove", onclick: guard(async () => { await api(`/queue/${i.queue_id}`, "DELETE"); loadDashboard(); }) }, "×")))
      : [el("li", {}, "Queue is empty. It fills automatically once tracks are indexed.")]));
    const h = await api("/history?limit=15");
    $("[data-d-history]").replaceChildren(...h.history.map(r => el("li", {},
      el("span", { class: "tag " + r.source }, r.source), el("span", { class: "t" }, `${r.artist ? r.artist + " – " : ""}${r.title}`),
      el("small", {}, new Date(r.started_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })))));
  }
  $("[data-skip]").addEventListener("click", guard(async () => { const r = await api("/skip", "POST", {}); toast(String(r.reply || "").indexOf("Going live") >= 0 ? "Going live now." : "Skipped."); setTimeout(loadDashboard, 1500); }));
  $("[data-live-toggle]").addEventListener("change", guard(async e => {
    await api("/live", "POST", { enabled: e.target.checked });
    toast(e.target.checked ? "Live takeover allowed." : "Live blocked: AutoDJ stays on air.");
  }));
  $("[data-record]").addEventListener("change", guard(async e => { await api("/record", "POST", { mode: e.target.value }); toast("Recording mode: " + e.target.value); }));
  $("[data-rescan]").addEventListener("click", guard(async () => { const r = await api("/sync", "POST", {}); toast(r.message); }));

  /* ---------------------------------------------------------- library */
  async function ensurePlaylists() {
    S.playlists = (await api("/playlists")).playlists;
    const manual = S.playlists.filter(p => p.kind === "manual");
    $("[data-l-target]").replaceChildren(el("option", { value: "" }, "Add to manual playlist…"), ...manual.map(p => el("option", { value: p.id }, p.name)));
    const cur = $("[data-l-playlist]").value;
    $("[data-l-playlist]").replaceChildren(el("option", { value: "" }, "Any playlist"), ...manual.map(p => el("option", { value: p.id }, p.name)));
    $("[data-l-playlist]").value = cur;
  }
  async function loadLibrary() {
    await ensurePlaylists();
    const folderSel = $("[data-l-folder]");
    if (folderSel.options.length <= 1) {
      const f = await api("/folders");
      f.folders.filter(x => x.folder).forEach(x => folderSel.append(el("option", { value: x.folder }, `${x.folder} (${x.tracks})`)));
    }
    const params = new URLSearchParams({ q: $("[data-l-q]").value, folder: folderSel.value, status: $("[data-l-status]").value, playlist: $("[data-l-playlist]").value, page: S.page });
    const d = await api("/tracks?" + params);
    $("[data-l-total]").textContent = `${d.total} tracks`;
    const pages = Math.max(1, Math.ceil(d.total / 100));
    $("[data-l-page]").textContent = `Page ${d.page + 1} of ${pages}`;
    $("[data-l-prev]").disabled = d.page === 0; $("[data-l-next]").disabled = d.page + 1 >= pages;
    S.selected.clear(); $("[data-l-all]").checked = false;
    $("[data-l-rows]").replaceChildren(...d.tracks.map(t => {
      const status = [t.cached ? "cached" : "", t.missing ? "missing" : "", t.bad ? "unplayable" : ""].filter(Boolean).join(", ") || "on NAS";
      const cb = el("input", { type: "checkbox", onchange: e => e.target.checked ? S.selected.add(t.id) : S.selected.delete(t.id) });
      const en = el("input", { type: "checkbox", title: "Enabled", onchange: guard(async e => { await api(`/tracks/${t.id}`, "PATCH", { enabled: e.target.checked }); e.target.closest("tr").classList.toggle("dim", !e.target.checked); }) });
      en.checked = !!t.enabled;
      return el("tr", { class: t.enabled ? "" : "dim" },
        el("td", {}, cb), el("td", { class: "w", title: t.relpath }, t.title || "(untitled)"), el("td", { class: "w" }, t.artist),
        el("td", {}, t.genre), el("td", {}, t.folder), el("td", {}, mmss(t.duration)), el("td", {}, String(t.play_count)),
        el("td", {}, status), el("td", {}, el("label", { class: "switch" }, en, "on"),
          el("button", { class: "btn small", title: "Play next", onclick: guard(async () => { await api("/queue", "POST", { track_id: t.id }); toast("Queued to play next."); }) }, "Play next")));
    }));
  }
  let qTimer;
  $("[data-l-q]").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.page = 0; guard(loadLibrary)(); }, 300); });
  ["[data-l-folder]", "[data-l-status]", "[data-l-playlist]"].forEach(s => $(s).addEventListener("change", () => { S.page = 0; guard(loadLibrary)(); }));
  $("[data-l-prev]").addEventListener("click", () => { S.page--; guard(loadLibrary)(); });
  $("[data-l-next]").addEventListener("click", () => { S.page++; guard(loadLibrary)(); });
  $("[data-l-all]").addEventListener("change", e => $$("[data-l-rows] input[type=checkbox]:not([title])").forEach(c => { c.checked = e.target.checked; c.dispatchEvent(new Event("change")); }));
  async function bulk(action) {
    const pid = action === "add" ? $("[data-l-target]").value : $("[data-l-playlist]").value;
    if (!pid) throw new Error(action === "add" ? "Choose a manual playlist first (create one in Playlists)." : "Filter by a playlist first.");
    if (!S.selected.size) throw new Error("Tick some tracks first.");
    const r = await api(`/playlists/${pid}/tracks`, "POST", { action, track_ids: [...S.selected] });
    toast(`${action === "add" ? "Added" : "Removed"} ${r.count} tracks.`);
    loadLibrary();
  }
  $("[data-l-add]").addEventListener("click", guard(() => bulk("add")));
  $("[data-l-remove]").addEventListener("click", guard(() => bulk("remove")));

  /* -------------------------------------------------------- playlists */
  const KIND = { all: "Whole library", folders: "Folders", genres: "Genres", manual: "Hand-picked" };
  async function loadPlaylists() {
    await ensurePlaylists();
    $("[data-p-list]").replaceChildren(el("div", { class: "plist" }, ...S.playlists.map(p => el("div", { class: "pitem", onclick: () => editPlaylist(p) },
      el("div", {}, el("b", {}, p.name), " ", p.is_default ? el("span", { class: "pill" }, "default") : "", !p.enabled ? el("span", { class: "pill off" }, "off") : "",
        el("small", {}, `${KIND[p.kind]}${p.rule.length ? ": " + p.rule.join(", ") : ""}`)),
      el("small", {}, `${p.tracks} tracks · ${p.cached} cached`)))));
  }
  async function editPlaylist(p) {
    const f = $("[data-p-form]"); f.hidden = false; f.reset();
    f.id.value = p ? p.id : "";
    $("[data-p-heading]").textContent = p ? `Edit: ${p.name}` : "New playlist";
    f.name.value = p ? p.name : ""; f.kind.value = p ? p.kind : "folders";
    f.no_repeat_hours.value = p ? p.no_repeat_hours : 24; f.artist_gap.value = p ? p.artist_gap : 4;
    f.enabled.checked = p ? !!p.enabled : true; f.is_default.checked = p ? !!p.is_default : false;
    $("[data-p-delete]").hidden = !p || !!p.is_default;
    const [folders, genres] = await Promise.all([api("/folders"), api("/genres")]);
    const rule = new Set(p ? p.rule : []);
    $("[data-p-folderlist]").replaceChildren(...folders.folders.filter(x => x.folder).map(x => {
      const c = el("input", { type: "checkbox", value: x.folder }); c.checked = rule.has(x.folder);
      return el("label", {}, c, `${x.folder} (${x.tracks})`);
    }));
    const known = new Set();
    $("[data-p-genrelist]").replaceChildren(...genres.genres.map(g => {
      known.add(g.genre.toLowerCase());
      const c = el("input", { type: "checkbox", value: g.genre }); c.checked = [...rule].some(r => r.toLowerCase() === g.genre.toLowerCase());
      return el("label", {}, c, `${g.genre} (${g.tracks})`);
    }));
    f.extra_genres.value = p && p.kind === "genres" ? p.rule.filter(r => !known.has(r.toLowerCase())).join(", ") : "";
    kindChanged();
    f.scrollIntoView({ behavior: "smooth" });
  }
  function kindChanged() {
    const k = $("[data-p-form]").kind.value;
    $("[data-p-folders]").hidden = k !== "folders"; $("[data-p-genres]").hidden = k !== "genres"; $("[data-p-manualhint]").hidden = k !== "manual";
  }
  $("[data-p-form]").kind.addEventListener("change", kindChanged);
  $("[data-p-new]").addEventListener("click", guard(() => editPlaylist(null)));
  $("[data-p-cancel]").addEventListener("click", () => { $("[data-p-form]").hidden = true; });
  $("[data-p-form]").addEventListener("submit", guard(async e => {
    e.preventDefault();
    const f = e.target, kind = f.kind.value;
    let rule = [];
    if (kind === "folders") rule = $$("[data-p-folderlist] input:checked").map(c => c.value);
    if (kind === "genres") rule = [...$$("[data-p-genrelist] input:checked").map(c => c.value), ...f.extra_genres.value.split(",").map(s => s.trim()).filter(Boolean)];
    if ((kind === "folders" || kind === "genres") && !rule.length) throw new Error("Pick at least one " + (kind === "folders" ? "folder." : "genre."));
    const body = { name: f.name.value, kind, rule, no_repeat_hours: +f.no_repeat_hours.value, artist_gap: +f.artist_gap.value, enabled: f.enabled.checked, is_default: f.is_default.checked };
    if (f.id.value) await api(`/playlists/${f.id.value}`, "PUT", body); else await api("/playlists", "POST", body);
    toast("Playlist saved."); f.hidden = true; loadPlaylists();
  }));
  $("[data-p-delete]").addEventListener("click", guard(async () => {
    const f = $("[data-p-form]");
    if (!confirm(`Delete playlist "${f.name.value}"? Tracks stay in the library.`)) return;
    await api(`/playlists/${f.id.value}`, "DELETE"); f.hidden = true; toast("Deleted."); loadPlaylists();
  }));

  /* --------------------------------------------------------- schedule */
  async function loadSchedule() {
    await ensurePlaylists();
    const d = await api("/shows");
    S.shows = d.shows;
    $("[data-s-tz]").textContent = `Times are station time (${d.timezone}). Outside shows, the default playlist plays.`;
    $("[data-s-week]").replaceChildren(...DAYS.map((name, i) => {
      const shows = d.shows.filter(s => s.days.split(",").map(Number).includes(i)).sort((a, b) => a.start_time.localeCompare(b.start_time));
      return el("div", { class: "wday" }, el("h4", {}, name), ...(shows.length ? shows.map(s => {
        const pl = S.playlists.find(p => p.id === s.playlist_id);
        return el("div", { class: "slot" + (s.is_live ? " live" : "") + (s.enabled ? "" : " off"), onclick: () => editShow(s) },
          el("b", {}, `${s.start_time}–${s.end_time}`), el("span", {}, s.name), el("small", {}, pl ? `· ${pl.name}` : "· default rotation"));
      }) : [el("small", { class: "hint" }, "Default rotation all day")]));
    }));
    $("[data-s-days]").replaceChildren(...DAYS.map((n, i) => el("label", {}, el("input", { type: "checkbox", value: i }), n)));
    $("[data-s-playlists]").replaceChildren(el("option", { value: "" }, "Default rotation"), ...S.playlists.map(p => el("option", { value: p.id }, p.name)));
  }
  function editShow(s) {
    const f = $("[data-s-form]"); f.hidden = false; f.reset();
    $("[data-s-heading]").textContent = s ? `Edit: ${s.name}` : "New show";
    f.id.value = s ? s.id : "";
    for (const k of ["name", "host", "description", "start_time", "end_time"]) f[k].value = s ? s[k] : "";
    f.playlist_id.value = s && s.playlist_id ? s.playlist_id : "";
    f.is_live.checked = s ? !!s.is_live : false; f.enabled.checked = s ? !!s.enabled : true;
    const days = new Set(s ? s.days.split(",").map(Number) : [0, 1, 2, 3, 4, 5, 6]);
    $$("[data-s-days] input").forEach(c => c.checked = days.has(+c.value));
    $("[data-s-delete]").hidden = !s;
    f.scrollIntoView({ behavior: "smooth" });
  }
  $("[data-s-new]").addEventListener("click", () => editShow(null));
  $("[data-s-cancel]").addEventListener("click", () => { $("[data-s-form]").hidden = true; });
  $("[data-s-form]").addEventListener("submit", guard(async e => {
    e.preventDefault();
    const f = e.target;
    const body = { name: f.name.value, host: f.host.value, description: f.description.value, start_time: f.start_time.value, end_time: f.end_time.value,
      days: $$("[data-s-days] input:checked").map(c => +c.value), playlist_id: f.playlist_id.value || null, is_live: f.is_live.checked, enabled: f.enabled.checked };
    if (f.id.value) await api(`/shows/${f.id.value}`, "PUT", body); else await api("/shows", "POST", body);
    toast("Show saved."); f.hidden = true; loadSchedule();
  }));
  $("[data-s-delete]").addEventListener("click", guard(async () => {
    const f = $("[data-s-form]");
    if (!confirm(`Delete show "${f.name.value}"?`)) return;
    await api(`/shows/${f.id.value}`, "DELETE"); f.hidden = true; toast("Deleted."); loadSchedule();
  }));

  /* --------------------------------------------------------- settings */
  async function loadSettings() {
    const s = await api("/settings"), f = $("[data-settings-form]");
    if (s.station_name) $("[data-station-name]").textContent = s.station_name;
    const st = s._art_stats || {};
    $("[data-art-stats]").textContent = `${st.with_art || 0} of ${st.cached || 0} cached tracks have embedded cover art. Covers are read from each file's ID3/MP4/FLAC tags when the track is copied to the VPS.`
      + (st.missing_files ? ` ${st.missing_files} cover files are missing on the server; the sync re-extracts them automatically.` : "")
      + (st.default_missing ? " Your uploaded default cover file is missing, so the bundled one is shown. Upload it again in Appearance." : "");
    for (const input of f.elements) {
      if (!input.name) continue;
      const [a, b] = input.name.split(".");
      let v = b ? (s[a] || {})[b] : s[a];
      if (input.name === "community.posts") v = (v || []).map(p => [p.title, p.subtitle, p.meta].filter(x => x !== undefined).join(" | ")).join("\n");
      if (input.name === "genres_banner") v = (v || []).join(", ");
      if (input.type === "checkbox") input.checked = !!v; else input.value = v ?? "";
    }
    if (!$("[data-map-style]").value) $("[data-map-style]").value = "osm-dark";
    mapFields();
  }
  function mapFields() {
    const style = $("[data-map-style]").value;
    const group = style.startsWith("carto") ? "carto" : style === "custom" ? "custom" : "osm";
    $$("[data-map-field]").forEach(n => { n.hidden = n.dataset.mapField !== group; });
  }
  $("[data-map-style]").addEventListener("change", mapFields);
  $("[data-settings-form]").addEventListener("submit", guard(async e => {
    e.preventDefault();
    const f = e.target, body = { social: {}, community: {} };
    for (const input of f.elements) {
      if (!input.name) continue;
      const [a, b] = input.name.split(".");
      let v = input.type === "checkbox" ? input.checked : input.type === "number" ? +input.value : input.value.trim();
      if (input.name === "community.posts") v = v.split("\n").map(l => l.split("|").map(x => x.trim())).filter(p => p[0]).map(([title, subtitle, meta]) => ({ title, subtitle: subtitle || "", meta: meta || "" }));
      if (input.name === "genres_banner") v = v.split(",").map(x => x.trim()).filter(Boolean);
      if (b) body[a][b] = v; else body[a] = v;
    }
    await api("/settings", "PUT", body); toast("Settings saved. The public site updates within a minute.");
  }));

  /* ------------------------------------------------------- cover art */
  fetch("/api/site").then(r => r.json()).then(site => {
    if (site.station_name) $("[data-station-name]").textContent = site.station_name;
  }).catch(() => {});

  /* ------------------------------------------------------- appearance */
  const readAsDataURL = file => new Promise((ok, bad) => {
    const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = () => bad(new Error("Could not read the file.")); r.readAsDataURL(file);
  });

  function slotCard(slot) {
    const preview = el("div", { class: "slot-preview" + (slot.key === "hero_bg" || slot.key === "cover" || slot.key === "icon" ? " dark" : "") });
    if (slot.url) preview.append(el("img", { src: slot.url + (slot.url.includes("?") ? "" : "?t=" + Date.now()), alt: `Current ${slot.title.toLowerCase()}` }));
    else preview.append(el("span", { class: "slot-none" }, slot.key === "logo" ? "Icon + station name (no logo uploaded)" : "Station name in large letters (no banner uploaded)"));
    const file = el("input", { type: "file", accept: slot.accept });
    const upload = el("button", { class: "btn primary small", type: "button", onclick: guard(async () => {
      const f = file.files[0];
      if (!f) throw new Error("Choose an image first.");
      if (f.size > slot.max_mb * 1024 * 1024) throw new Error(`That file is over ${slot.max_mb} MB.`);
      upload.disabled = true;
      try {
        const r = await api(`/brand/${slot.key}`, "POST", { data: await readAsDataURL(f), filename: f.name });
        toast(r.warning || `${slot.title} updated${r.width ? ` (${r.width}×${r.height})` : ""}.`, !!r.warning);
        await loadAppearance();
      } finally { upload.disabled = false; }
    }) }, "Upload");
    const reset = el("button", { class: "btn small", type: "button", disabled: !slot.custom, onclick: guard(async () => {
      if (!confirm(`Remove your ${slot.title.toLowerCase()} and use the default?`)) return;
      await api(`/brand/${slot.key}`, "DELETE"); toast(`${slot.title}: default restored.`); await loadAppearance();
    }) }, "Restore default");
    return el("div", { class: "slot-card" },
      preview,
      el("div", { class: "slot-info" },
        el("h3", {}, slot.title, el("span", { class: "pill" + (slot.custom ? "" : " dim") }, slot.custom ? "Custom" : "Default")),
        el("p", {}, slot.where),
        el("dl", {},
          el("dt", {}, "Best size"), el("dd", {}, slot.size),
          el("dt", {}, "Formats"), el("dd", {}, slot.formats.join(", ")),
          el("dt", {}, "Minimum"), el("dd", {}, slot.min),
          el("dt", {}, "Max file"), el("dd", {}, `${slot.max_mb} MB`)),
        el("p", { class: "hint" }, slot.tip),
        el("div", { class: "row" }, file, upload, reset)));
  }

  async function loadAppearance() {
    const a = await api("/appearance");
    $("[data-a-slots]").replaceChildren(...a.images.map(slotCard));
    $("[data-a-texts]").replaceChildren(...a.texts.map(t => {
      const attrs = { name: t.key, placeholder: t.default, value: t.multiline ? null : t.value };
      const input = t.multiline ? el("textarea", { name: t.key, rows: Math.min(5, t.default.split("\n").length + 1), placeholder: t.default }) : el("input", attrs);
      if (t.multiline) input.value = t.value;
      return el("label", {}, t.label, input, t.hint ? el("small", { class: "hint", style: "margin:0" }, t.hint) : "");
    }));
    const tf = $("[data-a-theme-form]");
    tf.accent.value = a.theme.accent; tf.highlight.value = a.theme.highlight;
    tf.dataset.defaults = JSON.stringify(a.theme_defaults);
    const cf = $("[data-a-code-form]");
    cf.custom_css.value = a.custom_css; cf.custom_js.value = a.custom_js;
  }
  $("[data-a-texts-form]").addEventListener("submit", guard(async e => {
    e.preventDefault();
    const texts = {};
    for (const input of e.target.elements) if (input.name && input.value.trim()) texts[input.name] = input.value;
    await api("/appearance", "PUT", { texts }); toast("Texts saved."); await loadAppearance();
  }));
  $("[data-a-theme-form]").addEventListener("submit", guard(async e => {
    e.preventDefault();
    const f = e.target;
    await api("/appearance", "PUT", { theme: { accent: f.accent.value, highlight: f.highlight.value } });
    toast("Colours saved.");
  }));
  $("[data-a-theme-reset]").addEventListener("click", guard(async () => {
    await api("/appearance", "PUT", { theme: {} }); toast("Default colours restored."); await loadAppearance();
  }));
  $("[data-a-code-form]").addEventListener("submit", guard(async e => {
    e.preventDefault();
    const f = e.target;
    await api("/appearance", "PUT", { custom_css: f.custom_css.value, custom_js: f.custom_js.value });
    toast("Custom CSS and JavaScript saved. Reload the public site to see it.");
  }));
  $("[data-art-retry]").addEventListener("click", guard(async () => { const r = await api("/art-retry", "POST", {}); toast(r.message); }));

  /* ------------------------------------------------------- duplicates */
  const size = b => b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
  async function loadDuplicates() {
    const d = await api("/duplicates?resolved=" + ($("[data-dup-resolved]").checked ? "1" : "0"));
    $("[data-dup-summary]").textContent = d.total_groups
      ? `${d.total_groups} groups · ${d.extra_copies} extra copies${d.groups.length < d.total_groups ? ` (showing first ${d.groups.length})` : ""}`
      : "No duplicates found.";
    $("[data-dup-list]").replaceChildren(...d.groups.map(g => {
      const rows = g.tracks.map(t => {
        const keepBtn = el("button", { class: "btn small", type: "button", onclick: guard(async () => {
          const others = g.tracks.filter(x => x.id !== t.id).map(x => x.id);
          const r = await api("/duplicates/resolve", "POST", { keep: t.id, disable: others });
          toast(`Kept one copy, disabled ${r.disabled}.`); loadDuplicates();
        }) }, "Keep this");
        return el("tr", { class: (t.id === g.keep ? "keep " : "") + (t.enabled ? "" : "dim") },
          el("td", {}, t.id === g.keep ? el("span", { class: "tag cached", title: "Suggested: enabled, has cover, largest file" }, "suggested") : ""),
          el("td", { class: "w" }, `${t.artist} – ${t.title}`),
          el("td", { class: "path" }, t.relpath),
          el("td", {}, size(t.size)), el("td", {}, t.duration ? `${Math.floor(t.duration / 60)}:${String(Math.round(t.duration % 60)).padStart(2, "0")}` : "–"),
          el("td", {}, t.enabled ? (t.missing ? "missing" : "enabled") : "disabled"),
          el("td", {}, String(t.play_count)), el("td", {}, keepBtn));
      });
      return el("div", { class: "dup" },
        el("header", {}, el("span", { class: "tag " + g.reason }, g.label), el("b", {}, `${g.tracks.length} copies`)),
        el("div", { class: "tablewrap", style: "border:0;border-radius:0" }, el("table", {},
          el("thead", {}, el("tr", {}, ...["", "Track", "Location on the drive", "Size", "Len", "Status", "Plays", ""].map(x => el("th", {}, x)))),
          el("tbody", {}, ...rows))));
    }));
  }
  $("[data-dup-resolved]").addEventListener("change", guard(loadDuplicates));

  /* --------------------------------------------------------- listeners */
  const L = { range: "7d", map: null, data: null };
  const NS = "http://www.w3.org/2000/svg";
  const sv = (tag, attrs = {}) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const nfmt = v => {
    v = Number(v) || 0;
    if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + "M";
    if (v >= 1e4) return (v / 1e3).toFixed(v >= 1e5 ? 0 : 1) + "K";
    return v.toLocaleString();
  };
  function dur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    if (sec < 60) return `${sec} s`;
    if (sec < 3600) return `${Math.round(sec / 60)} min`;
    const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
    if (h >= 48) return `${nfmt(Math.round(sec / 3600))} h`;
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  const flag = cc => /^[A-Z]{2}$/.test(cc || "") ? String.fromCodePoint(...[...cc].map(c => 0x1F1A5 + c.charCodeAt(0))) : "🌐";
  const place = p => [p.city, p.region && p.region !== p.city ? p.region : "", p.country_name].filter(Boolean).join(", ") || "Unknown location";
  const MOUNT_NAMES = { "/radio.mp3": "Main (192 kbps)", "/radio-low.mp3": "Data saver (96 kbps)", "/radio.m3u8": "HLS / IPTV apps (AAC)" };
  const MOUNT_SHORT = { "/radio.mp3": "192 kbps", "/radio-low.mp3": "96 kbps", "/radio.m3u8": "HLS" };
  const RANGE_WORDS = { "24h": "in the last 24 hours", "7d": "in the last 7 days", "30d": "in the last 30 days", "90d": "in the last 90 days", "365d": "in the last 12 months" };

  function tzFormat(opts) {
    try { return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: L.data && L.data.timezone || undefined }); }
    catch { return new Intl.DateTimeFormat(undefined, opts); }
  }

  async function loadListeners() {
    const view = $("[data-view=listeners]");
    view.classList.add("loading");
    try {
      const d = await api(`/stats?range=${L.range}`);
      L.data = d;
      renderListeners(d);
      $("[data-st-updated]").textContent = "Updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } finally { view.classList.remove("loading"); }
  }
  $$("[data-st-range] button").forEach(b => b.addEventListener("click", guard(async () => {
    L.range = b.dataset.range;
    $$("[data-st-range] button").forEach(x => x.classList.toggle("on", x === b));
    $("[data-st-csv]").href = `/api/admin/stats/sessions.csv?range=${L.range}`;
    L.fitted = false;
    await loadListeners();
  })));

  function renderListeners(d) {
    const t = d.totals, words = RANGE_WORDS[d.range] || "";
    // notices
    const notes = [];
    if (!d.enabled) notes.push(["Statistics are switched off. Turn them on in ", el("b", {}, "Site settings"), "."]);
    if (d.map && d.map.note) notes.push([d.map.note]);
    if (d.geoip && !d.geoip.available) notes.push(["No location database yet, so the map is empty. On the server run ", el("code", {}, "bash scripts/airctl geoip update"), " (listeners are still counted)."]);
    if (d.enabled && !t.sessions && !t.now) notes.push([`No listening sessions ${words}. Counting starts when AirStation 2.4 is installed; sessions shorter than ${d.min_session} seconds are ignored.`]);
    const notice = $("[data-st-notice]");
    notice.hidden = !notes.length;
    notice.replaceChildren(...notes.flatMap((n, i) => [...(i ? [el("br")] : []), ...n]));
    // KPIs
    const peakWhen = t.peak_at ? tzFormat({ weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(t.peak_at * 1000)) : "";
    const kpi = (label, value, sub, cls = "") => el("dl", { class: "kpi " + cls }, el("dt", {}, label), el("dd", {}, value), el("small", {}, sub || " "));
    $("[data-st-kpis]").replaceChildren(
      kpi("Listening now", String(t.now), t.now === 1 ? "1 connection" : `${t.now} connections`, "now" + (t.now ? " on" : "")),
      kpi("Unique listeners", nfmt(t.listeners), words),
      kpi("Listening hours", nfmt(t.hours), `${nfmt(t.sessions)} sessions`),
      kpi("Average session", dur(t.avg_minutes * 60), t.longest_minutes ? `longest ${dur(t.longest_minutes * 60)}` : ""),
      kpi("Most at once", String(t.peak), peakWhen),
      kpi("Countries", String(d.countries.filter(c => c.country).length), d.countries[0] && d.countries[0].country ? `most: ${d.countries[0].country_name}` : ""),
      kpi("Website visitors", nfmt(d.web.visitors), `${nfmt(d.web.views)} page views`),
      kpi("Location data", d.geoip && d.geoip.available ? "On" : "Off", d.geoip && d.geoip.available ? `${d.geoip.file} · ${d.geoip.built || ""}` : "no database installed"),
    );
    $("[data-st-geo]").innerHTML = d.geoip ? d.geoip.attribution || "" : "";   // trusted, from the server
    renderMap(d);
    renderTimeline(d);
    renderCountries(d);
    renderLive(d);
    const sessionsRow = r => [r.sessions, `${dur(r.seconds)} · ${nfmt(r.sessions)} session${r.sessions === 1 ? "" : "s"}`];
    bars($("[data-st-lengths]"), d.lengths.map(r => ({ label: r.label, value: r.sessions, text: `${nfmt(r.sessions)}` })), "No sessions yet.");
    bars($("[data-st-players]"), d.players.map(r => ({ label: r.player || "Unknown", value: r.seconds, text: sessionsRow(r)[1] })), "No sessions yet.");
    bars($("[data-st-mounts]"), d.mounts.map(r => ({ label: MOUNT_NAMES[r.mount] || r.mount, value: r.seconds, text: sessionsRow(r)[1] })), "No sessions yet.");
    const w = d.web;
    $("[data-st-websum]").textContent = `${nfmt(w.visitors)} visitors · ${nfmt(w.views)} page views ${words}`;
    bars($("[data-st-pages]"), w.pages.map(r => ({ label: r.page, value: r.views, text: nfmt(r.views) })), "No visits yet.", true);
    bars($("[data-st-referrers]"), w.referrers.map(r => ({ label: r.referrer, value: r.views, text: nfmt(r.views) })), "No links from other sites yet.", true);
    bars($("[data-st-devices]"), w.devices.map(r => ({ label: r.device ? r.device[0].toUpperCase() + r.device.slice(1) : "Other", value: r.views, text: nfmt(r.views) })), "No visits yet.", true);
    bars($("[data-st-webcountries]"), w.countries.slice(0, 8).map(r => ({ label: `${flag(r.country)} ${r.country_name || "Unknown"}`, value: r.visitors, text: `${nfmt(r.visitors)} visitors` })), "No visits yet.", true);
  }

  function bars(box, rows, emptyText, web = false) {
    if (!rows.length) { box.replaceChildren(el("p", { class: "empty" }, emptyText)); return; }
    const max = Math.max(...rows.map(r => r.value || 0), 1);
    box.replaceChildren(el("div", { class: "blist" + (web ? " web" : "") }, ...rows.map(r =>
      el("div", { class: "brow", title: `${r.label}: ${r.text}` },
        el("span", { class: "l" }, r.label), el("span", { class: "v" }, r.text),
        el("span", { class: "track" }, el("i", { style: `width:${Math.max(0.5, (r.value || 0) / max * 100)}%` }))))));
  }

  function renderCountries(d) {
    const rows = d.countries;
    const max = Math.max(...rows.map(r => r.seconds || 0), 1);
    $("[data-st-countries]").replaceChildren(
      el("thead", {}, el("tr", {}, el("th", {}, "Country"), el("th", { class: "num" }, "Listeners"), el("th", { class: "num" }, "Sessions"), el("th", {}, "Listening time"))),
      el("tbody", {}, ...(rows.length ? rows.map(r => el("tr", {},
        el("td", {}, el("span", { class: "flag" }, flag(r.country)), r.country_name || "Unknown"),
        el("td", { class: "num" }, nfmt(r.listeners)), el("td", { class: "num" }, nfmt(r.sessions)),
        el("td", {}, el("span", { class: "share", style: `width:${Math.max(2, r.seconds / max * 70)}px` }), dur(r.seconds))))
        : [el("tr", {}, el("td", { colspan: 4, class: "empty" }, "No listeners in this period."))])));
  }

  function renderLive(d) {
    $("[data-st-livecount]").textContent = String(d.live.length);
    $("[data-st-livecount]").className = "pill" + (d.live.length ? " live" : " dim");
    $("[data-st-live]").replaceChildren(
      el("thead", {}, el("tr", {}, el("th", {}, "From"), el("th", {}, "Connected"), el("th", {}, "Player"), el("th", {}, "Stream"))),
      el("tbody", {}, ...(d.live.length ? d.live.map(r => el("tr", {},
        el("td", { title: place(r) }, el("span", { class: "flag" }, flag(r.country)), [r.city, r.country_name].filter(Boolean).join(", ") || place(r)),
        el("td", {}, dur(r.seconds)), el("td", {}, r.player || "Unknown"),
        el("td", {}, MOUNT_SHORT[r.mount] || r.mount)))
        : [el("tr", {}, el("td", { colspan: 4, class: "empty" }, "Nobody is listening right now."))])));
  }

  /* map */
  function renderMap(d) {
    const box = $("[data-st-map]");
    if (!window.WorldMap) { box.textContent = "The map could not be loaded."; return; }
    if (L.map) L.map.setTiles(d.map.tiles, d.map.attribution, d.map.darken);
    if (!L.map) {
      L.map = new WorldMap(box, { tiles: d.map.tiles, attribution: d.map.attribution, darken: d.map.darken });
      $$("[data-st-layer]").forEach(cb => {
        L.map.visible[cb.dataset.stLayer] = cb.checked;
        cb.addEventListener("change", () => { L.map.show(cb.dataset.stLayer, cb.checked); });
      });
    }
    const liveAt = new Map();
    for (const p of d.live) if (p.lat != null) {
      const k = `${p.lat},${p.lon}`;
      liveAt.set(k, (liveAt.get(k) || 0) + 1);
    }
    const lmax = Math.max(...d.points.map(p => p.seconds || 0), 1);
    const listeners = d.points.map(p => {
      const live = liveAt.get(`${p.lat},${p.lon}`) || 0;
      liveAt.delete(`${p.lat},${p.lon}`);
      return { lat: p.lat, lon: p.lon, live, r: 4 + Math.sqrt((p.seconds || 0) / lmax) * 16,
        lines: [place(p), `${dur(p.seconds)} listening`, `${nfmt(p.listeners)} listener${p.listeners === 1 ? "" : "s"} · ${nfmt(p.sessions)} session${p.sessions === 1 ? "" : "s"}`,
          ...(live ? [`${live} listening now`] : [])] };
    });
    for (const p of d.live) {                     // just connected, not in the period totals yet
      const k = `${p.lat},${p.lon}`;
      if (p.lat == null || !liveAt.has(k)) continue;
      listeners.push({ lat: p.lat, lon: p.lon, live: liveAt.get(k), r: 5, lines: [place(p), `${liveAt.get(k)} listening now`] });
      liveAt.delete(k);
    }
    const wmax = Math.max(...d.web.points.map(p => p.views || 0), 1);
    const web = d.web.points.map(p => ({ lat: p.lat, lon: p.lon, r: 4 + Math.sqrt(p.views / wmax) * 14,
      lines: [place(p), `${nfmt(p.views)} page view${p.views === 1 ? "" : "s"}`, `${nfmt(p.visitors)} visitor${p.visitors === 1 ? "" : "s"}`] }));
    L.map.setLayers({ listeners, web });
    if (!L.fitted) { L.map.fit(); L.fitted = true; }
  }

  /* timeline: most listeners at once, per hour / 6 hours / day / week */
  function niceMax(v) {
    if (v <= 4) return Math.max(1, Math.ceil(v));
    const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
    return (n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }
  function renderTimeline(d) {
    const box = $("[data-st-timeline]"), tl = d.timeline;
    const step = d.bucket;
    const labelFmt = step < 86400 ? (step === 3600 ? tzFormat({ hour: "2-digit", minute: "2-digit" }) : tzFormat({ weekday: "short", hour: "2-digit" }))
      : tzFormat({ day: "numeric", month: "short" });
    const longFmt = step < 86400 ? tzFormat({ weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
      : tzFormat({ weekday: "short", day: "numeric", month: "short", year: "numeric" });
    const per = { 3600: "per hour", 21600: "per 6 hours", 86400: "per day", 604800: "per week" }[step] || "";
    $("[data-st-tlnote]").textContent = `most listeners at once, ${per}`;
    const W = Math.max(320, box.clientWidth || 800), H = 220, padL = 34, padB = 22, padT = 8;
    const max = niceMax(Math.max(...tl.map(b => b.peak), 0));
    const plotW = W - padL - 4, plotH = H - padB - padT;
    const slot = plotW / Math.max(1, tl.length);
    const bw = Math.max(1, Math.min(24, slot - 2));
    const y = v => padT + plotH - v / max * plotH;
    const s = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Most listeners at once ${per}, ${RANGE_WORDS[d.range] || ""}` });
    s.style.height = H + "px";
    const tstep = max <= 5 ? 1 : max % 5 === 0 ? max / 5 : max % 4 === 0 ? max / 4 : max / 2;   // whole listeners only
    const ticks = Array.from({ length: Math.round(max / tstep) + 1 }, (_, i) => i * tstep);
    for (const v of ticks) {
      s.append(sv("line", { x1: padL, x2: W, y1: y(v), y2: y(v), class: v ? "gl" : "base" }));
      const tx = sv("text", { x: padL - 6, y: y(v) + 4, "text-anchor": "end" });
      tx.textContent = nfmt(v);
      s.append(tx);
    }
    const every = Math.ceil(tl.length / Math.max(2, Math.floor(plotW / 95)));
    const tip = el("div", { class: "ctip", hidden: true });
    tl.forEach((b, i) => {
      const cx = padL + slot * i + slot / 2;
      const g = sv("g", { class: "b", tabindex: i % every === 0 || b.peak ? "0" : "-1" });
      g.append(sv("rect", { x: cx - slot / 2, y: padT, width: slot, height: plotH, class: "hit" }));
      if (b.peak > 0) {
        const top = y(b.peak), x0 = cx - bw / 2, r = Math.min(4, bw / 2, (padT + plotH - top));
        const base = padT + plotH;
        g.append(sv("path", { class: "col", d: `M${x0},${base}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x0 + bw - r}Q${x0 + bw},${top} ${x0 + bw},${top + r}V${base}Z` }));
      }
      const showTip = () => {
        tip.replaceChildren(el("b", {}, `${b.peak} at once`), el("span", {}, `average ${b.avg}`), el("span", {}, longFmt.format(new Date(b.at * 1000))));
        tip.hidden = false;
        const rect = box.getBoundingClientRect(), scale = rect.width / W;
        const left = Math.min(rect.width - tip.offsetWidth - 4, Math.max(0, cx * scale - tip.offsetWidth / 2));
        tip.style.left = left + "px";
        tip.style.top = Math.max(0, y(b.peak) * scale - tip.offsetHeight - 8) + "px";
      };
      g.addEventListener("pointerenter", showTip);
      g.addEventListener("focus", showTip);
      g.addEventListener("pointerleave", () => { tip.hidden = true; });
      g.addEventListener("blur", () => { tip.hidden = true; });
      s.append(g);
      if (i % every === 0) {
        const tx = sv("text", { x: cx, y: H - 6, "text-anchor": i === 0 ? "start" : "middle" });
        if (i === 0) tx.setAttribute("x", padL);
        tx.textContent = labelFmt.format(new Date(b.at * 1000));
        s.append(tx);
      }
    });
    const rows = tl.filter(b => b.peak > 0);
    const table = el("details", { class: "tableview" }, el("summary", {}, "Show as a table"),
      el("div", { class: "tablewrap" }, el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "From"), el("th", { class: "num" }, "Most at once"), el("th", { class: "num" }, "Average"))),
        el("tbody", {}, ...(rows.length ? rows.map(b => el("tr", {}, el("td", {}, longFmt.format(new Date(b.at * 1000))),
          el("td", { class: "num" }, String(b.peak)), el("td", { class: "num" }, String(b.avg))))
          : [el("tr", {}, el("td", { colspan: 3, class: "empty" }, "No listeners in this period."))])))));
    const open = box.querySelector("details[open]");
    if (open) table.open = true;
    box.replaceChildren(s, tip, table);
  }
  let resizeT;
  window.addEventListener("resize", () => {
    clearTimeout(resizeT);
    resizeT = setTimeout(() => { if (S.view === "listeners" && L.data) renderTimeline(L.data); }, 200);
  });

  /* ----------------------------------------------------------- go live */
  function kvRow(k, v) {
    return [el("dt", {}, k), el("dd", {}, v), el("button", { class: "btn small", type: "button", onclick: () => navigator.clipboard.writeText(v).then(() => toast("Copied.")) }, "Copy")];
  }
  async function loadConnect() {
    const c = await api("/connections"), m = c.mixxx;
    $("[data-c-mixxx]").replaceChildren(...[["Type", m.type], ["Host", m.host], ["Port", String(m.port)], ["Mount", m.mount], ["Login", m.login], ["Password", m.password], ["Format", m.format]].flatMap(([k, v]) => kvRow(k, v)));
    $("[data-c-obs]").replaceChildren(...[["SRT server", c.obs_srt], ["SRT key", "(leave empty)"], ["RTMP server", c.obs_rtmp.server], ["RTMP key", c.obs_rtmp.key]].flatMap(([k, v]) => kvRow(k, v)));
    const s = c.streams;
    $("[data-c-streams]").replaceChildren(...[["MP3 192 kbps", s.mp3], ["MP3 96 kbps", s.mp3_low], ["HLS (AAC)", s.hls], ["Logo", s.logo]].flatMap(([k, v]) => kvRow(k, v)));
    $("[data-c-hls]").textContent = s.hls_running ? "HLS is running." : "HLS isn't running yet: on the server run  docker compose up -d hls";
    $("[data-c-hls]").className = "hint " + (s.hls_running ? "ok" : "warn");
    const name = s.station.replace(/[",]/g, "");
    $("[data-c-m3u]").value = `#EXTM3U\n#EXTINF:-1 tvg-id="${new URL(s.hls).hostname}" tvg-name="${name}" tvg-logo="${s.logo}" group-title="Radio" radio="true",${name}\n${s.hls}\n`;
  }

  /* -------------------------------------------------------------- init */
  api("/me").then(loggedIn).catch(showLogin);
})();
