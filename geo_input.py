"""GeoJSON load, CRS detection/reprojection, Esri mis-upload guard."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GEOJSON_TYPES = frozenset(
    {"FeatureCollection", "Feature", "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}
)
ESRI_EXPORT_HINT = (
    "检测到 ArcGIS Esri JSON 格式。请在 ArcGIS 中选择「导出为 GeoJSON」并指定坐标系，"
    "然后使用 analyze_regions(input_path=导出的.geojson)。"
)
CRS_ASSUMED_MESSAGE = (
    "文件未声明坐标系，已假定 WGS84 (EPSG:4326)；若位置明显不对请重新导出并指定坐标系。"
)
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
TARGET_EPSG = 4326


@dataclass
class CRSInfo:
    epsg: int | None
    wkid: int | None
    assumed: bool = False
    raw: str | None = None


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


def _looks_like_esri(payload: dict[str, Any]) -> bool:
    gtype = payload.get("type")
    if gtype in GEOJSON_TYPES:
        return False
    if payload.get("geometryType") and gtype not in GEOJSON_TYPES:
        return True
    if "rings" in payload and "attributes" in payload:
        return True
    feats = payload.get("features")
    if isinstance(feats, list) and feats:
        first = feats[0]
        if isinstance(first, dict):
            geom = first.get("geometry")
            if first.get("attributes") and isinstance(geom, dict):
                if "rings" in geom or "paths" in geom:
                    return True
    sr = payload.get("spatialReference")
    if isinstance(sr, dict) and sr.get("wkid") is not None and gtype not in GEOJSON_TYPES:
        return True
    return False


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


def build_input_alerts(crs_meta: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if crs_meta.get("crs_assumed"):
        alerts.append({"code": "CRS_ASSUMED", "severity": "warning", "user_message": CRS_ASSUMED_MESSAGE})
    return alerts


def load_geo_input(*, geojson: dict[str, Any] | None = None, input_path: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    if (geojson is None) == (input_path is None):
        raise ValueError("Provide exactly one of geojson or input_path")
    load_meta: dict[str, Any] = {"source_path": None, "byte_size": None, "format_hint": "geojson"}
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
        raise ValueError(ESRI_EXPORT_HINT)
    return payload, load_meta


def normalize_geo_input(*, geojson: dict[str, Any] | None = None, input_path: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, load_meta = load_geo_input(geojson=geojson, input_path=input_path)
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
    input_meta["input_alerts"] = build_input_alerts(crs_meta)
    return fc, input_meta
