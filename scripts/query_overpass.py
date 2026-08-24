#!/usr/bin/env python3
"""
query_overpass.py — Query OpenStreetMap's Overpass API around a point to get
real-world landuse / buildings / amenities / roads / named places nearby.
This is the "online" data source; if it fails (no network, timeout, rate
limit), the skill must fall back to offline reasoning — this script makes
that failure explicit and machine-detectable via exit code.

Usage:
    python query_overpass.py <lat> <lon> [radius_m] [--timeout SECONDS]

Exit codes:
    0  success, JSON result printed to stdout
    2  network/timeout/HTTP error -> caller should fall back to offline mode
    1  bad arguments

Output (stdout, JSON) on success:
    {
      "center": {"lat":..., "lon":...},
      "radius_m": 300,
      "landuse": [{"tag": "residential", "name": "...", "distance_m": ...}, ...],
      "buildings": {"count": 12, "by_type": {"apartments": 8, "yes": 4}},
      "amenities": [{"tag": "amenity=school", "name": "...", "distance_m": ...}, ...],
      "roads": [{"name": "...", "highway": "primary"}, ...],
      "places": [{"tag": "place=suburb", "name": "..."}, ...]
    }

Notes:
- Radius defaults to 300m; widen it for very large or very small parcels
  by passing a radius derived from geo_stats.py's bbox_width/height.
- Only ONE Overpass endpoint is tried by default (overpass-api.de). If your
  environment blocks it, either allow it in network settings or point
  OVERPASS_URL env var at a mirror / self-hosted instance.
- Be polite to the public Overpass API: do not call this in a tight loop
  over many features without a short sleep (~1s) between calls.
"""
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse


OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")


def build_query(lat, lon, radius_m):
    return f"""
[out:json][timeout:25];
(
  way(around:{radius_m},{lat},{lon})["landuse"];
  relation(around:{radius_m},{lat},{lon})["landuse"];
  way(around:{radius_m},{lat},{lon})["building"];
  node(around:{radius_m},{lat},{lon})["amenity"];
  node(around:{radius_m},{lat},{lon})["shop"];
  node(around:{radius_m},{lat},{lon})["office"];
  way(around:{radius_m},{lat},{lon})["highway"]["name"];
  node(around:{radius_m},{lat},{lon})["place"];
  way(around:{radius_m},{lat},{lon})["place"];
);
out center tags;
"""


def run_query(lat, lon, radius_m, timeout):
    query = build_query(lat, lon, radius_m)
    body = ("data=" + urllib.parse.quote(query)).encode("utf-8")
    req = urllib.request.Request(OVERPASS_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def summarize(raw, lat, lon, radius_m):
    landuse, amenities, roads, places = [], [], [], []
    building_types = {}

    for el in raw.get("elements", []):
        tags = el.get("tags", {}) or {}
        name = tags.get("name")

        if "landuse" in tags:
            landuse.append({"tag": tags["landuse"], "name": name})
        if "building" in tags:
            btype = tags["building"]
            building_types[btype] = building_types.get(btype, 0) + 1
        if "amenity" in tags:
            amenities.append({"tag": f"amenity={tags['amenity']}", "name": name})
        if "shop" in tags:
            amenities.append({"tag": f"shop={tags['shop']}", "name": name})
        if "office" in tags:
            amenities.append({"tag": f"office={tags['office']}", "name": name})
        if "highway" in tags and name:
            roads.append({"name": name, "highway": tags["highway"]})
        if "place" in tags:
            places.append({"tag": f"place={tags['place']}", "name": name})

    return {
        "center": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "landuse": landuse[:30],
        "buildings": {"count": sum(building_types.values()), "by_type": building_types},
        "amenities": amenities[:30],
        "roads": roads[:20],
        "places": places[:10],
    }


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: query_overpass.py <lat> <lon> [radius_m] [--timeout SECONDS]", file=sys.stderr)
        sys.exit(1)

    lat = float(args[0])
    lon = float(args[1])
    radius_m = 300
    timeout = 20
    rest = args[2:]
    i = 0
    while i < len(rest):
        if rest[i] == "--timeout" and i + 1 < len(rest):
            timeout = float(rest[i + 1])
            i += 2
        else:
            radius_m = float(rest[i])
            i += 1

    try:
        raw = run_query(lat, lon, radius_m, timeout)
    except urllib.error.URLError as e:
        print(json.dumps({"error": "network_error", "detail": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(json.dumps({"error": "unexpected_error", "detail": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)

    print(json.dumps(summarize(raw, lat, lon, radius_m), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
