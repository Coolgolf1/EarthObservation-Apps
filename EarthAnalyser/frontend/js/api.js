/* EarthAnalyser — API communication, streaming NDJSON */

import { API_BASE } from "./config.js";
import { state } from "./state.js";
import { applyFrequencyStep, updateTimelineUI } from "./timeline.js";

export async function fetchDatasetsH3Stream(bbox, start, end, resolution, datasets, signal, onDataset) {
  const res = await fetch(`${API_BASE}/datasets/h3/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bbox, date_start: start, date_end: end, resolution, datasets, max_frames: 365 }),
    signal,
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text.slice(0, 200)}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const result = { datasets: {}, timeline: [] };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let newlineIdx;
    while ((newlineIdx = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newlineIdx).trim();
      buffer = buffer.slice(newlineIdx + 1);
      if (!line) continue;

      try {
        const msg = JSON.parse(line);
        if (msg.type === "header") {
          result.timeline = msg.timeline || [];
          if (result.timeline.length > 0 && (result.timeline.length > state.fullTimeline.length || state.timeline.length === 0)) {
            state.fullTimeline = result.timeline;
            state.timeline = applyFrequencyStep(state.fullTimeline);
            state.viewMode = "timeline";
            if (state.currentFrameIdx >= state.timeline.length) state.currentFrameIdx = 0;
            updateTimelineUI();
          }
        } else if (msg.type === "frame") {
          const key = msg.key;
          if (!result.datasets[key]) {
            result.datasets[key] = { dataset: key, frames: [], hexagons: [], total_hexagons: 0, frames_with_data: 0 };
          }
          result.datasets[key].frames.push(msg.frame);
          result.datasets[key].frames_with_data++;
          if (onDataset) onDataset(key, result.datasets[key]);
        } else if (msg.type === "dataset_done") {
          if (result.datasets[msg.key]) {
            result.datasets[msg.key].hexagons = msg.data.hexagons || [];
            result.datasets[msg.key].total_hexagons = msg.data.total_hexagons || 0;
          } else {
            const hexagons = msg.data.hexagons || [];
            result.datasets[msg.key] = {
              ...msg.data,
              frames: hexagons.length > 0 ? [{ acq_date: "static", hexagons }] : [],
              frames_with_data: hexagons.length > 0 ? 1 : 0,
            };
          }
          if (onDataset) onDataset(msg.key, result.datasets[msg.key]);
        } else if (msg.type === "dataset") {
          result.datasets[msg.key] = msg.data;
          if (onDataset) onDataset(msg.key, msg.data);
        } else if (msg.type === "error") {
          throw new Error(msg.detail || "Stream error");
        }
      } catch (parseErr) {
        if (parseErr.message && !parseErr.message.includes("JSON")) throw parseErr;
      }
    }
  }

  return result;
}

export function cancelActiveRequest() {
  if (state.activeAbortController) {
    state.activeAbortController.abort();
    state.activeAbortController = null;
  }
  fetch(`${API_BASE}/datasets/cancel`, { method: "POST" }).catch(() => {});
}
