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

// Initialize WebWorker for background parsing
let parseWorker = null;
if (typeof Worker !== "undefined") {
  parseWorker = new Worker("parse-worker.js");
}

let geoJsonLayer = L.geoJSON([], {
  renderer: canvasRenderer,
  style: (feature) => {
    const zoom = map.getZoom();
    // Make polygons more visible at lower zoom levels
    const fillOpacity = zoom <= 6 ? 0.5 : 0.35;
    const weight = zoom <= 6 ? 2 : 1;
    return {
      color: "#1d3557",
      weight: weight,
      fillColor: "#2a9d8f",
      fillOpacity: fillOpacity,
    };
  },
  pointToLayer: (feature, latlng) =>
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

// Render canvas layer directly to map
geoJsonLayer.addTo(map);

let debounceTimer = null;
let inFlight = false;
let pendingLoad = false;

function clearQueuedRequests() {
  if (debounceTimer) {
    clearTimeout(debounceTimer);
    debounceTimer = null;
  }
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

async function loadFeatures() {
  inFlight = true;

  const bbox = getBboxFromMap();
  const params = new URLSearchParams({
    bbox: `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`,
    limit: String(Math.max(1, Number(limitEl.value) || 1000)),
  });

  const category = categoryEl.value.trim();
  if (category) {
    params.set("category", category);
  }

  const url = `${API_BASE}/features?${params.toString()}`;
  const startedAt = performance.now();

  try {
    statsEl.textContent = "Loading features...";
    const response = await fetch(url);

    if (!response.ok) {
      throw new Error(`API error ${response.status}`);
    }

    const features = await response.json();

    // Use WebWorker for parsing if available, otherwise process on main thread
    if (parseWorker) {
      parseWorker.onmessage = (event) => {
        if (!event.data.success) {
          throw new Error(event.data.error);
        }

        const viewportFeatures = event.data.features;
        const featureCollection = {
          type: "FeatureCollection",
          features: viewportFeatures,
        };

        geoJsonLayer.clearLayers();
        geoJsonLayer.addData(featureCollection);

        const elapsedMs = Math.round(performance.now() - startedAt);
        statsEl.textContent = `Loaded ${viewportFeatures.length} features in ${elapsedMs} ms`;
      };

      parseWorker.postMessage({ features, mapBounds: bbox });
    } else {
      const mapBounds = map.getBounds();
      const viewportFeatures = features.filter((feature) => {
        const geometry = feature.geometry || {};
        if (geometry.type === "Point") {
          const coords = geometry.coordinates || [];
          const point = L.latLng(coords[1], coords[0]);
          return mapBounds.contains(point);
        }
        return true;
      });

      const featureCollection = {
        type: "FeatureCollection",
        features: viewportFeatures,
      };

      geoJsonLayer.clearLayers();
      geoJsonLayer.addData(featureCollection);

      const elapsedMs = Math.round(performance.now() - startedAt);
      statsEl.textContent = `Loaded ${viewportFeatures.length} features in ${elapsedMs} ms`;
    }
  } catch (error) {
    statsEl.textContent = `Request failed: ${error.message}`;
    console.error(error);
  } finally {
    inFlight = false;
    if (pendingLoad) {
      pendingLoad = false;
      runLoadCycle();
    }
  }
}

function runLoadCycle() {
  if (inFlight) {
    pendingLoad = true;
    return;
  }

  loadFeatures();
}

function scheduleLoad() {
  clearQueuedRequests();
  debounceTimer = setTimeout(runLoadCycle, 350);
}

map.on("moveend", scheduleLoad);
categoryEl.addEventListener("change", () => {
  clearQueuedRequests();
  runLoadCycle();
});
limitEl.addEventListener("change", () => {
  clearQueuedRequests();
  runLoadCycle();
});
formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  clearQueuedRequests();
  runLoadCycle();
});

runLoadCycle();
