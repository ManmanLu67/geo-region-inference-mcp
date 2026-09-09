"""Batch geometry + online-evidence orchestration (no MCP protocol)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .clients import (
    EXPAND_RADIUS_FACTOR,
    EXPAND_RADIUS_MAX_M,
    PROJECT_KEYWORDS,
    apply_regeo_for_jobs,
    merge_source_records,
    overpass_query_batch,
    probe_api_status,
    run_amap_baidu_job_batches,
)
from .evidence import (
    _source_lacks_project_evidence,
    assemble_feature_result,
    project_evidence_from_sources,
    summarize_analyze_result,
    summarize_online_channels,
)
from .geometry import feature_list, geometry_stats, radius_from_stats
from .inputs import (
    build_geometry_invalid_alerts,
    normalize_geo_input,
    scan_residual_esri_geometry,
    validate_geometry_fail_fast,
    validate_output_path,
)
from .validation import validate_payload
from .version import SERVER_VERSION

SERVER_NAME = "geo-region-inference"
MAX_FEATURES = 80


def geometry_pipeline(feats: list[dict[str, Any]], input_alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shared stats + residual Esri scan + fail-fast + GEOMETRY_INVALID alerts."""
    if len(feats) > MAX_FEATURES:
        raise ValueError(f"feature_count {len(feats)} exceeds limit {MAX_FEATURES}")
    stats = [geometry_stats(f, i) for i, f in enumerate(feats)]
    structure_reasons = scan_residual_esri_geometry(feats)
    validate_geometry_fail_fast(stats, len(feats), structure_reasons=structure_reasons)
    input_alerts.extend(
        build_geometry_invalid_alerts(stats, len(feats), structure_reasons=structure_reasons)
    )
    return stats


def effective_max_workers(max_workers: int) -> int:
    return min(max(int(max_workers), 1), 4)


def analyze_regions(
    geojson: dict[str, Any] | None = None,
    *,
    input_path: str | None = None,
    search_projects: bool = True,
    search_poi: bool = True,
    expand_radius_if_needed: bool = True,
    max_workers: int = 4,
    output_path: str | None = None,
) -> dict[str, Any]:
    workers = effective_max_workers(max_workers)
    fc, input_meta = normalize_geo_input(geojson=geojson, input_path=input_path)
    input_alerts = list(input_meta.get("input_alerts") or [])
    feats = feature_list(fc)
    stats = geometry_pipeline(feats, input_alerts)
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
        amap_by, baidu_by = run_amap_baidu_job_batches(jobs, keywords, workers)
        osm_by = overpass_query_batch([(idx, lat, lon, radius) for idx, lat, lon, radius in jobs])
        regeo_cache: dict[tuple[str, float, float], list[dict[str, Any]]] = {}
        apply_regeo_for_jobs(jobs, amap_by, baidu_by, regeo_cache)

    pending: dict[int, dict[str, Any]] = {}
    expand_amap_jobs: list[tuple[int, float, float, float]] = []
    expand_baidu_jobs: list[tuple[int, float, float, float]] = []
    expand_osm_jobs: list[tuple[int, float, float, float]] = []
    for idx, lat, lon, radius in jobs:
        sources = [s for s in (amap_by.get(idx), baidu_by.get(idx), osm_by.get(idx)) if s]
        direct = project_evidence_from_sources(sources) if search_projects else []
        want_expand = bool(expand_radius_if_needed and search_projects and not direct)
        expanded_r = min(radius * EXPAND_RADIUS_FACTOR, EXPAND_RADIUS_MAX_M) if want_expand else None
        do_amap = bool(want_expand and _source_lacks_project_evidence(amap_by.get(idx), direct))
        do_baidu = bool(want_expand and _source_lacks_project_evidence(baidu_by.get(idx), direct))
        do_osm = bool(want_expand and _source_lacks_project_evidence(osm_by.get(idx), direct))
        need_expand = bool(do_amap or do_baidu or do_osm)
        pending[idx] = {"sources": sources, "need_expand": need_expand, "direct": direct}
        if need_expand and expanded_r is not None:
            job = (idx, lat, lon, expanded_r)
            if do_amap:
                expand_amap_jobs.append(job)
            if do_baidu:
                expand_baidu_jobs.append(job)
            if do_osm:
                expand_osm_jobs.append(job)

    exp_amap: dict[int, dict[str, Any]] = {}
    exp_baidu: dict[int, dict[str, Any]] = {}
    exp_osm: dict[int, dict[str, Any]] = {}
    if expand_amap_jobs or expand_baidu_jobs:
        exp_amap, exp_baidu = run_amap_baidu_job_batches(
            expand_amap_jobs or expand_baidu_jobs,
            PROJECT_KEYWORDS,
            workers,
            query_amap_jobs=expand_amap_jobs,
            query_baidu_jobs=expand_baidu_jobs,
        )
    if expand_osm_jobs:
        exp_osm = overpass_query_batch([(idx, lat, lon, r) for idx, lat, lon, r in expand_osm_jobs])

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
            direct = project_evidence_from_sources(sources)
        else:
            tagged = []
            for s in sources:
                rec = dict(s)
                rec["radius_m"] = radius
                rec["expanded_radius_m"] = None
                tagged.append(rec)
            sources = tagged
            direct = info["direct"] if search_projects else project_evidence_from_sources(sources)
        results[idx] = assemble_feature_result(
            radius,
            sources,
            expanded_radius_used=expanded_used,
            expanded_radius_found_project=bool(expanded_used and direct),
            project_evidence=direct,
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
        "effective_max_workers": workers,
        "input_meta": {k: v for k, v in input_meta.items() if k != "input_alerts"},
        "input_alerts": input_alerts,
        "features": merged_out,
    }
    if want_net:
        out["online_summary"] = summarize_online_channels(merged_out)
    if output_path:
        dest = validate_output_path(Path(output_path))
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return summarize_analyze_result(out, str(dest))
    return out


def calculate_geometry(
    geojson: dict[str, Any] | None = None,
    *,
    input_path: str | None = None,
) -> dict[str, Any]:
    fc, input_meta = normalize_geo_input(geojson=geojson, input_path=input_path)
    input_alerts = list(input_meta.get("input_alerts") or [])
    feats = feature_list(fc)
    stats = geometry_pipeline(feats, input_alerts)
    return {
        "feature_count": len(stats),
        "input_meta": {k: v for k, v in input_meta.items() if k != "input_alerts"},
        "input_alerts": input_alerts,
        "features": stats,
    }


def validate_result(result: dict[str, Any]) -> dict[str, Any]:
    return validate_payload(result)


def check_api_status(
    lat: float | None = None,
    lon: float | None = None,
    probe_mode: str = "single",
) -> dict[str, Any]:
    plat = float(lat if lat is not None else 39.9042)
    plon = float(lon if lon is not None else 116.4074)
    mode = probe_mode if probe_mode in ("single", "burst") else "single"
    return probe_api_status(plat, plon, probe_mode=mode)
