#!/usr/bin/env python3
"""Scan Markdown relative links, heading anchors, and line endings (read-only doc audit)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def github_slug(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s)
    return s.strip("-")


def collect_anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {github_slug(m.group(2)) for m in HEADING_RE.finditer(text)}


def scan_line_endings(md: Path) -> list[str]:
    errors: list[str] = []
    data = md.read_bytes()
    for i in range(len(data) - 2):
        if data[i : i + 3] == b"\r\r\n":
            errors.append(
                f"{md.relative_to(ROOT)}: malformed line ending \\r\\r\\n at byte {i}"
            )
            break
    for i, byte in enumerate(data):
        if byte == 0x0D and (i + 1 >= len(data) or data[i + 1] != 0x0A):
            errors.append(
                f"{md.relative_to(ROOT)}: lone carriage return at byte {i}"
            )
            break
    return errors


def scan_file(md: Path) -> list[str]:
    errors: list[str] = []
    text = md.read_text(encoding="utf-8")
    base = md.parent
    for raw in LINK_RE.findall(text):
        target = raw.strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        path_part, _, anchor = target.partition("#")
        if path_part:
            resolved = (base / path_part).resolve()
            if not resolved.is_file():
                errors.append(f"{md.relative_to(ROOT)}: missing file [{target}]")
                continue
            if anchor:
                anchors = collect_anchors(resolved)
                slug = github_slug(anchor)
                if slug not in anchors:
                    errors.append(
                        f"{md.relative_to(ROOT)}: bad anchor #{anchor} in [{target}]"
                    )
    return errors


def main() -> int:
    all_errors: list[str] = []
    for md in sorted(ROOT.rglob("*.md")):
        all_errors.extend(scan_line_endings(md))
        all_errors.extend(scan_file(md))
    if all_errors:
        print("DOC AUDIT ERRORS:", len(all_errors))
        for e in all_errors:
            print(" ", e)
        return 1
    print("OK: markdown links resolve and line endings are clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
