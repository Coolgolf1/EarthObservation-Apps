"""Unit tests for the pydantic models in app.models."""
from __future__ import annotations

import pytest
from app.models.bbox import BBox
from app.models.features import Feature, FeatureCollection, Geometry
from pydantic import ValidationError


class TestBBox:
    def test_valid_bbox_as_tuple(self):
        b = BBox(min_lon=-10, min_lat=-5, max_lon=10, max_lat=5)
        assert b.as_tuple() == (-10, -5, 10, 5)

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(min_lon=10, min_lat=0, max_lon=10, max_lat=5),  # lon equal
            dict(min_lon=20, min_lat=0, max_lon=5, max_lat=5),   # lon inverted
            dict(min_lon=0, min_lat=5, max_lon=5, max_lat=5),    # lat equal
            dict(min_lon=0, min_lat=9, max_lon=5, max_lat=5),    # lat inverted
        ],
    )
    def test_invalid_bounds_raise(self, kwargs):
        with pytest.raises(ValidationError):
            BBox(**kwargs)


class TestGeometry:
    def test_point(self):
        g = Geometry(type="Point", coordinates=[1.0, 2.0])
        assert g.type == "Point" and g.coordinates == [1.0, 2.0]

    def test_nested_polygon_coordinates(self):
        g = Geometry(type="Polygon", coordinates=[[[0, 0], [1, 0], [1, 1], [0, 0]]])
        assert g.coordinates[0][0] == [0, 0]


class TestFeature:
    def test_defaults(self):
        f = Feature(geometry=Geometry(type="Point", coordinates=[0, 0]))
        assert f.type == "Feature"
        assert f.properties == {}

    def test_scalar_properties_preserved(self):
        f = Feature(
            geometry=Geometry(type="Point", coordinates=[0, 0]),
            properties={"name": "x", "count": 3, "ok": True, "score": 1.5, "none": None},
        )
        assert f.properties["count"] == 3 and f.properties["ok"] is True

    def test_feature_collection_default(self):
        fc = FeatureCollection()
        assert fc.type == "FeatureCollection" and fc.features == []
