"""GeoJSON load, CRS detection/reprojection, Esri mis-upload guard."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .geometry import rings_edges_cross

GEOJSON_TYPES = frozenset(
    {"FeatureCollection", "Feature", "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}
)
ESRI_EXPORT_HINT = (
    "检测到 ArcGIS Esri JSON 格式且无法自动转换为 GeoJSON。请在 ArcGIS 中选择「导出为 GeoJSON」并指定坐标系，"
    "然后使用 analyze_regions(input_path=导出的.geojson)。"
    "若含 curveRings/curvePaths，请先 Densify（密化）再导出。"
)
CRS_ASSUMED_MESSAGE = (
    "文件未声明坐标系，已假定 WGS84 (EPSG:4326)；若位置明显不对请重新导出并指定坐标系。"
)
GEOMETRY_SIMPLIFIED_MESSAGE = (
    "部分地物 Esri 几何经简化转换（如仅保留外环），面积/形状可能不准确；建议 ArcGIS 导出标准 GeoJSON。"
)
ESRI_PATHS_DROPPED_MESSAGE = (
    "部分地物同时含 Esri rings 与 paths：已按面（rings）处理，线路径（paths）已丢弃。"
)
GEOMETRY_SELF_INTERSECTING_MESSAGE = (
    "部分地物环自相交，面积无法可靠计算（area_m2 为空）；请修正几何后重跑。"
)
GEOMETRY_AUTO_CLOSED_MESSAGE = (
    "部分地物环未闭合，已自动补上首尾顶点；面积按闭合后计算。"
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
    simplified_reasons: dict[int, list[str]] = field(default_factory=dict)
    dropped_paths_indices: list[int] = field(default_factory=list)


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


validate_input_path = _validate_path


def validate_output_path(path: Path) -> Path:
    """Validate a write target. Does not require the file to exist yet."""
    resolved = path.expanduser().resolve()
    if resolved.suffix.lower() != ".json":
        raise ValueError(f"output_path must be .json, got {resolved.suffix!r}")
    parent = resolved.parent
    if not parent.is_dir():
        raise ValueError(f"output_path parent directory does not exist: {parent}")
    if resolved.exists() and not resolved.is_file():
        raise ValueError(f"output_path exists but is not a file: {path}")
    if _strict_paths():
        root = _input_root()
        try:
            resolved.relative_to(root)
        except ValueError as e:
            raise ValueError(
                f"output_path must be under GEO_INPUT_ROOT ({root}) when GEO_INPUT_STRICT=true"
            ) from e
    return resolved


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


def _has_curve_keys(geom: dict[str, Any] | None) -> bool:
    return isinstance(geom, dict) and ("curveRings" in geom or "curvePaths" in geom)


def _has_esri_geometry_keys(geom: dict[str, Any] | None) -> bool:
    if not isinstance(geom, dict):
        return False
    if "rings" in geom or "paths" in geom:
        return True
    if _has_curve_keys(geom):
        return True
    if "x" in geom and "y" in geom:
        return True
    return False


def _geometry_looks_esri(geom: dict[str, Any] | None) -> bool:
    if not _has_esri_geometry_keys(geom):
        return False
    if _has_valid_geojson_coordinates(geom):
        return False
    return True


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
    return False


# Ray-casting treats boundary hits as undefined. Esri hole rings sometimes share a
# vertex with the outer ring. Require a majority of vertices strictly inside so a
# shared vertex does not reject a real hole, while an edge-adjacent neighbour
# (≈0 interior vertices) is not treated as nested. Not a GIS standard constant.
_RING_INSIDE_MIN_INTERIOR_RATIO = 0.6


def _ring_abs_area(ring: list) -> float:
    s = 0.0
    n = len(ring)
    if n < 2:
        return 0.0
    for i in range(n - 1):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[i + 1][0]), float(ring[i + 1][1])
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _ring_bbox(ring: list) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in ring]
    ys = [float(p[1]) for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    """Axis-aligned containment. Reuse for later G1–G4; do not copy."""
    ominx, ominy, omaxx, omaxy = outer
    iminx, iminy, imaxx, imaxy = inner
    return iminx >= ominx and iminy >= ominy and imaxx <= omaxx and imaxy <= omaxy


def _ring_vertices(ring: list) -> list:
    if len(ring) > 1 and ring[0] == ring[-1]:
        return ring[:-1]
    return ring


def _point_in_ring(x: float, y: float, ring: list) -> bool:
    """Strict interior test (ray casting). Reuse for later G1–G4; do not copy."""
    pts = _ring_vertices(ring)
    n = len(pts)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(pts[i][0]), float(pts[i][1])
        xj, yj = float(pts[j][0]), float(pts[j][1])
        intersects = (yi > y) != (yj > y)
        if intersects:
            x_int = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_int:
                inside = not inside
        j = i
    return inside


def _ring_inside(inner: list, outer: list) -> bool:
    """True if `inner` is nested in `outer` by interior-vertex ratio.

    Reuse for later G1–G4 (curve rings, self-intersect, leftover paths); do not copy.
    """
    if not _bbox_contains(_ring_bbox(outer), _ring_bbox(inner)):
        return False
    pts = _ring_vertices(inner)
    if not pts:
        return False
    interior = sum(1 for p in pts if _point_in_ring(float(p[0]), float(p[1]), outer))
    return (interior / len(pts)) >= _RING_INSIDE_MIN_INTERIOR_RATIO


def _rings_cross(a: list, b: list) -> bool:
    """Partial overlap: bboxes meet, neither nest, some vertex lies inside the other.

    Reuse for later G1–G4; do not copy a second ray-cast. This round does not call
    these helpers to 'fix' G1–G4.
    """
    if _ring_inside(a, b) or _ring_inside(b, a):
        return False
    ab, bb = _ring_bbox(a), _ring_bbox(b)
    disjoint = ab[2] < bb[0] or bb[2] < ab[0] or ab[3] < bb[1] or bb[3] < ab[1]
    if disjoint:
        return False
    if rings_edges_cross(a, b):
        return True
    for p in _ring_vertices(a):
        if _point_in_ring(float(p[0]), float(p[1]), b):
            return True
    for p in _ring_vertices(b):
        if _point_in_ring(float(p[0]), float(p[1]), a):
            return True
    return False


def _resolve_esri_rings(rings: list) -> tuple[list[tuple[list, list]], bool]:
    """Nest Esri rings by containment (even-odd depth). Winding is ignored.

    Returns (parts, unresolved). Crossing rings → each ring is an independent part.
    """
    classified = [r for r in rings if r]
    if not classified:
        return [], True

    def _split_all() -> tuple[list[tuple[list, list]], bool]:
        return [(r, []) for r in classified], True

    n = len(classified)
    for i in range(n):
        for j in range(i + 1, n):
            if _rings_cross(classified[i], classified[j]):
                return _split_all()

    containers: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if _ring_inside(classified[i], classified[j]):
                containers[i].append(j)
    depth = [len(containers[i]) for i in range(n)]

    holes_of: dict[int, list] = {i: [] for i in range(n) if depth[i] % 2 == 0}
    for i in range(n):
        if depth[i] % 2 == 0:
            continue
        if not containers[i]:
            return _split_all()
        parent = min(containers[i], key=lambda j: (_ring_abs_area(classified[j]), j))
        if depth[parent] % 2 != 0 or parent not in holes_of:
            return _split_all()
        holes_of[parent].append(classified[i])

    parts = [(classified[i], holes_of[i]) for i in range(n) if depth[i] % 2 == 0]
    if not parts:
        return _split_all()
    return parts, False


def _convert_esri_geometry(geom: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(geom, dict):
        return None, []
    if _has_valid_geojson_coordinates(geom):
        return geom, []
    simplifications: list[str] = []
    if "rings" in geom:
        rings = geom.get("rings")
        if not isinstance(rings, list) or not rings:
            return None, []
        parts, unresolved = _resolve_esri_rings(rings)
        if not parts:
            return None, []
        if unresolved:
            simplifications.append("esri_ring_roles_unresolved")
        if len(parts) == 1:
            outer, holes = parts[0]
            return {"type": "Polygon", "coordinates": [outer, *holes]}, simplifications
        return {
            "type": "MultiPolygon",
            "coordinates": [[outer, *holes] for outer, holes in parts],
        }, simplifications
    if "paths" in geom:
        paths = geom.get("paths")
        if not isinstance(paths, list) or not paths:
            return None, []
        simplifications.append("paths_converted")
        if len(paths) == 1:
            return {"type": "LineString", "coordinates": paths[0]}, simplifications
        return {"type": "MultiLineString", "coordinates": paths}, simplifications
    if "x" in geom and "y" in geom:
        return {"type": "Point", "coordinates": [float(geom["x"]), float(geom["y"])]}, simplifications
    return None, []


def _esri_mixed_paths_dropped(geom: dict[str, Any]) -> bool:
    if "rings" not in geom:
        return False
    paths = geom.get("paths")
    return isinstance(paths, list) and len(paths) > 0


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
            meta.simplified_reasons.setdefault(0, []).extend(simplifications)
        if _esri_mixed_paths_dropped(payload):
            meta.dropped_paths_indices.append(0)
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
    actually_converted = False
    for idx, feat in enumerate(feats):
        if not isinstance(feat, dict):
            continue
        geom_raw = feat.get("geometry")
        if not isinstance(geom_raw, dict):
            continue
        geom, simplifications = _convert_esri_geometry(geom_raw)
        if geom is None:
            if _has_curve_keys(geom_raw):
                converted_features.append({
                    "type": "Feature",
                    "properties": _feature_properties(feat),
                    "geometry": geom_raw,
                })
            continue
        actually_converted = True
        if simplifications:
            meta.simplified_indices.append(idx)
            meta.simplified_reasons.setdefault(idx, []).extend(simplifications)
        if _esri_mixed_paths_dropped(geom_raw):
            meta.dropped_paths_indices.append(idx)
        converted_features.append({
            "type": "Feature",
            "properties": _feature_properties(feat),
            "geometry": geom,
        })

    if not converted_features or not actually_converted:
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
        alert: dict[str, Any] = {
            "code": "GEOMETRY_SIMPLIFIED",
            "severity": "warning",
            "feature_indices": list(esri_meta.simplified_indices),
            "user_message": GEOMETRY_SIMPLIFIED_MESSAGE,
        }
        if esri_meta.simplified_reasons:
            alert["simplify_reasons"] = {
                str(k): v for k, v in sorted(esri_meta.simplified_reasons.items())
            }
        alerts.append(alert)
    if esri_meta and esri_meta.dropped_paths_indices:
        alerts.append({
            "code": "ESRI_PATHS_DROPPED_MIXED_GEOMETRY",
            "severity": "warning",
            "feature_indices": list(esri_meta.dropped_paths_indices),
            "user_message": ESRI_PATHS_DROPPED_MESSAGE,
        })
    return alerts


def _geometry_has_residual_esri_keys(geom: dict[str, Any] | None) -> bool:
    if not _has_esri_geometry_keys(geom):
        return False
    if "rings" in geom or "paths" in geom:
        return True
    return not _has_valid_geojson_coordinates(geom)


def scan_residual_esri_geometry(features: list[dict[str, Any]]) -> dict[int, str]:
    """Post-normalize scan: geometry still contains unconverted Esri keys."""
    out: dict[int, str] = {}
    for idx, feat in enumerate(features):
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") if isinstance(feat.get("geometry"), dict) else None
        if _has_curve_keys(geom) and not _has_valid_geojson_coordinates(geom or {}):
            out[idx] = "unsupported_esri_curves"
        elif _geometry_has_residual_esri_keys(geom):
            out[idx] = "residual_esri_keys"
    return out


def merged_invalid_indices(
    stats: list[dict[str, Any]],
    structure_reasons: dict[int, str] | None = None,
    *,
    include_self_intersecting: bool = True,
) -> list[int]:
    reasons = structure_reasons or {}
    indices: set[int] = set(reasons.keys())
    for s in stats:
        if not include_self_intersecting and s.get("self_intersecting"):
            continue
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
        if s.get("self_intersecting"):
            continue
        if is_geometry_stat_invalid(s) and idx not in reasons:
            reasons[idx] = s.get("invalid_reason") or "geometry_stat_failed"
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
    curve_indices = sorted(i for i, r in invalid_reasons.items() if r == "unsupported_esri_curves")
    base = (
        f"{invalid_count}/{feature_count} 个地物几何无效或无法计算面积/质心，"
        "这些地物结果不完整；请检查 GeoJSON/Esri 格式。"
    )
    if esri_indices:
        idx_text = "、".join(str(i) for i in esri_indices)
        base += f" 地物 {idx_text} 含未转换的 Esri 几何键(rings/paths)；建议 ArcGIS 导出标准 GeoJSON。"
    if curve_indices:
        idx_text = "、".join(str(i) for i in curve_indices)
        base += f" 地物 {idx_text} 含 Esri 曲线(curveRings/curvePaths)，无法解析；请先 Densify（密化）再导出 GeoJSON。"
    return base


def build_geometry_invalid_alerts(
    stats: list[dict[str, Any]],
    feature_count: int,
    *,
    structure_reasons: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    invalid_indices = merged_invalid_indices(
        stats, structure_reasons, include_self_intersecting=False
    )
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


def build_self_intersecting_alerts(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indices = sorted(s["index"] for s in stats if s.get("self_intersecting"))
    if not indices:
        return []
    return [{
        "code": "GEOMETRY_SELF_INTERSECTING",
        "severity": "warning",
        "feature_indices": indices,
        "user_message": GEOMETRY_SELF_INTERSECTING_MESSAGE,
    }]


def build_auto_closed_alerts(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indices = sorted(s["index"] for s in stats if s.get("auto_closed") and not s.get("self_intersecting"))
    if not indices:
        return []
    return [{
        "code": "GEOMETRY_AUTO_CLOSED",
        "severity": "warning",
        "feature_indices": indices,
        "user_message": GEOMETRY_AUTO_CLOSED_MESSAGE,
    }]


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
    load_meta["esri_simplified_reasons"] = {
        str(k): list(v) for k, v in esri_meta.simplified_reasons.items()
    }
    load_meta["esri_dropped_paths_indices"] = list(esri_meta.dropped_paths_indices)
    return payload, load_meta


def normalize_geo_input(*, geojson: dict[str, Any] | None = None, input_path: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, load_meta = load_geo_input(geojson=geojson, input_path=input_path)
    raw_reasons = load_meta.get("esri_simplified_reasons") or {}
    simplified_reasons: dict[int, list[str]] = {
        int(k): list(v) for k, v in raw_reasons.items()
    }
    esri_meta = EsriConvertMeta(
        converted=bool(load_meta.get("esri_converted")),
        simplified_indices=list(load_meta.get("esri_simplified_indices") or []),
        simplified_reasons=simplified_reasons,
        dropped_paths_indices=[int(i) for i in (load_meta.get("esri_dropped_paths_indices") or [])],
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
