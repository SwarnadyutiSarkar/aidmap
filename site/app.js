/* AidMap front end. Static, no build step. All text from data is inserted with
   textContent (never innerHTML) so dataset contents cannot inject markup. */
(async function () {
  "use strict";
  const COLORS = { health: "#c0392b", water: "#1f78b4" };
  const FLAG_TEXT = {
    no_name: "no name recorded",
    no_type: "facility type not recorded",
    low_precision: "coordinates imprecise (≥100 m)",
    possible_duplicate: "possible duplicate entry",
  };
  const $ = (s) => document.querySelector(s);

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") n.className = v; else n.setAttribute(k, v);
    }
    for (const k of kids.flat()) if (k != null) n.append(k.nodeType ? k : document.createTextNode(String(k)));
    return n;
  }
  const fmtDate = (iso) => iso ? new Date(iso + (iso.length === 10 ? "T00:00:00Z" : "")).toLocaleDateString("en-GB",
    { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "unknown";
  const safeUrl = (u) => /^https:\/\//.test(u || "") ? u : "#";

  let meta, gj;
  try {
    meta = await (await fetch("data/metadata.json", { cache: "no-cache" })).json();
    gj = await (await fetch("data/aid_points.geojson?v=" + encodeURIComponent(meta.generated_at))).json();
  } catch (e) {
    $("#subtitle").textContent = "Could not load data. Please try again later.";
    return;
  }
  const feats = gj.features;
  $("#subtitle").textContent = `${meta.region.name} · ${feats.length.toLocaleString()} records · checked ${fmtDate(meta.generated_at)}`;

  /* ---------------- map ---------------- */
  const [a, b, c, d] = meta.region.bbox;
  const map = L.map("map", { preferCanvas: true }).fitBounds([[a, b], [c, d]]);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: "© OpenStreetMap contributors",
  }).addTo(map);
  const cluster = L.markerClusterGroup({ chunkedLoading: true, maxClusterRadius: 45 });
  map.addLayer(cluster);

  function popup(f) {
    const p = f.properties;
    const src = meta.sources.find((s) => s.id === p.src);
    const box = el("div", {},
      el("strong", {}, p.name), el("br"),
      `${p.kind} · ${p.category === "health" ? "health" : "water"}`, el("br"),
      "Status: ", p.status, el("br"));
    if (p.flags.length) box.append(el("div", { class: "flag" }, "⚠ " + p.flags.map((x) => FLAG_TEXT[x] || x).join("; ")));
    if (src) box.append(el("div", { class: "muted" }, `Source: ${src.title || src.id} (data up to ${fmtDate(src.coverage_end || src.last_modified)})`));
    return box;
  }
  const items = feats.map((f) => {
    const p = f.properties, [lon, lat] = f.geometry.coordinates;
    const m = L.circleMarker([lat, lon], {
      radius: 6, color: "#fff", weight: 1, fillColor: COLORS[p.category] || "#555",
      fillOpacity: p.quality === "good" ? 0.9 : 0.45, dashArray: p.flags.length ? "3" : null,
    });
    m.bindPopup(() => popup(f));
    return { f, m, lat, lon, name: p.name.toLowerCase() };
  });

  /* ---------------- filters ---------------- */
  const categories = [...new Set(feats.map((f) => f.properties.category))];
  const catBox = $("#cats");
  const catChecks = {};
  categories.forEach((cat) => {
    const n = feats.filter((f) => f.properties.category === cat).length;
    const src = meta.sources.find((s) => s.category === cat);
    const cb = el("input", { type: "checkbox", checked: "" });
    catChecks[cat] = cb;
    cb.addEventListener("change", apply);
    catBox.append(el("label", { class: "cat" }, cb,
      el("span", { class: "dot", style: `background:${COLORS[cat] || "#555"}` }),
      `${(src && src.label) || cat} (${n.toLocaleString()})`));
  });
  const kindSel = $("#kind");
  [...new Set(feats.map((f) => f.properties.kind))].sort().forEach((k) => kindSel.append(el("option", { value: k }, k)));
  ["kind", "search", "hideFlagged"].forEach((id) => $("#" + id).addEventListener("input", apply));

  function apply() {
    const q = $("#search").value.trim().toLowerCase();
    const kind = kindSel.value, hide = $("#hideFlagged").checked;
    const show = items.filter((i) => {
      const p = i.f.properties;
      return catChecks[p.category].checked && (!kind || p.kind === kind) &&
        (!hide || !p.flags.length) && (!q || i.name.includes(q));
    });
    cluster.clearLayers();
    cluster.addLayers(show.map((i) => i.m));
    $("#count").textContent = `Showing ${show.length.toLocaleString()} of ${items.length.toLocaleString()} records.`;
    if (q && show.length && show.length < 200) map.fitBounds(cluster.getBounds().pad(0.2), { maxZoom: 12 });
    drawGrid();
  }

  /* ---------------- density grid (information-gap hint) ---------------- */
  let gridLayer = L.layerGroup().addTo(map);
  function drawGrid() {
    gridLayer.clearLayers();
    if (!$("#grid").checked) return;
    const S = 0.25, cells = new Map();
    items.forEach((i) => {
      if (!catChecks[i.f.properties.category].checked) return;
      const key = Math.floor(i.lat / S) + ":" + Math.floor(i.lon / S);
      cells.set(key, (cells.get(key) || 0) + 1);
    });
    const max = Math.max(1, ...cells.values());
    cells.forEach((n, key) => {
      const [y, x] = key.split(":").map(Number);
      L.rectangle([[y * S, x * S], [(y + 1) * S, (x + 1) * S]], {
        interactive: true, weight: 0.5, color: "#444", fillColor: "#6a3d9a",
        fillOpacity: 0.12 + 0.5 * (n / max),
      }).bindTooltip(`${n} record${n > 1 ? "s" : ""} in this ~25 km cell`).addTo(gridLayer);
    });
  }
  $("#grid").addEventListener("change", drawGrid);

  /* ---------------- nearest services ---------------- */
  let picking = false, pin = null;
  const nearBtn = $("#nearBtn");
  nearBtn.addEventListener("click", () => {
    picking = !picking;
    nearBtn.classList.toggle("active", picking);
    nearBtn.textContent = picking ? "Click the map…" : "Pick a point on the map";
    map.getContainer().style.cursor = picking ? "crosshair" : "";
  });
  const rad = (x) => (x * Math.PI) / 180;
  function km(lat1, lon1, lat2, lon2) {
    const dLat = rad(lat2 - lat1), dLon = rad(lon2 - lon1);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(dLon / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.sqrt(h));
  }
  map.on("click", (e) => {
    if (!picking) return;
    picking = false; nearBtn.classList.remove("active"); nearBtn.textContent = "Pick another point";
    map.getContainer().style.cursor = "";
    if (pin) pin.remove();
    pin = L.marker(e.latlng).addTo(map);
    const out = $("#nearest"); out.replaceChildren();
    categories.forEach((cat) => {
      const list = items.filter((i) => i.f.properties.category === cat)
        .map((i) => ({ i, dist: km(e.latlng.lat, e.latlng.lng, i.lat, i.lon) }))
        .sort((x, y) => x.dist - y.dist).slice(0, 3);
      const src = meta.sources.find((s) => s.category === cat);
      const ol = el("ol");
      list.forEach(({ i, dist }) => {
        const li = el("li", {}, `${i.f.properties.name} · ${i.f.properties.kind} · ${dist.toFixed(1)} km`);
        li.addEventListener("click", () => { map.setView([i.lat, i.lon], 13); i.m.openPopup(); });
        ol.append(li);
      });
      out.append(el("strong", {}, (src && src.label) || cat), ol);
      if (list[0] && list[0].dist > 25) out.append(el("p", { class: "flag" },
        "⚠ Nearest record is over 25 km away. This may reflect missing data, not missing services."));
    });
    out.append(el("p", { class: "muted" }, "Straight-line distances only. Roads, security, seasonal access and opening hours are unknown."));
  });

  /* ---------------- banners, source cards, notes ---------------- */
  const banners = $("#banners");
  if (meta.is_sample) banners.append(el("div", { class: "banner bad" }, "SAMPLE DATA: synthetic points for testing. Not real services."));
  meta.sources.forEach((s) => {
    if (s.status === "failed") banners.append(el("div", { class: "banner bad" }, `${s.label}: could not be loaded. Missing from this map.`));
    if (s.status === "stale_fallback") banners.append(el("div", { class: "banner" }, `${s.label}: latest refresh failed; showing the previous download.`));
    if (s.age_days != null && s.age_days > 365) banners.append(el("div", { class: "banner bad" },
      `${s.label}: underlying data is ${Math.round(s.age_days / 365 * 10) / 10} years old. Verify before relying on it.`));
  });

  const srcBox = $("#sources");
  meta.sources.forEach((s) => {
    const age = s.age_days;
    const cls = age == null ? "old" : age <= 180 ? "fresh" : age <= 365 ? "aging" : "old";
    const txt = age == null ? "date unknown" : age <= 180 ? "recent" : age <= 365 ? "ageing" : "outdated";
    const cnt = s.counts || {};
    const hidden = [["dropped_outside_region", "outside region"], ["dropped_sensitive", "sensitive"],
      ["dropped_bad_coordinates", "bad coordinates"], ["dropped_zero_coordinates", "zero coordinates"]]
      .filter(([k]) => cnt[k]).map(([k, l]) => `${cnt[k]} ${l}`).join(", ");
    const flagged = (cnt.quality_check || 0) + (cnt.quality_poor || 0);
    const dl = el("dl", {},
      el("dt", {}, "Publisher"), el("dd", {}, s.organization || "unknown"),
      el("dt", {}, "Licence"), el("dd", {}, s.license || "unknown"),
      el("dt", {}, "Data up to"), el("dd", {}, fmtDate(s.coverage_end)),
      el("dt", {}, "Updated on HDX"), el("dd", {}, fmtDate(s.last_modified)),
      el("dt", {}, "Records"), el("dd", {}, `${(cnt.published || 0).toLocaleString()} shown of ${(cnt.raw || 0).toLocaleString()}`),
      el("dt", {}, "With warnings"), el("dd", {}, `${flagged.toLocaleString()}`),
      hidden ? el("dt", {}, "Not shown") : null, hidden ? el("dd", {}, hidden) : null);
    const card = el("div", { class: "card" },
      el("h3", {}, el("a", { href: safeUrl(s.url), target: "_blank", rel: "noopener" }, s.title || s.id), " ",
        el("span", { class: "badge " + cls }, txt)),
      dl);
    if (s.caveats) card.append(el("p", { class: "muted" }, "Publisher caveats: " + s.caveats));
    if (s.error) card.append(el("p", { class: "flag" }, "Error: " + s.error));
    if (s.warnings && s.warnings.length) card.append(el("ul", {}, s.warnings.map((w) => el("li", {}, w))));
    srcBox.append(card);
  });
  (meta.notes || []).forEach((n) => $("#notes").append(el("li", {}, n)));

  apply();
})();
