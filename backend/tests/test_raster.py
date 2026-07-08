"""Unit tests for windowing/read-shape logic in app.core.raster.

Uses a small in-memory GeoTIFF so no network or real data is needed. These
guard the M6 fix (window_for_bbox must cover the full requested area, not a
shrunken corner) and target_read_shape downsampling.
"""
from __future__ import annotations

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
from app.core.raster import target_read_shape, window_for_bbox  # noqa: E402
from rasterio.io import MemoryFile  # noqa: E402
from rasterio.transform import from_bounds  # noqa: E402
from rasterio.windows import bounds as window_bounds  # noqa: E402


@pytest.fixture
def ds():
    """20°×20° raster (-10..10 lon/lat), 200×200 px → 0.1°/px, EPSG:4326."""
    transform = from_bounds(-10, -10, 10, 10, 200, 200)
    data = np.zeros((1, 200, 200), dtype="uint8")
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff", height=200, width=200, count=1,
            dtype="uint8", crs="EPSG:4326", transform=transform,
        ) as dst:
            dst.write(data)
        with mem.open() as opened:
            yield opened


def test_window_covers_full_requested_bbox(ds):
    # M6 guard: the window must cover the central 10°×10° box, not shrink to a corner.
    win = window_for_bbox(ds, [-5, -5, 5, 5])
    assert win is not None
    west, south, east, north = window_bounds(win, ds.transform)
    assert west == pytest.approx(-5, abs=0.11)
    assert south == pytest.approx(-5, abs=0.11)
    assert east == pytest.approx(5, abs=0.11)
    assert north == pytest.approx(5, abs=0.11)


def test_window_none_when_bbox_outside_raster(ds):
    assert window_for_bbox(ds, [100, 100, 110, 110]) is None


def test_target_read_shape_downsamples_for_coarse_target(ds):
    win = window_for_bbox(ds, [-10, -10, 10, 10])  # full 200×200
    # h3_res 4 ≈ 0.5° target vs 0.1°/px → downsample expected
    shape = target_read_shape(win, ds, 4)
    assert shape is not None
    out_h, out_w = shape
    assert out_h < win.height and out_w < win.width


def test_target_read_shape_none_when_native_fine_enough(ds):
    win = window_for_bbox(ds, [-10, -10, 10, 10])
    # h3_res 8 ≈ 0.01° target, finer than the 0.1°/px raster → no downsample
    assert target_read_shape(win, ds, 8) is None
