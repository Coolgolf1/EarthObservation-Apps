const isLocalStaticFrontend =
  (window.location.hostname === "127.0.0.1" ||
    window.location.hostname === "localhost") &&
  window.location.port !== "8000";

const API_BASE =
  window.location.protocol === "file:" || isLocalStaticFrontend
    ? "http://127.0.0.1:8000"
    : window.location.origin;

const MAPBOX_TOKEN =
  "pk.eyJ1IjoiYWx2YXJvcG94b29wbGUiLCJhIjoiY21xZXo3NjVyMDc2ejMycjFkeXZ6ZDRkYiJ9.uKcKNDOsSr3vgkz62RZNSw";

const map = L.map("map", { zoomControl: true }).setView([40.7, -122.9], 7);
L.tileLayer(
  "https://api.mapbox.com/styles/v1/{id}/tiles/{z}/{x}/{y}?access_token={accessToken}",
  {
    id: "mapbox/satellite-streets-v12",
    accessToken: MAPBOX_TOKEN,
    tileSize: 512,
    zoomOffset: -1,
    maxZoom: 20,
    attribution:
      '&copy; <a href="https://www.mapbox.com/about/maps/">Mapbox</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  },
).addTo(map);

const dateStartEl = document.getElementById("date-start");
const dateEndEl = document.getElementById("date-end");
const confidenceEl = document.getElementById("confidence");
const bboxToggleEl = document.getElementById("bbox-toggle");
const bboxClearEl = document.getElementById("bbox-clear");
const loadFireEl = document.getElementById("load-fire");
const timelineEl = document.getElementById("timeline");
const timelineDayEl = document.getElementById("timeline-day");
const timelineCountEl = document.getElementById("timeline-count");
const statusEl = document.getElementById("status");
const prevDayEl = document.getElementById("prev-day");
const nextDayEl = document.getElementById("next-day");
const playEl = document.getElementById("play");
const pauseEl = document.getElementById("pause");

const bboxSelectionLayer = L.layerGroup().addTo(map);
let fireTileLayer = null;

let bboxDrawMode = false;
let bboxDrawPoints = [];
let resolvedBbox = null;
let fireFrames = [];
let timelineDates = [];
let countsByDate = {};
let playTimer = null;

function updateStatus(text) {
  statusEl.textContent = text;
}

function clearFireLayer() {
  if (fireTileLayer) {
    map.removeLayer(fireTileLayer);
    fireTileLayer = null;
  }
}

function stopPlayback() {
  if (playTimer) {
    clearInterval(playTimer);
    playTimer = null;
  }
}

function drawBboxSelection() {
  bboxSelectionLayer.clearLayers();

  for (const p of bboxDrawPoints) {
    L.circleMarker([p.lat, p.lng], {
      radius: 4,
      color: "#172554",
      fillColor: "#2563eb",
      fillOpacity: 0.9,
      weight: 1,
      interactive: false,
    }).addTo(bboxSelectionLayer);
  }

  if (bboxDrawPoints.length >= 2) {
    L.polyline(
      bboxDrawPoints.map((p) => [p.lat, p.lng]),
      {
        color: "#1e293b",
        weight: 2,
        dashArray: "5 4",
        interactive: false,
      },
    ).addTo(bboxSelectionLayer);
  }

  if (bboxDrawPoints.length >= 3) {
    const hull = computeConvexHull(bboxDrawPoints);
    L.polygon(
      hull.map((p) => [p.lat, p.lng]),
      {
        color: "#1e293b",
        weight: 2,
        fillColor: "#38bdf8",
        fillOpacity: 0.14,
        interactive: false,
      },
    ).addTo(bboxSelectionLayer);
  }
}

function computeConvexHull(points) {
  if (!Array.isArray(points) || points.length < 3) {
    return Array.isArray(points) ? [...points] : [];
  }

  const sorted = [...points].sort((a, b) =>
    a.lng === b.lng ? a.lat - b.lat : a.lng - b.lng,
  );

  const cross = (o, a, b) =>
    (a.lng - o.lng) * (b.lat - o.lat) - (a.lat - o.lat) * (b.lng - o.lng);

  const lower = [];
  for (const p of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) {
      lower.pop();
    }
    lower.push(p);
  }

  const upper = [];
  for (let idx = sorted.length - 1; idx >= 0; idx -= 1) {
    const p = sorted[idx];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) {
      upper.pop();
    }
    upper.push(p);
  }

  lower.pop();
  upper.pop();
  return [...lower, ...upper];
}

function resolveEnvelopeBbox() {
  if (bboxDrawPoints.length >= 3) {
    const hull = computeConvexHull(bboxDrawPoints);
    const lons = hull.map((p) => Number(p.lng));
    const lats = hull.map((p) => Number(p.lat));
    resolvedBbox = [
      Math.min(...lons),
      Math.min(...lats),
      Math.max(...lons),
      Math.max(...lats),
    ];
    return resolvedBbox;
  }

  const bounds = map.getBounds();
  resolvedBbox = [
    bounds.getWest(),
    bounds.getSouth(),
    bounds.getEast(),
    bounds.getNorth(),
  ];
  return resolvedBbox;
}

function refreshTimelineUI() {
  const hasTimeline = timelineDates.length > 0;
  timelineEl.disabled = !hasTimeline;
  timelineEl.max = hasTimeline ? String(timelineDates.length - 1) : "0";
  if (!hasTimeline) {
    timelineEl.value = "0";
    timelineDayEl.textContent = "No data loaded";
    timelineCountEl.textContent = "0 points";
    return;
  }

  const idx = Math.max(0, Math.min(Number(timelineEl.value), timelineDates.length - 1));
  timelineEl.value = String(idx);
  const day = timelineDates[idx];
  timelineDayEl.textContent = day;
  timelineCountEl.textContent = `${Number(countsByDate[day] || 0)} points`;
}

function renderCurrentDay() {
  clearFireLayer();
  if (!timelineDates.length) {
    refreshTimelineUI();
    return;
  }

  const idx = Number(timelineEl.value);
  const day = timelineDates[idx];

  const frame = fireFrames.find((f) => f?.acq_date === day);
  if (frame?.tilejson) {
    fetch(frame.tilejson)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`TileJSON ${response.status}`);
        }
        return response.json();
      })
      .then((tilePayload) => {
        const tileUrl = Array.isArray(tilePayload?.tiles) ? tilePayload.tiles[0] : null;
        if (!tileUrl) {
          return;
        }
        fireTileLayer = L.tileLayer(tileUrl, {
          opacity: 0.72,
          attribution: "MODIS FireMask",
        }).addTo(map);
      })
      .catch(() => {
        updateStatus(`Unable to render tile layer for ${day}.`);
      });
  }

  refreshTimelineUI();
}

async function loadFireData() {
  stopPlayback();
  clearFireLayer();

  const dateStart = String(dateStartEl.value || "").trim();
  const dateEnd = String(dateEndEl.value || "").trim();
  if (!dateStart || !dateEnd) {
    updateStatus("Choose both start and end date.");
    return;
  }

  const bbox = resolveEnvelopeBbox();
  updateStatus("Loading MODIS fire detections...");

  try {
    const response = await fetch(`${API_BASE}/fire/modis14a2/query`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        bbox,
        date_start: dateStart,
        date_end: dateEnd,
        max_items: 24,
        step_days: 8,
      }),
    });

    if (!response.ok) {
      const bodyText = await response.text();
      throw new Error(`Backend error ${response.status}: ${bodyText}`);
    }

    const payload = await response.json();
    fireFrames = Array.isArray(payload?.frames)
      ? payload.frames
      : [];
    timelineDates = Array.isArray(payload?.timeline) ? payload.timeline : [];
    countsByDate = payload?.counts_by_date || {};

    timelineEl.value = "0";
    renderCurrentDay();

    const pointsText = `${fireFrames.length} fire frames`;
    const daysText = `${timelineDates.length} day steps`;
    updateStatus(`Loaded ${pointsText} across ${daysText} for bbox [${bbox.map((v) => v.toFixed(3)).join(", ")}].`);
  } catch (error) {
    fireFrames = [];
    timelineDates = [];
    countsByDate = {};
    refreshTimelineUI();
    updateStatus(`Load failed: ${error.message}`);
  }
}

bboxToggleEl.addEventListener("click", () => {
  bboxDrawMode = !bboxDrawMode;
  bboxToggleEl.textContent = bboxDrawMode ? "Stop Bbox Select" : "Start Bbox Select";
  if (bboxDrawMode) {
    updateStatus("Bbox draw active: click at least 3 points on the map.");
  } else {
    updateStatus("Bbox draw stopped.");
  }
});

bboxClearEl.addEventListener("click", () => {
  bboxDrawPoints = [];
  bboxSelectionLayer.clearLayers();
  resolvedBbox = null;
  updateStatus("Bbox selection cleared.");
});

map.on("click", (event) => {
  if (!bboxDrawMode) {
    return;
  }
  bboxDrawPoints.push({ lat: event.latlng.lat, lng: event.latlng.lng });
  drawBboxSelection();
  if (bboxDrawPoints.length >= 3) {
    const bbox = resolveEnvelopeBbox();
    updateStatus(`Bbox ready: [${bbox.map((v) => v.toFixed(3)).join(", ")}]. Press Load fire detections.`);
  } else {
    updateStatus("Point added. Add at least 2 more points to define bbox.");
  }
});

loadFireEl.addEventListener("click", () => {
  void loadFireData();
});

timelineEl.addEventListener("input", () => {
  renderCurrentDay();
});

prevDayEl.addEventListener("click", () => {
  stopPlayback();
  if (!timelineDates.length) {
    return;
  }
  const idx = Math.max(0, Number(timelineEl.value) - 1);
  timelineEl.value = String(idx);
  renderCurrentDay();
});

nextDayEl.addEventListener("click", () => {
  stopPlayback();
  if (!timelineDates.length) {
    return;
  }
  const idx = Math.min(timelineDates.length - 1, Number(timelineEl.value) + 1);
  timelineEl.value = String(idx);
  renderCurrentDay();
});

playEl.addEventListener("click", () => {
  if (!timelineDates.length || playTimer) {
    return;
  }
  playTimer = setInterval(() => {
    const current = Number(timelineEl.value);
    const next = current + 1;
    if (next >= timelineDates.length) {
      stopPlayback();
      return;
    }
    timelineEl.value = String(next);
    renderCurrentDay();
  }, 700);
});

pauseEl.addEventListener("click", () => {
  stopPlayback();
});

refreshTimelineUI();
