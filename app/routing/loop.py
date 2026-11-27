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

from app.routing import costs, grid
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


def _ring_miles(graph: Graph, center: Snap, miles: float, count: int, shunned) -> list[Snap]:
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


# The fastest a minutes ring may reach, metres a second (about 80 mph): the
# search runs inside a circle that radius-in-time times this, round the center.
RING_SPEED = 36.0


def _ring_minutes(graph: Graph, center: Snap, minutes: float, count: int, options: costs.Options,
                  shunned) -> list[Snap]:
    """The fastest drive from the center, on intersections: for each sector,
    the intersection reached in the time closest to the radius. Only the
    roads within reach are read (grid.py)."""
    target = minutes * 60.0
    reach = target * SEARCH_LIMIT * RING_SPEED
    segs = grid.segments_near(graph, center.lat, center.lon, reach)
    segs = np.unique(np.concatenate((segs, [center.segment])))
    seconds = costs.segment_weights(graph, costs.Options(penalty=1.0, rules=options.rules), segs)
    a_all = np.asarray(graph.seg_a[segs], dtype=np.int64)
    b_all = np.asarray(graph.seg_b[segs], dtype=np.int64)
    oneway = np.asarray(graph.seg_oneway[segs])
    nodes, inverse = np.unique(np.concatenate((a_all, b_all)), return_inverse=True)
    la, lb = inverse[:len(segs)], inverse[len(segs):]
    forward, backward = oneway >= 0, oneway <= 0
    src = np.concatenate((la[forward], lb[backward]))
    dst = np.concatenate((lb[forward], la[backward]))
    weight = np.maximum(np.concatenate((seconds[forward], seconds[backward])), costs.MIN_WEIGHT)
    # Two roads between the same pair of intersections: the quicker one (a
    # sparse matrix would add them up).
    order = np.lexsort((weight, dst, src))
    src, dst, weight = src[order], dst[order], weight[order]
    first = np.ones(len(src), dtype=bool)
    first[1:] = (src[1:] != src[:-1]) | (dst[1:] != dst[:-1])
    size = len(nodes)
    matrix = csr_matrix((weight[first], (src[first], dst[first])), shape=(size, size))
    # Start from both ends of the center's segment, each with the part of the
    # segment it takes to get there.
    s = center.segment
    at = int(np.searchsorted(segs, s))
    length = float(graph.seg_len[s]) or 1.0
    to_a = seconds[at] * center.along / length
    to_b = seconds[at] * (length - center.along) / length
    a = int(np.searchsorted(nodes, int(graph.seg_a[s])))
    b = int(np.searchsorted(nodes, int(graph.seg_b[s])))
    times = dijkstra(matrix, directed=True, indices=[a, b], limit=target * SEARCH_LIMIT, min_only=False)
    times = np.minimum(times[0] + to_a, times[1] + to_b)
    reached = np.flatnonzero(np.isfinite(times) & (times < costs.EXCLUDED_WEIGHT))
    if len(reached) == 0:
        return []
    lat = np.asarray(graph.node_lat[nodes[reached]])
    lon = np.asarray(graph.node_lon[nodes[reached]])
    bearings = _bearing(center.lat, center.lon, lat, lon)
    width = 360.0 / count
    out = []
    for k in range(count):
        middle = width * k
        off = np.abs((bearings - middle + 180) % 360 - 180)
        inside = np.flatnonzero(off <= width / 2)
        if len(inside) == 0:
            continue
        best = int(inside[np.argmin(np.abs(times[reached[inside]] - target))])
        if times[reached[best]] < target * 0.4:
            continue  # nothing reaches out that far this way: a gap in the ring
        try:
            out.append(snap(graph, float(lat[best]), float(lon[best]), MIN_SNAP_M, shunned))
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
    shunned = lambda segments: costs.excluded_segments(graph, options, segments)  # noqa: E731
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
