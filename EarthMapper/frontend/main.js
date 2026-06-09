const API_BASE = "http://127.0.0.1:8000";

const map = L.map("map", {
  zoomControl: true,
}).setView([45, 10], 5);

const canvasRenderer = L.canvas({ padding: 0.5 });

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 15,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

const statsEl = document.getElementById("stats");
const categoryEl = document.getElementById("category");
const limitEl = document.getElementById("limit");
const formEl = document.getElementById("filters-form");

const geoJsonLayer = L.geoJSON([], {
  renderer: canvasRenderer,
  style: () => {
    const zoom = map.getZoom();
    const fillOpacity = zoom <= 6 ? 0.5 : 0.35;
    const weight = zoom <= 6 ? 2 : 1;
    return {
      color: "#1d3557",
      weight,
      fillColor: "#2a9d8f",
      fillOpacity,
    };
  },
  pointToLayer: (_, latlng) =>
    L.circleMarker(latlng, {
      renderer: canvasRenderer,
      radius: 5,
      color: "#0f5132",
      fillColor: "#52b788",
      fillOpacity: 0.9,
      weight: 1,
    }),
  onEachFeature: (feature, featureLayer) => {
    featureLayer.on("click", () => {
      if (featureLayer.getPopup()) {
        return;
      }

      const props = feature.properties || {};
      const title = props.name || props.label || props.category || "Feature";
      const details = Object.entries(props)
        .slice(0, 6)
        .map(([k, v]) => `<strong>${k}</strong>: ${String(v)}`)
        .join("<br>");
      featureLayer.bindPopup(`<div><h4>${title}</h4><div>${details}</div></div>`);
    });
  },
});

geoJsonLayer.addTo(map);

const featureCache = new Map();
const renderedFeatureIds = new Set();

let activeController = null;
let debounceTimer = null;
let loadVersion = 0;
let activeRequestBbox = null;
let pendingDeferredLoad = false;

const SMALL_MOVE_RATIO = 0.18;

function clearQueuedRequests() {
  if (debounceTimer) {
    clearTimeout(debounceTimer);
    debounceTimer = null;
  }
}

function makeFeatureId(feature) {
  const props = feature.properties || {};
  if (props.source != null && props.id != null) {
    return `${String(props.source)}:${String(props.id)}`;
  }
  return `${JSON.stringify(feature.geometry || {})}|${String(props.name || "")}`;
}

function getBboxFromMap() {
  const bounds = map.getBounds();
  return {
    west: bounds.getWest(),
    south: bounds.getSouth(),
    east: bounds.getEast(),
    north: bounds.getNorth(),
  };
}

function isSmallBboxMove(previousBbox, nextBbox) {
  if (!previousBbox || !nextBbox) {
    return false;
  }

  const prevWidth = Math.max(1e-9, Math.abs(previousBbox.east - previousBbox.west));
  const prevHeight = Math.max(1e-9, Math.abs(previousBbox.north - previousBbox.south));

  const prevCx = (previousBbox.east + previousBbox.west) / 2;
  const prevCy = (previousBbox.north + previousBbox.south) / 2;
  const nextCx = (nextBbox.east + nextBbox.west) / 2;
  const nextCy = (nextBbox.north + nextBbox.south) / 2;

  const dxRatio = Math.abs(nextCx - prevCx) / prevWidth;
  const dyRatio = Math.abs(nextCy - prevCy) / prevHeight;

  return dxRatio <= SMALL_MOVE_RATIO && dyRatio <= SMALL_MOVE_RATIO;
}

function featureInBounds(feature, bounds) {
  const geometry = feature.geometry || {};
  if (geometry.type !== "Point") {
    const extents = getGeometryExtents(geometry);
    if (!extents) {
      return false;
    }

    const geometryBounds = L.latLngBounds(
      [extents.minLat, extents.minLon],
      [extents.maxLat, extents.maxLon],
    );

    return bounds.intersects(geometryBounds);
  }

  const coords = geometry.coordinates || [];
  if (!Array.isArray(coords) || coords.length < 2) {
    return false;
  }

  const lon = Number(coords[0]);
  const lat = Number(coords[1]);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
    return false;
  }

  return bounds.contains(L.latLng(lat, lon));
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

  if (node.length >= 2 && typeof node[0] === "number" && typeof node[1] === "number") {
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
    !Number.isFinite(extents.minLon)
    || !Number.isFinite(extents.maxLon)
    || !Number.isFinite(extents.minLat)
    || !Number.isFinite(extents.maxLat)
  ) {
    return null;
  }

  return extents;
}

function layerInBounds(layer, bounds) {
  if (typeof layer.getLatLng === "function") {
    return bounds.contains(layer.getLatLng());
  }

  if (typeof layer.getBounds === "function") {
    return bounds.intersects(layer.getBounds());
  }

  return featureInBounds(layer.feature || {}, bounds);
}

function addFeatures(features) {
  if (features.length === 0) {
    return 0;
  }

  geoJsonLayer.addData({
    type: "FeatureCollection",
    features,
  });

  for (const feature of features) {
    renderedFeatureIds.add(makeFeatureId(feature));
  }

  return features.length;
}

function renderCacheForViewport() {
  const bounds = map.getBounds();
  const batch = [];

  for (const [featureId, feature] of featureCache.entries()) {
    if (renderedFeatureIds.has(featureId)) {
      continue;
    }
    if (!featureInBounds(feature, bounds)) {
      continue;
    }
    batch.push(feature);
  }

  return addFeatures(batch);
}

function pruneNonVisiblePlottedFeatures() {
  const bounds = map.getBounds();
  const toRemove = [];

  geoJsonLayer.eachLayer((layer) => {
    if (layerInBounds(layer, bounds)) {
      return;
    }

    toRemove.push(layer);
  });

  for (const layer of toRemove) {
    const featureId = makeFeatureId(layer.feature || {});
    renderedFeatureIds.delete(featureId);
    geoJsonLayer.removeLayer(layer);
  }

  return toRemove.length;
}

function updateStats(prefix = "") {
  const text = `Rendered ${renderedFeatureIds.size} | Cached ${featureCache.size}`;
  statsEl.textContent = prefix ? `${prefix} | ${text}` : text;
}

async function streamCurrentViewport(requestBbox = null) {
  const bbox = requestBbox || getBboxFromMap();

  if (activeController) {
    if (isSmallBboxMove(activeRequestBbox, bbox)) {
      pendingDeferredLoad = true;
      updateStats("Minor move: waiting current load");
      return;
    }

    activeController.abort();
  }

  const controller = new AbortController();
  activeController = controller;
  activeRequestBbox = bbox;
  pendingDeferredLoad = false;
  const currentLoadVersion = ++loadVersion;

  const params = new URLSearchParams({
    bbox: `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`,
    limit: String(Math.max(1, Number(limitEl.value) || 1000)),
  });

  const category = categoryEl.value.trim();
  if (category) {
    params.set("category", category);
  }

  const url = `${API_BASE}/features/stream?${params.toString()}`;
  let streamAdded = 0;

  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        Accept: "application/x-ndjson",
      },
    });

    if (!response.ok || !response.body) {
      throw new Error(`API error ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let textBuffer = "";
    let batch = [];
    const BATCH_SIZE = 100;

    while (true) {
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

        let parsed;
        try {
          parsed = JSON.parse(line);
        } catch {
          continue;
        }

        if (parsed.meta && parsed.meta.done) {
          continue;
        }

        const featureId = makeFeatureId(parsed);
        if (!featureCache.has(featureId)) {
          featureCache.set(featureId, parsed);
        }

        if (!renderedFeatureIds.has(featureId) && featureInBounds(parsed, map.getBounds())) {
          batch.push(parsed);
          if (batch.length >= BATCH_SIZE) {
            streamAdded += addFeatures(batch);
            batch = [];
            updateStats(`Streaming +${streamAdded}`);
          }
        }
      }
    }

    if (textBuffer.trim()) {
      try {
        const parsed = JSON.parse(textBuffer.trim());
        if (!(parsed.meta && parsed.meta.done)) {
          const featureId = makeFeatureId(parsed);
          if (!featureCache.has(featureId)) {
            featureCache.set(featureId, parsed);
          }
          if (!renderedFeatureIds.has(featureId) && featureInBounds(parsed, map.getBounds())) {
            batch.push(parsed);
          }
        }
      } catch {
        // Ignore trailing partial line.
      }
    }

    streamAdded += addFeatures(batch);
    updateStats(`Stream done +${streamAdded}`);
  } catch (error) {
    if (error.name !== "AbortError") {
      statsEl.textContent = `Request failed: ${error.message}`;
      console.error(error);
    }
  } finally {
    if (activeController === controller && loadVersion === currentLoadVersion) {
      activeController = null;
      activeRequestBbox = null;

      if (pendingDeferredLoad) {
        pendingDeferredLoad = false;
        loadFeatures();
      }
    }
  }
}

function loadFeatures() {
  const bbox = getBboxFromMap();
  const pruned = pruneNonVisiblePlottedFeatures();
  const fromCache = renderCacheForViewport();
  updateStats(`Pruned ${pruned} | Cache-first +${fromCache}`);
  void streamCurrentViewport(bbox);
}

function runLoadCycle() {
  loadFeatures();
}

function scheduleLoad() {
  clearQueuedRequests();
  debounceTimer = setTimeout(runLoadCycle, 250);
}

function resetCachesAndLayer() {
  if (activeController) {
    activeController.abort();
    activeController = null;
  }

  activeRequestBbox = null;
  pendingDeferredLoad = false;

  featureCache.clear();
  renderedFeatureIds.clear();
  geoJsonLayer.clearLayers();
  updateStats("Cache reset");
}

map.on("moveend", scheduleLoad);

categoryEl.addEventListener("change", () => {
  clearQueuedRequests();
  resetCachesAndLayer();
  runLoadCycle();
});

limitEl.addEventListener("change", () => {
  clearQueuedRequests();
  resetCachesAndLayer();
  runLoadCycle();
});

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  clearQueuedRequests();
  resetCachesAndLayer();
  runLoadCycle();
});

runLoadCycle();
