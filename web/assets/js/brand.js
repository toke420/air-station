/* AirStation: applies the station's look (Admin > Appearance) to every public page.
 *
 * Texts:   elements with data-t="key" get the station's text for that key.
 *          {station} and {year} are filled in, *word* is highlighted, and a new
 *          line becomes a line break.
 * Names:   elements with data-station show the station name.
 * Images:  a custom logo/banner replaces the text version (data-logo, data-hero).
 * Custom:  /api/custom.css is linked in <head>; /api/custom.js is added here.
 *          Add ?nocustom to any address to load the page without either.
 * Other scripts use `await AirBrand.ready` to get /api/site without fetching twice.
 */
(() => {
  "use strict";
  const root = document.documentElement;
  const KEY = "airstation.site";
  const safeMode = /[?&]nocustom\b/.test(location.search);
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  const esc = s => String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  function render(text, site) {
    return esc(text || "")
      .replace(/\{station\}/g, esc(site.station_name || ""))
      .replace(/\{year\}/g, String(new Date().getFullYear()))
      .replace(/\*([^*\n]+)\*/g, "<b>$1</b>")
      .replace(/\n/g, "<br>");
  }

  function plain(text, site) {
    return String(text || "").replace(/\{station\}/g, site.station_name || "")
      .replace(/\{year\}/g, String(new Date().getFullYear())).replace(/\*/g, "");
  }

  function showImage(img, src, textEls) {
    const custom = !!src;
    if (img) {
      img.hidden = !custom;
      if (custom && img.getAttribute("src") !== src) img.src = src;
    }
    textEls.forEach(el => el.classList.toggle("sr-only", custom));
  }

  function apply(site) {
    if (!site) return;
    const texts = site.texts || {};
    const custom = new Set(site.custom_images || []);
    const images = site.images || {};
    document.querySelectorAll("[data-t]").forEach(el => {
      const value = texts[el.dataset.t];
      if (typeof value === "string" && value) el.innerHTML = render(value, site);
    });
    document.querySelectorAll("[data-station]").forEach(el => { el.textContent = site.station_name || ""; });
    if (site.tagline) document.querySelectorAll("[data-tagline]").forEach(el => { el.textContent = site.tagline; });
    const page = document.body.dataset.page;
    const seo = site.seo || {};
    document.title = page === "live" ? (seo.live_title || `Live Video | ${site.station_name || ""}`)
      : (seo.title || plain(texts.page_title, site));
    const desc = document.querySelector('meta[name="description"]');
    if (desc && seo.description) desc.content = seo.description;
    document.querySelectorAll("[data-logo]").forEach(img =>
      showImage(img, custom.has("logo") ? images.logo : "", [img.parentElement.querySelector("[data-brand-text]")].filter(Boolean)));
    document.querySelectorAll("[data-hero]").forEach(img =>
      showImage(img, custom.has("hero") ? images.hero : "", [...img.parentElement.querySelectorAll("[data-hero-name]")]));
    root.classList.remove("brand-pending");
  }

  function loadCustomJs(site) {
    if (safeMode || !site || !site.custom_js) return;
    const s = document.createElement("script");
    s.src = "/api/custom.js";
    s.defer = true;
    document.body.append(s);
  }

  // One cookieless page-view beacon (website visits in Studio > Listeners).
  try {
    const body = JSON.stringify({ p: location.pathname, r: document.referrer || "" });
    if (navigator.sendBeacon) navigator.sendBeacon("/api/hit", new Blob([body], { type: "text/plain" }));
    else fetch("/api/hit", { method: "POST", body, keepalive: true, headers: { "Content-Type": "text/plain" } });
  } catch { /* statistics are optional */ }

  if (safeMode) {
    document.querySelectorAll("link[data-custom-css]").forEach(l => { l.disabled = true; });
    console.info("AirStation: custom CSS/JS skipped (?nocustom).");
  }

  // Last known look first (no flash of default texts), then the live one.
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem(KEY) || "null"); } catch { cached = null; }
  if (cached) apply(cached);

  fetch("/api/site", { headers: { Accept: "application/json" } })
    .then(r => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
    .then(site => {
      apply(site);
      try { localStorage.setItem(KEY, JSON.stringify(site)); } catch { /* private mode */ }
      loadCustomJs(site);
      resolveReady(site);
    })
    .catch(() => {
      root.classList.remove("brand-pending");
      resolveReady(cached);
    });

  window.AirBrand = { ready, apply, render };
})();
