"""Deterministic geometry statistics for GeoJSON features (MCP authoritative path)."""

from __future__ import annotations

import json
import math
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


def outer_rings(geom: dict[str, Any]) -> list[list[list[float]]]:
    t = geom.get("type")
    coords = geom.get("coordinates", [])
    if t == "Polygon" and coords:
        return [coords[0]]
    if t == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def _metric_ring(ring: list[list[float]]) -> list[tuple[float, float]]:
    mean_lat = sum(float(p[1]) for p in ring) / len(ring)
    mx, my = deg_to_m_factors(mean_lat)
    return [(float(p[0]) * mx, float(p[1]) * my) for p in ring]


def ring_area_perimeter(ring: list[list[float]]) -> tuple[float, float]:
    if len(ring) < 3:
        return 0.0, 0.0
    pts = _metric_ring(ring)
    area2 = 0.0
    perim = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        area2 += x1 * y2 - x2 * y1
        perim += math.hypot(x2 - x1, y2 - y1)
    return abs(area2) / 2.0, perim


def ring_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    if len(ring) < 3:
        return float(ring[0][0]), float(ring[0][1]), 0.0
    mean_lat = sum(float(p[1]) for p in ring) / len(ring)
    mx, my = deg_to_m_factors(mean_lat)
    pts = _metric_ring(ring)
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
        uniq = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
        xs = [float(p[0]) for p in uniq]
        ys = [float(p[1]) for p in uniq]
        return sum(xs) / len(xs), sum(ys) / len(ys), 0.0
    cx = cx / (6.0 * area)
    cy = cy / (6.0 * area)
    return cx / mx, cy / my, abs(area)


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
    rings = outer_rings(geom)
    t = geom.get("type")
    if rings:
        area_sum = perim_sum = 0.0
        cxs = cys = weights = 0.0
        for ring in rings:
            a, p = ring_area_perimeter(ring)
            rlon, rlat, ra = ring_centroid(ring)
            area_sum += a
            perim_sum += p
            w = ra if ra > 0 else a
            cxs += rlon * w
            cys += rlat * w
            weights += w
        area, perim = round(area_sum, 1), round(perim_sum, 1)
        if perim_sum > 0:
            compact = round((4 * math.pi * area_sum) / (perim_sum**2), 3)
        if weights > 0:
            cx, cy = cxs / weights, cys / weights
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
