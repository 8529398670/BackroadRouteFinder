"""Turn costs: what it costs, in seconds, to go from one road onto the next at
an intersection. Without them every route is as if each crossing were green
and empty, which flatters exactly the routes this app prefers: a backroads
trip turns and stops far more often than one down a state route.

The search runs on directed edges (driving along a segment in one direction),
and a turn is a move from one edge to the next, so its cost can depend on
both roads. Each turn's cost is the sum of:

- the angle: straight on 0, slight 2 s, right 5 s, left 8 s, sharp right
  10 s, sharp left 14 s (slowing down, waiting for oncoming traffic);
- the control, at a real intersection (three or more roads):
  - traffic signals: 12 s on average, 16 s turning left;
  - a stop sign (mapped, or implied because this road meets a bigger one):
    6 s, plus a wait for a gap when crossing or turning left onto a busy
    road (5 s onto a primary or bigger, 2 s otherwise);
  - two small roads meeting with nothing mapped: 2 s of caution;
  - staying on the bigger road, or merging from a ramp onto a freeway: 0.
- at a bend where one way simply continues as another (two roads), 3 s for
  a corner of 60 degrees or more, else nothing.

A U-turn is allowed only at a dead end (30 s): anywhere else the route has to
go round the block, the way a driver would.

These are averages for planning, not a traffic model. They are numbers to
tune: TODO.md lists better sources (mapped turn restrictions, signal timing).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.routing import classes

STRAIGHT_DEG = 30.0
SLIGHT_DEG = 60.0
SHARP_DEG = 150.0

SLIGHT_S = 2.0
RIGHT_S = 5.0
LEFT_S = 8.0
SHARP_RIGHT_S = 10.0
SHARP_LEFT_S = 14.0
UTURN_S = 30.0
BEND_S = 3.0

SIGNAL_S = 12.0
SIGNAL_LEFT_S = 4.0
STOP_S = 6.0
GAP_MAJOR_S = 5.0
GAP_MINOR_S = 2.0
MAJOR_RANK = 5
CAUTION_S = 2.0
MINOR_RANK = 2


@dataclass
class Turns:
    """The moves between edges, as a sparse matrix in CSR layout: the moves
    out of edge e are ptr[e]..ptr[e+1]-1, each onto edge to[i], costing
    seconds[i]."""

    ptr: np.ndarray
    to: np.ndarray
    seconds: np.ndarray

    def cost(self, from_edge: int, to_edge: int) -> float:
        row = slice(int(self.ptr[from_edge]), int(self.ptr[from_edge + 1]))
        hits = np.flatnonzero(self.to[row] == to_edge)
        return float(self.seconds[row][hits[0]]) if len(hits) else 0.0


def _bearing(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
    """Degrees clockwise from north."""
    return np.degrees(np.arctan2(dx, dy))


def build(
    *,
    node_count: int,
    seg_a: np.ndarray, seg_b: np.ndarray, seg_class: np.ndarray,
    seg_g0: np.ndarray, seg_g1: np.ndarray, seg_stop_a: np.ndarray, seg_stop_b: np.ndarray,
    node_signal: np.ndarray, node_stop: np.ndarray,
    geom_x: np.ndarray, geom_y: np.ndarray,
    edge_seg: np.ndarray, edge_rev: np.ndarray, edge_src: np.ndarray, edge_dst: np.ndarray,
) -> Turns:
    """edge_* must be sorted by edge_src."""
    edges = len(edge_seg)

    # Which way each edge leaves its start and arrives at its end.
    g0, g1 = seg_g0[edge_seg], seg_g1[edge_seg]
    first = _bearing(geom_x[g0 + 1] - geom_x[g0], geom_y[g0 + 1] - geom_y[g0])
    last = _bearing(geom_x[g1] - geom_x[g1 - 1], geom_y[g1] - geom_y[g1 - 1])
    leaves = np.where(edge_rev, (last + 180.0) % 360.0, first).astype(np.float32)
    arrives = np.where(edge_rev, (first + 180.0) % 360.0, last).astype(np.float32)
    del g0, g1, first, last

    # Each intersection: how many roads meet there, and the biggest of them.
    degree = np.bincount(seg_a, minlength=node_count) + np.bincount(seg_b, minlength=node_count)
    seg_rank = classes.RANKS[seg_class].astype(np.int16)
    biggest = np.zeros(node_count, dtype=np.int16)
    np.maximum.at(biggest, seg_a, seg_rank)
    np.maximum.at(biggest, seg_b, seg_rank)

    # Every move: from edge e onto each edge leaving the node e arrives at.
    out_ptr = np.zeros(node_count + 1, dtype=np.int64)
    np.cumsum(np.bincount(edge_src, minlength=node_count), out=out_ptr[1:])
    counts = out_ptr[edge_dst + 1] - out_ptr[edge_dst]
    move_from = np.repeat(np.arange(edges, dtype=np.int64), counts)
    offsets = np.arange(len(move_from), dtype=np.int64) - np.repeat(np.cumsum(counts) - counts, counts)
    move_to = out_ptr[edge_dst[move_from]] + offsets
    del offsets

    uturn = edge_seg[move_to] == edge_seg[move_from]
    dead_end = counts[move_from] == 1
    keep = ~uturn | dead_end
    move_from, move_to, uturn = move_from[keep], move_to[keep], uturn[keep]
    del dead_end, keep
    # Halves the memory of everything below; a turn's seconds need no more.
    move_from = move_from.astype(np.int32)
    move_to = move_to.astype(np.int32)

    node = edge_dst[move_from]
    delta = (leaves[move_to] - arrives[move_from] + 540.0) % 360.0 - 180.0
    angle = np.abs(delta)
    right = delta > 0
    straight = angle < STRAIGHT_DEG
    left = ~right & ~straight

    turn = np.select(
        [straight, angle < SLIGHT_DEG, angle < SHARP_DEG],
        [0.0, SLIGHT_S, np.where(right, RIGHT_S, LEFT_S)],
        np.where(right, SHARP_RIGHT_S, SHARP_LEFT_S),
    )

    from_class = seg_class[edge_seg[move_from]]
    to_class = seg_class[edge_seg[move_to]]
    from_rank = classes.RANKS[from_class]
    to_rank = classes.RANKS[to_class]
    big = biggest[node]
    merge = classes.LINKS[from_class] & classes.MERGE_TARGETS[to_class]
    seg_from = edge_seg[move_from]
    stop_sign = node_stop[node] | np.where(edge_rev[move_from], seg_stop_a[seg_from], seg_stop_b[seg_from])
    signal = node_signal[node]
    gives_way = (from_rank < big) & ~merge
    gap = np.where(right, 0.0, np.where(big >= MAJOR_RANK, GAP_MAJOR_S, GAP_MINOR_S))
    stays_on_top = (from_rank == big) & (to_rank == big) & straight

    control = np.select(
        [signal, stop_sign, merge, gives_way, big <= MINOR_RANK, stays_on_top],
        [SIGNAL_S + np.where(left, SIGNAL_LEFT_S, 0.0), STOP_S + gap, 0.0, STOP_S + gap, CAUTION_S, 0.0],
        0.0,
    )
    junction = degree[node] >= 3
    bend = np.where(angle >= SLIGHT_DEG, BEND_S, 0.0)
    seconds = np.where(junction, turn + control, bend)
    seconds = np.where(uturn, UTURN_S, seconds)

    ptr = np.zeros(edges + 1, dtype=np.int64)
    np.cumsum(np.bincount(move_from, minlength=edges), out=ptr[1:])
    return Turns(ptr=ptr, to=move_to.astype(np.int32), seconds=seconds.astype(np.float32))
