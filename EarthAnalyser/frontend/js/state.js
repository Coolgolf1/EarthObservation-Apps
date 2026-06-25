/* EarthAnalyser — shared mutable application state */

export const state = {
  drawMode: false,
  drawPoints: [],
  resolvedBbox: null,
  activePolygon: null,

  datasetResults: {},
  timeline: [],
  fullTimeline: [],
  currentFrameIdx: 0,
  playbackTimer: null,

  lastFetchKey: "",
  lastFetchBbox: null,
  lastFetchRes: null,
  currentH3Res: 4,
  queryDateRange: null,
  isFetching: false,
  refetchTimer: null,
  activeAbortController: null,
  _fetchGeneration: 0,
  _agentActive: false,

  userResolutionOverride: 0,
  usingSatellite: true,
  currentQuery: "",
  viewMode: "aggregate",

  _cachedPlaceName: "",
  _placeNameBbox: "",
};

export const MAX_CACHE_ENTRIES = 30;
export const resultCache = new Map();
export const REFETCH_DWELL_MS = 800;
