/* AirStation public site: player, now playing, schedule, requests. */
(() => {
  "use strict";
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const audio = $("[data-audio]");
  const state = { playing: false, np: null, quality: "hq", site: null, npTimer: null };
  const STREAMS = { hq: { url: "/radio.mp3", label: "192 kbps • MP3" }, lq: { url: "/radio-low.mp3", label: "96 kbps • Data saver" } };

  const store = {
    get(k, d) { try { const v = localStorage.getItem("air." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("air." + k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    let data = {};
    try { data = await res.json(); } catch { /* empty */ }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) node.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
    return node;
  }

  function toast(message, error = false) {
    const t = $("[data-toast]");
    t.textContent = message;
    t.classList.toggle("err", error);
    t.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("show"), 4200);
  }

  function ago(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min${m === 1 ? "" : "s"} ago`;
    const h = Math.round(m / 60);
    return `${h} hr${h === 1 ? "" : "s"} ago`;
  }

  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
  const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "short" });

  const BUNDLED_ART = "/assets/img/default-art.png";
  const stationName = () => (state.site && state.site.station_name) || "";
  const rotationName = () => (state.site && state.site.rotation_name) || "Non-stop music";
  const defaultArt = () => (state.site && state.site.default_art) || BUNDLED_ART;

  // Missing cover -> station default cover -> bundled cover. Every fallback is
  // shown whole ("default" = object-fit: contain), never cropped.
  function coverFallback(img) {
    img.addEventListener("error", () => {
      const step = Number(img.dataset.fallback || 0) + 1;
      img.dataset.fallback = step;
      const next = step === 1 && defaultArt() !== img.getAttribute("src") ? defaultArt()
        : img.getAttribute("src") !== BUNDLED_ART ? BUNDLED_ART : null;
      if (!next || step > 2) return;
      img.classList.remove("cover");
      img.classList.add("default");
      img.src = next;
    });
    return img;
  }

  function thumb(art) {
    return coverFallback(el("img", { class: "thumb" + (art ? "" : " default"), src: art || defaultArt(), alt: "", loading: "lazy" }));
  }

  function setCover(url) {
    const box = $("[data-art]");
    const want = url || defaultArt();
    const img = box.querySelector("img");
    if (img && img.dataset.want === want) return;
    const next = coverFallback(el("img", { class: url ? "cover" : "default", src: want, alt: "" }));
    next.dataset.want = want;
    box.replaceChildren(next);
  }

  /* ------------------------------------------------------------ player */
  const playBtn = $("[data-play]");
  const volume = $("[data-volume]");
  let ctx, analyser, sourceNode;

  function setVolume(v) {
    audio.volume = v / 100;
    volume.value = v;
    volume.style.setProperty("--v", v + "%");
    store.set("volume", v);
  }

  function setupAnalyser() {
    // Desktop only: routing a stream through Web Audio can stop background
    // playback on phones when the screen locks.
    if (analyser || !window.AudioContext || !matchMedia("(pointer: fine)").matches) return;
    try {
      ctx = new AudioContext();
      sourceNode = ctx.createMediaElementSource(audio);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 128;
      sourceNode.connect(analyser);
      analyser.connect(ctx.destination);
    } catch { analyser = null; }
  }

  async function play() {
    audio.src = STREAMS[state.quality].url + "?t=" + Date.now();
    setupAnalyser();
    try {
      if (ctx && ctx.state === "suspended") await ctx.resume();
      await audio.play();
      state.playing = true;
    } catch (e) {
      state.playing = false;
      toast("Couldn't start the stream. Tap play again.", true);
    }
    renderPlay();
  }

  function stop() {
    audio.pause();
    audio.removeAttribute("src");
    audio.load(); // drop the buffer so the next play is live, not delayed
    state.playing = false;
    renderPlay();
  }

  function renderPlay() {
    playBtn.setAttribute("aria-pressed", String(state.playing));
    playBtn.setAttribute("aria-label", state.playing ? "Stop live stream" : "Play live stream");
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = state.playing ? "playing" : "paused";
  }

  playBtn.addEventListener("click", () => (state.playing ? stop() : play()));
  $$("[data-listen]").forEach(b => b.addEventListener("click", () => {
    if (!state.playing) play();
    $("#on-air").scrollIntoView({ behavior: "smooth", block: "center" });
  }));
  volume.addEventListener("input", () => setVolume(+volume.value));
  audio.addEventListener("error", () => { if (state.playing) { state.playing = false; renderPlay(); toast("Stream interrupted. Press play to reconnect.", true); } });
  audio.addEventListener("pause", () => { if (state.playing && !audio.ended) { /* OS-level pause */ state.playing = false; renderPlay(); } });

  const qualityBtn = $("[data-quality]");
  function renderQuality() { qualityBtn.textContent = STREAMS[state.quality].label; }
  qualityBtn.addEventListener("click", () => {
    state.quality = state.quality === "hq" ? "lq" : "hq";
    store.set("quality", state.quality);
    renderQuality();
    if (state.playing) play();
  });

  if ("mediaSession" in navigator) {
    navigator.mediaSession.setActionHandler("play", play);
    navigator.mediaSession.setActionHandler("pause", stop);
    navigator.mediaSession.setActionHandler("stop", stop);
  }

  /* --------------------------------------------------------- visualiser */
  const viz = $("[data-viz]");
  const vctx = viz.getContext("2d");
  const bins = new Uint8Array(64);
  function drawViz(t) {
    const dpr = window.devicePixelRatio || 1;
    const w = viz.clientWidth, h = viz.clientHeight;
    if (viz.width !== w * dpr) { viz.width = w * dpr; viz.height = h * dpr; }
    vctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    vctx.clearRect(0, 0, w, h);
    const n = Math.max(24, Math.floor(w / 5));
    if (analyser && state.playing) analyser.getByteFrequencyData(bins);
    for (let i = 0; i < n; i++) {
      let v;
      if (analyser && state.playing) v = bins[Math.floor(i / n * 48)] / 255;
      else v = 0.12 + 0.1 * Math.abs(Math.sin(i * 0.55 + t / 900)) * (state.playing ? 3 : 1);
      const bh = Math.max(2, v * h);
      const g = vctx.createLinearGradient(0, h - bh, 0, h);
      g.addColorStop(0, "#6cc0ff"); g.addColorStop(1, "#0b6fd6");
      vctx.fillStyle = g;
      vctx.fillRect(i * 5, (h - bh) / 2, 2.5, bh);
    }
    requestAnimationFrame(drawViz);
  }
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) requestAnimationFrame(drawViz); else drawViz(0);

  function drawWaves() {
    $$("[data-wave]").forEach((c, k) => {
      const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
      if (!w) return;
      c.width = w * dpr; c.height = h * dpr;
      const x = c.getContext("2d"); x.scale(dpr, dpr);
      const n = Math.floor(w / 4);
      for (let i = 0; i < n; i++) {
        const p = k === 0 ? i / n : 1 - i / n;
        const v = Math.pow(p, 3) * (0.35 + 0.65 * Math.abs(Math.sin(i * 1.7)));
        const bh = Math.max(2, v * h);
        x.fillStyle = `rgba(31,155,255,${0.25 + 0.75 * p})`;
        x.fillRect(i * 4, (h - bh) / 2, 2, bh);
      }
    });
  }
  window.addEventListener("resize", drawWaves);

  /* -------------------------------------------------------- now playing */
  function renderNowPlaying(np) {
    state.np = np;
    const n = np.now || {};
    const live = np.is_live;
    const title = n.title || (live ? "Live on air" : np.online ? rotationName() : "Station offline");
    const artist = n.artist || (live ? (np.on_air && np.on_air.name) || stationName() : stationName());
    $("[data-np-title]").textContent = title;
    $("[data-np-artist]").textContent = artist;
    const badge = $("[data-np-badge]");
    badge.textContent = live ? "Live now" : "Now playing";
    badge.className = "badge" + (live ? " live" : "");
    const lb = $("[data-live-badge]");
    lb.textContent = np.online ? (live ? "LIVE DJ" : "LIVE") : "OFFLINE";
    lb.className = "badge " + (np.online ? "live" : "off");
    setCover(n.art);
    const oa = np.on_air || {};
    $("[data-onair-box]").classList.toggle("is-live", !!live || !!np.live_next);
    $("[data-onair-label]").textContent = live ? "LIVE ON AIR" : np.live_next ? "LIVE DJ UP NEXT" : "ON AIR";
    $("[data-onair-name]").textContent = oa.name || rotationName();
    $("[data-onair-desc]").textContent = live ? "Live from our studio" : (oa.description || "Non-stop rotation");
    $("[data-listeners]").textContent = np.listeners ? `${np.listeners} listening now` : "";
    // The tab title stays the station's own (Appearance > Texts > Browser tab title) so
    // search engines and bookmarks always see the same name; the song is shown on the
    // page and on the phone's lock screen (Media Session) instead.
    if ("mediaSession" in navigator && window.MediaMetadata) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title, artist, album: live ? `Live on ${stationName()}` : (n.album || stationName()),
        artwork: [{ src: n.art || defaultArt(), sizes: "512x512" }],
      });
    }
    const recent = $("[data-recent]");
    recent.replaceChildren(...(np.recent || []).slice(0, 5).map(r => el("li", {},
      thumb(r.art),
      el("div", { class: "t" }, el("b", {}, r.title || "Unknown"), el("span", {}, r.source === "live" || r.source === "obs" ? `${r.artist || "Live"} · live` : (r.artist || ""))),
      el("span", { class: "when" }, ago(r.started_at)))));
    if (!recent.children.length) recent.append(el("li", { class: "empty" }, "History appears once tracks have played."));
    renderProgress();
  }

  function renderProgress() {
    const np = state.np, bar = $("[data-progress]");
    if (!np || !np.now || !np.now.duration || np.is_live) { bar.style.width = "0"; return; }
    const elapsed = Date.now() / 1000 - np.started_at;
    bar.style.width = Math.min(100, elapsed / np.now.duration * 100) + "%";
  }
  setInterval(renderProgress, 1000);

  async function pollNowPlaying() {
    try { renderNowPlaying(await api("/api/nowplaying")); }
    catch { /* keep last state */ }
    clearTimeout(state.npTimer);
    state.npTimer = setTimeout(pollNowPlaying, document.hidden ? 30000 : 10000);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollNowPlaying(); });

  /* ----------------------------------------------------------- schedule */
  async function loadSchedule() {
    let data;
    try { data = await api("/api/schedule"); } catch { return; }
    const up = $("[data-upcoming]");
    const items = (data.upcoming || []).slice(0, 5);
    up.replaceChildren(...items.map(s => el("li", {},
      el("span", { class: "show-time" + (s.on_air ? " now" : "") }, s.on_air ? "ON AIR" : timeFmt.format(new Date(s.start * 1000))),
      el("div", { class: "t" }, el("b", {}, s.name), el("span", {}, [s.on_air ? "" : dayFmt.format(new Date(s.start * 1000)) + " · ", s.host ? `with ${s.host} · ` : "", s.description].join(""))))));
    if (!items.length) {
      const name = rotationName();
      const desc = (state.site && state.site.rotation_description) || "Non-stop music";
      up.append(el("li", {}, el("span", { class: "show-time now" }, "ON AIR"), el("div", { class: "t" }, el("b", {}, name), el("span", {}, desc))));
    }
    // Week view in the listener's time zone.
    const week = $("[data-week]");
    const days = [];
    const now = new Date();
    for (let d = 0; d < 7; d++) {
      const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() + d);
      days.push({ date, slots: [] });
    }
    const stationShows = data.shows || [];
    // Expand each show's weekly slots (station time) via the upcoming list for accuracy.
    const occurrences = await expandWeek(stationShows, data.timezone);
    for (const o of occurrences) {
      const local = new Date(o.start * 1000);
      const idx = days.findIndex(d => d.date.toDateString() === local.toDateString());
      if (idx >= 0) days[idx].slots.push(o);
    }
    week.replaceChildren(...days.map((d, i) => el("div", { class: "day" + (i === 0 ? " today" : "") },
      el("h3", {}, i === 0 ? "Today" : d.date.toLocaleDateString(undefined, { weekday: "long" })),
      ...(d.slots.length ? d.slots.sort((a, b) => a.start - b.start).map(s => el("div", { class: "slot" + (s.is_live ? " live" : "") },
        el("b", {}, s.name), el("span", {}, `${timeFmt.format(new Date(s.start * 1000))}–${timeFmt.format(new Date(s.end * 1000))}${s.host ? " · " + s.host : ""}`)))
        : [el("div", { class: "slot" }, el("b", {}, rotationName()), el("span", {}, "All day"))]))));
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    $("[data-tz]").textContent = `Times shown in your local time (${tz}). Station time: ${data.timezone}.`;
  }

  // Build occurrences for the next 7 days from weekly show definitions (station tz).
  async function expandWeek(shows, tz) {
    const out = [];
    const offsetFor = (date) => { // station tz offset in minutes at a given UTC instant
      const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).formatToParts(date);
      const g = t => +parts.find(p => p.type === t).value;
      const asUTC = Date.UTC(g("year"), g("month") - 1, g("day"), g("hour"), g("minute"));
      return (asUTC - date.getTime()) / 60000;
    };
    const today = new Date();
    for (let d = -1; d < 8; d++) {
      const probe = new Date(today.getTime() + d * 86400000);
      const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", weekday: "short" }).formatToParts(probe);
      const g = t => parts.find(p => p.type === t).value;
      const wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].indexOf(g("weekday"));
      for (const s of shows) {
        if (!String(s.days).split(",").map(Number).includes(wd)) continue;
        const [sh, sm] = s.start_time.split(":").map(Number);
        const [eh, em] = s.end_time.split(":").map(Number);
        const baseUTC = Date.UTC(+g("year"), +g("month") - 1, +g("day"), sh, sm);
        const off = offsetFor(new Date(baseUTC));
        const start = baseUTC - off * 60000;
        let dur = ((eh * 60 + em) - (sh * 60 + sm)) * 60000;
        if (dur <= 0) dur += 86400000;
        const end = start + dur;
        if (end < Date.now() || start > Date.now() + 7 * 86400000) continue;
        out.push({ ...s, start: start / 1000, end: end / 1000 });
      }
    }
    return out;
  }

  /* ----------------------------------------------------------- requests */
  function resultItem(t) {
    const btn = el("button", { class: "btn blue", type: "button" }, "Request");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const r = await api("/api/request", { method: "POST", body: JSON.stringify({ track_id: t.id }) });
        toast(r.message || "Requested!");
        btn.textContent = "Requested";
      } catch (e) { toast(e.message, true); btn.disabled = false; }
    });
    return el("li", {}, thumb(t.art), el("div", { class: "t" }, el("b", {}, t.title || "Untitled"), el("span", {}, [t.artist, t.album].filter(Boolean).join(" · "))), btn);
  }

  function bindSearch(input, list) {
    let timer, seq = 0;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = input.value.trim();
        const mine = ++seq;
        if (q.length < 2) { list.replaceChildren(); return; }
        try {
          const data = await api("/api/search?q=" + encodeURIComponent(q));
          if (mine !== seq) return;
          list.replaceChildren(...(data.results.length ? data.results.map(resultItem) : [el("li", { class: "empty" }, "No matches. Try another spelling.")]));
        } catch (e) { list.replaceChildren(el("li", { class: "empty" }, e.message)); }
      }, 250);
    });
  }
  bindSearch($("[data-search]"), $("[data-results]"));
  const dialog = $("[data-request-dialog]");
  bindSearch($("[data-dialog-search]"), $("[data-dialog-results]"));
  $$("[data-open-request]").forEach(b => b.addEventListener("click", () => {
    if (state.site && state.site.requests_enabled === false) { toast("Requests are closed right now."); return; }
    if (dialog.showModal) { dialog.showModal(); $("[data-dialog-search]").focus(); }
    else location.hash = "#music";
  }));
  $("[data-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });

  /* ---------------------------------------------------------- site data */
  const SOCIAL = { discord: ["DC", "Discord"], instagram: ["IG", "Instagram"], youtube: ["YT", "YouTube"], x: ["X", "X"], spotify: ["SP", "Spotify"] };

  function setLink(node, url, fallbackText) {
    if (url) { node.href = url; node.target = "_blank"; node.rel = "noopener"; }
    else { node.removeAttribute("target"); node.href = "#community"; node.addEventListener("click", e => { e.preventDefault(); toast(fallbackText); }); }
  }

  async function loadSite() {
    const s = window.AirBrand ? await window.AirBrand.ready : await api("/api/site").catch(() => null);
    if (!s) return;
    state.site = s;
    if (s.tagline) $("[data-tagline]").textContent = s.tagline;
    if (s.about) { $("[data-about]").textContent = s.about; $("[data-about-short]").textContent = s.about; }
    if (Array.isArray(s.genres_banner) && s.genres_banner.length) {
      const g = $("[data-genres]"); g.replaceChildren();
      s.genres_banner.forEach((x, i) => { if (i) g.append(el("br")); g.append(x); });
    }
    const socials = $("[data-socials]");
    socials.replaceChildren(...Object.entries(SOCIAL).filter(([k]) => s.social && s.social[k]).map(([k, [abbr, name]]) =>
      el("a", { href: s.social[k], target: "_blank", rel: "noopener", "aria-label": name, title: name }, abbr)));
    if (!socials.children.length) socials.append(el("span", { class: "empty", style: "padding:0" }, "Social links coming soon."));
    const c = s.community || {};
    const soon = "Community sign-up opens soon. Stay tuned!";
    setLink($("[data-signup]"), c.signup_url || c.forum_url, soon);
    setLink($("[data-login]"), c.login_url || c.forum_url, soon);
    setLink($("[data-video]"), c.video_chat_url, "Video chat opens soon.");
    if (!c.signup_url && !c.forum_url) $("[data-community-note]").textContent = "Community launching soon.";
    const posts = $("[data-posts]");
    const list = Array.isArray(c.posts) ? c.posts : [];
    posts.replaceChildren(...(list.length ? list.slice(0, 4).map(p => el("li", {},
      el("div", { class: "t" }, el("b", {}, p.title || ""), el("span", {}, p.subtitle || "")),
      el("span", { class: "when" }, p.meta || ""))) : [
        el("li", {}, el("div", { class: "t" }, el("b", {}, "New Music Friday"), el("span", {}, "What are you listening to?"))),
        el("li", {}, el("div", { class: "t" }, el("b", {}, "Local Artists"), el("span", {}, "Who should we be playing?"))),
        el("li", {}, el("div", { class: "t" }, el("b", {}, "Studio Gear Talk"), el("span", {}, "Share your setup and tips"))),
        el("li", {}, el("div", { class: "t" }, el("b", {}, "Show Ideas"), el("span", {}, "What would you like to hear on air?"))),
      ]));
    loadForumPosts();
    const lib = s.library || {};
    if (lib.tracks) {
      const fmt = n => n.toLocaleString();
      $("[data-stats]").replaceChildren(
        el("div", {}, el("b", {}, fmt(lib.tracks)), "tracks"),
        el("div", {}, el("b", {}, fmt(lib.artists)), "artists"),
        el("div", {}, el("b", {}, fmt(lib.genres)), "genres"));
    }
    if (state.np) renderNowPlaying(state.np);
    if (s.backup_stream_url) { $("[data-backup]").hidden = false; $("[data-backup-link]").href = s.backup_stream_url; }
  }

  /* ------------------------------------------- latest forum topics */
  async function loadForumPosts() {
    let data;
    try { data = await api("/api/community/latest"); } catch { return; }
    if (!data.available) return;
    $("[data-posts]").replaceChildren(...data.posts.slice(0, 4).map(p => el("li", {},
      el("div", { class: "t" },
        el("b", {}, el("a", { href: p.url, style: "color:inherit" }, p.title)),
        el("span", {}, [p.category, `${p.replies} ${p.replies === 1 ? "reply" : "replies"}`].filter(Boolean).join(" · "))),
      el("span", { class: "when" }, p.time ? ago(Date.parse(p.time) / 1000) : ""))));
  }

  /* ------------------------------------------------- live video badge */
  async function pollVideo() {
    try {
      const v = await api("/api/video-status");
      $$("[data-video-link]").forEach(a => { a.hidden = !v.enabled; });
      $$("[data-video-dot]").forEach(d => { d.hidden = !v.online; });
    } catch { /* optional feature */ }
    setTimeout(pollVideo, 60000);
  }

  /* -------------------------------------------------------- nav + init */
  const sections = ["top", "on-air", "schedule", "music", "about", "community"];
  const io = "IntersectionObserver" in window && new IntersectionObserver(entries => {
    for (const e of entries) if (e.isIntersecting) {
      $$(".nav a, .mobile-nav a").forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + e.target.id));
    }
  }, { rootMargin: "-45% 0px -50% 0px" });
  if (io) sections.forEach(id => { const n = document.getElementById(id === "top" ? "on-air" : id); if (n && id !== "top") io.observe(n); });

  state.quality = store.get("quality", "hq") === "lq" ? "lq" : "hq";
  renderQuality();
  setVolume(store.get("volume", 80));
  renderPlay();
  drawWaves();
  loadSite().then(loadSchedule);
  pollVideo();
  pollNowPlaying();
  setInterval(loadSchedule, 5 * 60000);
})();
