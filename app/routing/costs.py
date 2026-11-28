"""What a request costs to drive, edge by edge.

The search minimises the sum of weights, and a weight is seconds of driving,
multiplied on every road its rules say to avoid by that road type's avoid
strength: its own (Options.factors, e.g. gravel at 2x) or else the request's
penalty. So an avoided road is taken only where every way around it is more
than that many times slower: at 8x, a minute on the state route is worth
eight minutes of county road. Crossing an avoided road costs only the real seconds
of the stop and the wait for a gap (turns.py), never multiplied, since no time
is spent driving along it.

The rules are one of use / avoid / never for each road category
(classes.CATEGORIES: Interstate, US highway, state route, county road, local,
service, track) and for two properties any road can have (unpaved,
restricted). A road several rules avoid counts the strongest of their
strengths, not their product.
"Never" adds a huge weight rather than removing the road, so a destination
only reachable that way still gets a route.

With the penalty at 1 (backroads off) "avoid" changes nothing, whatever the
strengths; "never" still holds.

Service roads (alleys, access roads) count double whatever the rules, so
routes do not cut through parking lots and business parks to save ten
seconds; with backroads on, as much as the strongest avoided road type
(service_factor), or else a slow service road beside an avoided main road
costs the same and the route wanders down it.

Turns (turns.py) cost their seconds on top, never multiplied: a stop sign
takes as long on a backroad as anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

import numpy as np

from app.routing import classes
from app.routing.graph import Graph

SERVICE_FACTOR = 2.0
EXCLUDED_WEIGHT = 1e7
MIN_WEIGHT = 1e-3
# How a "never" road counts when another engine's route is scored on our map
# (it cannot be left out of a line already drawn).
NEVER_SCORE_FACTOR = 100.0
MIN_FACTOR, MAX_FACTOR = 1.0, 100.0

USE, AVOID, NEVER = "use", "avoid", "never"
RULE_VALUES = (USE, AVOID, NEVER)

# What the planner does with each road type unless told otherwise: what it
# did before there were rules (main roads avoided, tracks left out, gravel
# discouraged, private roads not in the map).
DEFAULT_RULES: dict[str, str] = {
    "interstate": AVOID,
    "us_highway": AVOID,
    "state_route": AVOID,
    "county_road": USE,
    "local": USE,
    "service": USE,
    "track": NEVER,
    "unpaved": AVOID,
    "restricted": NEVER,
}
assert tuple(DEFAULT_RULES) == classes.RULE_KEYS


def full_rules(rules: Mapping[str, str] | None) -> dict[str, str]:
    """Every rule key with a value: the given ones, defaults for the rest."""
    out = dict(DEFAULT_RULES)
    for key, value in (rules or {}).items():
        if key in out and value in RULE_VALUES:
            out[key] = value
    return out


def rules_from_legacy(options: Mapping[str, Any], base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Rules for options saved before there were rules: secondary_is_main and
    allow_unpaved. road_rules, where present, win; base (the server's
    defaults) fills in the rest."""
    rules = full_rules(base)
    if options.get("secondary_is_main") is False:
        rules["state_route"] = USE
    if options.get("allow_unpaved") is True:
        rules["unpaved"] = USE
        rules["track"] = USE
    rules.update({k: v for k, v in (options.get("road_rules") or {}).items() if k in rules and v in RULE_VALUES})
    return rules


def parse_pairs(items: list[str] | tuple[str, ...], label: str) -> dict[str, str]:
    """config.yaml's "type=value" lists (routing.road_rules,
    routing.avoid_strength) as a dict; a ValueError names what is wrong."""
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in DEFAULT_RULES:
            raise ValueError(f"{label}: {item!r} is not <road type>=<value> (road types: {', '.join(DEFAULT_RULES)})")
        out[key] = value
    return out


def clean_factors(factors: Mapping[str, Any] | None) -> dict[str, float]:
    """Avoid strengths for known road types, within 1-100."""
    out: dict[str, float] = {}
    for key, value in (factors or {}).items():
        if key in DEFAULT_RULES and isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(min(max(value, MIN_FACTOR), MAX_FACTOR))
    return out


@dataclass(frozen=True)
class Options:
    penalty: float = 8.0
    rules: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_RULES))
    # Avoid strengths of their own, by road type; the rest use penalty.
    factors: dict[str, float] = field(default_factory=dict)

    def fastest(self) -> "Options":
        """The plain fastest route under the same never-rules."""
        return replace(self, penalty=1.0)

    def rule(self, key: str) -> str:
        return self.rules.get(key, DEFAULT_RULES[key])

    def strength(self, key: str) -> float:
        """How many times over a minute on this road type counts when it is
        avoided: 1 with backroads off."""
        if self.penalty <= 1.0:
            return 1.0
        return float(self.factors.get(key, self.penalty))


def _column(array: np.ndarray, segments: np.ndarray | None) -> np.ndarray:
    """A per-segment column for these segments (all of them when None; only
    a small map should ask for all)."""
    return np.asarray(array) if segments is None else np.asarray(array[segments])


def _matching(graph: Graph, options: Options, value: str, segments: np.ndarray | None = None) -> np.ndarray:
    """Per segment: does any rule give it this value."""
    by_category = np.array([options.rule(name) == value for name in classes.CATEGORIES], dtype=bool)
    out = by_category[_column(graph.seg_category, segments)]
    if options.rule("unpaved") == value:
        out = out | _column(graph.seg_unpaved, segments).astype(bool)
    if options.rule("restricted") == value:
        out = out | _column(graph.seg_restricted, segments).astype(bool)
    return out


def segment_factors(graph: Graph, options: Options, segments: np.ndarray | None = None) -> np.ndarray:
    """Per segment: what a second on it counts, from its avoid rules (1 where
    none avoids it, the strongest strength where several do)."""
    by_category = np.array([options.strength(name) if options.rule(name) == AVOID else 1.0 for name in classes.CATEGORIES])
    out = by_category[_column(graph.seg_category, segments)]
    for flag, column in (("unpaved", graph.seg_unpaved), ("restricted", graph.seg_restricted)):
        if options.rule(flag) == AVOID:
            out = np.where(_column(column, segments).astype(bool), np.maximum(out, options.strength(flag)), out)
    return out


class _PerSegment:
    """A per-segment value worked out only for the segments asked for:
    rating.avoided[s] for one segment or an array of them."""

    def __init__(self, compute) -> None:
        self.compute = compute

    def __getitem__(self, index):
        array = np.atleast_1d(np.asarray(index, dtype=np.int64))
        values = self.compute(np.maximum(array, 0))
        return values[0] if np.ndim(index) == 0 else values.reshape(np.shape(index))


class Rating:
    """What a request makes of each segment, for summing up a route already
    found: avoided (counted as main, coloured red) and the factor its seconds
    count in the score. Indexed by segment; nothing is worked out for the
    whole map."""

    def __init__(self, graph: Graph, options: Options) -> None:
        self.avoided = _PerSegment(lambda segs: avoided_segments(graph, options, segs))

        def factor(segs: np.ndarray) -> np.ndarray:
            never = excluded_segments(graph, options, segs)
            base = segment_factors(graph, options, segs)
            return np.where(never, np.maximum(base, max(NEVER_SCORE_FACTOR, options.penalty)), base)

        self.factor = _PerSegment(factor)


def rating(graph: Graph, options: Options) -> Rating:
    return Rating(graph, options)


def avoided_segments(graph: Graph, options: Options, segments: np.ndarray | None = None) -> np.ndarray:
    """Per segment: is it a road the rules avoid or rule out. These are what a
    route's "main" time, the score and the line's colours count."""
    return _matching(graph, options, AVOID, segments) | _matching(graph, options, NEVER, segments)


def excluded_segments(graph: Graph, options: Options, segments: np.ndarray | None = None) -> np.ndarray:
    return _matching(graph, options, NEVER, segments)


def service_factor(options: Options) -> float:
    """What a second on a service road counts: SERVICE_FACTOR, and with
    backroads on at least as much as the strongest avoided road type, so a
    service road (an access road, a driveway out to a tower) never comes out
    cheaper than the main road it runs beside."""
    out = SERVICE_FACTOR
    if options.penalty > 1.0:
        for name in classes.CATEGORIES:
            if name not in ("service", "track") and options.rule(name) == AVOID:
                out = max(out, options.strength(name))
    return out


def segment_weights(graph: Graph, options: Options, segments: np.ndarray | None = None) -> np.ndarray:
    """One weight per segment (direction does not change it)."""
    weight = _column(graph.seg_seconds, segments).astype(np.float64) * segment_factors(graph, options, segments)
    weight = weight * np.where(_column(graph.seg_class, segments) == classes.SERVICE, service_factor(options), 1.0)
    weight = np.where(excluded_segments(graph, options, segments), weight + EXCLUDED_WEIGHT, weight)
    return np.maximum(weight, MIN_WEIGHT)
