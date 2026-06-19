const API_BASE = "http://127.0.0.1:8080";
const MAPBOX_TOKEN =
  "pk.eyJ1IjoiYWx2YXJvcG94b29wbGUiLCJhIjoiY21xZXo3NjVyMDc2ejMycjFkeXZ6ZDRkYiJ9.uKcKNDOsSr3vgkz62RZNSw";

const map = L.map("map", {
  zoomControl: true,
}).setView([45, 10], 5);

const canvasRenderer = L.canvas({ padding: 0.5 });

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
const categoryEl = document.getElementById("category");
const formEl = document.getElementById("filters-form");

const FRONTEND_LIMIT = 50000;
const LOW_ZOOM_NAMED_PLACES_THRESHOLD = 6;
const MOVE_DEBOUNCE_MS = 250;
const ADMIN_LIMIT_BY_DETAIL = {
  z4: 1500,
  z7: 6000,
  z10: 18000,
  full: FRONTEND_LIMIT,
};
const TRANSPORT_LIMIT_BY_ZOOM = {
  far: 2500,
  mid: 8000,
  near: 18000,
  full: FRONTEND_LIMIT,
};
const GEOMETRY_DETAIL_RANK = {
  z4: 1,
  z7: 2,
  z10: 3,
  full: 4,
};

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
  terrain: {
    stroke: "#8b5a2b",
    fill: "#a67c52",
    point: "#8b5a2b",
    fillOpacity: 0.4,
  },
  regions: {
    stroke: "#0f766e",
    fill: "#14b8a6",
    point: "#0f766e",
    fillOpacity: 0.3,
  },
  landmarks: {
    stroke: "#d62728",
    fill: "#ef4444",
    point: "#d62728",
    fillOpacity: 0.5,
  },
  default: {
    stroke: "#1d3557",
    fill: "#2a9d8f",
    point: "#0f5132",
    fillOpacity: 0.35,
  },
};

const POPUP_FIELD_PRIORITY = [
  "source",
  "category",
  "subcategory",
  "country_name",
  "country_code",
  "admin_level",
  "admin_type_en",
  "admin_id",
  "admin1_name",
  "admin2_name",
  "admin3_name",
  "min_zoom",
  "label_min_zoom",
  "display_rank",
  "importance_score",
];

function currentCategoryPalette(feature = null) {
  const cat = String(feature?.properties?.category || categoryEl.value || "")
    .trim()
    .toLowerCase();
  return CATEGORY_STYLE[cat] || CATEGORY_STYLE.default;
}

const geoJsonLayer = L.geoJSON([], {
  renderer: canvasRenderer,
  style: (feature) => {
    const palette = currentCategoryPalette(feature);
    const geometryType = feature?.geometry?.type || "";
    const featureCategory = String(feature?.properties?.category || "")
      .trim()
      .toLowerCase();
    const zoom = map.getZoom();

    if (geometryType.includes("LineString")) {
      let lineWeight = zoom <= 6 ? 2.2 : 1.5;
      if (featureCategory === "transport") {
        if (zoom <= 5) {
          lineWeight = 0.8;
        } else if (zoom <= 7) {
          lineWeight = 1.2;
        } else if (zoom <= 9) {
          lineWeight = 1.8;
        } else {
          lineWeight = 3.0;
        }
      }
      if (featureCategory === "hydrography") {
        lineWeight = zoom <= 6 ? 3.6 : 2.9;
      }
      return {
        color: palette.stroke,
        weight: lineWeight,
        fillOpacity: 0,
      };
    }

    if (featureCategory === "administrative") {
      const adminLevel = Number(feature?.properties?.admin_level);
      const levelWeight = Number.isFinite(adminLevel)
        ? Math.max(0.7, 2.3 - adminLevel * 0.25)
        : 1.2;
      return {
        color: palette.stroke,
        weight: zoom <= 6 ? Math.max(1.2, levelWeight) : levelWeight,
        fillColor: "transparent",
        fillOpacity: 0,
      };
    }

    return {
      color: palette.stroke,
      weight: zoom <= 6 ? 2 : 1,
      fillColor: palette.fill,
      fillOpacity: Math.min(zoom <= 6 ? 0.5 : 0.35, palette.fillOpacity),
    };
  },
  pointToLayer: (feature, latlng) => {
    const palette = currentCategoryPalette(feature);
    return L.circleMarker(latlng, {
      renderer: canvasRenderer,
      radius: 5,
      color: palette.stroke,
      fillColor: palette.point,
      fillOpacity: 0.9,
      weight: 1,
    });
  },
  onEachFeature: (feature, featureLayer) => {
    featureLayer.on("click", () => {
      if (featureLayer.getPopup()) {
        return;
      }

      const props = feature.properties || {};
      const title =
        props.admin_name ||
        props.name ||
        props.label ||
        props.category ||
        "Feature";
      const preferred = POPUP_FIELD_PRIORITY.filter(
        (field) => props[field] != null && props[field] !== "",
      ).map((field) => [field, props[field]]);
      const fallback = Object.entries(props).filter(
        ([field, value]) =>
          value != null &&
          value !== "" &&
          !POPUP_FIELD_PRIORITY.includes(field) &&
          field !== "id",
      );
      const details = [...preferred, ...fallback]
        .slice(0, 10)
        .map(([k, v]) => `<strong>${k}</strong>: ${String(v)}`)
        .join("<br>");
      featureLayer.bindPopup(
        `<div><h4>${title}</h4><div>${details}</div></div>`,
      );
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
let activeGeometryDetail = getGeometryDetailForZoom(map.getZoom());

const SMALL_MOVE_RATIO = 0.1;

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

function hasNonEmptyFeatureName(feature) {
  const rawName = feature?.properties?.name;
  return typeof rawName === "string" && rawName.trim().length > 0;
}

function shouldRestrictPlacesToNamed() {
  return map.getZoom() <= LOW_ZOOM_NAMED_PLACES_THRESHOLD;
}

function getGeometryDetailForZoom(zoom) {
  if (zoom <= 8) {
    return "z7";
  }
  if (zoom <= 11) {
    return "z10";
  }
  return "full";
}

function getRequestLimitForCategory(category) {
  const normalizedCategory = String(category || "").toLowerCase();
  if (normalizedCategory === "administrative") {
    const detail = getGeometryDetailForZoom(map.getZoom());
    return ADMIN_LIMIT_BY_DETAIL[detail] || FRONTEND_LIMIT;
  }
  if (normalizedCategory === "transport") {
    const zoom = map.getZoom();
    if (zoom <= 5) {
      return TRANSPORT_LIMIT_BY_ZOOM.far;
    }
    if (zoom <= 7) {
      return TRANSPORT_LIMIT_BY_ZOOM.mid;
    }
    if (zoom <= 9) {
      return TRANSPORT_LIMIT_BY_ZOOM.near;
    }
    return TRANSPORT_LIMIT_BY_ZOOM.full;
  }
  return FRONTEND_LIMIT;
}

function getFeatureGeometryDetail(feature) {
  return String(feature?.properties?.geom_detail || "full").toLowerCase();
}

function getFeatureDetailRank(feature) {
  return GEOMETRY_DETAIL_RANK[getFeatureGeometryDetail(feature)] || 0;
}

function shouldReplaceCachedFeature(existingFeature, nextFeature) {
  if (!existingFeature) {
    return true;
  }

  const existingRank = getFeatureDetailRank(existingFeature);
  const nextRank = getFeatureDetailRank(nextFeature);
  return nextRank > existingRank;
}

function rememberFeature(feature) {
  const featureId = makeFeatureId(feature);
  const existingFeature = featureCache.get(featureId);
  if (shouldReplaceCachedFeature(existingFeature, feature)) {
    featureCache.set(featureId, feature);
    return true;
  }
  return false;
}

function isNamedPlacesRuleSatisfied(feature) {
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

function isZoomRuleSatisfied(feature) {
  const props = feature?.properties || {};
  const minZoom = Number(props.min_zoom);
  if (!Number.isFinite(minZoom)) {
    return true;
  }
  return map.getZoom() >= minZoom;
}

function shouldRenderFeature(feature) {
  return (
    isCategoryGeometryAllowed(feature) &&
    isNamedPlacesRuleSatisfied(feature) &&
    isZoomRuleSatisfied(feature)
  );
}

function isCategoryGeometryAllowed(feature) {
  const category = String(feature?.properties?.category || "")
    .trim()
    .toLowerCase();
  const geometryType = String(feature?.geometry?.type || "");

  if (category === "places") {
    return (
      geometryType === "Point" ||
      geometryType === "MultiPoint" ||
      geometryType === "Polygon" ||
      geometryType === "MultiPolygon"
    );
  }

  if (category === "administrative") {
    return geometryType === "Polygon" || geometryType === "MultiPolygon";
  }

  return true;
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

  const prevWidth = Math.max(
    1e-9,
    Math.abs(previousBbox.east - previousBbox.west),
  );
  const prevHeight = Math.max(
    1e-9,
    Math.abs(previousBbox.north - previousBbox.south),
  );

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

  const filtered = features.filter((feature) => shouldRenderFeature(feature));
  if (filtered.length === 0) {
    return 0;
  }

  geoJsonLayer.addData({
    type: "FeatureCollection",
    features: filtered,
  });

  for (const feature of filtered) {
    renderedFeatureIds.add(makeFeatureId(feature));
  }

  return filtered.length;
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
    if (!shouldRenderFeature(feature)) {
      continue;
    }
    batch.push(feature);
  }

  return addFeatures(batch);
}

function clearRenderedFeatures() {
  renderedFeatureIds.clear();
  geoJsonLayer.clearLayers();
}

function pruneNonVisiblePlottedFeatures() {
  const bounds = map.getBounds();
  const toRemove = [];

  geoJsonLayer.eachLayer((layer) => {
    if (
      layerInBounds(layer, bounds) &&
      shouldRenderFeature(layer.feature || {})
    ) {
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

function delay(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function streamCurrentViewport(requestBbox = null, retryCount = 0) {
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
    limit: String(getRequestLimitForCategory(categoryEl.value.trim())),
    zoom: String(map.getZoom()),
  });

  const category = categoryEl.value.trim();
  if (category) {
    params.set("category", category);
  }
  if (
    (!category || category.toLowerCase() === "places") &&
    shouldRestrictPlacesToNamed()
  ) {
    params.set("places_named_only", "true");
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

    if (response.status === 429 && retryCount < 3) {
      updateStats("Server busy: replacing oldest request");
      if (activeController === controller) {
        activeController = null;
        activeRequestBbox = null;
      }
      await delay(180 * (retryCount + 1));
      void streamCurrentViewport(bbox, retryCount + 1);
      return;
    }

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
        const cacheReplaced = rememberFeature(parsed);
        if (!shouldRenderFeature(parsed)) {
          continue;
        }

        if (
          (!renderedFeatureIds.has(featureId) || cacheReplaced) &&
          featureInBounds(parsed, map.getBounds())
        ) {
          if (cacheReplaced && renderedFeatureIds.has(featureId)) {
            clearRenderedFeatures();
            streamAdded += renderCacheForViewport();
            continue;
          }
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
        if (!(parsed.meta && parsed.meta.done) && shouldRenderFeature(parsed)) {
          const featureId = makeFeatureId(parsed);
          const cacheReplaced = rememberFeature(parsed);
          if (
            (!renderedFeatureIds.has(featureId) || cacheReplaced) &&
            featureInBounds(parsed, map.getBounds())
          ) {
            if (cacheReplaced && renderedFeatureIds.has(featureId)) {
              clearRenderedFeatures();
              streamAdded += renderCacheForViewport();
            } else {
              batch.push(parsed);
            }
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
  const nextGeometryDetail = getGeometryDetailForZoom(map.getZoom());
  if (nextGeometryDetail !== activeGeometryDetail) {
    activeGeometryDetail = nextGeometryDetail;
    clearRenderedFeatures();
  }
  loadFeatures();
}

function scheduleLoad() {
  clearQueuedRequests();
  debounceTimer = setTimeout(runLoadCycle, MOVE_DEBOUNCE_MS);
}

function resetCachesAndLayer(showMessage = true) {
  if (activeController) {
    activeController.abort();
    activeController = null;
  }

  activeRequestBbox = null;
  pendingDeferredLoad = false;

  featureCache.clear();
  renderedFeatureIds.clear();
  geoJsonLayer.clearLayers();
  if (showMessage) {
    updateStats("Cache reset");
  }
}

map.on("moveend", scheduleLoad);

categoryEl.addEventListener("change", () => {
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
