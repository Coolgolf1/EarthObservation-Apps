"""Unit tests for the DATASET_CONFIG filter/classify lambdas in app.core.config."""
from __future__ import annotations

import numpy as np
import pytest
from app.core.config import DATASET_CONFIG


def classify(key, val):
    return DATASET_CONFIG[key]["classify"](val)


def filter_mask(key, arr):
    return DATASET_CONFIG[key]["filter"](np.array(arr)).tolist()


class TestFire:
    @pytest.mark.parametrize("val,expected", [(10, "high"), (9, "high"), (8, "nominal"), (7, "low"), (0, "low")])
    def test_classify(self, val, expected):
        assert classify("fire", val) == expected

    def test_filter_keeps_7_to_9(self):
        assert filter_mask("fire", [5, 6, 7, 8, 9, 10]) == [False, False, True, True, True, False]


class TestFlood:
    @pytest.mark.parametrize("val,expected", [(80, "permanent"), (79, "seasonal"), (30, "seasonal"), (29, "ephemeral"), (1, "ephemeral")])
    def test_classify(self, val, expected):
        assert classify("flood", val) == expected

    def test_filter_positive(self):
        assert filter_mask("flood", [0, 1, 50]) == [False, True, True]


class TestVegetation:
    @pytest.mark.parametrize("val,expected", [(6001, "dense"), (3001, "moderate"), (1001, "sparse"), (1000, "bare"), (0, "bare")])
    def test_classify(self, val, expected):
        assert classify("vegetation", val) == expected

    def test_scale(self):
        assert DATASET_CONFIG["vegetation"]["scale"] == 10000.0


class TestWeather:
    @pytest.mark.parametrize("val,expected", [(16001, "extreme"), (15001, "hot"), (14001, "warm"), (14000, "cool")])
    def test_classify(self, val, expected):
        assert classify("weather", val) == expected


class TestLanduse:
    @pytest.mark.parametrize(
        "val,expected",
        [(10, "tree"), (20, "shrub"), (30, "grass"), (40, "crop"), (50, "built"), (60, "bare"), (80, "water"), (70, "other"), (0, "other")],
    )
    def test_classify(self, val, expected):
        assert classify("landuse", val) == expected


def test_all_datasets_have_required_keys():
    required = {"collection", "asset", "description", "value_type", "filter", "classify", "max_h3_res"}
    for key, cfg in DATASET_CONFIG.items():
        assert required <= set(cfg), f"{key} missing keys"
        assert callable(cfg["filter"]) and callable(cfg["classify"])
