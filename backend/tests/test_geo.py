"""Unit tests for app.core.geo — pure geometry helpers, no network/DB."""
from __future__ import annotations

import pytest
from app.core.geo import (
    auto_h3_resolution,
    bbox_from_geometry,
    circle_polygon,
    validate_bbox,
)
from fastapi import HTTPException


class TestValidateBbox:
    def test_valid_bbox_returns_unpacked_floats(self):
        assert validate_bbox([-10, -5, 10, 5]) == (-10.0, -5.0, 10.0, 5.0)

    def test_coerces_ints_to_floats(self):
        west, south, east, north = validate_bbox([0, 0, 1, 1])
        assert isinstance(west, float) and isinstance(north, float)

    @pytest.mark.parametrize(
        "bbox",
        [
            [10, 0, 10, 5],   # west == east
            [10, 0, 5, 5],    # west > east
            [0, 5, 5, 5],     # south == north
            [0, 10, 5, 5],    # south > north
        ],
    )
    def test_degenerate_or_inverted_bbox_raises(self, bbox):
        with pytest.raises(HTTPException) as exc:
            validate_bbox(bbox)
        assert exc.value.status_code == 400

    @pytest.mark.parametrize(
        "bbox",
        [
            [-181, 0, 10, 5],
            [0, 0, 181, 5],
            [0, -91, 5, 5],
            [0, 0, 5, 91],
        ],
    )
    def test_out_of_wgs84_range_raises(self, bbox):
        with pytest.raises(HTTPException) as exc:
            validate_bbox(bbox)
        assert exc.value.status_code == 400


class TestAutoH3Resolution:
    @pytest.mark.parametrize(
        "span,expected",
        [
            (0.01, 10),
            (0.05, 9),
            (0.2, 8),
            (0.5, 7),
            (2.0, 6),
            (5.0, 5),
            (15.0, 4),
            (40.0, 3),
            (100.0, 2),
            (200.0, 1),
        ],
    )
    def test_resolution_by_span(self, span, expected):
        # square bbox centered on origin with the given span
        half = span / 2
        assert auto_h3_resolution([-half, -half, half, half]) == expected

    def test_uses_larger_dimension(self):
        # 0.5 deg wide, 0.01 deg tall -> span 0.5 -> res 7
        assert auto_h3_resolution([0, 0, 0.5, 0.01]) == 7


class TestCirclePolygon:
    def test_returns_closed_polygon(self):
        poly = circle_polygon(10.0, 45.0, 5.0, segments=24)
        assert poly["type"] == "Polygon"
        ring = poly["coordinates"][0]
        assert len(ring) == 25  # segments + closing point
        assert ring[0] == ring[-1]

    def test_radius_grows_the_ring(self):
        small = circle_polygon(0, 0, 1.0)["coordinates"][0]
        big = circle_polygon(0, 0, 50.0)["coordinates"][0]
        # max longitude offset scales with radius
        assert max(p[0] for p in big) > max(p[0] for p in small)


class TestBboxFromGeometry:
    def test_point(self):
        assert bbox_from_geometry({"type": "Point", "coordinates": [3.0, 4.0]}) == [3.0, 4.0, 3.0, 4.0]

    def test_polygon(self):
        geom = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 3], [0, 3], [0, 0]]]}
        assert bbox_from_geometry(geom) == [0.0, 0.0, 2.0, 3.0]

    def test_multipolygon(self):
        geom = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0, 0], [1, 1], [0, 1], [0, 0]]],
                [[[5, 5], [6, 6], [5, 6], [5, 5]]],
            ],
        }
        assert bbox_from_geometry(geom) == [0.0, 0.0, 6.0, 6.0]

    def test_missing_coordinates_returns_none(self):
        assert bbox_from_geometry({"type": "Polygon"}) is None

    def test_empty_coordinates_returns_none(self):
        assert bbox_from_geometry({"type": "Polygon", "coordinates": []}) is None
