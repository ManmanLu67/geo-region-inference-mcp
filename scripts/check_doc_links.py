#!/usr/bin/env python3
"""Scan Markdown relative links and heading anchors (read-only doc audit)."""

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
    skip = {"SKILL.md"}  # plan scope: audit all md except SKILL optional; include SKILL for links
    all_errors: list[str] = []
    for md in sorted(ROOT.rglob("*.md")):
        if md.name in skip:
            continue
        all_errors.extend(scan_file(md))
    if all_errors:
        print("DOC LINK ERRORS:", len(all_errors))
        for e in all_errors:
            print(" ", e)
        return 1
    print("OK: all relative markdown links resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
