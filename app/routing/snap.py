"""Puts a clicked or searched point onto the nearest road: which segment, and
how far along it from its first end."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from app.routing import classes, grid
from app.routing.disk import ranges
from app.routing.graph import Graph


class OffNetwork(Exception):
    """No drivable road near enough to the point."""


@dataclass(frozen=True)
class Snap:
    segment: int
    along: float      # metres from the segment's a end
    lat: float        # the point on the road
    lon: float
    distance: float   # metres from the asked point to the road
    # A point only there to steer the route (a nudge, a loop's ring point),
    # not a stop of the person's own: the route may fold away a spur out to
    # it and back (router._fold).
    soft: bool = field(default=False, compare=False)


# A soft point takes a through road (not a service road, no dead end at
# either end) up to this much further away than the nearest road.
SOFT_MARGIN_M = 50.0


def dead_ends(graph: Graph, nodes: np.ndarray) -> np.ndarray:
    """Per node: a dead end, at most one way to drive on from it (a node
    between a oneway in and a oneway out counts too, which only matters for
    preferring through roads)."""
    nodes = np.asarray(nodes, dtype=np.int64)
    ptr = graph.node_edge_ptr
    return (np.asarray(ptr[nodes + 1], dtype=np.int64) - np.asarray(ptr[nodes], dtype=np.int64)) <= 1


def through_segments(graph: Graph, segments: np.ndarray) -> np.ndarray:
    """Per segment: a through road, neither a service road nor a dead end."""
    segments = np.asarray(segments, dtype=np.int64)
    if not len(segments):
        return np.zeros(0, dtype=bool)
    service = np.asarray(graph.seg_class[segments]) == classes.SERVICE
    a = dead_ends(graph, np.asarray(graph.seg_a[segments], dtype=np.int64))
    b = dead_ends(graph, np.asarray(graph.seg_b[segments], dtype=np.int64))
    return ~service & ~a & ~b


# Rules out roads for snapping: given segment ids, which of them to avoid.
Shunned = Callable[[np.ndarray], np.ndarray]


def segment_along(graph: Graph, segment: int) -> np.ndarray:
    """Metres from the segment's a end at each of its shape points."""
    g0, g1 = int(graph.seg_g0[segment]), int(graph.seg_g1[segment])
    lat = np.asarray(graph.geom_lat[g0:g1 + 1])
    xy = graph.project(lat, graph.geom_lon[g0:g1 + 1], float(lat[0]))
    steps = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    along = np.concatenate(([0.0], np.cumsum(steps)))
    # Scale the flat-earth lengths to the segment's true (haversine) length.
    if along[-1] > 0:
        along *= graph.seg_len[segment] / along[-1]
    return along


def pieces_of(graph: Graph, segments: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Every piece (shape point to the next) of these segments: the index of
    its first shape point, and its segment."""
    g0 = np.asarray(graph.seg_g0[segments], dtype=np.int64)
    g1 = np.asarray(graph.seg_g1[segments], dtype=np.int64)
    starts = ranges(g0, g1)
    owner = np.repeat(np.asarray(segments, dtype=np.int64), g1 - g0)
    return starts, owner


def snap(graph: Graph, lat: float, lon: float, max_distance: float, shunned: Shunned | None = None,
         soft: bool = False, margin: float = SOFT_MARGIN_M) -> Snap:
    """shunned: roads to snap onto only when no other is within max_distance
    (the ones the request's rules rule out). soft: a point only there to
    steer (Snap.soft), which takes a through road up to margin metres further
    than the nearest, so it does not pull the route down a driveway."""
    candidates = grid.segments_near(graph, lat, lon, max_distance)
    if not len(candidates):
        raise OffNetwork(f"no road within {int(max_distance)} m")
    starts, owner = pieces_of(graph, candidates)
    order = np.argsort(starts)
    starts, owner = starts[order], owner[order]
    a_lat, a_lon = np.asarray(graph.geom_lat[starts]), np.asarray(graph.geom_lon[starts])
    b_lat, b_lon = np.asarray(graph.geom_lat[starts + 1]), np.asarray(graph.geom_lon[starts + 1])
    point = graph.project(lat, lon, lat)[0]
    a = graph.project(a_lat, a_lon, lat)
    b = graph.project(b_lat, b_lon, lat)
    piece = b - a
    length_sq = (piece ** 2).sum(axis=1)
    fraction = np.clip(((point - a) * piece).sum(axis=1) / np.where(length_sq == 0, 1, length_sq), 0.0, 1.0)
    fraction = np.where(length_sq == 0, 0.0, fraction)
    closest = a + fraction[:, None] * piece
    distance = np.hypot(*(point - closest).T)
    pick = int(np.argmin(distance))
    if shunned is not None:
        allowed = ~np.asarray(shunned(owner), dtype=bool)
        if allowed.any():
            ok = np.where(allowed, distance, np.inf)
            pick_ok = int(np.argmin(ok))
            if ok[pick_ok] <= max_distance:
                pick = pick_ok
    else:
        allowed = np.ones(len(owner), dtype=bool)
    if soft:
        through = allowed & through_segments(graph, owner)
        if through.any():
            near = np.where(through, distance, np.inf)
            pick_through = int(np.argmin(near))
            if near[pick_through] <= min(max_distance, distance[pick] + margin):
                pick = pick_through
    if distance[pick] > max_distance:
        raise OffNetwork(f"no road within {int(max_distance)} m")
    segment, start, frac = int(owner[pick]), int(starts[pick]), float(fraction[pick])
    along_points = segment_along(graph, segment)
    g0 = int(graph.seg_g0[segment])
    along = float(along_points[start - g0] + frac * (along_points[start - g0 + 1] - along_points[start - g0]))
    lat_on = float(a_lat[pick] + frac * (b_lat[pick] - a_lat[pick]))
    lon_on = float(a_lon[pick] + frac * (b_lon[pick] - a_lon[pick]))
    return Snap(segment=segment, along=along, lat=lat_on, lon=lon_on, distance=float(distance[pick]), soft=soft)
