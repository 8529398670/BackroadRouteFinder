"""One routing request from start to finish: snap the waypoints, search each
leg with the request's weights, summarise. Optionally the fastest route too,
so the page can show what the backroads cost and what they save."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.routing import costs, search
from app.routing.graph import Graph
from app.routing.snap import OffNetwork, Snap, snap
from app.routing.summarize import Summary, summarize


class PointOffNetwork(Exception):
    """A waypoint with no road near it. index is its place in the list."""

    def __init__(self, index: int, message: str) -> None:
        super().__init__(message)
        self.index = index


@dataclass
class Planned:
    kind: str           # "backroads" or "fastest"
    summary: Summary


@dataclass
class Result:
    routes: list[Planned] = field(default_factory=list)
    snapped: list[Snap] = field(default_factory=list)
    milliseconds: int = 0


def snap_all(graph: Graph, points: list[tuple[float, float]], max_snap_m: float, options: costs.Options | None = None) -> list[Snap]:
    """Each point onto its road; with options, onto a road the rules do not
    rule out wherever one is near enough."""
    shunned = costs.excluded_segments(graph, options) if options is not None else None
    snapped: list[Snap] = []
    for index, (lat, lon) in enumerate(points):
        try:
            snapped.append(snap(graph, lat, lon, max_snap_m, shunned))
        except OffNetwork as error:
            raise PointOffNetwork(index, str(error)) from None
    return snapped


def plan(graph: Graph, points: list[tuple[float, float]], options: costs.Options, compare_fastest: bool, max_snap_m: float,
         chosen: bool = True, snapped: list[Snap] | None = None, reuse_factor: float = 1.0) -> Result:
    """chosen=False leaves out the route options asks for, planning only the
    fastest one for comparison (when another engine does the choosing).
    reuse_factor > 1 makes each leg shy of roads earlier legs drove (a loop)."""
    began = time.monotonic()
    if snapped is None:
        snapped = snap_all(graph, points, max_snap_m, options)
    result = Result(snapped=snapped)
    kinds = [("backroads" if options.penalty > 1 else "fastest", options)] if chosen else []
    if compare_fastest and options.penalty > 1:
        kinds.append(("fastest", options.fastest()))
    rating = costs.rating(graph, options)
    for kind, kind_options in kinds:
        weights = search.weights(graph, kind_options)
        pieces: list[search.Piece] = []
        delay = 0.0
        for number, (start, end) in enumerate(zip(snapped[:-1], snapped[1:])):
            if number and reuse_factor > 1:
                weights = search.weights(graph, kind_options, discourage=search.driven(pieces), factor=reuse_factor)
            leg_pieces, leg_delay = search.leg(graph, weights, start, end)
            pieces.extend(leg_pieces)
            delay += leg_delay
        result.routes.append(Planned(kind=kind, summary=summarize(graph, pieces, rating, delay)))
    result.milliseconds = int((time.monotonic() - began) * 1000)
    return result
