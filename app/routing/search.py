"""The search: the cheapest way from one snapped point to the next.

It runs on directed edges, not intersections: a state is "has just driven
this edge", and moving to the next edge costs the turn between them
(turns.py) plus driving the new edge (costs.py). scipy's csgraph Dijkstra
does the work in C.

It never runs on the whole map. A Searcher takes the corridor round the
route's points (grid.py: every road within some distance of the straight
lines between them), reads only those roads' edges and turns from disk, and
searches that. A leg with no way through its corridor tries again in a wider
one (WIDTHS), so a route that has to swing far round a river or a mountain
still comes out, just slower. That is what keeps a country's map out of
memory: a request holds the roads it might use, not all of them.

A start point sits partway along a segment, so the search starts from a
virtual state (one extra row in the matrix) with a move onto each direction
the start's segment may be driven, costing the part of it still ahead. The
destination is reached by driving into its segment's edge and stopping short:
the cost of reaching that edge, less the part of it not driven.

A path is a list of pieces: (segment, from_along, to_along) in metres from
the segment's a end, in the order driven. A whole segment driven a->b is
(s, 0, L); driven b->a it is (s, L, 0). Each leg also says how many seconds
its turns and stops took.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from app.routing import costs, grid
from app.routing.disk import ranges
from app.routing.graph import Graph
from app.routing.snap import Snap

Piece = tuple[int, float, float]
MAX_STARTS = 2
EARTH_RADIUS_M = 6_371_008.8

# The corridor's half-width: a share of the longest leg, at least MIN and at
# most MAX metres; each try after the first multiplies both by the next step.
CORRIDOR_SHARE = 0.25
CORRIDOR_MIN_M = 6_000.0
CORRIDOR_MAX_M = 60_000.0
WIDTHS = (1.0, 2.5, 6.0)
# Within a corridor, a first search stops at the cost of driving the straight
# line at this speed (metres a second) plus a margin; one that does not get
# there searches the whole corridor.
LIMIT_SPEED = 4.0
LIMIT_MARGIN_S = 600.0


class NoRoute(Exception):
    """The destination cannot be reached from the start."""


def driven(pieces: list[Piece]) -> np.ndarray:
    """The segments a path drives (to count more on later legs)."""
    return np.unique(np.fromiter((p[0] for p in pieces), dtype=np.int64, count=len(pieces)))


def straight_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(h, 1.0)))


class Sub:
    """One corridor of the map, ready to search: its segments, edges and
    turns renumbered from 0, as a sparse matrix with room for the virtual
    start's row."""

    def __init__(self, graph: Graph, options: costs.Options, segments: np.ndarray) -> None:
        self.graph = graph
        self.segs = segments                                    # sorted global segment ids
        fwd = np.asarray(graph.seg_fwd[segments], dtype=np.int64)
        rev = np.asarray(graph.seg_rev[segments], dtype=np.int64)
        self.edges = np.sort(np.concatenate((fwd[fwd >= 0], rev[rev >= 0])))     # global edge ids
        n = len(self.edges)
        self.edge_seg = np.asarray(graph.edge_seg[self.edges], dtype=np.int64)
        self.edge_rev = np.asarray(graph.edge_rev[self.edges], dtype=bool)
        self.edge_seg_local = np.searchsorted(self.segs, self.edge_seg)
        p0 = np.asarray(graph.turns.ptr[self.edges], dtype=np.int64)
        p1 = np.asarray(graph.turns.ptr[self.edges + 1], dtype=np.int64)
        idx = ranges(p0, p1)
        to_global = np.asarray(graph.turns.to[idx])
        seconds = np.asarray(graph.turns.seconds[idx], dtype=np.float64)
        row = np.repeat(np.arange(n, dtype=np.int64), p1 - p0)
        # Global edge id to local: a lookup table over the span of edges the
        # corridor holds when that is small enough, else a binary search.
        lo_e, hi_e = (int(self.edges[0]), int(self.edges[-1])) if n else (0, -1)
        if hi_e - lo_e < 16_000_000:
            table = np.full(hi_e - lo_e + 1, -1, dtype=np.int32)
            table[self.edges - lo_e] = np.arange(n, dtype=np.int32)
            inside = (to_global >= lo_e) & (to_global <= hi_e)
            local = np.full(len(to_global), -1, dtype=np.int64)
            local[inside] = table[to_global[inside] - lo_e]
            inside &= local >= 0
            del table
        else:
            local = np.searchsorted(self.edges, to_global)
            inside = local < n
            inside[inside] = self.edges[local[inside]] == to_global[inside]
        row, local, seconds = row[inside], local[inside], seconds[inside]
        self.moves = len(local)
        self.indptr = np.zeros(n + 2, dtype=np.int64)
        np.cumsum(np.bincount(row, minlength=n), out=self.indptr[1:n + 1])
        self.indptr[n + 1] = self.moves
        self.indices = np.empty(self.moves + MAX_STARTS, dtype=np.int32)
        self.indices[:self.moves] = local
        self.turn_seconds = seconds
        self.base_weight = costs.segment_weights(graph, options, segments)
        self.weight = self.base_weight
        self.data = np.empty(self.moves + MAX_STARTS, dtype=np.float64)
        self._fill()

    def _fill(self) -> None:
        np.add(self.weight[self.edge_seg_local[self.indices[:self.moves]]], self.turn_seconds, out=self.data[:self.moves])
        np.maximum(self.data[:self.moves], costs.MIN_WEIGHT, out=self.data[:self.moves])

    def scale(self, segments: np.ndarray | None, factors: np.ndarray | float | None) -> None:
        """Count these segments factors times over (roads a loop already
        drove, or earlier alternatives used); None goes back to plain."""
        self.weight = self.base_weight
        if segments is not None and len(segments):
            where = np.searchsorted(self.segs, segments)
            ok = (where < len(self.segs))
            ok[ok] = self.segs[where[ok]] == np.asarray(segments)[ok]
            self.weight = self.base_weight.copy()
            f = factors if np.isscalar(factors) else np.asarray(factors)[ok]
            self.weight[where[ok]] *= f
        self._fill()

    def has(self, segment: int) -> bool:
        at = int(np.searchsorted(self.segs, segment))
        return at < len(self.segs) and int(self.segs[at]) == segment

    def local_edge(self, global_edge: int) -> int:
        at = int(np.searchsorted(self.edges, global_edge))
        return at if at < len(self.edges) and int(self.edges[at]) == global_edge else -1

    def per_metre(self, segment: int) -> float:
        length = float(self.graph.seg_len[segment])
        at = int(np.searchsorted(self.segs, segment))
        return float(self.weight[at] / length) if length > 0 else 0.0


class Searcher:
    """Searches the legs of one route (or loop) in the corridor round its
    points, widening it for a leg that needs more room."""

    def __init__(self, graph: Graph, options: costs.Options, points: list[Snap]) -> None:
        self.graph = graph
        self.options = options
        self.points = [(p.lat, p.lon) for p in points]
        self.longest = max((straight_m(*a, *b) for a, b in zip(self.points[:-1], self.points[1:])), default=0.0)
        self.subs: list[Sub | None] = [None] * len(WIDTHS)
        self._scale: tuple[np.ndarray | None, np.ndarray | float | None] = (None, None)
        self.extra = np.array([p.segment for p in points], dtype=np.int64)

    def width(self, step: int) -> float:
        k = WIDTHS[step]
        return min(max(CORRIDOR_MIN_M, CORRIDOR_SHARE * self.longest) * k, CORRIDOR_MAX_M * k)

    def sub(self, step: int) -> Sub:
        found = self.subs[step]
        if found is None:
            segments = grid.corridor_segments(self.graph, self.points, self.width(step))
            segments = grid.distinct(np.concatenate((segments, self.extra)), self.graph.segment_count)
            found = self.subs[step] = Sub(self.graph, self.options, segments)
            # A wider corridor replaces the narrower ones.
            for smaller in range(step):
                self.subs[smaller] = None
        if self._scale[0] is not None:
            found.scale(*self._scale)
        return found

    def scale(self, segments: np.ndarray | None, factors: np.ndarray | float | None) -> None:
        self._scale = (segments, factors)
        for sub in self.subs:
            if sub is not None:
                sub.scale(segments, factors)

    def leg(self, start: Snap, end: Snap) -> tuple[list[Piece], float]:
        metres = straight_m(start.lat, start.lon, end.lat, end.lon)
        first = next((i for i, s in enumerate(self.subs) if s is not None), 0)
        for step in range(first, len(WIDTHS)):
            sub = self.sub(step)
            for limit in (metres / LIMIT_SPEED + LIMIT_MARGIN_S, np.inf):
                try:
                    return _leg(sub, start, end, limit)
                except NoRoute:
                    continue
        raise NoRoute("no road connects these points")


def _starts(sub: Sub, at: Snap) -> list[tuple[int, float, Piece]]:
    """The edges a start point can drive along: (local edge, cost of the rest of it, piece)."""
    graph = sub.graph
    s, x = at.segment, at.along
    length = float(graph.seg_len[s])
    rate = sub.per_metre(s)
    out = []
    fwd, rev = int(graph.seg_fwd[s]), int(graph.seg_rev[s])
    if fwd >= 0 and sub.local_edge(fwd) >= 0:
        out.append((sub.local_edge(fwd), rate * (length - x), (s, x, length)))
    if rev >= 0 and sub.local_edge(rev) >= 0:
        out.append((sub.local_edge(rev), rate * x, (s, x, 0.0)))
    return out


def _ends(sub: Sub, at: Snap) -> list[tuple[int, float, Piece]]:
    """The edges a destination can be reached along: (local edge, cost of the
    part of it beyond the destination, the piece actually driven)."""
    graph = sub.graph
    s, y = at.segment, at.along
    length = float(graph.seg_len[s])
    rate = sub.per_metre(s)
    out = []
    fwd, rev = int(graph.seg_fwd[s]), int(graph.seg_rev[s])
    if fwd >= 0 and sub.local_edge(fwd) >= 0:
        out.append((sub.local_edge(fwd), rate * (length - y), (s, 0.0, y)))
    if rev >= 0 and sub.local_edge(rev) >= 0:
        out.append((sub.local_edge(rev), rate * y, (s, length, y)))
    return out


def _leg(sub: Sub, start: Snap, end: Snap, limit: float) -> tuple[list[Piece], float]:
    graph = sub.graph
    starts = _starts(sub, start)
    ends = _ends(sub, end)
    best_cost = np.inf
    best: tuple[list[Piece], float] | None = None

    # Both points on one segment, and it may be driven that way.
    if start.segment == end.segment:
        s = start.segment
        forward_ok = end.along >= start.along and graph.seg_fwd[s] >= 0
        backward_ok = end.along <= start.along and graph.seg_rev[s] >= 0
        if forward_ok or backward_ok:
            best_cost = sub.per_metre(s) * abs(end.along - start.along)
            best = ([(s, start.along, end.along)], 0.0)

    edges = len(sub.edges)
    count = len(starts)
    for index, (edge, cost, _) in enumerate(starts):
        sub.indices[sub.moves + index] = edge
        sub.data[sub.moves + index] = max(cost, costs.MIN_WEIGHT)
    sub.indptr[edges + 1] = sub.moves + count
    size = sub.moves + count
    matrix = csr_matrix((sub.data[:size], sub.indices[:size], sub.indptr), shape=(edges + 1, edges + 1), copy=False)
    distances, predecessors = dijkstra(matrix, directed=True, indices=edges, return_predecessors=True, limit=limit)

    start_edges = {edge: piece for edge, _, piece in starts}
    for edge, beyond, piece in ends:
        # A start edge's own distance is only where the trip begins; driving
        # it to a point behind the start is the one-segment case above.
        if edge in start_edges:
            continue
        total = distances[edge] - beyond
        if np.isfinite(distances[edge]) and total < best_cost:
            path = _walk(predecessors, edges, edge)
            pieces = [start_edges[path[0]]]
            for middle in path[1:-1]:
                s = int(sub.edge_seg[middle])
                length = float(graph.seg_len[s])
                pieces.append((s, length, 0.0) if sub.edge_rev[middle] else (s, 0.0, length))
            pieces.append(piece)
            glob = sub.edges[path]
            delay = sum(graph.turns.cost(int(a), int(b)) for a, b in zip(glob[:-1], glob[1:]))
            best_cost = total
            best = (pieces, delay)
    if best is None:
        raise NoRoute("no road connects these points")
    return best


def _walk(predecessors: np.ndarray, virtual: int, target: int) -> list[int]:
    """The edges driven, first to last, ending at target."""
    path = [target]
    while True:
        previous = int(predecessors[path[-1]])
        if previous == virtual:
            break
        if previous < 0:
            raise NoRoute("no road connects these points")
        path.append(previous)
    path.reverse()
    return path
