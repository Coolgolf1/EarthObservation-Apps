/* EarthAnalyser — timeline playback and chart drawing */

import { DAY_MS, DATASET_COLORS } from "./config.js";
import { state } from "./state.js";
import { getCheckedDatasets, frequency, playSpeed, timelineSlider, currentTimeLabel, timelineSummary, timelineChart } from "./utils.js";
import { renderAllDatasets } from "./rendering.js";

export function applyFrequencyStep(full) {
  const step = Math.max(1, parseInt(frequency?.value) || 1);
  if (step <= 1) return [...full];
  return full.filter((_, i) => i % step === 0);
}

export function stopPlayback() {
  if (state.playbackTimer) { clearInterval(state.playbackTimer); state.playbackTimer = null; }
}

function _timelineBarColor(ds, hexagons) {
  if (!hexagons || hexagons.length === 0) return DATASET_COLORS[ds]?.base || "#888";

  if (ds === "fire") {
    const meanPx = hexagons.reduce((s, h) => s + (h.pixel_count || 0), 0) / hexagons.length;
    const t = Math.min(1, meanPx / 50);
    return `rgb(255,${Math.round(200 - t * 180)},${Math.round(150 - t * 140)})`;
  }
  if (ds === "weather") {
    const meanRaw = hexagons.reduce((s, h) => s + (h.value_mean || 0), 0) / hexagons.length;
    const tempC = meanRaw * 0.02 - 273.15;
    const t = Math.max(0, Math.min(1, (tempC + 10) / 55));
    if (t < 0.5) {
      const s = t * 2;
      return `rgb(${Math.round(30 + s * 225)},${Math.round(100 + s * 155)},${Math.round(220 - s * 190)})`;
    }
    const s = (t - 0.5) * 2;
    return `rgb(255,${Math.round(200 - s * 180)},${Math.round(30 - s * 20)})`;
  }
  if (ds === "vegetation") {
    const meanRaw = hexagons.reduce((s, h) => s + (h.value_mean || 0), 0) / hexagons.length;
    const ndvi = Math.max(0, Math.min(1, meanRaw / 10000));
    return `rgb(${Math.round(160 - ndvi * 120)},${Math.round(100 + ndvi * 130)},${Math.round(40 + ndvi * 20)})`;
  }
  if (ds === "flood") {
    const meanPx = hexagons.reduce((s, h) => s + (h.pixel_count || 0), 0) / hexagons.length;
    const t = Math.min(1, meanPx / 30);
    return `rgb(${Math.round(150 - t * 130)},${Math.round(210 - t * 130)},${Math.round(255 - t * 50)})`;
  }
  return DATASET_COLORS[ds]?.base || "#6d4c41";
}

export function drawTimelineChart() {
  if (!timelineChart) return;
  const ctx = timelineChart.getContext("2d");
  if (!ctx) return;
  const w = timelineChart.clientWidth || 400;
  const h = timelineChart.clientHeight || 40;
  const dpr = devicePixelRatio || 1;
  timelineChart.width = Math.floor(w * dpr);
  timelineChart.height = Math.floor(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  if (!state.timeline || !state.timeline.length) {
    ctx.fillStyle = "#4d6074";
    ctx.font = "500 11px Manrope";
    ctx.fillText("No data", 8, 16);
    return;
  }

  const checked = getCheckedDatasets();
  const pad = 3;
  const iW = w - pad * 2;
  const iH = h - pad * 2;
  const barW = Math.max(1, iW / state.timeline.length);
  const bandH = Math.max(3, Math.floor(iH / Math.max(checked.length, 1)));

  checked.forEach((ds, bandIdx) => {
    const data = state.datasetResults[ds];
    if (!data) return;
    const frames = data.frames || [];
    const isStatic = frames.length > 0 && frames.every(f => f.acq_date === "static");
    const y0 = pad + bandIdx * bandH;

    if (isStatic) {
      const allHexes = frames.flatMap(f => f.hexagons || []);
      const totalHex = allHexes.length;
      const color = _timelineBarColor(ds, allHexes);
      const intensity = totalHex > 0 ? 0.6 + 0.4 * Math.min(1, totalHex / 200) : 0;
      for (let i = 0; i < state.timeline.length; i++) {
        ctx.fillStyle = totalHex > 0 ? color : "#e0e0e0";
        ctx.globalAlpha = totalHex > 0 ? intensity : 0.08;
        ctx.fillRect(Math.round(pad + i * barW), y0, Math.max(1, Math.floor(barW - 0.5)), bandH - 1);
      }
      ctx.globalAlpha = 1;
      return;
    }

    let maxHex = 1;
    for (const f of frames) { const c = (f.hexagons || []).length; if (c > maxHex) maxHex = c; }

    const slotCounts = new Array(state.timeline.length).fill(0);
    const slotHexes = new Array(state.timeline.length).fill(null);
    for (const f of frames) {
      const d = f.acq_date;
      if (!d || d === "static") continue;
      const hexes = f.hexagons || [];
      if (hexes.length === 0) continue;
      let lo = 0, hi = state.timeline.length - 1, slot = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (state.timeline[mid] <= d) { slot = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      if (slot >= 0 && hexes.length > slotCounts[slot]) {
        slotCounts[slot] = hexes.length;
        slotHexes[slot] = hexes;
      }
    }

    for (let i = 0; i < state.timeline.length; i++) {
      if (slotCounts[i] > 0) {
        ctx.fillStyle = _timelineBarColor(ds, slotHexes[i]);
        ctx.globalAlpha = 0.5 + 0.5 * (slotCounts[i] / maxHex);
      } else {
        ctx.fillStyle = "#e0e0e0";
        ctx.globalAlpha = 0.08;
      }
      ctx.fillRect(Math.round(pad + i * barW), y0, Math.max(1, Math.floor(barW - 0.5)), bandH - 1);
    }
    ctx.globalAlpha = 1;
  });

  if (state.viewMode === "timeline" && state.timeline.length > 0) {
    ctx.fillStyle = "#333";
    ctx.globalAlpha = 0.8;
    const x = Math.round(pad + (state.currentFrameIdx / Math.max(state.timeline.length - 1, 1)) * iW);
    ctx.fillRect(x, pad, 2, iH);
    ctx.globalAlpha = 1;
  }
}

export function updateTimelineUI() {
  const totalDays = state.timeline.length;
  if (!totalDays) {
    if (timelineSlider) { timelineSlider.max = "0"; timelineSlider.value = "0"; }
    if (currentTimeLabel) currentTimeLabel.textContent = "—";
    if (timelineSummary) timelineSummary.textContent = "Select area & dates, then run analysis";
    drawTimelineChart();
    return;
  }

  if (timelineSlider) {
    timelineSlider.max = String(Math.max(0, totalDays - 1));
    timelineSlider.value = String(state.currentFrameIdx);
  }

  const checked = getCheckedDatasets();
  const totalHexes = checked.reduce((s, ds) => s + ((state.datasetResults[ds]?.hexagons || []).length), 0);

  if (state.viewMode === "aggregate") {
    const dateLabel = totalDays === 1 ? state.timeline[0] : `${state.timeline[0]} — ${state.timeline[totalDays - 1]}`;
    if (currentTimeLabel) currentTimeLabel.textContent = dateLabel;
    if (timelineSummary) timelineSummary.textContent = `${totalHexes} hexagons · ${checked.join(", ")} · H3 res ${state.currentH3Res}`;
  } else {
    const currentDate = state.timeline[state.currentFrameIdx] || "—";
    if (currentTimeLabel) currentTimeLabel.textContent = currentDate;
    if (timelineSummary) timelineSummary.textContent = `Day ${state.currentFrameIdx + 1}/${totalDays} · H3 res ${state.currentH3Res}`;
  }
  drawTimelineChart();
}

function computeAnimationDelay() {
  // Use the play-speed dropdown value (ms per frame)
  const selected = parseInt(playSpeed?.value);
  if (Number.isFinite(selected) && selected > 0) return selected;
  return 700; // default: Normal
}

export function startPlayback() {
  stopPlayback();
  if (!state.timeline.length) return;
  state.viewMode = "timeline";
  const speed = computeAnimationDelay();
  state.playbackTimer = setInterval(() => {
    state.currentFrameIdx = (state.currentFrameIdx + 1) % state.timeline.length;
    renderAllDatasets();
    updateTimelineUI();
  }, speed);
}
