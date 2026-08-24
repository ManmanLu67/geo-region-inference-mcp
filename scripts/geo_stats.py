#!/usr/bin/env python3
"""
geo_stats.py — Compute deterministic geometric features from a GeoJSON-like
input (FeatureCollection, single Feature, or bare geometry), so the AI never
has to eyeball coordinates to estimate area/shape.

Usage:
    python geo_stats.py <input.json>
    python geo_stats.py -   # read JSON from stdin

Output (stdout, JSON):
    A list of per-feature stats:
    [
      {
        "index": 0,
        "geometry_type": "Polygon",
        "vertex_count": 42,
        "centroid": {"lon": 113.xxx, "lat": 23.xxx},
        "bbox": {"min_lon":..., "min_lat":..., "max_lon":..., "max_lat":...},
        "area_m2": 12345.6,           # null for non-polygon geometries
        "perimeter_m": 456.7,
        "compactness": 0.42,          # 4*pi*area/perimeter^2, 1=circle, ->0=elongated
        "bbox_width_m": ..., "bbox_height_m": ...,
        "aspect_ratio": 2.3,          # long side / short side of bbox
        "properties": {...}            # passed through unchanged
      },
      ...
    ]

Notes:
- Area/perimeter use an equirectangular approximation around each feature's
  own latitude (1 deg lat ~= 111320 m; 1 deg lon ~= 111320 * cos(lat) m).
  This is accurate enough for classifying parcel scale/shape, NOT for legal
  survey purposes.
- Only the outer ring of Polygon/MultiPolygon is used for area/perimeter
  (holes are ignored) — sufficient for shape classification.
- If coordinates look like projected (non lon/lat) values (e.g. abs(x) > 180
  or abs(y) > 90), the script skips the deg->m conversion and treats units
  as already-metric, flagging "coordinate_system_warning" in the output.
"""
import json
import math
import sys


def deg_to_m_factors(lat_deg):
    lat_rad = math.radians(lat_deg)
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(lat_rad)
    return m_per_deg_lon, m_per_deg_lat


def ring_area_perimeter(ring, projected):
    """Shoelace area + perimeter for a single ring of [x,y] pairs."""
    if len(ring) < 3:
        return 0.0, 0.0
    if projected:
        pts = ring
    else:
        # local equirectangular projection around ring's mean latitude
        mean_lat = sum(p[1] for p in ring) / len(ring)
        mx, my = deg_to_m_factors(mean_lat)
        pts = [(p[0] * mx, p[1] * my) for p in ring]

    area = 0.0
    perim = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
        perim += math.hypot(x2 - x1, y2 - y1)
    return abs(area) / 2.0, perim


def flatten_coords(geom):
    """Yield every [x, y] pair in a geometry, regardless of nesting depth."""
    t = geom.get("type")
    coords = geom.get("coordinates")
    if t == "Point":
        yield coords
    elif t in ("MultiPoint", "LineString"):
        for c in coords:
            yield c
    elif t == "MultiLineString" or t == "Polygon":
        for part in coords:
            for c in part:
                yield c
    elif t == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for c in ring:
                    yield c
    elif t == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from flatten_coords(g)


def outer_rings(geom):
    """Return list of outer rings (list of [x,y]) for Polygon/MultiPolygon."""
    t = geom.get("type")
    if t == "Polygon":
        coords = geom.get("coordinates", [])
        return [coords[0]] if coords else []
    if t == "MultiPolygon":
        return [poly[0] for poly in geom.get("coordinates", []) if poly]
    return []


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

    rings = outer_rings(geom)
    area_m2 = None
    perimeter_m = None
    compactness = None
    if rings:
        total_area = 0.0
        total_perim = 0.0
        for ring in rings:
            a, p = ring_area_perimeter(ring, projected)
            total_area += a
            total_perim += p
        area_m2 = round(total_area, 1)
        perimeter_m = round(total_perim, 1)
        if total_perim > 0:
            compactness = round((4 * math.pi * total_area) / (total_perim ** 2), 3)

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
        # bare geometry
        yield data, {}
    elif isinstance(data, list):
        # list of features or geometries
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
