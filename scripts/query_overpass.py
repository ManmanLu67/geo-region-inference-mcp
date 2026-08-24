#!/usr/bin/env python3
"""CLI wrapper for Overpass via geo_clients."""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from geo_clients import overpass_query  # noqa: E402


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: query_overpass.py <lat> <lon> [radius_m] [--timeout SECONDS]", file=sys.stderr)
        sys.exit(1)
    lat, lon = float(args[0]), float(args[1])
    radius_m = 300
    rest = args[2:]
    i = 0
    while i < len(rest):
        if rest[i] == "--timeout" and i + 1 < len(rest):
            os.environ["HTTP_TIMEOUT_SECONDS"] = rest[i + 1]
            i += 2
        else:
            radius_m = float(rest[i])
            i += 1
    result = overpass_query(lat, lon, radius_m)
    if result.get("status") == "error":
        print(json.dumps({"error": "network_error", "detail": result.get("reason")}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
