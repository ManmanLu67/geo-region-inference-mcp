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
import gov_search  # noqa: E402
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
from gov_search import (  # noqa: E402
    build_search_plan,
    extract_admin_division,
    extract_match_roads,
    load_gov_search_templates,
    prepare_gov_web_search,
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

    def test_esri_nested_features_converted(self):
        payload = {
            "features": [{
                "attributes": {"id": 1},
                "geometry": {"rings": [[[116.39, 39.91], [116.40, 39.91], [116.40, 39.92], [116.39, 39.92], [116.39, 39.91]]]},
            }],
            "spatialReference": {"wkid": 4326},
        }
        fc, meta = geo_input.normalize_geo_input(geojson=payload)
        stats = mcp_server.geometry_stats(fc["features"][0], 0)
        self.assertNotIn("error", stats)
        self.assertGreater(stats.get("area_m2") or 0, 0)
        self.assertTrue(meta.get("esri_converted"))

    def test_esri_top_level_rings_converted(self):
        payload = {
            "attributes": {"id": 1},
            "rings": [[[116.39, 39.91], [116.40, 39.91], [116.40, 39.92], [116.39, 39.92], [116.39, 39.91]]],
            "spatialReference": {"wkid": 4326},
        }
        fc, meta = geo_input.normalize_geo_input(geojson=payload)
        self.assertEqual(len(fc["features"]), 1)
        self.assertTrue(meta.get("esri_converted"))

    def test_esri_fixture_file(self):
        path = os.path.join(self.FIXTURES, "esri_featurecollection_rings.json")
        out = mcp_server.analyze_regions(input_path=path, search_projects=False, search_poi=False)
        self.assertEqual(out["feature_count"], 1)
        self.assertGreater(out["features"][0].get("area_m2") or 0, 0)

    def test_esri_paths_false_negative_converted(self):
        path = os.path.join(self.FIXTURES, "esri_paths_false_negative.json")
        fc, meta = geo_input.normalize_geo_input(input_path=path)
        stats = mcp_server.geometry_stats(fc["features"][0], 0)
        self.assertNotIn("error", stats)
        self.assertEqual(stats.get("geometry_type"), "LineString")

    def test_esri_inner_ring_geometry_simplified_alert(self):
        path = os.path.join(self.FIXTURES, "esri_polygon_with_hole.json")
        fc, meta = geo_input.normalize_geo_input(input_path=path)
        codes = [a["code"] for a in meta["input_alerts"]]
        self.assertIn("GEOMETRY_SIMPLIFIED", codes)
        idx_alert = next(a for a in meta["input_alerts"] if a["code"] == "GEOMETRY_SIMPLIFIED")
        self.assertIn(0, idx_alert.get("feature_indices", []))

    def test_geometry_invalid_partial_alert(self):
        good = json.loads(json.dumps(SQUARE))
        bad = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": []}}
        fc = {"type": "FeatureCollection", "features": [good, good, good, bad]}
        with mock.patch.dict(os.environ, {"GEOMETRY_FAIL_RATIO": "0.5"}):
            out = mcp_server.analyze_regions(geojson=fc, search_projects=False, search_poi=False)
        codes = [a["code"] for a in out["input_alerts"]]
        self.assertIn("GEOMETRY_INVALID", codes)
        alert = next(a for a in out["input_alerts"] if a["code"] == "GEOMETRY_INVALID")
        self.assertEqual(alert["severity"], "warning")
        self.assertIn(3, alert["invalid_indices"])

    def test_geometry_fail_fast_half_invalid(self):
        good = json.loads(json.dumps(SQUARE))
        bad = {"type": "Feature", "properties": {}, "geometry": {}}
        fc = {"type": "FeatureCollection", "features": [good, bad]}
        with mock.patch.dict(os.environ, {"GEOMETRY_FAIL_RATIO": "0.5"}):
            with self.assertRaises(ValueError):
                mcp_server.analyze_regions(geojson=fc, search_projects=False, search_poi=False)

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

    def test_gov_publicity_requires_source_url(self):
        result = _valid_result(related_projects=[{
            "label": "某项目",
            "confidence": 0.75,
            "evidence_type": "gov_publicity",
            "evidence": "规划公示转述",
        }])
        errors = collect_errors(result)
        self.assertTrue(any("source_url" in e for e in errors))

    def test_gov_publicity_weak_cap(self):
        result = _valid_result(related_projects=[{
            "label": "同区建设活动",
            "confidence": 0.5,
            "evidence_type": "gov_publicity_weak",
            "evidence": "仅确认同区有公示，未能对应本地块",
        }])
        errors = collect_errors(result)
        self.assertTrue(any("0.3" in e for e in errors))

    def test_gov_publicity_with_url_passes(self):
        result = _valid_result(related_projects=[{
            "label": "XX路以东地块项目",
            "confidence": 0.75,
            "evidence_type": "gov_publicity",
            "evidence": "自然资源局公示转述",
            "source_url": "https://example.gov.cn/plan/1",
        }])
        self.assertEqual(collect_errors(result), [])

    def test_missing_evidence_type_rejected(self):
        result = _valid_result(related_projects=[{
            "label": "x",
            "confidence": 0.5,
            "evidence": "e",
        }])
        errors = collect_errors(result)
        self.assertTrue(any("evidence_type" in e for e in errors))


class GovSearchTests(unittest.TestCase):
    def _feature(self, *, project_evidence=None, places=None, roads=None, index=0):
        sources = [{
            "source": "amap",
            "status": "ok",
            "places": places or [
                {"tag": "amap_city", "name": "广州市"},
                {"tag": "amap_district", "name": "天河区"},
                {"tag": "amap_township", "name": "猎德街道"},
            ],
            "roads": roads or [{"name": "猎德大道"}],
        }]
        feat = {"index": index, "sources": sources}
        if project_evidence is not None:
            feat["project_evidence"] = project_evidence
        return feat

    def test_extract_admin_street_label(self):
        admin = extract_admin_division(self._feature())
        self.assertEqual(admin["street_label"], "广州市天河区猎德街道")
        self.assertEqual(admin["district_label"], "广州市天河区")
        self.assertTrue(admin["has_district_level"])

    def test_extract_admin_baidu_only(self):
        feat = self._feature(places=[
            {"tag": "baidu_city", "name": "广州市"},
            {"tag": "baidu_district", "name": "天河区"},
        ])
        admin = extract_admin_division(feat)
        self.assertEqual(admin["street_label"], "广州市天河区")
        self.assertTrue(admin["has_district_level"])

    def test_build_search_plan_four_rounds(self):
        admin = extract_admin_division(self._feature())
        roads = extract_match_roads(self._feature())
        plan = build_search_plan(admin, roads, load_gov_search_templates())
        self.assertGreaterEqual(len(plan["rounds"]), 3)
        names = [r["name"] for r in plan["rounds"]]
        self.assertIn("street_core", names)
        self.assertIn("district_road_cross", names)

    def test_query_cap_at_24(self):
        admin = extract_admin_division(self._feature())
        roads = ["路A", "路B", "路C", "路D", "路E"]
        plan = build_search_plan(admin, roads, load_gov_search_templates())
        total = sum(len(r["queries"]) for r in plan["rounds"])
        self.assertLessEqual(total, 24)

    def test_empty_only_skips_project_evidence(self):
        analyze = {
            "features": [
                self._feature(project_evidence=[{"label": "已有项目"}]),
                self._feature(index=1, project_evidence=[]),
            ]
        }
        out = prepare_gov_web_search(analyze)
        self.assertEqual(out["candidate_count"], 1)
        self.assertEqual(out["candidates"][0]["index"], 1)
        self.assertEqual(out["skipped_summary"]["has_project_evidence"], 1)

    def test_prepare_skips_no_admin(self):
        feat = {"index": 0, "sources": [{"source": "amap", "status": "ok", "places": [], "roads": []}]}
        out = prepare_gov_web_search({"features": [feat]})
        self.assertEqual(out["candidate_count"], 0)
        self.assertEqual(out["skipped_summary"]["no_admin"], 1)

    def test_mcp_tool_prepare_gov_web_search(self):
        feat = self._feature(index=0)
        feat["project_evidence"] = []
        out = mcp_server.handle_tool("prepare_gov_web_search", {"analyze_result": {"features": [feat]}})
        self.assertFalse(out.get("isError"))
        body = out["structuredContent"]
        self.assertEqual(body["candidate_count"], 1)
        self.assertIn("search_plan", body["candidates"][0])


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


class RateLimitSummaryTests(unittest.TestCase):
    def test_batch_retry_recommended(self):
        features = []
        for i in range(4):
            features.append({
                "index": i,
                "sources": [
                    {"source": "amap", "status": "error", "reason_code": "RATE_LIMIT", "reason": "CUQPS"},
                    {"source": "baidu", "status": "empty"},
                    {"source": "osm", "status": "empty"},
                ],
            })
        summary = mcp_server.summarize_online_channels(features)
        self.assertTrue(summary["batch_retry_recommended"])
        self.assertIn("rate_limit", summary)


class CheckApiStatusTests(unittest.TestCase):
    def test_tool_listed(self):
        out = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = [t["name"] for t in out["result"]["tools"]]
        self.assertIn("check_api_status", names)

    def test_single_mode_no_key(self):
        env = {k: v for k, v in os.environ.items() if k not in ("AMAP_KEY", "BAIDU_AK")}
        with mock.patch.dict(os.environ, env, clear=True):
            out = mcp_server.handle_tool("check_api_status", {"probe_mode": "single"})
        body = json.loads(out["content"][0]["text"])
        self.assertFalse(body["amap"]["key_valid"])

    def test_burst_mode_detects_concurrent_limit(self):
        call_counts = {"n": 0}

        def fake_query(lat, lon, radius, keywords):
            call_counts["n"] += 1
            if call_counts["n"] >= 4:
                return {"status": "error", "reason_code": "RATE_LIMIT", "reason": "CUQPS"}
            return {"status": "ok", "pois": []}

        with mock.patch.object(geo_clients, "query_amap", side_effect=fake_query):
            with mock.patch.dict(os.environ, {"AMAP_KEY": "k", "BURST_PROBE_MAX_CONCURRENCY": "8"}):
                out = geo_clients.probe_amap_burst(23.1, 113.2)
        self.assertTrue(out["concurrent_limit_detected"])
        self.assertEqual(out["rate_limit_at_concurrency"], 3)
        self.assertEqual(out["estimated_concurrent_limit"], 2)
        self.assertEqual(out["suggested_amap_qps_limit"], 2)


class AmapRetryTests(unittest.TestCase):
    def test_amap_retries_on_rate_limit(self):
        calls = {"n": 0}

        class Fake:
            counts = {"amap": 0, "baidu": 0, "overpass": 0, "total": 0}

            def request_json(self, url, **kwargs):
                calls["n"] += 1
                if "amap" in url and calls["n"] == 1:
                    return {"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT"}
                if "amap" in url:
                    return {"status": "1", "pois": []}
                return {"status": 0, "results": []}

        with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
            with mock.patch.dict(os.environ, {"AMAP_KEY": "k", "BAIDU_AK": "k", "AMAP_RETRY_MAX": "2"}):
                out = geo_clients.query_amap(23.1, 113.2, 300, None)
        self.assertGreater(calls["n"], 1)
        self.assertIn(out["status"], ("ok", "empty", "error"))


if __name__ == "__main__":
    unittest.main()
