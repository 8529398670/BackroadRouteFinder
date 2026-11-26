"""A loop: a round trip that circles a center at about a given distance.

The ring is a handful of points around the center, one per compass sector:
- in miles, the points on a circle of that radius, each moved onto the
  nearest road;
- in minutes, the places the plain fastest drive from the center reaches in
  about that time (an isochrone), so the ring reaches out along fast roads
  and pulls in where the roads are slow.

The route then drives the ring in order, clockwise or anticlockwise, and
where it starts and ends is the person's choice:
- center: out from the center to the nearest ring point, round, and back;
- ring: on the ring itself, round and back to the first point;
- stop: from a start stop of their own to the ring point nearest it, round,
  and back to the stop.

Each leg is planned with the request's own road rules, and counts the roads
earlier legs drove REUSE_FACTOR times over, so the loop comes back by other
roads rather than doubling back along the way out.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from app.routing import costs
from app.routing.graph import Graph
from app.routing.search import NoRoute
from app.routing.snap import OffNetwork, Snap, snap

METRES_PER_MILE = 1609.344
EARTH_RADIUS_M = 6_371_008.8
REUSE_FACTOR = 3.0
# How far a ring point may move to find a road, as a share of the radius.
SNAP_SHARE = 0.25
MIN_SNAP_M = 300.0
# Minutes rings: the search stops at this many times the radius.
SEARCH_LIMIT = 1.5
MIN_RING = 3
# Ring points closer than this to the last one are one point.
MIN_GAP_M = 100.0

UNITS = ("mi", "min")
STARTS = ("center", "ring", "stop")
DIRECTIONS = ("cw", "ccw")


@dataclass
class Loop:
    stops: list[Snap]      # every point the route drives through, in order
    ring: list[Snap]       # the ring points, in driving order


def _offset(lat: float, lon: float, metres: float, bearing: float) -> tuple[float, float]:
    """The point metres away from (lat, lon) on a bearing (degrees from north)."""
    d = metres / EARTH_RADIUS_M
    b = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _bearing(lat1: float, lon1: float, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    p1, p2 = math.radians(lat1), np.radians(lat2)
    dl = np.radians(lon2 - lon1)
    y = np.sin(dl) * np.cos(p2)
    x = math.cos(p1) * np.sin(p2) - math.sin(p1) * np.cos(p2) * np.cos(dl)
    return (np.degrees(np.arctan2(y, x)) + 360) % 360


def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(a, 1.0)))


def _ring_miles(graph: Graph, center: Snap, miles: float, count: int, shunned: np.ndarray) -> list[Snap]:
    radius = miles * METRES_PER_MILE
    reach = max(MIN_SNAP_M, radius * SNAP_SHARE)
    out = []
    for k in range(count):
        lat, lon = _offset(center.lat, center.lon, radius, 360.0 * k / count)
        try:
            out.append(snap(graph, lat, lon, reach, shunned))
        except OffNetwork:
            continue
    return out


@dataclass
class _Pairs:
    """The node-to-node graph's shape, the same for every request: the edges
    sorted by (src, dst), where each distinct pair starts, and its csr layout."""
    order: np.ndarray
    first: np.ndarray
    dst: np.ndarray
    indptr: np.ndarray


_pairs_cache: dict[int, _Pairs] = {}


def _node_pairs(graph: Graph) -> _Pairs:
    cached = _pairs_cache.get(id(graph))
    if cached is not None:
        return cached
    order = np.lexsort((graph.edge_dst, graph.edge_src))
    src, dst = graph.edge_src[order], graph.edge_dst[order]
    new = np.ones(len(src), dtype=bool)
    new[1:] = (src[1:] != src[:-1]) | (dst[1:] != dst[:-1])
    first = np.flatnonzero(new)
    indptr = np.zeros(graph.node_count + 1, dtype=np.int64)
    np.add.at(indptr, src[first] + 1, 1)
    pairs = _Pairs(order=order, first=first, dst=dst[first].astype(np.int32), indptr=np.cumsum(indptr))
    _pairs_cache.clear()  # one map at a time
    _pairs_cache[id(graph)] = pairs
    return pairs


def _ring_minutes(graph: Graph, center: Snap, minutes: float, count: int, options: costs.Options,
                  shunned: np.ndarray) -> list[Snap]:
    """The fastest drive from the center, on intersections: for each sector,
    the intersection reached in the time closest to the radius."""
    target = minutes * 60.0
    seconds = costs.segment_weights(graph, costs.Options(penalty=1.0, rules=options.rules))
    pairs = _node_pairs(graph)
    # Two roads between the same pair of intersections: the quicker one (a
    # sparse matrix would add them up).
    weight = np.minimum.reduceat(seconds[graph.edge_seg][pairs.order], pairs.first)
    matrix = csr_matrix((np.maximum(weight, costs.MIN_WEIGHT), pairs.dst, pairs.indptr), shape=(graph.node_count, graph.node_count))
    # Start from both ends of the center's segment, each with the part of the
    # segment it takes to get there.
    s = center.segment
    length = float(graph.seg_len[s]) or 1.0
    to_a = seconds[s] * center.along / length
    to_b = seconds[s] * (length - center.along) / length
    a, b = int(graph.seg_a[s]), int(graph.seg_b[s])
    times = dijkstra(matrix, directed=True, indices=[a, b], limit=target * SEARCH_LIMIT, min_only=False)
    times = np.minimum(times[0] + to_a, times[1] + to_b)
    reached = np.flatnonzero(np.isfinite(times) & (times < costs.EXCLUDED_WEIGHT))
    if len(reached) == 0:
        return []
    bearings = _bearing(center.lat, center.lon, graph.node_lat[reached], graph.node_lon[reached])
    width = 360.0 / count
    out = []
    for k in range(count):
        middle = width * k
        off = np.abs((bearings - middle + 180) % 360 - 180)
        inside = reached[off <= width / 2]
        if len(inside) == 0:
            continue
        best = int(inside[np.argmin(np.abs(times[inside] - target))])
        if times[best] < target * 0.4:
            continue  # nothing reaches out that far this way: a gap in the ring
        try:
            out.append(snap(graph, float(graph.node_lat[best]), float(graph.node_lon[best]), MIN_SNAP_M, shunned))
        except OffNetwork:
            continue
    return out


def _dedupe(points: list[Snap]) -> list[Snap]:
    """Neighbours that landed on the same road segment, or next to each
    other, are one point."""
    def same(a: Snap, b: Snap) -> bool:
        return a.segment == b.segment or _distance(a.lat, a.lon, b.lat, b.lon) < MIN_GAP_M

    out: list[Snap] = []
    for point in points:
        if out and same(out[-1], point):
            continue
        out.append(point)
    while len(out) > 1 and same(out[0], out[-1]):
        out.pop()
    return out


def plan(graph: Graph, center: tuple[float, float], radius: float, unit: str, start: str, direction: str,
         options: costs.Options, max_snap_m: float, start_point: tuple[float, float] | None = None, count: int = 6) -> Loop:
    shunned = costs.excluded_segments(graph, options)
    try:
        middle = snap(graph, center[0], center[1], max_snap_m, shunned)
    except OffNetwork as error:
        raise NoRoute(f"the loop's center is not near a road on the map ({error})") from None
    if unit == "min":
        ring = _ring_minutes(graph, middle, radius, count, options, shunned)
    else:
        ring = _ring_miles(graph, middle, radius, count, shunned)
    ring = _dedupe(ring)
    if len(ring) < MIN_RING:
        raise NoRoute("not enough roads around the center at that distance for a loop: try another radius")
    if direction == "ccw":
        ring.reverse()

    def rotate(first: int) -> list[Snap]:
        return ring[first:] + ring[:first]

    def nearest(lat: float, lon: float) -> int:
        return min(range(len(ring)), key=lambda i: _distance(lat, lon, ring[i].lat, ring[i].lon))

    if start == "center":
        ring = rotate(nearest(middle.lat, middle.lon))
        stops = [middle, *ring, ring[0], middle]
    elif start == "stop":
        if start_point is None:
            raise NoRoute("a loop from your own start needs a start stop")
        try:
            own = snap(graph, start_point[0], start_point[1], max_snap_m, shunned)
        except OffNetwork as error:
            raise NoRoute(f"the start is not near a road on the map ({error})") from None
        ring = rotate(nearest(own.lat, own.lon))
        stops = [own, *ring, ring[0], own]
    else:
        stops = [*ring, ring[0]]
    return Loop(stops=stops, ring=ring)
