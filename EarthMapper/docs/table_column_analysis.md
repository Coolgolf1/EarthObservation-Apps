# Table Column Analysis

Live schema checked against the configured PostgreSQL database on 2026-06-19.

## App-facing tables

The API currently reads from:

- `public.natural_earth_silver`
- `public.gadm_silver`

The raw/source tables are also present:

- `public.natural_earth`
- `public.gadm`

`public.spatial_ref_sys` is a PostGIS metadata table and is not feature data.

## Current API exposure

`backend/app/routers/features.py` currently selects only:

- `id`
- `name`
- `category`
- `subcategory`
- `geom`
- synthetic `source`

So the new normalized/enrichment columns are not visible to the frontend yet.

## Shared silver columns

Both silver tables include normalized geometry and map-serving fields:

- `coords`: GeoJSON-like original geometry payload.
- `geom`: main PostGIS geometry used for bbox intersection and output.
- `centroid`: precomputed center point, useful for labels, clustering, and ranking nearby features.
- `source_layer`, `source_scale`, `source_id`: lineage/debug metadata.
- `search_name`: normalized text for search matching.
- `min_zoom`, `label_min_zoom`: visibility thresholds.
- `display_rank`, `importance_score`: ranking/sorting controls.
- `area_m2`, `length_m`, `vertex_count`: geometry metrics.
- `geom_z4`, `geom_z7`, `geom_z10`: simplified geometries for low/mid/high zoom rendering.
- `tile_z4`, `tile_z6`, `tile_z8`, `tile_z10`, `tile_z12`: tile bucketing keys for prefiltering/caching.

## `gadm_silver`

Category coverage:

- `administrative`: 348,694 rows.

Subcategory coverage:

- `country`: 256
- `state`: 3,648
- `county`: 47,187
- `district`: 144,193
- `ward`: 153,410

New columns are complete enough to use now. The checked enrichment fields are populated for 100% of rows, except lower hierarchy names that naturally do not exist on higher-level boundaries:

- `admin1_name`: 348,438 rows
- `admin2_name`: 344,790 rows
- `admin3_name`: 297,603 rows
- `admin_type_en`: 348,438 rows
- `alt_names`: 348,438 rows

Recommended use:

- Use `admin_level` and `subcategory` to filter hierarchy depth.
- Use `country_code` and `country_name` for country filtering and UI facets.
- Use `admin_id`, `admin_name`, `admin1_name`, `admin2_name`, `admin3_name` for drill-down, breadcrumbs, and exact boundary identification.
- Use `admin_type_en` and `alt_names` in details popups or search.
- Use `search_name` for backend text search.
- Use `min_zoom`, `label_min_zoom`, `display_rank`, and `importance_score` to avoid loading or labelling too many administrative polygons.
- Use `geom_z4`, `geom_z7`, and `geom_z10` for zoom-dependent geometry simplification.
- Use `area_m2`, `length_m`, and `vertex_count` for ranking, simplification diagnostics, and excluding tiny boundaries at low zoom.

Zoom/rank patterns:

- `country`: `min_zoom = 2`, `label_min_zoom = 3`, `display_rank = 100`
- `state`: `min_zoom = 5`, `label_min_zoom = 6`, `display_rank = 80`
- `county`: `min_zoom = 7`, `label_min_zoom = 8`, `display_rank = 60`
- `district`: `min_zoom = 9`, `label_min_zoom = 10`, `display_rank = 40`
- `ward`: `min_zoom = 11`, `label_min_zoom = 12`, `display_rank = 20`

## `natural_earth_silver`

Category coverage:

- `transport`: 57,728 rows
- `places`: 34,087 rows
- `hydrography`: 25,073 rows
- `terrain`: 22,395 rows
- `regions`: 2,065 rows
- `landmarks`: 1,216 rows

Base fields `id`, `name`, `category`, `subcategory`, `coords`, `geom`, and `centroid` are the reliable fields for this table.

Important caveat: the new enrichment fields are mostly empty. Only 842 of 142,564 rows have fields like `source_layer`, `source_scale`, `source_id`, `feature_class`, `min_zoom`, `label_min_zoom`, `display_rank`, `importance_score`, `area_m2`, `length_m`, `vertex_count`, and tile ids populated.

There is also a data-quality issue in those populated enrichment rows. Many rows categorized as `places` have `feature_class` values such as `River`, `Lake Centerline`, or `Intermittent River`, plus line geometry metrics. Example aggregate:

- `places` / `populated_place` / `River`: 311 rows
- `places` / `populated_place` / `Lake Centerline`: 178 rows
- `places` / `admin_1_capital` / `River`: 124 rows

Do not expose or depend on `natural_earth_silver.feature_class`, zoom/rank fields, metrics, simplified geometries, or tile ids until the ETL is repaired and backfilled.

Recommended use by category:

- `places`: use `name`, `subcategory`, `geom`, and `centroid`. Good for populated places, capitals, ports, airports, scientific stations, and urban polygons. Add search/ranking only after `population`, `country_code`, `country_name`, `wikidata_id`, and `search_name` are correctly populated.
- `transport`: use `name`, `subcategory`, and `geom`. Main subcategories are `road`, `railroad`, and `ferry`. Length and zoom fields are currently empty, so derive length on demand or backfill before using for route importance.
- `hydrography`: use `name`, `subcategory`, and geometry type. Main subcategories include `river`, `coastline`, `lake`, `lake_centerline`, `reefs`, and `reservoir`. Do not use the partial `feature_class` enrichment yet.
- `terrain`: use `subcategory` and `geom` for map layers such as bathymetry, land, minor islands/coastlines, and ice shelves. Enrichment fields are currently empty.
- `regions`: use `name`, `subcategory`, and `geom` for named geographic areas such as islands, seas, deserts, gulfs, bays, and mountain ranges. Enrichment fields are currently empty.
- `landmarks`: use `name`, `subcategory`, `geom`, and `centroid` for point landmarks such as mountains, capes, islands, spot elevations, waterfalls, poles, passes, and plains. Enrichment fields are currently empty.

## Integration recommendation

Short term:

- Add optional property passthrough for reliable `gadm_silver` columns in administrative responses.
- Keep `natural_earth_silver` responses limited to base fields plus geometry until enrichment is fixed.
- Use `label_min_zoom`, `min_zoom`, and `display_rank` first for `administrative`, because those columns are complete and internally consistent.

Medium term:

- Repair/backfill `natural_earth_silver` enrichment.
- Add indexes around high-use filters: `category`, `subcategory`, `country_code`, `admin_level`, `min_zoom`, and spatial indexes for `geom` plus simplified geometries if they are queried directly.
- Change map queries to choose `geom_z4`, `geom_z7`, `geom_z10`, or `geom` based on current zoom.
- Return `centroid` for polygon features to support labels and clustering without frontend geometry processing.

