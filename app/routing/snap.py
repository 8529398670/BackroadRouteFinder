"""Puts a clicked or searched point onto the nearest road: which segment, and
how far along it from its first end."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.routing.graph import Graph

# Nearest shape points to look at. Straight rural roads have few shape points,
# so the closest point is not always on the closest road; looking at several
# and measuring to the lines between them finds the right one.
CANDIDATES = 24


class OffNetwork(Exception):
    """No drivable road near enough to the point."""


@dataclass(frozen=True)
class Snap:
    segment: int
    along: float      # metres from the segment's a end
    lat: float        # the point on the road
    lon: float
    distance: float   # metres from the asked point to the road


def segment_along(graph: Graph, segment: int) -> np.ndarray:
    """Metres from the segment's a end at each of its shape points."""
    g0, g1 = int(graph.seg_g0[segment]), int(graph.seg_g1[segment])
    xy = graph.project(graph.geom_lat[g0:g1 + 1], graph.geom_lon[g0:g1 + 1])
    steps = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    along = np.concatenate(([0.0], np.cumsum(steps)))
    # Scale the flat-earth lengths to the segment's true (haversine) length.
    if along[-1] > 0:
        along *= graph.seg_len[segment] / along[-1]
    return along


def snap(graph: Graph, lat: float, lon: float, max_distance: float, shunned: np.ndarray | None = None) -> Snap:
    """shunned: per segment, roads to snap onto only when no other is within
    max_distance (the ones the request's rules rule out)."""
    point = graph.project(lat, lon)[0]
    count = min(CANDIDATES, len(graph.geom_lat))
    _, nearest = graph.tree.query(point, k=count)
    best: tuple[float, int, int, float] | None = None  # distance, segment, piece start index, fraction
    best_ok: tuple[float, int, int, float] | None = None  # the same, among roads not shunned
    for position in np.atleast_1d(nearest):
        position = int(position)
        segment = graph.segment_of_point(position)
        g0, g1 = int(graph.seg_g0[segment]), int(graph.seg_g1[segment])
        for start in (position - 1, position):
            if start < g0 or start + 1 > g1:
                continue
            ends = graph.project(graph.geom_lat[start:start + 2], graph.geom_lon[start:start + 2])
            piece = ends[1] - ends[0]
            length_sq = float(piece @ piece)
            fraction = 0.0 if length_sq == 0 else float(np.clip((point - ends[0]) @ piece / length_sq, 0.0, 1.0))
            closest = ends[0] + fraction * piece
            distance = float(np.hypot(*(point - closest)))
            if best is None or distance < best[0]:
                best = (distance, segment, start, fraction)
            if shunned is not None and not shunned[segment] and (best_ok is None or distance < best_ok[0]):
                best_ok = (distance, segment, start, fraction)
    if best_ok is not None and best_ok[0] <= max_distance:
        best = best_ok
    if best is None or best[0] > max_distance:
        raise OffNetwork(f"no road within {int(max_distance)} m")
    distance, segment, start, fraction = best
    along_points = segment_along(graph, segment)
    g0 = int(graph.seg_g0[segment])
    along = float(along_points[start - g0] + fraction * (along_points[start - g0 + 1] - along_points[start - g0]))
    lat_on = float(graph.geom_lat[start] + fraction * (graph.geom_lat[start + 1] - graph.geom_lat[start]))
    lon_on = float(graph.geom_lon[start] + fraction * (graph.geom_lon[start + 1] - graph.geom_lon[start]))
    return Snap(segment=segment, along=along, lat=lat_on, lon=lon_on, distance=distance)
