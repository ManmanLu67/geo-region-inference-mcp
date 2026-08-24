#!/usr/bin/env python3
"""
query_amap.py — Query AMap (高德地图) Web服务API for nearby POIs and reverse
geocoding around a point. Optional data source: only runs if AMAP_KEY is set.

Setup: see references/map_api_setup.md for how to get a key.
Usage:
    export AMAP_KEY=your_key_here
    python query_amap.py <wgs84_lat> <wgs84_lon> [radius_m] [--timeout SECONDS]
    python query_amap.py <wgs84_lat> <wgs84_lon> [radius_m] [--keywords "在建|项目|工地|建设"]

Exit codes:
    0  success, JSON result printed to stdout
    2  network/HTTP error -> caller should try the next data source
    3  AMAP_KEY not set -> this source is simply unavailable, skip it
       (distinct from 2 so the caller doesn't confuse "not configured"
        with "configured but temporarily down")
    1  bad arguments

Output shape mirrors query_overpass.py's summary so the reasoning step can
treat all sources uniformly: {"landuse": [], "buildings": {...}, "amenities":
[...], "roads": [...], "places": [...], "source": "amap"}.
AMap's public APIs don't expose raw landuse polygons, so "landuse" is always
empty here — POI type + address component are used instead as the main
evidence (see references/overpass_query_guide.md, same interpretation logic
applies).
"""
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

from coord_transform import wgs84_to_gcj02

REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"
AROUND_URL = "https://restapi.amap.com/v3/place/around"


def http_get_json(url, params, timeout):
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{qs}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(wgs_lat, wgs_lon, radius_m, timeout, key, keywords=None):
    gcj_lon, gcj_lat = wgs84_to_gcj02(wgs_lon, wgs_lat)
    location = f"{gcj_lon:.6f},{gcj_lat:.6f}"

    regeo = http_get_json(REGEO_URL, {
        "key": key, "location": location, "radius": int(radius_m),
        "extensions": "all", "output": "json",
    }, timeout)

    around_params = {
        "key": key, "location": location, "radius": int(radius_m),
        "offset": 25, "page": 1, "output": "json",
    }
    if keywords:
        around_params["keywords"] = keywords
    around = http_get_json(AROUND_URL, around_params, timeout)

    if regeo.get("status") != "1" or around.get("status") != "1":
        raise RuntimeError(f"AMap API error: regeo={regeo.get('info')}, around={around.get('info')}")

    return regeo, around


def summarize(regeo, around, radius_m, keywords=None):
    amenities, roads, places = [], [], []
    building_types = {}

    rc = regeo.get("regeocode", {})
    addr_comp = rc.get("addressComponent", {})
    formatted = rc.get("formatted_address")
    if formatted:
        places.append({"tag": "amap_formatted_address", "name": formatted})
    for level in ("province", "city", "district", "township"):
        val = addr_comp.get(level)
        if isinstance(val, str) and val:
            places.append({"tag": f"amap_{level}", "name": val})

    for r in (addr_comp.get("streetNumber", {}).get("street") and [addr_comp["streetNumber"]] or []):
        if r.get("street"):
            roads.append({"name": r["street"], "highway": "unknown"})
    for poi in rc.get("pois", []) or []:
        name, ptype = poi.get("name"), poi.get("type")
        if name:
            amenities.append({"tag": f"amap_poi={ptype}", "name": name})

    for poi in around.get("pois", []) or []:
        name, ptype = poi.get("name"), poi.get("type")
        big_type = (ptype or "").split(";")[0].split(":")[0]
        if "building" in (ptype or "").lower() or "住宅" in (big_type or "") or "楼" in (name or ""):
            building_types[big_type or "unknown"] = building_types.get(big_type or "unknown", 0) + 1
        if name:
            amenities.append({"tag": f"amap_poi={ptype}", "name": name, "distance_m": poi.get("distance")})

    return {
        "source": "amap",
        "radius_m": radius_m,
        "query_keywords": keywords,
        "landuse": [],  # AMap public API does not expose landuse polygons
        "buildings": {"count": sum(building_types.values()), "by_type": building_types},
        "amenities": amenities[:30],
        "roads": roads[:20],
        "places": places[:10],
    }


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: query_amap.py <wgs84_lat> <wgs84_lon> [radius_m] [--timeout SECONDS]", file=sys.stderr)
        sys.exit(1)

    key = os.environ.get("AMAP_KEY")
    if not key:
        print(json.dumps({"error": "missing_api_key", "detail": "AMAP_KEY env var not set"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)

    lat, lon = float(args[0]), float(args[1])
    radius_m, timeout = 300, 20
    keywords = None
    rest = args[2:]
    i = 0
    while i < len(rest):
        if rest[i] == "--timeout" and i + 1 < len(rest):
            timeout = float(rest[i + 1]); i += 2
        elif rest[i] == "--keywords" and i + 1 < len(rest):
            keywords = rest[i + 1]; i += 2
        else:
            radius_m = float(rest[i]); i += 1

    try:
        regeo, around = run(lat, lon, radius_m, timeout, key, keywords=keywords)
    except urllib.error.URLError as e:
        print(json.dumps({"error": "network_error", "detail": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(json.dumps({"error": "unexpected_error", "detail": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)

    print(json.dumps(summarize(regeo, around, radius_m, keywords=keywords), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
