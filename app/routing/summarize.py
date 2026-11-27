"""A found path, as the page shows it: totals, how much of it is on main
roads (any road the request's rules avoid: costs.avoided_segments), the line split into main and backroad runs for colouring, and the
roads it follows."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.routing.costs import Rating
from app.routing.graph import Graph
from app.routing.search import Piece

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


def _piece_coords(lat: np.ndarray, lon: np.ndarray, along: np.ndarray, start: float, end: float) -> list[list[float]]:
    """The shape of one piece, in the direction driven: lat/lon are the
    segment's shape points, along their distance from its a end."""
    lo, hi = min(start, end), max(start, end)
    inside = (along > lo) & (along < hi)
    lats = np.concatenate(([np.interp(lo, along, lat)], lat[inside], [np.interp(hi, along, lat)]))
    lons = np.concatenate(([np.interp(lo, along, lon)], lon[inside], [np.interp(hi, along, lon)]))
    points = np.column_stack((np.round(lats, DECIMALS), np.round(lons, DECIMALS))).tolist()
    return points[::-1] if end < start else points


def _alongs(lat: np.ndarray, lon: np.ndarray, length: float) -> np.ndarray:
    """Metres from the first shape point at each one, scaled to the true length."""
    cos0 = np.cos(np.radians(lat[0]))
    steps = np.hypot(np.diff(lon) * cos0, np.diff(lat)) * 111_320.0
    along = np.concatenate(([0.0], np.cumsum(steps)))
    if along[-1] > 0:
        along *= length / along[-1]
    return along


def summarize(graph: Graph, pieces: list[Piece], rating: Rating, delay_seconds: float = 0.0) -> Summary:
    """rating: what the request makes of each segment (costs.rating)."""
    out = Summary(seconds=delay_seconds, delay_seconds=delay_seconds)
    via: list[Via] = []
    if not pieces:
        return out
    # Everything per segment read at once, in disk order.
    segs = np.fromiter((p[0] for p in pieces), dtype=np.int64, count=len(pieces))
    uniq, back = np.unique(segs, return_inverse=True)
    seg_len = np.asarray(graph.seg_len[uniq], dtype=np.float64)[back]
    seg_seconds = np.asarray(graph.seg_seconds[uniq], dtype=np.float64)[back]
    unpaved = np.asarray(graph.seg_unpaved[uniq], dtype=bool)[back]
    name_ids = np.asarray(graph.seg_name[uniq])[back]
    avoided = np.asarray(rating.avoided[uniq], dtype=bool)[back]
    factor = np.asarray(rating.factor[uniq], dtype=np.float64)[back]
    g0 = np.asarray(graph.seg_g0[uniq], dtype=np.int64)
    g1 = np.asarray(graph.seg_g1[uniq], dtype=np.int64)
    from app.routing.disk import ranges

    where = ranges(g0, g1 + 1)
    all_lat = np.asarray(graph.geom_lat[where])
    all_lon = np.asarray(graph.geom_lon[where])
    offsets = np.concatenate(([0], np.cumsum(g1 - g0 + 1)))
    names: dict[int, str] = {}
    for i, (s, start, end) in enumerate(pieces):
        metres = abs(end - start)
        if metres <= 0:
            continue
        length = seg_len[i]
        seconds = float(seg_seconds[i] * metres / length) if length > 0 else 0.0
        main = bool(avoided[i])
        out.extra_seconds += seconds * (float(factor[i]) - 1.0)
        out.seconds += seconds
        out.meters += metres
        if main:
            out.main_seconds += seconds
            out.main_meters += metres
        if unpaved[i]:
            out.unpaved_meters += metres

        u = back[i]
        lat = all_lat[offsets[u]:offsets[u + 1]]
        lon = all_lon[offsets[u]:offsets[u + 1]]
        coords = _piece_coords(lat, lon, _alongs(lat, lon, float(length)), start, end)
        if out.runs and out.runs[-1].main == main:
            run = out.runs[-1]
            run.coords.extend(coords[1:] if run.coords and run.coords[-1] == coords[0] else coords)
        else:
            # A new run starts where the last one ended, so the line has no gap.
            joined = ([out.runs[-1].coords[-1]] if out.runs and out.runs[-1].coords[-1] != coords[0] else []) + coords
            out.runs.append(Run(main=main, coords=joined))

        name_id = int(name_ids[i])
        name = names.get(name_id)
        if name is None:
            name = names[name_id] = graph.names[name_id]
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


def scaled(summary: Summary, factor: float) -> Summary:
    """The same roads with every time factor times over."""
    return Summary(
        seconds=summary.seconds * factor, meters=summary.meters, main_seconds=summary.main_seconds * factor,
        main_meters=summary.main_meters, unpaved_meters=summary.unpaved_meters, extra_seconds=summary.extra_seconds * factor,
        delay_seconds=summary.delay_seconds * factor, runs=summary.runs,
        via=[Via(name=v.name, main=v.main, meters=v.meters, seconds=v.seconds * factor) for v in summary.via],
    )


def join(*parts: Summary) -> Summary:
    """Routes driven one after the other as one: the totals added, the lines
    joined (a run carries on where the next part starts the same colour) and
    the roads followed in order."""
    out = Summary()
    via: list[Via] = []
    for part in parts:
        out.seconds += part.seconds
        out.meters += part.meters
        out.main_seconds += part.main_seconds
        out.main_meters += part.main_meters
        out.unpaved_meters += part.unpaved_meters
        out.extra_seconds += part.extra_seconds
        out.delay_seconds += part.delay_seconds
        for run in part.runs:
            if not run.coords:
                continue
            if out.runs and out.runs[-1].main == run.main:
                last = out.runs[-1].coords
                last.extend(run.coords[1:] if last and last[-1] == run.coords[0] else run.coords)
            else:
                out.runs.append(Run(main=run.main, coords=list(run.coords)))
        for item in part.via:
            if via and via[-1].name == item.name:
                via[-1] = Via(name=item.name, main=via[-1].main or item.main, meters=via[-1].meters + item.meters,
                              seconds=via[-1].seconds + item.seconds)
            else:
                via.append(Via(name=item.name, main=item.main, meters=item.meters, seconds=item.seconds))
    out.via = _fold(via)
    return out
