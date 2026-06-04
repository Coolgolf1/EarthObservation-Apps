const API_BASE = "http://127.0.0.1:8000";

const map = L.map("map", {
  zoomControl: true,
}).setView([20, 0], 2);

const canvasRenderer = L.canvas({ padding: 0.5 });

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

const statsEl = document.getElementById("stats");
const categoryEl = document.getElementById("category");
const limitEl = document.getElementById("limit");
const formEl = document.getElementById("filters-form");

let layer = L.geoJSON([], {
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
    const props = feature.properties || {};
    const title = props.name || props.label || props.category || "Feature";
    const details = Object.entries(props)
      .slice(0, 6)
      .map(([k, v]) => `<strong>${k}</strong>: ${String(v)}`)
      .join("<br>");
    featureLayer.bindPopup(`<div><h4>${title}</h4><div>${details}</div></div>`);
  },
}).addTo(map);

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
  const params = new URLSearchParams({
    min_lon: String(bbox.min_lon),
    min_lat: String(bbox.min_lat),
    max_lon: String(bbox.max_lon),
    max_lat: String(bbox.max_lat),
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

    const featureCollection = {
      type: "FeatureCollection",
      features,
    };

    layer.clearLayers();
    layer.addData(featureCollection);

    const elapsedMs = Math.round(performance.now() - startedAt);
    statsEl.textContent = `Loaded ${features.length} features in ${elapsedMs} ms`;
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
