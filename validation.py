"""Shared output-schema validation used by the MCP tool and the CLI script."""

from __future__ import annotations

import re
from typing import Any

REQUIRED_TOP_KEYS = {
    "feature_index",
    "data_source",
    "region_type",
    "possible_buildings",
    "related_projects",
}
REQUIRED_CANDIDATE_KEYS = {"label", "confidence", "evidence"}
REQUIRED_PROJECT_KEYS = {"label", "confidence", "evidence", "evidence_type"}
VALID_DATA_SOURCES = {"amap", "baidu", "osm", "offline", "hybrid"}

VALID_EVIDENCE_TYPES = {
    "gov_publicity",
    "gov_publicity_weak",
    "poi_name",
    "attribute_field",
    "project_number",
    "inferred",
}
DIRECT_EVIDENCE_TYPES = {"gov_publicity", "poi_name", "attribute_field", "project_number"}
GOV_EVIDENCE_TYPES = {"gov_publicity", "gov_publicity_weak"}
URL_PATTERN = re.compile(r"^https?://", re.I)

CONFIDENCE_CAP = {
    "inferred": 0.4,
    "gov_publicity_weak": 0.3,
}


def schema_data_source(source_names: list[Any]) -> str:
    unique = sorted({str(n) for n in source_names if n})
    if not unique:
        return "offline"
    if len(unique) == 1 and unique[0] in VALID_DATA_SOURCES:
        return unique[0]
    return "hybrid"


def _valid_url(url: Any) -> bool:
    return isinstance(url, str) and bool(URL_PATTERN.match(url.strip()))


def _check_project_candidate(item: dict[str, Any], index: int, errors: list[str]) -> None:
    et = item.get("evidence_type")
    supported_by = item.get("supported_by")
    conf = item.get("confidence")
    source_url = item.get("source_url")

    if et not in VALID_EVIDENCE_TYPES:
        errors.append(
            f"related_projects[{index}].evidence_type must be one of {sorted(VALID_EVIDENCE_TYPES)}, got {et!r}"
        )
        return

    if not isinstance(conf, (int, float)):
        return

    cap = CONFIDENCE_CAP.get(str(et))
    if cap is not None and conf > cap + 1e-9:
        errors.append(f"related_projects[{index}].confidence cannot exceed {cap} for evidence_type={et!r}")

    if et in DIRECT_EVIDENCE_TYPES and conf > 0.6 + 1e-9:
        pass  # allowed
    elif conf > 0.6 + 1e-9:
        errors.append(
            f"related_projects[{index}].confidence > 0.6 requires a direct evidence_type "
            f"({sorted(DIRECT_EVIDENCE_TYPES)}), got {et!r}"
        )

    if et == "inferred":
        if not isinstance(supported_by, str) or not supported_by.strip():
            errors.append(f"related_projects[{index}] requires non-empty supported_by when evidence_type is inferred")

    if et == "gov_publicity":
        if not _valid_url(source_url):
            errors.append(
                f"related_projects[{index}] requires source_url (http/https) when evidence_type is gov_publicity"
            )

    if et == "gov_publicity_weak":
        if source_url is not None and source_url != "" and not _valid_url(source_url):
            errors.append(f"related_projects[{index}].source_url must be http/https when provided")

    if et not in DIRECT_EVIDENCE_TYPES and et != "gov_publicity_weak":
        if not isinstance(supported_by, str) or not supported_by.strip():
            errors.append(
                f"related_projects[{index}] requires non-empty supported_by when evidence_type is {et!r}"
            )


def _check_candidate_list(name: str, items: Any, errors: list[str]) -> None:
    if not isinstance(items, list):
        errors.append(f"{name} must be a list")
        return
    if len(items) == 0:
        errors.append(f"{name} must not be empty (use a low-confidence 'unknown' candidate instead of omitting)")
        return
    prev_conf = None
    required = REQUIRED_PROJECT_KEYS if name == "related_projects" else REQUIRED_CANDIDATE_KEYS
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"{name}[{i}] must be an object")
            continue
        missing = required - item.keys()
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
        if name == "related_projects" and isinstance(conf, (int, float)) and "evidence_type" in item:
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
