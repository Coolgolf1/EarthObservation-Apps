/* EarthAnalyser — entry point: event wiring + query orchestration */

import { API_BASE } from "./config.js";
import { state } from "./state.js";
import {
  $, $$, autoExpand, updateStatus, setFetching, showLoading, hideLoading, setProgress,
  parseDateRange, activeBbox, getCheckedDatasets, makeFetchKey, getEffectiveResolution,
  updateResolutionLabel,
  extractDateRange, extractPlaceName, startDate, endDate, frequency, playSpeed,
  drawToggle, drawClear, layerSatellite, timelinePlay, timelinePause, timelineSlider,
  exportCsv, askHint, chatForm, chatInput, queryForm, resolutionSlider, resolutionValue,
} from "./utils.js";
import {
  map, baseSatellite, baseLight, datasetLayers, drawBoundary, clearBoundary,
  clearDrawing, showDrawConfirm, redrawDrawing, confirmDrawing,
} from "./map.js";
import { renderAllDatasets, renderDatasetHexagons } from "./rendering.js";
import { applyFrequencyStep, stopPlayback, startPlayback, updateTimelineUI, drawTimelineChart } from "./timeline.js";
import { fetchDatasetsH3Stream, cancelActiveRequest } from "./api.js";
import { cacheStoreDatasets, cacheLookupDatasets } from "./cache.js";
import {
  addChat, addTyping, removeTyping, streamText,
  getAIResponse, generateInsight,
  inferNeededDatasets, inferContextDatasets, updatePlaceNameCache,
} from "./agent.js";

// Init textareas
for (const ta of $$("textarea")) { ta.addEventListener("input", () => autoExpand(ta)); autoExpand(ta); }

// ─── CSV Export ──────────────────────────────────────────────

function exportData() {
  const checked = getCheckedDatasets();
  const rows = ["dataset,h3_index,pixel_count,value_mean,value_max,dominant_class,days_active,lat,lng"];
  for (const ds of checked) {
    for (const h of (state.datasetResults[ds]?.hexagons || [])) {
      rows.push(`${ds},${h.h3},${h.pixel_count || h.total_pixels || 0},${h.value_mean},${h.value_max},${h.dominant_class},${h.days_active || 0},${h.center[0]},${h.center[1]}`);
    }
  }
  if (rows.length <= 1) { updateStatus("No data"); return; }
  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `earth_h3_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
  updateStatus("CSV exported");
}

// ─── Zoom/Pan refetch ────────────────────────────────────────

function scheduleRefetch() {
  if (state.refetchTimer) clearTimeout(state.refetchTimer);
  state.refetchTimer = setTimeout(() => refetchForCurrentView(), 800);
}

function cancelRefetch() {
  if (state.refetchTimer) { clearTimeout(state.refetchTimer); state.refetchTimer = null; }
}

async function refetchForCurrentView() {
  if (!state.queryDateRange || state._agentActive) return;

  const bbox = activeBbox(map);
  const newRes = getEffectiveResolution(bbox, map);
  const datasets = getCheckedDatasets();
  const key = makeFetchKey(bbox, newRes, state.queryDateRange, datasets);
  if (key === state.lastFetchKey) return;

  if (state.lastFetchBbox && state.lastFetchRes === newRes) {
    const [lw, ls, le, ln] = state.lastFetchBbox;
    const [cw, cs, ce, cn] = bbox;
    const overlapW = Math.max(0, Math.min(le, ce) - Math.max(lw, cw));
    const overlapH = Math.max(0, Math.min(ln, cn) - Math.max(ls, cs));
    const currentArea = Math.max(0.0001, (ce - cw) * (cn - cs));
    if ((overlapW * overlapH) / currentArea >= 0.7) {
      state.lastFetchKey = key;
      return;
    }
  }

  const { cached, missing, cachedTimeline } = cacheLookupDatasets(bbox, newRes, state.queryDateRange, datasets);
  if (Object.keys(cached).length > 0) {
    for (const [ds, data] of Object.entries(cached)) {
      state.datasetResults[ds] = data;
      renderDatasetHexagons(ds, data.hexagons || []);
    }
    if (cachedTimeline.length > state.fullTimeline.length) {
      state.fullTimeline = cachedTimeline;
      state.timeline = applyFrequencyStep(state.fullTimeline);
    }
    updateTimelineUI();
  }

  if (missing.length === 0) {
    state.lastFetchKey = key;
    state.lastFetchBbox = [...bbox];
    state.lastFetchRes = newRes;
    state.currentH3Res = newRes;
    renderAllDatasets();
    updateTimelineUI();
    return;
  }

  cancelActiveRequest();
  state.activeAbortController = new AbortController();
  const { signal } = state.activeAbortController;
  state.isFetching = true;
  const fetchId = ++state._fetchGeneration;
  setFetching(true);
  state.currentH3Res = newRes;
  showLoading(`Loading ${missing.join(", ")}...`);

  try {
    const result = await fetchDatasetsH3Stream(
      bbox, state.queryDateRange.start, state.queryDateRange.end, newRes, missing, signal,
      (dsKey, dsData) => {
        if (fetchId !== state._fetchGeneration) return;
        state.datasetResults[dsKey] = dsData;
        state.viewMode = "timeline";
        if (state.currentFrameIdx >= state.timeline.length) state.currentFrameIdx = 0;
        renderAllDatasets();
        updateTimelineUI();
        updateStatus(`${dsKey}: ${(dsData.frames || []).length} days loaded...`);
      }
    );
    if (fetchId !== state._fetchGeneration) return;
    if (result?.datasets) {
      for (const [ds, data] of Object.entries(result.datasets)) state.datasetResults[ds] = data;
      if (result.timeline?.length > state.fullTimeline.length) {
        state.fullTimeline = result.timeline;
        state.timeline = applyFrequencyStep(state.fullTimeline);
      }
      cacheStoreDatasets(bbox, newRes, state.queryDateRange, result.datasets, state.fullTimeline);
    }
    state.lastFetchKey = key;
    state.lastFetchBbox = [...bbox];
    state.lastFetchRes = newRes;
    renderAllDatasets();
    updateTimelineUI();
  } catch (err) {
    if (err.name !== "AbortError" && fetchId === state._fetchGeneration) updateStatus(`Error: ${err.message}`);
  } finally {
    if (fetchId === state._fetchGeneration) { state.isFetching = false; setFetching(false); hideLoading(); }
  }
}

// ─── Main query ──────────────────────────────────────────────

async function runQuery() {
  stopPlayback();
  cancelActiveRequest();
  for (const layer of Object.values(datasetLayers)) layer.clearLayers();
  state.datasetResults = {};
  state.timeline = [];
  state.currentFrameIdx = 0;
  state.viewMode = "aggregate";
  state.lastFetchKey = "";
  setProgress(0);

  const range = parseDateRange();
  if (!range) { hideLoading(); updateStatus("Invalid date range"); return; }

  const datasets = getCheckedDatasets();
  state.queryDateRange = range;
  state.currentQuery = "";

  const bbox = activeBbox(map);
  state.currentH3Res = getEffectiveResolution(bbox, map);

  const cacheKey = makeFetchKey(bbox, state.currentH3Res, range, datasets);
  const { cached: cachedDs, missing: missingDs, cachedTimeline } = cacheLookupDatasets(bbox, state.currentH3Res, range, datasets);

  if (Object.keys(cachedDs).length > 0) {
    for (const [ds, data] of Object.entries(cachedDs)) state.datasetResults[ds] = data;
    if (cachedTimeline.length > 0) {
      state.fullTimeline = cachedTimeline;
      state.timeline = applyFrequencyStep(state.fullTimeline);
    }
  }

  if (missingDs.length === 0) {
    state.lastFetchKey = cacheKey;
    state.lastFetchBbox = [...bbox];
    state.lastFetchRes = state.currentH3Res;
    renderAllDatasets();
    updateTimelineUI();
    if (askHint) askHint.classList.add("hidden");
    const report = generateInsight(state.currentQuery);
    const response = await getAIResponse(state.currentQuery, report);
    await streamText(response);
    return;
  }

  cancelActiveRequest();
  state.isFetching = true;
  setFetching(true);
  showLoading(`Loading ${missingDs.join(", ")}...`);
  setProgress(5);

  state.activeAbortController = new AbortController();
  const { signal } = state.activeAbortController;

  if (Object.keys(cachedDs).length > 0) renderAllDatasets();

  try {
    const totalDs = datasets.length;
    let datasetsLoaded = Object.keys(cachedDs).length;
    const seenDs = new Set(Object.keys(cachedDs));

    const result = await fetchDatasetsH3Stream(
      bbox, range.start, range.end, state.currentH3Res, missingDs, signal,
      (dsKey, dsData) => {
        state.datasetResults[dsKey] = dsData;
        if (!seenDs.has(dsKey) && dsData.total_hexagons != null) { seenDs.add(dsKey); datasetsLoaded++; }
        setProgress(Math.round((datasetsLoaded / totalDs) * 85) + 5);
        state.viewMode = "timeline";
        if (state.currentFrameIdx >= state.timeline.length) state.currentFrameIdx = 0;
        renderAllDatasets();
        updateTimelineUI();
        updateStatus(`${dsKey}: ${(dsData.frames || []).length} days loaded...`);
      }
    );

    if (!result?.datasets && Object.keys(cachedDs).length === 0) {
      state.isFetching = false; setFetching(false); hideLoading();
      updateStatus("No data found for this area and date range");
      addChat("agent", "I couldn't find any data for that area and time period.");
      updateTimelineUI();
      return;
    }

    if (result?.datasets) {
      for (const [ds, data] of Object.entries(result.datasets)) state.datasetResults[ds] = data;
      if (result.timeline?.length > state.fullTimeline.length) {
        state.fullTimeline = result.timeline;
        state.timeline = applyFrequencyStep(state.fullTimeline);
      }
      cacheStoreDatasets(bbox, state.currentH3Res, range, result.datasets, state.fullTimeline);
    }

    state.lastFetchKey = cacheKey;
    state.lastFetchBbox = [...bbox];
    state.lastFetchRes = state.currentH3Res;
    setProgress(100);
    state.isFetching = false; setFetching(false); hideLoading();
    if (askHint) askHint.classList.add("hidden");
    renderAllDatasets();

    const report = generateInsight(state.currentQuery);
    const response = await getAIResponse(state.currentQuery, report);
    await streamText(response);
    updateTimelineUI();
  } catch (err) {
    if (err.name === "AbortError") { state.isFetching = false; setFetching(false); hideLoading(); return; }
    state.isFetching = false; setFetching(false); hideLoading();
    updateStatus(`Error: ${err.message}`);
    addChat("agent", `Something went wrong: ${err.message}\n\nMake sure the backend is running.`);
  }
}

// ─── Agent question handler ─────────────────────────────────

async function handleAgentQuestion(question) {
  state._agentActive = true;
  const hasLoadedData = Object.values(state.datasetResults).some(d => d?.total_hexagons > 0);
  const placeQuery = extractPlaceName(question);
  const extractedRange = extractDateRange(question);
  const usesDrawnArea = /\b(drawn|selected|current|this|my)\s+(bbox|area|polygon|region|selection|box|zone)\b/i.test(question)
      || /\b(bbox|drawn area|drawn polygon|selected area)\b/i.test(question);
  const isFollowUp = hasLoadedData && !placeQuery && !extractedRange && !usesDrawnArea;

  if (isFollowUp) {
    removeTyping();
    const report = generateInsight(question, state._cachedPlaceName);
    const response = await getAIResponse(question, report);
    await streamText(response);
    state._agentActive = false;
    updatePlaceNameCache(map, activeBbox);
    return;
  }

  // New query flow
  const classifyPromise = fetch(`${API_BASE}/datasets/classify`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  }).then(r => r.ok ? r.json() : null).catch(() => null);

  if (extractedRange) {
    state.queryDateRange = extractedRange;
    if (startDate) startDate.value = extractedRange.start;
    if (endDate) endDate.value = extractedRange.end;
  } else if (!state.queryDateRange) {
    const range = parseDateRange();
    if (range) state.queryDateRange = range;
  }

  let bbox = activeBbox(map);
  if (placeQuery) {
    try {
      updateStatus(`Locating "${placeQuery}"...`);
      const geoRes = await fetch(`${API_BASE}/datasets/geocode`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: placeQuery }),
      });
      if (geoRes.ok) {
        const geo = await geoRes.json();
        if (geo.bbox && geo.lat && geo.lon) {
          bbox = geo.bbox;
          state.resolvedBbox = [...bbox];
          if (geo.polygon) { state.activePolygon = geo.polygon; drawBoundary(geo.polygon); } else { clearBoundary(); }
          map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [40, 40], animate: true });
          state.currentH3Res = getEffectiveResolution(bbox, map);
          updateStatus(`Centered on ${geo.name?.split(",")[0] || placeQuery}`);
          await new Promise(r => setTimeout(r, 300));
        }
      }
    } catch (e) { /* fallback */ }
  }

  const classifyResult = await Promise.race([classifyPromise, new Promise(r => setTimeout(() => r(null), 8000))]);
  if (classifyResult?.date_start && classifyResult?.date_end) {
    state.queryDateRange = { start: classifyResult.date_start, end: classifyResult.date_end };
    if (startDate) startDate.value = classifyResult.date_start;
    if (endDate) endDate.value = classifyResult.date_end;
  }

  const plotCategories = classifyResult?.categories?.length ? classifyResult.categories : inferNeededDatasets(question);
  const contextCategories = classifyResult?.context_categories?.length ? classifyResult.context_categories : inferContextDatasets(question, plotCategories);
  const needed = [...new Set([...plotCategories, ...contextCategories])];

  for (const cb of $$("#dataset-grid input[type='checkbox']")) cb.checked = plotCategories.includes(cb.value);

  if (!state.queryDateRange) {
    removeTyping();
    await streamText("I need a date range. Set the 'From' and 'To' dates, then ask me again.");
    state._agentActive = false;
    return;
  }

  const res = state.currentH3Res || getEffectiveResolution(bbox, map);
  state.currentH3Res = res;
  for (const layer of Object.values(datasetLayers)) layer.clearLayers();
  state.datasetResults = {};
  state.timeline = [];

  cancelActiveRequest();
  state.activeAbortController = new AbortController();
  state.isFetching = true;
  setFetching(true);
  showLoading("Analyzing...");
  updateStatus(`Loading ${needed.join(", ")}...`);

  const { cached: agentCached, missing: agentMissing, cachedTimeline: agentCachedTimeline } = cacheLookupDatasets(bbox, res, state.queryDateRange, needed);

  if (Object.keys(agentCached).length > 0) {
    for (const [ds, data] of Object.entries(agentCached)) {
      state.datasetResults[ds] = data;
      if (plotCategories.includes(ds)) renderDatasetHexagons(ds, data.hexagons || []);
    }
    if (agentCachedTimeline.length > state.fullTimeline.length) {
      state.fullTimeline = agentCachedTimeline;
      state.timeline = applyFrequencyStep(state.fullTimeline);
    }
    updateTimelineUI();
  }

  if (agentMissing.length === 0) {
    state.isFetching = false; setFetching(false); hideLoading();
    renderAllDatasets(); updateTimelineUI();
    removeTyping();
    const report = generateInsight(question);
    const response = await getAIResponse(question, report);
    await streamText(response);
    state._agentActive = false;
    return;
  }

  try {
    const result = await fetchDatasetsH3Stream(
      bbox, state.queryDateRange.start, state.queryDateRange.end, res, agentMissing, state.activeAbortController.signal,
      (dsKey, dsData) => {
        state.datasetResults[dsKey] = dsData;
        if (plotCategories.includes(dsKey)) {
          const cb = $(`#dataset-grid input[value="${dsKey}"]`);
          if (cb && !cb.checked) cb.checked = true;
        }
        state.viewMode = "timeline";
        if (state.currentFrameIdx >= state.timeline.length) state.currentFrameIdx = 0;
        renderAllDatasets();
        updateTimelineUI();
        updateStatus(`${dsKey}: ${(dsData.frames || []).length} days loaded...`);
      }
    );
    if (result?.datasets) {
      for (const [ds, data] of Object.entries(result.datasets)) state.datasetResults[ds] = data;
      if (result.timeline?.length > state.fullTimeline.length) {
        state.fullTimeline = result.timeline;
        state.timeline = applyFrequencyStep(state.fullTimeline);
      }
      cacheStoreDatasets(bbox, res, state.queryDateRange, result.datasets, state.fullTimeline);
      renderAllDatasets();
      updateTimelineUI();
    }
  } catch (err) {
    if (err.name === "AbortError") { state.isFetching = false; setFetching(false); hideLoading(); state._agentActive = false; return; }
    removeTyping();
    addChat("agent", `I had trouble fetching the data: ${err.message}`);
    state.isFetching = false; setFetching(false); hideLoading(); state._agentActive = false;
    return;
  }

  state.isFetching = false; setFetching(false); hideLoading();
  if (askHint) askHint.classList.add("hidden");

  const totalFound = Object.values(state.datasetResults).reduce((s, d) => s + (d.total_hexagons || 0), 0);
  updateStatus(`${totalFound} hexagons · ${Object.keys(state.datasetResults).filter(ds => state.datasetResults[ds]?.total_hexagons > 0).join(", ")} · H3 res ${state.currentH3Res}`);
  if (totalFound === 0) {
    removeTyping();
    await streamText(`No data found for: ${plotCategories.join(", ")} in this area and time period.`);
    state._agentActive = false;
    return;
  }

  removeTyping();
  state._agentActive = false;
  const agentReport = generateInsight(question, state._cachedPlaceName);
  const response = await getAIResponse(question, agentReport);
  await streamText(response);
  updatePlaceNameCache(map, activeBbox);
}

// ─── Event Listeners ─────────────────────────────────────────

if (queryForm) queryForm.addEventListener("submit", (e) => { e.preventDefault(); runQuery(); });

if (chatForm) chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const msg = (chatInput ? chatInput.value : "").trim();
  if (!msg) return;
  addChat("user", msg);
  if (chatInput) chatInput.value = "";
  if (chatInput) autoExpand(chatInput);
  addTyping();
  handleAgentQuestion(msg);
});

if (chatInput) chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (chatForm) chatForm.requestSubmit();
  }
});

// Drawing
map.on("click", (e) => {
  if (!state.drawMode) return;
  state.drawPoints.push({ lat: e.latlng.lat, lng: e.latlng.lng });
  redrawDrawing();
  updateStatus(`${state.drawPoints.length} points`);
  if (state.drawPoints.length >= 3) showDrawConfirm();
});

const drawConfirmBtn = $("#draw-confirm-btn");
const drawCancelBtn = $("#draw-cancel-btn");
if (drawConfirmBtn) drawConfirmBtn.addEventListener("click", () => { if (state.drawPoints.length >= 3) confirmDrawing(getEffectiveResolution); });
if (drawCancelBtn) drawCancelBtn.addEventListener("click", () => {
  clearDrawing(); state.drawMode = false;
  if (drawToggle) { drawToggle.classList.remove("active"); drawToggle.textContent = "Draw polygon"; }
});

if (drawToggle) drawToggle.addEventListener("click", () => {
  state.drawMode = !state.drawMode;
  drawToggle.classList.toggle("active", state.drawMode);
  drawToggle.textContent = state.drawMode ? "Drawing..." : "Draw polygon";
  if (state.drawMode) { clearDrawing(); updateStatus("Click map to add points"); }
  else if (state.drawPoints.length >= 3) confirmDrawing(getEffectiveResolution);
});

if (drawClear) drawClear.addEventListener("click", () => {
  clearDrawing(); state.drawMode = false;
  if (drawToggle) { drawToggle.classList.remove("active"); drawToggle.textContent = "Draw polygon"; }
});

// Timeline
if (timelineSlider) timelineSlider.addEventListener("input", () => {
  stopPlayback(); state.viewMode = "timeline";
  state.currentFrameIdx = Number(timelineSlider.value) || 0;
  renderAllDatasets(); updateTimelineUI();
});
if (timelinePlay) timelinePlay.addEventListener("click", startPlayback);
if (timelinePause) timelinePause.addEventListener("click", () => { stopPlayback(); renderAllDatasets(); updateTimelineUI(); });
if (playSpeed) playSpeed.addEventListener("change", () => { if (state.playbackTimer) startPlayback(); });

if (frequency) frequency.addEventListener("change", () => {
  state.timeline = applyFrequencyStep(state.fullTimeline);
  if (state.currentFrameIdx >= state.timeline.length) state.currentFrameIdx = Math.max(0, state.timeline.length - 1);
  renderAllDatasets();
  updateTimelineUI();
});

// Layer toggle
if (layerSatellite) layerSatellite.addEventListener("click", () => {
  state.usingSatellite = !state.usingSatellite;
  layerSatellite.classList.toggle("active", state.usingSatellite);
  layerSatellite.textContent = state.usingSatellite ? "Satellite" : "Light";
  if (state.usingSatellite) { if (!map.hasLayer(baseSatellite)) baseSatellite.addTo(map); if (map.hasLayer(baseLight)) map.removeLayer(baseLight); }
  else { if (!map.hasLayer(baseLight)) baseLight.addTo(map); if (map.hasLayer(baseSatellite)) map.removeLayer(baseSatellite); }
});

if (exportCsv) exportCsv.addEventListener("click", exportData);

// Dataset chip toggles
for (const chip of $$("#dataset-grid input[type='checkbox']")) {
  chip.addEventListener("change", () => {
    const ds = chip.value;
    const layer = datasetLayers[ds];
    if (!layer) return;
    if (chip.checked) {
      if (!map.hasLayer(layer)) layer.addTo(map);
      if (state.queryDateRange && !state.datasetResults[ds]) scheduleRefetch();
      else if (state.datasetResults[ds]) renderAllDatasets();
    } else {
      layer.clearLayers();
    }
    updateTimelineUI();
  });
}

// Zoom/Pan
map.on("zoomstart", cancelRefetch);
map.on("movestart", cancelRefetch);
map.on("zoomend", () => {
  if (state.userResolutionOverride === 0) {
    const newRes = getEffectiveResolution(activeBbox(map), map);
    if (newRes !== state.currentH3Res) state.currentH3Res = newRes;
    updateResolutionLabel();
  }
  if (!state.queryDateRange) return;
  scheduleRefetch();
});
map.on("moveend", () => {
  if (!state.queryDateRange) return;
  scheduleRefetch();
  updatePlaceNameCache(map, activeBbox);
});

// Resolution slider
if (resolutionSlider) resolutionSlider.addEventListener("input", () => {
  const val = Number(resolutionSlider.value);
  state.userResolutionOverride = val;
  state.currentH3Res = getEffectiveResolution(activeBbox(map), map);
  updateResolutionLabel();
  if (state.queryDateRange) { state.lastFetchKey = ""; scheduleRefetch(); }
});

window.addEventListener("resize", drawTimelineChart);

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runQuery(); }
  if (e.key === "Escape" && state.drawMode) {
    clearDrawing(); state.drawMode = false;
    if (drawToggle) { drawToggle.classList.remove("active"); drawToggle.textContent = "Draw polygon"; }
  }
});

// ─── Init ────────────────────────────────────────────────────

updateTimelineUI();
state.currentH3Res = getEffectiveResolution(activeBbox(map), map);
updateResolutionLabel();
addChat("agent", "Hi! I'm your Earth observation analyst. Ask me about fire, flooding, vegetation, temperatures, or land use anywhere on the planet.\n\nExamples:\n• \"Show me fires in Portugal\"\n• \"What's the vegetation health in California?\"\n• \"How hot has it been in southern Spain?\"");
