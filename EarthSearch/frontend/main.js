const API_BASE = "http://127.0.0.1:8000";

function isHydroFeatureAllowed(feature) {
  const geometryType = String(feature?.geometry?.type || "");
  const props = feature?.properties || {};
  const text = `${String(props.name || "")} ${String(props.subcategory || "")}`.toLowerCase();

  if (geometryType.includes("LineString")) {
    if (text.includes("coast") || text.includes("shore")) {
      return false;
    }
    return (
      text.includes("river")
      || text.includes("stream")
      || text.includes("canal")
      || text.includes("waterway")
    );
  }

  if (geometryType.includes("Polygon")) {
    return (
      text.includes("ocean")
      || text.includes("sea")
    );
  }

  return false;
}

const SEARCH_STREAM_SPECS = [
  { category: "transport", label: "transport", limit: 10 },
  {
    category: "hydrography",
    label: "rivers/ocean",
    limit: 10,
    featureFilter: isHydroFeatureAllowed,
  },
  { category: "places", label: "places", limit: 10 },
  { category: "administrative", label: "administrative", limit: 100 },
];

const CATEGORY_STYLE = {
  hydrography: { stroke: "#1f77b4", fill: "#4aa3df", point: "#1f77b4", fillOpacity: 0.7 },
  administrative: { stroke: "#2ca25f", fill: "transparent", point: "#2ca25f", fillOpacity: 0.0 },
  transport: { stroke: "#6b7280", fill: "#9ca3af", point: "#6b7280", fillOpacity: 0.45 },
  places: { stroke: "#f59e0b", fill: "#f59e0b", point: "#f59e0b", fillOpacity: 0.35 },
  default: { stroke: "#1d3557", fill: "#2a9d8f", point: "#0f5132", fillOpacity: 0.35 },
};

const map = L.map("map", {
  zoomControl: true,
}).setView([45, 10], 5);

const canvasRenderer = L.canvas({ padding: 0.5 });

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 15,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

const statsEl = document.getElementById("stats");
const formEl = document.getElementById("search-form");
const placeQueryEl = document.getElementById("place-query");
const minutesEl = document.getElementById("minutes");
const vehicleEl = document.getElementById("vehicle");
const searchBboxEl = document.getElementById("search-bbox");

const geoJsonLayer = L.geoJSON([], {
  renderer: canvasRenderer,
  style: (feature) => {
    const cat = String(feature?.properties?.category || "").trim().toLowerCase();
    const palette = CATEGORY_STYLE[cat] || CATEGORY_STYLE.default;
    const geometryType = feature?.geometry?.type || "";
    const zoom = map.getZoom();

    if (geometryType.includes("LineString")) {
      let lineWeight = zoom <= 6 ? 2.2 : 1.5;
      if (cat === "transport") {
        lineWeight = zoom <= 6 ? 3.2 : 2.6;
      }
      if (cat === "hydrography") {
        lineWeight = zoom <= 6 ? 3.0 : 2.4;
      }
      return {
        color: palette.stroke,
        weight: lineWeight,
        fillOpacity: 0,
      };
    }

    if (cat === "administrative") {
      return {
        color: palette.stroke,
        weight: zoom <= 6 ? 1.8 : 1.2,
        fillColor: "transparent",
        fillOpacity: 0,
      };
    }

    return {
      color: palette.stroke,
      weight: zoom <= 6 ? 2 : 1,
      fillColor: palette.fill,
      fillOpacity: palette.fillOpacity,
    };
  },
  pointToLayer: (feature, latlng) => {
    const cat = String(feature?.properties?.category || "").trim().toLowerCase();
    const palette = CATEGORY_STYLE[cat] || CATEGORY_STYLE.default;
    return L.circleMarker(latlng, {
      renderer: canvasRenderer,
      radius: 5,
      color: palette.stroke,
      fillColor: palette.point,
      fillOpacity: 0.9,
      weight: 1,
    });
  },
});
geoJsonLayer.addTo(map);

const reachAreaLayer = L.geoJSON([], {
  renderer: canvasRenderer,
  style: {
    color: "#111827",
    weight: 2,
    fillColor: "#111827",
    fillOpacity: 0.08,
    dashArray: "6 4",
  },
});
reachAreaLayer.addTo(map);

const centerMarkerLayer = L.layerGroup().addTo(map);

let activeController = null;
let runVersion = 0;
let pendingSearchTimeout = null;
let pendingSearchMoveendHandler = null;

function updateStats(text) {
  statsEl.textContent = text;
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
}

function clearMap() {
  geoJsonLayer.clearLayers();
  reachAreaLayer.clearLayers();
  centerMarkerLayer.clearLayers();
}

function addFeatures(features) {
  if (!features.length) {
    return 0;
  }
  geoJsonLayer.addData({
    type: "FeatureCollection",
    features,
  });
  return features.length;
}

function bboxContainsPoint(bbox, lon, lat) {
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
    return false;
  }
  return lon >= bbox.west && lon <= bbox.east && lat >= bbox.south && lat <= bbox.north;
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

function extentsIntersectBbox(extents, bbox) {
  if (!extents) {
    return false;
  }

  return !(
    extents.maxLon < bbox.west
    || extents.minLon > bbox.east
    || extents.maxLat < bbox.south
    || extents.minLat > bbox.north
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

async function streamCategory({ bbox, spec, signal, onProgress }) {
  const params = new URLSearchParams({
    bbox: `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`,
    category: spec.category,
    limit: String(spec.limit),
  });

  const response = await fetch(`${API_BASE}/features/stream?${params.toString()}`, {
    signal,
    headers: {
      Accept: "application/x-ndjson",
    },
  });

  if (!response.ok || !response.body) {
    throw new Error(`Stream API error ${response.status} for ${spec.label}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let textBuffer = "";
  let batch = [];
  let added = 0;
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

      if (typeof spec.featureFilter === "function" && !spec.featureFilter(parsed)) {
        continue;
      }

      if (!featureIntersectsBbox(parsed, bbox)) {
        continue;
      }

      batch.push(parsed);
      if (batch.length >= BATCH_SIZE) {
        added += addFeatures(batch);
        batch = [];
        onProgress(added);
      }
    }
  }

  if (textBuffer.trim()) {
    try {
      const parsed = JSON.parse(textBuffer.trim());
      if (!(parsed.meta && parsed.meta.done)) {
        if (
          (typeof spec.featureFilter !== "function" || spec.featureFilter(parsed))
          && featureIntersectsBbox(parsed, bbox)
        ) {
          batch.push(parsed);
        }
      }
    } catch {
      // Ignore trailing partial line.
    }
  }

  added += addFeatures(batch);
  onProgress(added);
  return added;
}

async function runSearch() {
  cancelPending();
  const localRunVersion = runVersion;

  const placeQuery = placeQueryEl.value.trim();
  if (!placeQuery) {
    updateStats("Provide a place query.");
    return;
  }

  const controller = new AbortController();
  activeController = controller;

  updateStats("Resolving search bbox...");

  const params = new URLSearchParams({
    place_query: placeQuery,
    minutes: String(Math.max(1, Number(minutesEl.value) || 30)),
    vehicle: vehicleEl.value,
    metadata_only: "true",
  });

  const rawBbox = searchBboxEl.value.trim();
  if (rawBbox) {
    params.set("bbox", rawBbox);
  }

  try {
    const response = await fetch(`${API_BASE}/features/search-mode?${params.toString()}`, {
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(`API error ${response.status}`);
    }

    const payload = await response.json();

    if (localRunVersion !== runVersion || controller.signal.aborted) {
      return;
    }

    clearMap();

    if (payload.reach_area && payload.reach_area.type && payload.reach_area.coordinates) {
      reachAreaLayer.addData({
        type: "Feature",
        geometry: payload.reach_area,
        properties: { kind: "reach-area" },
      });
    }

    if (payload.center && Number.isFinite(Number(payload.center.lat)) && Number.isFinite(Number(payload.center.lon))) {
      const starIcon = L.divIcon({
        className: "center-star-icon",
        html: "<span>★</span>",
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      });
      L.marker([Number(payload.center.lat), Number(payload.center.lon)], { icon: starIcon })
        .bindPopup(`Center (${payload.center_source || "geocoder"})`)
        .addTo(centerMarkerLayer);
    }

    const bboxValues = Array.isArray(payload.bbox) ? payload.bbox.map((v) => Number(v)) : null;
    if (!bboxValues || bboxValues.length !== 4 || !bboxValues.every(Number.isFinite)) {
      updateStats("No valid bbox returned.");
      return;
    }

    const fitBounds = L.latLngBounds([bboxValues[1], bboxValues[0]], [bboxValues[3], bboxValues[2]]);
    const searchBbox = {
      west: fitBounds.getWest(),
      south: fitBounds.getSouth(),
      east: fitBounds.getEast(),
      north: fitBounds.getNorth(),
    };

    const streamAfterZoom = async () => {
      if (localRunVersion !== runVersion || controller.signal.aborted) {
        return;
      }

      const progress = new Map();
      for (const spec of SEARCH_STREAM_SPECS) {
        progress.set(spec.label, 0);
      }

      updateStats("Streaming requested categories...");

      const jobs = SEARCH_STREAM_SPECS.map((spec) =>
        streamCategory({
          bbox: searchBbox,
          spec,
          signal: controller.signal,
          onProgress: (delta) => {
            progress.set(spec.label, delta);
            const total = Array.from(progress.values()).reduce((sum, n) => sum + n, 0);
            updateStats(`${spec.label}: +${delta} | total +${total}`);
          },
        })
      );

      const results = await Promise.all(jobs);
      const total = results.reduce((sum, n) => sum + n, 0);
      updateStats(`Done. Total plotted: ${total}`);
    };

    let started = false;
    const start = () => {
      if (started) {
        return;
      }
      started = true;
      pendingSearchTimeout = null;
      pendingSearchMoveendHandler = null;
      void streamAfterZoom();
    };

    pendingSearchMoveendHandler = start;
    map.once("moveend", start);
    map.fitBounds(fitBounds, { animate: true, padding: [24, 24] });
    pendingSearchTimeout = setTimeout(start, 700);
  } catch (error) {
    if (error.name !== "AbortError") {
      updateStats(`Search failed: ${error.message}`);
      console.error(error);
    }
  } finally {
    if (activeController === controller) {
      activeController = null;
    }
  }
}

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  void runSearch();
});
