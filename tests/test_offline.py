#!/usr/bin/env python3
"""Offline tests: geometry, validation, status contract, protocol, batch Overpass."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import geo_clients  # noqa: E402
import geo_input  # noqa: E402
import mcp_server  # noqa: E402
from geo_clients import (  # noqa: E402
    DISABLED,
    INVALID_API_KEY,
    NO_API_KEY,
    classify_amap_around,
    classify_baidu_search,
    has_admin_context,
    merge_source_records,
    overpass_query,
    overpass_query_batch,
    query_amap,
    query_baidu,
)
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
            "evidence_type": "poi_name",
        }],
    }
    base.update(overrides)
    return base


class GeometryTests(unittest.TestCase):
    def test_polygon_centroid_and_area(self):
        stats = mcp_server.geometry_stats(SQUARE, 0)
        self.assertEqual(stats["index"], 0)
        self.assertEqual(stats["geometry_type"], "Polygon")
        self.assertAlmostEqual(stats["centroid"]["lon"], 113.2649, places=4)
        self.assertAlmostEqual(stats["centroid"]["lat"], 23.1296, places=4)
        self.assertGreater(stats["area_m2"], 0)
        self.assertGreater(stats["bbox_width_m"], 0)

    def test_empty_geometry(self):
        stats = mcp_server.geometry_stats({"type": "Feature", "geometry": {}, "properties": {}}, 1)
        self.assertIn("error", stats)

    def test_feature_count_limit(self):
        geojson = {"type": "FeatureCollection", "features": [SQUARE] * (mcp_server.MAX_FEATURES + 1)}
        with self.assertRaises(ValueError):
            mcp_server.analyze_regions(geojson, search_projects=False, search_poi=False)

    def test_calculate_geometry_limit(self):
        geojson = {"type": "FeatureCollection", "features": [SQUARE] * (mcp_server.MAX_FEATURES + 1)}
        out = mcp_server.handle_tool("calculate_geometry", {"geojson": geojson})
        self.assertTrue(out["isError"])

    def test_no_coordinate_system_warning(self):
        stats = mcp_server.geometry_stats(SQUARE, 0)
        self.assertNotIn("coordinate_system_warning", stats)


class GeoInputTests(unittest.TestCase):
    FIXTURES = os.path.join(ROOT, "tests", "fixtures")

    def test_esri_nested_features_rejected(self):
        payload = {
            "features": [{
                "attributes": {"id": 1},
                "geometry": {"rings": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
            }]
        }
        with self.assertRaises(ValueError) as ctx:
            geo_input.load_geo_input(geojson=payload)
        self.assertIn("GeoJSON", str(ctx.exception))

    def test_esri_top_level_rings_rejected(self):
        payload = {"attributes": {}, "rings": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
        with self.assertRaises(ValueError) as ctx:
            geo_input.load_geo_input(geojson=payload)
        self.assertIn("Esri JSON", str(ctx.exception))

    def test_projected_without_crs_rejected(self):
        payload = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [[[500000, 4000000], [500100, 4000000], [500100, 4000100], [500000, 4000000]]]},
            }],
        }
        with self.assertRaises(ValueError) as ctx:
            geo_input.normalize_geo_input(geojson=payload)
        self.assertIn("projected", str(ctx.exception).lower())

    def test_crs_assumed_alert(self):
        fc, meta = geo_input.normalize_geo_input(geojson={"type": "FeatureCollection", "features": [SQUARE]})
        self.assertTrue(meta["crs"]["crs_assumed"])
        codes = [a["code"] for a in meta["input_alerts"]]
        self.assertIn("CRS_ASSUMED", codes)

    def test_reproject_4509_fixture(self):
        path = os.path.join(self.FIXTURES, "cgcs2000_4509_sample.geojson")
        fc, meta = geo_input.normalize_geo_input(input_path=path)
        self.assertTrue(meta["crs"]["reprojected"])
        stats = mcp_server.geometry_stats(fc["features"][0], 0)
        self.assertGreater(stats["centroid"]["lon"], 116)
        self.assertGreater(stats["centroid"]["lat"], 40)
        from pyproj import Transformer

        t = Transformer.from_crs(4509, 4326, always_xy=True)
        lon, lat = t.transform(443743.295, 4432179.854)
        self.assertAlmostEqual(stats["centroid"]["lon"], lon, places=2)
        self.assertAlmostEqual(stats["centroid"]["lat"], lat, places=2)

    def test_input_path_reads_fixture(self):
        path = os.path.join(self.FIXTURES, "multi_feature_sample.json")
        out = mcp_server.analyze_regions(input_path=path, search_projects=False, search_poi=False)
        self.assertGreater(out["feature_count"], 0)
        self.assertIn("input_meta", out)

    def test_strict_path_rejected(self):
        path = os.path.join(self.FIXTURES, "multi_feature_sample.json")
        with mock.patch.dict(os.environ, {"GEO_INPUT_STRICT": "true", "GEO_INPUT_ROOT": "C:\\nonexistent-root"}):
            with self.assertRaises(ValueError):
                geo_input.load_geo_input(input_path=path)

    def test_z_stripped(self):
        zsquare = json.loads(json.dumps(SQUARE))
        zsquare["geometry"]["coordinates"][0] = [
            [113.2644, 23.1291, 100],
            [113.2654, 23.1291, 100],
            [113.2654, 23.1301, 100],
            [113.2644, 23.1301, 100],
            [113.2644, 23.1291, 100],
        ]
        fc, meta = geo_input.normalize_geo_input(
            geojson={"type": "FeatureCollection", "features": [zsquare]}
        )
        self.assertTrue(meta["z_stripped"])
        stats = mcp_server.geometry_stats(fc["features"][0], 0)
        self.assertNotIn("error", stats)


class OnlineSummaryTests(unittest.TestCase):
    def test_all_channels_unavailable(self):
        env = {k: v for k, v in os.environ.items() if k not in ("AMAP_KEY", "BAIDU_AK")}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(geo_clients, "OSM_ENABLED", False):
                out = mcp_server.analyze_regions(
                    geojson={"type": "FeatureCollection", "features": [SQUARE]},
                    search_projects=True,
                    search_poi=False,
                    expand_radius_if_needed=False,
                )
        summary = out["online_summary"]
        self.assertTrue(summary["all_channels_unavailable"])
        self.assertTrue(summary["warnings"])
        self.assertIn("input_alerts", out)


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
            "evidence_type": "inferred",
        }])
        errors = collect_errors(result)
        self.assertTrue(any("supported_by" in e for e in errors))

    def test_high_confidence_without_direct_evidence(self):
        result = _valid_result(related_projects=[{
            "label": "某小区",
            "confidence": 0.9,
            "evidence": "看起来像住宅区",
            "evidence_type": "inferred",
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
        lon, lat = geo_clients.wgs84_to_gcj02(0.0, 51.5)
        self.assertEqual((lon, lat), (0.0, 51.5))

    def test_inside_china_offsets(self):
        lon, lat = geo_clients.wgs84_to_gcj02(113.2644, 23.1291)
        self.assertNotEqual((lon, lat), (113.2644, 23.1291))


class StatusContractTests(unittest.TestCase):
    def test_amap_invalid_key_is_error(self):
        out = classify_amap_around({"status": "0", "info": "INVALID_USER_KEY", "pois": []})
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["reason_code"], INVALID_API_KEY)

    def test_amap_empty_pois_is_empty(self):
        out = classify_amap_around({"status": "1", "pois": []})
        self.assertEqual(out["status"], "empty")

    def test_amap_with_pois_is_ok(self):
        out = classify_amap_around({"status": "1", "pois": [{"name": "A"}]})
        self.assertEqual(out["status"], "ok")

    def test_baidu_ok_and_empty(self):
        self.assertEqual(classify_baidu_search({"status": 0, "results": []})["status"], "empty")
        self.assertEqual(classify_baidu_search({"status": 0, "results": [{"name": "B"}]})["status"], "ok")

    def test_missing_key_unavailable(self):
        env = {k: v for k, v in os.environ.items() if k not in ("AMAP_KEY", "BAIDU_AK")}
        with mock.patch.dict(os.environ, env, clear=True):
            a = query_amap(23.1, 113.2, 300)
            b = query_baidu(23.1, 113.2, 300)
        self.assertEqual(a["status"], "unavailable")
        self.assertEqual(a["reason_code"], NO_API_KEY)
        self.assertEqual(b["status"], "unavailable")


class ProtocolTests(unittest.TestCase):
    def test_initialize_legacy(self):
        out = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(out["result"]["protocolVersion"], "2025-11-25")

    def test_initialize_does_not_echo_2026(self):
        out = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2026-07-28"}})
        self.assertEqual(out["result"]["protocolVersion"], "2025-11-25")

    def test_discover_versions_unchanged(self):
        out = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}})
        self.assertEqual(out["result"]["supportedVersions"], ["2026-07-28", "2025-11-25"])


class MergeAndAdminTests(unittest.TestCase):
    def test_merge_keeps_one_source(self):
        base = {"source": "amap", "status": "empty", "items": []}
        exp = {"source": "amap", "status": "ok", "items": [{"name": "工地A"}]}
        merged = merge_source_records(base, exp, 300, 750)
        self.assertEqual(merged["source"], "amap")
        self.assertEqual(merged["expanded_radius_m"], 750)
        self.assertEqual(merged["status"], "ok")
        self.assertEqual(len(merged["items"]), 1)

    def test_has_admin_context(self):
        self.assertTrue(has_admin_context({"places": [{"tag": "amap_district", "name": "天河区"}]}))
        self.assertFalse(has_admin_context({"items": [{"name": "x"}]}))


    def test_search_projects_false_omits_project_keywords(self):
        seen = []

        class Fake:
            counts = {"amap": 0, "baidu": 0, "overpass": 0, "total": 0}

            def request_json(self, url, **kwargs):
                seen.append(url)
                if "interpreter" in url or "overpass" in url:
                    return {"elements": []}
                if "amap" in url:
                    return {"status": "1", "pois": [{"name": "店", "address": "某路1号"}]}
                return {"status": 0, "results": [{"name": "店", "address": "某路1号"}]}

        with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
            with mock.patch.dict(os.environ, {"AMAP_KEY": "k", "BAIDU_AK": "k"}):
                mcp_server.analyze_regions(
                    {"type": "FeatureCollection", "features": [SQUARE]},
                    search_projects=False,
                    search_poi=True,
                    expand_radius_if_needed=False,
                )
        joined = " ".join(seen)
        self.assertNotIn("keywords=", joined)
        self.assertNotIn("%E9%A1%B9%E7%9B%AE", joined)

    def test_twenty_features_two_overpass_calls(self):
        calls = []

        class Fake:
            counts = {"amap": 0, "baidu": 0, "overpass": 0, "total": 0}

            def request_json(self, url, **kwargs):
                calls.append(url)
                self.counts["total"] += 1
                if "overpass" in url or "interpreter" in url:
                    self.counts["overpass"] += 1
                    return {"elements": []}
                self.counts["amap" if "amap" in url else "baidu"] += 1
                return {"status": "1", "pois": []} if "amap" in url else {"status": 0, "results": []}

        fake = Fake()
        features = []
        for i in range(20):
            features.append({
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [113.26 + i * 0.01, 23.12],
                        [113.261 + i * 0.01, 23.12],
                        [113.261 + i * 0.01, 23.121],
                        [113.26 + i * 0.01, 23.121],
                        [113.26 + i * 0.01, 23.12],
                    ]],
                },
            })
        with mock.patch.object(geo_clients, "get_http", return_value=fake):
            with mock.patch.dict(os.environ, {"AMAP_KEY": "k", "BAIDU_AK": "k"}):
                mcp_server.analyze_regions(
                    {"type": "FeatureCollection", "features": features},
                    search_projects=True,
                    search_poi=True,
                    expand_radius_if_needed=False,
                )
        self.assertEqual(fake.counts["overpass"], 2)


class OsmEnabledTests(unittest.TestCase):
    def test_disabled_overpass_query_skips_network(self):
        calls = []

        class Fake:
            def request_json(self, url, **kwargs):
                calls.append(url)
                return {"elements": []}

        with mock.patch.object(geo_clients, "OSM_ENABLED", False):
            with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
                out = overpass_query(23.1, 113.2, 300)
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(out["reason_code"], DISABLED)
        self.assertEqual(calls, [])

    def test_disabled_overpass_batch_skips_network(self):
        calls = []

        class Fake:
            def request_json(self, url, **kwargs):
                calls.append(url)
                return {"elements": []}

        points = [(i, 23.1 + i * 0.001, 113.2 + i * 0.001, 300) for i in range(25)]
        with mock.patch.object(geo_clients, "OSM_ENABLED", False):
            with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
                out = overpass_query_batch(points)
        self.assertEqual(set(out.keys()), {i for i, *_ in points})
        for rec in out.values():
            self.assertEqual(rec["status"], "unavailable")
            self.assertEqual(rec["reason_code"], DISABLED)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
