/* EarthAnalyser — entry point: event wiring + query orchestration */

import { API_BASE } from "./config.js";
import { state } from "./state.js";
import {
  $, $$, autoExpand, updateStatus, setFetching, showLoading, hideLoading, setProgress,
  parseDateRange, activeBbox, getCheckedDatasets, makeFetchKey, getEffectiveResolution,
  updateResolutionLabel, guardResolutionForArea,
  extractDateRange, extractPlaceName, startDate, endDate, frequency, playSpeed,
  drawToggle, drawClear, layerSatellite, timelinePlay, timelinePause, timelineSlider,
  exportCsv, askHint, chatForm, chatInput, queryForm, resolutionSlider, resolutionValue,
} from "./utils.js";
import {
  map, baseSatellite, baseLight, datasetLayers, drawBoundary, clearBoundary,
  clearDrawing, showDrawConfirm, redrawDrawing, confirmDrawing,
} from "./map.js";
import { renderAllDatasets, renderDatasetHexagons } from "./rendering.js";
import { applyFrequencyStep, stopPlayback, startPlayback, updateTimelineUI, drawTimelineChart, allDatasetsStatic } from "./timeline.js";
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
  if (datasets.length === 0) return;
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
  if (datasets.length === 0) { updateStatus("Select at least one dataset"); return; }
  state.queryDateRange = range;
  state.currentQuery = "";

  const bbox = activeBbox(map);
  guardResolutionForArea(bbox);
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

// ─── Intent classification ───────────────────────────────────

function classifyIntent(question) {
  const q = question.toLowerCase().trim();
  if (q.length < 3) return "chat";

  // Greetings, thanks, random
  if (/^(hi|hello|hey|hola|sup|yo|thanks|thank you|ok|okay|cool|nice|great|bye|goodbye|cheers|lol|haha|hmm|wow|damn|wtf|what the)\b/i.test(q)) return "chat";
  // About the agent
  if (/^(who are you|what are you|what can you do|how do you work|help|what is this|are you ai|are you real)/i.test(q)) return "chat";

  // Does it have earth/data keywords?
  const hasEarthKeywords = /fire|flood|vegetation|weather|temp|heat|cold|burn|wildfire|water|rain|plant|ndvi|land.?use|urban|crop|drought|forest|green|dry|risk|danger/i.test(q);
  const hasActionKeywords = /\b(show|analyz|check|find|get|fetch|plot|display|look at|explore|monitor|assess|compare)\b/i.test(q);
  const hasDateRef = extractDateRange(question) !== null;
  const usesDrawnArea = /\b(drawn|selected|current|this|my)\s+(bbox|area|polygon|region|selection|box|zone)\b/i.test(question)
      || /\b(bbox|drawn area|drawn polygon|selected area)\b/i.test(question);

  // Geographic feature keywords (deserts, lakes, mountains, etc.)
  const hasGeoFeature = /\b(desert|lake|river|mountain|valley|ocean|sea|island|peninsula|gulf|bay|canyon|plateau|glacier|volcano|basin|coast|cape|reef|jungle|rainforest|savanna|tundra|wetland|marsh|delta|strait|fjord|crater|national\s*park|reserve)\b/i.test(q);

  const looksLikeGeoQuery = hasEarthKeywords || hasActionKeywords || hasDateRef || usesDrawnArea || hasGeoFeature;

  // Check for place-like patterns (e.g. "in Portugal", "near Madrid")
  const hasPlacePattern = /\b(in|at|near|around|over|of)\s+[A-Z]/.test(question)
      || hasGeoFeature
      || /[A-Z][a-z]+\s+[A-Z][a-z]+/.test(question);  // Capitalized multi-word names

  // Has loaded data and no geo signals → follow-up
  const hasData = Object.values(state.datasetResults).some(d => d?.total_hexagons > 0);
  if (hasData && !looksLikeGeoQuery && !hasPlacePattern) {
    // Earth-related follow-up
    if (/why|cause|trend|pattern|normal|compare|worse|better|change|tell me more|what about|is (this|that|it)|should i|how (bad|good|much|many)|safe/i.test(q)) return "follow_up";
    // Question while data is loaded
    if (/\?$/.test(q) || /^(why|what|how|when|where|is|are|do|does|can|could|should|will|would)\b/i.test(q)) return "follow_up";
    return "chat";
  }

  if (looksLikeGeoQuery || hasPlacePattern) return "data_query";
  return "chat";
}

function handleChatMessage(question) {
  const q = question.toLowerCase().trim();
  removeTyping();
  let response;
  if (/^(hi|hello|hey|hola|sup|yo)\b/i.test(q)) {
    response = "Hey! I'm Earth Analyst. Ask me about fires, vegetation, weather, or floods anywhere on the planet. Try something like \"show me fires in Portugal last summer\" or just pick a spot on the map and ask what's happening there.";
  } else if (/^(thanks|thank you|cheers)\b/i.test(q)) {
    response = "You're welcome! Let me know if you want to explore more.";
  } else if (/^(who are you|what are you|what can you do|help)\b/i.test(q)) {
    response = "I'm Earth Analyst — I help you explore satellite data about our planet. I can show you fire activity, vegetation health, surface temperatures, flood risk, and land use for any place and time range. Just tell me where and when you're interested in, or ask me to explain what the data means.";
  } else if (/^(how do you work|what is this)\b/i.test(q)) {
    response = "I pull satellite data from NASA MODIS, ESA WorldCover, and ERA5 weather archives, then crunch it into hexagonal cells you can see on the map. Ask me about a place and time, and I'll fetch the relevant data and explain what I find.";
  } else {
    response = "I'm not sure what you mean by that. I'm best at answering questions about Earth observation data — try asking about fires, vegetation, weather, or flooding in a specific area. For example: \"Any fires near Madrid last month?\" or \"How's the vegetation in the Amazon this year?\"";
  }
  streamText(response);
}

// ─── Agent Question Handler ──────────────────────────────────

async function handleAgentQuestion(question) {
  state._agentActive = true;

  // ─── Intent classification: chat, follow-up, or data query?
  const intent = classifyIntent(question);

  if (intent === "chat") {
    handleChatMessage(question);
    state._agentActive = false;
    return;
  }

  if (intent === "follow_up") {
    // Check if the question asks about datasets we haven't loaded yet
    const asked = inferNeededDatasets(question);
    const loaded = Object.keys(state.datasetResults).filter(k => state.datasetResults[k]?.total_hexagons > 0);
    const missing = asked.filter(ds => !loaded.includes(ds));

    if (missing.length > 0 && state.queryDateRange) {
      // Fetch the missing data before answering
      addTyping();
      updateStatus(`Loading ${missing.join(", ")} for analysis...`);
      const bbox = activeBbox(map);
      const res = state.currentH3Res || getEffectiveResolution(bbox, map);
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        await fetchDatasetsH3Stream(
          bbox, state.queryDateRange.start, state.queryDateRange.end, res, missing, controller.signal,
          (dsKey, dsData) => {
            state.datasetResults[dsKey] = dsData;
            renderDatasetHexagons(dsKey, dsData.hexagons || []);
            updateTimelineUI();
          }
        );
        clearTimeout(timeout);
      } catch (e) { /* answer with whatever we have */ }
      removeTyping();
    }

    removeTyping();
    const report = generateInsight(question, state._cachedPlaceName);
    const response = await getAIResponse(question, report);
    await streamText(response);
    state._agentActive = false;
    updatePlaceNameCache(map, activeBbox);
    return;
  }

  // ─── intent === "data_query": geocode + fetch data
  const placeQuery = extractPlaceName(question);
  const extractedRange = extractDateRange(question);
  const usesDrawnArea = /\b(drawn|selected|current|this|my)\s+(bbox|area|polygon|region|selection|box|zone)\b/i.test(question)
      || /\b(bbox|drawn area|drawn polygon|selected area)\b/i.test(question);

  // Run LLM classification (may also provide corrected place/date)
  const classifyResult = await Promise.race([
    fetch(`${API_BASE}/datasets/classify`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    }).then(r => r.ok ? r.json() : null).catch(() => null),
    new Promise(r => setTimeout(() => r(null), 8000)),
  ]);

  // Apply date range: prefer LLM result, then client-side extraction, then smart default
  if (classifyResult?.date_start && classifyResult?.date_end) {
    state.queryDateRange = { start: classifyResult.date_start, end: classifyResult.date_end };
    if (startDate) startDate.value = classifyResult.date_start;
    if (endDate) endDate.value = classifyResult.date_end;
  } else if (extractedRange) {
    state.queryDateRange = extractedRange;
    if (startDate) startDate.value = extractedRange.start;
    if (endDate) endDate.value = extractedRange.end;
  } else {
    // Agent infers a default: last 3 months
    const now = new Date();
    const end = now.toISOString().slice(0, 10);
    const startD = new Date(now);
    startD.setMonth(startD.getMonth() - 3);
    const start = startD.toISOString().slice(0, 10);
    state.queryDateRange = { start, end };
    if (startDate) startDate.value = start;
    if (endDate) endDate.value = end;
  }

  // Geocode: use extracted place name, or the full question as fallback if it looks like a place
  let bbox = null;
  const hasGeoFeatureWord = /\b(desert|lake|river|mountain|valley|ocean|sea|island|peninsula|gulf|bay|canyon|plateau|glacier|volcano|basin|coast|cape|reef|jungle|rainforest|savanna|tundra|wetland|marsh|delta|strait|fjord|crater|national\s*park|reserve)\b/i.test(question);
  const geocodeQuery = placeQuery
      || ((/\b(in|at|near|around)\s+\w/i.test(question)) ? question : null)
      || (hasGeoFeatureWord ? question : null)
      || ((/[A-Z][a-z]+\s+[A-Z][a-z]+/.test(question)) ? question : null);

  if (usesDrawnArea && state.resolvedBbox) {
    bbox = [...state.resolvedBbox];
    map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [30, 30], animate: true });
    state.currentH3Res = getEffectiveResolution(bbox, map);
  } else if (geocodeQuery) {
    try {
      updateStatus(`Locating "${placeQuery || geocodeQuery}"...`);
      const geoRes = await fetch(`${API_BASE}/datasets/geocode`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: geocodeQuery }),
      });
      if (geoRes.ok) {
        const geo = await geoRes.json();
        if (geo.bbox && geo.lat && geo.lon) {
          bbox = geo.bbox;
          // "near" implies a wider area — expand bbox by 50%
          if (/\bnear\b/i.test(question)) {
            const [w, s, e, n] = bbox;
            const lngExpand = (e - w) * 0.25;
            const latExpand = (n - s) * 0.25;
            bbox = [w - lngExpand, s - latExpand, e + lngExpand, n + latExpand];
          }
          // If bbox is too small (< ~0.1° span), expand to a minimum usable size
          const lngSpan = bbox[2] - bbox[0];
          const latSpan = bbox[3] - bbox[1];
          const MIN_SPAN = 0.15;  // ~15km minimum
          if (lngSpan < MIN_SPAN || latSpan < MIN_SPAN) {
            const cLng = (bbox[0] + bbox[2]) / 2;
            const cLat = (bbox[1] + bbox[3]) / 2;
            const halfSpan = MIN_SPAN / 2;
            bbox = [cLng - halfSpan, cLat - halfSpan, cLng + halfSpan, cLat + halfSpan];
          }
          state.resolvedBbox = [...bbox];
          if (geo.polygon) { state.activePolygon = geo.polygon; drawBoundary(geo.polygon); } else { clearBoundary(); }
          map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [40, 40], animate: true });
          state.currentH3Res = getEffectiveResolution(bbox, map);
          // For small areas, ensure resolution is at least 6
          if (lngSpan < 0.5 && latSpan < 0.5 && state.currentH3Res < 6) {
            state.currentH3Res = 6;
          }
          updateStatus(`Centered on ${geo.name?.split(",")[0] || placeQuery || geocodeQuery}`);
          await new Promise(r => setTimeout(r, 300));
        }
      }
    } catch (e) { /* fallback to viewport */ }
  }

  // Fallback: if no bbox from geocode/drawn area, use viewport
  if (!bbox) {
    bbox = activeBbox(map);
  }

  let plotCategories = classifyResult?.categories?.length ? classifyResult.categories : inferNeededDatasets(question);
  // For initial data queries, default to fire if nothing was inferred
  if (plotCategories.length === 0) plotCategories = ["fire"];
  const contextCategories = classifyResult?.context_categories?.length ? classifyResult.context_categories : inferContextDatasets(question, plotCategories);
  const needed = [...new Set([...plotCategories, ...contextCategories])];

  for (const cb of $$("#dataset-grid input[type='checkbox']")) cb.checked = plotCategories.includes(cb.value);

  guardResolutionForArea(bbox);
  const res = state.currentH3Res || getEffectiveResolution(bbox, map);
  state.currentH3Res = res;
  for (const layer of Object.values(datasetLayers)) layer.clearLayers();
  state.datasetResults = {};
  state.timeline = [];
  state.currentFrameIdx = 0;

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
  try {
    const agentReport = generateInsight(question, state._cachedPlaceName);
    const response = await getAIResponse(question, agentReport);
    await streamText(response);
  } catch (e) {
    const fallback = `Found ${totalFound} hexagons for ${plotCategories.join(", ")}. Ask me follow-up questions about this data.`;
    await streamText(fallback);
  }
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

// Clear map
const clearMapBtn = $("#clear-map");
if (clearMapBtn) clearMapBtn.addEventListener("click", () => {
  stopPlayback();
  cancelActiveRequest();
  for (const layer of Object.values(datasetLayers)) layer.clearLayers();
  clearDrawing();
  clearBoundary();
  state.drawMode = false;
  state.drawPoints = [];
  state.resolvedBbox = null;
  state.activePolygon = null;
  state.datasetResults = {};
  state.timeline = [];
  state.fullTimeline = [];
  state.currentFrameIdx = 0;
  state.queryDateRange = null;
  state.lastFetchKey = "";
  state.lastFetchBbox = null;
  state.lastFetchRes = null;
  state.isFetching = false;
  state.viewMode = "aggregate";
  state._cachedPlaceName = "";
  if (drawToggle) { drawToggle.classList.remove("active"); drawToggle.textContent = "Draw polygon"; }
  updateTimelineUI();
  updateStatus("Map cleared. Ready for a new search.");
});

// Timeline
if (timelineSlider) timelineSlider.addEventListener("input", () => {
  if (allDatasetsStatic()) return;
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
