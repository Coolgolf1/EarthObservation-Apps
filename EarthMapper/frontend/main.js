const API_BASE = "http://127.0.0.1:8000";

const map = L.map("map", {
  zoomControl: true,
}).setView([45, 10], 5);

const canvasRenderer = L.canvas({ padding: 0.5 });

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

const statsEl = document.getElementById("stats");
const categoryEl = document.getElementById("category");
const limitEl = document.getElementById("limit");
const formEl = document.getElementById("filters-form");

// Initialize WebWorker for background parsing
let parseWorker = null;
if (false && typeof Worker !== "undefined") {  // DISABLED FOR DEBUGGING
  parseWorker = new Worker("parse-worker.js");
}

const clusterGroup = L.markerClusterGroup({
  maxClusterRadius: 80,
  disableClusteringAtZoom: 10,
});

let geoJsonLayer = L.geoJSON([], {
  renderer: canvasRenderer,
  style: {
    color: "#1d3557",
    weight: 1,
    fillColor: "#2a9d8f",
    fillOpacity: 0.35,
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

clusterGroup.addLayer(geoJsonLayer);
clusterGroup.addTo(map);

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
    min_lon: bounds.getWest(),
    min_lat: bounds.getSouth(),
    max_lon: bounds.getEast(),
    max_lat: bounds.getNorth(),
  };
}

async function loadFeatures() {
  inFlight = true;

  const bbox = getBboxFromMap();
  const zoom = map.getZoom();
  const params = new URLSearchParams({
    min_lon: String(bbox.min_lon),
    min_lat: String(bbox.min_lat),
    max_lon: String(bbox.max_lon),
    max_lat: String(bbox.max_lat),
    zoom: String(zoom),
    limit: String(Math.max(1, Number(limitEl.value) || 400)),
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
    console.log("API response length:", features.length);
    if (features.length > 0) {
      console.log("First feature:", features[0]);
      console.log("Geometry types:", features.map(f => f.geometry?.type).slice(0, 10));
    }

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
        statsEl.textContent = `Loaded ${viewportFeatures.length} features in ${elapsedMs} ms (zoom ${zoom})`;
      };

      parseWorker.postMessage({ features, mapBounds: bbox });
    } else {
      // Fallback: process on main thread
      const mapBounds = map.getBounds();
      // TEMPORARILY DISABLE VIEWPORT FILTERING FOR DEBUGGING
      const viewportFeatures = features; // Removed: .filter((feature) => { ... })
      
      console.log("After viewport filter:", viewportFeatures.length, "features");

      const featureCollection = {
        type: "FeatureCollection",
        features: viewportFeatures,
      };

      geoJsonLayer.clearLayers();
      geoJsonLayer.addData(featureCollection);

      const elapsedMs = Math.round(performance.now() - startedAt);
      statsEl.textContent = `Loaded ${viewportFeatures.length} features in ${elapsedMs} ms (zoom ${zoom})`;
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
