#!/usr/bin/env python3
"""
coord_transform.py — Convert WGS84 (standard GPS / ArcGIS default) coordinates
to GCJ-02 (used by AMap/高德) and BD-09 (used by Baidu Maps/百度), using the
standard public offset algorithm used throughout the Chinese mapping
ecosystem. No API key required — this is pure math, not a network call.

Why this matters: coordinates exported from ArcGIS as WGS84 will be off by
roughly 100-700m if sent directly to AMap/Baidu APIs without this conversion,
which can shift a query point onto the wrong parcel entirely.

Usage as a CLI (prints JSON):
    python coord_transform.py wgs84_to_gcj02 <lon> <lat>
    python coord_transform.py wgs84_to_bd09  <lon> <lat>
    python coord_transform.py gcj02_to_bd09  <lon> <lat>

Usage as a library:
    from coord_transform import wgs84_to_gcj02, wgs84_to_bd09
    gcj_lon, gcj_lat = wgs84_to_gcj02(113.2644, 23.1291)
"""
import json
import math
import sys

X_PI = math.pi * 3000.0 / 180.0
PI = math.pi
A = 6378245.0  # semi-major axis
EE = 0.00669342162296594323  # eccentricity squared


def _out_of_china(lon, lat):
    """GCJ-02 offset only applies inside mainland China's bounding area."""
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * PI) + 320 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon, lat):
    """WGS84 -> GCJ-02 (AMap/高德, and Google Maps China)."""
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * PI
    magic = math.sin(rad_lat)
    magic = 1 - EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrt_magic) * PI)
    dlon = (dlon * 180.0) / (A / sqrt_magic * math.cos(rad_lat) * PI)
    return lon + dlon, lat + dlat


def gcj02_to_bd09(lon, lat):
    """GCJ-02 -> BD-09 (Baidu Maps/百度)."""
    z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * X_PI)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * X_PI)
    bd_lon = z * math.cos(theta) + 0.0065
    bd_lat = z * math.sin(theta) + 0.006
    return bd_lon, bd_lat


def wgs84_to_bd09(lon, lat):
    """WGS84 -> BD-09, chained through GCJ-02."""
    gcj_lon, gcj_lat = wgs84_to_gcj02(lon, lat)
    return gcj02_to_bd09(gcj_lon, gcj_lat)


FUNCS = {
    "wgs84_to_gcj02": wgs84_to_gcj02,
    "wgs84_to_bd09": wgs84_to_bd09,
    "gcj02_to_bd09": gcj02_to_bd09,
}


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in FUNCS:
        print(f"Usage: coord_transform.py <{'|'.join(FUNCS)}> <lon> <lat>", file=sys.stderr)
        sys.exit(1)
    fn = FUNCS[sys.argv[1]]
    lon, lat = float(sys.argv[2]), float(sys.argv[3])
    out_lon, out_lat = fn(lon, lat)
    print(json.dumps({"lon": round(out_lon, 6), "lat": round(out_lat, 6)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
