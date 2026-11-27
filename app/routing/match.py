"""Another engine's route, measured on our own map: lays its line on our road
graph and works out how much of it is on main roads, the roads it follows and
where its main and backroad runs are, so it can be shown and compared exactly
like one of ours.

The line is cut into points every STEP_M metres, and each point goes to the
nearest road, preferring one running the same way (so a route crossing a
freeway on a bridge stays on its own road). A stretch shorter than
MIN_RUN_POINTS points that differs from the road on both sides of it is taken
as a mismatch at a junction and given the neighbours' class. Points farther
than MAX_OFF_M from any road are "not on the map" (outside the map, or a road we
leave out), counted as backroad.

The time is the engine's own; only its split into main and backroad comes
from our map (in proportion to our speeds on the roads matched).
"""
from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from app.routing import grid
from app.routing.costs import Rating
from app.routing.graph import Graph
from app.routing.snap import pieces_of
from app.routing.summarize import DECIMALS, Run, Summary, Via, _fold, scaled

STEP_M = 20.0
CANDIDATES = 10
MAX_OFF_M = 45.0
# Metres added to a road's distance when it runs across the line rather than
# along it, so a parallel road wins over a crossing one.
CROSSING_M = 25.0
MIN_RUN_POINTS = 4


def _densify(graph: Graph, coords: np.ndarray, lat0: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Points every STEP_M along the line: their lat/lon, local x/y, and
    whether each was one of the line's own points."""
    xy = graph.project(coords[:, 0], coords[:, 1], lat0)
    lat_out, lon_out, vertex = [coords[:1, 0]], [coords[:1, 1]], [np.array([True])]
    steps = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    for i, length in enumerate(steps):
        parts = max(1, int(np.ceil(length / STEP_M)))
        t = np.arange(1, parts + 1) / parts
        lat_out.append(coords[i, 0] + t * (coords[i + 1, 0] - coords[i, 0]))
        lon_out.append(coords[i, 1] + t * (coords[i + 1, 1] - coords[i, 1]))
        mark = np.zeros(parts, dtype=bool)
        mark[-1] = True
        vertex.append(mark)
    lat, lon = np.concatenate(lat_out), np.concatenate(lon_out)
    return np.column_stack((lat, lon)), graph.project(lat, lon, lat0), np.concatenate(vertex)


# Pieces are sampled every SAMPLE_M for the nearest-piece lookup, so a long
# straight piece (a freeway, a section-line road) with shape points far apart
# is found as readily as a short one.
SAMPLE_M = 30.0


def _local_pieces(graph: Graph, points: np.ndarray, lat0: float):
    """The roads near the line: their pieces (first shape point index, segment,
    both ends in local x/y), and a tree of points along them with each point's
    piece."""
    reach = MAX_OFF_M + CROSSING_M
    dlat = reach / grid.METRES_PER_DEGREE
    dlon = reach / (grid.METRES_PER_DEGREE * max(0.05, np.cos(np.radians(lat0))))
    keys = []
    for sl, so in ((-dlat, -dlon), (-dlat, dlon), (dlat, -dlon), (dlat, dlon)):
        r, c = grid.cell_rc(points[:, 0] + sl, points[:, 1] + so)
        keys.append(grid.key_of(r, c))
    segments = grid.segments_in_cells(graph, np.concatenate(keys))
    starts, owner = pieces_of(graph, segments)
    order = np.argsort(starts)
    starts, owner = starts[order], owner[order]
    a = graph.project(graph.geom_lat[starts], graph.geom_lon[starts], lat0)
    b = graph.project(graph.geom_lat[starts + 1], graph.geom_lon[starts + 1], lat0)
    length = np.hypot(*(b - a).T)
    parts = np.maximum(1, np.ceil(length / SAMPLE_M).astype(np.int64))
    which = np.repeat(np.arange(len(starts)), parts + 1)
    step = np.arange(len(which)) - np.repeat(np.cumsum(parts + 1) - (parts + 1), parts + 1)
    t = step / np.repeat(parts, parts + 1)
    samples = a[which] + t[:, None] * (b[which] - a[which])
    return owner, a, b, (cKDTree(samples) if len(samples) else None), which


def _nearest_segments(graph: Graph, points: np.ndarray, xy: np.ndarray, lat0: float) -> tuple[np.ndarray, np.ndarray]:
    """For each point, the segment it is on (-1 when none is near) and its
    distance to it."""
    count = len(xy)
    owner, pa, pb, tree, which = _local_pieces(graph, points, lat0)
    if tree is None:
        return np.full(count, -1, dtype=np.int64), np.full(count, np.inf)
    k = min(CANDIDATES * 2, tree.n)
    _, near = tree.query(xy, k=k)
    cand = which[np.asarray(near).reshape(count, -1)]       # (count, k) piece indices

    # The direction of travel at each point.
    heading = np.zeros_like(xy)
    heading[:-1] = xy[1:] - xy[:-1]
    heading[-1] = heading[-2] if count > 1 else 0
    norm = np.hypot(heading[:, 0], heading[:, 1])
    norm[norm == 0] = 1
    heading /= norm[:, None]

    rows = np.arange(count)
    a, b = pa[cand], pb[cand]
    piece = b - a
    length_sq = (piece ** 2).sum(axis=2)
    safe = np.where(length_sq == 0, 1, length_sq)
    t = np.clip(((xy[:, None, :] - a) * piece).sum(axis=2) / safe, 0, 1)
    closest = a + t[..., None] * piece
    dist = np.hypot(*(xy[:, None, :] - closest).transpose(2, 0, 1))
    along = np.abs((piece * heading[:, None, :]).sum(axis=2)) / np.sqrt(safe)
    along = np.where(length_sq == 0, 1, along)
    cost = dist + CROSSING_M * (1 - along)
    pick = np.argmin(cost, axis=1)
    best_dist = dist[rows, pick]
    best_seg = owner[cand[rows, pick]]
    best_seg = np.where(best_dist <= MAX_OFF_M, best_seg, -1)
    return best_seg, best_dist


def _smooth(flags: np.ndarray) -> np.ndarray:
    """Short runs that differ from the same thing on both sides take that
    thing's value."""
    out = flags.copy()
    n = len(out)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and out[j + 1] == out[i]:
            j += 1
        if i > 0 and j < n - 1 and (j - i + 1) < MIN_RUN_POINTS and out[i - 1] == out[j + 1]:
            out[i:j + 1] = out[i - 1]
        i = j + 1
    return out


def measure(graph: Graph, coords: list[tuple[float, float]], seconds: float | None, rating: Rating) -> tuple[Summary, float]:
    """The line as a Summary (like one of our own routes), and how many
    metres of it are not on our map. rating: what the request makes of each
    segment (costs.rating). seconds: the engine's own time for the line;
    None times it at our own speeds."""
    line = np.asarray(coords, dtype=np.float64)
    if len(line) < 2:
        return Summary(seconds=seconds or 0.0), 0.0
    lat0 = float(np.mean(line[:, 0]))
    points, xy, vertex = _densify(graph, line, lat0)
    segments, _ = _nearest_segments(graph, points, xy, lat0)
    on_map = segments >= 0
    seg = np.where(on_map, segments, 0)
    main = np.where(on_map, rating.avoided[seg], False)
    factor = np.where(on_map, rating.factor[seg], 1.0)
    main = _smooth(main)

    step = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))     # step i: point i -> i+1, on point i's road
    speed = np.where(graph.seg_seconds[seg] > 0, graph.seg_len[seg] / np.maximum(graph.seg_seconds[seg], 1e-6), 15.0)
    speed = np.where(on_map, speed, 60 / 3.6)[:-1]
    our_seconds = step / speed
    total_ours = float(our_seconds.sum()) or 1.0
    main_steps = main[:-1]
    if seconds is None:
        seconds = float(our_seconds.sum())
    scale = seconds / total_ours

    out = Summary(seconds=seconds, meters=float(step.sum()))
    out.main_seconds = float(our_seconds[main_steps].sum() * scale)
    out.main_meters = float(step[main_steps].sum())
    out.extra_seconds = float((our_seconds * (factor[:-1] - 1.0)).sum() * scale)
    unpaved = on_map[:-1] & graph.seg_unpaved[seg][:-1].astype(bool)
    out.unpaved_meters = float(step[unpaved].sum())
    off_map = float(step[~on_map[:-1]].sum())

    # Runs: keep the line's own points, plus the points where main and
    # backroad change over, so the coloured line is no heavier than the
    # engine's own.
    rounded = np.round(points, DECIMALS)
    run_start = 0
    for i in range(1, len(points) + 1):
        if i < len(points) and main[i] == main[run_start]:
            continue
        stop = min(i, len(points) - 1)
        keep = [j for j in range(run_start, stop + 1) if vertex[j] or j in (run_start, stop)]
        out.runs.append(Run(main=bool(main[run_start]), coords=rounded[keep].tolist()))
        run_start = i

    # The roads it follows, by name.
    via: list[Via] = []
    for i in range(len(step)):
        name = graph.names[int(graph.seg_name[seg[i]])] if on_map[i] else ""
        metres, secs, is_main = float(step[i]), float(our_seconds[i] * scale), bool(main[i])
        if via and via[-1].name == name:
            via[-1].meters += metres
            via[-1].seconds += secs
            via[-1].main = via[-1].main or is_main
        else:
            via.append(Via(name=name, main=is_main, meters=metres, seconds=secs))
    out.via = _fold(via)
    return out, off_map


def measure_parts(graph: Graph, coords: list[tuple[float, float]], seconds: float, rating: Rating,
                  start: int, end: int) -> tuple[Summary, Summary, float]:
    """A kept (locked) route's two frozen ends: the line up to coords[start]
    and the line from coords[end], each measured on our map. Their times are
    our own speeds scaled so the whole line takes seconds (the route as it was
    planned, turns and stops included). Also the metres of both not on our map."""
    whole, _ = measure(graph, coords, None, rating)
    scale = seconds / whole.seconds if whole.seconds > 0 else 1.0
    before, off_before = measure(graph, coords[:start + 1], None, rating)
    after, off_after = measure(graph, coords[end:], None, rating)
    return scaled(before, scale), scaled(after, scale), off_before + off_after
