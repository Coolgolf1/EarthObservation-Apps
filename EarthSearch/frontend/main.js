const isLocalStaticFrontend =
  (window.location.hostname === "127.0.0.1" ||
    window.location.hostname === "localhost") &&
  window.location.port !== "8080";

const API_BASE =
  window.location.protocol === "file:" || isLocalStaticFrontend
    ? "http://127.0.0.1:8080"
    : window.location.origin;
const MAPBOX_TOKEN =
  "pk.eyJ1IjoiYWx2YXJvcG94b29wbGUiLCJhIjoiY21xZXo3NjVyMDc2ejMycjFkeXZ6ZDRkYiJ9.uKcKNDOsSr3vgkz62RZNSw";

function isHydroFeatureAllowed(feature) {
  const geometryType = String(feature?.geometry?.type || "");
  const props = feature?.properties || {};
  const text =
    `${String(props.name || "")} ${String(props.subcategory || "")}`.toLowerCase();

  if (geometryType.includes("LineString")) {
    if (text.includes("coast") || text.includes("shore")) {
      return false;
    }
    return (
      text.includes("river") ||
      text.includes("stream") ||
      text.includes("canal") ||
      text.includes("waterway")
    );
  }

  if (geometryType.includes("Polygon")) {
    return text.includes("ocean") || text.includes("sea");
  }

  return false;
}

function formatFeatureHoverText(feature) {
  const props = feature?.properties || {};
  const name = String(props.name || "Unnamed feature");
  const category = String(props.category || "unknown");
  const subcategory = String(props.subcategory || "");
  const source = String(props.source || "");

  const lines = [name, `Category: ${category}`];
  if (subcategory) {
    lines.push(`Subcategory: ${subcategory}`);
  }
  if (source) {
    lines.push(`Source: ${source}`);
  }

  return lines.join("<br>");
}

const SEARCH_CATEGORIES = [
  {
    category: "transport",
    label: "transport",
    limit: 50000,
  },
  {
    category: "hydrography",
    label: "rivers/ocean",
    featureFilter: isHydroFeatureAllowed,
    limit: 50000,
  },
  {
    category: "places",
    label: "places",
    limit: 50000,
  },
];

const STREAMING_LIMIT = 50000;
const LOW_ZOOM_NAMED_PLACES_THRESHOLD = 6;

const CATEGORY_STYLE = {
  hydrography: {
    stroke: "#1f77b4",
    fill: "#4aa3df",
    point: "#1f77b4",
    fillOpacity: 0.7,
  },
  administrative: {
    stroke: "#2ca25f",
    fill: "transparent",
    point: "#2ca25f",
    fillOpacity: 0.0,
  },
  transport: {
    stroke: "#facc15",
    fill: "#facc15",
    point: "#facc15",
    fillOpacity: 0.45,
  },
  places: {
    stroke: "#f59e0b",
    fill: "#f59e0b",
    point: "#f59e0b",
    fillOpacity: 0.35,
  },
  default: {
    stroke: "#1d3557",
    fill: "#2a9d8f",
    point: "#0f5132",
    fillOpacity: 0.35,
  },
};

const map = L.map("map", {
  zoomControl: true,
}).setView([45, 10], 5);

map.createPane("placesPane");
map.getPane("placesPane").style.zIndex = "350";
map.createPane("transportPane");
map.getPane("transportPane").style.zIndex = "410";
map.createPane("hydroPane");
map.getPane("hydroPane").style.zIndex = "420";
map.createPane("adminPane");
map.getPane("adminPane").style.zIndex = "430";
map.createPane("reachAreaPane");
map.getPane("reachAreaPane").style.zIndex = "500";
map.getPane("reachAreaPane").style.pointerEvents = "none";

const canvasRenderer = L.canvas({ padding: 0.5 });

function getCategoryPane(category) {
  const cat = String(category || "")
    .trim()
    .toLowerCase();
  if (cat === "places") {
    return "placesPane";
  }
  if (cat === "transport") {
    return "transportPane";
  }
  if (cat === "hydrography") {
    return "hydroPane";
  }
  if (cat === "administrative") {
    return "adminPane";
  }
  return "overlayPane";
}

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

const statsEl = document.getElementById("stats");
const formEl = document.getElementById("search-form");
const promptEl = document.getElementById("agent-prompt");
const vehicleEl = document.getElementById("vehicle");
const areaValueLabelEl = document.getElementById("area-value-label");
const areaModeToggleEl = document.getElementById("area-mode-toggle");
const areaValueEl = document.getElementById("area-value");
const areaControlsEl = document.querySelector(".area-controls");
const bboxDrawToggleEl = document.getElementById("bbox-draw-toggle");
const bboxClearEl = document.getElementById("bbox-clear");
const confirmPointWrapEl = document.getElementById("confirm-point-wrap");
const confirmPointBtnEl = document.getElementById("confirm-point-btn");

const geoJsonLayer = L.geoJSON([], {
  renderer: canvasRenderer,
  style: (feature) => {
    const cat = String(feature?.properties?.category || "")
      .trim()
      .toLowerCase();
    const palette = CATEGORY_STYLE[cat] || CATEGORY_STYLE.default;
    const geometryType = feature?.geometry?.type || "";
    const zoom = map.getZoom();

    if (geometryType.includes("LineString")) {
      let lineWeight = zoom <= 6 ? 2.2 : 1.5;
      if (cat === "transport") {
        lineWeight = zoom <= 6 ? 4.6 : 3.6;
      }
      if (cat === "hydrography") {
        lineWeight = zoom <= 6 ? 3.6 : 2.9;
      }
      return {
        pane: getCategoryPane(cat),
        color: palette.stroke,
        weight: lineWeight,
        fillOpacity: 0,
      };
    }

    if (cat === "administrative") {
      return {
        pane: getCategoryPane(cat),
        color: palette.stroke,
        weight: zoom <= 6 ? 1.8 : 1.2,
        fillColor: "transparent",
        fillOpacity: 0,
      };
    }

    return {
      pane: getCategoryPane(cat),
      color: palette.stroke,
      weight: zoom <= 6 ? 2 : 1,
      fillColor: palette.fill,
      fillOpacity: palette.fillOpacity,
    };
  },
  pointToLayer: (feature, latlng) => {
    const cat = String(feature?.properties?.category || "")
      .trim()
      .toLowerCase();
    const palette = CATEGORY_STYLE[cat] || CATEGORY_STYLE.default;
    return L.circleMarker(latlng, {
      pane: getCategoryPane(cat),
      renderer: canvasRenderer,
      radius: 5,
      color: palette.stroke,
      fillColor: palette.point,
      fillOpacity: 0.9,
      weight: 1,
    });
  },
  onEachFeature: (feature, layer) => {
    layer.bindTooltip(formatFeatureHoverText(feature), {
      sticky: true,
      opacity: 0.95,
      direction: "top",
      className: "feature-hover-tooltip",
    });

    layer.on("mouseover", () => {
      if (typeof layer.openTooltip === "function") {
        layer.openTooltip();
      }
    });
    layer.on("mouseout", () => {
      if (typeof layer.closeTooltip === "function") {
        layer.closeTooltip();
      }
    });
  },
});
geoJsonLayer.addTo(map);

const reachAreaLayer = L.geoJSON([], {
  pane: "reachAreaPane",
  renderer: canvasRenderer,
  interactive: false,
  style: {
    color: "#111827",
    weight: 2,
    fillColor: "#111827",
    fillOpacity: 0.08,
    dashArray: "6 4",
  },
});
reachAreaLayer.addTo(map);

function keepReachAreaOnTop() {
  if (reachAreaLayer && typeof reachAreaLayer.bringToFront === "function") {
    reachAreaLayer.bringToFront();
  }
}

const centerMarkerLayer = L.layerGroup().addTo(map);
const bboxSelectionLayer = L.layerGroup().addTo(map);
const pendingPointLayer = L.layerGroup().addTo(map);
const cachedFeaturesById = new Map();
const plottedFeatureIds = new Set();

let activeController = null;
let runVersion = 0;
let pendingSearchTimeout = null;
let pendingSearchMoveendHandler = null;
let promptChangeDebounce = null;
let streamInFlight = false;
let activePlotBbox = null;
let areaMode = "time";
let bboxDrawMode = false;
let bboxDrawPoints = [];
let pendingPoint = null;
const clientSessionId = (() => {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `client-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
})();

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function fetchWith429Retry(
  url,
  options,
  { retries = 1, retryDelayMs = 1000 } = {},
) {
  let attempt = 0;
  while (true) {
    const response = await fetch(url, options);
    if (response.status !== 429 || attempt >= retries) {
      return response;
    }
    attempt += 1;
    await sleep(retryDelayMs);
  }
}

function updateStats(text) {
  statsEl.textContent = text;
}

function getMapBoundsArray() {
  const bounds = map.getBounds();
  return [
    bounds.getWest(),
    bounds.getSouth(),
    bounds.getEast(),
    bounds.getNorth(),
  ];
}

function applyAreaModeUI() {
  if (areaMode === "radius") {
    areaValueLabelEl.textContent = "Radius (km)";
    areaModeToggleEl.textContent = "Switch to Time";
    areaModeToggleEl.setAttribute("aria-pressed", "true");
    areaValueEl.min = "0.5";
    areaValueEl.max = "500";
    areaValueEl.step = "0.1";
    const numeric = Math.max(0.5, Number(areaValueEl.value) || 10);
    areaValueEl.value = String(Number(numeric.toFixed(1)));
    return;
  }

  areaValueLabelEl.textContent = "Time (minutes)";
  areaModeToggleEl.textContent = "Switch to Km";
  areaModeToggleEl.setAttribute("aria-pressed", "false");
  areaValueEl.min = "1";
  areaValueEl.max = "240";
  areaValueEl.step = "1";
  const numeric = Math.max(1, Math.round(Number(areaValueEl.value) || 30));
  areaValueEl.value = String(numeric);
}

function updateBBoxDrawUI() {
  bboxDrawToggleEl.textContent = bboxDrawMode
    ? "Stop Polygon Select"
    : "Start Polygon Select";
  bboxDrawToggleEl.setAttribute(
    "aria-pressed",
    bboxDrawMode ? "true" : "false",
  );

  const hideAreaInputs = bboxDrawMode;
  areaValueLabelEl.style.display = hideAreaInputs ? "none" : "";
  if (areaControlsEl) {
    areaControlsEl.style.display = hideAreaInputs ? "none" : "grid";
  }

  refreshConfirmButtonVisibility();
}

function clearBBoxSelection() {
  bboxDrawPoints = [];
  bboxSelectionLayer.clearLayers();
  refreshConfirmButtonVisibility();
}

function showConfirmPointButton() {
  confirmPointWrapEl.classList.remove("hidden");
}

function hideConfirmPointButton() {
  confirmPointWrapEl.classList.add("hidden");
}

function refreshConfirmButtonVisibility() {
  const hasPendingPoint = pendingPoint != null;
  const hasPolygonReady = bboxDrawMode && getPolygonFromPoints() != null;
  if (hasPendingPoint || hasPolygonReady) {
    showConfirmPointButton();
  } else {
    hideConfirmPointButton();
  }
}

function setPendingPoint(lat, lon) {
  pendingPoint = { lat, lon };
  pendingPointLayer.clearLayers();
  L.circleMarker([lat, lon], {
    radius: 5,
    color: "#0f172a",
    fillColor: "#22c55e",
    fillOpacity: 0.95,
    weight: 1,
  }).addTo(pendingPointLayer);
  refreshConfirmButtonVisibility();
}

function clearPendingPoint() {
  pendingPoint = null;
  pendingPointLayer.clearLayers();
  refreshConfirmButtonVisibility();
}

function _cross(o, a, b) {
  return (a.lng - o.lng) * (b.lat - o.lat) - (a.lat - o.lat) * (b.lng - o.lng);
}

function computeConvexHull(points) {
  if (!Array.isArray(points) || points.length <= 1) {
    return Array.isArray(points) ? [...points] : [];
  }

  const sorted = [...points].sort((p1, p2) => {
    if (p1.lng !== p2.lng) {
      return p1.lng - p2.lng;
    }
    return p1.lat - p2.lat;
  });

  const lower = [];
  for (const p of sorted) {
    while (
      lower.length >= 2 &&
      _cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0
    ) {
      lower.pop();
    }
    lower.push(p);
  }

  const upper = [];
  for (let i = sorted.length - 1; i >= 0; i -= 1) {
    const p = sorted[i];
    while (
      upper.length >= 2 &&
      _cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0
    ) {
      upper.pop();
    }
    upper.push(p);
  }

  lower.pop();
  upper.pop();
  return [...lower, ...upper];
}

function getPolygonFromPoints() {
  if (!Array.isArray(bboxDrawPoints) || bboxDrawPoints.length < 3) {
    return null;
  }

  const hull = computeConvexHull(bboxDrawPoints);
  if (hull.length < 3) {
    return null;
  }

  return hull.map((p) => [Number(p.lng), Number(p.lat)]);
}

function redrawBBoxSelection() {
  bboxSelectionLayer.clearLayers();
  if (!bboxDrawPoints.length) {
    refreshConfirmButtonVisibility();
    return;
  }

  for (const p of bboxDrawPoints) {
    L.circleMarker([p.lat, p.lng], {
      radius: 4,
      color: "#0f172a",
      fillColor: "#2563eb",
      fillOpacity: 0.9,
      weight: 1,
      interactive: false,
    }).addTo(bboxSelectionLayer);
  }

  const hull = computeConvexHull(bboxDrawPoints);

  if (hull.length >= 2) {
    L.polyline(
      hull.map((p) => [p.lat, p.lng]),
      {
        color: "#1f2937",
        weight: 2,
        dashArray: "6 4",
        interactive: false,
      },
    ).addTo(bboxSelectionLayer);
  }

  if (hull.length >= 3) {
    L.polygon(
      hull.map((p) => [p.lat, p.lng]),
      {
        color: "#1f2937",
        weight: 2,
        fillColor: "#60a5fa",
        fillOpacity: 0.12,
        interactive: false,
      },
    ).addTo(bboxSelectionLayer);
  }

  refreshConfirmButtonVisibility();
}

function getAreaSelection() {
  const normalizedValue = String(areaValueEl.value || "")
    .replace(",", ".")
    .trim();
  const raw = Number(normalizedValue);
  if (areaMode === "radius") {
    return {
      areaMode: "radius",
      minutes: 30,
      radiusKm: Math.max(0.5, Number.isFinite(raw) ? raw : 10),
    };
  }

  return {
    areaMode: "time",
    minutes: Math.max(1, Number.isFinite(raw) ? Math.round(raw) : 30),
    radiusKm: 10,
  };
}

function syncControlsFromIntent(intent) {
  if (!intent || typeof intent !== "object") {
    return;
  }

  const mode = String(intent.mode || "time").toLowerCase();
  if (mode === "radius") {
    areaMode = "radius";
    const resolvedRadiusKm = Number(intent.radius_km);
    if (Number.isFinite(resolvedRadiusKm)) {
      areaValueEl.value = String(
        Math.max(0.5, Math.min(500, resolvedRadiusKm)),
      );
    }
  } else {
    areaMode = "time";
    const resolvedMinutes = Number(intent.minutes);
    if (Number.isFinite(resolvedMinutes)) {
      areaValueEl.value = String(
        Math.max(1, Math.min(240, Math.round(resolvedMinutes))),
      );
    }
  }

  applyAreaModeUI();

  const resolvedVehicle = String(intent.vehicle || "")
    .trim()
    .toLowerCase();
  if (
    ["walking", "cycling", "driving", "driving-hgv"].includes(resolvedVehicle)
  ) {
    vehicleEl.value = resolvedVehicle;
  }
}

function cancelPending() {
  runVersion += 1;
  if (activeController) {
    activeController.abort();
    activeController = null;
  }
  if (pendingSearchTimeout) {
    clearTimeout(pendingSearchTimeout);
    pendingSearchTimeout = null;
  }
  if (pendingSearchMoveendHandler) {
    map.off("moveend", pendingSearchMoveendHandler);
    pendingSearchMoveendHandler = null;
  }

  activePlotBbox = null;

  // Immediately clear previous results when a new search cycle begins.
  clearMap();
}

function clearMap() {
  geoJsonLayer.clearLayers();
  reachAreaLayer.clearLayers();
  centerMarkerLayer.clearLayers();
  plottedFeatureIds.clear();
}

function makeFeatureId(feature) {
  const props = feature?.properties || {};
  if (props.source != null && props.id != null) {
    return `${String(props.source)}:${String(props.id)}`;
  }
  return `${JSON.stringify(feature?.geometry || {})}|${String(props.name || "")}`;
}

function hasNonEmptyFeatureName(feature) {
  const rawName = feature?.properties?.name;
  return typeof rawName === "string" && rawName.trim().length > 0;
}

function shouldRestrictPlacesToNamed() {
  return map.getZoom() <= LOW_ZOOM_NAMED_PLACES_THRESHOLD;
}

function isSeaOrOceanFeature(feature) {
  const category = String(feature?.properties?.category || "")
    .trim()
    .toLowerCase();
  if (category !== "hydrography") {
    return false;
  }

  const geometryType = String(feature?.geometry?.type || "");
  if (!geometryType.includes("Polygon")) {
    return false;
  }

  const props = feature?.properties || {};
  const text =
    `${String(props.name || "")} ${String(props.subcategory || "")}`.toLowerCase();
  return text.includes("sea") || text.includes("ocean");
}

function featureEligibleForSearchCache(feature) {
  const category = String(feature?.properties?.category || "")
    .trim()
    .toLowerCase();
  const spec = _findCategorySpec(category);
  if (!spec) {
    return false;
  }

  if (
    typeof spec.featureFilter === "function" &&
    !spec.featureFilter(feature)
  ) {
    return false;
  }

  return true;
}

function featureVisibleAtCurrentZoom(feature) {
  const category = String(feature?.properties?.category || "")
    .trim()
    .toLowerCase();
  if (category !== "places") {
    return true;
  }
  if (!shouldRestrictPlacesToNamed()) {
    return true;
  }
  return hasNonEmptyFeatureName(feature);
}

function featureRelevantToBbox(feature, bbox) {
  if (!bbox) {
    return true;
  }

  if (!featureIntersectsBbox(feature, bbox)) {
    return false;
  }
  return true;
}

function addFeatures(features) {
  if (!features.length) {
    return 0;
  }

  const toPlot = [];
  for (const feature of features) {
    if (!featureEligibleForSearchCache(feature)) {
      continue;
    }

    if (!featureRelevantToBbox(feature, activePlotBbox)) {
      continue;
    }

    const featureId = makeFeatureId(feature);
    cachedFeaturesById.set(featureId, feature);

    if (!featureVisibleAtCurrentZoom(feature)) {
      continue;
    }

    if (plottedFeatureIds.has(featureId)) {
      continue;
    }

    plottedFeatureIds.add(featureId);
    toPlot.push(feature);
  }

  if (!toPlot.length) {
    return 0;
  }

  geoJsonLayer.addData({
    type: "FeatureCollection",
    features: toPlot,
  });

  keepReachAreaOnTop();

  return toPlot.length;
}

function loadCachedFeaturesForBbox(bbox) {
  const cached = [];

  for (const feature of cachedFeaturesById.values()) {
    if (!featureIntersectsBbox(feature, bbox)) {
      continue;
    }
    cached.push(feature);
  }

  return addFeatures(cached);
}

function rerenderFromCacheForCurrentViewport() {
  if (!activePlotBbox) {
    return;
  }

  const bounds = map.getBounds();
  const viewportBbox = {
    west: bounds.getWest(),
    south: bounds.getSouth(),
    east: bounds.getEast(),
    north: bounds.getNorth(),
  };
  geoJsonLayer.clearLayers();
  plottedFeatureIds.clear();

  const visibleFromCache = [];
  for (const feature of cachedFeaturesById.values()) {
    if (!featureIntersectsBbox(feature, activePlotBbox)) {
      continue;
    }
    if (!featureIntersectsBbox(feature, viewportBbox)) {
      continue;
    }
    visibleFromCache.push(feature);
  }

  const rendered = addFeatures(visibleFromCache);
  updateStats(`Zoom refresh from cache +${rendered}`);
}

function bboxContainsPoint(bbox, lon, lat) {
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
    return false;
  }
  return (
    lon >= bbox.west &&
    lon <= bbox.east &&
    lat >= bbox.south &&
    lat <= bbox.north
  );
}

function updateExtents(extents, lon, lat) {
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
    return;
  }
  extents.minLon = Math.min(extents.minLon, lon);
  extents.maxLon = Math.max(extents.maxLon, lon);
  extents.minLat = Math.min(extents.minLat, lat);
  extents.maxLat = Math.max(extents.maxLat, lat);
}

function walkCoordinates(node, extents) {
  if (!Array.isArray(node)) {
    return;
  }

  if (
    node.length >= 2 &&
    typeof node[0] === "number" &&
    typeof node[1] === "number"
  ) {
    updateExtents(extents, Number(node[0]), Number(node[1]));
    return;
  }

  for (const child of node) {
    walkCoordinates(child, extents);
  }
}

function getGeometryExtents(geometry) {
  if (!geometry || geometry.coordinates == null) {
    return null;
  }

  const extents = {
    minLon: Infinity,
    maxLon: -Infinity,
    minLat: Infinity,
    maxLat: -Infinity,
  };

  walkCoordinates(geometry.coordinates, extents);

  if (
    !Number.isFinite(extents.minLon) ||
    !Number.isFinite(extents.maxLon) ||
    !Number.isFinite(extents.minLat) ||
    !Number.isFinite(extents.maxLat)
  ) {
    return null;
  }

  return extents;
}

function extentsIntersectBbox(extents, bbox) {
  if (!extents) {
    return false;
  }

  return !(
    extents.maxLon < bbox.west ||
    extents.minLon > bbox.east ||
    extents.maxLat < bbox.south ||
    extents.minLat > bbox.north
  );
}

function featureIntersectsBbox(feature, bbox) {
  const geometry = feature?.geometry || {};
  if (geometry.type === "Point") {
    const coords = geometry.coordinates || [];
    if (!Array.isArray(coords) || coords.length < 2) {
      return false;
    }
    return bboxContainsPoint(bbox, Number(coords[0]), Number(coords[1]));
  }

  return extentsIntersectBbox(getGeometryExtents(geometry), bbox);
}

function _findCategorySpec(category) {
  return SEARCH_CATEGORIES.find((s) => s.category === category) || null;
}

async function streamSingleCategory({
  bbox,
  spec,
  signal,
  shouldContinue,
  onProgress,
  clientId,
  searchId,
  countsByLabel,
  addedRef,
  limit,
}) {
  if (!shouldContinue()) {
    throw new DOMException("Search cancelled", "AbortError");
  }

  const params = new URLSearchParams({
    bbox: `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`,
    category: spec.category,
    limit: String(limit),
    client_id: clientId,
    search_id: searchId,
  });
  if (spec.category === "places" && shouldRestrictPlacesToNamed()) {
    params.set("places_named_only", "true");
  }

  const response = await fetchWith429Retry(
    `${API_BASE}/features/stream?${params.toString()}`,
    {
      signal,
      headers: {
        Accept: "application/x-ndjson",
      },
    },
    {
      retries: 1,
      retryDelayMs: 1000,
    },
  );

  if (!response.ok || !response.body) {
    throw new Error(`Stream API error ${response.status} for ${spec.label}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let textBuffer = "";
  let batch = [];
  const BATCH_SIZE = 100;

  while (true) {
    if (!shouldContinue()) {
      throw new DOMException("Search cancelled", "AbortError");
    }

    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    textBuffer += decoder.decode(value, { stream: true });
    const lines = textBuffer.split("\n");
    textBuffer = lines.pop() || "";

    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) {
        continue;
      }

      if (!shouldContinue()) {
        throw new DOMException("Search cancelled", "AbortError");
      }

      let parsed;
      try {
        parsed = JSON.parse(line);
      } catch {
        continue;
      }

      if (parsed.meta && parsed.meta.done) {
        continue;
      }

      if (
        typeof spec.featureFilter === "function" &&
        !spec.featureFilter(parsed)
      ) {
        continue;
      }

      if (
        spec.category === "places" &&
        shouldRestrictPlacesToNamed() &&
        !hasNonEmptyFeatureName(parsed)
      ) {
        continue;
      }

      if (!featureRelevantToBbox(parsed, bbox)) {
        continue;
      }

      batch.push(parsed);
      countsByLabel.set(spec.label, (countsByLabel.get(spec.label) || 0) + 1);
      if (batch.length >= BATCH_SIZE) {
        if (!shouldContinue()) {
          throw new DOMException("Search cancelled", "AbortError");
        }
        addedRef.value += addFeatures(batch);
        batch = [];
        onProgress(addedRef.value, countsByLabel);
      }
    }
  }

  if (textBuffer.trim()) {
    try {
      const parsed = JSON.parse(textBuffer.trim());
      if (!(parsed.meta && parsed.meta.done)) {
        if (
          (typeof spec.featureFilter !== "function" ||
            spec.featureFilter(parsed)) &&
          (!(spec.category === "places" && shouldRestrictPlacesToNamed()) ||
            hasNonEmptyFeatureName(parsed)) &&
          featureRelevantToBbox(parsed, bbox)
        ) {
          batch.push(parsed);
          countsByLabel.set(
            spec.label,
            (countsByLabel.get(spec.label) || 0) + 1,
          );
        }
      }
    } catch {
      // Ignore trailing partial line.
    }
  }

  if (!shouldContinue()) {
    throw new DOMException("Search cancelled", "AbortError");
  }

  addedRef.value += addFeatures(batch);
  onProgress(addedRef.value, countsByLabel);
}

async function streamCombined({
  bbox,
  signal,
  shouldContinue,
  onProgress,
  clientId,
  searchId,
}) {
  const countsByLabel = new Map(SEARCH_CATEGORIES.map((s) => [s.label, 0]));
  const addedRef = { value: 0 };

  for (const spec of SEARCH_CATEGORIES) {
    if (!shouldContinue()) {
      throw new DOMException("Search cancelled", "AbortError");
    }

    await streamSingleCategory({
      bbox,
      spec,
      signal,
      shouldContinue,
      onProgress,
      clientId,
      searchId,
      countsByLabel,
      addedRef,
      limit: STREAMING_LIMIT,
    });
  }

  return addedRef.value;
}

async function runSearchRequest({ endpoint, body, loadingMessage }) {
  cancelPending();
  clearPendingPoint();
  const localRunVersion = runVersion;
  const searchId =
    globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
      ? globalThis.crypto.randomUUID()
      : `search-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

  const controller = new AbortController();
  activeController = controller;

  updateStats(loadingMessage);

  try {
    const response = await fetchWith429Retry(
      `${API_BASE}${endpoint}`,
      {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          ...body,
          client_id: clientSessionId,
          search_id: searchId,
        }),
      },
      {
        retries: 1,
        retryDelayMs: 1000,
      },
    );

    if (!response.ok) {
      const errorText = await response.text();
      const compactError = String(errorText || "request failed")
        .replace(/\s+/g, " ")
        .trim();
      const staticServerHint =
        response.status === 501 &&
        /Unsupported method \('POST'\)/i.test(compactError)
          ? " The request likely went to a static frontend server instead of FastAPI."
          : "";
      throw new Error(
        `API error ${response.status}: ${compactError}${staticServerHint}`,
      );
    }

    const payload = await response.json();
    syncControlsFromIntent(payload.parsed_intent);

    if (localRunVersion !== runVersion || controller.signal.aborted) {
      return;
    }

    clearMap();

    if (
      payload.reach_area &&
      payload.reach_area.type &&
      payload.reach_area.coordinates
    ) {
      reachAreaLayer.addData({
        type: "Feature",
        geometry: payload.reach_area,
        properties: { kind: "reach-area" },
      });
      keepReachAreaOnTop();
    }

    if (
      payload.center &&
      Number.isFinite(Number(payload.center.lat)) &&
      Number.isFinite(Number(payload.center.lon))
    ) {
      const starIcon = L.divIcon({
        className: "center-star-icon",
        html: "<span>★</span>",
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      });
      L.marker([Number(payload.center.lat), Number(payload.center.lon)], {
        icon: starIcon,
      })
        .bindPopup(`Center (${payload.center_source || "geocoder"})`)
        .addTo(centerMarkerLayer);
    }

    const bboxValues = Array.isArray(payload.bbox)
      ? payload.bbox.map((v) => Number(v))
      : null;
    if (
      !bboxValues ||
      bboxValues.length !== 4 ||
      !bboxValues.every(Number.isFinite)
    ) {
      updateStats("No valid bbox returned.");
      return;
    }

    const fitBounds = L.latLngBounds(
      [bboxValues[1], bboxValues[0]],
      [bboxValues[3], bboxValues[2]],
    );
    const searchBbox = {
      west: fitBounds.getWest(),
      south: fitBounds.getSouth(),
      east: fitBounds.getEast(),
      north: fitBounds.getNorth(),
    };
    activePlotBbox = searchBbox;

    const streamAfterZoom = async () => {
      if (localRunVersion !== runVersion || controller.signal.aborted) {
        return;
      }

      const shouldContinue = () =>
        localRunVersion === runVersion && !controller.signal.aborted;
      const progress = new Map(SEARCH_CATEGORIES.map((s) => [s.label, 0]));

      const intent = payload.parsed_intent || {};
      const mode = intent.mode || "default";
      const modeText =
        mode === "radius"
          ? `${Number(intent.radius_km || 0).toFixed(1)} km radius`
          : `${Number(intent.minutes || 0)} min ${intent.vehicle || "driving"}`;

      const cachedAdded = loadCachedFeaturesForBbox(searchBbox);
      updateStats(
        `Using cache +${cachedAdded}. Streaming categories for ${payload.place_name || "location"} (${modeText})...`,
      );

      streamInFlight = true;
      const total = await streamCombined({
        bbox: searchBbox,
        signal: controller.signal,
        shouldContinue,
        clientId: String(payload.client_id || clientSessionId),
        searchId: String(payload.search_id || searchId),
        onProgress: (loadedTotal, countsByLabel) => {
          if (!shouldContinue()) {
            return;
          }
          for (const [label, value] of countsByLabel.entries()) {
            progress.set(label, value);
          }
          const details = Array.from(progress.entries())
            .map(([label, value]) => `${label}: +${value}`)
            .join(" | ");
          updateStats(
            `${details} | cache +${cachedAdded} | streamed +${loadedTotal} | total +${cachedAdded + loadedTotal}`,
          );
        },
      });

      streamInFlight = false;

      updateStats(
        `Done (${payload.parser_mode || "agent"}). Cached +${cachedAdded} | streamed +${total} | total plotted: ${cachedAdded + total}`,
      );
    };

    // Fit immediately and start loading right away for a snappy submit action.
    map.fitBounds(fitBounds, { animate: false, padding: [24, 24] });
    void streamAfterZoom();
  } catch (error) {
    if (error.name !== "AbortError") {
      updateStats(`Search failed: ${error.message}`);
      console.error(error);
    }
    streamInFlight = false;
  } finally {
    if (activeController === controller) {
      activeController = null;
    }
  }
}

async function runSearch() {
  const selectedPolygon = getPolygonFromPoints();
  if (selectedPolygon) {
    await runBBoxSearch(selectedPolygon);
    return;
  }

  const prompt = promptEl.value.trim();
  if (!prompt) {
    updateStats(
      "Provide a location prompt, or draw a bbox and press Run search.",
    );
    return;
  }

  const area = getAreaSelection();
  await runSearchRequest({
    endpoint: "/features/agent-search",
    loadingMessage: "Running agentic planner...",
    body: {
      prompt,
      vehicle: vehicleEl.value,
      area_mode: area.areaMode,
      minutes: area.minutes,
      radius_km: area.radiusKm,
      metadata_only: true,
    },
  });
}

async function runBBoxSearch(bboxValues) {
  await runSearchRequest({
    endpoint: "/features/bbox-search",
    loadingMessage: "Running bbox selection search...",
    body: {
      polygon: bboxValues,
      metadata_only: true,
    },
  });
}

async function runPointSearch(lat, lon) {
  const area = getAreaSelection();
  await runSearchRequest({
    endpoint: "/features/point-search",
    loadingMessage: "Running map-click search...",
    body: {
      lat,
      lon,
      vehicle: vehicleEl.value,
      area_mode: area.areaMode,
      minutes: area.minutes,
      radius_km: area.radiusKm,
      metadata_only: true,
    },
  });
}

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  if (promptChangeDebounce) {
    clearTimeout(promptChangeDebounce);
    promptChangeDebounce = null;
  }
  void runSearch();
});

areaModeToggleEl.addEventListener("click", () => {
  areaMode = areaMode === "time" ? "radius" : "time";
  applyAreaModeUI();
});

bboxDrawToggleEl.addEventListener("click", () => {
  bboxDrawMode = !bboxDrawMode;
  if (bboxDrawMode) {
    clearPendingPoint();
  }
  updateBBoxDrawUI();
  if (bboxDrawMode) {
    updateStats(
      "Polygon select enabled: click multiple spots on the map, then press Run search.",
    );
  }
});

bboxClearEl.addEventListener("click", () => {
  clearBBoxSelection();
  updateStats("Polygon selection cleared.");
});

promptEl.addEventListener("input", () => {
  // Abort in-flight requests immediately when the prompt changes.
  cancelPending();
  updateStats("Prompt updated. Searching...");

  if (promptChangeDebounce) {
    clearTimeout(promptChangeDebounce);
    promptChangeDebounce = null;
  }

  promptChangeDebounce = setTimeout(() => {
    promptChangeDebounce = null;
    if (!promptEl.value.trim()) {
      updateStats("Provide a location prompt.");
      return;
    }
    void runSearch();
  }, 450);
});

map.on("click", (event) => {
  const lat = Number(event?.latlng?.lat);
  const lon = Number(event?.latlng?.lng);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    return;
  }

  if (bboxDrawMode) {
    bboxDrawPoints.push({ lat, lng: lon });
    redrawBBoxSelection();
    const selectedPolygon = getPolygonFromPoints();
    if (selectedPolygon) {
      updateStats(
        `Polygon ready (${selectedPolygon.length} points). Press Run search to load.`,
      );
    } else {
      updateStats("Polygon point added. Add at least two more points.");
    }
    return;
  }

  setPendingPoint(lat, lon);
  updateStats("Point selected. Press Confirm to run search.");
});

map.on("zoomend", () => {
  rerenderFromCacheForCurrentViewport();
});

confirmPointBtnEl.addEventListener("click", () => {
  if (promptChangeDebounce) {
    clearTimeout(promptChangeDebounce);
    promptChangeDebounce = null;
  }

  if (bboxDrawMode) {
    const polygon = getPolygonFromPoints();
    if (polygon) {
      void runBBoxSearch(polygon);
      return;
    }
  }

  if (pendingPoint) {
    void runPointSearch(pendingPoint.lat, pendingPoint.lon);
  }
});

applyAreaModeUI();
updateBBoxDrawUI();
refreshConfirmButtonVisibility();
