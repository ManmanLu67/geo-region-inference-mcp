#!/usr/bin/env python3
"""
validate_output.py — Validate the final inference result against the schema
in references/output_schema.md before showing it to the user. Catches the
most common mistakes: missing fields, unsorted confidence, out-of-range
scores, empty evidence.

Usage:
    python validate_output.py <result.json>
    python validate_output.py -   # read JSON from stdin

Exit codes:
    0  valid
    1  invalid (details printed to stderr)
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from validation import collect_errors as validate_result  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print("Usage: validate_output.py <result.json | ->", file=sys.stderr)
        sys.exit(1)

    src = sys.argv[1]
    raw = sys.stdin.read() if src == "-" else open(src, "r", encoding="utf-8").read()
    data = json.loads(raw)

    # Accept either a single result object or a list of them (batch mode)
    results = data if isinstance(data, list) else [data]

    all_errors = []
    for idx, result in enumerate(results):
        errs = validate_result(result)
        for e in errs:
            all_errors.append(f"[item {idx}] {e}")

    if all_errors:
        print("INVALID:", file=sys.stderr)
        for e in all_errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Valid! {len(results)} result(s) passed all checks.")
    sys.exit(0)


if __name__ == "__main__":
    main()
