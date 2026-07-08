/* EarthAnalyser — LRU per-dataset result cache */

import { resultCache, MAX_CACHE_ENTRIES } from "./state.js";
import { makeDatasetCacheKey } from "./utils.js";

export function cacheGet(key) {
  if (!resultCache.has(key)) return null;
  const val = resultCache.get(key);
  resultCache.delete(key);
  resultCache.set(key, val);
  return val;
}

export function cachePut(key, value) {
  if (resultCache.has(key)) resultCache.delete(key);
  resultCache.set(key, value);
  if (resultCache.size > MAX_CACHE_ENTRIES) {
    const firstKey = resultCache.keys().next().value;
    resultCache.delete(firstKey);
  }
}

export function cacheStoreDatasets(bbox, res, dateRange, datasets, timeline) {
  for (const [dsName, dsData] of Object.entries(datasets)) {
    const key = makeDatasetCacheKey(bbox, res, dateRange, dsName);
    cachePut(key, { data: dsData, timeline });
  }
}

export function cacheLookupDatasets(bbox, res, dateRange, datasetNames) {
  const cached = {};
  const missing = [];
  let cachedTimeline = [];
  for (const ds of datasetNames) {
    const key = makeDatasetCacheKey(bbox, res, dateRange, ds);
    const entry = cacheGet(key);
    if (entry) {
      cached[ds] = entry.data;
      if (entry.timeline && entry.timeline.length > cachedTimeline.length) cachedTimeline = entry.timeline;
    } else {
      missing.push(ds);
    }
  }
  return { cached, missing, cachedTimeline };
}
