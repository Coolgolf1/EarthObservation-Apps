import os
from typing import Any

import requests
import pandas as pd
from shapely import wkt
from shapely.geometry import Point, shape
from dotenv import load_dotenv


load_dotenv()

EARTHMAPPER_API_BASE = os.getenv("EARTHMAPPER_API_BASE", "http://127.0.0.1:8000")
EARTHMAPPER_FEATURES_URL = f"{EARTHMAPPER_API_BASE.rstrip('/')}/features"
ESRI_GEOCODE_URL = os.getenv(
    "ESRI_GEOCODE_URL",
    "https://geocode-api.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates",
)
ESRI_API_KEY = os.getenv("ESRI_API_KEY")
OSM_ORS_API_KEY = os.getenv("OSM_ORS_API_KEY")
OSM_ORS_ISOCHRONE_URL = os.getenv("OSM_ORS_ISOCHRONE_URL", "https://api.openrouteservice.org/v2/isochrones")


def esri_auth_params() -> dict[str, Any]:
    return {"token": ESRI_API_KEY} if ESRI_API_KEY else {}


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


def geocode_center_point(address: str, country_hint: str = "ESP") -> dict[str, Any] | None:
    params = {
        "f": "json",
        "singleLine": address,
        "countryCode": country_hint,
        "maxLocations": 1,
        "outFields": "*",
        **esri_auth_params(),
    }
    resp = requests.get(ESRI_GEOCODE_URL, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    cands = payload.get("candidates", [])
    if not cands:
        return None
    best = cands[0]
    loc = best.get("location") or {}
    if "x" not in loc or "y" not in loc:
        return None
    return {
        "name": best.get("address"),
        "point": Point(float(loc["x"]), float(loc["y"])),
        "raw": best,
    }


def fetch_osm_isochrone(center_point: Point, minutes: int = 50, profile: str = "driving-car"):
    if not OSM_ORS_API_KEY:
        raise RuntimeError("Set OSM_ORS_API_KEY in .env to run this script")

    headers = {"Authorization": OSM_ORS_API_KEY, "Content-Type": "application/json"}
    body = {
        "locations": [[float(center_point.x), float(center_point.y)]],
        "range": [int(minutes) * 60],
        "range_type": "time",
    }

    url = f"{OSM_ORS_ISOCHRONE_URL.rstrip('/')}/{profile}"
    resp = requests.post(url, headers=headers, json=body, timeout=45)
    resp.raise_for_status()
    payload = resp.json()
    feats = payload.get("features", []) if isinstance(payload, dict) else []
    if not feats:
        raise ValueError("No isochrone features returned")
    return shape(feats[0]["geometry"])


def fetch_earthmapper_in_area(area_poly, category: str, limit: int = 600) -> pd.DataFrame:
    bbox = polygon_to_bbox(area_poly)
    params = {"bbox": ",".join(str(v) for v in bbox), "category": category, "limit": int(limit)}
    resp = requests.get(EARTHMAPPER_FEATURES_URL, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    items = data if isinstance(data, list) else data.get("features", data.get("results", []))

    rows = []
    for item in items:
        geom = parse_earthmapper_geometry(item)
        if geometry_intersects_area(geom, area_poly):
            rows.append(item)
    return pd.DataFrame(rows)


def fetch_esri_in_area(area_poly, query_text: str, max_locations: int = 25) -> pd.DataFrame:
    min_lon, min_lat, max_lon, max_lat = area_poly.bounds
    center_lon = (min_lon + max_lon) / 2.0
    center_lat = (min_lat + max_lat) / 2.0

    params = {
        "f": "json",
        "singleLine": query_text,
        "location": f"{center_lon},{center_lat}",
        "searchExtent": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "maxLocations": int(max_locations),
        "outFields": "*",
        **esri_auth_params(),
    }
    resp = requests.get(ESRI_GEOCODE_URL, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for c in payload.get("candidates", []):
        loc = c.get("location") or {}
        if "x" not in loc or "y" not in loc:
            continue
        geom = Point(float(loc["x"]), float(loc["y"]))
        if geometry_intersects_area(geom, area_poly):
            rows.append(c)
    return pd.DataFrame(rows)


def endpoint_time_features(place_query: str, minutes: int) -> dict[str, Any]:
    categories = ["hydrography", "transport", "places", "administrative"]
    center = geocode_center_point(place_query, country_hint="ESP")
    if center is None:
        raise RuntimeError(f"Could not geocode: {place_query}")

    iso_poly = fetch_osm_isochrone(center["point"], minutes=minutes, profile="driving-car")

    summary_rows = []
    for cat in categories:
        esri_df = fetch_esri_in_area(iso_poly, query_text=cat, max_locations=25)
        earth_df = fetch_earthmapper_in_area(iso_poly, category=cat, limit=700)
        summary_rows.append(
            {
                "category": cat,
                "esri_count": int(esri_df.shape[0]),
                "earthmapper_count": int(earth_df.shape[0]),
                "total_count": int(esri_df.shape[0] + earth_df.shape[0]),
            }
        )

    summary_df = pd.DataFrame(summary_rows)
    return {
        "place": place_query,
        "minutes": minutes,
        "esri_total_features": int(summary_df["esri_count"].sum()),
        "summary": summary_df,
    }


if __name__ == "__main__":
    result = endpoint_time_features(place_query="Plaza de Espana, Sevilla", minutes=40)
    print("Request:", result["place"], "| minutes:", result["minutes"])
    print("Total ESRI features:", result["esri_total_features"])
    print(result["summary"].sort_values("total_count", ascending=False).to_string(index=False))
