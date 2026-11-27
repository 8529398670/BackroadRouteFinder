"""One routing request from start to finish: snap the waypoints, search each
leg with the request's weights, summarise. Optionally the fastest route too,
so the page can show what the backroads cost and what they save, and up to
a few alternatives (different roads, not much worse), for the person to
choose between.

Alternatives use the penalty method: after each route is found, the roads it
used count ALT_FACTOR times over, and the search runs again. A new route is
kept if it shares less than ALT_MAX_SHARED of its length with every route
kept so far and scores within ALT_MAX_STRETCH of the best.

Plans are cached for CACHE_SECONDS (the last CACHE_SIZE of them), so going
back to options or points tried a moment ago answers at once.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

from app.routing import costs, search
from app.routing.graph import Graph
from app.routing.snap import OffNetwork, Snap, snap
from app.routing.summarize import Summary, summarize

ALT_FACTOR = 1.4
ALT_MAX_SHARED = 0.7
ALT_MAX_STRETCH = 1.5
CACHE_SIZE = 64
CACHE_SECONDS = 15 * 60


class PointOffNetwork(Exception):
    """A waypoint with no road near it. index is its place in the list."""

    def __init__(self, index: int, message: str) -> None:
        super().__init__(message)
        self.index = index


@dataclass
class Planned:
    kind: str           # "backroads" or "fastest"
    summary: Summary
    rank: int = 0       # 0 the best, then each alternative in order
    compare: bool = False  # the plain fastest route, planned only to compare


@dataclass
class Result:
    routes: list[Planned] = field(default_factory=list)
    snapped: list[Snap] = field(default_factory=list)
    milliseconds: int = 0


def shunned_by(graph: Graph, options: costs.Options | None):
    """The roads a request's rules rule out, for snapping (snap.Shunned)."""
    if options is None:
        return None
    return lambda segments: costs.excluded_segments(graph, options, segments)


def snap_all(graph: Graph, points: list[tuple[float, float]], max_snap_m: float, options: costs.Options | None = None) -> list[Snap]:
    """Each point onto its road; with options, onto a road the rules do not
    rule out wherever one is near enough."""
    shunned = shunned_by(graph, options)
    snapped: list[Snap] = []
    for index, (lat, lon) in enumerate(points):
        try:
            snapped.append(snap(graph, lat, lon, max_snap_m, shunned))
        except OffNetwork as error:
            raise PointOffNetwork(index, str(error)) from None
    return snapped


def _metres(pieces: list[search.Piece]) -> dict[int, float]:
    out: dict[int, float] = {}
    for s, a, b in pieces:
        out[s] = out.get(s, 0.0) + abs(b - a)
    return out


def _shared(a: dict[int, float], b: dict[int, float]) -> float:
    """The share of a's length that b also drives."""
    total = sum(a.values()) or 1.0
    return sum(min(m, b[s]) for s, m in a.items() if s in b) / total


def _drive(searcher: search.Searcher, snapped: list[Snap], reuse_factor: float,
           base: tuple[np.ndarray, np.ndarray] | None = None) -> tuple[list[search.Piece], float]:
    """All the legs, in order. reuse_factor > 1: each leg counts the roads
    earlier legs drove that many times over. base: extra (segments, factors)
    on top (alternatives)."""
    pieces: list[search.Piece] = []
    delay = 0.0
    base_segs = base[0] if base is not None else np.zeros(0, dtype=np.int64)
    base_fac = base[1] if base is not None else np.zeros(0)
    searcher.scale(base_segs if len(base_segs) else None, base_fac if len(base_segs) else None)
    for number, (start, end) in enumerate(zip(snapped[:-1], snapped[1:])):
        if number and reuse_factor > 1:
            used = search.driven(pieces)
            segs = np.concatenate((base_segs, used))
            facs = np.concatenate((base_fac, np.full(len(used), reuse_factor)))
            order = np.argsort(segs, kind="stable")
            segs, facs = segs[order], facs[order]
            # A segment in both: the factors multiply.
            unique, first = np.unique(segs, return_index=True)
            facs = np.multiply.reduceat(facs, first) if len(facs) else facs
            searcher.scale(unique, facs)
        leg_pieces, leg_delay = searcher.leg(start, end)
        pieces.extend(leg_pieces)
        delay += leg_delay
    return pieces, delay


_cache: OrderedDict[tuple, tuple[float, Result]] = OrderedDict()
_cache_lock = threading.Lock()


def _key(graph: Graph, snapped: list[Snap], options: costs.Options, compare_fastest: bool, chosen: bool,
         reuse_factor: float, alternatives: int) -> tuple:
    points = tuple((s.segment, round(s.along, 1)) for s in snapped)
    return (str(graph.folder), graph.meta.get("built_at"), points, options.penalty, tuple(sorted(options.rules.items())),
            tuple(sorted(options.factors.items())), compare_fastest, chosen, round(reuse_factor, 3), alternatives)


def plan(graph: Graph, points: list[tuple[float, float]], options: costs.Options, compare_fastest: bool, max_snap_m: float,
         chosen: bool = True, snapped: list[Snap] | None = None, reuse_factor: float = 1.0, alternatives: int = 1) -> Result:
    """chosen=False leaves out the route options asks for, planning only the
    fastest one for comparison (when another engine does the choosing).
    reuse_factor > 1 makes each leg shy of roads earlier legs drove (a loop).
    alternatives: how many routes of the chosen kind, at most."""
    began = time.monotonic()
    if snapped is None:
        snapped = snap_all(graph, points, max_snap_m, options)
    key = _key(graph, snapped, options, compare_fastest, chosen, reuse_factor, alternatives)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
            _cache.move_to_end(key)
            cached = hit[1]
            return Result(routes=list(cached.routes), snapped=cached.snapped, milliseconds=int((time.monotonic() - began) * 1000))
    result = Result(snapped=snapped)
    kinds = [("backroads" if options.penalty > 1 else "fastest", options)] if chosen else []
    if compare_fastest and options.penalty > 1:
        kinds.append(("fastest", options.fastest()))
    rating = costs.rating(graph, options)
    for kind_number, (kind, kind_options) in enumerate(kinds):
        searcher = search.Searcher(graph, kind_options, snapped)
        pieces, delay = _drive(searcher, snapped, reuse_factor)
        best = summarize(graph, pieces, rating, delay)
        result.routes.append(Planned(kind=kind, summary=best, compare=not chosen or kind_number > 0))
        if kind_number == 0 and chosen and alternatives > 1:
            result.routes.extend(_alternatives(graph, searcher, snapped, reuse_factor, rating, kind, pieces, best, alternatives))
    result.milliseconds = int((time.monotonic() - began) * 1000)
    with _cache_lock:
        _cache[key] = (time.monotonic(), result)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return result


def _score(summary: Summary) -> float:
    return summary.seconds + summary.extra_seconds


def _alternatives(graph: Graph, searcher: search.Searcher, snapped: list[Snap], reuse_factor: float, rating: costs.Rating,
                  kind: str, best_pieces: list[search.Piece], best: Summary, count: int) -> list[Planned]:
    kept = [_metres(best_pieces)]
    out: list[Planned] = []
    penalty: dict[int, float] = {}
    best_score = _score(best)
    tries = 0
    last = best_pieces
    while len(out) < count - 1 and tries < 2 * count:
        tries += 1
        for s in search.driven(last):
            penalty[int(s)] = penalty.get(int(s), 1.0) * ALT_FACTOR
        segs = np.array(sorted(penalty), dtype=np.int64)
        facs = np.array([penalty[int(s)] for s in segs])
        try:
            pieces, delay = _drive(searcher, snapped, reuse_factor, base=(segs, facs))
        except search.NoRoute:
            break
        last = pieces
        metres = _metres(pieces)
        if any(_shared(metres, other) >= ALT_MAX_SHARED for other in kept):
            continue
        summary = summarize(graph, pieces, rating, delay)
        if _score(summary) > best_score * ALT_MAX_STRETCH:
            break
        kept.append(metres)
        out.append(Planned(kind=kind, summary=summary, rank=len(out) + 1))
    searcher.scale(None, None)
    return out
