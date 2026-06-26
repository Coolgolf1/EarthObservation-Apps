/* EarthMapper — shared constants and styles */

export const API_BASE = "http://127.0.0.1:8080";

export let MAPBOX_TOKEN = "";
try {
  const res = await fetch("http://127.0.0.1:8000/health/config");
  if (res.ok) { const cfg = await res.json(); MAPBOX_TOKEN = cfg.mapbox_token || ""; }
} catch (e) { console.warn("Could not fetch mapbox token:", e); }

export const FRONTEND_LIMIT = 50000;
export const LOW_ZOOM_NAMED_PLACES_THRESHOLD = 6;
export const MOVE_DEBOUNCE_MS = 250;
export const SMALL_MOVE_RATIO = 0.15;

export const ADMIN_LIMIT_BY_DETAIL = { z4: 1500, z7: 6000, z10: 18000, full: FRONTEND_LIMIT };
export const TRANSPORT_LIMIT_BY_ZOOM = { far: 2500, mid: 8000, near: 18000, full: FRONTEND_LIMIT };
export const GEOMETRY_DETAIL_RANK = { z4: 1, z7: 2, z10: 3, full: 4 };

export const POPUP_FIELD_PRIORITY = [
  "source", "category", "subcategory", "country_name", "country_code",
  "admin_level", "admin_type_en", "admin_id", "admin1_name", "admin2_name",
  "admin3_name", "min_zoom", "label_min_zoom", "display_rank", "importance_score",
];

export const CATEGORY_STYLE = {
  hydrography:    { stroke: "#1f77b4", fill: "#4aa3df", point: "#1f77b4", fillOpacity: 0.7 },
  administrative: { stroke: "#2ca25f", fill: "transparent", point: "#2ca25f", fillOpacity: 0.0 },
  transport:      { stroke: "#facc15", fill: "#facc15", point: "#facc15", fillOpacity: 0.45 },
  places:         { stroke: "#f59e0b", fill: "#f59e0b", point: "#f59e0b", fillOpacity: 0.35 },
  terrain:        { stroke: "#8b5a2b", fill: "#a67c52", point: "#8b5a2b", fillOpacity: 0.4 },
  regions:        { stroke: "#0f766e", fill: "#14b8a6", point: "#0f766e", fillOpacity: 0.3 },
  landmarks:      { stroke: "#d62728", fill: "#ef4444", point: "#d62728", fillOpacity: 0.5 },
  default:        { stroke: "#1d3557", fill: "#2a9d8f", point: "#0f5132", fillOpacity: 0.35 },
};
