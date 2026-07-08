/* EarthAnalyser — DOM helpers, date/place parsing */

import { state } from "./state.js";
import { bboxToH3Res } from "./config.js";

export const $ = (s) => document.querySelector(s);
export const $$ = (s) => document.querySelectorAll(s);

/** Escape a value for safe interpolation into HTML strings. */
export function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

// ─── DOM references ──────────────────────────────────────────
export const statusBox = $("#status-box");
export const queryForm = $("#query-form");
export const queryInput = $("#query-input");
export const startDate = $("#start-date");
export const endDate = $("#end-date");
export const frequency = $("#frequency");
export const playSpeed = $("#play-speed");
export const drawToggle = $("#draw-toggle");
export const drawClear = $("#draw-clear");
export const layerSatellite = $("#layer-satellite");
export const timelinePlay = $("#timeline-play");
export const timelinePause = $("#timeline-pause");
export const timelineSlider = $("#timeline-slider");
export const currentTimeLabel = $("#current-time-label");
export const timelineSummary = $("#timeline-summary");
export const timelineChart = $("#timeline-chart");
export const exportCsv = $("#export-csv");
export const askHint = $("#ask-hint");
export const loadingOverlay = $("#loading-overlay");
export const loadingText = $("#loading-text");
export const progressBar = $("#progress-bar");
export const chatLog = $("#chat-log");
export const chatForm = $("#chat-form");
export const chatInput = $("#chat-input");
export const resolutionSlider = $("#resolution-slider");
export const resolutionValue = $("#resolution-value");

// ─── Helpers ─────────────────────────────────────────────────

export function autoExpand(el) {
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, parseInt(getComputedStyle(el).maxHeight) || 120) + "px";
}

export function updateStatus(msg) { if (statusBox) statusBox.textContent = msg; }
export function setFetching(on) { if (statusBox) statusBox.classList.toggle("fetching", on); }
export function showLoading(msg) { if (loadingText) loadingText.textContent = msg || "Loading..."; if (loadingOverlay) loadingOverlay.classList.remove("hidden"); }
export function hideLoading() { if (loadingOverlay) loadingOverlay.classList.add("hidden"); }
export function setProgress(p) { if (progressBar) progressBar.style.width = `${Math.max(0, Math.min(100, p))}%`; }

export function parseDateRange() {
  if (!startDate || !endDate) return null;
  const s = new Date(`${startDate.value}T00:00:00Z`);
  const e = new Date(`${endDate.value}T00:00:00Z`);
  if (!Number.isFinite(s.getTime()) || !Number.isFinite(e.getTime())) return null;
  return s <= e ? { start: startDate.value, end: endDate.value } : { start: endDate.value, end: startDate.value };
}

export function mapBoundsBbox(map) {
  const b = map.getBounds();
  const lngSpan = b.getEast() - b.getWest();
  const latSpan = b.getNorth() - b.getSouth();
  const buf = 0.1;
  return [
    b.getWest() + lngSpan * buf,
    b.getSouth() + latSpan * buf,
    b.getEast() - lngSpan * buf,
    b.getNorth() - latSpan * buf,
  ];
}

export function activeBbox(map) {
  return state.resolvedBbox && state.resolvedBbox.length === 4 ? [...state.resolvedBbox] : mapBoundsBbox(map);
}

export function getCheckedDatasets() {
  const checked = [];
  for (const cb of $$("#dataset-grid input[type='checkbox']")) {
    if (cb.checked) checked.push(cb.value);
  }
  return checked;
}

export function makeFetchKey(bbox, res, dateRange, datasets) {
  const bboxKey = bbox.map((v) => v.toFixed(2)).join(",");
  return `${bboxKey}|${res}|${dateRange?.start}|${dateRange?.end}|${datasets.sort().join(",")}`;
}

export function makeDatasetCacheKey(bbox, res, dateRange, dsName) {
  const bboxKey = bbox.map((v) => v.toFixed(2)).join(",");
  return `${bboxKey}|${res}|${dateRange?.start}|${dateRange?.end}|${dsName}`;
}

export function getEffectiveResolution(bbox, map) {
  if (state.userResolutionOverride > 0) return Math.min(state.userResolutionOverride, 7);
  return Math.min(bboxToH3Res(bbox || activeBbox(map)), 7);
}

/**
 * If the area is large but resolution is manually set to 6+, reset to auto
 * and show a warning popup. Returns true if resolution was downgraded.
 */
export function guardResolutionForArea(bbox) {
  if (state.userResolutionOverride < 6) return false;
  const autoRes = bboxToH3Res(bbox);
  if (autoRes <= 4) {
    // Area is big (auto would be res 4 or less) but user set 6/7 — too many hexagons
    state.userResolutionOverride = 0;
    state.currentH3Res = autoRes;
    if (resolutionSlider) resolutionSlider.value = "0";
    updateResolutionLabel();
    // Show popup
    const msg = document.createElement("div");
    msg.className = "resolution-warning";
    msg.textContent = "Resolution reset to Auto — high detail on large areas causes memory issues.";
    msg.style.cssText = "position:fixed;top:1rem;left:50%;transform:translateX(-50%);background:#1a1a2e;color:#f0ad4e;padding:0.75rem 1.5rem;border-radius:8px;z-index:9999;font-size:0.85rem;box-shadow:0 4px 12px rgba(0,0,0,0.4);border:1px solid #f0ad4e;";
    document.body.appendChild(msg);
    setTimeout(() => msg.remove(), 4000);
    return true;
  }
  return false;
}

export function updateResolutionLabel() {
  if (!resolutionValue) return;
  if (state.userResolutionOverride > 0) {
    resolutionValue.textContent = `${state.currentH3Res}`;
  } else {
    resolutionValue.textContent = `Auto (${state.currentH3Res})`;
  }
}

// ─── Date range extraction ───────────────────────────────────

export function extractDateRange(question) {
  const q = question.trim();
  const now = new Date();
  const currentYear = now.getFullYear();

  const yearRange = q.match(/(?:from\s+)?(\d{4})\s*(?:to|[-–]|and)\s*(\d{4})/i);
  if (yearRange) {
    const y1 = parseInt(yearRange[1]);
    const y2 = parseInt(yearRange[2]);
    if (y1 >= 1980 && y2 >= 1980 && y1 <= currentYear + 1 && y2 <= currentYear + 1) {
      return { start: `${y1}-01-01`, end: `${y2}-12-31` };
    }
  }

  const singleYear = q.match(/\b(?:in|during|for)\s+(\d{4})\b/i);
  if (singleYear) {
    const y = parseInt(singleYear[1]);
    if (y >= 1980 && y <= currentYear + 1) return { start: `${y}-01-01`, end: `${y}-12-31` };
  }

  const monthsAgo = q.match(/(?:last|past)\s+(\d+)\s*months?/i);
  if (monthsAgo) {
    const m = parseInt(monthsAgo[1]);
    const end = now.toISOString().slice(0, 10);
    const startD = new Date(now);
    startD.setMonth(startD.getMonth() - m);
    return { start: startD.toISOString().slice(0, 10), end };
  }

  const yearsAgo = q.match(/(?:last|past)\s+(\d*)\s*years?/i);
  if (yearsAgo) {
    const y = parseInt(yearsAgo[1]) || 1;
    const end = now.toISOString().slice(0, 10);
    const startD = new Date(now);
    startD.setFullYear(startD.getFullYear() - y);
    return { start: startD.toISOString().slice(0, 10), end };
  }

  const weeksAgo = q.match(/(?:last|past)\s+(\d*)\s*weeks?/i);
  if (weeksAgo) {
    const w = parseInt(weeksAgo[1]) || 1;
    const end = now.toISOString().slice(0, 10);
    const startD = new Date(now);
    startD.setDate(startD.getDate() - w * 7);
    return { start: startD.toISOString().slice(0, 10), end };
  }

  const since = q.match(/\b(?:since|from)\s+(\d{4})\b/i);
  if (since && !yearRange) {
    const y = parseInt(since[1]);
    if (y >= 1980 && y <= currentYear) return { start: `${y}-01-01`, end: now.toISOString().slice(0, 10) };
  }

  return null;
}

// ─── Place name extraction ───────────────────────────────────

export function extractPlaceName(question) {
  const q = question.trim();

  if (/\b(drawn|selected|current|this|my)\s+(bbox|area|polygon|region|selection|box|zone)\b/i.test(q)
      || /\b(bbox|drawn area|drawn polygon|selected area)\b/i.test(q)) {
    return null;
  }

  if (/^(why|what|how|when|who|can you|could you|tell me|explain|is there|are there|show me more|go deeper|more detail)/i.test(q)) {
    const explicitPlace = q.match(/\b(?:in|at|near|around)\s+([A-Z][a-zA-Z\s]{2,}?)(?:\s+(?:last|next|past|from|between|for|during)\b|[?!.]|$)/);
    if (explicitPlace && explicitPlace[1].trim().length >= 2) return explicitPlace[1].trim();
    return null;
  }

  const inMatch = q.match(/\b(?:in|at|near|around|over|of)\s+(.+?)(?:\s+(?:last|next|past|from|between|for|during|in|at|on|since|over)\b|[?!.]|$)/i);
  if (inMatch && inMatch[1].trim().length >= 2) {
    const place = inMatch[1].trim()
      .replace(/\b(fire|flood|vegetation|weather|temperature|heat|data|activity|risk)\b/gi, "")
      .replace(/\b(in|at|on|the|a|an|for|to|of)\s*$/gi, "")
      .replace(/\s+/g, " ").trim();
    if (place.length >= 2) return place;
  }

  const beforeDs = q.match(/(?:show|find|get|check|analyze)\s+(.+?)\s+(?:fire|flood|vegetation|weather|temperature|land\s*use|data|activity)/i);
  if (beforeDs && beforeDs[1].trim().length >= 2) return beforeDs[1].trim();

  const stripped = q
    .replace(/\b(show|find|search|analyze|get|fetch|plot|display|check|look|tell|me|about|the|any|some|this|that|a|an|for|and|or|with|its)\b/gi, "")
    .replace(/\b(fire|fires|flood|floods|vegetation|weather|land\s*use|temperature|heat|ndvi|risk|danger|activity|data|detections?|conditions?|status|wildfire|burn|blaze)\b/gi, "")
    .replace(/\b(last|next|past|from|to|over|between|during|since|until|ago)\b/gi, "")
    .replace(/\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b/gi, "")
    .replace(/\b(year|years|month|months|week|weeks|day|days|summer|winter|spring|autumn)\b/gi, "")
    .replace(/\b\d+\s*(km|min|minutes?|hours?|days?|weeks?|months?|years?)?\b/gi, "")
    .replace(/[?!.,;:]+/g, "").replace(/\s+/g, " ").trim();
  if (stripped.length >= 2) return stripped;

  return null;
}
