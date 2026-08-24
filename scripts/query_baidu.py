#!/usr/bin/env python3
"""
query_baidu.py — Query Baidu Maps (百度地图) Web服务API for reverse geocoding
with nearby POIs around a point. Optional data source: only runs if BAIDU_AK
is set.

Setup: see references/map_api_setup.md for how to get an AK (access key).
Usage:
    export BAIDU_AK=your_ak_here
    python query_baidu.py <wgs84_lat> <wgs84_lon> [radius_m] [--timeout SECONDS]

Exit codes:
    0  success, JSON result printed to stdout
    2  network/HTTP error -> caller should try the next data source
    3  BAIDU_AK not set -> this source is simply unavailable, skip it
    1  bad arguments

NOTE: Baidu's reverse-geocoding response field names have changed across API
versions in the past. If parsing errors show up in real use, check the
current response shape at Baidu's API console and adjust `summarize()` —
this script's HTTP calls and coordinate handling are correct, but exact JSON
field names should be re-verified against your account's API version since
this could not be live-tested in this sandbox (outbound access to
api.map.baidu.com is not available here).
"""
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

from coord_transform import wgs84_to_bd09

REGEO_URL = "https://api.map.baidu.com/reverse_geocoding/v3/"


def http_get_json(url, params, timeout):
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{qs}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(wgs_lat, wgs_lon, radius_m, timeout, ak):
    bd_lon, bd_lat = wgs84_to_bd09(wgs_lon, wgs_lat)
    result = http_get_json(REGEO_URL, {
        "ak": ak, "output": "json", "coordtype": "bd09ll",
        "location": f"{bd_lat:.6f},{bd_lon:.6f}",
        "poi": 1, "poi_types": "", "radius": int(radius_m),
    }, timeout)

    if result.get("status") != 0:
        raise RuntimeError(f"Baidu API error: status={result.get('status')}, message={result.get('message')}")

    return result


def summarize(result, radius_m):
    amenities, roads, places = [], [], []
    building_types = {}

    r = result.get("result", {})
    formatted = r.get("formatted_address")
    if formatted:
        places.append({"tag": "baidu_formatted_address", "name": formatted})

    addr_comp = r.get("addressComponent", {})
    for level in ("province", "city", "district", "town", "street"):
        val = addr_comp.get(level)
        if isinstance(val, str) and val:
            places.append({"tag": f"baidu_{level}", "name": val})
            if level == "street":
                roads.append({"name": val, "highway": "unknown"})

    for poi in r.get("pois", []) or []:
        name, ptag = poi.get("name"), poi.get("tag")
        if not name:
            continue
        if "住宅" in (ptag or "") or "小区" in (name or "") or "楼" in (name or ""):
            key = ptag or "unknown"
            building_types[key] = building_types.get(key, 0) + 1
        amenities.append({"tag": f"baidu_poi={ptag}", "name": name, "distance_m": poi.get("distance")})

    return {
        "source": "baidu",
        "radius_m": radius_m,
        "landuse": [],  # Baidu public API does not expose landuse polygons
        "buildings": {"count": sum(building_types.values()), "by_type": building_types},
        "amenities": amenities[:30],
        "roads": roads[:20],
        "places": places[:10],
    }


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: query_baidu.py <wgs84_lat> <wgs84_lon> [radius_m] [--timeout SECONDS]", file=sys.stderr)
        sys.exit(1)

    ak = os.environ.get("BAIDU_AK")
    if not ak:
        print(json.dumps({"error": "missing_api_key", "detail": "BAIDU_AK env var not set"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)

    lat, lon = float(args[0]), float(args[1])
    radius_m, timeout = 300, 20
    rest = args[2:]
    i = 0
    while i < len(rest):
        if rest[i] == "--timeout" and i + 1 < len(rest):
            timeout = float(rest[i + 1]); i += 2
        else:
            radius_m = float(rest[i]); i += 1

    try:
        result = run(lat, lon, radius_m, timeout, ak)
    except urllib.error.URLError as e:
        print(json.dumps({"error": "network_error", "detail": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(json.dumps({"error": "unexpected_error", "detail": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)

    print(json.dumps(summarize(result, radius_m), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
