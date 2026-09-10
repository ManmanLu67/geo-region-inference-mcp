"""Esri ring nesting: containment, even-odd depth, edge cross.

Not format conversion (that stays in inputs.py) and not area/centroid (geometry.py).
"""

from __future__ import annotations

from .geometry import rings_edges_cross

# Ray-casting treats boundary hits as undefined. Esri hole rings sometimes share a
# vertex with the outer ring. Require a majority of vertices strictly inside so a
# shared vertex does not reject a real hole, while an edge-adjacent neighbour
# (≈0 interior vertices) is not treated as nested. Not a GIS standard constant.
_RING_INSIDE_MIN_INTERIOR_RATIO = 0.6


def _ring_abs_area(ring: list) -> float:
    s = 0.0
    n = len(ring)
    if n < 2:
        return 0.0
    for i in range(n - 1):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[i + 1][0]), float(ring[i + 1][1])
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _ring_bbox(ring: list) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in ring]
    ys = [float(p[1]) for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    ominx, ominy, omaxx, omaxy = outer
    iminx, iminy, imaxx, imaxy = inner
    return iminx >= ominx and iminy >= ominy and imaxx <= omaxx and imaxy <= omaxy


def _ring_vertices(ring: list) -> list:
    if len(ring) > 1 and ring[0] == ring[-1]:
        return ring[:-1]
    return ring


def _point_in_ring(x: float, y: float, ring: list) -> bool:
    """Strict interior test (ray casting). Do not copy into geometry.py / inputs.py."""
    pts = _ring_vertices(ring)
    n = len(pts)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(pts[i][0]), float(pts[i][1])
        xj, yj = float(pts[j][0]), float(pts[j][1])
        intersects = (yi > y) != (yj > y)
        if intersects:
            x_int = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_int:
                inside = not inside
        j = i
    return inside


def _ring_inside(inner: list, outer: list) -> bool:
    """True if `inner` is nested in `outer` by interior-vertex ratio."""
    if not _bbox_contains(_ring_bbox(outer), _ring_bbox(inner)):
        return False
    pts = _ring_vertices(inner)
    if not pts:
        return False
    interior = sum(1 for p in pts if _point_in_ring(float(p[0]), float(p[1]), outer))
    return (interior / len(pts)) >= _RING_INSIDE_MIN_INTERIOR_RATIO


def _rings_cross(a: list, b: list) -> bool:
    """Partial overlap: bboxes meet, neither nest, edges cross or a vertex lies inside."""
    if _ring_inside(a, b) or _ring_inside(b, a):
        return False
    ab, bb = _ring_bbox(a), _ring_bbox(b)
    disjoint = ab[2] < bb[0] or bb[2] < ab[0] or ab[3] < bb[1] or bb[3] < ab[1]
    if disjoint:
        return False
    if rings_edges_cross(a, b):
        return True
    for p in _ring_vertices(a):
        if _point_in_ring(float(p[0]), float(p[1]), b):
            return True
    for p in _ring_vertices(b):
        if _point_in_ring(float(p[0]), float(p[1]), a):
            return True
    return False


def _resolve_esri_rings(rings: list) -> tuple[list[tuple[list, list]], bool]:
    """Nest Esri rings by containment (even-odd depth). Winding is ignored.

    Returns (parts, unresolved). Crossing rings → each ring is an independent part.
    """
    classified = [r for r in rings if r]
    if not classified:
        return [], True

    def _split_all() -> tuple[list[tuple[list, list]], bool]:
        return [(r, []) for r in classified], True

    n = len(classified)
    for i in range(n):
        for j in range(i + 1, n):
            if _rings_cross(classified[i], classified[j]):
                return _split_all()

    containers: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if _ring_inside(classified[i], classified[j]):
                containers[i].append(j)
    depth = [len(containers[i]) for i in range(n)]

    holes_of: dict[int, list] = {i: [] for i in range(n) if depth[i] % 2 == 0}
    for i in range(n):
        if depth[i] % 2 == 0:
            continue
        if not containers[i]:
            return _split_all()
        parent = min(containers[i], key=lambda j: (_ring_abs_area(classified[j]), j))
        if depth[parent] % 2 != 0 or parent not in holes_of:
            return _split_all()
        holes_of[parent].append(classified[i])

    parts = [(classified[i], holes_of[i]) for i in range(n) if depth[i] % 2 == 0]
    if not parts:
        return _split_all()
    return parts, False
