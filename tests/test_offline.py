#!/usr/bin/env python3
"""Offline tests: geometry, validation, data_source mapping. No API keys."""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import mcp_server  # noqa: E402
from validation import collect_errors, schema_data_source, validate_payload  # noqa: E402


SQUARE = {
    "type": "Feature",
    "properties": {"fj": "R2"},
    "geometry": {
        "type": "Polygon",
        "coordinates": [[
            [113.2644, 23.1291],
            [113.2654, 23.1291],
            [113.2654, 23.1301],
            [113.2644, 23.1301],
            [113.2644, 23.1291],
        ]],
    },
}


def _valid_result(**overrides):
    base = {
        "feature_index": 0,
        "data_source": "hybrid",
        "region_type": [{"label": "住宅小区", "confidence": 0.7, "evidence": "landuse=R2"}],
        "possible_buildings": [{"label": "多层住宅楼", "confidence": 0.6, "evidence": "building=apartments"}],
        "related_projects": [{
            "label": "XX花园二期建设项目",
            "confidence": 0.8,
            "evidence": "MCP 查询到 POI name=XX花园二期建设项目",
        }],
    }
    base.update(overrides)
    return base


class GeometryTests(unittest.TestCase):
    def test_polygon_centroid_and_area(self):
        stats = mcp_server.geometry_stats(SQUARE, 0)
        self.assertEqual(stats["index"], 0)
        self.assertEqual(stats["geometry_type"], "Polygon")
        self.assertAlmostEqual(stats["centroid"]["lon"], 113.2648, places=4)
        self.assertAlmostEqual(stats["centroid"]["lat"], 23.1295, places=4)
        self.assertGreater(stats["area_m2"], 0)
        self.assertGreater(stats["bbox_width_m"], 0)

    def test_empty_geometry(self):
        stats = mcp_server.geometry_stats({"type": "Feature", "geometry": {}, "properties": {}}, 1)
        self.assertIn("error", stats)

    def test_feature_count_limit(self):
        geojson = {"type": "FeatureCollection", "features": [SQUARE] * (mcp_server.MAX_FEATURES + 1)}
        with self.assertRaises(ValueError):
            mcp_server.analyze_regions(geojson, search_projects=False, search_poi=False)


class ValidationTests(unittest.TestCase):
    def test_schema_example_passes(self):
        self.assertEqual(collect_errors(_valid_result()), [])
        self.assertTrue(validate_payload(_valid_result())["valid"])

    def test_missing_top_keys(self):
        errors = collect_errors({"region_type": [{"label": "x", "confidence": 0.1, "evidence": "e"}]})
        self.assertTrue(any("missing top-level keys" in e for e in errors))

    def test_invalid_data_source_rejected(self):
        errors = collect_errors(_valid_result(data_source="amap+baidu+osm"))
        self.assertTrue(any("data_source" in e for e in errors))

    def test_indirect_project_needs_supported_by(self):
        result = _valid_result(related_projects=[{
            "label": "住宅类建设项目，具体名称未知",
            "confidence": 0.4,
            "evidence": "未发现直接项目名称，只能根据区域和建筑证据推断",
        }])
        errors = collect_errors(result)
        self.assertTrue(any("supported_by" in e for e in errors))

    def test_high_confidence_without_direct_evidence(self):
        result = _valid_result(related_projects=[{
            "label": "某小区",
            "confidence": 0.9,
            "evidence": "看起来像住宅区",
            "supported_by": "region_type[0]",
        }])
        errors = collect_errors(result)
        self.assertTrue(any("0.6" in e or "0.4" in e for e in errors))


class DataSourceTests(unittest.TestCase):
    def test_offline_when_empty(self):
        self.assertEqual(schema_data_source([]), "offline")

    def test_single_source(self):
        self.assertEqual(schema_data_source(["amap", "amap"]), "amap")

    def test_multi_source_is_hybrid(self):
        self.assertEqual(schema_data_source(["amap", "osm"]), "hybrid")


class CoordTests(unittest.TestCase):
    def test_outside_china_passthrough(self):
        lon, lat = mcp_server.wgs84_to_gcj02(0.0, 51.5)
        self.assertEqual((lon, lat), (0.0, 51.5))

    def test_inside_china_offsets(self):
        lon, lat = mcp_server.wgs84_to_gcj02(113.2644, 23.1291)
        self.assertNotEqual((lon, lat), (113.2644, 23.1291))


if __name__ == "__main__":
    unittest.main()
