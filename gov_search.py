"""Government web search plan: admin/road extraction and multi-round query generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_TEMPLATES_PATH = ROOT / "references" / "gov_search_templates.json"

_BUILTIN_TEMPLATES: dict[str, Any] = {
    "rounds": [
        {
            "round": 1,
            "name": "street_core",
            "admin_level": "street",
            "templates": ["{admin} 在建项目", "{admin} 规划公示"],
        },
    ],
    "limits": {"max_search_per_feature": 24, "max_fetch_per_feature": 4, "stop_on_confirmed_match": True},
}


def load_gov_search_templates(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_TEMPLATES_PATH
    if p.is_file():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return json.loads(json.dumps(_BUILTIN_TEMPLATES))


def _place_value(places: list[dict[str, Any]], prefix: str) -> str:
    for p in places:
        tag = str(p.get("tag") or "")
        name = str(p.get("name") or "").strip()
        if name and tag.startswith(prefix):
            return name
    return ""


def _merge_places(feature: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for src in feature.get("sources") or []:
        if src.get("status") not in ("ok", "empty"):
            continue
        for p in src.get("places") or []:
            out.append(p)
    return out


def extract_admin_division(feature: dict[str, Any]) -> dict[str, Any]:
    places = _merge_places(feature)
    province = _place_value(places, "amap_province") or _place_value(places, "baidu_province")
    city = _place_value(places, "amap_city") or _place_value(places, "baidu_city")
    district = _place_value(places, "amap_district") or _place_value(places, "baidu_district")
    township = _place_value(places, "amap_township")

    parts_city = [x for x in (city, district) if x]
    district_label = "".join(parts_city) if parts_city else ""
    if not district_label and province and district:
        district_label = f"{province}{district}"

    street_parts = [x for x in (city, district, township) if x]
    street_label = "".join(street_parts) if street_parts else district_label

    formatted = _place_value(places, "amap_formatted_address") or _place_value(places, "baidu_formatted_address")

    return {
        "province": province or None,
        "city": city or None,
        "district": district or None,
        "township": township or None,
        "street_label": street_label or None,
        "district_label": district_label or None,
        "formatted_address": formatted or None,
        "has_district_level": bool(district_label),
    }


def extract_match_roads(feature: dict[str, Any], limit: int = 5) -> list[str]:
    seen: set[str] = set()
    roads: list[str] = []
    for src in feature.get("sources") or []:
        for r in src.get("roads") or []:
            if isinstance(r, dict):
                name = str(r.get("name") or "").strip()
            else:
                name = str(r).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            roads.append(name)
            if len(roads) >= limit:
                return roads
    return roads


def _fill_template(tpl: str, admin: str, road: str = "", road_a: str = "", road_b: str = "") -> str:
    return (
        tpl.replace("{admin}", admin)
        .replace("{road}", road)
        .replace("{road_a}", road_a)
        .replace("{road_b}", road_b)
        .strip()
    )


def _round_queries(
    round_cfg: dict[str, Any],
    admin: dict[str, Any],
    roads: list[str],
) -> tuple[list[str], str | None, str]:
    level = round_cfg.get("admin_level", "street")
    admin_used: str | None = None
    queries: list[str] = []

    if level == "street":
        admin_used = admin.get("street_label") or admin.get("district_label")
        if not admin_used:
            return [], None, level
        for tpl in round_cfg.get("templates") or []:
            q = _fill_template(str(tpl), admin_used)
            if q:
                queries.append(q)
    elif level == "district":
        admin_used = admin.get("district_label")
        if not admin_used:
            return [], None, level
        for tpl in round_cfg.get("templates") or []:
            q = _fill_template(str(tpl), admin_used)
            if q:
                queries.append(q)
        for tpl in round_cfg.get("road_templates") or []:
            for road in roads:
                q = _fill_template(str(tpl), admin_used, road=road)
                if q:
                    queries.append(q)
    elif level == "place_only":
        admin_used = None
        for tpl in round_cfg.get("road_templates") or []:
            for road in roads:
                q = _fill_template(str(tpl), "", road=road)
                if q:
                    queries.append(q)
        for tpl in round_cfg.get("intersection_templates") or []:
            if len(roads) >= 2:
                q = _fill_template(str(tpl), "", road_a=roads[0], road_b=roads[1])
                if q:
                    queries.append(q)
    return queries, admin_used, level


def build_search_plan(
    admin: dict[str, Any],
    roads: list[str],
    templates_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = templates_config or load_gov_search_templates()
    limits = dict(cfg.get("limits") or {})
    max_total = int(limits.get("max_search_per_feature", 24))
    rounds_out: list[dict[str, Any]] = []
    seen_global: set[str] = set()
    remaining = max_total

    for round_cfg in sorted(cfg.get("rounds") or [], key=lambda r: int(r.get("round", 0))):
        if round_cfg.get("enabled") is False:
            continue
        if remaining <= 0:
            break
        raw_queries, admin_used, level = _round_queries(round_cfg, admin, roads)
        if not raw_queries and level == "place_only" and not roads:
            continue
        round_queries: list[str] = []
        for q in raw_queries:
            if q in seen_global:
                continue
            if remaining <= 0:
                break
            seen_global.add(q)
            round_queries.append(q)
            remaining -= 1
        if not round_queries:
            continue
        rounds_out.append(
            {
                "round": int(round_cfg.get("round", len(rounds_out) + 1)),
                "name": round_cfg.get("name"),
                "admin_level": level,
                "admin_used": admin_used,
                "queries": round_queries,
            }
        )

    return {"rounds": rounds_out, "limits": limits}


def _feature_has_project_evidence(feature: dict[str, Any]) -> bool:
    pe = feature.get("project_evidence") or []
    return len(pe) > 0


def prepare_gov_web_search(analyze_result: dict[str, Any]) -> dict[str, Any]:
    features = analyze_result.get("features") or []
    templates = load_gov_search_templates()
    candidates: list[dict[str, Any]] = []
    skipped = {"has_project_evidence": 0, "no_admin": 0}

    for feat in features:
        if _feature_has_project_evidence(feat):
            skipped["has_project_evidence"] += 1
            continue
        admin = extract_admin_division(feat)
        if not admin.get("has_district_level"):
            skipped["no_admin"] += 1
            continue
        roads = extract_match_roads(feat)
        plan = build_search_plan(admin, roads, templates)
        if not any(r.get("queries") for r in plan.get("rounds") or []):
            skipped["no_admin"] += 1
            continue
        candidates.append(
            {
                "index": feat.get("index"),
                "admin": admin,
                "match_roads": roads,
                "search_plan": plan,
            }
        )

    return {
        "candidate_count": len(candidates),
        "skipped_summary": skipped,
        "candidates": candidates,
    }
