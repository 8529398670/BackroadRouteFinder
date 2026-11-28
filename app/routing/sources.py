"""A plan from several sources at once: our own router and any of the other
engines (providers/), each asked for its backroads route (or its fastest,
with backroads off) and its alternatives.

Every route is measured on our map the same way (match.py for the others), and
scored the way our own router scores a route: its time, plus each minute on a
main road counted penalty times over. With a count of 1 only the lowest
score is kept; with more, that many come back, best first, so the person
can look at them side by side and pick one. Near-copies (two engines finding the same
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
from app.routing.summarize import Summary, join

MAX_ROUTES = 36
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
    """Where each other engine is and the key this person asks it with: what
    makes it usable. own_required are the engines whose shared key this
    person may not use (the owner's policy), for why_not."""
    keys: dict[str, str]
    urls: dict[str, str]
    timeout: float
    own_required: frozenset[str] = frozenset()

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
        if engine_id in self.own_required:
            return "needs your own API key: add it on the Settings screen"
        return "no API key yet: add your own on the Settings screen, or ask the owner for a shared one"


def score(summary: Summary, penalty: float = 0.0) -> float:
    """Seconds, plus what the avoided roads add (each road type's avoid
    strength, Summary.extra_seconds)."""
    return summary.seconds + summary.extra_seconds


def with_shape(snapped: list[Snap], shape: list[tuple[int, Snap]]) -> list[Snap]:
    """The points driven through, with each shape point (a nudge: leg, point)
    after waypoint leg. Shape points on one leg keep the order given.

    Each shape point is marked soft (Snap.soft: the route may fold away a
    spur out to it), in place, since callers know their nudges by identity."""
    out: list[Snap] = []
    for index, point in enumerate(snapped):
        out.append(point)
        for leg, at in shape:
            if leg == index:
                if not at.soft:
                    object.__setattr__(at, "soft", True)
                out.append(at)
    return out


def splice(plan: Plan, before: Summary, after: Summary, off_map_meters: float) -> None:
    """A plan of a kept route's open middle, made whole: every route (not the
    comparison) gets the frozen before and after parts added round it."""
    for choice in plan.routes:
        if choice.compare:
            continue
        choice.summary = join(before, choice.summary, after)
        choice.score = score(choice.summary)
        choice.off_map_meters += off_map_meters


def _same(a: Summary, b: Summary) -> bool:
    return abs(a.meters - b.meters) <= max(150.0, 0.004 * a.meters) and abs(a.main_meters - b.main_meters) <= max(150.0, 0.01 * a.meters)


def plan(graph: Graph, points: list[tuple[float, float]], options: costs.Options, sources: list[str], count: int,
         compare_fastest: bool, max_snap_m: float, access: Access, snapped: list[Snap] | None = None,
         reuse_factor: float = 1.0) -> Plan:
    """count: how many routes to keep, best first (our router plans that many
    alternatives itself). snapped: the points already on the road (a loop's
    ring), else snapped here. reuse_factor: see router.plan."""
    import time

    began = time.monotonic()
    backroads = options.penalty > 1
    kind = "backroads" if backroads else "fastest"
    out = Plan()
    if snapped is None:
        snapped = router.snap_all(graph, points, max_snap_m, options)
    out.snapped = snapped
    on_road = [(s.lat, s.lon) for s in snapped]
    ask = base.Ask.from_rules(options.rules, backroads)

    asked: dict[str, concurrent.futures.Future] = {}
    for source in sources:
        if source == OURS or source not in ENGINES:
            continue
        if not access.usable(source):
            out.problems.append(Problem(source, access.why_not(source)))
            continue
        engine = ENGINES[source]
        asked[source] = _pool.submit(base.cached_fetch, engine, on_road, ask,
                                     access.keys.get(source, ""), access.urls.get(source, ""), access.timeout)

    choices: list[Choice] = []
    fastest: Choice | None = None
    use_ours = OURS in sources
    if use_ours or (compare_fastest and backroads):
        try:
            ours = router.plan(graph, points, options, compare_fastest, max_snap_m, chosen=use_ours, snapped=snapped,
                               reuse_factor=reuse_factor, alternatives=count if use_ours else 1)
        except NoRoute as error:
            if not asked:
                raise
            out.problems.append(Problem(OURS, str(error)))
        else:
            for planned in ours.routes:
                if planned.compare:
                    fastest = Choice(OURS, "fastest", planned.summary, planned.summary.seconds, compare=True)
                else:
                    choices.append(Choice(OURS, kind, planned.summary, score(planned.summary, options.penalty), rank=planned.rank))

    rating = costs.rating(graph, options) if asked else None
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
            summary, off_map = match.measure(graph, candidate.coords, candidate.seconds, rating)
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
    out.routes = kept[:max(1, min(count, MAX_ROUTES))]
    if fastest is not None:
        out.routes.append(fastest)
    out.milliseconds = int((time.monotonic() - began) * 1000)
    return out
