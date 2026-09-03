"""Deterministic geometry statistics for GeoJSON features (MCP authoritative path)."""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any


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


def polygon_parts(geom: dict[str, Any]) -> list[tuple[list[list[float]], list[list[list[float]]]]]:
    """RFC 7946 index semantics: coordinates[0] outer, [1:] holes. MultiPolygon per-part."""
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon" and coords:
        return [(coords[0], list(coords[1:]))]
    if t == "MultiPolygon":
        return [(poly[0], list(poly[1:])) for poly in coords if poly]
    return []


def _local_metric_ring(ring: list[list[float]], *, projected: bool = False):
    """米制坐标 + 平移到局部原点，返回 (局部坐标, 原点, 米制系数)。

    经纬度直接乘 111320 后坐标量级约 1e7，shoelace 叉积项约 4e13，
    而小地块（数十~数百 m²）的有效信号只有 1e2 量级 —— float64 的
    2.2e-16 相对精度在累加中会被放大，导致质心偏移数十到数百米
    （面积越小越严重，实测 110m² 地块偏 264m、114m² 偏 379m）。
    平移到首顶点后，叉积项量级降到 1e4 左右，信号不再被淹没。

    projected=True 时短路 deg_to_m_factors（投影坐标的 y 是北向米，不可当纬度求 cos）。
    """
    if projected:
        mx, my = 1.0, 1.0
        pts = [(float(p[0]), float(p[1])) for p in ring]
    else:
        mean_lat = sum(float(p[1]) for p in ring) / len(ring)
        mx, my = deg_to_m_factors(mean_lat)
        pts = [(float(p[0]) * mx, float(p[1]) * my) for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    ox, oy = pts[0]
    return [(x - ox, y - oy) for x, y in pts], (ox, oy), (mx, my)


def ring_area_perimeter(ring: list[list[float]], *, projected: bool = False) -> tuple[float, float]:
    if len(ring) < 3:
        return 0.0, 0.0
    pts, _origin, _factors = _local_metric_ring(ring, projected=projected)
    area2 = 0.0
    perim = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        area2 += x1 * y2 - x2 * y1
        perim += math.hypot(x2 - x1, y2 - y1)
    return abs(area2) / 2.0, perim


def ring_centroid(ring: list[list[float]], *, projected: bool = False) -> tuple[float, float, float]:
    if len(ring) < 3:
        return float(ring[0][0]), float(ring[0][1]), 0.0
    pts, (ox, oy), (mx, my) = _local_metric_ring(ring, projected=projected)
    area2 = 0.0
    cx = cy = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area = area2 / 2.0
    if abs(area) < 1e-12:
        return sum(x for x, _ in pts) / n / mx + ox / mx, sum(y for _, y in pts) / n / my + oy / my, 0.0
    cx = cx / (6.0 * area)
    cy = cy / (6.0 * area)
    return (cx + ox) / mx, (cy + oy) / my, abs(area)


def _hole_debug_enabled() -> bool:
    raw = os.environ.get("GEO_HOLE_DEBUG", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _hole_debug_ratio() -> float:
    raw = os.environ.get("GEO_HOLE_DEBUG_RATIO", "").strip()
    if raw:
        return float(raw)
    return 0.95


def _maybe_log_holes(
    index: int,
    area_sum: float,
    outer_area: float,
    hole_count: int,
    hole_perim: float,
) -> None:
    if not _hole_debug_enabled() or outer_area <= 0:
        return
    ratio = area_sum / outer_area
    if ratio >= _hole_debug_ratio():
        return
    print(
        f"[geo_geometry] hole-debug index={index} hole_count={hole_count} "
        f"hole_perimeter_m={hole_perim:.1f} net/outer={ratio:.3f}",
        file=sys.stderr,
        flush=True,
    )


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
    mx, my = deg_to_m_factors(cy)
    width = abs(max_x - min_x) * mx
    height = abs(max_y - min_y) * my
    long_side = max(width, height)
    short_side = max(min(width, height), 1e-9)
    aspect = long_side / short_side
    area = perim = compact = None
    parts = polygon_parts(geom)
    t = geom.get("type")
    if parts:
        area_sum = perim_sum = 0.0
        outer_area = 0.0
        hole_perim = 0.0
        hole_count = 0
        num_lon = num_lat = weights = 0.0
        fallback_cx = fallback_cy = None
        for outer, holes in parts:
            a_o, p_o = ring_area_perimeter(outer)
            lon_o, lat_o, ra_o = ring_centroid(outer)
            area_sum += a_o
            outer_area += a_o
            perim_sum += p_o
            w = ra_o if ra_o > 0 else a_o
            num_lon += lon_o * w
            num_lat += lat_o * w
            weights += w
            if fallback_cx is None:
                fallback_cx, fallback_cy = lon_o, lat_o
            for h in holes:
                a_h, p_h = ring_area_perimeter(h)
                lon_h, lat_h, ra_h = ring_centroid(h)
                area_sum -= a_h
                hole_perim += p_h
                hole_count += 1
                w_h = ra_h if ra_h > 0 else a_h
                num_lon -= lon_h * w_h
                num_lat -= lat_h * w_h
                weights -= w_h
        area_sum = max(area_sum, 0.0)
        area, perim = round(area_sum, 1), round(perim_sum, 1)
        if perim_sum > 0 and area_sum > 0:
            compact = round((4 * math.pi * area_sum) / (perim_sum**2), 3)
        if weights > 0:
            cx, cy = num_lon / weights, num_lat / weights
        elif fallback_cx is not None:
            cx, cy = fallback_cx, fallback_cy
        _maybe_log_holes(index, area_sum, outer_area, hole_count, hole_perim)
    elif t in ("LineString", "MultiLineString", "MultiPoint"):
        uniq = pts[:-1] if len(pts) > 1 and pts[0] == pts[-1] else pts
        cx = sum(float(p[0]) for p in uniq) / len(uniq)
        cy = sum(float(p[1]) for p in uniq) / len(uniq)
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
