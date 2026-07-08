/* EarthAnalyser — H3 hexagon rendering per dataset */

import { DATASET_COLORS, LANDUSE_COLORS } from "./config.js";
import { state } from "./state.js";
import { getCheckedDatasets, escapeHtml } from "./utils.js";
import { datasetLayers, pointInPolygon } from "./map.js";

// ─── Tooltip ─────────────────────────────────────────────────

function hexTooltip(hex, dataset) {
  const lines = [`<strong>${dataset.toUpperCase()} · H3 res ${state.currentH3Res}</strong>`];
  lines.push(`Pixels: ${hex.pixel_count}`);
  if (hex.days_active) lines.push(`Days active: ${hex.days_active}`);

  if (dataset === "fire") {
    const c = hex.classes || {};
    lines.push(`High: ${c.high || 0} · Nom: ${c.nominal || 0} · Low: ${c.low || 0}`);
  } else if (dataset === "flood") {
    lines.push(`Water pixels: ${hex.pixel_count}`);
    if (hex.days_active) lines.push(`Days detected: ${hex.days_active}`);
  } else if (dataset === "vegetation") {
    const ndvi = hex.value_mean / 10000;
    lines.push(`NDVI: ${ndvi.toFixed(3)}`);
    lines.push(`Class: ${escapeHtml(hex.dominant_class)}`);
  } else if (dataset === "weather") {
    const tempC = (hex.value_mean * 0.02 - 273.15).toFixed(1);
    lines.push(`LST: ${tempC}°C`);
    lines.push(`Class: ${escapeHtml(hex.dominant_class)}`);
  } else if (dataset === "landuse") {
    lines.push(`Class: ${escapeHtml(hex.dominant_class)}`);
    const c = hex.classes || {};
    const entries = Object.entries(c).sort(([, a], [, b]) => b - a).slice(0, 3);
    lines.push(entries.map(([k, v]) => `${escapeHtml(k)}: ${v}`).join(" · "));
  }
  return lines.join("<br>");
}

// ─── Fill colors ─────────────────────────────────────────────

function hexFillColor(hex, dataset, maxCount) {
  const ratio = Math.min(1, hex.pixel_count / Math.max(maxCount, 1));

  if (dataset === "fire") {
    const mc = hex.dominant_class;
    if (mc === "high") return `rgba(227,26,28,${0.5 + ratio * 0.45})`;
    if (mc === "nominal") return `rgba(255,140,0,${0.4 + ratio * 0.45})`;
    return `rgba(255,237,111,${0.4 + ratio * 0.4})`;
  }
  if (dataset === "flood") {
    const mc = hex.dominant_class;
    if (mc === "permanent") return `rgba(13,71,161,${0.5 + ratio * 0.4})`;
    if (mc === "seasonal") return `rgba(21,101,192,${0.4 + ratio * 0.4})`;
    return `rgba(100,181,246,${0.3 + ratio * 0.4})`;
  }
  if (dataset === "vegetation") {
    const ndvi = hex.value_mean / 10000;
    if (ndvi > 0.6) return `rgba(27,94,32,${0.5 + ratio * 0.4})`;
    if (ndvi > 0.3) return `rgba(46,125,50,${0.4 + ratio * 0.4})`;
    return `rgba(139,195,74,${0.3 + ratio * 0.4})`;
  }
  if (dataset === "weather") {
    const tempC = hex.value_mean * 0.02 - 273.15;
    if (tempC > 40) return `rgba(183,28,28,${0.5 + ratio * 0.4})`;
    if (tempC > 30) return `rgba(230,81,0,${0.4 + ratio * 0.4})`;
    if (tempC > 20) return `rgba(255,183,77,${0.3 + ratio * 0.4})`;
    return `rgba(100,181,246,${0.3 + ratio * 0.3})`;
  }
  if (dataset === "landuse") {
    return LANDUSE_COLORS[hex.dominant_class] || "#888888";
  }
  return DATASET_COLORS[dataset]?.fill(ratio) || `rgba(100,100,100,${0.3 + ratio * 0.4})`;
}

// ─── Render hexagons ─────────────────────────────────────────

export function renderDatasetHexagons(dataset, hexagons) {
  const layer = datasetLayers[dataset];
  if (!layer) return;
  layer.clearLayers();
  if (!hexagons || !hexagons.length) return;

  // Only filter by polygon if it's large enough (countries/regions).
  // Small polygons (cities, parks, landmarks) would incorrectly clip hexagons
  // whose centers fall just outside the tight boundary.
  let filtered = hexagons;
  if (state.activePolygon) {
    const bbox = state.resolvedBbox;
    const bboxArea = bbox ? (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) : 0;
    if (bboxArea > 4) {  // Only clip for areas > ~2° x 2° (large regions/countries)
      filtered = hexagons.filter((h) => h.center && pointInPolygon(h.center[0], h.center[1], state.activePolygon));
    }
  }
  if (!filtered.length) return;

  const maxCount = Math.max(...filtered.map((h) => h.pixel_count));
  const colors = DATASET_COLORS[dataset] || DATASET_COLORS.fire;

  for (const hex of filtered) {
    if (!hex.boundary || !hex.boundary.length) continue;
    const latlngs = hex.boundary.map((c) => [c[0], c[1]]);
    const fill = hexFillColor(hex, dataset, maxCount);

    const polygon = L.polygon(latlngs, {
      color: colors.stroke,
      weight: 1,
      fillColor: fill,
      fillOpacity: dataset === "landuse" ? 0.6 : 0.7,
      className: "h3-hex",
    });
    polygon.bindTooltip(hexTooltip(hex, dataset), { sticky: true, className: "hex-tooltip" });
    polygon.addTo(layer);
  }
}

// ─── Render all active datasets ──────────────────────────────

export function renderAllDatasets() {
  const checked = getCheckedDatasets();
  for (const ds of Object.keys(datasetLayers)) {
    if (!checked.includes(ds) || !state.datasetResults[ds]) {
      datasetLayers[ds].clearLayers();
      continue;
    }
    const data = state.datasetResults[ds];
    const frames = data.frames || [];
    const isStatic = frames.length > 0 && frames.every(f => f.acq_date === "static");

    if (state.viewMode === "aggregate" || isStatic) {
      let hexes = data.hexagons;
      if ((!hexes || !hexes.length) && frames.length > 0) {
        const hexMap = new Map();
        for (const f of frames) {
          for (const h of (f.hexagons || [])) hexMap.set(h.h3, h);
        }
        hexes = [...hexMap.values()];
      }
      renderDatasetHexagons(ds, hexes || []);
    } else if (frames.length > 0) {
      const currentDate = state.timeline[state.currentFrameIdx];
      if (currentDate) {
        let best = null;
        for (const f of frames) {
          if (f.acq_date <= currentDate) {
            if (!best || f.acq_date > best.acq_date) best = f;
          }
        }
        if (!best) best = frames[0];
        renderDatasetHexagons(ds, best.hexagons || []);
      } else {
        const last = frames[frames.length - 1];
        renderDatasetHexagons(ds, last.hexagons || []);
      }
    } else if (data.hexagons && data.hexagons.length > 0) {
      renderDatasetHexagons(ds, data.hexagons);
    } else {
      datasetLayers[ds].clearLayers();
    }
  }
}
