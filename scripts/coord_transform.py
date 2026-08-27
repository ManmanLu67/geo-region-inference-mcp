#!/usr/bin/env python3
# DEPRECATED — 见 scripts/README.md
"""CLI wrapper around geo_clients coordinate transforms."""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from geo_clients import gcj02_to_bd09, wgs84_to_bd09, wgs84_to_gcj02  # noqa: E402

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
