#!/usr/bin/env python3
# DEPRECATED — 见 scripts/README.md
"""
geo_stats.py — Compute deterministic geometric features from a GeoJSON-like
input (FeatureCollection, single Feature, or bare geometry).

Usage:
    python geo_stats.py <input.json>
    python geo_stats.py -   # read JSON from stdin
"""
import json
import math
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from geo_geometry import (  # noqa: E402
    deg_to_m_factors,
    flatten_coords,
    polygon_parts,
    ring_area_perimeter,
    ring_centroid,
)


def compute_stats(geom, properties, index):
    pts = list(flatten_coords(geom))
    if not pts:
        return {"index": index, "error": "no coordinates found", "properties": properties}

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    min_lon, max_lon = min(xs), max(xs)
    min_lat, max_lat = min(ys), max(ys)
    centroid_lon = sum(xs) / len(xs)
    centroid_lat = sum(ys) / len(ys)

    projected = abs(centroid_lon) > 180 or abs(centroid_lat) > 90
    warning = "coordinates look projected (not lon/lat); treated as already-metric" if projected else None

    mx, my = (1.0, 1.0) if projected else deg_to_m_factors(centroid_lat)
    bbox_width_m = (max_lon - min_lon) * mx
    bbox_height_m = (max_lat - min_lat) * my
    long_side = max(bbox_width_m, bbox_height_m)
    short_side = max(min(bbox_width_m, bbox_height_m), 1e-9)
    aspect_ratio = round(long_side / short_side, 2)

    parts = polygon_parts(geom)
    area_m2 = None
    perimeter_m = None
    compactness = None
    if parts:
        area_sum = 0.0
        perim_sum = 0.0
        num_lon = num_lat = weights = 0.0
        fallback = None
        for outer, holes in parts:
            a_o, p_o = ring_area_perimeter(outer, projected=projected)
            lon_o, lat_o, ra_o = ring_centroid(outer, projected=projected)
            area_sum += a_o
            perim_sum += p_o
            w = ra_o if ra_o > 0 else a_o
            num_lon += lon_o * w
            num_lat += lat_o * w
            weights += w
            if fallback is None:
                fallback = (lon_o, lat_o)
            for h in holes:
                a_h, _p_h = ring_area_perimeter(h, projected=projected)
                lon_h, lat_h, ra_h = ring_centroid(h, projected=projected)
                area_sum -= a_h
                w_h = ra_h if ra_h > 0 else a_h
                num_lon -= lon_h * w_h
                num_lat -= lat_h * w_h
                weights -= w_h
        area_sum = max(area_sum, 0.0)
        area_m2 = round(area_sum, 1)
        perimeter_m = round(perim_sum, 1)
        if perim_sum > 0 and area_sum > 0:
            compactness = round((4 * math.pi * area_sum) / (perim_sum ** 2), 3)
        if weights > 0:
            centroid_lon, centroid_lat = num_lon / weights, num_lat / weights
        elif fallback is not None:
            centroid_lon, centroid_lat = fallback

    result = {
        "index": index,
        "geometry_type": geom.get("type"),
        "vertex_count": len(pts),
        "centroid": {"lon": round(centroid_lon, 6), "lat": round(centroid_lat, 6)},
        "bbox": {
            "min_lon": round(min_lon, 6), "min_lat": round(min_lat, 6),
            "max_lon": round(max_lon, 6), "max_lat": round(max_lat, 6),
        },
        "bbox_width_m": round(bbox_width_m, 1),
        "bbox_height_m": round(bbox_height_m, 1),
        "aspect_ratio": aspect_ratio,
        "area_m2": area_m2,
        "perimeter_m": perimeter_m,
        "compactness": compactness,
        "properties": properties,
    }
    if warning:
        result["coordinate_system_warning"] = warning
    return result


def iter_features(data):
    """Normalize input into a list of (geometry, properties) pairs."""
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        for f in data.get("features", []):
            yield f.get("geometry", {}), f.get("properties", {}) or {}
    elif isinstance(data, dict) and data.get("type") == "Feature":
        yield data.get("geometry", {}), data.get("properties", {}) or {}
    elif isinstance(data, dict) and "type" in data and "coordinates" in data:
        yield data, {}
    elif isinstance(data, list):
        for item in data:
            if item.get("type") == "Feature":
                yield item.get("geometry", {}), item.get("properties", {}) or {}
            else:
                yield item, {}
    else:
        raise ValueError("Unrecognized input JSON shape (expected FeatureCollection / Feature / geometry / list)")


def main():
    if len(sys.argv) != 2:
        print("Usage: python geo_stats.py <input.json | ->", file=sys.stderr)
        sys.exit(1)

    src = sys.argv[1]
    raw = sys.stdin.read() if src == "-" else open(src, "r", encoding="utf-8").read()
    data = json.loads(raw)

    out = []
    for i, (geom, props) in enumerate(iter_features(data)):
        try:
            out.append(compute_stats(geom, props, i))
        except Exception as e:
            out.append({"index": i, "error": str(e), "properties": props})

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
