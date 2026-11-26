"""A found path, as the page shows it: totals, how much of it is on main
roads (any road the request's rules avoid: costs.avoided_segments), the line split into main and backroad runs for colouring, and the
roads it follows."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.routing.costs import Rating
from app.routing.graph import Graph
from app.routing.search import Piece
from app.routing.snap import segment_along

# A road shorter than this in the "via" list is folded into its neighbours.
VIA_MIN_METRES = 150.0
DECIMALS = 5


@dataclass
class Run:
    main: bool
    coords: list[list[float]] = field(default_factory=list)


@dataclass
class Via:
    name: str
    main: bool
    meters: float
    seconds: float


@dataclass
class Summary:
    seconds: float = 0.0
    meters: float = 0.0
    main_seconds: float = 0.0
    main_meters: float = 0.0
    unpaved_meters: float = 0.0
    # What the avoided roads add to the score: each second on one counts its
    # road type's avoid strength (costs.Rating.factor) less the second itself.
    extra_seconds: float = 0.0
    # Of seconds: turning, and waiting at signals and stop signs.
    delay_seconds: float = 0.0
    runs: list[Run] = field(default_factory=list)
    via: list[Via] = field(default_factory=list)


def _piece_coords(graph: Graph, s: int, start: float, end: float) -> list[list[float]]:
    """The shape of one piece, in the direction driven."""
    lo, hi = min(start, end), max(start, end)
    g0, g1 = int(graph.seg_g0[s]), int(graph.seg_g1[s])
    lat = graph.geom_lat[g0:g1 + 1]
    lon = graph.geom_lon[g0:g1 + 1]
    along = segment_along(graph, s)
    inside = (along > lo) & (along < hi)
    lats = np.concatenate(([np.interp(lo, along, lat)], lat[inside], [np.interp(hi, along, lat)]))
    lons = np.concatenate(([np.interp(lo, along, lon)], lon[inside], [np.interp(hi, along, lon)]))
    points = np.column_stack((np.round(lats, DECIMALS), np.round(lons, DECIMALS))).tolist()
    return points[::-1] if end < start else points


def summarize(graph: Graph, pieces: list[Piece], rating: Rating, delay_seconds: float = 0.0) -> Summary:
    """rating: what the request makes of each segment (costs.rating)."""
    out = Summary(seconds=delay_seconds, delay_seconds=delay_seconds)
    via: list[Via] = []
    for s, start, end in pieces:
        metres = abs(end - start)
        if metres <= 0:
            continue
        length = graph.seg_len[s]
        seconds = float(graph.seg_seconds[s] * metres / length) if length > 0 else 0.0
        main = bool(rating.avoided[s])
        out.extra_seconds += seconds * (float(rating.factor[s]) - 1.0)
        out.seconds += seconds
        out.meters += metres
        if main:
            out.main_seconds += seconds
            out.main_meters += metres
        if graph.seg_unpaved[s]:
            out.unpaved_meters += metres

        coords = _piece_coords(graph, s, start, end)
        if out.runs and out.runs[-1].main == main:
            run = out.runs[-1]
            run.coords.extend(coords[1:] if run.coords and run.coords[-1] == coords[0] else coords)
        else:
            # A new run starts where the last one ended, so the line has no gap.
            joined = ([out.runs[-1].coords[-1]] if out.runs and out.runs[-1].coords[-1] != coords[0] else []) + coords
            out.runs.append(Run(main=main, coords=joined))

        name = graph.names[int(graph.seg_name[s])]
        if via and via[-1].name == name:
            via[-1].meters += metres
            via[-1].seconds += seconds
            via[-1].main = via[-1].main or main
        else:
            via.append(Via(name=name, main=main, meters=metres, seconds=seconds))

    out.via = _fold(via)
    return out


def _fold(via: list[Via]) -> list[Via]:
    """Drops the short bits (a few metres of a cross street at a jog) and
    joins the neighbours they separated, if those are the same road."""
    if len(via) <= 1:
        return via
    kept: list[Via] = []
    for item in via:
        if item.meters < VIA_MIN_METRES:
            continue
        if kept and kept[-1].name == item.name:
            kept[-1].meters += item.meters
            kept[-1].seconds += item.seconds
            kept[-1].main = kept[-1].main or item.main
        else:
            kept.append(item)
    return kept or via[:1]
