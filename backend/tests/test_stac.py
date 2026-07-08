"""Unit tests for the pure helpers in app.core.stac (no network)."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from app.core import stac


@pytest.fixture(autouse=True)
def _clear_cache():
    stac._stac_cache.clear()
    yield
    stac._stac_cache.clear()


def test_cache_key_rounds_bbox_to_2dp():
    key = stac._cache_key("modis", [1.234, 2.567, 3.0, 4.0], "2024-01-01", "2024-01-02")
    assert key == "modis|1.23,2.57,3.00,4.00|2024-01-01|2024-01-02"


def test_cache_put_evicts_oldest_over_capacity():
    for i in range(stac._STAC_CACHE_MAX + 3):
        stac._cache_put(f"k{i}", [i])
    assert len(stac._stac_cache) == stac._STAC_CACHE_MAX
    assert "k0" not in stac._stac_cache          # oldest evicted
    assert f"k{stac._STAC_CACHE_MAX + 2}" in stac._stac_cache  # newest kept


class _FakeSearch:
    def __init__(self, items):
        self._items = items

    def item_collection(self):
        return self._items


def test_search_items_uses_item_collection():
    assert stac._search_items(_FakeSearch([1, 2, 3])) == [1, 2, 3]


def test_search_items_unknown_object_returns_empty():
    assert stac._search_items(object()) == []


class _FakeItem:
    def __init__(self, dt=None, props=None):
        self.datetime = dt
        self.properties = props or {}


def test_item_acq_date_from_datetime():
    item = _FakeItem(dt=datetime(2024, 5, 26, 12, 0, 0))
    assert stac.item_acq_date(item, date(2000, 1, 1)) == "2024-05-26"


def test_item_acq_date_from_start_datetime_property():
    item = _FakeItem(dt=None, props={"start_datetime": "2024-05-26T00:00:00Z"})
    assert stac.item_acq_date(item, date(2000, 1, 1)) == "2024-05-26"


def test_item_acq_date_falls_back():
    item = _FakeItem(dt=None, props={})
    assert stac.item_acq_date(item, date(2000, 1, 1)) == "2000-01-01"


class TestBuildDatetimeSteps:
    def test_every_day_when_range_fits(self):
        steps = stac.build_datetime_steps(date(2024, 1, 1), date(2024, 1, 5), max_items=10)
        assert steps == [date(2024, 1, d) for d in range(1, 6)]

    def test_samples_when_range_exceeds_max(self):
        steps = stac.build_datetime_steps(date(2024, 1, 1), date(2024, 12, 31), max_items=12)
        assert len(steps) == 12
        assert steps[0] == date(2024, 1, 1)
        assert steps == sorted(steps)

    def test_nonpositive_range_returns_start(self):
        assert stac.build_datetime_steps(date(2024, 1, 5), date(2024, 1, 1), max_items=10) == [date(2024, 1, 5)]
