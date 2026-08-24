"""Shared output-schema validation used by the MCP tool and the CLI script."""

from __future__ import annotations

from typing import Any

REQUIRED_TOP_KEYS = {
    "feature_index",
    "data_source",
    "region_type",
    "possible_buildings",
    "related_projects",
}
REQUIRED_CANDIDATE_KEYS = {"label", "confidence", "evidence"}
VALID_DATA_SOURCES = {"amap", "baidu", "osm", "offline", "hybrid"}

# Phrases that mention project-name markers while stating they were NOT found.
NEGATED_DIRECT_PHRASES = (
    "未发现直接项目名称",
    "未发现直接项目名",
    "未发现直接项目",
    "没有直接项目名称",
    "未见直接项目",
    "无直接项目名称",
    "无直接项目线索",
    "未发现直接项目线索",
)

DIRECT_MARKERS = (
    "项目名称",
    "项目名",
    "project_name",
    "project_no",
    "project_id",
    "project_code",
    "plan_id",
    "plan_no",
    "planning_id",
    "permit_no",
    "permit_id",
    "备案",
    "规划",
    "公示",
    "POI",
    "OSM name",
    "construction",
    "在建",
    "建设中",
    "工地",
    "工程名称",
)


def schema_data_source(source_names: list[Any]) -> str:
    unique = sorted({str(n) for n in source_names if n})
    if not unique:
        return "offline"
    if len(unique) == 1 and unique[0] in VALID_DATA_SOURCES:
        return unique[0]
    return "hybrid"


def _check_project_candidate(item: dict[str, Any], index: int, errors: list[str]) -> None:
    evidence = str(item.get("evidence", ""))
    supported_by = item.get("supported_by")
    conf = item.get("confidence")
    scanned = evidence
    for phrase in NEGATED_DIRECT_PHRASES:
        scanned = scanned.replace(phrase, "")
    has_direct = any(m in scanned for m in DIRECT_MARKERS)

    if conf > 0.6 and not has_direct:
        errors.append(
            f"related_projects[{index}].confidence > 0.6 requires direct project evidence "
            "(project name/number, planning/filing/public notice, or explicit construction-status naming)"
        )
    if not has_direct and conf > 0.4:
        errors.append(
            f"related_projects[{index}].confidence cannot exceed 0.4 without direct project evidence"
        )
    if not has_direct and (not isinstance(supported_by, str) or not supported_by.strip()):
        errors.append(
            f"related_projects[{index}] requires non-empty supported_by when evidence is indirect"
        )


def _check_candidate_list(name: str, items: Any, errors: list[str]) -> None:
    if not isinstance(items, list):
        errors.append(f"{name} must be a list")
        return
    if len(items) == 0:
        errors.append(f"{name} must not be empty (use a low-confidence 'unknown' candidate instead of omitting)")
        return
    prev_conf = None
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"{name}[{i}] must be an object")
            continue
        missing = REQUIRED_CANDIDATE_KEYS - item.keys()
        if missing:
            errors.append(f"{name}[{i}] missing keys: {sorted(missing)}")
            continue
        conf = item["confidence"]
        if not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
            errors.append(f"{name}[{i}].confidence must be a number in [0,1], got {conf!r}")
        if not isinstance(item["evidence"], str) or not item["evidence"].strip():
            errors.append(f"{name}[{i}].evidence must be a non-empty string")
        if not isinstance(item["label"], str) or not item["label"].strip():
            errors.append(f"{name}[{i}].label must be a non-empty string")
        if prev_conf is not None and isinstance(conf, (int, float)) and conf > prev_conf + 1e-9:
            errors.append(f"{name} not sorted descending by confidence at index {i} ({conf} > {prev_conf})")
        if isinstance(conf, (int, float)):
            prev_conf = conf
        if name == "related_projects" and isinstance(conf, (int, float)):
            _check_project_candidate(item, i, errors)


def collect_errors(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return ["result must be a JSON object"]
    errors: list[str] = []
    missing = REQUIRED_TOP_KEYS - result.keys()
    if missing:
        errors.append(f"missing top-level keys: {sorted(missing)}")
    if "data_source" in result and result["data_source"] not in VALID_DATA_SOURCES:
        errors.append(
            f"data_source must be one of {sorted(VALID_DATA_SOURCES)}, got {result.get('data_source')!r}"
        )
    for key in ("region_type", "possible_buildings", "related_projects"):
        if key in result:
            _check_candidate_list(key, result[key], errors)
    return errors


def validate_payload(result: Any) -> dict[str, Any]:
    errors = collect_errors(result)
    return {"valid": not errors, "errors": errors}
