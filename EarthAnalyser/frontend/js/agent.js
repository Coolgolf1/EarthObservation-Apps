/* EarthAnalyser — AI agent, chat UI, insight generation */

import { API_BASE } from "./config.js";
import { state } from "./state.js";
import { $, getCheckedDatasets } from "./utils.js";

// ─── Chat UI ─────────────────────────────────────────────────

const chatLog = $("#chat-log");

export function addChat(role, text) {
  if (!chatLog) return;
  const el = document.createElement("div");
  el.className = `chat-msg ${role}`;
  el.textContent = text;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
}

export function addTyping() {
  if (!chatLog) return;
  const el = document.createElement("div");
  el.className = "chat-msg agent";
  el.id = "typing";
  el.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
}

export function removeTyping() { const el = $("#typing"); if (el) el.remove(); }

export function streamText(text) {
  return new Promise((resolve) => {
    removeTyping();
    if (!chatLog) { resolve(); return; }
    const el = document.createElement("div");
    el.className = "chat-msg agent";
    chatLog.appendChild(el);

    const html = formatInsightHTML(text);
    if (html) {
      el.classList.add("rich");
      el.innerHTML = html;
      chatLog.scrollTop = chatLog.scrollHeight;
      resolve();
      return;
    }

    el.textContent = text;
    chatLog.scrollTop = chatLog.scrollHeight;
    resolve();
  });
}

// ─── Rich insight cards ──────────────────────────────────────

function formatInsightHTML(text) {
  if (!text) return null;
  const hasStructure = /\b(FIRE|TEMPERATURE|VEGETATION|WATER|LAND USE|WHY):/i.test(text);
  if (!hasStructure) return null;

  let html = '<div class="insight-cards">';
  const introMatch = text.match(/^([\s\S]*?)(?=(?:FIRE|TEMPERATURE|VEGETATION|WATER|LAND USE|WHY):)/i);
  const intro = introMatch ? introMatch[1].replace(/REPORT:.*\n?/i, "").replace(/Area:.*\n?/i, "").replace(/Location:.*\n?/i, "").replace(/---\n?/g, "").trim() : "";
  if (intro) html += `<div class="insight-intro">${_esc(intro)}</div>`;

  const sections = [
    { key: "fire",    regex: /FIRE:\s*([\s\S]*?)(?=\n\n|\nTEMPERATURE:|\nVEGETATION:|\nWATER:|\nLAND USE:|\nWHY:|\n---|$)/i, icon: "🔥", title: "Fire Activity" },
    { key: "weather", regex: /TEMPERATURE:\s*([\s\S]*?)(?=\n\n|\nFIRE:|\nVEGETATION:|\nWATER:|\nLAND USE:|\nWHY:|\n---|$)/i, icon: "🌡️", title: "Temperature" },
    { key: "vegetation", regex: /VEGETATION:\s*([\s\S]*?)(?=\n\n|\nFIRE:|\nTEMPERATURE:|\nWATER:|\nLAND USE:|\nWHY:|\n---|$)/i, icon: "🌱", title: "Vegetation" },
    { key: "water",   regex: /WATER:\s*([\s\S]*?)(?=\n\n|\nFIRE:|\nTEMPERATURE:|\nVEGETATION:|\nLAND USE:|\nWHY:|\n---|$)/i, icon: "🌊", title: "Water / Flood" },
    { key: "landuse", regex: /LAND USE:\s*([\s\S]*?)(?=\n\n|\nFIRE:|\nTEMPERATURE:|\nVEGETATION:|\nWATER:|\nWHY:|\n---|$)/i, icon: "🏙️", title: "Land Use" },
    { key: "why",     regex: /WHY:\s*([\s\S]*?)(?=\n---|$)/i, icon: "💡", title: "Analysis" },
  ];
  for (const s of sections) {
    const m = text.match(s.regex);
    if (m) html += `<div class="insight-card ${s.key}"><div class="card-header">${s.icon} ${_esc(s.title)}</div><div class="card-body">${_highlightMetrics(m[1].trim())}</div></div>`;
  }

  html += "</div>";
  return html;
}

function _esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function _highlightMetrics(text) {
  let s = _esc(text);
  s = s.replace(/(\d+\.?\d*)\s*°C/g, '<span class="metric">$1°C</span>');
  s = s.replace(/(\d+\.?\d*)%/g, '<span class="metric">$1%</span>');
  s = s.replace(/NDVI\s*(\d+\.\d+)/g, 'NDVI <span class="metric">$1</span>');
  s = s.replace(/(\d+)\s*cells/g, '<span class="metric">$1</span> cells');
  s = s.replace(/(\d+)\s*days/g, '<span class="metric">$1</span> days');
  s = s.replace(/(\d[\d,]*)\s*(?:total\s)?pixels/g, '<span class="metric">$1</span> pixels');
  s = s.replace(/\n/g, "<br>");
  return s;
}

// ─── LLM integration ────────────────────────────────────────

let _llmAvailable = false;
let _llmFailCount = 0;
let _llmChecked = false;

export async function getAIResponse(question, dataReport) {
  if (!_llmChecked) {
    _llmChecked = true;
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 8000);
      const res = await fetch(`${API_BASE}/datasets/analyse`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: question || "test", data_report: dataReport }),
        signal: controller.signal,
      });
      clearTimeout(timeout);
      if (res.ok) {
        const data = await res.json();
        if (data.model && data.model !== "fallback") { _llmAvailable = true; return data.analysis; }
        return data.analysis || dataReport;
      }
    } catch (e) { /* LLM unavailable */ }
    return dataReport;
  }

  if (!_llmAvailable) return dataReport;

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    const res = await fetch(`${API_BASE}/datasets/analyse`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question || "Summarize what you see in this area", data_report: dataReport }),
      signal: controller.signal,
    });
    clearTimeout(timeout);
    if (res.ok) {
      const data = await res.json();
      _llmFailCount = 0;
      if (data.analysis && data.model !== "fallback") return data.analysis;
      return data.analysis || dataReport;
    }
  } catch (e) {
    _llmFailCount++;
    if (_llmFailCount >= 3) {
      _llmAvailable = false;
      setTimeout(() => { _llmAvailable = true; _llmFailCount = 0; }, 60000);
    }
  }
  return dataReport;
}

// ─── Insight generation (local data summary) ─────────────────

export function generateInsight(question, placeName) {
  const allLoaded = Object.keys(state.datasetResults).filter(ds => state.datasetResults[ds]?.total_hexagons > 0);
  const checked = [...new Set([...getCheckedDatasets(), ...allLoaded])];
  const hasData = checked.some((ds) => state.datasetResults[ds]?.total_hexagons > 0);
  if (!hasData) return "No data loaded. Set dates and ask a question — I'll fetch what's needed.";

  const hasFire = state.datasetResults.fire?.total_hexagons > 0;
  const hasVeg = state.datasetResults.vegetation?.total_hexagons > 0;
  const hasWeather = state.datasetResults.weather?.total_hexagons > 0;
  const hasFlood = state.datasetResults.flood?.total_hexagons > 0;
  const hasLanduse = state.datasetResults.landuse?.total_hexagons > 0;

  let r = "";
  const period = state.queryDateRange ? `${state.queryDateRange.start} to ${state.queryDateRange.end}` : "selected period";
  r += `REPORT: ${period}\n`;
  if (placeName) r += `Location: ${placeName}\n`;
  r += "\n";

  if (hasFire) {
    const data = state.datasetResults.fire;
    const hexagons = data.hexagons || [];
    const frames = data.frames || [];
    const firePixels = hexagons.reduce((s, h) => s + (h.pixel_count || h.total_pixels || 0), 0);
    let high = 0, nom = 0, low = 0;
    for (const h of hexagons) { high += (h.classes?.high || 0); nom += (h.classes?.nominal || 0); low += (h.classes?.low || 0); }
    const total = high + nom + low;
    const fireHighPct = total > 0 ? Math.round(high / total * 100) : 0;
    r += `FIRE: ${data.total_hexagons} cells detected fire over ${frames.length} days (${firePixels.toLocaleString()} total pixels). ${fireHighPct}% high-confidence detections.\n\n`;
  }

  if (hasWeather) {
    const hexs = state.datasetResults.weather.hexagons;
    const tempSum = hexs.reduce((s, h) => s + (h.value_mean || 0), 0);
    const avgTemp = tempSum / hexs.length * 0.02 - 273.15;
    const maxTemp = Math.max(...hexs.map(h => h.value_max || 0)) * 0.02 - 273.15;
    r += `TEMPERATURE: Mean surface temp ${avgTemp.toFixed(1)}°C, max ${maxTemp.toFixed(1)}°C.\n\n`;
  }

  if (hasVeg) {
    const hexs = state.datasetResults.vegetation.hexagons;
    const avgNdvi = hexs.reduce((s, h) => s + (h.value_mean || 0), 0) / hexs.length / 10000;
    const lowVegPct = Math.round(hexs.filter(h => (h.value_mean / 10000) < 0.3).length / hexs.length * 100);
    r += `VEGETATION: Mean NDVI ${avgNdvi.toFixed(3)}. ${lowVegPct}% of area below 0.3 (dry/stressed).\n\n`;
  }

  if (hasLanduse) {
    const hexs = state.datasetResults.landuse.hexagons;
    const classCounts = {};
    for (const h of hexs) for (const [cls, cnt] of Object.entries(h.classes || {})) classCounts[cls] = (classCounts[cls] || 0) + cnt;
    const totalC = Object.values(classCounts).reduce((s, v) => s + v, 0);
    const breakdown = Object.entries(classCounts).sort(([,a],[,b]) => b - a).slice(0, 4).map(([k, v]) => `${k} ${Math.round(v / totalC * 100)}%`);
    r += `LAND USE: ${breakdown.join(", ")}.\n\n`;
  }

  if (hasFlood) {
    const data = state.datasetResults.flood;
    const hexes = data.hexagons || [];
    const totalPx = hexes.reduce((s, h) => s + (h.pixel_count || 0), 0);
    r += `WATER: ${data.total_hexagons} cells with water detected, ${totalPx} total water pixels.\n\n`;
  }

  return r;
}

// ─── Dataset inference from questions ────────────────────────

export function inferNeededDatasets(question) {
  const q = question.toLowerCase();
  const needed = new Set();
  if (/fire|frie|fier|fyre|burn|wildfire|incendio|fuego/.test(q)) needed.add("fire");
  if (/flo[ou]d|flod|water|rain|river|inundaci/.test(q)) needed.add("flood");
  if (/veget|vegitat|ndvi|plant|green|drought|plantas/.test(q)) needed.add("vegetation");
  if (/weather|wether|wheather|temp|heat|hot|cold|climate|calor/.test(q)) needed.add("weather");
  if (/land.?use|urban|crop|forest|city|ciudad/.test(q)) needed.add("landuse");
  if (/what.*(happen|going|see)|analyz|summar|overview|assess|everything|all/.test(q)) {
    for (const ds of ["fire", "flood", "weather", "vegetation", "landuse"]) needed.add(ds);
  }
  return [...needed];
}

export function inferContextDatasets(question, plotDatasets) {
  const q = question.toLowerCase();
  const context = new Set();
  if (/why|cause|reason|explain|because|led to|trigger|risk|danger|threat|prevent/.test(q)) {
    if (plotDatasets.includes("fire")) { context.add("weather"); context.add("vegetation"); }
    if (plotDatasets.includes("flood")) { context.add("weather"); context.add("landuse"); }
    if (plotDatasets.includes("vegetation")) context.add("weather");
  }
  for (const ds of plotDatasets) context.delete(ds);
  return [...context];
}

// ─── Reverse geocode place name cache ────────────────────────

export function updatePlaceNameCache(map, activeBbox) {
  const bbox = activeBbox(map);
  const key = bbox.map(v => v.toFixed(1)).join(",");
  if (key === state._placeNameBbox) return;
  state._placeNameBbox = key;
  const [w, s, e, n] = bbox;
  const lat = (s + n) / 2, lon = (w + e) / 2;
  fetch(`${API_BASE}/datasets/reverse-geocode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lat, lon }),
  }).then(r => r.ok ? r.json() : null).then(d => {
    if (d?.name) state._cachedPlaceName = d.name;
  }).catch(() => {});
}
