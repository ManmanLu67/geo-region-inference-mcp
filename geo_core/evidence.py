"""Project evidence extraction, source compaction, and online-channel summaries."""

from __future__ import annotations

from typing import Any

from .clients import (
    EXPAND_RADIUS_FACTOR,
    EXPAND_RADIUS_MAX_M,
    LAST_RETRY_AFTER_MS,
    PROJECT_KEYWORDS,
    RATE_LIMIT,
    RATE_LIMIT_BATCH_RATIO,
    maybe_regeo_amap,
    maybe_regeo_baidu,
    merge_source_records,
    overpass_query,
    project_signal,
    query_amap,
    query_baidu,
)
from .validation import schema_data_source

_CHANNEL_RANK = {"ok": 4, "empty": 3, "error": 2, "unavailable": 1}


def _source_lacks_project_evidence(src: dict[str, Any] | None, direct: list[dict[str, Any]]) -> bool:
    if not src or src.get("status") == "unavailable":
        return False
    name = src.get("source")
    return not any(x.get("source") == name for x in direct)


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
                    rec = {"label": label, "source": name, "evidence": item}
                    if item.get("page_url"):
                        rec["page_url"] = item["page_url"]
                    out.append(rec)
        for item in src.get("project_signals", []):
            label = item.get("name")
            if label and label not in seen:
                seen.add(label)
                rec = {"label": label, "source": name, "evidence": item}
                if item.get("page_url"):
                    rec["page_url"] = item["page_url"]
                out.append(rec)
    return out[:30]


def compact_source(c: dict[str, Any], direct: list[dict[str, Any]]) -> dict[str, Any]:
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
    feature_count = len(features)
    rate_limit: dict[str, dict[str, Any]] = {}
    for name in ("amap", "baidu", "osm"):
        best: dict[str, Any] | None = None
        best_rank = 0
        rl_count = 0
        for feat in features:
            for src in feat.get("sources") or []:
                if src.get("source") != name:
                    continue
                if src.get("reason_code") == RATE_LIMIT:
                    rl_count += 1
                status = str(src.get("status") or "unavailable")
                rank = _CHANNEL_RANK.get(status, 0)
                if rank > best_rank:
                    best_rank = rank
                    best = src
        ratio = (rl_count / feature_count) if feature_count else 0.0
        rate_limit[name] = {
            "feature_count": rl_count,
            "feature_ratio": round(ratio, 4),
            "retry_after_hint_ms": LAST_RETRY_AFTER_MS.get(name, 0),
        }
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
        rl = rate_limit.get(name, {})
        if rl.get("feature_count", 0) > 0:
            label = {"amap": "高德", "baidu": "百度", "osm": "OSM"}.get(name, name)
            warnings.append(f"{label}: {rl['feature_count']} 个地物遭遇限流（已自动退避重试）")
    usable = sum(1 for c in channels.values() if c.get("status") in ("ok", "empty"))
    all_failed = usable == 0
    user_message = None
    if all_failed:
        user_message = (
            "所有在线数据源均不可用，结果仅为离线几何统计；请配置 AMAP_KEY / BAIDU_AK 或检查 Overpass 连通性。"
        )
    batch_retry_recommended = False
    batch_retry_reason = None
    for name, rl in rate_limit.items():
        if rl.get("feature_ratio", 0) >= RATE_LIMIT_BATCH_RATIO:
            batch_retry_recommended = True
            batch_retry_reason = f"{name} rate_limit ratio {rl['feature_ratio']} >= threshold {RATE_LIMIT_BATCH_RATIO}"
            break
    return {
        "channels": channels,
        "rate_limit": rate_limit,
        "batch_retry_recommended": batch_retry_recommended,
        "batch_retry_reason": batch_retry_reason,
        "all_channels_unavailable": all_failed,
        "warnings": warnings,
        "user_message": user_message,
    }


def slim_project_evidence(direct: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for x in direct:
        rec: dict[str, Any] = {"label": x.get("label"), "source": x.get("source")}
        if x.get("page_url"):
            rec["page_url"] = x["page_url"]
        out.append(rec)
    return out


def slim_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source": s.get("source"),
            "status": s.get("status"),
            "reason_code": s.get("reason_code"),
            "count": s.get("count"),
            "project_signal_count": s.get("project_signal_count"),
            "expanded_radius_m": s.get("expanded_radius_m"),
        }
        for s in sources
    ]


def summarize_analyze_result(full: dict[str, Any], output_path: str) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    for feat in full.get("features") or []:
        row = {k: v for k, v in feat.items() if k not in ("sources", "project_evidence")}
        row["project_evidence"] = slim_project_evidence(feat.get("project_evidence") or [])
        row["sources"] = slim_sources(feat.get("sources") or [])
        features.append(row)
    summary: dict[str, Any] = {
        "server": full.get("server"),
        "server_version": full.get("server_version"),
        "output_path": output_path,
        "output_written": True,
        "feature_count": full.get("feature_count"),
        "effective_max_workers": full.get("effective_max_workers"),
        "input_meta": full.get("input_meta"),
        "input_alerts": full.get("input_alerts"),
        "features": features,
    }
    if "online_summary" in full:
        summary["online_summary"] = full["online_summary"]
    return summary


def assemble_feature_result(
    radius: float,
    sources: list[dict[str, Any]],
    *,
    expanded_radius_used: bool,
    expanded_radius_found_project: bool,
    project_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    usable = [c for c in sources if c.get("status") == "ok"]
    source_names = [c.get("source") for c in usable]
    direct = project_evidence if project_evidence is not None else project_evidence_from_sources(sources)
    return {
        "radius_m": radius,
        "expanded_radius_used": expanded_radius_used,
        "expanded_radius_found_project": expanded_radius_found_project,
        "data_source": schema_data_source(source_names),
        "project_evidence": direct,
        "sources": [compact_source(c, direct) for c in sources],
    }


def search_project_evidence(
    lat: float,
    lon: float,
    radius_m: float = 300,
    *,
    expand_if_empty: bool = True,
) -> dict[str, Any]:
    radius = float(radius_m)
    amap = maybe_regeo_amap(query_amap(lat, lon, radius, PROJECT_KEYWORDS), lat, lon)
    baidu = maybe_regeo_baidu(query_baidu(lat, lon, radius, PROJECT_KEYWORDS), lat, lon)
    osm = overpass_query(lat, lon, radius)
    sources = [amap, baidu, osm]
    direct = project_evidence_from_sources(sources)
    expanded_r = None
    if not direct and expand_if_empty:
        expanded_cand = min(radius * EXPAND_RADIUS_FACTOR, EXPAND_RADIUS_MAX_M)
        extra_amap = None if amap.get("status") == "unavailable" else query_amap(lat, lon, expanded_cand, PROJECT_KEYWORDS)
        extra_baidu = None if baidu.get("status") == "unavailable" else query_baidu(lat, lon, expanded_cand, PROJECT_KEYWORDS)
        extra_osm = None if osm.get("status") == "unavailable" else overpass_query(lat, lon, expanded_cand)
        if extra_amap is not None or extra_baidu is not None or extra_osm is not None:
            expanded_r = expanded_cand
            sources = [
                merge_source_records(amap, extra_amap, radius, expanded_r),
                merge_source_records(baidu, extra_baidu, radius, expanded_r),
                merge_source_records(osm, extra_osm, radius, expanded_r),
            ]
            direct = project_evidence_from_sources(sources)
    return {
        "center": {"lat": lat, "lon": lon},
        "initial_radius_m": radius,
        "expanded_search_used": expanded_r is not None,
        "expanded_radius_found_project": bool(expanded_r is not None and direct),
        "project_evidence": direct,
        "sources": [compact_source(s, direct) for s in sources],
    }
