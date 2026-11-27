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


def bearing_deg(lat0, lon0, lat1, lon1) -> np.ndarray:
    """Degrees clockwise from north, from one point to the next (flat, which
    is exact enough over a few metres)."""
    lat0, lat1 = np.asarray(lat0, dtype=np.float64), np.asarray(lat1, dtype=np.float64)
    dx = ((np.asarray(lon1, dtype=np.float64) - np.asarray(lon0, dtype=np.float64) + 540.0) % 360.0 - 180.0) * np.cos(np.radians((lat0 + lat1) / 2))
    dy = lat1 - lat0
    return np.degrees(np.arctan2(dx, dy)) % 360.0


@dataclass
class Tables:
    """What pricing a move needs, all per edge, segment or node; disk arrays
    (build_stream step H) or in memory. edge_* sorted by start node, and
    node_edge_ptr[n] the first edge leaving node n."""
    edge_seg: np.ndarray
    edge_rev: np.ndarray
    edge_dst: np.ndarray
    node_edge_ptr: np.ndarray
    seg_class: np.ndarray
    seg_stop_a: np.ndarray
    seg_stop_b: np.ndarray
    node_signal: np.ndarray
    node_stop: np.ndarray
    node_degree: np.ndarray
    node_biggest: np.ndarray
    leaves: np.ndarray        # degrees: which way each edge leaves its start
    arrives: np.ndarray       # and arrives at its end


def _take(source: np.ndarray, index: np.ndarray) -> np.ndarray:
    """source[index], reading a disk array in order."""
    index = np.asarray(index)
    order = np.argsort(index, kind="stable")
    out = np.empty(len(index), dtype=source.dtype)
    out[order] = source[index[order]]
    return out


def moves(t: Tables, lo: int, hi: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Every move out of edges lo..hi-1: how many leave each edge, and each
    move's next edge and seconds, in order of the edge they leave."""
    edges = hi - lo
    from_ids = np.arange(lo, hi, dtype=np.int64)
    dst = np.asarray(t.edge_dst[lo:hi], dtype=np.int64)
    out_lo = _take(t.node_edge_ptr, dst)
    out_hi = _take(t.node_edge_ptr, dst + 1)
    counts = out_hi - out_lo
    move_from = np.repeat(from_ids, counts)
    offsets = np.arange(len(move_from), dtype=np.int64) - np.repeat(np.cumsum(counts) - counts, counts)
    move_to = np.repeat(out_lo, counts) + offsets
    del offsets

    seg_from_all = np.asarray(t.edge_seg[lo:hi], dtype=np.int64)
    rev_from_all = np.asarray(t.edge_rev[lo:hi], dtype=bool)
    arrives_all = np.asarray(t.arrives[lo:hi], dtype=np.float32)
    local = move_from - lo
    seg_from = seg_from_all[local]
    seg_to = _take(t.edge_seg, move_to).astype(np.int64)
    uturn = seg_to == seg_from
    dead_end = counts[local] == 1
    keep = ~uturn | dead_end
    move_from, move_to, uturn, local, seg_from, seg_to = move_from[keep], move_to[keep], uturn[keep], local[keep], seg_from[keep], seg_to[keep]

    node = dst[local]
    delta = (_take(t.leaves, move_to) - arrives_all[local] + 540.0) % 360.0 - 180.0
    angle = np.abs(delta)
    right = delta > 0
    straight = angle < STRAIGHT_DEG
    left = ~right & ~straight

    turn = np.select(
        [straight, angle < SLIGHT_DEG, angle < SHARP_DEG],
        [0.0, SLIGHT_S, np.where(right, RIGHT_S, LEFT_S)],
        np.where(right, SHARP_RIGHT_S, SHARP_LEFT_S),
    )
    from_class = _take(t.seg_class, seg_from)
    to_class = _take(t.seg_class, seg_to)
    from_rank = classes.RANKS[from_class]
    to_rank = classes.RANKS[to_class]
    big = _take(t.node_biggest, node)
    merge = classes.LINKS[from_class] & classes.MERGE_TARGETS[to_class]
    stop_end = np.where(rev_from_all[local], _take(t.seg_stop_a, seg_from), _take(t.seg_stop_b, seg_from))
    stop_sign = _take(t.node_stop, node) | stop_end
    signal = _take(t.node_signal, node)
    gives_way = (from_rank < big) & ~merge
    gap = np.where(right, 0.0, np.where(big >= MAJOR_RANK, GAP_MAJOR_S, GAP_MINOR_S))
    stays_on_top = (from_rank == big) & (to_rank == big) & straight

    control = np.select(
        [signal, stop_sign, merge, gives_way, big <= MINOR_RANK, stays_on_top],
        [SIGNAL_S + np.where(left, SIGNAL_LEFT_S, 0.0), STOP_S + gap, 0.0, STOP_S + gap, CAUTION_S, 0.0],
        0.0,
    )
    junction = _take(t.node_degree, node) >= 3
    bend = np.where(angle >= SLIGHT_DEG, BEND_S, 0.0)
    seconds = np.where(junction, turn + control, bend)
    seconds = np.where(uturn, UTURN_S, seconds)
    out_counts = np.bincount(move_from - lo, minlength=edges).astype(np.int64)
    return out_counts, move_to.astype(np.int32), seconds.astype(np.float32)
