"""Puts a clicked or searched point onto the nearest road: which segment, and
how far along it from its first end."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from app.routing import grid
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


def snap(graph: Graph, lat: float, lon: float, max_distance: float, shunned: Shunned | None = None) -> Snap:
    """shunned: roads to snap onto only when no other is within max_distance
    (the ones the request's rules rule out)."""
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
    if distance[pick] > max_distance:
        raise OffNetwork(f"no road within {int(max_distance)} m")
    segment, start, frac = int(owner[pick]), int(starts[pick]), float(fraction[pick])
    along_points = segment_along(graph, segment)
    g0 = int(graph.seg_g0[segment])
    along = float(along_points[start - g0] + frac * (along_points[start - g0 + 1] - along_points[start - g0]))
    lat_on = float(a_lat[pick] + frac * (b_lat[pick] - a_lat[pick]))
    lon_on = float(a_lon[pick] + frac * (b_lon[pick] - a_lon[pick]))
    return Snap(segment=segment, along=along, lat=lat_on, lon=lon_on, distance=float(distance[pick]))
