/* EarthSearch — shared constants and configuration */

export const API_BASE = (() => {
  const isLocal =
    (window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost") &&
    window.location.port !== "8000";
  return window.location.protocol === "file:" || isLocal ? "http://127.0.0.1:8000" : window.location.origin;
})();

export let MAPBOX_TOKEN = "";
try {
  const res = await fetch(`${API_BASE}/health/config`);
  if (res.ok) { const cfg = await res.json(); MAPBOX_TOKEN = cfg.mapbox_token || ""; }
} catch (e) { console.warn("Could not fetch mapbox token:", e); }

export const STREAMING_LIMIT = 0;
export const LOW_ZOOM_NAMED_PLACES_THRESHOLD = 6;

export const CATEGORY_STYLE = {
  hydrography:    { stroke: "#1f77b4", fill: "#4aa3df", point: "#1f77b4", fillOpacity: 0.7 },
  administrative: { stroke: "#2ca25f", fill: "transparent", point: "#2ca25f", fillOpacity: 0.0 },
  transport:      { stroke: "#facc15", fill: "#facc15", point: "#facc15", fillOpacity: 0.45 },
  places:         { stroke: "#f59e0b", fill: "#f59e0b", point: "#f59e0b", fillOpacity: 0.35 },
  landmarks:      { stroke: "#ef4444", fill: "#ef4444", point: "#ef4444", fillOpacity: 0.55 },
  default:        { stroke: "#1d3557", fill: "#2a9d8f", point: "#0f5132", fillOpacity: 0.35 },
};

export const SEARCH_CATEGORIES = [
  { category: "transport", label: "transport", limit: 50000 },
  { category: "hydrography", label: "rivers/ocean", featureFilter: isHydroFeatureAllowed, limit: 50000 },
  { category: "places", label: "places", limit: 50000 },
  { category: "landmarks", label: "landmarks", featureFilter: isLandmarkFeatureAllowed, limit: 50000 },
];

export function isHydroFeatureAllowed(feature) {
  const geometryType = String(feature?.geometry?.type || "");
  const props = feature?.properties || {};
  const text = `${String(props.name || "")} ${String(props.subcategory || "")}`.toLowerCase();
  if (geometryType.includes("LineString")) {
    if (text.includes("coast") || text.includes("shore")) return false;
    return text.includes("river") || text.includes("stream") || text.includes("canal") || text.includes("waterway");
  }
  if (geometryType.includes("Polygon")) return text.includes("ocean") || text.includes("sea");
  return false;
}

export function isLandmarkFeatureAllowed(feature) {
  const geometryType = String(feature?.geometry?.type || "");
  return geometryType === "Point" || geometryType === "MultiPoint";
}

export function formatFeatureHoverText(feature) {
  const props = feature?.properties || {};
  const name = String(props.name || "Unnamed feature");
  const category = String(props.category || "unknown");
  const subcategory = String(props.subcategory || "");
  const source = String(props.source || "");
  const lines = [name, `Category: ${category}`];
  if (subcategory) lines.push(`Subcategory: ${subcategory}`);
  if (source) lines.push(`Source: ${source}`);
  return lines.join("<br>");
}

export function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

export async function fetchWith429Retry(url, options, { retries = 1, retryDelayMs = 1000 } = {}) {
  let attempt = 0;
  while (true) {
    const response = await fetch(url, options);
    if (response.status !== 429 || attempt >= retries) return response;
    attempt += 1;
    await sleep(retryDelayMs);
  }
}
