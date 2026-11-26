"""A plan from several sources at once: our own router and any of the other
engines (providers/), each asked for its backroads route (or its fastest,
with backroads off) and its alternatives.

Every route is measured on our map the same way (match.py for the others), and
scored the way our own router scores a route: its time, plus each minute on a
main road counted penalty times over. With combine="best" only the lowest
score is kept; with "all" every route comes back, best first, so the person
can look at them side by side. Near-copies (two engines finding the same
roads) are kept once.

The other engines are asked in parallel while our own router runs, and an
engine that fails or is slow only drops out of this plan: what went wrong
comes back in problems, beside the routes that did come.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field

from app.routing import costs, match, router
from app.routing.graph import Graph
from app.routing.providers import ENGINES, OURS, ProviderError, base
from app.routing.search import NoRoute
from app.routing.snap import Snap
from app.routing.summarize import Summary

MAX_ROUTES = 12
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="provider")


@dataclass
class Choice:
    source: str
    kind: str                # backroads, or fastest
    summary: Summary
    score: float
    compare: bool = False    # the plain fastest route, shown for comparison
    estimated: bool = False  # times split by our map, not planned on it
    off_map_meters: float = 0.0
    rank: int = 0            # this route's place among the source's own answers
    also: list[str] = field(default_factory=list)  # other sources that found the same roads


@dataclass
class Problem:
    source: str
    message: str


@dataclass
class Plan:
    routes: list[Choice] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    snapped: list[Snap] = field(default_factory=list)
    milliseconds: int = 0


@dataclass(frozen=True)
class Access:
    """Where each other engine is and its key: what makes it usable."""
    keys: dict[str, str]
    urls: dict[str, str]
    timeout: float

    def usable(self, engine_id: str) -> bool:
        engine = ENGINES[engine_id]
        if engine.url_key and not self.urls.get(engine_id):
            return False
        if engine.needs_key and not self.keys.get(engine_id):
            # GraphHopper self-hosted needs no key; the hosted one does.
            return engine_id == "graphhopper" and "graphhopper.com" not in self.urls.get(engine_id, "")
        return True

    def why_not(self, engine_id: str) -> str:
        engine = ENGINES[engine_id]
        if engine.url_key and not self.urls.get(engine_id):
            return f"switched off (providers.{engine.url_key} is empty)"
        return "no API key yet: the owner can add one on the Settings screen"


def score(summary: Summary, penalty: float) -> float:
    return summary.seconds + (penalty - 1.0) * summary.main_seconds


def _same(a: Summary, b: Summary) -> bool:
    return abs(a.meters - b.meters) <= max(150.0, 0.004 * a.meters) and abs(a.main_meters - b.main_meters) <= max(150.0, 0.01 * a.meters)


def plan(graph: Graph, points: list[tuple[float, float]], options: costs.Options, sources: list[str], combine: str,
         compare_fastest: bool, max_snap_m: float, access: Access) -> Plan:
    import time

    began = time.monotonic()
    backroads = options.penalty > 1
    kind = "backroads" if backroads else "fastest"
    out = Plan()
    snapped = router.snap_all(graph, points, max_snap_m)
    out.snapped = snapped
    on_road = [(s.lat, s.lon) for s in snapped]

    asked: dict[str, concurrent.futures.Future] = {}
    for source in sources:
        if source == OURS or source not in ENGINES:
            continue
        if not access.usable(source):
            out.problems.append(Problem(source, access.why_not(source)))
            continue
        engine = ENGINES[source]
        asked[source] = _pool.submit(base.cached_fetch, engine, on_road, backroads,
                                     access.keys.get(source, ""), access.urls.get(source, ""), access.timeout)

    choices: list[Choice] = []
    fastest: Choice | None = None
    use_ours = OURS in sources
    if use_ours or (compare_fastest and backroads):
        try:
            ours = router.plan(graph, points, options, compare_fastest, max_snap_m, chosen=use_ours)
        except NoRoute as error:
            if not asked:
                raise
            out.problems.append(Problem(OURS, str(error)))
        else:
            for planned in ours.routes:
                if use_ours and planned.kind == kind and not choices:
                    choices.append(Choice(OURS, kind, planned.summary, score(planned.summary, options.penalty)))
                else:
                    fastest = Choice(OURS, "fastest", planned.summary, planned.summary.seconds, compare=True)

    for source, future in asked.items():
        try:
            candidates = future.result(timeout=access.timeout + 5)
        except concurrent.futures.TimeoutError:
            out.problems.append(Problem(source, f"no answer within {int(access.timeout)} s"))
            continue
        except ProviderError as error:
            out.problems.append(Problem(source, str(error)))
            continue
        except Exception as error:  # noqa: BLE001 -- one engine's bug never fails the plan
            out.problems.append(Problem(source, f"failed: {error}"))
            continue
        for rank, candidate in enumerate(candidates):
            summary, off_map = match.measure(graph, candidate.coords, candidate.seconds, options.secondary_is_main)
            summary.meters = candidate.meters or summary.meters
            choices.append(Choice(source, kind, summary, score(summary, options.penalty),
                                  estimated=True, off_map_meters=off_map, rank=rank))

    if not choices:
        if out.problems:
            raise NoRoute("no source found a route: " + "; ".join(f"{ENGINES[p.source].name if p.source in ENGINES else 'our map'}: {p.message}" for p in out.problems))
        raise NoRoute("no route found")

    choices.sort(key=lambda c: (c.score, c.rank))
    kept: list[Choice] = []
    for choice in choices:
        twin = next((other for other in kept if _same(choice.summary, other.summary)), None)
        if twin is not None:
            if choice.source != twin.source and choice.source not in twin.also:
                twin.also.append(choice.source)
            continue
        kept.append(choice)
    out.routes = kept[:1] if combine == "best" else kept[:MAX_ROUTES]
    if fastest is not None:
        out.routes.append(fastest)
    out.milliseconds = int((time.monotonic() - began) * 1000)
    return out
