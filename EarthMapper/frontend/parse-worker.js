// WebWorker for non-blocking JSON parsing and geometry processing
// Offloads expensive parsing/transformation work to background thread

self.onmessage = (event) => {
  const { features, mapBounds } = event.data;

  try {
    // Parse and filter features on background thread
    const processedFeatures = features.map((feature) => {
      // Ensure geometry is valid
      const geometry = feature.geometry || {};
      if (!geometry.type || !geometry.coordinates) {
        return null;
      }

      // Viewport-only filtering for point features
      if (
        mapBounds &&
        geometry.type === "Point" &&
        geometry.coordinates &&
        geometry.coordinates.length >= 2
      ) {
        const lon = geometry.coordinates[0];
        const lat = geometry.coordinates[1];

        if (
          lon < mapBounds.min_lon ||
          lon > mapBounds.max_lon ||
          lat < mapBounds.min_lat ||
          lat > mapBounds.max_lat
        ) {
          return null; // Outside viewport
        }
      }

      return feature;
    }).filter(Boolean); // Remove nulls

    self.postMessage({
      success: true,
      features: processedFeatures,
      count: processedFeatures.length,
    });
  } catch (error) {
    self.postMessage({
      success: false,
      error: error.message,
    });
  }
};
