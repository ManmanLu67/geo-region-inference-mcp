#!/usr/bin/env python3
"""
geo-region-inference MCP server.

GIS/API work stays in geo_clients.py (sync httpx singleton). This module is
geometry, tool orchestration, and the stdio JSON-RPC surface.
"""

from __future__ import annotations

import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from geo_clients import (
    PROJECT_KEYWORDS,
    close_http,
    maybe_regeo_amap,
    maybe_regeo_baidu,
    merge_source_records,
    overpass_query,
    overpass_query_batch,
    query_amap,
    query_baidu,
    wgs84_to_gcj02,
)
from validation import schema_data_source, validate_payload

SERVER_NAME = "geo-region-inference"
SERVER_VERSION = "2.1.0"
MAX_FEATURES = 80
PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"


def log(msg: str) -> None:
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


def json_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
        "isError": is_error,
    }


def ok(value: Any) -> dict[str, Any]:
    return json_result(value, False)


def err(message: str, details: Any | None = None) -> dict[str, Any]:
    payload = {"error": message}
    if details is not None:
        payload["details"] = details
    return json_result(payload, True)


def deg_to_m_factors(lat_deg: float) -> tuple[float, float]:
    lat_rad = math.radians(lat_deg)
    return 111320.0 * math.cos(lat_rad), 111320.0


def flatten_coords(geom: dict[str, Any]):
    t = geom.get("type")
    coords = geom.get("coordinates")
    if t == "Point":
        yield coords
    elif t in ("MultiPoint", "LineString"):
        for c in coords or []:
            yield c
    elif t in ("MultiLineString", "Polygon"):
        for part in coords or []:
            for c in part:
                yield c
    elif t == "MultiPolygon":
        for poly in coords or []:
            for ring in poly:
                for c in ring:
                    yield c
    elif t == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from flatten_coords(g)


def outer_rings(geom: dict[str, Any]) -> list[list[list[float]]]:
    t = geom.get("type")
    coords = geom.get("coordinates", [])
    if t == "Polygon" and coords:
        return [coords[0]]
    if t == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def _projected_ring(ring: list[list[float]], projected: bool) -> list[tuple[float, float]]:
    if projected:
        return [(float(p[0]), float(p[1])) for p in ring]
    mean_lat = sum(float(p[1]) for p in ring) / len(ring)
    mx, my = deg_to_m_factors(mean_lat)
    return [(float(p[0]) * mx, float(p[1]) * my) for p in ring]


def ring_area_perimeter(ring: list[list[float]], projected: bool) -> tuple[float, float]:
    if len(ring) < 3:
        return 0.0, 0.0
    pts = _projected_ring(ring, projected)
    area2 = 0.0
    perim = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        area2 += x1 * y2 - x2 * y1
        perim += math.hypot(x2 - x1, y2 - y1)
    return abs(area2) / 2.0, perim


def ring_centroid(ring: list[list[float]], projected: bool) -> tuple[float, float, float]:
    if len(ring) < 3:
        return float(ring[0][0]), float(ring[0][1]), 0.0
    mean_lat = sum(float(p[1]) for p in ring) / len(ring)
    mx, my = (1.0, 1.0) if projected else deg_to_m_factors(mean_lat)
    pts = _projected_ring(ring, projected)
    area2 = 0.0
    cx = cy = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area = area2 / 2.0
    if abs(area) < 1e-12:
        uniq = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
        xs = [float(p[0]) for p in uniq]
        ys = [float(p[1]) for p in uniq]
        return sum(xs) / len(xs), sum(ys) / len(ys), 0.0
    cx = cx / (6.0 * area)
    cy = cy / (6.0 * area)
    return cx / mx, cy / my, abs(area)


def compact_properties(props: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    keys = list(props.keys())
    if len(keys) <= 25 and len(json.dumps(props, ensure_ascii=False)) <= 5000:
        return props, keys
    signal_tokens = (
        "project", "项目", "plan", "规划", "permit", "备案", "license", "许可",
        "code", "编号", "工程", "建设", "construct", "develop", "parcel", "地块", "fj",
    )
    selected = {k: props[k] for k in keys if any(token in str(k).lower() for token in signal_tokens)}
    return selected, keys


def geometry_stats(feature: dict[str, Any], index: int) -> dict[str, Any]:
    geom = feature.get("geometry") or {}
    props = feature.get("properties") or {}
    compact_props, property_keys = compact_properties(props)
    pts = list(flatten_coords(geom))
    if not pts:
        return {"index": index, "error": "no coordinates found", "properties": compact_props, "property_keys": property_keys}
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    projected = abs(cx) > 180 or abs(cy) > 90
    mx, my = (1.0, 1.0) if projected else deg_to_m_factors(cy)
    width = abs(max_x - min_x) * mx
    height = abs(max_y - min_y) * my
    long_side = max(width, height)
    short_side = max(min(width, height), 1e-9)
    aspect = long_side / short_side
    area = perim = compact = None
    rings = outer_rings(geom)
    t = geom.get("type")
    if rings:
        area_sum = perim_sum = 0.0
        cxs = cys = weights = 0.0
        for ring in rings:
            a, p = ring_area_perimeter(ring, projected)
            rlon, rlat, ra = ring_centroid(ring, projected)
            area_sum += a
            perim_sum += p
            w = ra if ra > 0 else a
            cxs += rlon * w
            cys += rlat * w
            weights += w
        area, perim = round(area_sum, 1), round(perim_sum, 1)
        if perim_sum > 0:
            compact = round((4 * math.pi * area_sum) / (perim_sum**2), 3)
        if weights > 0:
            cx, cy = cxs / weights, cys / weights
    elif t in ("LineString", "MultiLineString", "MultiPoint"):
        uniq = pts[:-1] if len(pts) > 1 and pts[0] == pts[-1] else pts
        cx = sum(float(p[0]) for p in uniq) / len(uniq)
        cy = sum(float(p[1]) for p in uniq) / len(uniq)
    result = {
        "index": index,
        "geometry_type": geom.get("type"),
        "vertex_count": len(pts),
        "centroid": {"lon": round(cx, 6), "lat": round(cy, 6)},
        "bbox": {"min_lon": round(min_x, 6), "min_lat": round(min_y, 6), "max_lon": round(max_x, 6), "max_lat": round(max_y, 6)},
        "area_m2": area,
        "perimeter_m": perim,
        "compactness": compact,
        "bbox_width_m": round(width, 1),
        "bbox_height_m": round(height, 1),
        "aspect_ratio": round(aspect, 2),
        "properties": compact_props,
        "property_keys": property_keys,
    }
    if projected:
        result["coordinate_system_warning"] = "coordinates look projected; treated as already-metric"
    return result


def feature_list(geojson: dict[str, Any]) -> list[dict[str, Any]]:
    if geojson.get("type") == "FeatureCollection":
        return list(geojson.get("features", []))
    if geojson.get("type") == "Feature":
        return [geojson]
    return [{"type": "Feature", "geometry": geojson, "properties": {}}]


def radius_from_stats(stats: dict[str, Any], minimum: float = 150.0, maximum: float = 2500.0) -> float:
    size = max(float(stats.get("bbox_width_m") or 0), float(stats.get("bbox_height_m") or 0))
    return round(min(max(size * 0.6, minimum), maximum), 1)


def project_signal(p: dict[str, Any]) -> bool:
    s = " ".join(str(p.get(k) or "") for k in ("name", "type", "address", "businessarea", "description")).lower()
    keywords = ("项目", "建设", "工程", "工地", "在建", "construction", "development", "project")
    return any(k.lower() in s for k in keywords)


def project_evidence_from_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in sources:
        name = src.get("source")
        for item in src.get("items", []):
            if project_signal(item):
                label = item.get("name") or item.get("address") or item.get("type")
                if label and label not in seen:
                    seen.add(label)
                    out.append({"label": label, "source": name, "evidence": item})
        for item in src.get("project_signals", []):
            label = item.get("name")
            if label and label not in seen:
                seen.add(label)
                out.append({"label": label, "source": name, "evidence": item})
    return out[:30]


def _compact_source(c: dict[str, Any], direct: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source": c.get("source"),
        "status": c.get("status"),
        "reason_code": c.get("reason_code"),
        "reason": c.get("reason"),
        "count": c.get("count"),
        "radius_m": c.get("radius_m"),
        "expanded_radius_m": c.get("expanded_radius_m"),
        "project_signal_count": len(c.get("project_signals", [])),
        "landuse": c.get("landuse", [])[:12],
        "buildings": c.get("buildings", {}),
        "amenities": c.get("amenities", [])[:12],
        "roads": c.get("roads", [])[:10],
        "places": c.get("places", [])[:10],
        "items": c.get("items", [])[:12],
        "project_evidence": [x for x in direct if x.get("source") == c.get("source")][:10],
    }


def assemble_feature_result(
    radius: float,
    sources: list[dict[str, Any]],
    *,
    expanded_radius_used: bool,
    expanded_radius_found_project: bool,
) -> dict[str, Any]:
    usable = [c for c in sources if c.get("status") == "ok"]
    source_names = [c.get("source") for c in usable]
    direct = project_evidence_from_sources(sources)
    return {
        "radius_m": radius,
        "expanded_radius_used": expanded_radius_used,
        "expanded_radius_found_project": expanded_radius_found_project,
        "data_source": schema_data_source(source_names),
        "project_evidence": direct,
        "sources": [_compact_source(c, direct) for c in sources],
    }


def _amap_baidu_for_jobs(
    jobs: list[tuple[int, float, float, float]],
    keywords: str | None,
    max_workers: int,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    amap_by: dict[int, dict[str, Any]] = {}
    baidu_by: dict[int, dict[str, Any]] = {}
    if not jobs:
        return amap_by, baidu_by
    workers = min(max(max_workers, 1), 4)

    def one(job: tuple[int, float, float, float]):
        idx, lat, lon, radius = job
        return idx, query_amap(lat, lon, radius, keywords), query_baidu(lat, lon, radius, keywords)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, job) for job in jobs]
        for fut in as_completed(futs):
            idx, amap, baidu = fut.result()
            amap_by[idx] = amap
            baidu_by[idx] = baidu
    return amap_by, baidu_by


def analyze_regions(
    geojson: dict[str, Any],
    search_projects: bool = True,
    search_poi: bool = True,
    expand_radius_if_needed: bool = True,
    max_workers: int = 8,
) -> dict[str, Any]:
    feats = feature_list(geojson)
    if len(feats) > MAX_FEATURES:
        raise ValueError(f"feature_count {len(feats)} exceeds limit {MAX_FEATURES}")
    stats = [geometry_stats(f, i) for i, f in enumerate(feats)]
    jobs: list[tuple[int, float, float, float]] = []
    for s in stats:
        if "centroid" not in s:
            continue
        radius = radius_from_stats(s)
        jobs.append((s["index"], s["centroid"]["lat"], s["centroid"]["lon"], radius))

    want_net = search_projects or search_poi
    keywords = PROJECT_KEYWORDS if search_projects else None
    amap_by: dict[int, dict[str, Any]] = {}
    baidu_by: dict[int, dict[str, Any]] = {}
    osm_by: dict[int, dict[str, Any]] = {}
    if want_net and jobs:
        amap_by, baidu_by = _amap_baidu_for_jobs(jobs, keywords, max_workers)
        osm_by = overpass_query_batch([(idx, lat, lon, radius) for idx, lat, lon, radius in jobs])
        for idx, lat, lon, _radius in jobs:
            amap_by[idx] = maybe_regeo_amap(amap_by[idx], lat, lon)
            baidu_by[idx] = maybe_regeo_baidu(baidu_by[idx], lat, lon)

    pending: dict[int, dict[str, Any]] = {}
    expand_jobs: list[tuple[int, float, float, float]] = []
    for idx, lat, lon, radius in jobs:
        sources = [s for s in (amap_by.get(idx), baidu_by.get(idx), osm_by.get(idx)) if s]
        direct = project_evidence_from_sources(sources) if search_projects else []
        need_expand = bool(expand_radius_if_needed and search_projects and not direct)
        pending[idx] = {"sources": sources, "need_expand": need_expand}
        if need_expand:
            expand_jobs.append((idx, lat, lon, min(radius * 2.5, 5000)))

    exp_amap: dict[int, dict[str, Any]] = {}
    exp_baidu: dict[int, dict[str, Any]] = {}
    exp_osm: dict[int, dict[str, Any]] = {}
    if expand_jobs:
        exp_amap, exp_baidu = _amap_baidu_for_jobs(expand_jobs, "项目|建设|工地|在建", max_workers)
        exp_osm = overpass_query_batch([(idx, lat, lon, r) for idx, lat, lon, r in expand_jobs])

    results: dict[int, dict[str, Any]] = {}
    for idx, _lat, _lon, radius in jobs:
        info = pending[idx]
        sources = info["sources"]
        expanded_used = bool(info["need_expand"])
        expanded_r = min(radius * 2.5, 5000) if expanded_used else None
        if expanded_used:
            merged = []
            by_name = {s.get("source"): s for s in sources}
            for name, getter in (("amap", exp_amap), ("baidu", exp_baidu), ("osm", exp_osm)):
                base = by_name.get(name)
                extra = getter.get(idx)
                if base is None and extra is None:
                    continue
                if base is None:
                    extra = dict(extra)
                    extra["radius_m"] = radius
                    extra["expanded_radius_m"] = expanded_r
                    merged.append(extra)
                else:
                    merged.append(merge_source_records(base, extra, radius, expanded_r))
            sources = merged
        else:
            tagged = []
            for s in sources:
                rec = dict(s)
                rec["radius_m"] = radius
                rec["expanded_radius_m"] = None
                tagged.append(rec)
            sources = tagged
        direct = project_evidence_from_sources(sources) if search_projects else []
        results[idx] = assemble_feature_result(
            radius,
            sources,
            expanded_radius_used=expanded_used,
            expanded_radius_found_project=bool(expanded_used and direct),
        )

    merged_out = []
    for s in stats:
        result = dict(s)
        result.update(
            results.get(
                s["index"],
                {
                    "radius_m": None,
                    "data_source": "offline",
                    "project_evidence": [],
                    "sources": [],
                    "expanded_radius_used": False,
                    "expanded_radius_found_project": False,
                },
            )
        )
        merged_out.append(result)
    return {"server": SERVER_NAME, "server_version": SERVER_VERSION, "feature_count": len(merged_out), "features": merged_out}


def validate_result(result: dict[str, Any]) -> dict[str, Any]:
    return validate_payload(result)


TOOLS = {
    "analyze_regions": {
        "description": "Batch-process a GeoJSON/FeatureCollection: geometry stats plus concurrent AMap/Baidu/OSM evidence. When search_projects is true, project-keyword search already covers the POI channel; search_poi does not add a second query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "geojson": {"type": "object"},
                "search_projects": {
                    "type": "boolean",
                    "default": True,
                    "description": "Project-keyword POI search. When true, search_poi adds no extra request.",
                },
                "search_poi": {
                    "type": "boolean",
                    "default": True,
                    "description": "General nearby search only when search_projects is false. Does not disable project search.",
                },
                "expand_radius_if_needed": {"type": "boolean", "default": True},
                "max_workers": {"type": "integer", "default": 8, "minimum": 1, "maximum": 8},
            },
            "required": ["geojson"],
        },
    },
    "calculate_geometry": {
        "description": "Compute deterministic geometry statistics for one Feature or a FeatureCollection without loading heavyweight GIS dependencies.",
        "inputSchema": {"type": "object", "properties": {"geojson": {"type": "object"}}, "required": ["geojson"]},
    },
    "search_project_evidence": {
        "description": "Search project-oriented evidence around one WGS84 point. AMap, Baidu, and OSM queries run concurrently; if direct project evidence is absent, the search can automatically expand once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "radius_m": {"type": "number", "default": 300},
                "expand_if_empty": {"type": "boolean", "default": True},
            },
            "required": ["lat", "lon"],
        },
    },
    "validate_result": {
        "description": "Validate one feature inference result against the Skill's output and project-confidence rules.",
        "inputSchema": {"type": "object", "properties": {"result": {"type": "object"}}, "required": ["result"]},
    },
}


def handle_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "analyze_regions":
        try:
            workers = int(args.get("max_workers", 8))
            workers = max(1, min(workers, 8))
            return ok(
                analyze_regions(
                    args["geojson"],
                    bool(args.get("search_projects", True)),
                    bool(args.get("search_poi", True)),
                    bool(args.get("expand_radius_if_needed", True)),
                    workers,
                )
            )
        except ValueError as e:
            return err(str(e))
    if name == "calculate_geometry":
        features = feature_list(args["geojson"])
        if len(features) > MAX_FEATURES:
            return err(f"feature_count {len(features)} exceeds limit {MAX_FEATURES}")
        return ok({"feature_count": len(features), "features": [geometry_stats(f, i) for i, f in enumerate(features)]})
    if name == "search_project_evidence":
        lat, lon, radius = float(args["lat"]), float(args["lon"]), float(args.get("radius_m", 300))
        amap = maybe_regeo_amap(query_amap(lat, lon, radius, PROJECT_KEYWORDS), lat, lon)
        baidu = maybe_regeo_baidu(query_baidu(lat, lon, radius, PROJECT_KEYWORDS), lat, lon)
        osm = overpass_query(lat, lon, radius)
        sources = [amap, baidu, osm]
        direct = project_evidence_from_sources(sources)
        expanded_r = None
        if not direct and bool(args.get("expand_if_empty", True)):
            expanded_r = min(radius * 2.5, 5000)
            sources = [
                merge_source_records(amap, query_amap(lat, lon, expanded_r, "项目|建设|工地|在建"), radius, expanded_r),
                merge_source_records(baidu, query_baidu(lat, lon, expanded_r, "项目|建设|工地|在建"), radius, expanded_r),
                merge_source_records(osm, overpass_query(lat, lon, expanded_r), radius, expanded_r),
            ]
            direct = project_evidence_from_sources(sources)
        return ok(
            {
                "center": {"lat": lat, "lon": lon},
                "initial_radius_m": radius,
                "expanded_search_used": expanded_r is not None,
                "expanded_radius_found_project": bool(expanded_r is not None and direct),
                "project_evidence": direct,
                "sources": [_compact_source(s, direct) for s in sources],
            }
        )
    if name == "validate_result":
        return ok(validate_result(args["result"]))
    return err(f"Unknown tool: {name}")


def capabilities() -> dict[str, Any]:
    return {"tools": {"listChanged": False}}


def response(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def error_response(req_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    out = {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    if data is not None:
        out["error"]["data"] = data
    return out


def negotiate_initialize(requested: str | None) -> str:
    if requested == LEGACY_PROTOCOL_VERSION:
        return LEGACY_PROTOCOL_VERSION
    return LEGACY_PROTOCOL_VERSION


def handle_rpc(req: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in req:
        return None
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "server/discover":
        result = {
            "supportedVersions": [PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION],
            "capabilities": capabilities(),
            "instructions": "Use analyze_regions for normal work: it batches geometry + project-oriented online evidence and returns compact evidence for semantic inference.",
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return response(req_id, result)
    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        negotiated = negotiate_initialize(str(requested) if requested else None)
        result = {
            "protocolVersion": negotiated,
            "capabilities": capabilities(),
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Use analyze_regions for normal work.",
        }
        return response(req_id, result)
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return response(
            req_id,
            {"tools": [{"name": n, "description": v["description"], "inputSchema": v["inputSchema"]} for n, v in TOOLS.items()]},
        )
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        return response(req_id, handle_tool(name, args))
    return error_response(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    log(f"started v{SERVER_VERSION}; stdio transport")
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except json.JSONDecodeError as e:
                print(json.dumps(error_response(None, -32700, "Parse error", str(e))), flush=True)
                continue
            try:
                out = handle_rpc(req)
            except Exception as e:
                log(f"error in {req.get('method')}: {e!r}")
                out = error_response(req.get("id"), -32000, "Tool/server error", str(e))
            if out is not None:
                print(json.dumps(out, ensure_ascii=False, separators=(",", ":")), flush=True)
    finally:
        close_http()


if __name__ == "__main__":
    main()
