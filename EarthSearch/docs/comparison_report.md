# EarthMapper vs ESRI Geospatial Comparison Report

## Executive Summary
The current comparison suggests that EarthMapper and ESRI Geocode API are **not direct substitutes**, but they can be **highly complementary**.

- ESRI geocoding is strongly address/street-oriented (streets, places, administrative labels).
- EarthMapper is stronger on mapped geographic features (for example hydrography and curated non-address features).
- This explains low overlap in some categories and supports a combined strategy: use both systems together.

## What The Results Show
The final notebook run completed 9 scenarios (3 bbox sizes x 3 categories):
- `hydrography`
- `transport`
- `places`

### Coverage behavior
1. `hydrography`:
- Large Spain: EarthMapper 175 vs ESRI 19, intersection 11, coverage ratio 0.060.
- Medium Catalonia: EarthMapper 33 vs ESRI 8, intersection 3, coverage ratio 0.079.
- Small Madrid: EarthMapper 16 vs ESRI 2, intersection 0, coverage ratio 0.000.

2. `transport`:
- Large Spain: EarthMapper 300 vs ESRI 1, intersection 1, coverage ratio 0.003.
- Medium Catalonia: EarthMapper 51 vs ESRI 5, intersection 3, coverage ratio 0.057.
- Small Madrid: EarthMapper 13 vs ESRI 0, intersection 0, coverage ratio 0.000.

3. `places`:
- Large Spain: EarthMapper 300 vs ESRI 22, intersection 10, coverage ratio 0.032.
- Medium Catalonia: EarthMapper 76 vs ESRI 44, intersection 21, coverage ratio 0.212 (best overlap).
- Small Madrid: EarthMapper 8 vs ESRI 2, intersection 2, coverage ratio 0.250 (good local overlap).

## Interpretation
Your interpretation is consistent with the observed behavior:

- ESRI Geocode API is returning many address/street-oriented candidates.
- EarthMapper is returning feature datasets where streets are not always represented in the same way (or at all), but natural and curated geospatial features are present.
- Therefore, overlap is naturally low in categories where data models differ.

In short:
- ESRI is strong for geocoding and street/address context.
- EarthMapper is strong for domain geospatial layers.
- A fusion approach can produce better coverage than either system alone.

## Why They Can Work Well Together
A practical hybrid model:

1. Use ESRI first for user text geocoding and street/address intent.
2. Use EarthMapper for map feature enrichment and spatial layers.
3. Join/enrich with Lakebase (`gadm`, `natural_earth`) to add metadata and hierarchy.
4. Return a unified response with source attribution and confidence.

## Recommended Integration Strategy
1. Intent routing
- If query looks like address/street/POI -> ESRI-first.
- If query looks like river/lake/mountain/region feature -> EarthMapper-first.

2. Cross-enrichment
- From ESRI point/extent, query EarthMapper bbox for nearby features.
- From EarthMapper feature name/centroid, request ESRI geocode candidates for user-facing labels.

3. Unified output contract
- Keep normalized schema used in notebook:
  - `id`, `name`, `category`, `subcategory`, `geometry`, `properties`, `source`
- Add `confidence` and `match_method` fields.

4. Monitoring metrics
- Keep tracking coverage ratio and intersection by category.
- Track category-specific precision/recall in a validation set.
- Add per-category fallback rates and source contribution percentages.

## Conclusion
The low overlap does not mean one API is bad; it means they solve different parts of the problem.

Given the current evidence, combining ESRI (street/geocode strength) with EarthMapper (feature-layer strength) is a strong architecture choice.
