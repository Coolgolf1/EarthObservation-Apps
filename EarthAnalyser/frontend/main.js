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
  return 3;
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
  // Add 20% buffer around viewport to pre-fetch nearby data
  const lngSpan = b.getEast() - b.getWest();
  const latSpan = b.getNorth() - b.getSouth();
  const buf = 0.2;
  return [
    b.getWest() - lngSpan * buf,
    b.getSouth() - latSpan * buf,
    b.getEast() + lngSpan * buf,
    b.getNorth() + latSpan * buf,
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

function clearDrawing() { drawPoints = []; drawingLayer.clearLayers(); resolvedBbox = null; hideDrawConfirm(); }

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
  updateStatus(`Area: ${resolvedBbox.map((v) => v.toFixed(2)).join(", ")} · H3 res ${currentH3Res}`);
  // Auto-refetch if a query is active
  if (queryDateRange) {
    lastFetchKey = ""; // force refetch
    scheduleRefetch();
  }
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
    lines.push(`Class: ${hex.dominant_class}`);
    lines.push(`Occurrence: ${hex.value_mean}%`);
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

  const maxCount = Math.max(...hexagons.map((h) => h.pixel_count));
  const colors = DATASET_COLORS[dataset] || DATASET_COLORS.fire;

  for (const hex of hexagons) {
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
    if (viewMode === "aggregate") {
      renderDatasetHexagons(ds, data.hexagons || []);
    } else {
      // Timeline mode: find the frame matching the current date
      const currentDate = timeline[currentFrameIdx];
      const frames = data.frames || [];
      // Static datasets (flood, landuse) always show all frames
      const isStatic = frames.length > 0 && frames.every(f => f.acq_date === "static");
      if (isStatic) {
        // Merge all static frames
        const allHexes = frames.flatMap(f => f.hexagons || []);
        renderDatasetHexagons(ds, allHexes);
      } else {
        const frame = frames.find(f => f.acq_date === currentDate);
        if (frame) {
          renderDatasetHexagons(ds, frame.hexagons || []);
        } else {
          // No frame for this date — show nothing (gap day)
          datasetLayers[ds].clearLayers();
        }
      }
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
    const frameByDate = new Map(frames.map((f) => [f.acq_date, f]));

    const y0 = pad + bandIdx * bandH;
    const colors = DATASET_COLORS[ds] || DATASET_COLORS.fire;

    for (let i = 0; i < timeline.length; i++) {
      const frame = frameByDate.get(timeline[i]);
      const hexCount = frame ? (frame.hexagons || []).length : 0;
      if (hexCount > 0) {
        ctx.fillStyle = colors.base;
        ctx.globalAlpha = 0.3 + Math.min(0.7, hexCount / 50);
      } else {
        ctx.fillStyle = "#ddd";
        ctx.globalAlpha = 0.2;
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

function startPlayback() {
  stopPlayback();
  if (!timeline.length) return;
  viewMode = "timeline";
  const speed = Math.max(300, Number(playSpeed?.value) || 700);
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
          if (result.timeline.length > fullTimeline.length) {
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
          // Notify callback with current accumulated data
          if (onDataset) onDataset(key, result.datasets[key]);
        } else if (msg.type === "dataset_done") {
          // Final aggregate data for this dataset
          if (result.datasets[msg.key]) {
            result.datasets[msg.key].hexagons = msg.data.hexagons || [];
            result.datasets[msg.key].total_hexagons = msg.data.total_hexagons || 0;
          } else {
            result.datasets[msg.key] = msg.data;
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
      if (result.timeline && result.timeline.length > fullTimeline.length) {
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
    let i = 0;
    const chars = text.split("");
    const speed = Math.max(2, Math.min(8, 800 / chars.length)); // faster for longer texts
    const chunkSize = Math.max(3, Math.ceil(chars.length / 150));
    const iv = setInterval(() => {
      el.textContent += chars.slice(i, i + chunkSize).join("");
      i += chunkSize;
      chatLog.scrollTop = chatLog.scrollHeight;
      if (i >= chars.length) { clearInterval(iv); resolve(); }
    }, speed);
  });
}

/**
 * Get a natural AI response. Sends the data report to the LLM and returns
 * a conversational answer. Falls back to a simple summary if LLM unavailable.
 */
async function getAIResponse(question, dataReport) {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000); // 8s max
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
      if (data.analysis) return data.analysis;
    }
  } catch (e) {
    console.warn("LLM unavailable:", e);
  }
  // Fallback: return a cleaned-up version of the data report
  return buildFallbackResponse(question, dataReport);
}

function buildFallbackResponse(question, report) {
  // Convert the structured report into a more readable summary
  if (!report || report.length < 20) return "I don't have enough data yet. Try running an analysis first.";
  // Just clean up the report a bit
  return report
    .replace(/^REPORT:.*\n/m, "")
    .replace(/^Area:.*\n/m, "")
    .replace(/---\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function generateInsight(question) {
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
  r += `Area: ${activeBbox().map(v => v.toFixed(2)).join(", ")} | H3 res ${currentH3Res}\n\n`;

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
    let perm = 0, seas = 0;
    for (const h of (data.hexagons || [])) { perm += (h.classes?.permanent || 0); seas += (h.classes?.seasonal || 0); }
    const wtotal = perm + seas;
    r += `WATER: ${data.total_hexagons} cells with water. `;
    if (wtotal > 0) r += `${Math.round(perm/wtotal*100)}% permanent, ${Math.round(seas/wtotal*100)}% seasonal.`;
    r += `\n\n`;
  }

  // ─── Explanation / Why section (always included if enough data)
  if (hasFire && (hasWeather || hasVeg || hasLanduse)) {
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

  // Hotspots
  if (hasFire) {
    const fireHexes = datasetResults.fire.hexagons || [];
    if (fireHexes.length > 0) {
      const top = [...fireHexes].sort((a, b) => (b.pixel_count || 0) - (a.pixel_count || 0)).slice(0, 3);
      r += `\nHOTSPOTS: ` + top.map(h => `(${h.center[0].toFixed(3)}, ${h.center[1].toFixed(3)}) ${h.pixel_count}px`).join(" | ");
      r += `\n`;
    }
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
    addTyping();
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
      if (result.timeline && result.timeline.length > fullTimeline.length) {
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

    // Generate AI response
    addTyping();
    const report = generateInsight(currentQuery);
    const response = await getAIResponse(currentQuery, report);
    await streamText(response);
    updateTimelineUI();

  } catch (err) {
    if (err.name === "AbortError") return;
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
function inferNeededDatasets(question) {
  const q = question.toLowerCase();
  const needed = new Set();

  // Always include fire if asking about fire or cause/risk
  if (/fire|burn|blaze|wildfire|ignit/.test(q)) needed.add("fire");
  if (/flood|water|rain|river|inundat/.test(q)) needed.add("flood");
  if (/veget|ndvi|plant|green|dry|drought|fuel|biomass/.test(q)) needed.add("vegetation");
  if (/temp|heat|hot|cold|weather|climate|lst|thermal/.test(q)) needed.add("weather");
  if (/land.?use|urban|crop|forest|built|city|agriculture/.test(q)) needed.add("landuse");

  // Causal/risk/why questions need multiple datasets for explanation
  if (/why|cause|reason|explain|because|led to|trigger|risk|danger|threat/.test(q)) {
    // If asking about fire causes, pull supporting datasets
    if (needed.has("fire") || datasetResults.fire?.total_hexagons > 0) {
      needed.add("fire");
      needed.add("weather");
      needed.add("vegetation");
      needed.add("landuse");
    }
    // If asking about floods
    if (needed.has("flood") || datasetResults.flood?.total_hexagons > 0) {
      needed.add("flood");
      needed.add("weather");
      needed.add("vegetation");
    }
  }

  // Trend / temporal questions — keep what's already loaded + add context
  if (/trend|increas|decreas|spread|chang|over time|compar/.test(q)) {
    for (const ds of Object.keys(datasetResults)) {
      if (datasetResults[ds]?.total_hexagons > 0) needed.add(ds);
    }
    if (needed.has("fire")) { needed.add("weather"); needed.add("vegetation"); }
  }

  // Generic "what happened" / "analyze" — use all relevant context
  if (/what.*(happen|going|see)|analyz|summar|overview|assess/.test(q)) {
    needed.add("fire");
    needed.add("weather");
    needed.add("vegetation");
  }

  // If nothing inferred, at least use whatever is already loaded
  if (needed.size === 0) {
    for (const ds of Object.keys(datasetResults)) {
      if (datasetResults[ds]?.total_hexagons > 0) needed.add(ds);
    }
  }

  return [...needed];
}

// Auto-fetch missing datasets needed to answer a question
async function handleAgentQuestion(question) {
  // Run LLM classification and geocoding in parallel for speed
  const classifyPromise = fetch(`${API_BASE}/datasets/classify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  }).then(r => r.ok ? r.json() : null).catch(() => null);

  // ─── Extract date range from question ───
  const extractedRange = extractDateRange(question);
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
  const placeQuery = extractPlaceName(question);
  if (placeQuery) {
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
          const [w, s, e, n] = bbox;
          map.fitBounds([[s, w], [n, e]], { padding: [40, 40], animate: true });
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

  // Await LLM classification with a fast timeout — don't let it block
  const classifyTimeout = new Promise(r => setTimeout(() => r(null), 2000));
  const classifyResult = await Promise.race([classifyPromise, classifyTimeout]);
  const needed = classifyResult?.categories?.length
    ? classifyResult.categories
    : inferNeededDatasets(question); // fallback to keyword-based (instant)

  // Update UI checkboxes to match LLM selection
  for (const cb of $$("#dataset-grid input[type='checkbox']")) {
    cb.checked = needed.includes(cb.value);
  }

  // If no categories determined, just respond with current data
  if (needed.length === 0) {
    removeTyping();
    const report = generateInsight(question);
    const response = await getAIResponse(question, report);
    await streamText(response);
    return;
  }

  // If still no date range, can't fetch
  if (!queryDateRange) {
    removeTyping();
    await streamText("I need a date range to look at. Set the 'From' and 'To' dates on the left, then ask me again.");
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

  // Check per-dataset cache — only fetch what's actually missing
  const { cached: agentCached, missing: agentMissing, cachedTimeline: agentCachedTimeline } =
    cacheLookupDatasets(bbox, res, queryDateRange, needed);

  // Apply cached datasets immediately
  if (Object.keys(agentCached).length > 0) {
    for (const [ds, data] of Object.entries(agentCached)) {
      datasetResults[ds] = data;
      const cb = $(`#dataset-grid input[value="${ds}"]`);
      if (cb && !cb.checked) cb.checked = true;
      const layer = datasetLayers[ds];
      if (layer && !map.hasLayer(layer)) layer.addTo(map);
      renderDatasetHexagons(ds, data.hexagons || []);
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
    addTyping();
    const report = generateInsight(question);
    const response = await getAIResponse(question, report);
    await streamText(response);
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
        const cb = $(`#dataset-grid input[value="${dsKey}"]`);
        if (cb && !cb.checked) cb.checked = true;
        const layer = datasetLayers[dsKey];
        if (layer && !map.hasLayer(layer)) layer.addTo(map);
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
      if (result.timeline && result.timeline.length > fullTimeline.length) {
        fullTimeline = result.timeline;
        timeline = applyFrequencyStep(fullTimeline);
      }
      lastFetchBbox = [...bbox];
      lastFetchRes = res;
      lastFetchKey = makeFetchKey(bbox, res, queryDateRange, Object.keys(datasetResults));
      // Cache newly fetched datasets individually
      cacheStoreDatasets(bbox, res, queryDateRange, result.datasets, timeline);
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
      return;
    }
    console.warn("Agent fetch failed:", err);
    removeTyping();
    addChat("agent", `I had trouble fetching the data: ${err.message}`);
    isFetching = false;
    setFetching(false);
    hideLoading();
    return;
  }

  isFetching = false;
  setFetching(false);
  hideLoading();
  if (askHint) askHint.classList.add("hidden");

  // Generate natural AI response
  removeTyping();
  addTyping();
  const agentReport = generateInsight(question);
  const response = await getAIResponse(question, agentReport);
  await streamText(response);
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

  // Last resort: strip all action/dataset/time words and see if a place remains
  const stripped = q
    .replace(/\b(show|find|search|analyze|get|fetch|plot|display|check|look|tell|me|about|what|where|why|how|is|are|the|any|some|this|that)\b/gi, "")
    .replace(/\b(fire|flood|vegetation|weather|land\s*use|temperature|heat|ndvi|risk|danger|activity|data|detections?|conditions?|status|report|trends?|history)\b/gi, "")
    .replace(/\b(last|next|past|from|to|over|between|during)\b/gi, "")
    .replace(/\b\d+\s*(km|min|minutes?|hours?|days?|weeks?|months?|years?)\b/gi, "")
    .replace(/[?!.,;:]+/g, "")
    .replace(/\s+/g, " ")
    .trim();

  return stripped.length >= 3 ? stripped : null;
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
        renderDatasetHexagons(ds, datasetResults[ds].hexagons || []);
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
