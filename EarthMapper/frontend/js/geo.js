/* EarthMapper — geometry extent calculations and bounds checking */

export function updateExtents(extents, lon, lat) {
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;
  extents.minLon = Math.min(extents.minLon, lon);
  extents.maxLon = Math.max(extents.maxLon, lon);
  extents.minLat = Math.min(extents.minLat, lat);
  extents.maxLat = Math.max(extents.maxLat, lat);
}

export function walkCoordinates(node, extents) {
  if (!Array.isArray(node)) return;
  if (node.length >= 2 && typeof node[0] === "number" && typeof node[1] === "number") {
    updateExtents(extents, Number(node[0]), Number(node[1]));
    return;
  }
  for (const child of node) walkCoordinates(child, extents);
}

export function getGeometryExtents(geometry) {
  if (!geometry || geometry.coordinates == null) return null;
  const extents = { minLon: Infinity, maxLon: -Infinity, minLat: Infinity, maxLat: -Infinity };
  walkCoordinates(geometry.coordinates, extents);
  if (!Number.isFinite(extents.minLon) || !Number.isFinite(extents.maxLon) ||
      !Number.isFinite(extents.minLat) || !Number.isFinite(extents.maxLat)) return null;
  return extents;
}

export function featureInBounds(feature, bounds) {
  const geometry = feature.geometry || {};
  if (geometry.type !== "Point") {
    const extents = getGeometryExtents(geometry);
    if (!extents) return false;
    const geometryBounds = L.latLngBounds([extents.minLat, extents.minLon], [extents.maxLat, extents.maxLon]);
    return bounds.intersects(geometryBounds);
  }
  const coords = geometry.coordinates || [];
  if (!Array.isArray(coords) || coords.length < 2) return false;
  const lon = Number(coords[0]);
  const lat = Number(coords[1]);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return false;
  return bounds.contains(L.latLng(lat, lon));
}

export function layerInBounds(layer, bounds) {
  if (typeof layer.getLatLng === "function") return bounds.contains(layer.getLatLng());
  if (typeof layer.getBounds === "function") return bounds.intersects(layer.getBounds());
  return featureInBounds(layer.feature || {}, bounds);
}

export function isSmallBboxMove(previousBbox, nextBbox, ratio) {
  if (!previousBbox || !nextBbox) return false;
  const prevWidth = Math.max(1e-9, Math.abs(previousBbox.east - previousBbox.west));
  const prevHeight = Math.max(1e-9, Math.abs(previousBbox.north - previousBbox.south));
  const prevCx = (previousBbox.east + previousBbox.west) / 2;
  const prevCy = (previousBbox.north + previousBbox.south) / 2;
  const nextCx = (nextBbox.east + nextBbox.west) / 2;
  const nextCy = (nextBbox.north + nextBbox.south) / 2;
  return Math.abs(nextCx - prevCx) / prevWidth <= ratio && Math.abs(nextCy - prevCy) / prevHeight <= ratio;
}
