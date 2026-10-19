/* EarthAnalyser — map initialization, layers, drawing, polygon clipping */

import { MAPBOX_TOKEN } from "./config.js";
import { state } from "./state.js";
import { $, updateStatus } from "./utils.js";

// ─── Map ─────────────────────────────────────────────────────

export const map = L.map("map", { zoomControl: true, worldCopyJump: true, preferCanvas: true }).setView([39.5, -5.5], 5);

export const hasMapboxToken = Boolean(MAPBOX_TOKEN);
const osmLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
});
const mapboxAttribution =
  '&copy; <a href="https://www.mapbox.com/about/maps/">Mapbox</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

export const baseSatellite = hasMapboxToken
  ? L.tileLayer("https://api.mapbox.com/styles/v1/{id}/tiles/{z}/{x}/{y}?access_token={accessToken}", {
    id: "mapbox/satellite-streets-v12", accessToken: MAPBOX_TOKEN,
    tileSize: 512, zoomOffset: -1, maxZoom: 20, attribution: mapboxAttribution,
  })
  : osmLayer;
baseSatellite.addTo(map);

export const baseLight = hasMapboxToken
  ? L.tileLayer("https://api.mapbox.com/styles/v1/{id}/tiles/{z}/{x}/{y}?access_token={accessToken}", {
    id: "mapbox/light-v11", accessToken: MAPBOX_TOKEN,
    tileSize: 512, zoomOffset: -1, maxZoom: 20, attribution: mapboxAttribution,
  })
  : osmLayer;

export const drawingLayer = L.layerGroup().addTo(map);
export const boundaryLayer = L.layerGroup().addTo(map);

export const datasetLayers = {
  fire: L.layerGroup().addTo(map),
  flood: L.layerGroup().addTo(map),
  vegetation: L.layerGroup().addTo(map),
  weather: L.layerGroup().addTo(map),
  landuse: L.layerGroup().addTo(map),
};

// ─── Polygon Clipping (point-in-polygon) ─────────────────────

function _pointInRing(lat, lng, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if ((yi > lat) !== (yj > lat) && lng < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

export function pointInPolygon(lat, lng, geojson) {
  if (!geojson) return true;
  if (geojson.type === "Polygon") {
    const rings = geojson.coordinates;
    if (!_pointInRing(lat, lng, rings[0])) return false;
    for (let i = 1; i < rings.length; i++) {
      if (_pointInRing(lat, lng, rings[i])) return false;
    }
    return true;
  }
  if (geojson.type === "MultiPolygon") {
    for (const polygon of geojson.coordinates) {
      if (_pointInRing(lat, lng, polygon[0])) {
        let inHole = false;
        for (let i = 1; i < polygon.length; i++) {
          if (_pointInRing(lat, lng, polygon[i])) { inHole = true; break; }
        }
        if (!inHole) return true;
      }
    }
    return false;
  }
  return true;
}

// ─── Boundary drawing ────────────────────────────────────────

export function drawBoundary(geojson) {
  boundaryLayer.clearLayers();
  if (!geojson) return;
  L.geoJSON(geojson, {
    style: { color: "#007f5f", weight: 2, fillColor: "#007f5f", fillOpacity: 0.03, dashArray: "5,4" },
  }).addTo(boundaryLayer);
}

export function clearBoundary() {
  boundaryLayer.clearLayers();
  state.activePolygon = null;
}

// ─── Drawing ─────────────────────────────────────────────────

function convexHull(points) {
  if (points.length < 3) return points;
  const sorted = [...points].sort((a, b) => a.lng - b.lng || a.lat - b.lat);
  const cross = (o, a, b) => (a.lng - o.lng) * (b.lat - o.lat) - (a.lat - o.lat) * (b.lng - o.lng);
  const lower = [];
  for (const p of sorted) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop(); lower.push(p); }
  const upper = [];
  for (let i = sorted.length - 1; i >= 0; i--) { const p = sorted[i]; while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop(); upper.push(p); }
  upper.pop(); lower.pop();
  return lower.concat(upper);
}

export function clearDrawing() {
  state.drawPoints = [];
  drawingLayer.clearLayers();
  state.resolvedBbox = null;
  clearBoundary();
  hideDrawConfirm();
}

function hideDrawConfirm() {
  const bar = $("#draw-confirm-bar");
  if (bar) bar.classList.add("hidden");
}

export function showDrawConfirm() {
  const bar = $("#draw-confirm-bar");
  const text = $("#draw-confirm-text");
  if (bar) bar.classList.remove("hidden");
  if (text) text.textContent = `${state.drawPoints.length} points`;
}

export function redrawDrawing() {
  drawingLayer.clearLayers();
  for (const p of state.drawPoints) {
    L.circleMarker([p.lat, p.lng], { radius: 4, color: "#007f5f", fillColor: "#38d9a9", fillOpacity: 0.9, weight: 1.5 }).addTo(drawingLayer);
  }
  if (state.drawPoints.length >= 3) {
    const hull = convexHull(state.drawPoints);
    L.polygon(hull.map((p) => [p.lat, p.lng]), { color: "#007f5f", weight: 2, fillColor: "#007f5f", fillOpacity: 0.08 }).addTo(drawingLayer);
  }
}

export function confirmDrawing(getEffectiveResolution) {
  if (state.drawPoints.length < 3) return;
  const hull = convexHull(state.drawPoints);
  const lons = hull.map((p) => p.lng), lats = hull.map((p) => p.lat);
  state.resolvedBbox = [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
  state.drawMode = false;
  hideDrawConfirm();
  const drawToggle = $("#draw-toggle");
  if (drawToggle) { drawToggle.classList.remove("active"); drawToggle.textContent = "Draw polygon"; }
  const [w, s, e, n] = state.resolvedBbox;
  map.fitBounds([[s, w], [n, e]], { padding: [30, 30] });
  state.currentH3Res = getEffectiveResolution(state.resolvedBbox, map);
  updateStatus("Area confirmed — select datasets and run analysis, or ask in chat");
}

export { hideDrawConfirm };
