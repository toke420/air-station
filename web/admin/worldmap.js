/* AirStation Studio: a small dependency-free world map for the Listeners tab.
 *
 * Web Mercator tiles (OpenStreetMap by default, darkened with a CSS filter; CARTO with a
 * free key, or any XYZ tile server, are chosen in Site settings > Listener map)
 * with an SVG layer of circles on top. Drag to pan, wheel / double-click / + - to zoom,
 * pinch on phones, arrow keys when focused. If the tiles cannot load, a latitude /
 * longitude grid stays visible underneath, so the circles still make sense.
 *
 *   const map = new WorldMap(el, { tiles, attribution });
 *   map.setLayers({ listeners: [{ lat, lon, r, live, lines: ["Berlin", "3 h"] }], web: [...] });
 *   map.show("web", false); map.fit();
 */
(() => {
  "use strict";
  const TILE = 256, MIN_Z = 1, MAX_Z = 12, SVGNS = "http://www.w3.org/2000/svg";
  const clampLat = lat => Math.max(-85.05, Math.min(85.05, lat));

  function project(lat, lon, z) {
    const size = TILE * 2 ** z, s = Math.sin(clampLat(lat) * Math.PI / 180);
    return [(lon + 180) / 360 * size, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * size];
  }
  function unproject(x, y, z) {
    const size = TILE * 2 ** z, n = Math.PI - 2 * Math.PI * y / size;
    return [180 / Math.PI * Math.atan(Math.sinh(n)), x / size * 360 - 180];
  }
  const svg = (tag, attrs) => {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };

  class WorldMap {
    constructor(el, opts = {}) {
      this.el = el;
      this.tiles = opts.tiles || "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png";
      this.layers = {};
      this.visible = { listeners: true, web: false };
      this.z = 2; this.cx = 0; this.cy = 0;
      this.tileEls = new Map();
      this.failed = 0; this.loaded = 0;
      el.classList.add("wmap");
      el.classList.toggle("wmap-darken", !!opts.darken);
      el.tabIndex = 0;
      el.setAttribute("role", "application");
      el.setAttribute("aria-label", "World map. Arrow keys move, plus and minus zoom.");
      this.grid = svg("svg", { class: "wmap-grid", "aria-hidden": "true" });
      this.tileLayer = document.createElement("div");
      this.tileLayer.className = "wmap-tiles";
      this.overlay = svg("svg", { class: "wmap-overlay" });
      this.tip = document.createElement("div");
      this.tip.className = "wmap-tip"; this.tip.hidden = true;
      const zoom = document.createElement("div");
      zoom.className = "wmap-zoom";
      for (const [label, d, title] of [["+", 1, "Zoom in"], ["−", -1, "Zoom out"], ["⤢", 0, "Fit all"]]) {
        const b = document.createElement("button");
        b.type = "button"; b.textContent = label; b.title = title; b.setAttribute("aria-label", title);
        b.addEventListener("click", e => { e.stopPropagation(); d ? this.zoomBy(d) : this.fit(); });
        zoom.append(b);
      }
      this.attr = document.createElement("div");
      this.attr.className = "wmap-attr";
      this.attr.innerHTML = opts.attribution || "";      // trusted: from the station's own config
      el.replaceChildren(this.grid, this.tileLayer, this.overlay, zoom, this.attr, this.tip);
      this._events();
      this.resize();
      new ResizeObserver(() => { if (el.clientWidth) this.resize(); }).observe(el);   // not while hidden
    }

    /* ------------------------------------------------------------ view */
    get w() { return this.el.clientWidth || 600; }
    get h() { return this.el.clientHeight || 360; }
    minZoom() { return Math.max(MIN_Z, Math.ceil(Math.log2(Math.max(this.h, this.w / 2) / TILE))); }

    resize() {
      if (!this._sized) { this._sized = true; this.setView(20, 0, this.minZoom()); return; }
      this.setView(...unproject(this.cx, this.cy, this.z), this.z);
    }

    setView(lat, lon, z) {
      this.z = Math.max(this.minZoom(), Math.min(MAX_Z, Math.round(z)));
      [this.cx, this.cy] = project(lat, lon, this.z);
      this._clampY();
      this.render();
    }

    _clampY() {
      const size = TILE * 2 ** this.z, half = this.h / 2;
      this.cy = size <= this.h ? size / 2 : Math.max(half, Math.min(size - half, this.cy));
      this.cx = ((this.cx % size) + size) % size;
    }

    zoomBy(d, px = this.w / 2, py = this.h / 2) {
      const z = Math.max(this.minZoom(), Math.min(MAX_Z, this.z + d));
      if (z === this.z) return;
      // keep the point under the pointer where it is
      const wx = this.cx - this.w / 2 + px, wy = this.cy - this.h / 2 + py, f = 2 ** (z - this.z);
      this.z = z;
      this.cx = wx * f - px + this.w / 2;
      this.cy = wy * f - py + this.h / 2;
      this._clampY();
      this.render();
    }

    fit() {
      const pts = Object.entries(this.layers).filter(([k]) => this.visible[k]).flatMap(([, v]) => v);
      if (!pts.length) { this.setView(20, 0, this.minZoom()); return; }
      if (pts.length === 1) { this.setView(pts[0].lat, pts[0].lon, 5); return; }
      // longitude span the short way round (a Sydney + Honolulu audience crosses 180°)
      const lons = pts.map(p => p.lon).sort((a, b) => a - b);
      let gap = 360 - (lons[lons.length - 1] - lons[0]), west = lons[0], east = lons[lons.length - 1];
      for (let i = 1; i < lons.length; i++) {
        if (lons[i] - lons[i - 1] > gap) { gap = lons[i] - lons[i - 1]; west = lons[i]; east = lons[i - 1] + 360; }
      }
      const lats = pts.map(p => clampLat(p.lat));
      const north = Math.max(...lats), south = Math.min(...lats);
      let z = MAX_Z;
      for (; z > this.minZoom(); z--) {
        const [x1, y1] = project(north, west, z), [x2, y2] = project(south, east, z);
        if (x2 - x1 < this.w - 80 && y2 - y1 < this.h - 80) break;
      }
      z = Math.min(z, 6);
      const [x1, y1] = project(north, west, z), [x2, y2] = project(south, east, z);
      this.z = Math.max(this.minZoom(), z);
      const f = 2 ** (this.z - z);
      this.cx = (x1 + x2) / 2 * f; this.cy = (y1 + y2) / 2 * f;
      this._clampY();
      this.render();
    }

    /** Switch tile server (Site settings changed). */
    setTiles(tiles, attribution, darken) {
      this.el.classList.toggle("wmap-darken", !!darken);
      if (tiles === this.tiles) return;
      this.tiles = tiles;
      this.attr.innerHTML = attribution || "";
      for (const img of this.tileEls.values()) img.remove();
      this.tileEls.clear();
      this.failed = 0; this.loaded = 0;
      this.render();
    }

    /* ------------------------------------------------------------ data */
    setLayers(layers) { this.layers = layers; this.render(); }
    show(name, on) { this.visible[name] = on; this.render(); }

    /* ---------------------------------------------------------- render */
    render() {
      const { w, h, z } = this, n = 2 ** z, size = TILE * n;
      const left = this.cx - w / 2, top = this.cy - h / 2;
      this._grid(left, top, size);
      // tiles
      const want = new Set();
      const x0 = Math.floor(left / TILE), x1 = Math.floor((left + w) / TILE);
      const y0 = Math.max(0, Math.floor(top / TILE)), y1 = Math.min(n - 1, Math.floor((top + h) / TILE));
      for (let ty = y0; ty <= y1; ty++) {
        for (let tx = x0; tx <= x1; tx++) {
          const key = `${z}/${tx}/${ty}`;
          want.add(key);
          let img = this.tileEls.get(key);
          if (!img) {
            img = new Image();
            img.alt = ""; img.draggable = false; img.decoding = "async";
            img.referrerPolicy = "strict-origin-when-cross-origin";
            img.onload = () => { this.loaded++; img.classList.add("in"); this._tileState(); };
            img.onerror = () => { this.failed++; img.remove(); this._tileState(); };
            const wx = ((tx % n) + n) % n;
            img.src = this.tiles.replace("{s}", "abc"[(wx + ty) % 3]).replace("{z}", z).replace("{x}", wx)
              .replace("{y}", ty).replace("{r}", devicePixelRatio > 1 ? "@2x" : "");
            this.tileEls.set(key, img);
            this.tileLayer.append(img);
          }
          img.style.transform = `translate(${Math.round(tx * TILE - left)}px, ${Math.round(ty * TILE - top)}px)`;
        }
      }
      for (const [key, img] of this.tileEls) if (!want.has(key)) { img.remove(); this.tileEls.delete(key); }
      this._markers(left, top, size);
    }

    _tileState() {
      this.el.classList.toggle("no-tiles", this.failed > 0 && this.loaded === 0);
    }

    _grid(left, top, size) {
      const g = this.grid, z = this.z, kids = [];
      g.setAttribute("viewBox", `0 0 ${this.w} ${this.h}`);
      for (let lat = -60; lat <= 80; lat += 20) {
        const y = project(lat, 0, z)[1] - top;
        if (y >= 0 && y <= this.h) kids.push(svg("line", { x1: 0, x2: this.w, y1: y, y2: y, class: lat ? "" : "eq" }));
      }
      for (let lon = -180; lon < 180; lon += 30) {
        for (const k of [-1, 0, 1]) {
          const x = project(0, lon, z)[0] + k * size - left;
          if (x >= 0 && x <= this.w) kids.push(svg("line", { y1: 0, y2: this.h, x1: x, x2: x }));
        }
      }
      g.replaceChildren(...kids);
    }

    _markers(left, top, size) {
      const o = this.overlay, kids = [];
      o.setAttribute("viewBox", `0 0 ${this.w} ${this.h}`);
      for (const name of ["web", "listeners"]) {          // listeners drawn on top
        if (!this.visible[name]) continue;
        const pts = [...(this.layers[name] || [])].sort((a, b) => b.r - a.r);
        for (const p of pts) {
          const [px, py] = project(p.lat, p.lon, this.z);
          for (const k of [-1, 0, 1]) {
            const x = px + k * size - left, y = py - top;
            if (x < -40 || x > this.w + 40 || y < -40 || y > this.h + 40) continue;
            const g = svg("g", { class: `pt ${name}${p.live ? " live" : ""}`, tabindex: "0", role: "img",
              "aria-label": p.lines.join(", ") });
            if (p.live) g.append(svg("circle", { cx: x, cy: y, r: p.r + 6, class: "pulse" }));
            g.append(svg("circle", { cx: x, cy: y, r: Math.max(p.r, 12), class: "hit" }),
                     svg("circle", { cx: x, cy: y, r: p.r, class: "dot" }));
            g._lines = p.lines;
            kids.push(g);
          }
        }
      }
      o.replaceChildren(...kids);
    }

    /* ------------------------------------------------------------ tips */
    _showTip(g, x, y) {
      this.tip.replaceChildren(...g._lines.map((line, i) => {
        const d = document.createElement(i ? "span" : "b");
        d.textContent = line;
        return d;
      }));
      this.tip.hidden = false;
      const tw = this.tip.offsetWidth, th = this.tip.offsetHeight;
      this.tip.style.left = Math.max(6, Math.min(this.w - tw - 6, x + 14)) + "px";
      this.tip.style.top = Math.max(6, y - th - 12 < 6 ? y + 16 : y - th - 12) + "px";
    }

    /* ---------------------------------------------------------- events */
    _events() {
      const el = this.el, ptrs = new Map();
      let drag = null, pinch = null, moved = false;
      const local = e => { const r = el.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
      el.addEventListener("pointerdown", e => {
        if (e.target.closest(".wmap-zoom, .wmap-attr")) return;
        ptrs.set(e.pointerId, local(e));
        el.setPointerCapture(e.pointerId);
        moved = false;
        if (ptrs.size === 1) drag = { x: e.clientX, y: e.clientY, cx: this.cx, cy: this.cy };
        if (ptrs.size === 2) {
          const [a, b] = [...ptrs.values()];
          pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] };
          drag = null;
        }
      });
      el.addEventListener("pointermove", e => {
        if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, local(e));
        if (pinch && ptrs.size === 2) {
          const [a, b] = [...ptrs.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
          if (d / pinch.d > 1.5) { this.zoomBy(1, ...pinch.mid); pinch.d = d; }
          else if (d / pinch.d < 0.66) { this.zoomBy(-1, ...pinch.mid); pinch.d = d; }
          return;
        }
        if (drag) {
          const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
          if (Math.abs(dx) + Math.abs(dy) > 3) { moved = true; this.tip.hidden = true; el.classList.add("dragging"); }
          if (moved) {
            this.cx = drag.cx - dx; this.cy = drag.cy - dy; this._clampY();
            cancelAnimationFrame(this._raf); this._raf = requestAnimationFrame(() => this.render());
          }
          return;
        }
        const g = e.target.closest && e.target.closest(".pt");
        if (g && e.pointerType === "mouse") this._showTip(g, ...local(e));
        else if (e.pointerType === "mouse") this.tip.hidden = true;
      });
      const up = e => {
        ptrs.delete(e.pointerId);
        if (ptrs.size < 2) pinch = null;
        if (!ptrs.size) {
          el.classList.remove("dragging");
          if (drag && !moved) {                         // a tap or click
            const hit = document.elementFromPoint(e.clientX, e.clientY);
            const g = hit && hit.closest && hit.closest(".pt");
            if (g) this._showTip(g, ...local(e)); else this.tip.hidden = true;
          }
          drag = null;
        }
      };
      el.addEventListener("pointerup", up);
      el.addEventListener("pointercancel", up);
      el.addEventListener("pointerleave", e => { if (e.pointerType === "mouse" && !drag) this.tip.hidden = true; });
      el.addEventListener("wheel", e => {
        e.preventDefault();
        const now = Date.now();
        if (now - (this._wheelAt || 0) < 250) return;      // one zoom step per wheel gesture
        this._wheelAt = now;
        this.zoomBy(e.deltaY < 0 ? 1 : -1, ...local(e));
      }, { passive: false });
      el.addEventListener("dblclick", e => { if (!e.target.closest(".wmap-zoom")) this.zoomBy(e.shiftKey ? -1 : 1, ...local(e)); });
      el.addEventListener("keydown", e => {
        const step = 80, moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
        if (moves[e.key]) { this.cx += moves[e.key][0]; this.cy += moves[e.key][1]; this._clampY(); this.render(); }
        else if (e.key === "+" || e.key === "=") this.zoomBy(1);
        else if (e.key === "-") this.zoomBy(-1);
        else if (e.key === "Escape") this.tip.hidden = true;
        else return;
        e.preventDefault();
      });
      el.addEventListener("focusin", e => {
        const g = e.target.closest && e.target.closest(".pt");
        if (g) { const c = g.querySelector(".dot"); this._showTip(g, +c.getAttribute("cx"), +c.getAttribute("cy")); }
      });
    }
  }

  window.WorldMap = WorldMap;
})();
