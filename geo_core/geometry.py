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


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _seg_bbox_disjoint(
    a: list[float], b: list[float], c: list[float], d: list[float]
) -> bool:
    return (
        max(a[0], b[0]) < min(c[0], d[0])
        or max(c[0], d[0]) < min(a[0], b[0])
        or max(a[1], b[1]) < min(c[1], d[1])
        or max(c[1], d[1]) < min(a[1], b[1])
    )


def _collinear_overlap_length(
    ax: float, ay: float, bx: float, by: float,
    cx: float, cy: float, dx: float, dy: float,
) -> bool:
    x_lo, x_hi = max(min(ax, bx), min(cx, dx)), min(max(ax, bx), max(cx, dx))
    y_lo, y_hi = max(min(ay, by), min(cy, dy)), min(max(ay, by), max(cy, dy))
    if x_hi < x_lo or y_hi < y_lo:
        return False
    return (x_hi - x_lo) + (y_hi - y_lo) > 0


def segments_properly_intersect(
    a: list[float], b: list[float], c: list[float], d: list[float]
) -> bool:
    """True if AB and CD cross or collinear-overlap with positive length.

    Sharing only an endpoint is not an intersection.
    """
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    cx, cy = float(c[0]), float(c[1])
    dx, dy = float(d[0]), float(d[1])
    if _seg_bbox_disjoint([ax, ay], [bx, by], [cx, cy], [dx, dy]):
        return False
    o1 = _orient(ax, ay, bx, by, cx, cy)
    o2 = _orient(ax, ay, bx, by, dx, dy)
    o3 = _orient(cx, cy, dx, dy, ax, ay)
    o4 = _orient(cx, cy, dx, dy, bx, by)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    if o1 == 0 and o2 == 0 and o3 == 0 and o4 == 0:
        return _collinear_overlap_length(ax, ay, bx, by, cx, cy, dx, dy)
    return False


def _ring_edge_pts(ring: list) -> list[list[float]]:
    pts = [[float(p[0]), float(p[1])] for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def ring_self_intersects(ring: list) -> bool:
    """True if a non-adjacent pair of edges properly intersects."""
    pts = _ring_edge_pts(ring)
    n = len(pts)
    if n < 4:
        return False
    edges = [(pts[i], pts[(i + 1) % n]) for i in range(n)]
    boxes = [
        (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        for a, b in edges
    ]
    last = n - 1
    for i in range(n):
        bi = boxes[i]
        a, b = edges[i]
        j0 = i + 2
        j1 = last if i == 0 else n
        for j in range(j0, j1):
            bj = boxes[j]
            if bi[2] < bj[0] or bj[2] < bi[0] or bi[3] < bj[1] or bj[3] < bi[1]:
                continue
            c, d = edges[j]
            if segments_properly_intersect(a, b, c, d):
                return True
    return False


def rings_edges_cross(a: list, b: list) -> bool:
    """True if any edge of ring a properly intersects any edge of ring b."""
    pa, pb = _ring_edge_pts(a), _ring_edge_pts(b)
    na, nb = len(pa), len(pb)
    if na < 2 or nb < 2:
        return False
    for i in range(na):
        e1, e2 = pa[i], pa[(i + 1) % na]
        for j in range(nb):
            f1, f2 = pb[j], pb[(j + 1) % nb]
            if segments_properly_intersect(e1, e2, f1, f2):
                return True
    return False


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


def ring_stats(ring: list[list[float]], *, projected: bool = False) -> tuple[float, float, float, float]:
    """One pass over a ring: (area_m2, perimeter_m, centroid_lon, centroid_lat).

    ring_area_perimeter + ring_centroid each re-project the ring via _local_metric_ring
    and re-accumulate the same shoelace cross products; this walks the ring once.
    """
    if len(ring) < 3:
        if not ring:
            return 0.0, 0.0, 0.0, 0.0
        return 0.0, 0.0, float(ring[0][0]), float(ring[0][1])
    pts, (ox, oy), (mx, my) = _local_metric_ring(ring, projected=projected)
    n = len(pts)
    area2 = 0.0
    perim = 0.0
    cx = cy = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
        perim += math.hypot(x2 - x1, y2 - y1)
    area = area2 / 2.0
    if abs(area) < 1e-12:
        lon = sum(x for x, _ in pts) / n / mx + ox / mx
        lat = sum(y for _, y in pts) / n / my + oy / my
        return abs(area), perim, lon, lat
    return abs(area), perim, (cx / (6.0 * area) + ox) / mx, (cy / (6.0 * area) + oy) / my


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


def _ring_body(ring: list) -> list:
    if len(ring) > 1 and ring[0] == ring[-1]:
        return ring[:-1]
    return list(ring)


def _ring_unique_count(ring: list) -> int:
    return len({(float(p[0]), float(p[1])) for p in _ring_body(ring) if len(p) >= 2})


def _ensure_closed_ring(ring: list) -> tuple[list, bool]:
    if not ring:
        return ring, False
    if len(ring) > 1 and ring[0] == ring[-1]:
        return ring, False
    return list(ring) + [list(ring[0])], True


def _normalize_polygon_rings_inplace(geom: dict[str, Any]) -> tuple[bool, bool]:
    """Close unclosed rings and flag degenerates.

    Intentionally mutates feature["geometry"] in place. Downstream
    (geometry_pipeline → POI radius / evidence) reads this same FeatureCollection;
    there is no "input GeoJSON is immutable" contract.
    """
    t = geom.get("type")
    coords = geom.get("coordinates")
    if t not in ("Polygon", "MultiPolygon") or not coords:
        return False, False
    auto_closed = False
    degenerate = False

    def fix_poly(poly: list) -> list:
        nonlocal auto_closed, degenerate
        out: list = []
        for ring in poly:
            if _ring_unique_count(ring) < 3:
                degenerate = True
                out.append(ring)
                continue
            closed, did = _ensure_closed_ring(ring)
            if did:
                auto_closed = True
            out.append(closed)
        return out

    if t == "Polygon":
        geom["coordinates"] = fix_poly(coords)
    else:
        geom["coordinates"] = [fix_poly(poly) for poly in coords]
    return auto_closed, degenerate


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
    self_intersecting = False
    auto_closed = False
    degenerate = False
    t = geom.get("type")
    if t in ("Polygon", "MultiPolygon"):
        auto_closed, degenerate = _normalize_polygon_rings_inplace(geom)
    parts = polygon_parts(geom)
    if parts:
        if not degenerate and any(
            ring_self_intersects(outer) or any(ring_self_intersects(h) for h in holes)
            for outer, holes in parts
        ):
            self_intersecting = True
        area_sum = perim_sum = 0.0
        outer_area = 0.0
        hole_perim = 0.0
        hole_count = 0
        num_lon = num_lat = weights = 0.0
        fallback_cx = fallback_cy = None
        for outer, holes in parts:
            a_o, p_o, lon_o, lat_o = ring_stats(outer)
            area_sum += a_o
            outer_area += a_o
            perim_sum += p_o
            num_lon += lon_o * a_o
            num_lat += lat_o * a_o
            weights += a_o
            if fallback_cx is None:
                fallback_cx, fallback_cy = lon_o, lat_o
            for h in holes:
                a_h, p_h, lon_h, lat_h = ring_stats(h)
                area_sum -= a_h
                hole_perim += p_h
                hole_count += 1
                num_lon -= lon_h * a_h
                num_lat -= lat_h * a_h
                weights -= a_h
        area_sum = max(area_sum, 0.0)
        if self_intersecting or degenerate:
            area = None
            compact = None
            perim = round(perim_sum, 1) if not degenerate else None
        else:
            area, perim = round(area_sum, 1), round(perim_sum, 1)
            if perim_sum > 0 and area_sum > 0:
                compact = round((4 * math.pi * area_sum) / (perim_sum**2), 3)
        if weights > 0:
            cx, cy = num_lon / weights, num_lat / weights
        elif fallback_cx is not None:
            cx, cy = fallback_cx, fallback_cy
        if not self_intersecting and not degenerate:
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
    if self_intersecting:
        result["self_intersecting"] = True
    if auto_closed:
        result["auto_closed"] = True
    if degenerate:
        result["invalid_reason"] = "degenerate_ring"
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
