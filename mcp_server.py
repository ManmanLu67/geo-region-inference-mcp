#!/usr/bin/env python3
"""
geo-region-inference MCP server.

GIS/API work stays in geo_clients.py (sync httpx singleton). This module is
geometry, tool orchestration, and the stdio JSON-RPC surface.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from geo_clients import (
    EXPAND_RADIUS_FACTOR,
    EXPAND_RADIUS_MAX_M,
    PROJECT_KEYWORDS,
    close_http,
    maybe_regeo_amap,
    maybe_regeo_baidu,
    merge_source_records,
    overpass_query,
    overpass_query_batch,
    query_amap,
    query_baidu,
)
from geo_geometry import feature_list, geometry_stats, radius_from_stats
from geo_input import normalize_geo_input
from validation import schema_data_source, validate_payload

SERVER_NAME = "geo-region-inference"
SERVER_VERSION = "2.3.0"

_CHANNEL_RANK = {"ok": 4, "empty": 3, "error": 2, "unavailable": 1}
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


def summarize_online_channels(features: list[dict[str, Any]]) -> dict[str, Any]:
    channels: dict[str, dict[str, Any]] = {}
    for name in ("amap", "baidu", "osm"):
        best: dict[str, Any] | None = None
        best_rank = 0
        for feat in features:
            for src in feat.get("sources") or []:
                if src.get("source") != name:
                    continue
                status = str(src.get("status") or "unavailable")
                rank = _CHANNEL_RANK.get(status, 0)
                if rank > best_rank:
                    best_rank = rank
                    best = src
        if best:
            channels[name] = {
                "status": best.get("status"),
                "reason_code": best.get("reason_code"),
                "reason": best.get("reason"),
            }
        else:
            channels[name] = {"status": "unavailable", "reason_code": None, "reason": "no query attempted"}
    warnings: list[str] = []
    for name, info in channels.items():
        status = info.get("status")
        reason = info.get("reason") or info.get("reason_code") or status
        if status in ("unavailable", "error"):
            label = {"amap": "高德", "baidu": "百度", "osm": "OSM"}.get(name, name)
            warnings.append(f"{label}: {reason}")
    usable = sum(1 for c in channels.values() if c.get("status") in ("ok", "empty"))
    all_failed = usable == 0
    user_message = None
    if all_failed:
        user_message = (
            "所有在线数据源均不可用，结果仅为离线几何统计；请配置 AMAP_KEY / BAIDU_AK 或检查 Overpass 连通性。"
        )
    return {
        "channels": channels,
        "all_channels_unavailable": all_failed,
        "warnings": warnings,
        "user_message": user_message,
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
    geojson: dict[str, Any] | None = None,
    *,
    input_path: str | None = None,
    search_projects: bool = True,
    search_poi: bool = True,
    expand_radius_if_needed: bool = True,
    max_workers: int = 8,
) -> dict[str, Any]:
    fc, input_meta = normalize_geo_input(geojson=geojson, input_path=input_path)
    input_alerts = list(input_meta.get("input_alerts") or [])
    feats = feature_list(fc)
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
            expand_jobs.append((idx, lat, lon, min(radius * EXPAND_RADIUS_FACTOR, EXPAND_RADIUS_MAX_M)))

    exp_amap: dict[int, dict[str, Any]] = {}
    exp_baidu: dict[int, dict[str, Any]] = {}
    exp_osm: dict[int, dict[str, Any]] = {}
    if expand_jobs:
        exp_amap, exp_baidu = _amap_baidu_for_jobs(expand_jobs, PROJECT_KEYWORDS, max_workers)
        exp_osm = overpass_query_batch([(idx, lat, lon, r) for idx, lat, lon, r in expand_jobs])

    results: dict[int, dict[str, Any]] = {}
    for idx, _lat, _lon, radius in jobs:
        info = pending[idx]
        sources = info["sources"]
        expanded_used = bool(info["need_expand"])
        expanded_r = min(radius * EXPAND_RADIUS_FACTOR, EXPAND_RADIUS_MAX_M) if expanded_used else None
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
    out: dict[str, Any] = {
        "server": SERVER_NAME,
        "server_version": SERVER_VERSION,
        "feature_count": len(merged_out),
        "input_meta": {k: v for k, v in input_meta.items() if k != "input_alerts"},
        "input_alerts": input_alerts,
        "features": merged_out,
    }
    if want_net:
        out["online_summary"] = summarize_online_channels(merged_out)
    return out


def validate_result(result: dict[str, Any]) -> dict[str, Any]:
    return validate_payload(result)


TOOLS = {
    "analyze_regions": {
        "description": "Batch-process a GeoJSON/FeatureCollection from inline geojson or input_path: geometry stats plus concurrent AMap/Baidu/OSM evidence. When search_projects is true, project-keyword search already covers the POI channel; search_poi does not add a second query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "geojson": {"type": "object", "description": "Inline GeoJSON FeatureCollection, Feature, or geometry."},
                "input_path": {
                    "type": "string",
                    "description": "Local .json/.geojson file path (preferred for large datasets). Mutually exclusive with geojson.",
                },
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
        },
    },
    "calculate_geometry": {
        "description": "Compute deterministic geometry statistics for one Feature or a FeatureCollection without loading heavyweight GIS dependencies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "geojson": {"type": "object"},
                "input_path": {"type": "string", "description": "Local .json/.geojson file path. Mutually exclusive with geojson."},
            },
        },
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


def _resolve_geo_args(args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    geojson = args.get("geojson")
    input_path = args.get("input_path")
    if (geojson is None) == (input_path is None):
        raise ValueError("Provide exactly one of geojson or input_path")
    return normalize_geo_input(geojson=geojson, input_path=input_path)


def handle_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "analyze_regions":
        try:
            workers = int(args.get("max_workers", 8))
            workers = max(1, min(workers, 8))
            geojson = args.get("geojson")
            input_path = args.get("input_path")
            return ok(
                analyze_regions(
                    geojson,
                    input_path=input_path,
                    search_projects=bool(args.get("search_projects", True)),
                    search_poi=bool(args.get("search_poi", True)),
                    expand_radius_if_needed=bool(args.get("expand_radius_if_needed", True)),
                    max_workers=workers,
                )
            )
        except ValueError as e:
            return err(str(e))
    if name == "calculate_geometry":
        try:
            fc, input_meta = _resolve_geo_args(args)
            features = feature_list(fc)
            if len(features) > MAX_FEATURES:
                return err(f"feature_count {len(features)} exceeds limit {MAX_FEATURES}")
            return ok(
                {
                    "feature_count": len(features),
                    "input_meta": {k: v for k, v in input_meta.items() if k != "input_alerts"},
                    "input_alerts": list(input_meta.get("input_alerts") or []),
                    "features": [geometry_stats(f, i) for i, f in enumerate(features)],
                }
            )
        except ValueError as e:
            return err(str(e))
    if name == "search_project_evidence":
        lat, lon, radius = float(args["lat"]), float(args["lon"]), float(args.get("radius_m", 300))
        amap = maybe_regeo_amap(query_amap(lat, lon, radius, PROJECT_KEYWORDS), lat, lon)
        baidu = maybe_regeo_baidu(query_baidu(lat, lon, radius, PROJECT_KEYWORDS), lat, lon)
        osm = overpass_query(lat, lon, radius)
        sources = [amap, baidu, osm]
        direct = project_evidence_from_sources(sources)
        expanded_r = None
        if not direct and bool(args.get("expand_if_empty", True)):
            expanded_r = min(radius * EXPAND_RADIUS_FACTOR, EXPAND_RADIUS_MAX_M)
            sources = [
                merge_source_records(amap, query_amap(lat, lon, expanded_r, PROJECT_KEYWORDS), radius, expanded_r),
                merge_source_records(baidu, query_baidu(lat, lon, expanded_r, PROJECT_KEYWORDS), radius, expanded_r),
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
