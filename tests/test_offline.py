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
    apply_regeo_for_jobs,
    classify_amap_around,
    classify_baidu_search,
    has_admin_context,
    maybe_regeo_amap,
    maybe_regeo_baidu,
    merge_source_records,
    overpass_query,
    overpass_query_batch,
    project_signal,
    project_signal_tokens,
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

    def test_calculate_geometry_emits_geometry_invalid(self):
        good = json.loads(json.dumps(SQUARE))
        bad = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": []}}
        fc = {"type": "FeatureCollection", "features": [good, good, good, bad]}
        with mock.patch.dict(os.environ, {"GEOMETRY_FAIL_RATIO": "0.5"}):
            out = mcp_server.handle_tool("calculate_geometry", {"geojson": fc})
        self.assertFalse(out["isError"])
        payload = out["structuredContent"]
        alert = next(a for a in payload["input_alerts"] if a["code"] == "GEOMETRY_INVALID")
        self.assertIn(3, alert["invalid_indices"])
        self.assertEqual(alert.get("invalid_reasons", {}).get("3"), "geometry_stat_failed")

    def test_calculate_geometry_fail_fast_half_invalid(self):
        good = json.loads(json.dumps(SQUARE))
        bad = {"type": "Feature", "properties": {}, "geometry": {}}
        fc = {"type": "FeatureCollection", "features": [good, bad]}
        with mock.patch.dict(os.environ, {"GEOMETRY_FAIL_RATIO": "0.5"}):
            out = mcp_server.handle_tool("calculate_geometry", {"geojson": fc})
        self.assertTrue(out["isError"])

    def test_no_coordinate_system_warning(self):
        stats = mcp_server.geometry_stats(SQUARE, 0)
        self.assertNotIn("coordinate_system_warning", stats)


class CentroidAndHoleTests(unittest.TestCase):
    FIXTURES = os.path.join(ROOT, "tests", "fixtures")

    def _beijing_rect(self, width_m=11.0, height_m=10.0, lon=116.39, lat=39.91):
        from geo_geometry import deg_to_m_factors

        mx, my = deg_to_m_factors(lat)
        dlon, dlat = (width_m / 2.0) / mx, (height_m / 2.0) / my
        ring = [
            [lon - dlon, lat - dlat],
            [lon + dlon, lat - dlat],
            [lon + dlon, lat + dlat],
            [lon - dlon, lat + dlat],
            [lon - dlon, lat - dlat],
        ]
        return {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }, width_m * height_m, lon, lat, ring

    def test_small_beijing_polygon_centroid_in_bbox(self):
        feat, _area, lon, lat, _ring = self._beijing_rect()
        stats = mcp_server.geometry_stats(feat, 0)
        bbox = stats["bbox"]
        c = stats["centroid"]
        self.assertGreaterEqual(c["lon"], bbox["min_lon"])
        self.assertLessEqual(c["lon"], bbox["max_lon"])
        self.assertGreaterEqual(c["lat"], bbox["min_lat"])
        self.assertLessEqual(c["lat"], bbox["max_lat"])
        self.assertAlmostEqual(c["lon"], lon, places=5)
        self.assertAlmostEqual(c["lat"], lat, places=5)

    def test_local_metric_vs_vertex_avg_small_ring(self):
        from geo_geometry import deg_to_m_factors

        feat, _area, _lon, _lat, ring = self._beijing_rect()
        stats = mcp_server.geometry_stats(feat, 0)
        n = len(ring) - 1
        avg_lon = sum(p[0] for p in ring[:n]) / n
        avg_lat = sum(p[1] for p in ring[:n]) / n
        mx, my = deg_to_m_factors(avg_lat)
        dist = ((stats["centroid"]["lon"] - avg_lon) * mx) ** 2 + ((stats["centroid"]["lat"] - avg_lat) * my) ** 2
        self.assertLess(dist ** 0.5, 50.0)

    def test_centroid_area_vs_analytic(self):
        feat, expected, _lon, _lat, _ring = self._beijing_rect()
        stats = mcp_server.geometry_stats(feat, 0)
        self.assertLess(abs(stats["area_m2"] - expected) / expected, 0.01)

    def test_projected_ring_skips_deg_to_m_factors(self):
        from geo_geometry import ring_area_perimeter

        ring = [[500000.0, 4400000.0], [500100.0, 4400000.0], [500100.0, 4400100.0], [500000.0, 4400100.0], [500000.0, 4400000.0]]
        with mock.patch("geo_geometry.deg_to_m_factors", side_effect=AssertionError("must not convert projected y")):
            area, perim = ring_area_perimeter(ring, projected=True)
        self.assertAlmostEqual(area, 10000.0, delta=1.0)
        self.assertGreater(perim, 0)

    def test_polygon_with_hole_net_area_and_outer_perimeter(self):
        from geo_geometry import ring_area_perimeter, ring_centroid

        path = os.path.join(self.FIXTURES, "polygon_with_hole_wgs84.json")
        with open(path, encoding="utf-8") as f:
            fc = json.loads(f.read())
        feat = fc["features"][0]
        coords = feat["geometry"]["coordinates"]
        with mock.patch.dict(os.environ, {"GEO_HOLE_DEBUG": "0"}):
            stats = mcp_server.geometry_stats(feat, 0)
        a_o, p_o = ring_area_perimeter(coords[0])
        a_h, _p_h = ring_area_perimeter(coords[1])
        lon_o, lat_o, ra_o = ring_centroid(coords[0])
        lon_h, lat_h, ra_h = ring_centroid(coords[1])
        net = a_o - a_h
        w_o = ra_o if ra_o > 0 else a_o
        w_h = ra_h if ra_h > 0 else a_h
        exp_lon = (lon_o * w_o - lon_h * w_h) / (w_o - w_h)
        exp_lat = (lat_o * w_o - lat_h * w_h) / (w_o - w_h)
        self.assertAlmostEqual(stats["area_m2"], round(net, 1), places=1)
        self.assertAlmostEqual(stats["perimeter_m"], round(p_o, 1), places=1)
        self.assertAlmostEqual(stats["centroid"]["lon"], round(exp_lon, 6), places=6)
        self.assertAlmostEqual(stats["centroid"]["lat"], round(exp_lat, 6), places=6)
        self.assertLess(net / a_o, 0.95)

    def test_multipolygon_with_hole_covers_parts(self):
        square = SQUARE["geometry"]["coordinates"][0]
        with open(os.path.join(self.FIXTURES, "polygon_with_hole_wgs84.json"), encoding="utf-8") as f:
            hole_poly = json.loads(f.read())
        hole_coords = hole_poly["features"][0]["geometry"]["coordinates"]
        feat = {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "MultiPolygon", "coordinates": [[square], hole_coords]},
        }
        with mock.patch.dict(os.environ, {"GEO_HOLE_DEBUG": "0"}):
            stats = mcp_server.geometry_stats(feat, 0)
        self.assertEqual(stats["geometry_type"], "MultiPolygon")
        self.assertGreater(stats["area_m2"], 0)
        self.assertIsNotNone(stats["centroid"])

    def test_hole_debug_stderr_when_ratio_below_threshold(self):
        import io

        path = os.path.join(self.FIXTURES, "polygon_with_hole_wgs84.json")
        with open(path, encoding="utf-8") as f:
            feat = json.loads(f.read())["features"][0]
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GEO_HOLE_DEBUG": "1", "GEO_HOLE_DEBUG_RATIO": "0.95"}):
            with mock.patch("geo_geometry.sys.stderr", buf):
                mcp_server.geometry_stats(feat, 7)
        text = buf.getvalue()
        self.assertIn("hole-debug", text)
        self.assertIn("index=7", text)
        self.assertIn("hole_count=", text)

    def test_zero_net_area_compactness_none(self):
        ring = [[116.39, 39.91], [116.391, 39.91], [116.391, 39.911], [116.39, 39.911], [116.39, 39.91]]
        feat = {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [ring, list(ring)]},
        }
        with mock.patch.dict(os.environ, {"GEO_HOLE_DEBUG": "0"}):
            stats = mcp_server.geometry_stats(feat, 0)
        self.assertEqual(stats["area_m2"], 0.0)
        self.assertIsNone(stats["compactness"])


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
        self.assertTrue(geo_input._looks_like_esri(payload))
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
        reasons = idx_alert.get("simplify_reasons") or {}
        self.assertIn("esri_ring_roles_unresolved", reasons.get("0", []))
        self.assertEqual(fc["features"][0]["geometry"]["type"], "MultiPolygon")
        self.assertEqual(len(fc["features"][0]["geometry"]["coordinates"]), 2)

    def test_esri_cw_hole_preserved_no_simplified_alert(self):
        path = os.path.join(self.FIXTURES, "esri_polygon_with_hole_cw.json")
        fc, meta = geo_input.normalize_geo_input(input_path=path)
        codes = [a["code"] for a in meta["input_alerts"]]
        self.assertNotIn("GEOMETRY_SIMPLIFIED", codes)
        geom = fc["features"][0]["geometry"]
        self.assertEqual(geom["type"], "Polygon")
        self.assertEqual(len(geom["coordinates"]), 2)

    def test_esri_paths_converted_keeps_generic_message(self):
        path = os.path.join(self.FIXTURES, "esri_paths_false_negative.json")
        fc, meta = geo_input.normalize_geo_input(input_path=path)
        alert = next(a for a in meta["input_alerts"] if a["code"] == "GEOMETRY_SIMPLIFIED")
        self.assertIn("简化转换", alert["user_message"])
        self.assertNotIn("环角色", alert["user_message"])
        self.assertIn("paths_converted", (alert.get("simplify_reasons") or {}).get("0", []))
        self.assertEqual(fc["features"][0]["geometry"]["type"], "LineString")

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
        self.assertEqual(alert.get("invalid_reasons", {}).get("3"), "geometry_stat_failed")

    def test_residual_esri_keys_hybrid_geometry(self):
        path = os.path.join(self.FIXTURES, "esri_hybrid_paths_with_coordinates.json")
        fc, _meta = geo_input.normalize_geo_input(input_path=path)
        from geo_geometry import feature_list

        feats = feature_list(fc)
        reasons = geo_input.scan_residual_esri_geometry(feats)
        stats = [mcp_server.geometry_stats(f, i) for i, f in enumerate(feats)]
        alerts = geo_input.build_geometry_invalid_alerts(stats, len(feats), structure_reasons=reasons)
        alert = next(a for a in alerts if a["code"] == "GEOMETRY_INVALID")
        self.assertIn(0, alert["invalid_indices"])
        self.assertEqual(alert["invalid_reasons"]["0"], "residual_esri_keys")
        self.assertIn("Esri", alert["user_message"])
        with open(path, encoding="utf-8") as f:
            hybrid = json.load(f)["features"][0]
        fc_multi = {"type": "FeatureCollection", "features": [json.loads(json.dumps(SQUARE))] * 3 + [hybrid]}
        with mock.patch.dict(os.environ, {"GEOMETRY_FAIL_RATIO": "0.5"}):
            out = mcp_server.analyze_regions(geojson=fc_multi, search_projects=False, search_poi=False)
        alert2 = next(a for a in out["input_alerts"] if a["code"] == "GEOMETRY_INVALID")
        self.assertIn(3, alert2["invalid_indices"])
        self.assertEqual(alert2["invalid_reasons"]["3"], "residual_esri_keys")

    def test_scan_residual_esri_geometry_unit(self):
        feats = [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
                "paths": [[[0, 0], [1, 1]]],
            },
            "properties": {},
        }]
        reasons = geo_input.scan_residual_esri_geometry(feats)
        self.assertEqual(reasons, {0: "residual_esri_keys"})

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
    def test_batch_retry_recommended_at_threshold(self):
        features = []
        for i in range(10):
            amap = {"source": "amap", "status": "error", "reason_code": "RATE_LIMIT", "reason": "CUQPS"} if i == 0 else {"source": "amap", "status": "empty"}
            features.append({
                "index": i,
                "sources": [
                    amap,
                    {"source": "baidu", "status": "empty"},
                    {"source": "osm", "status": "empty"},
                ],
            })
        summary = mcp_server.summarize_online_channels(features)
        self.assertTrue(summary["batch_retry_recommended"])
        self.assertIn("rate_limit", summary)
        self.assertAlmostEqual(summary["rate_limit"]["amap"]["feature_ratio"], 0.1)

    def test_batch_retry_not_recommended_below_threshold(self):
        features = []
        for i in range(10):
            amap = {"source": "amap", "status": "error", "reason_code": "RATE_LIMIT", "reason": "CUQPS"} if i == 0 else {"source": "amap", "status": "empty"}
            features.append({
                "index": i,
                "sources": [amap, {"source": "baidu", "status": "empty"}, {"source": "osm", "status": "empty"}],
            })
        with mock.patch.object(mcp_server, "RATE_LIMIT_BATCH_RATIO", 0.15):
            summary = mcp_server.summarize_online_channels(features)
        self.assertFalse(summary["batch_retry_recommended"])


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


TEST3_PATH = os.path.join(ROOT, "tests", "fixtures", "test3.local.json")


@unittest.skipUnless(os.path.exists(TEST3_PATH), "test3.local.json not present")
class Test3GroundTruthTests(unittest.TestCase):
    def test_all_25_features_centroid_and_area(self):
        from geo_geometry import deg_to_m_factors

        with open(TEST3_PATH, encoding="utf-8") as f:
            fc = json.loads(f.read())
        self.assertEqual(len(fc["features"]), 25)
        with mock.patch.dict(os.environ, {"GEO_HOLE_DEBUG": "0"}):
            for i, feat in enumerate(fc["features"]):
                pr = feat["properties"]
                ax, ay = float(pr["X"]), float(pr["Y"])
                expected_area = float(pr["Area"])
                stats = mcp_server.geometry_stats(feat, i)
                mx, my = deg_to_m_factors(ay)
                dist = ((stats["centroid"]["lon"] - ax) * mx) ** 2 + ((stats["centroid"]["lat"] - ay) * my) ** 2
                self.assertLess(dist ** 0.5, 1.0, msg=f"FID {pr.get('FID')} centroid {dist ** 0.5:.2f} m")
                self.assertLess(abs(stats["area_m2"] - expected_area) / expected_area, 0.005, msg=f"FID {pr.get('FID')} area")


class ProjectSignalSsotTests(unittest.TestCase):
    def test_tokens_include_keywords_and_extras(self):
        tokens = project_signal_tokens()
        for tok in ("在建", "项目", "工地", "建设", "工程", "construction", "development", "project"):
            self.assertIn(tok, tokens)

    def test_project_signal_matches_engineering_not_shop(self):
        self.assertTrue(project_signal({"name": "某某工程"}))
        self.assertTrue(project_signal({"name": "Riverfront Construction"}))
        self.assertFalse(project_signal({"name": "便利店"}))

    def test_osm_name_tokens_include_engineering(self):
        summarized = geo_clients._summarize_overpass_elements([{"tags": {"name": "某某工程"}}])
        self.assertTrue(summarized["project_signals"])


class RegeoBatchTests(unittest.TestCase):
    def _amap_src(self):
        return {"source": "amap", "status": "ok", "places": [], "items": []}

    def _baidu_src(self):
        return {"source": "baidu", "status": "ok", "places": [], "items": []}

    def test_amap_batch_regeo_one_http_two_points(self):
        urls = []

        class Fake:
            def request_json(self, url, **kwargs):
                urls.append(url)
                return {
                    "status": "1",
                    "regeocodes": [
                        {"formatted_address": "甲地", "addressComponent": {"district": "区A"}},
                        {"formatted_address": "乙地", "addressComponent": {"district": "区B"}},
                    ],
                }

        amap_by = {0: self._amap_src(), 1: self._amap_src()}
        jobs = [(0, 23.1, 113.2, 300.0), (1, 23.2, 113.3, 300.0)]
        cache: dict = {}
        with mock.patch.dict(os.environ, {"AMAP_KEY": "k"}):
            with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
                with mock.patch.object(geo_clients._AMAP_BUCKET, "acquire") as acq:
                    apply_regeo_for_jobs(jobs, amap_by, {}, cache)
                    self.assertEqual(acq.call_count, 1)
        self.assertEqual(len(urls), 1)
        self.assertIn("geocode/regeo", urls[0])
        self.assertIn("batch=true", urls[0])
        self.assertTrue("|" in urls[0] or "%7C" in urls[0])
        self.assertTrue(any(p.get("name") == "甲地" for p in amap_by[0]["places"]))
        self.assertTrue(any(p.get("name") == "乙地" for p in amap_by[1]["places"]))

    def test_same_centroid_cache_single_amap_regeo(self):
        urls = []

        class Fake:
            def request_json(self, url, **kwargs):
                urls.append(url)
                return {
                    "status": "1",
                    "regeocodes": [
                        {"formatted_address": "同址", "addressComponent": {"district": "区C"}},
                    ],
                }

        amap_by = {0: self._amap_src(), 1: self._amap_src()}
        jobs = [(0, 23.1291, 113.2644, 300.0), (1, 23.1291, 113.2644, 300.0)]
        with mock.patch.dict(os.environ, {"AMAP_KEY": "k"}):
            with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
                apply_regeo_for_jobs(jobs, amap_by, {}, {})
        self.assertEqual(len(urls), 1)
        loc = urls[0].split("location=")[-1].split("&")[0]
        self.assertNotIn("|", loc)
        self.assertNotIn("%7C", loc)
        self.assertTrue(any(p.get("name") == "同址" for p in amap_by[0]["places"]))
        self.assertTrue(any(p.get("name") == "同址" for p in amap_by[1]["places"]))

    def test_baidu_regeo_acquires_bucket(self):
        class Fake:
            def request_json(self, url, **kwargs):
                return {"status": 0, "result": {"formatted_address": "百度址", "addressComponent": {"district": "区D"}}}

        with mock.patch.dict(os.environ, {"BAIDU_AK": "k"}):
            with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
                with mock.patch.object(geo_clients._BAIDU_BUCKET, "acquire") as acq:
                    out = maybe_regeo_baidu(self._baidu_src(), 23.1, 113.2)
                    acq.assert_called_once()
        self.assertTrue(any(p.get("name") == "百度址" for p in out["places"]))

    def test_amap_single_regeo_acquires_bucket(self):
        class Fake:
            def request_json(self, url, **kwargs):
                return {"status": "1", "regeocode": {"formatted_address": "高德址", "addressComponent": {}}}

        with mock.patch.dict(os.environ, {"AMAP_KEY": "k"}):
            with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
                with mock.patch.object(geo_clients._AMAP_BUCKET, "acquire") as acq:
                    out = maybe_regeo_amap(self._amap_src(), 23.1, 113.2)
                    acq.assert_called_once()
        self.assertTrue(any(p.get("name") == "高德址" for p in out["places"]))


class ExpandDedupTests(unittest.TestCase):
    def test_expand_skips_unavailable_amap(self):
        amap_radii: list[float] = []
        orig_amap = geo_clients.query_amap
        orig_baidu = geo_clients.query_baidu

        def wrap_amap(lat, lon, radius, keywords=None):
            amap_radii.append(radius)
            return orig_amap(lat, lon, radius, keywords)

        baidu_radii: list[float] = []

        def wrap_baidu(lat, lon, radius, keywords=None):
            baidu_radii.append(radius)
            return orig_baidu(lat, lon, radius, keywords)

        class Fake:
            def request_json(self, url, **kwargs):
                if "amap" in url:
                    return {"status": "1", "pois": []}
                if "reverse_geocoding" in url:
                    return {"status": 0, "result": {"formatted_address": "x", "addressComponent": {}}}
                return {"status": 0, "results": []}

        env = {k: v for k, v in os.environ.items() if k != "AMAP_KEY"}
        env["BAIDU_AK"] = "k"
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(geo_clients, "OSM_ENABLED", False):
                with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
                    with mock.patch.object(geo_clients, "query_amap", wrap_amap):
                        with mock.patch.object(geo_clients, "query_baidu", wrap_baidu):
                            mcp_server.analyze_regions(
                                {"type": "FeatureCollection", "features": [SQUARE]},
                                search_projects=True,
                                search_poi=True,
                                expand_radius_if_needed=True,
                            )
        self.assertEqual(len(amap_radii), 1)
        self.assertEqual(len(baidu_radii), 2)
        self.assertGreater(baidu_radii[1], baidu_radii[0])

    def test_expand_still_queries_ok_amap(self):
        amap_radii: list[float] = []
        orig_amap = geo_clients.query_amap

        def wrap_amap(lat, lon, radius, keywords=None):
            amap_radii.append(radius)
            return orig_amap(lat, lon, radius, keywords)

        class Fake:
            def request_json(self, url, **kwargs):
                if "geocode/regeo" in url:
                    return {"status": "1", "regeocode": {"formatted_address": "x", "addressComponent": {}}}
                if "amap" in url:
                    return {"status": "1", "pois": [{"name": "便利店", "address": "某路1号"}]}
                return {"status": 0, "results": []}

        env = {k: v for k, v in os.environ.items() if k != "BAIDU_AK"}
        env["AMAP_KEY"] = "k"
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(geo_clients, "OSM_ENABLED", False):
                with mock.patch.object(geo_clients, "get_http", return_value=Fake()):
                    with mock.patch.object(geo_clients, "query_amap", wrap_amap):
                        mcp_server.analyze_regions(
                            {"type": "FeatureCollection", "features": [SQUARE]},
                            search_projects=True,
                            search_poi=True,
                            expand_radius_if_needed=True,
                        )
        self.assertEqual(len(amap_radii), 2)
        self.assertGreater(amap_radii[1], amap_radii[0])

    def test_search_project_evidence_skips_unavailable_expand(self):
        amap_radii: list[float] = []

        def fake_amap(lat, lon, radius, keywords=None):
            amap_radii.append(radius)
            return geo_clients._source_shell(
                "amap", status="unavailable", reason_code=NO_API_KEY, reason="AMAP_KEY not configured"
            )

        def fake_baidu(lat, lon, radius, keywords=None):
            return geo_clients._source_shell("baidu", status="ok", items=[], count=0)

        def fake_osm(lat, lon, radius):
            return geo_clients._source_shell("osm", status="empty")

        with mock.patch.object(mcp_server, "query_amap", fake_amap):
            with mock.patch.object(mcp_server, "query_baidu", fake_baidu):
                with mock.patch.object(mcp_server, "overpass_query", fake_osm):
                    with mock.patch.object(mcp_server, "maybe_regeo_amap", lambda src, lat, lon: src):
                        with mock.patch.object(mcp_server, "maybe_regeo_baidu", lambda src, lat, lon: src):
                            mcp_server.handle_tool(
                                "search_project_evidence",
                                {"lat": 23.1, "lon": 113.2, "radius_m": 300, "expand_if_empty": True},
                            )
        self.assertEqual(amap_radii, [300.0])


class VersionSsotTests(unittest.TestCase):
    def test_server_version_from_version_module(self):
        import version

        self.assertEqual(version.SERVER_VERSION, "2.5.3")
        self.assertEqual(mcp_server.SERVER_VERSION, version.SERVER_VERSION)
        self.assertEqual(geo_clients.SERVER_VERSION, version.SERVER_VERSION)
        ua = geo_clients.get_http().client.headers.get("User-Agent", "")
        self.assertIn(f"geo-region-inference/{version.SERVER_VERSION}", ua)


if __name__ == "__main__":
    unittest.main()
