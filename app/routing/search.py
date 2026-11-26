"""The search: the cheapest way from one snapped point to the next.

It runs on directed edges, not intersections: a state is "has just driven
this edge", and moving to the next edge costs the turn between them
(turns.py) plus driving the new edge (costs.py). scipy's csgraph Dijkstra
does the work in C.

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

from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from app.routing import costs
from app.routing.graph import Graph
from app.routing.snap import Snap

Piece = tuple[int, float, float]
MAX_STARTS = 2


class NoRoute(Exception):
    """The destination cannot be reached from the start."""


@dataclass
class Weights:
    """A request's weights, worked out once and used for every leg. The
    matrix arrays have room at the end for the virtual start's row, which
    each leg fills in."""

    segment: np.ndarray
    indptr: np.ndarray
    indices: np.ndarray
    data: np.ndarray
    moves: int


def weights(graph: Graph, options: costs.Options) -> Weights:
    seg_weight = costs.segment_weights(graph, options)
    table = graph.turns
    moves = len(table.to)
    edges = len(graph.edge_seg)
    indices = np.empty(moves + MAX_STARTS, dtype=np.int32)
    indices[:moves] = table.to
    data = np.empty(moves + MAX_STARTS, dtype=np.float64)
    np.add(seg_weight[graph.edge_seg[table.to]], table.seconds, out=data[:moves])
    np.maximum(data[:moves], costs.MIN_WEIGHT, out=data[:moves])
    indptr = np.empty(edges + 2, dtype=np.int64)
    indptr[: edges + 1] = table.ptr
    indptr[edges + 1] = moves
    return Weights(segment=seg_weight, indptr=indptr, indices=indices, data=data, moves=moves)


def _per_metre(graph: Graph, w: Weights, segment: int) -> float:
    length = graph.seg_len[segment]
    return float(w.segment[segment] / length) if length > 0 else 0.0


def _starts(graph: Graph, w: Weights, at: Snap) -> list[tuple[int, float, Piece]]:
    """The edges a start point can drive along: (edge, cost of the rest of it, piece)."""
    s, x = at.segment, at.along
    length = float(graph.seg_len[s])
    rate = _per_metre(graph, w, s)
    out = []
    if graph.seg_fwd[s] >= 0:
        out.append((int(graph.seg_fwd[s]), rate * (length - x), (s, x, length)))
    if graph.seg_rev[s] >= 0:
        out.append((int(graph.seg_rev[s]), rate * x, (s, x, 0.0)))
    return out


def _ends(graph: Graph, w: Weights, at: Snap) -> list[tuple[int, float, Piece]]:
    """The edges a destination can be reached along: (edge, cost of the part
    of it beyond the destination, the piece actually driven)."""
    s, y = at.segment, at.along
    length = float(graph.seg_len[s])
    rate = _per_metre(graph, w, s)
    out = []
    if graph.seg_fwd[s] >= 0:
        out.append((int(graph.seg_fwd[s]), rate * (length - y), (s, 0.0, y)))
    if graph.seg_rev[s] >= 0:
        out.append((int(graph.seg_rev[s]), rate * y, (s, length, y)))
    return out


def leg(graph: Graph, w: Weights, start: Snap, end: Snap) -> tuple[list[Piece], float]:
    starts = _starts(graph, w, start)
    ends = _ends(graph, w, end)
    best_cost = np.inf
    best: tuple[list[Piece], float] | None = None

    # Both points on one segment, and it may be driven that way.
    if start.segment == end.segment:
        s = start.segment
        forward_ok = end.along >= start.along and graph.seg_fwd[s] >= 0
        backward_ok = end.along <= start.along and graph.seg_rev[s] >= 0
        if forward_ok or backward_ok:
            best_cost = _per_metre(graph, w, s) * abs(end.along - start.along)
            best = ([(s, start.along, end.along)], 0.0)

    # The virtual start: one more row, moving onto each start edge.
    edges = len(graph.edge_seg)
    count = len(starts)
    for index, (edge, cost, _) in enumerate(starts):
        w.indices[w.moves + index] = edge
        w.data[w.moves + index] = max(cost, costs.MIN_WEIGHT)
    w.indptr[edges + 1] = w.moves + count
    size = w.moves + count
    matrix = csr_matrix((w.data[:size], w.indices[:size], w.indptr), shape=(edges + 1, edges + 1), copy=False)
    distances, predecessors = dijkstra(matrix, directed=True, indices=edges, return_predecessors=True)

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
                s = int(graph.edge_seg[middle])
                length = float(graph.seg_len[s])
                pieces.append((s, length, 0.0) if graph.edge_rev[middle] else (s, 0.0, length))
            pieces.append(piece)
            delay = sum(graph.turns.cost(a, b) for a, b in zip(path[:-1], path[1:]))
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
