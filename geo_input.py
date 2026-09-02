"""GeoJSON load, CRS detection/reprojection, Esri mis-upload guard."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GEOJSON_TYPES = frozenset(
    {"FeatureCollection", "Feature", "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}
)
ESRI_EXPORT_HINT = (
    "检测到 ArcGIS Esri JSON 格式且无法自动转换为 GeoJSON。请在 ArcGIS 中选择「导出为 GeoJSON」并指定坐标系，"
    "然后使用 analyze_regions(input_path=导出的.geojson)。"
)
CRS_ASSUMED_MESSAGE = (
    "文件未声明坐标系，已假定 WGS84 (EPSG:4326)；若位置明显不对请重新导出并指定坐标系。"
)
GEOMETRY_SIMPLIFIED_MESSAGE = (
    "部分地物 Esri 几何经简化转换（如仅保留外环），面积/形状可能不准确；建议 ArcGIS 导出标准 GeoJSON。"
)
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
TARGET_EPSG = 4326
DEFAULT_GEOMETRY_FAIL_RATIO = 0.5


@dataclass
class CRSInfo:
    epsg: int | None
    wkid: int | None
    assumed: bool = False
    raw: str | None = None


@dataclass
class EsriConvertMeta:
    converted: bool = False
    simplified_indices: list[int] = field(default_factory=list)


def geometry_fail_ratio() -> float:
    raw = os.environ.get("GEOMETRY_FAIL_RATIO", "").strip()
    if raw:
        return float(raw)
    return DEFAULT_GEOMETRY_FAIL_RATIO


def _max_bytes() -> int:
    raw = os.environ.get("GEO_INPUT_MAX_BYTES", "")
    if raw.strip():
        return int(raw)
    return DEFAULT_MAX_BYTES


def _strict_paths() -> bool:
    return os.environ.get("GEO_INPUT_STRICT", "").strip().lower() in ("1", "true", "yes", "on")


def _input_root() -> Path:
    raw = os.environ.get("GEO_INPUT_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home().resolve()


def _validate_path(path: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"input_path not found or not a file: {path}")
    suffix = resolved.suffix.lower()
    if suffix not in (".json", ".geojson"):
        raise ValueError(f"input_path must be .json or .geojson, got {suffix!r}")
    size = resolved.stat().st_size
    limit = _max_bytes()
    if size > limit:
        raise ValueError(
            f"input file size {size} exceeds GEO_INPUT_MAX_BYTES limit ({limit}); "
            "raise the env var or reduce the dataset."
        )
    if _strict_paths():
        root = _input_root()
        try:
            resolved.relative_to(root)
        except ValueError as e:
            raise ValueError(f"input_path must be under GEO_INPUT_ROOT ({root}) when GEO_INPUT_STRICT=true") from e


def _has_valid_geojson_coordinates(geom: dict[str, Any]) -> bool:
    coords = geom.get("coordinates")
    if coords is None:
        return False
    flat: list[tuple[float, float]] = []

    def walk(c: Any) -> None:
        if not c:
            return
        if isinstance(c[0], (int, float)):
            flat.append((float(c[0]), float(c[1])))
            return
        for part in c:
            walk(part)

    walk(coords)
    return len(flat) > 0


def _geometry_looks_esri(geom: dict[str, Any] | None) -> bool:
    if not isinstance(geom, dict):
        return False
    if _has_valid_geojson_coordinates(geom):
        return False
    if "rings" in geom or "paths" in geom:
        return True
    if "x" in geom and "y" in geom:
        return True
    return False


def _looks_like_esri(payload: dict[str, Any]) -> bool:
    gtype = payload.get("type")
    if payload.get("geometryType") and gtype not in GEOJSON_TYPES:
        return True
    if "rings" in payload and "attributes" in payload:
        return True
    sr = payload.get("spatialReference")
    if isinstance(sr, dict) and sr.get("wkid") is not None and gtype not in GEOJSON_TYPES:
        return True
    feats = payload.get("features")
    if isinstance(feats, list):
        for feat in feats:
            if not isinstance(feat, dict):
                continue
            geom = feat.get("geometry")
            if isinstance(geom, dict) and _geometry_looks_esri(geom):
                return True
    if gtype == "FeatureCollection" and isinstance(feats, list):
        for feat in feats:
            if isinstance(feat, dict) and _geometry_looks_esri(feat.get("geometry")):
                return True
    return False


def _convert_esri_geometry(geom: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(geom, dict):
        return None, []
    if _has_valid_geojson_coordinates(geom):
        return geom, []
    simplifications: list[str] = []
    if "rings" in geom:
        rings = geom.get("rings") or []
        if not rings:
            return None, []
        if len(rings) > 1:
            simplifications.append("dropped_inner_rings")
        outer = rings[0]
        if not outer:
            return None, []
        return {"type": "Polygon", "coordinates": [outer]}, simplifications
    if "paths" in geom:
        paths = geom.get("paths") or []
        if not paths:
            return None, []
        simplifications.append("paths_converted")
        if len(paths) == 1:
            return {"type": "LineString", "coordinates": paths[0]}, simplifications
        return {"type": "MultiLineString", "coordinates": paths}, simplifications
    if "x" in geom and "y" in geom:
        return {"type": "Point", "coordinates": [float(geom["x"]), float(geom["y"])]}, simplifications
    return None, []


def _feature_properties(feat: dict[str, Any]) -> dict[str, Any]:
    if "properties" in feat and isinstance(feat.get("properties"), dict):
        return dict(feat["properties"])
    attrs = feat.get("attributes")
    if isinstance(attrs, dict):
        return dict(attrs)
    return {}


def try_convert_esri_to_geojson(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, EsriConvertMeta]:
    meta = EsriConvertMeta()
    if not _looks_like_esri(payload):
        return None, meta

    out: dict[str, Any] = {}
    if payload.get("type") in GEOJSON_TYPES:
        out["type"] = payload["type"]
    else:
        out["type"] = "FeatureCollection"

    if "spatialReference" in payload:
        sr = payload["spatialReference"]
        if isinstance(sr, dict) and sr.get("wkid") is not None:
            out["crs"] = {"type": "name", "properties": {"name": f"EPSG:{int(sr['wkid'])}"}}

    if "rings" in payload and "attributes" in payload:
        geom, simplifications = _convert_esri_geometry(payload)
        if geom is None:
            return None, meta
        if simplifications:
            meta.simplified_indices.append(0)
        meta.converted = True
        return {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": dict(payload.get("attributes") or {}),
                "geometry": geom,
            }],
            **({"crs": out["crs"]} if "crs" in out else {}),
        }, meta

    feats = payload.get("features")
    if not isinstance(feats, list):
        return None, meta

    converted_features: list[dict[str, Any]] = []
    for idx, feat in enumerate(feats):
        if not isinstance(feat, dict):
            continue
        geom_raw = feat.get("geometry")
        if not isinstance(geom_raw, dict):
            continue
        geom, simplifications = _convert_esri_geometry(geom_raw)
        if geom is None:
            continue
        if simplifications:
            meta.simplified_indices.append(idx)
        converted_features.append({
            "type": "Feature",
            "properties": _feature_properties(feat),
            "geometry": geom,
        })

    if not converted_features:
        return None, meta

    meta.converted = True
    result: dict[str, Any] = {"type": "FeatureCollection", "features": converted_features}
    if "crs" in out:
        result["crs"] = out["crs"]
    elif isinstance(payload.get("crs"), dict):
        result["crs"] = payload["crs"]
    return result, meta


def _parse_epsg_from_name(name: str) -> int | None:
    if not name:
        return None
    m = re.search(r"EPSG(?::|::)(\d+)", name, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"^(\d+)$", name.strip())
    if m:
        return int(m.group(1))
    return None


def _crs_from_object(obj: dict[str, Any] | None) -> CRSInfo | None:
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "name":
        name = (obj.get("properties") or {}).get("name", "")
        epsg = _parse_epsg_from_name(str(name))
        if epsg:
            return CRSInfo(epsg=epsg, wkid=epsg, raw=str(name))
    if obj.get("type") == "EPSG":
        code = (obj.get("properties") or {}).get("code")
        if code is not None:
            epsg = int(code)
            return CRSInfo(epsg=epsg, wkid=epsg, raw=f"EPSG:{epsg}")
    wkid = obj.get("wkid") or obj.get("latestWkid")
    if wkid is not None:
        epsg = int(wkid)
        return CRSInfo(epsg=epsg, wkid=epsg, raw=f"wkid:{epsg}")
    return None


def extract_crs_info(payload: dict[str, Any], fc: dict[str, Any]) -> CRSInfo:
    for source in (payload, fc):
        if not isinstance(source, dict):
            continue
        if "crs" in source:
            info = _crs_from_object(source.get("crs"))
            if info:
                return info
        if "spatialReference" in source:
            info = _crs_from_object(source.get("spatialReference"))
            if info:
                return info
    return CRSInfo(epsg=None, wkid=None, assumed=True)


def _flatten_coord_values(coords: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []

    def walk(c: Any) -> None:
        if not c:
            return
        if isinstance(c[0], (int, float)):
            out.append((float(c[0]), float(c[1])))
            return
        for part in c:
            walk(part)

    walk(coords)
    return out


def _coords_look_projected(fc: dict[str, Any]) -> bool:
    for feat in fc.get("features") or []:
        geom = feat.get("geometry") or {}
        for x, y in _flatten_coord_values(geom.get("coordinates")):
            if abs(x) > 180 or abs(y) > 90:
                return True
    return False


def to_feature_collection(payload: dict[str, Any]) -> dict[str, Any]:
    t = payload.get("type")
    if t == "FeatureCollection":
        return payload
    if t == "Feature":
        return {"type": "FeatureCollection", "features": [payload]}
    if t in GEOJSON_TYPES - {"FeatureCollection", "Feature"}:
        return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": payload, "properties": {}}]}
    raise ValueError("JSON is not a valid GeoJSON FeatureCollection, Feature, or geometry object")


def _strip_z_in_coords(coords: Any) -> tuple[Any, bool]:
    stripped = False

    def walk(c: Any) -> Any:
        nonlocal stripped
        if not c:
            return c
        if isinstance(c[0], (int, float)):
            if len(c) > 2:
                stripped = True
                return [float(c[0]), float(c[1])]
            return c
        return [walk(part) for part in c]

    return walk(coords), stripped


def strip_z_to_2d(fc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    out = json.loads(json.dumps(fc))
    any_stripped = False
    for feat in out.get("features") or []:
        geom = feat.get("geometry")
        if not geom or "coordinates" not in geom:
            continue
        geom["coordinates"], stripped = _strip_z_in_coords(geom["coordinates"])
        any_stripped = any_stripped or stripped
    return out, any_stripped


def _transform_coords(coords: Any, transformer) -> Any:
    if not coords:
        return coords
    if isinstance(coords[0], (int, float)):
        x, y = float(coords[0]), float(coords[1])
        lon, lat = transformer.transform(x, y)
        return [lon, lat]
    return [_transform_coords(part, transformer) for part in coords]


def reproject_to_wgs84(fc: dict[str, Any], crs_info: CRSInfo) -> tuple[dict[str, Any], dict[str, Any]]:
    from pyproj import CRS, Transformer

    meta: dict[str, Any] = {
        "source_epsg": crs_info.epsg,
        "source_wkid": crs_info.wkid,
        "target_epsg": TARGET_EPSG,
        "reprojected": False,
        "crs_assumed": crs_info.assumed,
    }
    if crs_info.assumed:
        return fc, meta
    if crs_info.epsg is None:
        raise ValueError("Unable to determine coordinate reference system; export GeoJSON with CRS from ArcGIS.")
    if crs_info.epsg == TARGET_EPSG:
        return fc, meta
    try:
        src = CRS.from_epsg(crs_info.epsg)
        dst = CRS.from_epsg(TARGET_EPSG)
    except Exception as e:
        raise ValueError(f"Unsupported CRS EPSG:{crs_info.epsg}; re-export as WGS84 GeoJSON from ArcGIS.") from e
    if src == dst:
        return fc, meta
    transformer = Transformer.from_crs(src, dst, always_xy=True)
    out = json.loads(json.dumps(fc))
    for feat in out.get("features") or []:
        geom = feat.get("geometry")
        if geom and "coordinates" in geom:
            geom["coordinates"] = _transform_coords(geom["coordinates"], transformer)
    meta["reprojected"] = True
    return out, meta


def build_input_alerts(crs_meta: dict[str, Any], *, esri_meta: EsriConvertMeta | None = None) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if crs_meta.get("crs_assumed"):
        alerts.append({"code": "CRS_ASSUMED", "severity": "warning", "user_message": CRS_ASSUMED_MESSAGE})
    if esri_meta and esri_meta.simplified_indices:
        alerts.append({
            "code": "GEOMETRY_SIMPLIFIED",
            "severity": "warning",
            "feature_indices": list(esri_meta.simplified_indices),
            "user_message": GEOMETRY_SIMPLIFIED_MESSAGE,
        })
    return alerts


def _geometry_has_residual_esri_keys(geom: dict[str, Any] | None) -> bool:
    if not isinstance(geom, dict):
        return False
    if "rings" in geom or "paths" in geom:
        return True
    if "x" in geom and "y" in geom and not _has_valid_geojson_coordinates(geom):
        return True
    return False


def scan_residual_esri_geometry(features: list[dict[str, Any]]) -> dict[int, str]:
    """Post-normalize scan: geometry still contains unconverted Esri keys."""
    out: dict[int, str] = {}
    for idx, feat in enumerate(features):
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if _geometry_has_residual_esri_keys(geom if isinstance(geom, dict) else None):
            out[idx] = "residual_esri_keys"
    return out


def merged_invalid_indices(
    stats: list[dict[str, Any]],
    structure_reasons: dict[int, str] | None = None,
) -> list[int]:
    reasons = structure_reasons or {}
    indices: set[int] = set(reasons.keys())
    for s in stats:
        if is_geometry_stat_invalid(s):
            indices.add(s["index"])
    return sorted(indices)


def merged_invalid_reasons(
    stats: list[dict[str, Any]],
    structure_reasons: dict[int, str] | None = None,
) -> dict[int, str]:
    reasons: dict[int, str] = dict(structure_reasons or {})
    for s in stats:
        idx = s["index"]
        if is_geometry_stat_invalid(s) and idx not in reasons:
            reasons[idx] = "geometry_stat_failed"
    return reasons


def is_geometry_stat_invalid(stat: dict[str, Any]) -> bool:
    if stat.get("error"):
        return True
    if "centroid" not in stat:
        return True
    gt = stat.get("geometry_type")
    if gt in ("Polygon", "MultiPolygon") and stat.get("area_m2") is None:
        return True
    return False


def _geometry_invalid_user_message(
    invalid_count: int,
    feature_count: int,
    invalid_reasons: dict[int, str],
) -> str:
    esri_indices = sorted(i for i, r in invalid_reasons.items() if r == "residual_esri_keys")
    base = (
        f"{invalid_count}/{feature_count} 个地物几何无效或无法计算面积/质心，"
        "这些地物结果不完整；请检查 GeoJSON/Esri 格式。"
    )
    if esri_indices:
        idx_text = "、".join(str(i) for i in esri_indices)
        base += f" 地物 {idx_text} 含未转换的 Esri 几何键(rings/paths)；建议 ArcGIS 导出标准 GeoJSON。"
    return base


def build_geometry_invalid_alerts(
    stats: list[dict[str, Any]],
    feature_count: int,
    *,
    structure_reasons: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    invalid_indices = merged_invalid_indices(stats, structure_reasons)
    if not invalid_indices:
        return []
    invalid_reasons = merged_invalid_reasons(stats, structure_reasons)
    invalid_count = len(invalid_indices)
    severity = "error" if invalid_count >= feature_count else "warning"
    alert: dict[str, Any] = {
        "code": "GEOMETRY_INVALID",
        "severity": severity,
        "invalid_count": invalid_count,
        "invalid_indices": invalid_indices,
        "user_message": _geometry_invalid_user_message(invalid_count, feature_count, invalid_reasons),
    }
    if invalid_reasons:
        alert["invalid_reasons"] = {str(k): v for k, v in sorted(invalid_reasons.items())}
    return [alert]


def validate_geometry_fail_fast(
    stats: list[dict[str, Any]],
    feature_count: int,
    *,
    structure_reasons: dict[int, str] | None = None,
) -> None:
    if feature_count <= 0:
        return
    invalid_count = len(merged_invalid_indices(stats, structure_reasons))
    if invalid_count <= 0:
        return
    ratio = invalid_count / feature_count
    if ratio >= geometry_fail_ratio():
        raise ValueError(
            f"{invalid_count}/{feature_count} features have invalid geometry "
            f"(ratio {ratio:.2f} >= GEOMETRY_FAIL_RATIO {geometry_fail_ratio()}). "
            "Check Esri/GeoJSON format and re-export from ArcGIS."
        )


def load_geo_input(*, geojson: dict[str, Any] | None = None, input_path: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    if (geojson is None) == (input_path is None):
        raise ValueError("Provide exactly one of geojson or input_path")
    load_meta: dict[str, Any] = {"source_path": None, "byte_size": None, "format_hint": "geojson"}
    esri_meta = EsriConvertMeta()
    if input_path is not None:
        path = Path(input_path)
        _validate_path(path)
        raw_text = path.read_text(encoding="utf-8")
        load_meta["source_path"] = str(path.resolve())
        load_meta["byte_size"] = len(raw_text.encode("utf-8"))
        payload = json.loads(raw_text)
    else:
        payload = geojson
        load_meta["byte_size"] = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Geo input must be a JSON object")
    if _looks_like_esri(payload):
        converted, esri_meta = try_convert_esri_to_geojson(payload)
        if converted is None:
            raise ValueError(ESRI_EXPORT_HINT)
        payload = converted
        load_meta["format_hint"] = "esri_converted"
        load_meta["esri_converted"] = True
    load_meta["esri_simplified_indices"] = list(esri_meta.simplified_indices)
    return payload, load_meta


def normalize_geo_input(*, geojson: dict[str, Any] | None = None, input_path: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, load_meta = load_geo_input(geojson=geojson, input_path=input_path)
    esri_meta = EsriConvertMeta(
        converted=bool(load_meta.get("esri_converted")),
        simplified_indices=list(load_meta.get("esri_simplified_indices") or []),
    )
    fc = to_feature_collection(payload)
    crs_info = extract_crs_info(payload, fc)
    if crs_info.assumed and _coords_look_projected(fc):
        raise ValueError(
            "Coordinates look projected (|x|>180 or |y|>90) but no CRS was declared. "
            "Re-export GeoJSON with coordinate system from ArcGIS."
        )
    fc, crs_meta = reproject_to_wgs84(fc, crs_info)
    fc, z_stripped = strip_z_to_2d(fc)
    input_meta = {**load_meta, "crs": crs_meta, "z_stripped": z_stripped}
    input_meta["input_alerts"] = build_input_alerts(crs_meta, esri_meta=esri_meta)
    return fc, input_meta
