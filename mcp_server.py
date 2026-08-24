#!/usr/bin/env python3
"""
geo-region-inference MCP server.

Design goals:
- Keep GIS/API work out of the Skill's LLM orchestration loop.
- Keep one long-lived Python process alive per MCP connection.
- Batch features and run independent network lookups concurrently.
- Return compact, project-oriented evidence instead of raw API payloads.
- Standard-library implementation of the MCP stdio JSON-RPC surface so the
  bundle does not require a heavy Python GIS stack. It supports the modern
  2026-07-28 discovery flow and the legacy initialize flow used by older hosts.

Environment:
  AMAP_KEY, BAIDU_AK, OVERPASS_URL, HTTP_TIMEOUT_SECONDS

No GeoPandas/GDAL/Fiona/Shapely dependency is required.
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from validation import schema_data_source, validate_payload

SERVER_NAME = "geo-region-inference"
SERVER_VERSION = "2.0.1"
MAX_FEATURES = 80
BAIDU_DEFAULT_QUERY = "公司|住宅|写字楼|生活服务"
PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "12"))
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")


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


def ring_area_perimeter(ring: list[list[float]], projected: bool) -> tuple[float, float]:
    if len(ring) < 3:
        return 0.0, 0.0
    if projected:
        pts = [(float(p[0]), float(p[1])) for p in ring]
    else:
        mean_lat = sum(float(p[1]) for p in ring) / len(ring)
        mx, my = deg_to_m_factors(mean_lat)
        pts = [(float(p[0]) * mx, float(p[1]) * my) for p in ring]
    area2 = 0.0
    perim = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        area2 += x1 * y2 - x2 * y1
        perim += math.hypot(x2 - x1, y2 - y1)
    return abs(area2) / 2.0, perim


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
    if rings:
        area_sum = perim_sum = 0.0
        for ring in rings:
            a, p = ring_area_perimeter(ring, projected)
            area_sum += a
            perim_sum += p
        area, perim = round(area_sum, 1), round(perim_sum, 1)
        if perim_sum > 0:
            compact = round((4 * math.pi * area_sum) / (perim_sum**2), 3)
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


def request_json(url: str, *, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None, timeout: float = HTTP_TIMEOUT) -> Any:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lon, lat
    pi = math.pi
    a, ee = 6378245.0, 0.00669342162296594323
    x, y = lon - 105.0, lat - 35.0
    def tl(x, y):
        r = -100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x))
        r += (20*math.sin(6*x*pi)+20*math.sin(2*x*pi))*2/3
        r += (20*math.sin(y*pi)+40*math.sin(y*pi/3))*2/3
        r += (160*math.sin(y*pi/12)+320*math.sin(y*pi/30))*2/3
        return r
    def tlo(x, y):
        r = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x))
        r += (20*math.sin(6*x*pi)+20*math.sin(2*x*pi))*2/3
        r += (20*math.sin(x*pi)+40*math.sin(x*pi/3))*2/3
        r += (150*math.sin(x*pi/12)+300*math.sin(x*pi/30))*2/3
        return r
    dlat = tl(x, y); dlon = tlo(x, y)
    rad = lat / 180 * pi; magic = math.sin(rad); magic = 1 - ee * magic * magic; sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180) / ((a*(1-ee))/(magic*sqrt_magic)*pi)
    dlon = (dlon * 180) / (a/sqrt_magic*math.cos(rad)*pi)
    return lon + dlon, lat + dlat


def gcj02_to_bd09(lon: float, lat: float) -> tuple[float, float]:
    x_pi = math.pi * 3000.0 / 180.0
    z = math.sqrt(lon*lon + lat*lat) + 0.00002 * math.sin(lat*x_pi)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon*x_pi)
    return z*math.cos(theta)+0.0065, z*math.sin(theta)+0.006


def wgs84_to_bd09(lon: float, lat: float) -> tuple[float, float]:
    return gcj02_to_bd09(*wgs84_to_gcj02(lon, lat))


def _compact_poi(item: dict[str, Any], source: str) -> dict[str, Any]:
    if source == "amap":
        return {"name": item.get("name"), "type": item.get("type"), "address": item.get("address"), "businessarea": item.get("businessarea"), "location": item.get("location")}
    if source == "baidu":
        return {"name": item.get("name"), "type": item.get("tag") or item.get("type"), "address": item.get("address"), "uid": item.get("uid"), "location": item.get("location")}
    return item


def query_amap(lat: float, lon: float, radius: float, keywords: str | None = None) -> dict[str, Any]:
    key = os.environ.get("AMAP_KEY")
    if not key:
        return {"source": "amap", "status": "unavailable", "reason": "AMAP_KEY not configured", "items": []}
    gcj_lon, gcj_lat = wgs84_to_gcj02(lon, lat)
    params = {"key": key, "location": f"{gcj_lon},{gcj_lat}", "radius": str(int(radius)), "extensions": "all", "output": "JSON", "offset": "50", "page": "1"}
    if keywords:
        params["keywords"] = keywords
    url = "https://restapi.amap.com/v3/place/around?" + urllib.parse.urlencode(params)
    try:
        raw = request_json(url)
        pois = raw.get("pois") or []
        items = [_compact_poi(p, "amap") for p in pois]
        return {"source": "amap", "status": "ok" if items else "empty", "count": len(items), "items": items[:30]}
    except Exception as e:
        return {"source": "amap", "status": "error", "reason": str(e), "items": []}


def query_baidu(lat: float, lon: float, radius: float, keywords: str | None = None) -> dict[str, Any]:
    ak = os.environ.get("BAIDU_AK")
    if not ak:
        return {"source": "baidu", "status": "unavailable", "reason": "BAIDU_AK not configured", "items": []}
    bd_lon, bd_lat = wgs84_to_bd09(lon, lat)
    params = {"ak": ak, "location": f"{bd_lat},{bd_lon}", "radius": str(int(radius)), "output": "json", "page_size": "50", "page_num": "0", "query": keywords or BAIDU_DEFAULT_QUERY}
    url = "https://api.map.baidu.com/place/v2/search?" + urllib.parse.urlencode(params)
    try:
        raw = request_json(url)
        results = raw.get("results") or []
        items = [_compact_poi(p, "baidu") for p in results]
        return {"source": "baidu", "status": "ok" if items else "empty", "count": len(items), "items": items[:30]}
    except Exception as e:
        return {"source": "baidu", "status": "error", "reason": str(e), "items": []}


def overpass_query(lat: float, lon: float, radius: float) -> dict[str, Any]:
    query = f"""[out:json][timeout:20];(way(around:{radius},{lat},{lon})[\"landuse\"];relation(around:{radius},{lat},{lon})[\"landuse\"];way(around:{radius},{lat},{lon})[\"building\"];node(around:{radius},{lat},{lon})[\"amenity\"];node(around:{radius},{lat},{lon})[\"shop\"];node(around:{radius},{lat},{lon})[\"office\"];way(around:{radius},{lat},{lon})[\"highway\"][\"name\"];node(around:{radius},{lat},{lon})[\"place\"];way(around:{radius},{lat},{lon})[\"place\"]);out center tags;"""
    body = ("data=" + urllib.parse.quote(query)).encode("utf-8")
    try:
        raw = request_json(OVERPASS_URL, method="POST", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=max(20.0, HTTP_TIMEOUT))
        landuse, amenities, roads, places = [], [], [], []
        building_types: dict[str, int] = {}
        project_signals: list[dict[str, Any]] = []
        for el in raw.get("elements", []):
            tags = el.get("tags") or {}
            name = tags.get("name")
            if "landuse" in tags:
                landuse.append({"tag": tags["landuse"], "name": name})
            if "building" in tags:
                b = tags["building"]; building_types[b] = building_types.get(b, 0) + 1
            if "amenity" in tags:
                amenities.append({"tag": f"amenity={tags['amenity']}", "name": name})
            if "shop" in tags:
                amenities.append({"tag": f"shop={tags['shop']}", "name": name})
            if "office" in tags:
                amenities.append({"tag": f"office={tags['office']}", "name": name})
            if "highway" in tags and name:
                roads.append({"name": name, "highway": tags["highway"]})
            if "place" in tags:
                places.append({"tag": f"place={tags['place']}", "name": name})
            if tags.get("construction") or (name and any(w in name for w in ("在建", "项目", "建设", "工程", "工地"))):
                project_signals.append({"name": name, "construction": tags.get("construction"), "description": tags.get("description")})
        return {"source": "osm", "status": "ok" if (landuse or building_types or amenities or project_signals) else "empty", "landuse": landuse[:30], "buildings": {"count": sum(building_types.values()), "by_type": building_types}, "amenities": amenities[:30], "roads": roads[:20], "places": places[:10], "project_signals": project_signals[:30]}
    except Exception as e:
        return {"source": "osm", "status": "error", "reason": str(e), "landuse": [], "buildings": {"count": 0, "by_type": {}}, "amenities": [], "roads": [], "places": [], "project_signals": []}


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


def analyze_regions(geojson: dict[str, Any], search_projects: bool = True, search_poi: bool = True, expand_radius_if_needed: bool = True, max_workers: int = 8) -> dict[str, Any]:
    feats = feature_list(geojson)
    if len(feats) > MAX_FEATURES:
        raise ValueError(f"feature_count {len(feats)} exceeds limit {MAX_FEATURES}")
    stats = [geometry_stats(f, i) for i, f in enumerate(feats)]
    jobs = []
    for s in stats:
        if "centroid" not in s:
            continue
        radius = radius_from_stats(s)
        jobs.append((s["index"], s["centroid"]["lat"], s["centroid"]["lon"], radius))

    def fetch(idx: int, lat: float, lon: float, radius: float):
        calls = []
        expanded_used = False
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            if search_projects or search_poi:
                futures[pool.submit(query_amap, lat, lon, radius, "在建|项目|工地|建设" if search_projects else None)] = "amap_project" if search_projects else "amap"
                futures[pool.submit(query_baidu, lat, lon, radius, "在建|项目|工地|建设" if search_projects else None)] = "baidu_project" if search_projects else "baidu"
                futures[pool.submit(overpass_query, lat, lon, radius)] = "osm"
            for fut in as_completed(futures):
                calls.append(fut.result())
        direct = project_evidence_from_sources(calls) if search_projects else []
        if expand_radius_if_needed and search_projects and not direct:
            expanded = min(radius * 2.5, 5000)
            expanded_used = True
            with ThreadPoolExecutor(max_workers=3) as pool:
                fs = [
                    pool.submit(query_amap, lat, lon, expanded, "项目|建设|工地|在建"),
                    pool.submit(query_baidu, lat, lon, expanded, "项目|建设|工地|在建"),
                    pool.submit(overpass_query, lat, lon, expanded),
                ]
                expanded_sources = [f.result() for f in as_completed(fs)]
            direct = project_evidence_from_sources(expanded_sources)
            if direct:
                calls.extend(expanded_sources)
        usable = [c for c in calls if c.get("status") == "ok"]
        source_names = [c.get("source") for c in usable]
        compact_sources = []
        for c in calls:
            compact_sources.append({"source": c.get("source"), "status": c.get("status"), "count": c.get("count"), "reason": c.get("reason"), "project_signal_count": len(c.get("project_signals", [])), "landuse": c.get("landuse", [])[:12], "buildings": c.get("buildings", {}), "amenities": c.get("amenities", [])[:12], "roads": c.get("roads", [])[:10], "project_evidence": [x for x in direct if x.get("source") == c.get("source")][:10]})
        return idx, {"radius_m": radius, "expanded_radius_used": expanded_used, "data_source": schema_data_source(source_names), "project_evidence": direct, "sources": compact_sources}

    results: dict[int, dict[str, Any]] = {}
    if jobs:
        # One bounded pool across features keeps total in-flight API calls predictable.
        # Each feature task internally uses at most 3 source calls at once.
        with ThreadPoolExecutor(max_workers=min(max_workers, 4)) as pool:
            futures = [pool.submit(fetch, *job) for job in jobs]
            for fut in as_completed(futures):
                idx, result = fut.result()
                results[idx] = result
    merged = []
    for s in stats:
        result = dict(s)
        result.update(results.get(s["index"], {"radius_m": None, "data_source": "offline", "project_evidence": [], "sources": []}))
        merged.append(result)
    return {"server": SERVER_NAME, "server_version": SERVER_VERSION, "feature_count": len(merged), "features": merged}


def validate_result(result: dict[str, Any]) -> dict[str, Any]:
    return validate_payload(result)


TOOLS = {
    "analyze_regions": {
        "description": "Batch-process a GeoJSON/FeatureCollection: compute deterministic geometry statistics, query project-oriented POI/OSM evidence in parallel, expand the search radius only when direct project evidence is missing, and return compact evidence for LLM semantic inference.",
        "inputSchema": {"type": "object", "properties": {"geojson": {"type": "object"}, "search_projects": {"type": "boolean", "default": True}, "search_poi": {"type": "boolean", "default": True}, "expand_radius_if_needed": {"type": "boolean", "default": True}, "max_workers": {"type": "integer", "default": 8, "minimum": 1, "maximum": 8}}, "required": ["geojson"]},
    },
    "calculate_geometry": {
        "description": "Compute deterministic geometry statistics for one Feature or a FeatureCollection without loading heavyweight GIS dependencies.",
        "inputSchema": {"type": "object", "properties": {"geojson": {"type": "object"}}, "required": ["geojson"]},
    },
    "search_project_evidence": {
        "description": "Search project-oriented evidence around one WGS84 point. AMap, Baidu, and OSM queries run concurrently; if direct project evidence is absent, the search can automatically expand once.",
        "inputSchema": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}, "radius_m": {"type": "number", "default": 300}, "expand_if_empty": {"type": "boolean", "default": True}}, "required": ["lat", "lon"]},
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
            return ok(analyze_regions(args["geojson"], bool(args.get("search_projects", True)), bool(args.get("search_poi", True)), bool(args.get("expand_radius_if_needed", True)), workers))
        except ValueError as e:
            return err(str(e))
    if name == "calculate_geometry":
        features = feature_list(args["geojson"])
        return ok({"feature_count": len(features), "features": [geometry_stats(f, i) for i, f in enumerate(features)]})
    if name == "search_project_evidence":
        lat, lon, radius = float(args["lat"]), float(args["lon"]), float(args.get("radius_m", 300))
        sources = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            fs = [pool.submit(query_amap, lat, lon, radius, "在建|项目|工地|建设"), pool.submit(query_baidu, lat, lon, radius, "在建|项目|工地|建设"), pool.submit(overpass_query, lat, lon, radius)]
            for f in as_completed(fs):
                sources.append(f.result())
        direct = project_evidence_from_sources(sources)
        expanded_sources = []
        if not direct and bool(args.get("expand_if_empty", True)):
            expanded = min(radius * 2.5, 5000)
            with ThreadPoolExecutor(max_workers=3) as pool:
                fs = [pool.submit(query_amap, lat, lon, expanded, "项目|建设|工地|在建"), pool.submit(query_baidu, lat, lon, expanded, "项目|建设|工地|在建"), pool.submit(overpass_query, lat, lon, expanded)]
                expanded_sources = [f.result() for f in as_completed(fs)]
            direct = project_evidence_from_sources(expanded_sources)
        return ok({"center": {"lat": lat, "lon": lon}, "initial_radius_m": radius, "expanded_search_used": bool(expanded_sources), "project_evidence": direct, "sources": [{"source": s.get("source"), "status": s.get("status"), "count": s.get("count"), "reason": s.get("reason"), "items": s.get("items", [])[:12], "project_signals": s.get("project_signals", [])[:12], "landuse": s.get("landuse", [])[:12], "buildings": s.get("buildings", {})} for s in sources + expanded_sources]})
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


def main() -> None:
    log(f"started v{SERVER_VERSION}; stdio transport")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            print(json.dumps(error_response(None, -32700, "Parse error", str(e))), flush=True)
            continue
        if "id" not in req:
            continue
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        try:
            if method == "server/discover":
                result = {"supportedVersions": [PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION], "capabilities": capabilities(), "instructions": "Use analyze_regions for normal work: it batches geometry + project-oriented online evidence and returns compact evidence for semantic inference.", "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
                out = response(req_id, result)
            elif method == "initialize":
                result = {"protocolVersion": LEGACY_PROTOCOL_VERSION, "capabilities": capabilities(), "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}, "instructions": "Use analyze_regions for normal work."}
                out = response(req_id, result)
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                out = response(req_id, {"tools": [{"name": n, "description": v["description"], "inputSchema": v["inputSchema"]} for n, v in TOOLS.items()]})
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                out = response(req_id, handle_tool(name, args))
            else:
                out = error_response(req_id, -32601, f"Method not found: {method}")
        except Exception as e:
            log(f"error in {method}: {e!r}")
            out = error_response(req_id, -32000, "Tool/server error", str(e))
        print(json.dumps(out, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
