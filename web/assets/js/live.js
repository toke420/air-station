/* AirStation Live Video page: Owncast embed + status. */
(() => {
  "use strict";
  const $ = s => document.querySelector(s);
  const $$ = s => [...document.querySelectorAll(s)];
  let loaded = false;
  let station = "";
  const liveTitle = () => station ? `${station} Live` : "Live video";

  async function getJSON(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }

  async function refresh() {
    let v;
    try { v = await getJSON("/api/video-status"); } catch { setTimeout(refresh, 30000); return; }
    if (!v.enabled || !v.url) {
      $("[data-v-badge]").textContent = "Unavailable";
      $("[data-v-title]").textContent = "Live video is switched off right now";
      $("[data-v-offline]").hidden = false;
      return;
    }
    if (!loaded) {
      // Muted start lets browsers autoplay; viewers unmute in the player.
      $("[data-v-video]").src = `${v.embed_video}?initiallyMuted=true`;
      $("[data-v-chat]").src = v.embed_chat;
      $("[data-v-open]").href = v.url;
      loaded = true;
    }
    const badge = $("[data-v-badge]");
    badge.textContent = v.online ? "Live now" : (v.reachable === false ? "Unreachable" : "Offline");
    badge.className = "badge " + (v.online ? "live" : "off");
    $("[data-v-title]").textContent = v.online ? (v.title || liveTitle()) : liveTitle();
    $("[data-v-viewers]").textContent = v.online && v.viewers ? `${v.viewers} watching` : "";
    $("[data-v-offline]").hidden = !!v.online;
    $$("[data-video-dot]").forEach(d => { d.hidden = !v.online; });
    setTimeout(refresh, v.online ? 30000 : 60000);
  }

  const site = window.AirBrand ? window.AirBrand.ready : getJSON("/api/site").catch(() => null);
  site.then(s => {
    if (!s) return;
    station = s.station_name || "";
    if (s.default_art) $("[data-v-art]").src = s.default_art;
  }).finally(refresh);
})();
