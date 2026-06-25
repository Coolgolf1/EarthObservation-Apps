"""STAC catalog query utilities — shared by datasets and fire routers."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import planetary_computer
import pystac_client

from .config import CATALOG_URL

# ─── Item cache (avoids re-querying same collection/bbox/date range) ──
_stac_cache: dict[str, list] = {}
_STAC_CACHE_MAX = 100


def _cache_key(
    collection: str, bbox: list[float], dt_start: str, dt_end: str
) -> str:
    bk = ",".join(f"{v:.2f}" for v in bbox)
    return f"{collection}|{bk}|{dt_start}|{dt_end}"


def _cache_put(key: str, items: list) -> None:
    _stac_cache[key] = items
    if len(_stac_cache) > _STAC_CACHE_MAX:
        oldest = next(iter(_stac_cache))
        del _stac_cache[oldest]


def _search_items(search: Any) -> list:
    """Extract items from a STAC search result (handles API variations)."""
    if hasattr(search, "item_collection"):
        return list(search.item_collection())
    if hasattr(search, "get_all_items"):
        return list(search.get_all_items())
    if hasattr(search, "items"):
        return list(search.items())
    return []


# ─── Public API ──────────────────────────────────────────────


def open_catalog() -> pystac_client.Client:
    """Open a signed Planetary Computer STAC catalog."""
    return pystac_client.Client.open(
        CATALOG_URL, modifier=planetary_computer.sign_inplace
    )


def query_items_bbox(
    catalog: pystac_client.Client,
    collection: str,
    bbox: list[float],
    dt: date,
    max_items: int = 10,
) -> list:
    """Query STAC items intersecting *bbox* for a single date."""
    key = _cache_key(collection, bbox, dt.isoformat(), dt.isoformat())
    if key in _stac_cache:
        return _stac_cache[key]

    search = catalog.search(
        collections=[collection],
        bbox=bbox,
        datetime=dt.isoformat(),
        limit=max_items,
    )
    items = _search_items(search)
    _cache_put(key, items)
    return items


def query_items_bbox_daterange(
    catalog: pystac_client.Client,
    collection: str,
    bbox: list[float],
    dt_start: date,
    dt_end: date,
    max_items: int = 200,
) -> list:
    """Query STAC items for an entire date range in a single request."""
    key = _cache_key(collection, bbox, dt_start.isoformat(), dt_end.isoformat())
    if key in _stac_cache:
        return _stac_cache[key]

    dt_str = f"{dt_start.isoformat()}/{dt_end.isoformat()}"
    search = catalog.search(
        collections=[collection],
        bbox=bbox,
        datetime=dt_str,
        limit=max_items,
    )
    items = _search_items(search)
    _cache_put(key, items)
    return items


def query_items_bbox_no_date(
    catalog: pystac_client.Client,
    collection: str,
    bbox: list[float],
    max_items: int = 10,
) -> list:
    """Query STAC items intersecting *bbox* without a date constraint."""
    key = _cache_key(collection, bbox, "none", "none")
    if key in _stac_cache:
        return _stac_cache[key]

    search = catalog.search(
        collections=[collection],
        bbox=bbox,
        limit=max_items,
    )
    items = _search_items(search)
    _cache_put(key, items)
    return items


def item_acq_date(item: Any, fallback: date) -> str:
    """Extract acquisition date string from a STAC item."""
    if getattr(item, "datetime", None):
        return item.datetime.date().isoformat()
    start_dt = (item.properties or {}).get("start_datetime")
    if start_dt:
        return start_dt[:10]
    return fallback.isoformat()


def build_datetime_steps(
    start: date, end: date, max_items: int
) -> list[date]:
    """Build date steps, sampling evenly if the range exceeds *max_items*."""
    total_days = (end - start).days + 1
    if total_days <= 0:
        return [start]
    if total_days <= max_items:
        return [start + timedelta(days=i) for i in range(total_days)]
    step = total_days / max_items
    return [start + timedelta(days=int(i * step)) for i in range(max_items)]
