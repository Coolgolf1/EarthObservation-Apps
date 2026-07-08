from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
import requests
from dotenv import load_dotenv
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from shapely import wkt
from shapely.geometry import Point, box, shape


@dataclass
class ServiceConfig:
    earthmapper_features_url: str
    esri_geocode_url: str
    nominatim_url: str
    esri_api_key: str | None
    osm_ors_api_key: str | None
    osm_ors_isochrone_url: str
    categories: list[str]


def load_config() -> ServiceConfig:
    load_dotenv()
    earthmapper_api_base = os.getenv("EARTHMAPPER_API_BASE", "http://127.0.0.1:8000")
    return ServiceConfig(
        earthmapper_features_url=f"{earthmapper_api_base.rstrip('/')}/features",
        esri_geocode_url=os.getenv(
            "ESRI_GEOCODE_URL",
            "https://geocode-api.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates",
        ),
        nominatim_url=os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"),
        esri_api_key=os.getenv("ESRI_API_KEY"),
        osm_ors_api_key=os.getenv("OSM_ORS_API_KEY"),
        osm_ors_isochrone_url=os.getenv("OSM_ORS_ISOCHRONE_URL", "https://api.openrouteservice.org/v2/isochrones"),
        categories=["administrative", "hydrography", "transport", "places"],
    )


def _esri_auth_params(cfg: ServiceConfig) -> dict[str, Any]:
    return {"token": cfg.esri_api_key} if cfg.esri_api_key else {}


def _select_best_geocode_candidate(place_query: str, candidates: list[dict[str, Any]], city_ref: Point | None = None) -> dict[str, Any] | None:
    if not candidates:
        return None

    # Favor candidates whose address matches comma-separated query parts (e.g., "..., Valencia").
    query_parts = [p.strip().lower() for p in place_query.split(",") if p.strip()]
    city_part = query_parts[-1] if query_parts else ""

    def whole_word_count(text: str, token: str) -> float:
        if not token:
            return 0.0
        pattern = rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])"
        return float(len(re.findall(pattern, text)))

    def rank(c: dict[str, Any], city_ref: Point | None = None) -> tuple[float, float, float]:
        addr = str(c.get("address") or "").lower()
        matched_parts = 0.0
        for p in query_parts:
            ww = whole_word_count(addr, p)
            if ww > 0:
                matched_parts += 2.0 * ww
            elif p in addr:
                matched_parts += 0.25

        # Strongly prioritize exact city segment matches after separators.
        if city_part:
            if f", {city_part}" in addr or f": {city_part}" in addr or f" {city_part}," in addr:
                matched_parts += 5.0

        score = float(c.get("score") or 0.0)
        proximity = 0.0
        if city_ref is not None:
            loc = c.get("location") or {}
            if "x" in loc and "y" in loc:
                # Negative euclidean distance in lon/lat degrees: closer to city is better.
                dx = float(loc["x"]) - float(city_ref.x)
                dy = float(loc["y"]) - float(city_ref.y)
                proximity = -((dx * dx + dy * dy) ** 0.5)
        return (matched_parts, proximity, score)

    return max(candidates, key=lambda c: rank(c, city_ref=city_ref))


def parse_earthmapper_geometry(rec: dict[str, Any]):
    if isinstance(rec.get("geometry"), dict):
        try:
            return shape(rec["geometry"])
        except Exception:
            pass

    geom_val = rec.get("geom")
    centroid_val = rec.get("centroid")

    if isinstance(geom_val, dict):
        try:
            return shape(geom_val)
        except Exception:
            pass
    if isinstance(geom_val, str):
        try:
            return wkt.loads(geom_val)
        except Exception:
            pass

    if isinstance(centroid_val, dict):
        try:
            return shape(centroid_val)
        except Exception:
            pass
    if isinstance(centroid_val, str):
        try:
            return wkt.loads(centroid_val)
        except Exception:
            pass

    coords = rec.get("coords")
    if isinstance(coords, dict) and coords.get("x") is not None and coords.get("y") is not None:
        return Point(float(coords["x"]), float(coords["y"]))

    return None


def geometry_intersects_area(geom, area_geom) -> bool:
    if geom is None or area_geom is None:
        return False
    if geom.is_empty or area_geom.is_empty:
        return False
    return geom.intersects(area_geom)


def polygon_to_bbox(poly) -> list[float]:
    minx, miny, maxx, maxy = poly.bounds
    return [float(minx), float(miny), float(maxx), float(maxy)]


def expand_context_area(poly, padding_ratio: float = 0.6):
    minx, miny, maxx, maxy = poly.bounds
    pr = max(float(padding_ratio), 0.0)
    padx = max((maxx - minx) * pr, 0.05)
    pady = max((maxy - miny) * pr, 0.05)
    return box(minx - padx, miny - pady, maxx + padx, maxy + pady)


def geocode_center_point(cfg: ServiceConfig, place_query: str, country_hint: str | None = None) -> dict[str, Any] | None:
    def _nominatim_center() -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "q": place_query,
            "format": "jsonv2",
            "limit": 1,
        }
        if country_hint:
            params["countrycodes"] = country_hint.lower()

        headers = {"User-Agent": "arcgis-time-service/1.0"}
        try:
            resp = requests.get(cfg.nominatim_url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return None
            r = rows[0]
            lon = float(r["lon"])
            lat = float(r["lat"])
            return {
                "name": r.get("display_name") or place_query,
                "point": Point(lon, lat),
                "raw": r,
                "source": "nominatim",
            }
        except Exception:
            return None

    params: dict[str, Any] = {
        "f": "json",
        "singleLine": place_query,
        "maxLocations": 1,
        "outFields": "*",
        **_esri_auth_params(cfg),
    }
    if country_hint:
        params["countryCode"] = country_hint

    cands: list[dict[str, Any]] = []

    # First attempt: direct singleLine query.
    resp = requests.get(cfg.esri_geocode_url, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    cands.extend(payload.get("candidates", []))

    city_ref: Point | None = None

    # Second attempt: if query looks like "street, city", explicitly pass city.
    parts = [p.strip() for p in place_query.split(",") if p.strip()]
    if len(parts) >= 2:
        street = ", ".join(parts[:-1])
        city = parts[-1]
        city_params: dict[str, Any] = {
            "f": "json",
            "singleLine": street,
            "city": city,
            "maxLocations": 5,
            "outFields": "*",
            **_esri_auth_params(cfg),
        }
        if country_hint:
            city_params["countryCode"] = country_hint

        try:
            city_resp = requests.get(cfg.esri_geocode_url, params=city_params, timeout=30)
            city_resp.raise_for_status()
            city_payload = city_resp.json()
            cands.extend(city_payload.get("candidates", []))
        except Exception:
            pass

        # Build a city reference point for disambiguation.
        city_ref_params: dict[str, Any] = {
            "f": "json",
            "singleLine": city,
            "maxLocations": 1,
            "outFields": "*",
            **_esri_auth_params(cfg),
        }
        if country_hint:
            city_ref_params["countryCode"] = country_hint
        try:
            city_ref_resp = requests.get(cfg.esri_geocode_url, params=city_ref_params, timeout=30)
            city_ref_resp.raise_for_status()
            city_ref_cands = city_ref_resp.json().get("candidates", [])
            if city_ref_cands:
                loc = (city_ref_cands[0].get("location") or {})
                if "x" in loc and "y" in loc:
                    city_ref = Point(float(loc["x"]), float(loc["y"]))
        except Exception:
            pass

    if not cands:
        return None

    best = _select_best_geocode_candidate(place_query, cands, city_ref=city_ref)
    if best is None:
        return _nominatim_center()
    loc = best.get("location") or {}
    if "x" not in loc or "y" not in loc:
        return _nominatim_center()

    esri_result = {
        "name": best.get("address"),
        "point": Point(float(loc["x"]), float(loc["y"])),
        "raw": best,
        "source": "esri",
    }

    osm_result = _nominatim_center()
    if osm_result is None:
        return esri_result

    # If ESRI is far from OSM geocode, trust OSM for center-point orientation.
    dx = esri_result["point"].x - osm_result["point"].x
    dy = esri_result["point"].y - osm_result["point"].y
    if (dx * dx + dy * dy) ** 0.5 > 1.0:
        return osm_result

    return esri_result


def _fallback_time_circle(center_point: Point, minutes: int, speed_kmh: float = 35.0):
    km = speed_kmh * (minutes / 60.0)
    deg = km / 111.32
    return center_point.buffer(deg)


def fetch_osm_isochrone(cfg: ServiceConfig, center_point: Point, minutes: int = 50, profile: str = "driving-car"):
    if not cfg.osm_ors_api_key:
        return _fallback_time_circle(center_point, minutes=minutes, speed_kmh=35.0), "fallback_circle"

    headers = {"Authorization": cfg.osm_ors_api_key, "Content-Type": "application/json"}
    body = {
        "locations": [[float(center_point.x), float(center_point.y)]],
        "range": [int(minutes) * 60],
        "range_type": "time",
    }

    url = f"{cfg.osm_ors_isochrone_url.rstrip('/')}/{profile}"
    resp = requests.post(url, headers=headers, json=body, timeout=45)
    resp.raise_for_status()
    payload = resp.json()

    feats = payload.get("features", []) if isinstance(payload, dict) else []
    if not feats:
        raise ValueError("No isochrone features returned from OSM service")

    iso = shape(feats[0]["geometry"])
    return iso, "osm_ors_isochrone"


def normalize_earthmapper_features(items: list[dict[str, Any]], category: str) -> pd.DataFrame:
    rows = []
    for r in items:
        props = r.get("properties") if isinstance(r.get("properties"), dict) else {}
        geom = parse_earthmapper_geometry(r)
        rows.append(
            {
                "id": str(r.get("id")) if r.get("id") is not None else None,
                "name": r.get("name") or props.get("name") or props.get("NAME") or props.get("Name"),
                "category": r.get("category") or category,
                "subcategory": r.get("subcategory") or props.get("subcategory"),
                "geometry": geom,
                "properties": props if props else r,
                "source": "earthmapper",
            }
        )
    return pd.DataFrame(rows)


def fetch_earthmapper_in_area(cfg: ServiceConfig, area_poly, category: str, limit: int = 700) -> pd.DataFrame:
    bbox = polygon_to_bbox(area_poly)
    params = {
        "bbox": ",".join(str(v) for v in bbox),
        "category": category,
        "limit": int(limit),
    }
    resp = requests.get(cfg.earthmapper_features_url, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    items = data if isinstance(data, list) else data.get("features", data.get("results", []))

    filtered = []
    for item in items:
        geom = parse_earthmapper_geometry(item)
        if geometry_intersects_area(geom, area_poly):
            filtered.append(item)

    df = normalize_earthmapper_features(filtered, category=category)
    if not df.empty:
        df["centroid"] = df["geometry"].apply(lambda g: g.centroid if g is not None and not g.is_empty else None)
    return df


ESRI_SEED_TERMS = {
    "administrative": ["city hall", "municipality", "province"],
    "hydrography": ["river", "lake", "reservoir"],
    "transport": ["airport", "train station", "bus station", "highway"],
    "places": ["hospital", "school", "museum", "park"],
}


def fetch_esri_in_area(cfg: ServiceConfig, area_poly, category: str, max_per_seed: int = 4) -> pd.DataFrame:
    min_lon, min_lat, max_lon, max_lat = area_poly.bounds
    center_lon = (min_lon + max_lon) / 2.0
    center_lat = (min_lat + max_lat) / 2.0

    rows = []
    seen = set()

    for seed in ESRI_SEED_TERMS.get(category, [category]):
        params = {
            "f": "json",
            "singleLine": seed,
            "location": f"{center_lon},{center_lat}",
            "searchExtent": f"{min_lon},{min_lat},{max_lon},{max_lat}",
            "maxLocations": int(max_per_seed),
            "outFields": "*",
            **_esri_auth_params(cfg),
        }

        try:
            resp = requests.get(cfg.esri_geocode_url, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            continue

        for c in payload.get("candidates", []):
            loc = c.get("location") or {}
            if "x" not in loc or "y" not in loc:
                continue
            geom = Point(float(loc["x"]), float(loc["y"]))
            if not geometry_intersects_area(geom, area_poly):
                continue

            key = (c.get("address"), round(geom.x, 6), round(geom.y, 6))
            if key in seen:
                continue
            seen.add(key)

            attrs = c.get("attributes") or {}
            rows.append(
                {
                    "id": str(attrs.get("ResultID")) if attrs.get("ResultID") is not None else str(c.get("magicKey")) if c.get("magicKey") else None,
                    "name": c.get("address"),
                    "category": category,
                    "subcategory": attrs.get("Type") or attrs.get("Addr_type"),
                    "geometry": geom,
                    "centroid": geom,
                    "properties": {**attrs, **{k: v for k, v in c.items() if k != "attributes"}},
                    "source": "esri",
                }
            )

    return pd.DataFrame(rows)


def endpoint_time_features(
    place_query: str,
    minutes: int,
    profile: str = "driving-car",
    country_hint: str | None = None,
    categories: list[str] | None = None,
    context_fetch_padding_ratio: float = 0.65,
) -> dict[str, Any]:
    """
    API-like function: returns travel-time area + data payload.
    """
    cfg = load_config()
    cats = categories if categories is not None else cfg.categories

    center_info = geocode_center_point(cfg, place_query, country_hint=country_hint)
    if center_info is None:
        raise RuntimeError(f"Could not geocode center: {place_query}")

    center_point = center_info["point"]
    iso_poly, iso_source = fetch_osm_isochrone(cfg, center_point, minutes=minutes, profile=profile)
    context_area_poly = expand_context_area(iso_poly, padding_ratio=context_fetch_padding_ratio)

    artifacts: dict[str, dict[str, pd.DataFrame]] = {}
    summary_rows = []

    for cat in cats:
        esri_df = fetch_esri_in_area(cfg, context_area_poly, category=cat, max_per_seed=4)
        earth_df = fetch_earthmapper_in_area(cfg, context_area_poly, category=cat, limit=700)
        artifacts[cat] = {"esri": esri_df, "earthmapper": earth_df}

        summary_rows.append(
            {
                "category": cat,
                "esri_count": int(esri_df.shape[0]),
                "earthmapper_count": int(earth_df.shape[0]),
            }
        )

    df_summary = pd.DataFrame(summary_rows)

    return {
        "place_query": place_query,
        "minutes": int(minutes),
        "profile": profile,
        "iso_source": iso_source,
        "center_name": center_info["name"],
        "center_point": center_point,
        "iso_poly": iso_poly,
        "context_area_poly": context_area_poly,
        "bbox": polygon_to_bbox(iso_poly),
        "context_bbox": polygon_to_bbox(context_area_poly),
        "summary": df_summary,
        "artifacts": artifacts,
    }


def _build_plot_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    iso = payload["iso_poly"]
    context_area = payload.get("context_area_poly", iso)
    plot_rows = [
        {"source": "context", "category": "context_window", "geometry": context_area},
        {"source": "context", "category": "isochrone", "geometry": iso},
        {"source": "context", "category": "center", "geometry": payload["center_point"]},
    ]

    for cat, data in payload["artifacts"].items():
        earth_df = data.get("earthmapper", pd.DataFrame())
        for _, r in earth_df.iterrows():
            g = r.get("geometry")
            if g is not None and not g.is_empty:
                # Clip large EarthMapper geometries to the broader context window.
                try:
                    clipped = g.intersection(context_area)
                except Exception:
                    clipped = g
                if clipped is None or clipped.is_empty:
                    continue
                plot_rows.append(
                    {
                        "source": "earthmapper",
                        "category": cat,
                        "name": r.get("name"),
                        "subcategory": r.get("subcategory"),
                        "properties": r.get("properties"),
                        "geometry": clipped,
                    }
                )

        for _, r in data.get("esri", pd.DataFrame()).iterrows():
            g = r.get("geometry")
            if g is not None and not g.is_empty:
                plot_rows.append(
                    {
                        "source": "esri",
                        "category": cat,
                        "name": r.get("name"),
                        "geometry": g,
                    }
                )

    return plot_rows


def _enforce_north_up(ax) -> None:
    """Ensure map orientation stays north-up and east-right."""
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()

    if x0 > x1:
        ax.set_xlim(x1, x0)
    if y0 > y1:
        ax.set_ylim(y1, y0)

    ax.set_aspect("equal", adjustable="box")


def endpoint_render_map(payload: dict[str, Any], title: str | None = None, context_padding_ratio: float = 0.4):
    """
    API-like function: renders map for a payload from endpoint_time_features.
    Returns a matplotlib figure.
    """
    plot_rows = _build_plot_rows(payload)
    if not plot_rows:
        raise RuntimeError("No geometries available to render")

    gdf_plot = gpd.GeoDataFrame(plot_rows, geometry="geometry", crs="EPSG:4326")
    fig, ax = plt.subplots(figsize=(12, 10))

    ctx_iso = gdf_plot[(gdf_plot["source"] == "context") & (gdf_plot["category"] == "isochrone")]
    ctx_window = gdf_plot[(gdf_plot["source"] == "context") & (gdf_plot["category"] == "context_window")]
    if not ctx_window.empty:
        ctx_window.boundary.plot(ax=ax, color="#9ca3af", linewidth=1.0, linestyle="--", zorder=0)

    em = gdf_plot[gdf_plot["source"] == "earthmapper"]
    if not em.empty:
        em_hydro = em[em["category"] == "hydrography"]
        if not em_hydro.empty:
            hydro_polys = em_hydro[em_hydro.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
            hydro_lines = em_hydro[em_hydro.geometry.geom_type.isin(["LineString", "MultiLineString"])]
            hydro_points = em_hydro[em_hydro.geometry.geom_type.isin(["Point", "MultiPoint"])]

            # 2) Ocean/lakes drawn above administrative polygons.
            if not hydro_polys.empty:
                hydro_polys.plot(ax=ax, color="#4aa3df", alpha=1.0, linewidth=0.0, zorder=2)

            # Keep only river-like hydro lines; suppress ocean/coast outlines.
            if not hydro_lines.empty:
                river_tokens = (
                    "river",
                    "rio",
                    "stream",
                    "canal",
                    "tributary",
                    "wadi",
                )
                ocean_tokens = (
                    "ocean",
                    "sea",
                    "coast",
                    "coastline",
                    "shore",
                    "bay",
                    "gulf",
                )

                def _hydro_text(row: pd.Series) -> str:
                    parts = [
                        str(row.get("name") or ""),
                        str(row.get("subcategory") or ""),
                        str(row.get("properties") or ""),
                    ]
                    return " ".join(parts).lower()

                hydro_text = hydro_lines.apply(_hydro_text, axis=1)
                river_mask = hydro_text.str.contains("|".join(river_tokens), regex=True)
                ocean_mask = hydro_text.str.contains("|".join(ocean_tokens), regex=True)
                filtered_hydro_lines = hydro_lines[river_mask & (~ocean_mask)]

                # Fallback: if no explicit river metadata is present, keep original lines except clear ocean/coast hints.
                if filtered_hydro_lines.empty:
                    filtered_hydro_lines = hydro_lines[~ocean_mask]

        em_admin = em[em["category"] == "administrative"]
        if not em_admin.empty:
            # 1) Administrative polygons first, below ocean/lakes.
            admin_polys = em_admin[em_admin.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
            admin_lines = em_admin[em_admin.geometry.geom_type.isin(["LineString", "MultiLineString"])]
            admin_points = em_admin[em_admin.geometry.geom_type.isin(["Point", "MultiPoint"])]
            if not admin_polys.empty:
                admin_polys.plot(ax=ax, facecolor="white", edgecolor="none", linewidth=0.0, alpha=1.0, zorder=1)
                admin_polys.boundary.plot(ax=ax, color="#2ca25f", linewidth=0.5, alpha=1.0, zorder=1.25)
            if not admin_lines.empty:
                admin_lines.plot(ax=ax, color="#2ca25f", linewidth=0.5, alpha=1.0, zorder=1.2)
            if not admin_points.empty:
                admin_points.plot(ax=ax, color="#2ca25f", markersize=10, alpha=1.0, zorder=1.3)

        em_transport = em[em["category"] == "transport"]
        if not em_transport.empty:
            em_transport.plot(ax=ax, color="#6b7280", linewidth=0.7, alpha=1.0, zorder=2.6)

        em_places = em[em["category"] == "places"]
        if not em_places.empty:
            place_polys = em_places[em_places.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
            place_lines = em_places[em_places.geometry.geom_type.isin(["LineString", "MultiLineString"])]
            place_points = em_places[em_places.geometry.geom_type.isin(["Point", "MultiPoint"])]
            if not place_polys.empty:
                # 3) Places at 30% opacity.
                place_polys.plot(ax=ax, color="#f59e0b", alpha=0.3, linewidth=0.0, zorder=3)
            if not place_lines.empty:
                place_lines.plot(ax=ax, color="#f59e0b", alpha=0.3, linewidth=0.7, zorder=3.1)
            if not place_points.empty:
                place_points.plot(ax=ax, color="#f59e0b", markersize=9, alpha=0.3, zorder=3.2)

        # 4) Rivers above places.
        if not em_hydro.empty:
            if 'filtered_hydro_lines' in locals() and not filtered_hydro_lines.empty:
                filtered_hydro_lines.plot(ax=ax, color="#1f77b4", linewidth=1.6, alpha=1.0, zorder=4)
            if not hydro_points.empty:
                hydro_points.plot(ax=ax, color="#1f77b4", markersize=7, alpha=1.0, zorder=4.1)

    esri = gdf_plot[gdf_plot["source"] == "esri"]
    if not esri.empty:
        esri.plot(ax=ax, color="#d62728", markersize=18, marker="x", linewidth=1.0, alpha=1.0, zorder=7)

    ctx_center = gdf_plot[(gdf_plot["source"] == "context") & (gdf_plot["category"] == "center")]
    if not ctx_center.empty:
        ctx_center.plot(ax=ax, color="#111827", markersize=80, marker="*", zorder=8)

    # 5) Reach area on top.
    if not ctx_iso.empty:
        ctx_iso.boundary.plot(ax=ax, color="black", linewidth=2.2, zorder=10)

    minx, miny, maxx, maxy = payload["iso_poly"].bounds
    pad_ratio = max(float(context_padding_ratio), 0.0)
    padx = max((maxx - minx) * pad_ratio, 0.05)
    pady = max((maxy - miny) * pad_ratio, 0.05)
    ax.set_xlim(minx - padx, maxx + padx)
    ax.set_ylim(miny - pady, maxy + pady)
    _enforce_north_up(ax)

    legend_handles = [
        Patch(facecolor="none", edgecolor="black", label="Travel-time reach area"),
        Patch(facecolor="#4aa3df", edgecolor="#4aa3df", label="Water bodies (ocean/lakes)"),
        Line2D([0], [0], color="#1f77b4", lw=2.0, label="Rivers"),
        Line2D([0], [0], color="#6b7280", lw=1.6, alpha=0.6, label="EarthMapper transport"),
        Patch(facecolor="white", edgecolor="#2ca25f", label="Administrative"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#f59e0b", markersize=6, alpha=0.6, label="EarthMapper places"),
        Line2D([0], [0], marker="x", color="#d62728", markersize=8, lw=0, label="ESRI candidates"),
        Line2D([0], [0], marker="*", color="#111827", markersize=10, lw=0, label="Center point"),
    ]
    ax.legend(
        handles=legend_handles,
        loc="center left",
        bbox_to_anchor=(1.01, 0.5),
        frameon=True,
        framealpha=0.95,
        title="Layers",
        borderpad=0.8,
        labelspacing=0.6,
    )

    map_title = title or f"{payload['place_query']} - {payload['minutes']}-minute reach"
    ax.set_title(map_title)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(alpha=0.2, linestyle="--", linewidth=0.5)
    fig.subplots_adjust(right=0.74)
    return fig


def endpoint_esri_feature_counts(payload: dict[str, Any]) -> dict[str, int]:
    """
    API-like function: returns ESRI feature counts by category.
    """
    out: dict[str, int] = {}
    for cat, data in payload["artifacts"].items():
        out[cat] = int(data.get("esri", pd.DataFrame()).shape[0])
    return out


def run_example() -> None:
    payload = endpoint_time_features(
        place_query="Plaza de Catalunya, Barcelona",
        minutes=45,
        profile="driving-car",
        country_hint="ESP",
    )

    print("Center:", payload["center_name"])
    print("Iso source:", payload["iso_source"])
    print("BBox:", payload["bbox"])
    print("ESRI counts:", endpoint_esri_feature_counts(payload))
    print("\nSummary table:")
    print(payload["summary"].to_string(index=False))

    fig = endpoint_render_map(payload, title="Barcelona 45-minute area")
    plt.show()


if __name__ == "__main__":
    run_example()
