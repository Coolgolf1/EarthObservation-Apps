/* ============================================================
   EarthAnalyser — Multi-Dataset H3 Visualization
   Supports: fire, flood, vegetation, weather, land use
   Zoom-responsive, daily granularity, multi-layer rendering
   ============================================================ */

// ─── Config ──────────────────────────────────────────────────

const isLocal =
  (location.hostname === "127.0.0.1" || location.hostname === "localhost") &&
  location.port !== "8000";

const API_BASE = location.protocol === "file:" || isLocal ? "http://127.0.0.1:8000" : location.origin;

const MAPBOX_TOKEN =
  "pk.eyJ1IjoiYWx2YXJvcG94b29wbGUiLCJhIjoiY21xZXo3NjVyMDc2ejMycjFkeXZ6ZDRkYiJ9.uKcKNDOsSr3vgkz62RZNSw";

const DAY_MS = 86400000;

// Resolution slider: 0 = auto, 1-10 = manual override
let userResolutionOverride = 0;

// Dataset color themes
const DATASET_COLORS = {
  fire:       { base: "#e65100", fill: (r) => `rgba(227,26,28,${0.4+r*0.5})`,   stroke: "#b71c1c" },
  flood:      { base: "#1565c0", fill: (r) => `rgba(21,101,192,${0.3+r*0.5})`,  stroke: "#0d47a1" },
  vegetation: { base: "#2e7d32", fill: (r) => `rgba(46,125,50,${0.3+r*0.5})`,   stroke: "#1b5e20" },
  weather:    { base: "#e65100", fill: (r) => `rgba(230,81,0,${0.3+r*0.5})`,    stroke: "#bf360c" },
  landuse:    { base: "#6d4c41", fill: (r) => `rgba(109,76,65,${0.3+r*0.5})`,   stroke: "#3e2723" },
};

// Land use class colors (ESA WorldCover)
const LANDUSE_COLORS = {
  tree: "#006400", shrub: "#ffbb22", grass: "#ffff4c", crop: "#f096ff",
  built: "#fa0000", bare: "#b4b4b4", water: "#0064c8", other: "#888888",
};

// Auto H3 resolution from bbox span — one level coarser for performance
function bboxToH3Res(bbox) {
  const [west, south, east, north] = bbox;
  const span = Math.max(Math.abs(east - west), Math.abs(north - south));
  if (span < 0.02) return 9;
  if (span < 0.1) return 8;
  if (span < 0.3) return 7;
  if (span < 1.0) return 6;
  if (span < 3.0) return 5;
  if (span < 8.0) return 4;
  if (span < 20.0) return 3;
  if (span < 60.0) return 2;
  return 1;
}

// ─── Map ─────────────────────────────────────────────────────

const map = L.map("map", { zoomControl: true, worldCopyJump: true, preferCanvas: true }).setView([39.5, -5.5], 5);

const baseSatellite = L.tileLayer(
  "https://api.mapbox.com/styles/v1/{id}/tiles/{z}/{x}/{y}?access_token={accessToken}",
  { id: "mapbox/satellite-streets-v12", accessToken: MAPBOX_TOKEN, tileSize: 512, zoomOffset: -1, maxZoom: 20 },
).addTo(map);

const baseLight = L.tileLayer(
  "https://api.mapbox.com/styles/v1/{id}/tiles/{z}/{x}/{y}?access_token={accessToken}",
  { id: "mapbox/light-v11", accessToken: MAPBOX_TOKEN, tileSize: 512, zoomOffset: -1, maxZoom: 20 },
);

const drawingLayer = L.layerGroup().addTo(map);
const boundaryLayer = L.layerGroup().addTo(map); // country/region polygon outline

// One layer per dataset
const datasetLayers = {
  fire: L.layerGroup().addTo(map),
  flood: L.layerGroup().addTo(map),
  vegetation: L.layerGroup().addTo(map),
  weather: L.layerGroup().addTo(map),
  landuse: L.layerGroup().addTo(map),
};

// ─── State ───────────────────────────────────────────────────

let drawMode = false;
let drawPoints = [];
let resolvedBbox = null;
let activePolygon = null; // GeoJSON polygon for country/region clipping

// Multi-dataset state
let datasetResults = {};   // {fire: {frames, hexagons, ...}, flood: {...}, ...}
let timeline = [];
let currentFrameIdx = 0;
let playbackTimer = null;

// Zoom-responsive
let lastFetchKey = "";
let lastFetchBbox = null;    // bbox of last successful fetch
let lastFetchRes = null;     // resolution of last successful fetch
let currentH3Res = 4;
let queryDateRange = null;
let isFetching = false;
let refetchTimer = null;
let activeAbortController = null; // cancel in-flight requests on new fetch
let _fetchGeneration = 0; // monotonically increasing; detects stale fetches
let _agentActive = false; // true while agent is processing (blocks refetch)

// Client-side result cache (key → {datasets, timeline})
const resultCache = new Map();
const MAX_CACHE_ENTRIES = 30;

let usingSatellite = true;
let currentQuery = "";
let viewMode = "aggregate";

// ─── DOM ─────────────────────────────────────────────────────

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const statusBox = $("#status-box");
const queryForm = $("#query-form");
const queryInput = $("#query-input");
const startDate = $("#start-date");
const endDate = $("#end-date");
const frequency = $("#frequency");
const playSpeed = $("#play-speed");
const drawToggle = $("#draw-toggle");
const drawClear = $("#draw-clear");
const layerSatellite = $("#layer-satellite");
const timelinePlay = $("#timeline-play");
const timelinePause = $("#timeline-pause");
const timelineSlider = $("#timeline-slider");
const currentTimeLabel = $("#current-time-label");
const timelineSummary = $("#timeline-summary");
const timelineChart = $("#timeline-chart");
const exportCsv = $("#export-csv");
const askHint = $("#ask-hint");
const loadingOverlay = $("#loading-overlay");
const loadingText = $("#loading-text");
const progressBar = $("#progress-bar");
const chatLog = $("#chat-log");
const chatForm = $("#chat-form");
const chatInput = $("#chat-input");
const resolutionSlider = $("#resolution-slider");
const resolutionValue = $("#resolution-value");

// ─── Helpers ─────────────────────────────────────────────────

function autoExpand(el) {
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, parseInt(getComputedStyle(el).maxHeight) || 120) + "px";
}
for (const ta of $$("textarea")) { ta.addEventListener("input", () => autoExpand(ta)); autoExpand(ta); }

function updateStatus(msg) { if (statusBox) statusBox.textContent = msg; }
function setFetching(on) { if (statusBox) statusBox.classList.toggle("fetching", on); }
function showLoading(msg) { if (loadingText) loadingText.textContent = msg || "Loading..."; if (loadingOverlay) loadingOverlay.classList.remove("hidden"); }
function hideLoading() { if (loadingOverlay) loadingOverlay.classList.add("hidden"); }
function setProgress(p) { if (progressBar) progressBar.style.width = `${Math.max(0, Math.min(100, p))}%`; }

function parseDateRange() {
  if (!startDate || !endDate) return null;
  const s = new Date(`${startDate.value}T00:00:00Z`);
  const e = new Date(`${endDate.value}T00:00:00Z`);
  if (!Number.isFinite(s.getTime()) || !Number.isFinite(e.getTime())) return null;
  return s <= e ? { start: startDate.value, end: endDate.value } : { start: endDate.value, end: startDate.value };
}

function mapBoundsBbox() {
  const b = map.getBounds();
  // Shrink 10% inward from viewport edges — only query/plot what's clearly visible
  const lngSpan = b.getEast() - b.getWest();
  const latSpan = b.getNorth() - b.getSouth();
  const buf = 0.1;
  return [
    b.getWest() + lngSpan * buf,
    b.getSouth() + latSpan * buf,
    b.getEast() - lngSpan * buf,
    b.getNorth() - latSpan * buf,
  ];
}

function activeBbox() {
  return resolvedBbox && resolvedBbox.length === 4 ? [...resolvedBbox] : mapBoundsBbox();
}

function getCheckedDatasets() {
  const checked = [];
  for (const cb of $$("#dataset-grid input[type='checkbox']")) {
    if (cb.checked) checked.push(cb.value);
  }
  return checked.length ? checked : ["fire"];
}

function makeFetchKey(bbox, res, dateRange, datasets) {
  const bboxKey = bbox.map((v) => v.toFixed(2)).join(",");
  return `${bboxKey}|${res}|${dateRange?.start}|${dateRange?.end}|${datasets.sort().join(",")}`;
}

// Per-dataset cache key (bbox + res + dates + single dataset name)
function makeDatasetCacheKey(bbox, res, dateRange, dsName) {
  const bboxKey = bbox.map((v) => v.toFixed(2)).join(",");
  return `${bboxKey}|${res}|${dateRange?.start}|${dateRange?.end}|${dsName}`;
}

// Get the effective resolution: slider override or auto
function getEffectiveResolution(bbox) {
  if (userResolutionOverride > 0) return userResolutionOverride;
  return bboxToH3Res(bbox || activeBbox());
}

function cancelActiveRequest() {
  if (activeAbortController) {
    activeAbortController.abort();
    activeAbortController = null;
  }
  // Tell backend to cancel all in-flight tasks
  fetch(`${API_BASE}/datasets/cancel`, { method: "POST" }).catch(() => {});
}

// ─── Polygon Clipping (point-in-polygon) ─────────────────────

/** Ray-casting point-in-polygon for a single ring */
function _pointInRing(lat, lng, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i]; // GeoJSON: [lng, lat]
    const [xj, yj] = ring[j];
    if ((yi > lat) !== (yj > lat) && lng < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

/** Check if a point [lat, lng] is inside a GeoJSON Polygon or MultiPolygon */
function pointInPolygon(lat, lng, geojson) {
  if (!geojson) return true; // no polygon = no filtering
  if (geojson.type === "Polygon") {
    const rings = geojson.coordinates;
    if (!_pointInRing(lat, lng, rings[0])) return false;
    // Check holes
    for (let i = 1; i < rings.length; i++) {
      if (_pointInRing(lat, lng, rings[i])) return false;
    }
    return true;
  }
  if (geojson.type === "MultiPolygon") {
    for (const polygon of geojson.coordinates) {
      if (_pointInRing(lat, lng, polygon[0])) {
        let inHole = false;
        for (let i = 1; i < polygon.length; i++) {
          if (_pointInRing(lat, lng, polygon[i])) { inHole = true; break; }
        }
        if (!inHole) return true;
      }
    }
    return false;
  }
  return true;
}

/** Draw country/region boundary on map */
function drawBoundary(geojson) {
  boundaryLayer.clearLayers();
  if (!geojson) return;
  const geoLayer = L.geoJSON(geojson, {
    style: { color: "#007f5f", weight: 2, fillColor: "#007f5f", fillOpacity: 0.03, dashArray: "5,4" },
  });
  geoLayer.addTo(boundaryLayer);
}

function clearBoundary() {
  boundaryLayer.clearLayers();
  activePolygon = null;
}

// ─── Drawing ─────────────────────────────────────────────────

function convexHull(points) {
  if (points.length < 3) return points;
  const sorted = [...points].sort((a, b) => a.lng - b.lng || a.lat - b.lat);
  const cross = (o, a, b) => (a.lng - o.lng) * (b.lat - o.lat) - (a.lat - o.lat) * (b.lng - o.lng);
  const lower = [];
  for (const p of sorted) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop(); lower.push(p); }
  const upper = [];
  for (let i = sorted.length - 1; i >= 0; i--) { const p = sorted[i]; while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop(); upper.push(p); }
  upper.pop(); lower.pop();
  return lower.concat(upper);
}

function clearDrawing() { drawPoints = []; drawingLayer.clearLayers(); resolvedBbox = null; clearBoundary(); hideDrawConfirm(); }

function hideDrawConfirm() {
  const bar = $("#draw-confirm-bar");
  if (bar) bar.classList.add("hidden");
}

function showDrawConfirm() {
  const bar = $("#draw-confirm-bar");
  const text = $("#draw-confirm-text");
  if (bar) bar.classList.remove("hidden");
  if (text) text.textContent = `${drawPoints.length} points`;
}

function redrawDrawing() {
  drawingLayer.clearLayers();
  for (const p of drawPoints) L.circleMarker([p.lat, p.lng], { radius: 4, color: "#007f5f", fillColor: "#38d9a9", fillOpacity: 0.9, weight: 1.5 }).addTo(drawingLayer);
  if (drawPoints.length >= 3) {
    const hull = convexHull(drawPoints);
    L.polygon(hull.map((p) => [p.lat, p.lng]), { color: "#007f5f", weight: 2, fillColor: "#007f5f", fillOpacity: 0.08 }).addTo(drawingLayer);
  }
}

function confirmDrawing() {
  if (drawPoints.length < 3) return;
  const hull = convexHull(drawPoints);
  const lons = hull.map((p) => p.lng), lats = hull.map((p) => p.lat);
  resolvedBbox = [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
  drawMode = false;
  hideDrawConfirm();
  if (drawToggle) { drawToggle.classList.remove("active"); drawToggle.textContent = "Draw polygon"; }
  const [w, s, e, n] = resolvedBbox;
  map.fitBounds([[s, w], [n, e]], { padding: [30, 30] });
  // Update resolution to match drawn bbox size (unless slider is set)
  currentH3Res = getEffectiveResolution(resolvedBbox);
  updateStatus(`Area confirmed — select datasets and run analysis, or ask in chat`);
}

// ─── H3 Rendering (Multi-Dataset) ───────────────────────────

function hexTooltip(hex, dataset) {
  const lines = [`<strong>${dataset.toUpperCase()} · H3 res ${currentH3Res}</strong>`];
  lines.push(`Pixels: ${hex.pixel_count}`);
  if (hex.days_active) lines.push(`Days active: ${hex.days_active}`);

  if (dataset === "fire") {
    const c = hex.classes || {};
    lines.push(`High: ${c.high || 0} · Nom: ${c.nominal || 0} · Low: ${c.low || 0}`);
  } else if (dataset === "flood") {
    lines.push(`Water pixels: ${hex.pixel_count}`);
    if (hex.days_active) lines.push(`Days detected: ${hex.days_active}`);
  } else if (dataset === "vegetation") {
    const ndvi = hex.value_mean / 10000;
    lines.push(`NDVI: ${ndvi.toFixed(3)}`);
    lines.push(`Class: ${hex.dominant_class}`);
  } else if (dataset === "weather") {
    const tempC = (hex.value_mean * 0.02 - 273.15).toFixed(1);
    lines.push(`LST: ${tempC}°C`);
    lines.push(`Class: ${hex.dominant_class}`);
  } else if (dataset === "landuse") {
    lines.push(`Class: ${hex.dominant_class}`);
    const c = hex.classes || {};
    const entries = Object.entries(c).sort(([, a], [, b]) => b - a).slice(0, 3);
    lines.push(entries.map(([k, v]) => `${k}: ${v}`).join(" · "));
  }
  return lines.join("<br>");
}

function hexFillColor(hex, dataset, maxCount) {
  const ratio = Math.min(1, hex.pixel_count / Math.max(maxCount, 1));

  if (dataset === "fire") {
    const mc = hex.dominant_class;
    if (mc === "high") return `rgba(227,26,28,${0.5 + ratio * 0.45})`;
    if (mc === "nominal") return `rgba(255,140,0,${0.4 + ratio * 0.45})`;
    return `rgba(255,237,111,${0.4 + ratio * 0.4})`;
  }
  if (dataset === "flood") {
    const mc = hex.dominant_class;
    if (mc === "permanent") return `rgba(13,71,161,${0.5 + ratio * 0.4})`;
    if (mc === "seasonal") return `rgba(21,101,192,${0.4 + ratio * 0.4})`;
    return `rgba(100,181,246,${0.3 + ratio * 0.4})`;
  }
  if (dataset === "vegetation") {
    const ndvi = hex.value_mean / 10000;
    if (ndvi > 0.6) return `rgba(27,94,32,${0.5 + ratio * 0.4})`;
    if (ndvi > 0.3) return `rgba(46,125,50,${0.4 + ratio * 0.4})`;
    return `rgba(139,195,74,${0.3 + ratio * 0.4})`;
  }
  if (dataset === "weather") {
    const tempC = hex.value_mean * 0.02 - 273.15;
    if (tempC > 40) return `rgba(183,28,28,${0.5 + ratio * 0.4})`;
    if (tempC > 30) return `rgba(230,81,0,${0.4 + ratio * 0.4})`;
    if (tempC > 20) return `rgba(255,183,77,${0.3 + ratio * 0.4})`;
    return `rgba(100,181,246,${0.3 + ratio * 0.3})`;
  }
  if (dataset === "landuse") {
    return LANDUSE_COLORS[hex.dominant_class] || "#888888";
  }
  return DATASET_COLORS[dataset]?.fill(ratio) || `rgba(100,100,100,${0.3 + ratio * 0.4})`;
}

function renderDatasetHexagons(dataset, hexagons) {
  const layer = datasetLayers[dataset];
  if (!layer) return;
  layer.clearLayers();
  if (!hexagons || !hexagons.length) return;

  // Filter by active polygon (country/region boundary)
  const filtered = activePolygon
    ? hexagons.filter((h) => h.center && pointInPolygon(h.center[0], h.center[1], activePolygon))
    : hexagons;
  if (!filtered.length) return;

  const maxCount = Math.max(...filtered.map((h) => h.pixel_count));
  const colors = DATASET_COLORS[dataset] || DATASET_COLORS.fire;

  for (const hex of filtered) {
    if (!hex.boundary || !hex.boundary.length) continue;
    const latlngs = hex.boundary.map((c) => [c[0], c[1]]);
    const fill = hexFillColor(hex, dataset, maxCount);

    const polygon = L.polygon(latlngs, {
      color: colors.stroke,
      weight: 1,
      fillColor: fill,
      fillOpacity: dataset === "landuse" ? 0.6 : 0.7,
      className: "h3-hex",
    });

    polygon.bindTooltip(hexTooltip(hex, dataset), { sticky: true, className: "hex-tooltip" });
    polygon.addTo(layer);
  }
}

function renderAllDatasets() {
  const checked = getCheckedDatasets();
  for (const ds of Object.keys(datasetLayers)) {
    if (!checked.includes(ds) || !datasetResults[ds]) {
      datasetLayers[ds].clearLayers();
      continue;
    }
    const data = datasetResults[ds];
    const frames = data.frames || [];
    const isStatic = frames.length > 0 && frames.every(f => f.acq_date === "static");

    if (viewMode === "aggregate" || isStatic) {
      // Prefer pre-aggregated hexagons; fall back to merging frames if unavailable
      let hexes = data.hexagons;
      if ((!hexes || !hexes.length) && frames.length > 0) {
        const hexMap = new Map();
        for (const f of frames) {
          for (const h of (f.hexagons || [])) {
            hexMap.set(h.h3, h);
          }
        }
        hexes = [...hexMap.values()];
      }
      renderDatasetHexagons(ds, hexes || []);
    } else if (frames.length > 0) {
      // Timeline mode: show the frame matching the current date
      const currentDate = timeline[currentFrameIdx];
      if (currentDate) {
        // Carry-forward: show the most recent frame at or before the current date
        let best = null;
        for (const f of frames) {
          if (f.acq_date <= currentDate) {
            if (!best || f.acq_date > best.acq_date) best = f;
          }
        }
        // If no frame at-or-before, show first frame
        if (!best) best = frames[0];
        renderDatasetHexagons(ds, best.hexagons || []);
      } else {
        // Timeline not ready yet — show latest available frame rather than nothing
        const last = frames[frames.length - 1];
        renderDatasetHexagons(ds, last.hexagons || []);
      }
    } else if (data.hexagons && data.hexagons.length > 0) {
      // No frames but we have aggregate hexagons — render them directly
      renderDatasetHexagons(ds, data.hexagons);
    } else {
      datasetLayers[ds].clearLayers();
    }
  }
}

// ─── Timeline ────────────────────────────────────────────────

/** Full unfiltered timeline from backend (every day in range) */
let fullTimeline = [];

/** Apply frequency step to the full timeline */
function applyFrequencyStep(full) {
  const step = Math.max(1, parseInt(frequency?.value) || 1);
  if (step <= 1) return [...full];
  return full.filter((_, i) => i % step === 0);
}

function stopPlayback() { if (playbackTimer) { clearInterval(playbackTimer); playbackTimer = null; } }

/**
 * Compute a bar color for the timeline histogram based on the mean value of hexagons.
 * - fire: light orange (few pixels) → deep red (many pixels)
 * - weather: blue (cold) → orange → red (hot), based on mean surface temperature
 * - vegetation: brown (low NDVI) → green (high NDVI)
 * - flood: light blue (low occurrence) → deep blue (high occurrence)
 * - landuse: base brown
 */
function _timelineBarColor(ds, hexagons) {
  if (!hexagons || hexagons.length === 0) return DATASET_COLORS[ds]?.base || "#888";

  if (ds === "fire") {
    // Color by mean pixel count intensity: light peach → deep red
    const meanPx = hexagons.reduce((s, h) => s + (h.pixel_count || 0), 0) / hexagons.length;
    const t = Math.min(1, meanPx / 50); // normalize: 50px = max intensity
    const r = Math.round(255);
    const g = Math.round(200 - t * 180); // 200→20
    const b = Math.round(150 - t * 140); // 150→10
    return `rgb(${r},${g},${b})`;
  }

  if (ds === "weather") {
    // Color by mean temperature: blue (cold <10°C) → orange (warm) → red (hot >40°C)
    const meanRaw = hexagons.reduce((s, h) => s + (h.value_mean || 0), 0) / hexagons.length;
    const tempC = meanRaw * 0.02 - 273.15;
    // Map -10°C..45°C to 0..1
    const t = Math.max(0, Math.min(1, (tempC + 10) / 55));
    if (t < 0.5) {
      // cold: blue → yellow
      const s = t * 2; // 0..1
      const r = Math.round(30 + s * 225);
      const g = Math.round(100 + s * 155);
      const b = Math.round(220 - s * 190);
      return `rgb(${r},${g},${b})`;
    } else {
      // warm: yellow → red
      const s = (t - 0.5) * 2; // 0..1
      const r = Math.round(255);
      const g = Math.round(200 - s * 180);
      const b = Math.round(30 - s * 20);
      return `rgb(${r},${g},${b})`;
    }
  }

  if (ds === "vegetation") {
    // Color by mean NDVI: brown (0) → green (1)
    const meanRaw = hexagons.reduce((s, h) => s + (h.value_mean || 0), 0) / hexagons.length;
    const ndvi = Math.max(0, Math.min(1, meanRaw / 10000));
    const r = Math.round(160 - ndvi * 120); // 160→40
    const g = Math.round(100 + ndvi * 130); // 100→230
    const b = Math.round(40 + ndvi * 20);   // 40→60
    return `rgb(${r},${g},${b})`;
  }

  if (ds === "flood") {
    // Color by mean water pixel count: light sky blue (few) → deep blue (many)
    const meanPx = hexagons.reduce((s, h) => s + (h.pixel_count || 0), 0) / hexagons.length;
    const t = Math.min(1, meanPx / 30); // normalize: 30px = max intensity
    const r = Math.round(150 - t * 130);  // 150→20
    const g = Math.round(210 - t * 130);  // 210→80
    const b = Math.round(255 - t * 50);   // 255→205
    return `rgb(${r},${g},${b})`;
  }

  // landuse: use base color
  return DATASET_COLORS[ds]?.base || "#6d4c41";
}

function drawTimelineChart() {
  if (!timelineChart) return;
  const ctx = timelineChart.getContext("2d");
  if (!ctx) return;
  const w = timelineChart.clientWidth || 400;
  const h = timelineChart.clientHeight || 40;
  const dpr = devicePixelRatio || 1;
  timelineChart.width = Math.floor(w * dpr);
  timelineChart.height = Math.floor(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  if (!timeline || !timeline.length) {
    ctx.fillStyle = "#4d6074";
    ctx.font = "500 11px Manrope";
    ctx.fillText("No data", 8, 16);
    return;
  }

  const checked = getCheckedDatasets();
  const pad = 3;
  const iW = w - pad * 2;
  const iH = h - pad * 2;
  const barW = Math.max(1, iW / timeline.length);
  const bandH = Math.max(3, Math.floor(iH / Math.max(checked.length, 1)));

  // Draw a band per dataset
  checked.forEach((ds, bandIdx) => {
    const data = datasetResults[ds];
    if (!data) return;
    const frames = data.frames || [];
    const isStatic = frames.length > 0 && frames.every(f => f.acq_date === "static");
    const frameByDate = new Map(frames.map((f) => [f.acq_date, f]));

    const y0 = pad + bandIdx * bandH;

    // For static datasets, compute total hex count once (shown uniformly)
    if (isStatic) {
      const totalHex = frames.reduce((s, f) => s + (f.hexagons || []).length, 0);
      const allHexes = frames.flatMap(f => f.hexagons || []);
      const color = _timelineBarColor(ds, allHexes);
      const intensity = totalHex > 0 ? 0.6 + 0.4 * Math.min(1, totalHex / 200) : 0;
      for (let i = 0; i < timeline.length; i++) {
        if (totalHex > 0) {
          ctx.fillStyle = color;
          ctx.globalAlpha = intensity;
        } else {
          ctx.fillStyle = "#e0e0e0";
          ctx.globalAlpha = 0.08;
        }
        ctx.fillRect(Math.round(pad + i * barW), y0, Math.max(1, Math.floor(barW - 0.5)), bandH - 1);
      }
      ctx.globalAlpha = 1;
      return;
    }

    // Find max hexagon count across all frames for relative intensity scaling
    let maxHex = 0;
    for (const f of frames) {
      const c = (f.hexagons || []).length;
      if (c > maxHex) maxHex = c;
    }
    if (maxHex < 1) maxHex = 1;

    // Bin frames into timeline slots with mean values
    const slotCounts = new Array(timeline.length).fill(0);
    const slotHexes = new Array(timeline.length).fill(null); // store hexagons for color calc
    for (const f of frames) {
      const d = f.acq_date;
      if (!d || d === "static") continue;
      const hexes = f.hexagons || [];
      if (hexes.length === 0) continue;
      // Find the timeline slot this frame belongs to (binary search for nearest <=)
      let lo = 0, hi = timeline.length - 1, slot = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (timeline[mid] <= d) { slot = mid; lo = mid + 1; }
        else { hi = mid - 1; }
      }
      if (slot >= 0) {
        if (hexes.length > slotCounts[slot]) {
          slotCounts[slot] = hexes.length;
          slotHexes[slot] = hexes;
        }
      }
    }

    for (let i = 0; i < timeline.length; i++) {
      const hexCount = slotCounts[i];
      if (hexCount > 0) {
        // Intensity: 0.5 (few hexagons) to 1.0 (max hexagons)
        const intensity = 0.5 + 0.5 * (hexCount / maxHex);
        ctx.fillStyle = _timelineBarColor(ds, slotHexes[i]);
        ctx.globalAlpha = intensity;
      } else {
        ctx.fillStyle = "#e0e0e0";
        ctx.globalAlpha = 0.08;
      }
      ctx.fillRect(Math.round(pad + i * barW), y0, Math.max(1, Math.floor(barW - 0.5)), bandH - 1);
    }
    ctx.globalAlpha = 1;
  });

  // Current position marker
  if (viewMode === "timeline" && timeline.length > 0) {
    ctx.fillStyle = "#333";
    ctx.globalAlpha = 0.8;
    const x = Math.round(pad + (currentFrameIdx / Math.max(timeline.length - 1, 1)) * iW);
    ctx.fillRect(x, pad, 2, iH);
    ctx.globalAlpha = 1;
  }
}

function updateTimelineUI() {
  const totalDays = timeline.length;

  if (!totalDays) {
    if (timelineSlider) { timelineSlider.max = "0"; timelineSlider.value = "0"; }
    if (currentTimeLabel) currentTimeLabel.textContent = "—";
    if (timelineSummary) timelineSummary.textContent = "Select area & dates, then run analysis";
    drawTimelineChart();
    return;
  }

  if (timelineSlider) {
    timelineSlider.max = String(Math.max(0, totalDays - 1));
    timelineSlider.value = String(currentFrameIdx);
  }

  const checked = getCheckedDatasets();
  const totalHexes = checked.reduce((s, ds) => s + ((datasetResults[ds]?.hexagons || []).length), 0);

  if (viewMode === "aggregate") {
    // Show date range
    const dateLabel = totalDays === 1
      ? timeline[0]
      : `${timeline[0]} — ${timeline[totalDays - 1]}`;
    if (currentTimeLabel) currentTimeLabel.textContent = dateLabel;
    if (timelineSummary) timelineSummary.textContent = `${totalHexes} hexagons · ${checked.join(", ")} · H3 res ${currentH3Res}`;
  } else {
    // Show current day
    const currentDate = timeline[currentFrameIdx] || "—";
    if (currentTimeLabel) currentTimeLabel.textContent = currentDate;
    if (timelineSummary) timelineSummary.textContent = `Day ${currentFrameIdx + 1}/${totalDays} · H3 res ${currentH3Res}`;
  }
  drawTimelineChart();
}

function computeAnimationDelay() {
  if (!timeline || timeline.length < 2) return 1000;
  // Calculate time differences between consecutive timeline entries (in days)
  const diffs = [];
  for (let i = 1; i < timeline.length; i++) {
    const prev = new Date(timeline[i - 1]).getTime();
    const curr = new Date(timeline[i]).getTime();
    const diffDays = Math.round((curr - prev) / DAY_MS);
    diffs.push(diffDays);
  }
  // Check if all differences are the same (consistent)
  const firstDiff = diffs[0];
  const isConsistent = diffs.every(d => d === firstDiff);
  // If consistent, use that step (in seconds); otherwise use 1 second
  const stepSeconds = isConsistent ? firstDiff : 1;
  return Math.max(100, stepSeconds * 1000);
}

function startPlayback() {
  stopPlayback();
  if (!timeline.length) return;
  viewMode = "timeline";
  const speed = computeAnimationDelay();
  playbackTimer = setInterval(() => {
    currentFrameIdx = (currentFrameIdx + 1) % timeline.length;
    renderAllDatasets();
    updateTimelineUI();
  }, speed);
}

// ─── API (Streaming NDJSON) ──────────────────────────────────

/**
 * Streaming fetch: reads NDJSON from /datasets/h3/stream.
 * Calls onDataset(key, data) for each dataset as it arrives.
 * Returns the full result {datasets, timeline, bbox, resolution}.
 */
async function fetchDatasetsH3Stream(bbox, start, end, resolution, datasets, signal, onDataset) {
  const res = await fetch(`${API_BASE}/datasets/h3/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      bbox,
      date_start: start,
      date_end: end,
      resolution,
      datasets,
      max_frames: 365,
    }),
    signal,
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text.slice(0, 200)}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let header = null;
  const result = { datasets: {}, timeline: [] };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Process complete lines
    let newlineIdx;
    while ((newlineIdx = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newlineIdx).trim();
      buffer = buffer.slice(newlineIdx + 1);
      if (!line) continue;

      try {
        const msg = JSON.parse(line);
        if (msg.type === "header") {
          header = msg;
          result.timeline = msg.timeline || [];
          // Set global timeline immediately from header so UI shows full date range
          if (result.timeline.length > 0 && (result.timeline.length > fullTimeline.length || timeline.length === 0)) {
            fullTimeline = result.timeline;
            timeline = applyFrequencyStep(fullTimeline);
            viewMode = "timeline";
            if (currentFrameIdx >= timeline.length) currentFrameIdx = 0;
            updateTimelineUI();
          }
        } else if (msg.type === "frame") {
          // Per-day frame arrival — accumulate into dataset results
          const key = msg.key;
          if (!result.datasets[key]) {
            result.datasets[key] = {
              dataset: key,
              frames: [],
              hexagons: [],
              total_hexagons: 0,
              frames_with_data: 0,
            };
          }
          result.datasets[key].frames.push(msg.frame);
          result.datasets[key].frames_with_data++;
          console.log(`[FRAME] ${key}: frame ${result.datasets[key].frames_with_data}, ${msg.frame?.hexagons?.length ?? 0} hexagons`);
          // Notify callback with current accumulated data
          if (onDataset) onDataset(key, result.datasets[key]);
        } else if (msg.type === "dataset_done") {
          // Final aggregate data for this dataset
          console.log(`[DATA RECEIVED] ${msg.key}:`, msg.data?.total_hexagons ?? 0, "hexagons,", (result.datasets[msg.key]?.frames?.length ?? 0), "frames");
          if (result.datasets[msg.key]) {
            result.datasets[msg.key].hexagons = msg.data.hexagons || [];
            result.datasets[msg.key].total_hexagons = msg.data.total_hexagons || 0;
          } else {
            // No frames arrived before dataset_done — build a synthetic frame so rendering works
            const hexagons = msg.data.hexagons || [];
            result.datasets[msg.key] = {
              ...msg.data,
              frames: hexagons.length > 0 ? [{ acq_date: "static", hexagons }] : [],
              frames_with_data: hexagons.length > 0 ? 1 : 0,
            };
          }
          if (onDataset) onDataset(msg.key, result.datasets[msg.key]);
        } else if (msg.type === "dataset") {
          // Legacy: full dataset in one message
          result.datasets[msg.key] = msg.data;
          if (onDataset) onDataset(msg.key, msg.data);
        } else if (msg.type === "error") {
          throw new Error(msg.detail || "Stream error");
        }
        // "done" — just break naturally
      } catch (parseErr) {
        if (parseErr.message && !parseErr.message.includes("JSON")) throw parseErr;
      }
    }
  }

  return result;
}

// Fallback non-streaming fetch (used for cache validation)
async function fetchDatasetsH3(bbox, start, end, resolution, datasets, signal) {
  const res = await fetch(`${API_BASE}/datasets/h3`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      bbox,
      date_start: start,
      date_end: end,
      resolution,
      datasets,
      max_frames: 365,
    }),
    signal,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text.slice(0, 200)}`);
  }
  return res.json();
}

// ─── Per-Dataset Result Cache (LRU) ──────────────────────────
// Keyed by (bbox, res, dateRange, singleDataset) so datasets are cached independently.

function cacheGet(key) {
  if (!resultCache.has(key)) return null;
  const val = resultCache.get(key);
  resultCache.delete(key);
  resultCache.set(key, val);
  return val;
}

function cachePut(key, value) {
  if (resultCache.has(key)) resultCache.delete(key);
  resultCache.set(key, value);
  if (resultCache.size > MAX_CACHE_ENTRIES) {
    const firstKey = resultCache.keys().next().value;
    resultCache.delete(firstKey);
  }
}

// Store each dataset's result individually in the cache
function cacheStoreDatasets(bbox, res, dateRange, datasets, timeline) {
  for (const [dsName, dsData] of Object.entries(datasets)) {
    const key = makeDatasetCacheKey(bbox, res, dateRange, dsName);
    cachePut(key, { data: dsData, timeline });
  }
}

// Look up which datasets are cached and which are missing
function cacheLookupDatasets(bbox, res, dateRange, datasetNames) {
  const cached = {};
  const missing = [];
  let cachedTimeline = [];
  for (const ds of datasetNames) {
    const key = makeDatasetCacheKey(bbox, res, dateRange, ds);
    const entry = cacheGet(key);
    if (entry) {
      cached[ds] = entry.data;
      if (entry.timeline && entry.timeline.length > cachedTimeline.length) {
        cachedTimeline = entry.timeline;
      }
    } else {
      missing.push(ds);
    }
  }
  return { cached, missing, cachedTimeline };
}

// ─── Zoom/Pan Refetch (debounced — waits for user to dwell) ─

const REFETCH_DWELL_MS = 800; // refetch after user stops interacting for 0.8s

function scheduleRefetch() {
  if (refetchTimer) clearTimeout(refetchTimer);
  refetchTimer = setTimeout(() => refetchForCurrentView(), REFETCH_DWELL_MS);
}

function cancelRefetch() {
  if (refetchTimer) { clearTimeout(refetchTimer); refetchTimer = null; }
}

async function refetchForCurrentView() {
  if (!queryDateRange) return;
  if (_agentActive) return; // don't interrupt the agent's fetch

  // Use drawn bbox if available, otherwise map bounds
  const bbox = activeBbox();
  // Resolution from slider (manual) or from bbox size (auto)
  const newRes = getEffectiveResolution(bbox);
  const datasets = getCheckedDatasets();
  const key = makeFetchKey(bbox, newRes, queryDateRange, datasets);

  if (key === lastFetchKey) return;

  // Skip refetch if the new view still largely overlaps with already-loaded data
  if (lastFetchBbox && lastFetchRes === newRes) {
    const [lw, ls, le, ln] = lastFetchBbox;
    const [cw, cs, ce, cn] = bbox;

    // Calculate overlap between current viewport and last-fetched bbox
    const overlapW = Math.max(0, Math.min(le, ce) - Math.max(lw, cw));
    const overlapH = Math.max(0, Math.min(ln, cn) - Math.max(ls, cs));
    const overlapArea = overlapW * overlapH;
    const currentArea = Math.max(0.0001, (ce - cw) * (cn - cs));
    const coverage = overlapArea / currentArea;

    // If 70%+ of the current view is already covered, don't refetch
    if (coverage >= 0.7) {
      lastFetchKey = key;
      updateStatus(`${Object.values(datasetResults).reduce((s, d) => s + (d.total_hexagons || 0), 0)} hexagons · H3 res ${newRes}`);
      return;
    }
  }

  // Check per-dataset cache — only fetch what's missing
  const { cached, missing, cachedTimeline } = cacheLookupDatasets(bbox, newRes, queryDateRange, datasets);

  // Apply cached datasets immediately
  if (Object.keys(cached).length > 0) {
    for (const [ds, data] of Object.entries(cached)) {
      datasetResults[ds] = data;
      renderDatasetHexagons(ds, data.hexagons || []);
    }
    if (cachedTimeline.length > fullTimeline.length) {
      fullTimeline = cachedTimeline;
      timeline = applyFrequencyStep(fullTimeline);
    }
    updateTimelineUI();
  }

  // If everything was cached, we're done
  if (missing.length === 0) {
    lastFetchKey = key;
    lastFetchBbox = [...bbox];
    lastFetchRes = newRes;
    currentH3Res = newRes;
    renderAllDatasets();
    updateTimelineUI();
    updateStatus(`${Object.values(datasetResults).reduce((s, d) => s + (d.total_hexagons || 0), 0)} hexagons · H3 res ${newRes} (cached)`);
    return;
  }

  // Cancel any in-flight request before starting a new one
  cancelActiveRequest();
  activeAbortController = new AbortController();
  const { signal } = activeAbortController;

  isFetching = true;
  const fetchId = ++_fetchGeneration;
  setFetching(true);
  currentH3Res = newRes;
  showLoading(`Loading ${missing.join(", ")}...`);
  setProgress(5);
  updateStatus(`Loading ${missing.join(", ")} at H3 res ${newRes}...`);

  let datasetsLoaded = Object.keys(cached).length;
  const totalDs = datasets.length;
  const seenDatasets = new Set(Object.keys(cached));

  try {
    const result = await fetchDatasetsH3Stream(
      bbox, queryDateRange.start, queryDateRange.end, newRes, missing, signal,
      (dsKey, dsData) => {
        if (fetchId !== _fetchGeneration) return;
        datasetResults[dsKey] = dsData;
        if (!seenDatasets.has(dsKey) && dsData.total_hexagons != null) {
          seenDatasets.add(dsKey);
          datasetsLoaded++;
        }
        setProgress(Math.round((datasetsLoaded / totalDs) * 90));
        viewMode = "timeline";
        if (currentFrameIdx >= timeline.length) currentFrameIdx = 0;
        renderAllDatasets();
        updateTimelineUI();
        updateStatus(`${dsKey}: ${(dsData.frames || []).length} days loaded...`);
      }
    );

    if (fetchId !== _fetchGeneration) return;

    if (result && result.datasets) {
      for (const [ds, data] of Object.entries(result.datasets)) {
        datasetResults[ds] = data;
      }
      if (result.timeline && result.timeline.length > 0 && (result.timeline.length > fullTimeline.length || timeline.length === 0)) {
        fullTimeline = result.timeline;
        timeline = applyFrequencyStep(fullTimeline);
      }
      // Cache the newly fetched datasets
      cacheStoreDatasets(bbox, newRes, queryDateRange, result.datasets, fullTimeline);
    }
    lastFetchKey = key;
    lastFetchBbox = [...bbox];
    lastFetchRes = newRes;
    renderAllDatasets();
    updateTimelineUI();
    setProgress(100);
    updateStatus(`${Object.values(datasetResults).reduce((s, d) => s + (d.total_hexagons || 0), 0)} hexagons · H3 res ${newRes}`);
  } catch (err) {
    if (err.name === "AbortError") return;
    if (fetchId !== _fetchGeneration) return;
    console.warn("Refetch failed:", err);
    updateStatus(`Error: ${err.message}`);
  } finally {
    if (fetchId === _fetchGeneration) {
      isFetching = false;
      setFetching(false);
      hideLoading();
    }
  }
}

// ─── AI Agent ────────────────────────────────────────────────

function addChat(role, text) {
  if (!chatLog) return;
  const el = document.createElement("div");
  el.className = `chat-msg ${role}`;
  el.textContent = text;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function addTyping() {
  if (!chatLog) return;
  const el = document.createElement("div");
  el.className = "chat-msg agent";
  el.id = "typing";
  el.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function removeTyping() { const el = $("#typing"); if (el) el.remove(); }

function streamText(text) {
  return new Promise((resolve) => {
    removeTyping();
    if (!chatLog) { resolve(); return; }
    const el = document.createElement("div");
    el.className = "chat-msg agent";
    chatLog.appendChild(el);

    // Try to render as rich insight cards
    const html = formatInsightHTML(text);
    if (html) {
      el.classList.add("rich");
      el.innerHTML = html;
      chatLog.scrollTop = chatLog.scrollHeight;
      resolve();
      return;
    }

    // Fallback: plain text
    el.textContent = text;
    chatLog.scrollTop = chatLog.scrollHeight;
    resolve();
  });
}

/** Convert a plain-text insight report into rich HTML cards */
function formatInsightHTML(text) {
  if (!text) return null;

  // Detect if this is a structured report (has dataset section headers)
  const hasStructure = /\b(FIRE|TEMPERATURE|VEGETATION|WATER|LAND USE|WHY):/i.test(text);
  if (!hasStructure) return null;

  let html = '<div class="insight-cards">';

  // Extract intro (everything before first section keyword)
  const introMatch = text.match(/^([\s\S]*?)(?=(?:FIRE|TEMPERATURE|VEGETATION|WATER|LAND USE|WHY):)/i);
  const intro = introMatch ? introMatch[1].replace(/REPORT:.*\n?/i, "").replace(/Area:.*\n?/i, "").replace(/Location:.*\n?/i, "").replace(/---\n?/g, "").trim() : "";
  if (intro) {
    html += `<div class="insight-intro">${_esc(intro)}</div>`;
  }

  // Fire
  const fireMatch = text.match(/FIRE:\s*([\s\S]*?)(?=\n\n|\nTEMPERATURE:|\nVEGETATION:|\nWATER:|\nLAND USE:|\nWHY:|\n---|$)/i);
  if (fireMatch) {
    html += _makeCard("fire", "🔥", "Fire Activity", _highlightMetrics(fireMatch[1].trim()));
  }

  // Temperature
  const tempMatch = text.match(/TEMPERATURE:\s*([\s\S]*?)(?=\n\n|\nFIRE:|\nVEGETATION:|\nWATER:|\nLAND USE:|\nWHY:|\n---|$)/i);
  if (tempMatch) {
    html += _makeCard("weather", "🌡️", "Temperature", _highlightMetrics(tempMatch[1].trim()));
  }

  // Vegetation
  const vegMatch = text.match(/VEGETATION:\s*([\s\S]*?)(?=\n\n|\nFIRE:|\nTEMPERATURE:|\nWATER:|\nLAND USE:|\nWHY:|\n---|$)/i);
  if (vegMatch) {
    html += _makeCard("vegetation", "🌱", "Vegetation", _highlightMetrics(vegMatch[1].trim()));
  }

  // Water/Flood
  const waterMatch = text.match(/WATER:\s*([\s\S]*?)(?=\n\n|\nFIRE:|\nTEMPERATURE:|\nVEGETATION:|\nLAND USE:|\nWHY:|\n---|$)/i);
  if (waterMatch) {
    html += _makeCard("water", "🌊", "Water / Flood", _highlightMetrics(waterMatch[1].trim()));
  }

  // Land Use
  const landMatch = text.match(/LAND USE:\s*([\s\S]*?)(?=\n\n|\nFIRE:|\nTEMPERATURE:|\nVEGETATION:|\nWATER:|\nWHY:|\n---|$)/i);
  if (landMatch) {
    html += _makeCard("landuse", "🏙️", "Land Use", _highlightMetrics(landMatch[1].trim()));
  }

  // Why
  const whyMatch = text.match(/WHY:\s*([\s\S]*?)(?=\n---|$)/i);
  if (whyMatch) {
    html += _makeCard("why", "💡", "Analysis", _highlightMetrics(whyMatch[1].trim()));
  }

  html += "</div>";
  return html;
}

function _makeCard(cls, icon, title, bodyHTML) {
  return `<div class="insight-card ${cls}"><div class="card-header">${icon} ${_esc(title)}</div><div class="card-body">${bodyHTML}</div></div>`;
}

function _esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function _highlightMetrics(text) {
  // Escape first, then highlight numbers/metrics
  let s = _esc(text);
  // Bold numbers with units
  s = s.replace(/(\d+\.?\d*)\s*°C/g, '<span class="metric">$1°C</span>');
  s = s.replace(/(\d+\.?\d*)%/g, '<span class="metric">$1%</span>');
  s = s.replace(/NDVI\s*(\d+\.\d+)/g, 'NDVI <span class="metric">$1</span>');
  s = s.replace(/(\d+)\s*cells/g, '<span class="metric">$1</span> cells');
  s = s.replace(/(\d+)\s*days/g, '<span class="metric">$1</span> days');
  s = s.replace(/(\d[\d,]*)\s*(?:total\s)?pixels/g, '<span class="metric">$1</span> pixels');
  // Replace — with an em-dash that doesn't break
  s = s.replace(/\n/g, "<br>");
  return s;
}

/**
 * Get a natural AI response. Tries the LLM with a fast timeout,
 * falls back instantly to local summary if unavailable.
 */
let _llmAvailable = false; // start disabled; enable after first successful call
let _llmFailCount = 0;
let _llmChecked = false; // have we tried at all yet?

async function getAIResponse(question, dataReport) {
  // Quick check on first call — if it works, enable LLM going forward
  if (!_llmChecked) {
    _llmChecked = true;
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 2000); // 2s probe
      const res = await fetch(`${API_BASE}/datasets/analyse`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: question || "test", data_report: dataReport }),
        signal: controller.signal,
      });
      clearTimeout(timeout);
      if (res.ok) {
        const data = await res.json();
        if (data.model && data.model !== "fallback") {
          _llmAvailable = true;
          return data.analysis;
        }
        // Server returned fallback — LLM is down, use local
        return data.analysis || buildFallbackResponse(question, dataReport);
      }
    } catch (e) { /* LLM unavailable */ }
    return buildFallbackResponse(question, dataReport);
  }

  // After first check, skip network if LLM is known unavailable
  if (!_llmAvailable) {
    return buildFallbackResponse(question, dataReport);
  }

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);
    const res = await fetch(`${API_BASE}/datasets/analyse`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: question || "Summarize what you see in this area",
        data_report: dataReport,
      }),
      signal: controller.signal,
    });
    clearTimeout(timeout);
    if (res.ok) {
      const data = await res.json();
      _llmFailCount = 0;
      if (data.analysis && data.model !== "fallback") return data.analysis;
      return data.analysis || buildFallbackResponse(question, dataReport);
    }
  } catch (e) {
    _llmFailCount++;
    if (_llmFailCount >= 2) {
      _llmAvailable = false;
      setTimeout(() => { _llmAvailable = true; _llmFailCount = 0; }, 60000);
    }
  }
  return buildFallbackResponse(question, dataReport);
}

function buildFallbackResponse(question, report) {
  if (!report || report.length < 20) return "I don't have enough data yet. Try running an analysis first.";
  // Return the structured report directly — the rich card formatter will handle display
  return report;
}

function generateInsight(question, placeName) {
  // Use all loaded datasets for analysis, not just checked ones
  const allLoaded = Object.keys(datasetResults).filter(ds => datasetResults[ds]?.total_hexagons > 0);
  const checked = [...new Set([...getCheckedDatasets(), ...allLoaded])];
  const hasData = checked.some((ds) => datasetResults[ds]?.total_hexagons > 0);

  if (!hasData) {
    return "No data loaded. Set dates and ask a question — I'll fetch what's needed.";
  }

  const q = (question || "").toLowerCase();

  // ─── Gather all stats upfront
  const hasFire = datasetResults.fire?.total_hexagons > 0;
  const hasVeg = datasetResults.vegetation?.total_hexagons > 0;
  const hasWeather = datasetResults.weather?.total_hexagons > 0;
  const hasFlood = datasetResults.flood?.total_hexagons > 0;
  const hasLanduse = datasetResults.landuse?.total_hexagons > 0;

  // Fire stats
  let fireHexCount = 0, firePixels = 0, fireDays = 0;
  let fireTrend = null, fireFirstHalf = 0, fireSecondHalf = 0, firePeakDate = null, firePeakCount = 0;
  let fireHighPct = 0;
  if (hasFire) {
    const data = datasetResults.fire;
    const hexagons = data.hexagons || [];
    const frames = data.frames || [];
    fireHexCount = data.total_hexagons;
    firePixels = hexagons.reduce((s, h) => s + (h.pixel_count || h.total_pixels || 0), 0);
    fireDays = frames.length;

    let high = 0, nom = 0, low = 0;
    for (const h of hexagons) { high += (h.classes?.high || 0); nom += (h.classes?.nominal || 0); low += (h.classes?.low || 0); }
    const total = high + nom + low;
    fireHighPct = total > 0 ? Math.round(high / total * 100) : 0;

    if (frames.length >= 2) {
      const mid = Math.floor(frames.length / 2);
      const first = frames.slice(0, mid);
      const second = frames.slice(mid);
      fireFirstHalf = first.reduce((s, f) => s + (f.hexagons?.length || 0), 0) / Math.max(first.length, 1);
      fireSecondHalf = second.reduce((s, f) => s + (f.hexagons?.length || 0), 0) / Math.max(second.length, 1);
      fireTrend = fireSecondHalf > fireFirstHalf * 1.3 ? "increasing" : fireSecondHalf < fireFirstHalf * 0.7 ? "decreasing" : "stable";
      for (const f of frames) {
        const cnt = (f.hexagons || []).length;
        if (cnt > firePeakCount) { firePeakCount = cnt; firePeakDate = f.acq_date; }
      }
    }
  }

  // Temperature
  let avgTemp = null, maxTemp = null, hotHexPct = 0;
  if (hasWeather) {
    const hexs = datasetResults.weather.hexagons;
    const tempSum = hexs.reduce((s, h) => s + (h.value_mean || 0), 0);
    avgTemp = (tempSum / hexs.length * 0.02 - 273.15);
    maxTemp = (Math.max(...hexs.map(h => h.value_max || 0)) * 0.02 - 273.15);
    hotHexPct = Math.round(hexs.filter(h => (h.value_mean * 0.02 - 273.15) > 35).length / hexs.length * 100);
  }

  // Vegetation
  let avgNdvi = null, lowVegPct = 0;
  if (hasVeg) {
    const hexs = datasetResults.vegetation.hexagons;
    avgNdvi = hexs.reduce((s, h) => s + (h.value_mean || 0), 0) / hexs.length / 10000;
    lowVegPct = Math.round(hexs.filter(h => (h.value_mean / 10000) < 0.3).length / hexs.length * 100);
  }

  // Land use
  let landBreakdown = null;
  if (hasLanduse) {
    const hexs = datasetResults.landuse.hexagons;
    const classCounts = {};
    for (const h of hexs) for (const [cls, cnt] of Object.entries(h.classes || {})) classCounts[cls] = (classCounts[cls] || 0) + cnt;
    const total = Object.values(classCounts).reduce((s, v) => s + v, 0);
    landBreakdown = Object.entries(classCounts).sort(([,a],[,b]) => b - a).map(([k, v]) => ({ name: k, pct: Math.round(v / total * 100) }));
  }

  // ─── Build a single unified report
  let r = "";
  const period = queryDateRange ? `${queryDateRange.start} to ${queryDateRange.end}` : "selected period";

  // Always lead with a direct summary of what the data shows
  r += `REPORT: ${period}\n`;
  const locationStr = placeName ? `Location: ${placeName}\n` : "";
  r += locationStr + "\n";

  // Fire section
  if (hasFire) {
    r += `FIRE: ${fireHexCount} cells detected fire over ${fireDays} days (${firePixels.toLocaleString()} total pixels). `;
    r += `${fireHighPct}% high-confidence detections. `;
    if (fireTrend === "increasing") r += `Activity is increasing — went from ~${fireFirstHalf.toFixed(0)} to ~${fireSecondHalf.toFixed(0)} detections/day. `;
    else if (fireTrend === "decreasing") r += `Activity declining (${fireFirstHalf.toFixed(0)} → ${fireSecondHalf.toFixed(0)}/day). `;
    else if (fireTrend === "stable") r += `Steady rate throughout the period. `;
    if (firePeakDate) r += `Peak: ${firePeakDate} (${firePeakCount} cells).`;
    r += `\n\n`;
  }

  // Temperature section
  if (hasWeather) {
    r += `TEMPERATURE: Mean surface temp ${avgTemp.toFixed(1)}°C, max ${maxTemp.toFixed(1)}°C. `;
    if (hotHexPct > 0) r += `${hotHexPct}% of the area above 35°C. `;
    if (avgTemp > 35) r += `This is extreme heat — fuels dry rapidly at these temps.`;
    else if (avgTemp > 28) r += `Elevated but not extreme.`;
    else r += `Moderate temperatures.`;
    r += `\n\n`;
  }

  // Vegetation section
  if (hasVeg) {
    r += `VEGETATION: Mean NDVI ${avgNdvi.toFixed(3)}. ${lowVegPct}% of area below 0.3 (dry/stressed). `;
    if (avgNdvi < 0.2) r += `Very dry — dead or dormant vegetation dominates, high fuel availability.`;
    else if (avgNdvi < 0.35) r += `Mixed — some dry areas that can carry fire, some green.`;
    else if (avgNdvi < 0.5) r += `Moderately green. Fires here are less about drought and more about ignition sources.`;
    else r += `Lush vegetation — fires unlikely unless human-caused.`;
    r += `\n\n`;
  }

  // Land use section
  if (hasLanduse && landBreakdown) {
    r += `LAND USE: ` + landBreakdown.slice(0, 4).map(c => `${c.name} ${c.pct}%`).join(", ") + `.\n\n`;
  }

  // Flood section
  if (hasFlood) {
    const data = datasetResults.flood;
    const hexes = data.hexagons || [];
    const totalPx = hexes.reduce((s, h) => s + (h.pixel_count || 0), 0);
    const daysActive = hexes.length > 0 ? Math.max(...hexes.map(h => h.days_active || 0)) : 0;
    r += `WATER: ${data.total_hexagons} cells with water detected, ${totalPx} total water pixels. `;
    if (daysActive > 0) r += `Active up to ${daysActive} days in period.`;
    r += `\n\n`;
  }

  // ─── Explanation / Why section
  if (hasFire) {
    r += `---\nWHY: `;
    const reasons = [];

    if (hasWeather && avgTemp > 30) {
      reasons.push(`surface temperatures averaging ${avgTemp.toFixed(0)}°C dry out fuels`);
    }
    if (hasVeg && avgNdvi < 0.35) {
      reasons.push(`${lowVegPct}% of vegetation is dry/stressed (NDVI < 0.3) providing abundant fuel`);
    }
    if (hasLanduse && landBreakdown) {
      const shrub = landBreakdown.find(c => c.name === "shrub" || c.name === "grass");
      const crop = landBreakdown.find(c => c.name === "crop");
      if (shrub && shrub.pct > 20) reasons.push(`${shrub.pct}% ${shrub.name}land is fire-prone`);
      if (crop && crop.pct > 15) reasons.push(`${crop.pct}% cropland suggests possible agricultural burns`);
    }
    if (fireTrend === "increasing") {
      reasons.push(`fire is spreading (escalating ${Math.round((fireSecondHalf/Math.max(fireFirstHalf,0.1)-1)*100)}% over the period)`);
    }

    // If no supporting datasets loaded, provide general fire causes based on season/location
    if (!hasWeather && !hasVeg && !hasLanduse) {
      const period = queryDateRange || {};
      const startMonth = period.start ? new Date(period.start + "T00:00:00Z").getMonth() : -1;
      if (startMonth >= 5 && startMonth <= 8) {
        reasons.push("summer heat and drought create ideal conditions for fire ignition and spread");
        reasons.push("dry vegetation acts as fuel — lower NDVI values correlate with higher fire risk");
      } else if (startMonth >= 2 && startMonth <= 4) {
        reasons.push("spring conditions with dry grassland from winter can carry fire, especially with agricultural burns");
      } else {
        reasons.push("fires in this season may be related to land clearing, agricultural burns, or unusual drought");
      }
      reasons.push("common ignition sources include human activity (arson, equipment), lightning, and agricultural burning");
      if (placeName && /portugal|spain|mediterranean|greece|italy/i.test(placeName)) {
        reasons.push("Mediterranean landscapes with eucalyptus and maquis shrubland are particularly fire-prone");
      }
    }

    if (reasons.length > 0) {
      r += reasons.join("; ") + `. `;
    }

    // Context-specific for well-known fire-prone areas
    if (hasVeg && avgNdvi > 0.4 && fireHexCount > 5) {
      r += `Note: vegetation is relatively green, so these fires are likely human-ignited (arson, equipment, power lines) or from dry wind events rather than drought.`;
    } else if (hasWeather && avgTemp > 35 && hasVeg && avgNdvi < 0.3) {
      r += `Classic fire weather pattern: extreme heat + dry fuel = rapid ignition and spread.`;
    } else if (fireHexCount <= 5) {
      r += `Low number of detections — could be small/contained burns or early-stage ignitions.`;
    }
    r += `\n`;
  }

  return r;
}

// ─── Export ──────────────────────────────────────────────────

function exportData() {
  const checked = getCheckedDatasets();
  const rows = ["dataset,h3_index,pixel_count,value_mean,value_max,dominant_class,days_active,lat,lng"];
  for (const ds of checked) {
    const hexagons = datasetResults[ds]?.hexagons || [];
    for (const h of hexagons) {
      rows.push(`${ds},${h.h3},${h.pixel_count || h.total_pixels || 0},${h.value_mean},${h.value_max},${h.dominant_class},${h.days_active || 0},${h.center[0]},${h.center[1]}`);
    }
  }
  if (rows.length <= 1) { updateStatus("No data"); return; }
  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `earth_h3_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
  updateStatus("CSV exported");
}

// ─── Main Query ──────────────────────────────────────────────

async function runQuery() {
  stopPlayback();
  cancelActiveRequest();
  for (const layer of Object.values(datasetLayers)) layer.clearLayers();
  datasetResults = {};
  timeline = [];
  currentFrameIdx = 0;
  viewMode = "aggregate";
  lastFetchKey = "";
  setProgress(0);

  const range = parseDateRange();
  if (!range) { hideLoading(); updateStatus("Invalid date range"); return; }

  const datasets = getCheckedDatasets();
  queryDateRange = range;
  currentQuery = "";

  const bbox = activeBbox();
  currentH3Res = getEffectiveResolution(bbox);

  // Check per-dataset cache — only fetch what's missing
  const cacheKey = makeFetchKey(bbox, currentH3Res, range, datasets);
  const { cached: cachedDs, missing: missingDs, cachedTimeline } = cacheLookupDatasets(bbox, currentH3Res, range, datasets);

  // Apply any cached datasets immediately
  if (Object.keys(cachedDs).length > 0) {
    for (const [ds, data] of Object.entries(cachedDs)) {
      datasetResults[ds] = data;
    }
    if (cachedTimeline.length > 0) {
      fullTimeline = cachedTimeline;
      timeline = applyFrequencyStep(fullTimeline);
    }
  }

  // If everything was cached, render and respond
  if (missingDs.length === 0) {
    lastFetchKey = cacheKey;
    lastFetchBbox = [...bbox];
    lastFetchRes = currentH3Res;
    renderAllDatasets();
    updateTimelineUI();
    if (askHint) askHint.classList.add("hidden");
    const totalHex = Object.values(datasetResults).reduce((s, d) => s + (d.total_hexagons || 0), 0);
    updateStatus(`${totalHex} hexagons · ${datasets.join(", ")} · H3 res ${currentH3Res} (cached)`);
    const report = generateInsight(currentQuery);
    const response = await getAIResponse(currentQuery, report);
    await streamText(response);
    return;
  }

  cancelActiveRequest();
  isFetching = true;
  setFetching(true);
  showLoading(`Loading ${missingDs.join(", ")}...`);
  setProgress(5);
  updateStatus(`Loading ${missingDs.join(", ")}...`);

  activeAbortController = new AbortController();
  const { signal } = activeAbortController;

  let datasetsLoaded = Object.keys(cachedDs).length;
  const totalDs = datasets.length;
  const seenDs = new Set(Object.keys(cachedDs));

  // Render cached data while we wait
  if (Object.keys(cachedDs).length > 0) renderAllDatasets();

  try {
    const result = await fetchDatasetsH3Stream(
      bbox, range.start, range.end, currentH3Res, missingDs, signal,
      (dsKey, dsData) => {
        datasetResults[dsKey] = dsData;
        if (!seenDs.has(dsKey) && dsData.total_hexagons != null) {
          seenDs.add(dsKey);
          datasetsLoaded++;
        }
        setProgress(Math.round((datasetsLoaded / totalDs) * 85) + 5);
        viewMode = "timeline";
        if (currentFrameIdx >= timeline.length) currentFrameIdx = 0;
        renderAllDatasets();
        updateTimelineUI();
        updateStatus(`${dsKey}: ${(dsData.frames || []).length} days loaded...`);
      }
    );

    if (!result || !result.datasets || Object.keys(result.datasets).length === 0) {
      // If we had some cached data, that's still OK
      if (Object.keys(cachedDs).length === 0) {
        isFetching = false;
        setFetching(false);
        hideLoading();
        updateStatus("No data found for this area and date range");
        addChat("agent", "I couldn't find any data for that area and time period. Try a different region or adjust the dates.");
        updateTimelineUI();
        return;
      }
    }

    if (result && result.datasets) {
      for (const [ds, data] of Object.entries(result.datasets)) {
        datasetResults[ds] = data;
      }
      if (result.timeline && result.timeline.length > 0 && (result.timeline.length > fullTimeline.length || timeline.length === 0)) {
        fullTimeline = result.timeline;
        timeline = applyFrequencyStep(fullTimeline);
      }
      // Cache newly fetched datasets individually
      cacheStoreDatasets(bbox, currentH3Res, range, result.datasets, fullTimeline);
    }
    lastFetchKey = cacheKey;
    lastFetchBbox = [...bbox];
    lastFetchRes = currentH3Res;

    setProgress(90);
    renderAllDatasets();

    setProgress(100);
    isFetching = false;
    setFetching(false);
    hideLoading();
    if (askHint) askHint.classList.add("hidden");

    const totalHex = Object.values(datasetResults).reduce((s, d) => s + (d.total_hexagons || 0), 0);
    updateStatus(`${totalHex} hexagons · ${datasets.join(", ")} · H3 res ${currentH3Res}`);

    // Generate instant response
    const report = generateInsight(currentQuery);
    const response = await getAIResponse(currentQuery, report);
    await streamText(response);
    updateTimelineUI();

  } catch (err) {
    if (err.name === "AbortError") {
      isFetching = false;
      setFetching(false);
      hideLoading();
      return;
    }
    isFetching = false;
    setFetching(false);
    hideLoading();
    updateStatus(`Error: ${err.message}`);
    addChat("agent", `Something went wrong: ${err.message}\n\nMake sure the backend is running.`);
  }
}

// ─── Event Listeners ─────────────────────────────────────────

if (queryForm) queryForm.addEventListener("submit", (e) => { e.preventDefault(); runQuery(); });

if (chatForm) chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const msg = (chatInput ? chatInput.value : "").trim();
  if (!msg) return;
  addChat("user", msg);
  if (chatInput) chatInput.value = "";
  if (chatInput) autoExpand(chatInput);
  addTyping();
  handleAgentQuestion(msg);
});

// Determine which datasets the agent needs to answer a question
// Simple keyword fallback — the LLM classifier handles typos/misspellings natively
function inferNeededDatasets(question) {
  const q = question.toLowerCase();
  const needed = new Set();

  // Keyword matching with common typos/misspellings (LLM handles this better; fallback only)
  if (/fire|frie|fier|fyre|burn|wildfire|incendio|fuego/.test(q)) needed.add("fire");
  if (/flo[ou]d|flod|water|rain|river|inundaci/.test(q)) needed.add("flood");
  if (/veget|vegitat|ndvi|plant|green|drought|plantas/.test(q)) needed.add("vegetation");
  if (/weather|wether|wheather|temp|heat|hot|cold|climate|calor/.test(q)) needed.add("weather");
  if (/land.?use|urban|crop|forest|city|ciudad/.test(q)) needed.add("landuse");

  // Generic "what happened" / "analyze" / "show everything" — use all
  if (/what.*(happen|going|see)|analyz|summar|overview|assess|everything|all/.test(q)) {
    for (const ds of ["fire", "flood", "weather", "vegetation", "landuse"]) needed.add(ds);
  }

  // If nothing matched, default to fire
  if (needed.size === 0) {
    needed.add("fire");
  }

  return [...needed];
}

// Infer CONTEXT categories (loaded for reasoning but not plotted)
function inferContextDatasets(question, plotDatasets) {
  const q = question.toLowerCase();
  const context = new Set();

  // Causal/risk/why questions need supporting data
  if (/why|cause|reason|explain|because|led to|trigger|risk|danger|threat|prevent/.test(q)) {
    if (plotDatasets.includes("fire")) {
      context.add("weather");
      context.add("vegetation");
    }
    if (plotDatasets.includes("flood")) {
      context.add("weather");
      context.add("landuse");
    }
    if (plotDatasets.includes("vegetation")) {
      context.add("weather");
    }
  }

  // Remove anything already in plot
  for (const ds of plotDatasets) context.delete(ds);
  return [...context];
}

// Auto-fetch missing datasets needed to answer a question
async function handleAgentQuestion(question) {
  _agentActive = true;

  // ─── Follow-up detection: if we already have data and this looks like
  //     a follow-up question (no new place, no new date), just answer from
  //     existing data without re-fetching or moving the map.
  const hasLoadedData = Object.values(datasetResults).some(d => d?.total_hexagons > 0);
  const placeQuery = extractPlaceName(question);
  const extractedRange = extractDateRange(question);
  // Detect explicit reference to the drawn area
  const usesDrawnArea = /\b(drawn|selected|current|this|my)\s+(bbox|area|polygon|region|selection|box|zone)\b/i.test(question)
      || /\b(bbox|drawn area|drawn polygon|selected area)\b/i.test(question);
  const isFollowUp = hasLoadedData && !placeQuery && !extractedRange && !usesDrawnArea;

  if (isFollowUp) {
    // Answer directly from already-loaded data
    removeTyping();

    // For causal/why questions, load context datasets BEFORE answering
    const isCausalQuestion = /why|cause|reason|explain|prevent|risk|trigger|what.*(led|start|happen)/i.test(question);
    const classifyResult = await Promise.race([
      fetch(`${API_BASE}/datasets/classify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      }).then(r => r.ok ? r.json() : null).catch(() => null),
      new Promise(r => setTimeout(() => r(null), 2000)),
    ]);

    const contextCats = classifyResult?.context_categories || inferContextDatasets(question, Object.keys(datasetResults));
    // Fetch missing context datasets — WAIT for them on causal questions
    if (contextCats.length > 0 && queryDateRange) {
      const bbox = activeBbox();
      const res = currentH3Res || getEffectiveResolution(bbox);
      const { missing } = cacheLookupDatasets(bbox, res, queryDateRange, contextCats);
      if (missing.length > 0) {
        if (isCausalQuestion) {
          // Wait for context data so we can explain causes properly (max 10s)
          addTyping();
          updateStatus(`Loading ${missing.join(", ")} for analysis...`);
          const ctxController = new AbortController();
          const ctxTimeout = setTimeout(() => ctxController.abort(), 10000);
          try {
            await fetchDatasetsH3Stream(
              bbox, queryDateRange.start, queryDateRange.end, res, missing, ctxController.signal,
              (dsKey, dsData) => { datasetResults[dsKey] = dsData; }
            );
          } catch (e) { /* best effort — answer with whatever we have */ }
          clearTimeout(ctxTimeout);
          removeTyping();
        } else {
          // Non-causal follow-up: background load
          fetchDatasetsH3Stream(
            bbox, queryDateRange.start, queryDateRange.end, res, missing, new AbortController().signal,
            (dsKey, dsData) => { datasetResults[dsKey] = dsData; }
          ).catch(() => {});
        }
      }
    }

    const report = generateInsight(question, _cachedPlaceName);
    const response = await getAIResponse(question, report);
    await streamText(response);
    _agentActive = false;
    _updatePlaceNameCache();
    return;
  }

  // ─── New query: full flow with geocoding + data fetch ───
  // Run LLM classification in parallel for speed
  const classifyPromise = fetch(`${API_BASE}/datasets/classify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  }).then(r => r.ok ? r.json() : null).catch(() => null);

  // ─── Extract date range from question (client-side fallback, AI may override) ───
  if (extractedRange) {
    queryDateRange = extractedRange;
    if (startDate) startDate.value = extractedRange.start;
    if (endDate) endDate.value = extractedRange.end;
  } else if (!queryDateRange) {
    const range = parseDateRange();
    if (range) {
      queryDateRange = range;
    }
  }

  // ─── Step 1: Geocode — extract place name and center the map there
  let bbox = activeBbox();
  if (usesDrawnArea && resolvedBbox) {
    // User explicitly referenced the drawn area — use it as-is
    bbox = [...resolvedBbox];
    map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [30, 30], animate: true });
    currentH3Res = getEffectiveResolution(bbox);
    updateStatus("Using drawn area");
    await new Promise(r => setTimeout(r, 300));
  } else if (usesDrawnArea && !resolvedBbox) {
    removeTyping();
    await streamText("Draw an area on the map first using the **Draw polygon** button, then ask me again.");
    _agentActive = false;
    return;
  } else if (placeQuery) {
    try {
      updateStatus(`Locating "${placeQuery}"...`);
      const geoRes = await fetch(`${API_BASE}/datasets/geocode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: placeQuery }),
      });
      if (geoRes.ok) {
        const geo = await geoRes.json();
        if (geo.bbox && geo.lat && geo.lon) {
          bbox = geo.bbox;
          resolvedBbox = [...bbox];

          // If a polygon is returned (country/region), use it for clipping
          if (geo.polygon) {
            activePolygon = geo.polygon;
            drawBoundary(geo.polygon);
          } else {
            clearBoundary();
          }

          map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [40, 40], animate: true });
          currentH3Res = getEffectiveResolution(bbox);
          updateStatus(`Centered on ${geo.name?.split(",")[0] || placeQuery}`);
          // Small delay to let map animate
          await new Promise(r => setTimeout(r, 300));
        }
      }
    } catch (e) { /* fallback to current view */ }
  }

  // ─── Step 2: Handle km/minutes reach area from question
  const kmMatch = question.match(/(\d+)\s*km/i);
  const minMatch = question.match(/(\d+)\s*min/i);
  if (kmMatch || minMatch) {
    const [w, s, e, n] = bbox;
    const centerLat = (s + n) / 2;
    const centerLon = (w + e) / 2;
    try {
      const reachBody = {
        lat: centerLat,
        lon: centerLon,
        area_mode: kmMatch ? "radius" : "time",
        radius_km: kmMatch ? Number(kmMatch[1]) : 10,
        minutes: minMatch ? Number(minMatch[1]) : 30,
        vehicle: "driving",
      };
      const reachRes = await fetch(`${API_BASE}/datasets/reach-area`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reachBody),
      });
      if (reachRes.ok) {
        const reachData = await reachRes.json();
        if (reachData.bbox) {
          bbox = reachData.bbox;
          resolvedBbox = [...bbox];
          const [rw, rs, re, rn] = bbox;
          map.fitBounds([[rs, rw], [rn, re]], { padding: [30, 30], animate: true });
          currentH3Res = getEffectiveResolution(bbox);
        }
      }
    } catch (e) { /* fallback */ }
  }

  // Await LLM classification — this is essential for handling typos/misspellings
  const classifyTimeout = new Promise(r => setTimeout(() => r(null), 8000));
  const classifyResult = await Promise.race([classifyPromise, classifyTimeout]);

  // Apply AI-determined date range (overrides client-side extraction)
  if (classifyResult?.date_start && classifyResult?.date_end) {
    queryDateRange = { start: classifyResult.date_start, end: classifyResult.date_end };
    if (startDate) startDate.value = classifyResult.date_start;
    if (endDate) endDate.value = classifyResult.date_end;
  }

  // Separate "plot" categories (shown on map) from "context" categories (loaded for reasoning)
  const plotCategories = classifyResult?.categories?.length
    ? classifyResult.categories
    : inferNeededDatasets(question); // fallback to keyword-based (instant)

  const contextCategories = classifyResult?.context_categories?.length
    ? classifyResult.context_categories
    : inferContextDatasets(question, plotCategories); // fallback
  // All datasets to fetch = plot + context (but only plot ones are shown)
  const needed = [...new Set([...plotCategories, ...contextCategories])];

  // Update UI checkboxes — only check the PLOT categories (what user sees)
  for (const cb of $$("#dataset-grid input[type='checkbox']")) {
    cb.checked = plotCategories.includes(cb.value);
  }

  // If no categories determined, just respond with current data
  if (needed.length === 0) {
    removeTyping();
    const report = generateInsight(question);
    const response = await getAIResponse(question, report);
    await streamText(response);
    _agentActive = false;
    return;
  }

  // If still no date range, can't fetch
  if (!queryDateRange) {
    removeTyping();
    await streamText("I need a date range to look at. Set the 'From' and 'To' dates on the left, then ask me again.");
    _agentActive = false;
    return;
  }

  // ─── Step 3: Fetch datasets with per-dataset caching
  const res = currentH3Res || getEffectiveResolution(bbox);
  currentH3Res = res;

  // Clear layers (but not datasetResults yet — cache lookup needs the bbox)
  for (const layer of Object.values(datasetLayers)) layer.clearLayers();
  datasetResults = {};
  timeline = [];

  cancelActiveRequest();
  activeAbortController = new AbortController();
  const agentSignal = activeAbortController.signal;
  isFetching = true;
  setFetching(true);
  showLoading(`Analyzing...`);
  setProgress(5);

  // Check per-dataset cache — only fetch what's actually missing
  const { cached: agentCached, missing: agentMissing, cachedTimeline: agentCachedTimeline } =
    cacheLookupDatasets(bbox, res, queryDateRange, needed);

  // Apply cached datasets immediately
  if (Object.keys(agentCached).length > 0) {
    for (const [ds, data] of Object.entries(agentCached)) {
      datasetResults[ds] = data;
      // Only render on map if it's a plot category
      if (plotCategories.includes(ds)) {
        const cb = $(`#dataset-grid input[value="${ds}"]`);
        if (cb && !cb.checked) cb.checked = true;
        const layer = datasetLayers[ds];
        if (layer && !map.hasLayer(layer)) layer.addTo(map);
        renderDatasetHexagons(ds, data.hexagons || []);
      }
    }
    if (agentCachedTimeline.length > fullTimeline.length) {
      fullTimeline = agentCachedTimeline;
      timeline = applyFrequencyStep(fullTimeline);
    }
    updateTimelineUI();
  }

  // If everything is cached, skip straight to AI response
  if (agentMissing.length === 0) {
    isFetching = false;
    setFetching(false);
    hideLoading();
    if (askHint) askHint.classList.add("hidden");
    lastFetchBbox = [...bbox];
    lastFetchRes = res;
    renderAllDatasets();
    updateTimelineUI();
    updateStatus(`${Object.values(datasetResults).reduce((s, d) => s + (d.total_hexagons || 0), 0)} hexagons (cached)`);
    removeTyping();
    const report = generateInsight(question);
    const response = await getAIResponse(question, report);
    await streamText(response);
    _agentActive = false;
    return;
  }

  // Only fetch the missing datasets
  try {
    const totalDs = needed.length;
    let loadedDs = Object.keys(agentCached).length;
    const seenAgentDs = new Set(Object.keys(agentCached));

    const result = await fetchDatasetsH3Stream(
      bbox, queryDateRange.start, queryDateRange.end, res, agentMissing, agentSignal,
      (dsKey, dsData) => {
        datasetResults[dsKey] = dsData;
        if (!seenAgentDs.has(dsKey) && dsData.total_hexagons != null) {
          seenAgentDs.add(dsKey);
          loadedDs++;
        }
        setProgress(Math.round((loadedDs / totalDs) * 85) + 5);
        // Only show on map if it's a PLOT category (not context-only)
        if (plotCategories.includes(dsKey)) {
          const cb = $(`#dataset-grid input[value="${dsKey}"]`);
          if (cb && !cb.checked) cb.checked = true;
          const layer = datasetLayers[dsKey];
          if (layer && !map.hasLayer(layer)) layer.addTo(map);
        }
        viewMode = "timeline";
        if (currentFrameIdx >= timeline.length) currentFrameIdx = 0;
        renderAllDatasets();
        updateTimelineUI();
        updateStatus(`${dsKey}: ${(dsData.frames || []).length} days loaded...`);
      }
    );

    if (result && result.datasets) {
      for (const [ds, data] of Object.entries(result.datasets)) {
        datasetResults[ds] = data;
      }
      if (result.timeline && result.timeline.length > 0 && (result.timeline.length > fullTimeline.length || timeline.length === 0)) {
        fullTimeline = result.timeline;
        timeline = applyFrequencyStep(fullTimeline);
      }
      lastFetchBbox = [...bbox];
      lastFetchRes = res;
      lastFetchKey = makeFetchKey(bbox, res, queryDateRange, Object.keys(datasetResults));
      // Cache newly fetched datasets individually
      cacheStoreDatasets(bbox, res, queryDateRange, result.datasets, fullTimeline);
      renderAllDatasets();
      updateTimelineUI();
      setProgress(100);
      updateStatus(`${Object.values(datasetResults).reduce((s, d) => s + (d.total_hexagons || 0), 0)} hexagons · ${Object.keys(datasetResults).filter(ds => datasetResults[ds]?.total_hexagons > 0).join(", ")}`);
    }
  } catch (err) {
    if (err.name === "AbortError") {
      isFetching = false;
      setFetching(false);
      hideLoading();
      _agentActive = false;
      return;
    }
    console.warn("Agent fetch failed:", err);
    removeTyping();
    addChat("agent", `I had trouble fetching the data: ${err.message}`);
    isFetching = false;
    setFetching(false);
    hideLoading();
    _agentActive = false;
    return;
  }

  isFetching = false;
  setFetching(false);
  hideLoading();
  if (askHint) askHint.classList.add("hidden");

  // Check if any data was actually found
  const totalFound = Object.values(datasetResults).reduce((s, d) => s + (d.total_hexagons || 0), 0);
  if (totalFound === 0) {
    removeTyping();
    const dsNames = plotCategories.join(", ");
    await streamText(`Data was not found for: ${dsNames} in this area and time period. Try a different location, broader date range, or different datasets.`);
    _agentActive = false;
    _updatePlaceNameCache();
    return;
  }

  // Generate response instantly using local data — don't wait for network
  removeTyping();
  _agentActive = false;

  // Use cached place name or fire-and-forget reverse geocode for next time
  const agentReport = generateInsight(question, _cachedPlaceName);
  const response = await getAIResponse(question, agentReport);
  await streamText(response);

  // Update place name cache in background (for next question)
  _updatePlaceNameCache();
}

// Cached place name (updated in background, never blocks)
let _cachedPlaceName = "";
let _placeNameBbox = "";

function _updatePlaceNameCache() {
  const bbox = activeBbox();
  const key = bbox.map(v => v.toFixed(1)).join(",");
  if (key === _placeNameBbox) return; // already have it for this area
  _placeNameBbox = key;
  const [w, s, e, n] = bbox;
  const lat = (s + n) / 2, lon = (w + e) / 2;
  fetch(`${API_BASE}/datasets/reverse-geocode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lat, lon }),
  }).then(r => r.ok ? r.json() : null).then(d => {
    if (d?.name) _cachedPlaceName = d.name;
  }).catch(() => {});
}

// Extract date range from a natural language query
function extractDateRange(question) {
  const q = question.trim();
  const now = new Date();
  const currentYear = now.getFullYear();

  // "2023 to 2026", "from 2020 to 2024", "between 2021 and 2023"
  const yearRange = q.match(/(?:from\s+)?(\d{4})\s*(?:to|[-–]|and)\s*(\d{4})/i);
  if (yearRange) {
    const y1 = parseInt(yearRange[1]);
    const y2 = parseInt(yearRange[2]);
    if (y1 >= 1980 && y2 >= 1980 && y1 <= currentYear + 1 && y2 <= currentYear + 1) {
      return { start: `${y1}-01-01`, end: `${y2}-12-31` };
    }
  }

  // "in 2023", "during 2024" — single year
  const singleYear = q.match(/\b(?:in|during|for)\s+(\d{4})\b/i);
  if (singleYear) {
    const y = parseInt(singleYear[1]);
    if (y >= 1980 && y <= currentYear + 1) {
      return { start: `${y}-01-01`, end: `${y}-12-31` };
    }
  }

  // "last 3 months", "past 6 months"
  const monthsAgo = q.match(/(?:last|past)\s+(\d+)\s*months?/i);
  if (monthsAgo) {
    const m = parseInt(monthsAgo[1]);
    const end = now.toISOString().slice(0, 10);
    const startD = new Date(now);
    startD.setMonth(startD.getMonth() - m);
    return { start: startD.toISOString().slice(0, 10), end };
  }

  // "last 2 years", "past year"
  const yearsAgo = q.match(/(?:last|past)\s+(\d*)\s*years?/i);
  if (yearsAgo) {
    const y = parseInt(yearsAgo[1]) || 1;
    const end = now.toISOString().slice(0, 10);
    const startD = new Date(now);
    startD.setFullYear(startD.getFullYear() - y);
    return { start: startD.toISOString().slice(0, 10), end };
  }

  // "last week", "past 2 weeks"
  const weeksAgo = q.match(/(?:last|past)\s+(\d*)\s*weeks?/i);
  if (weeksAgo) {
    const w = parseInt(weeksAgo[1]) || 1;
    const end = now.toISOString().slice(0, 10);
    const startD = new Date(now);
    startD.setDate(startD.getDate() - w * 7);
    return { start: startD.toISOString().slice(0, 10), end };
  }

  // "since 2022", "from 2023"
  const since = q.match(/\b(?:since|from)\s+(\d{4})\b/i);
  if (since && !yearRange) {
    const y = parseInt(since[1]);
    if (y >= 1980 && y <= currentYear) {
      return { start: `${y}-01-01`, end: now.toISOString().slice(0, 10) };
    }
  }

  return null;
}

// Extract a place name from a natural language query
function extractPlaceName(question) {
  const q = question.trim();

  // ─── Drawn-area reference: user wants to query the bbox they drew on the map
  if (/\b(drawn|selected|current|this|my)\s+(bbox|area|polygon|region|selection|box|zone)\b/i.test(q)
      || /\b(bbox|drawn area|drawn polygon|selected area)\b/i.test(q)) {
    return null; // no geocoding — use existing resolvedBbox
  }

  // ─── Follow-up detection: skip geocoding for conversational follow-ups
  // These are questions that reference the current context, not a new place
  if (/^(why|what|how|when|who|can you|could you|tell me|explain|is there|are there|show me more|go deeper|more detail)/i.test(q)) {
    // Only geocode if there's an explicit "in [place]" pattern
    const explicitPlace = q.match(/\b(?:in|at|near|around)\s+([A-Z][a-zA-Z\s]{2,}?)(?:\s+(?:last|next|past|from|between|for|during)\b|[?!.]|$)/);
    if (explicitPlace && explicitPlace[1].trim().length >= 2) {
      return explicitPlace[1].trim();
    }
    return null; // follow-up — stay where we are
  }

  // Try to find "in/at/near/around [place]" patterns first (most reliable)
  const inMatch = q.match(/\b(?:in|at|near|around|over|of)\s+(.+?)(?:\s+(?:last|next|past|from|between|for|during)\b|[?!.]|$)/i);
  if (inMatch && inMatch[1].trim().length >= 2) {
    // Clean up: remove dataset words from the extracted place
    const place = inMatch[1].trim()
      .replace(/\b(fire|flood|vegetation|weather|temperature|heat|data|activity|risk)\b/gi, "")
      .replace(/\s+/g, " ")
      .trim();
    if (place.length >= 2) return place;
  }

  // Try "show/find [place] fire/data" — place before dataset word
  const beforeDs = q.match(/(?:show|find|get|check|analyze)\s+(.+?)\s+(?:fire|flood|vegetation|weather|temperature|land\s*use|data|activity)/i);
  if (beforeDs && beforeDs[1].trim().length >= 2) {
    return beforeDs[1].trim();
  }

  // Strip-and-extract fallback: remove known non-place words, whatever remains is likely a place
  const stripped = q
    .replace(/\b(show|find|search|analyze|get|fetch|plot|display|check|look|tell|me|about|the|any|some|this|that|a|an|for|and|or|with|its)\b/gi, "")
    .replace(/\b(fire|fires|flood|floods|vegetation|weather|land\s*use|temperature|heat|ndvi|risk|danger|activity|data|detections?|conditions?|status|wildfire|burn|blaze)\b/gi, "")
    .replace(/\b(last|next|past|from|to|over|between|during|since|until|ago)\b/gi, "")
    .replace(/\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b/gi, "")
    .replace(/\b(year|years|month|months|week|weeks|day|days|summer|winter|spring|autumn)\b/gi, "")
    .replace(/\b\d+\s*(km|min|minutes?|hours?|days?|weeks?|months?|years?)?\b/gi, "")
    .replace(/[?!.,;:]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (stripped.length >= 2) return stripped;

  return null;
}

// Drawing
map.on("click", (e) => {
  if (!drawMode) return;
  drawPoints.push({ lat: e.latlng.lat, lng: e.latlng.lng });
  redrawDrawing();
  updateStatus(`${drawPoints.length} points`);
  if (drawPoints.length >= 3) showDrawConfirm();
});

// Confirm/cancel bar buttons
const drawConfirmBtn = $("#draw-confirm-btn");
const drawCancelBtn = $("#draw-cancel-btn");
if (drawConfirmBtn) drawConfirmBtn.addEventListener("click", () => { if (drawPoints.length >= 3) confirmDrawing(); });
if (drawCancelBtn) drawCancelBtn.addEventListener("click", () => {
  clearDrawing(); drawMode = false;
  if (drawToggle) { drawToggle.classList.remove("active"); drawToggle.textContent = "Draw polygon"; }
  updateStatus("Drawing cancelled");
});

if (drawToggle) drawToggle.addEventListener("click", () => {
  drawMode = !drawMode;
  drawToggle.classList.toggle("active", drawMode);
  drawToggle.textContent = drawMode ? "Drawing..." : "Draw polygon";
  if (drawMode) { clearDrawing(); updateStatus("Click map to add points"); }
  else if (drawPoints.length >= 3) confirmDrawing();
  else hideDrawConfirm();
});

if (drawClear) drawClear.addEventListener("click", () => {
  clearDrawing(); drawMode = false;
  if (drawToggle) { drawToggle.classList.remove("active"); drawToggle.textContent = "Draw polygon"; }
  updateStatus("Area cleared");
});

// Timeline
if (timelineSlider) timelineSlider.addEventListener("input", () => {
  stopPlayback(); viewMode = "timeline";
  currentFrameIdx = Number(timelineSlider.value) || 0;
  renderAllDatasets(); updateTimelineUI();
});
if (timelinePlay) timelinePlay.addEventListener("click", startPlayback);
if (timelinePause) timelinePause.addEventListener("click", () => { stopPlayback(); renderAllDatasets(); updateTimelineUI(); });
if (playSpeed) playSpeed.addEventListener("input", () => { if (playbackTimer) startPlayback(); });

// Frequency step change — re-filter timeline
if (frequency) frequency.addEventListener("change", () => {
  timeline = applyFrequencyStep(fullTimeline);
  if (currentFrameIdx >= timeline.length) currentFrameIdx = Math.max(0, timeline.length - 1);
  renderAllDatasets();
  updateTimelineUI();
});

// Satellite
if (layerSatellite) layerSatellite.addEventListener("click", () => {
  usingSatellite = !usingSatellite;
  layerSatellite.classList.toggle("active", usingSatellite);
  layerSatellite.textContent = usingSatellite ? "Satellite" : "Light";
  if (usingSatellite) { if (!map.hasLayer(baseSatellite)) baseSatellite.addTo(map); if (map.hasLayer(baseLight)) map.removeLayer(baseLight); }
  else { if (!map.hasLayer(baseLight)) baseLight.addTo(map); if (map.hasLayer(baseSatellite)) map.removeLayer(baseSatellite); }
});

if (exportCsv) exportCsv.addEventListener("click", exportData);

// Dataset chip toggles — show/hide layers + refetch if needed
for (const chip of $$("#dataset-grid input[type='checkbox']")) {
  chip.addEventListener("change", () => {
    const ds = chip.value;
    const layer = datasetLayers[ds];
    if (!layer) return;

    if (chip.checked) {
      if (!map.hasLayer(layer)) layer.addTo(map);
      // If we have an active query but no data for this dataset, refetch
      if (queryDateRange && !datasetResults[ds]) {
        scheduleRefetch();
      } else if (datasetResults[ds]) {
        renderAllDatasets();
      }
    } else {
      layer.clearLayers();
    }
    updateTimelineUI();
  });
}

// Zoom/Pan → refetch with dwell debounce
// Only cancel pending scheduled refetch on new interaction (not active requests — let them finish)
map.on("zoomstart", () => { cancelRefetch(); });
map.on("movestart", () => { cancelRefetch(); });
map.on("zoomend", () => {
  if (!queryDateRange) return;
  if (userResolutionOverride === 0) {
    const newRes = getEffectiveResolution(activeBbox());
    if (newRes !== currentH3Res) { currentH3Res = newRes; }
  }
  scheduleRefetch();
});
map.on("moveend", () => {
  if (!queryDateRange) return;
  scheduleRefetch();
  // Pre-cache place name for instant agent responses
  _updatePlaceNameCache();
});

// Resolution slider
if (resolutionSlider) resolutionSlider.addEventListener("input", () => {
  const val = Number(resolutionSlider.value);
  userResolutionOverride = val;
  if (resolutionValue) resolutionValue.textContent = val === 0 ? "Auto" : `${val}`;
  const bbox = activeBbox();
  currentH3Res = getEffectiveResolution(bbox);
  updateStatus(`H3 resolution: ${currentH3Res}${val === 0 ? " (auto)" : " (manual)"}`);
  if (queryDateRange) {
    lastFetchKey = ""; // force refetch
    scheduleRefetch();
  }
});

window.addEventListener("resize", drawTimelineChart);

// Keyboard shortcuts
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runQuery(); }
  if (e.key === "Escape" && drawMode) { clearDrawing(); drawMode = false; if (drawToggle) { drawToggle.classList.remove("active"); drawToggle.textContent = "Draw polygon"; } updateStatus("Drawing cancelled"); }
});

// ─── Init ────────────────────────────────────────────────────

updateTimelineUI();
addChat("agent", "Hi! I'm your Earth observation analyst. I can help you understand fire activity, flooding, vegetation health, temperatures, and land use anywhere on the planet.\n\nJust ask me something like:\n• \"Show me fires in Portugal\"\n• \"Why is there so much fire activity here?\"\n• \"What's the vegetation health in California?\"\n• \"How hot has it been in southern Spain?\"\n\nOr set an area and dates on the left, click 'Run analysis', and I'll tell you what I find.");
