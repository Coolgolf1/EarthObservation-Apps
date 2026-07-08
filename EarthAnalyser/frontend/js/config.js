/* EarthAnalyser — shared constants and configuration */

export const API_BASE = (() => {
  const isLocal =
    (location.hostname === "127.0.0.1" || location.hostname === "localhost") &&
    location.port !== "8000";
  return location.protocol === "file:" || isLocal ? "http://127.0.0.1:8000" : location.origin;
})();

// Fetched from backend at startup
export let MAPBOX_TOKEN = "";
try {
  const res = await fetch(`${API_BASE}/health/config`);
  if (res.ok) { const cfg = await res.json(); MAPBOX_TOKEN = cfg.mapbox_token || ""; }
} catch (e) { console.warn("Could not fetch mapbox token:", e); }

export const DAY_MS = 86400000;

export const DATASET_COLORS = {
  fire:       { base: "#e65100", fill: (r) => `rgba(227,26,28,${0.4+r*0.5})`,   stroke: "#b71c1c" },
  flood:      { base: "#1565c0", fill: (r) => `rgba(21,101,192,${0.3+r*0.5})`,  stroke: "#0d47a1" },
  vegetation: { base: "#2e7d32", fill: (r) => `rgba(46,125,50,${0.3+r*0.5})`,   stroke: "#1b5e20" },
  weather:    { base: "#e65100", fill: (r) => `rgba(230,81,0,${0.3+r*0.5})`,    stroke: "#bf360c" },
  landuse:    { base: "#6d4c41", fill: (r) => `rgba(109,76,65,${0.3+r*0.5})`,   stroke: "#3e2723" },
};

export const LANDUSE_COLORS = {
  tree: "#006400", shrub: "#ffbb22", grass: "#ffff4c", crop: "#f096ff",
  built: "#fa0000", bare: "#b4b4b4", water: "#0064c8", other: "#888888",
};

export function bboxToH3Res(bbox) {
  const [west, south, east, north] = bbox;
  const span = Math.max(Math.abs(east - west), Math.abs(north - south));
  if (span < 0.02) return 9;
  if (span < 0.1) return 8;
  if (span < 0.3) return 7;
  if (span < 1.0) return 6;
  if (span < 3.0) return 5;
  if (span < 8.0) return 4;
  if (span < 20.0) return 3;
  if (span < 60.0) return 2;
  return 1;
}
