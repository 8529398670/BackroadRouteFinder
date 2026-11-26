"""What a request costs to drive, edge by edge.

The search minimises the sum of weights, and a weight is seconds of driving,
multiplied on main roads by the request's penalty. So a main road is taken
only where every way around it is more than `penalty` times slower: at 8x, a
minute on the state route is worth eight minutes of county road. Crossing a
main road costs only the real seconds of the stop and the wait for a gap
(turns.py), never multiplied, since no time is spent driving along it.

Two smaller nudges, both well below any sensible penalty:
- service roads (alleys, access roads) count double, so routes do not cut
  through parking lots and business parks to save ten seconds;
- unpaved roads count double unless gravel is welcome, and tracks are left
  out altogether then (a huge weight, not a missing edge, so a destination
  only reachable by a track still gets a route).

Turns (turns.py) cost their seconds on top, never multiplied: a stop sign
takes as long on a backroad as anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.routing import classes
from app.routing.graph import Graph

SERVICE_FACTOR = 2.0
UNPAVED_FACTOR = 2.0
EXCLUDED_WEIGHT = 1e7
MIN_WEIGHT = 1e-3


@dataclass(frozen=True)
class Options:
    penalty: float = 8.0
    secondary_is_main: bool = False
    allow_unpaved: bool = False


def segment_weights(graph: Graph, options: Options) -> np.ndarray:
    """One weight per segment (direction does not change it)."""
    main = classes.main_mask(options.secondary_is_main)[graph.seg_class]
    weight = graph.seg_seconds * np.where(main, options.penalty, 1.0)
    weight = weight * np.where(graph.seg_class == classes.SERVICE, SERVICE_FACTOR, 1.0)
    if not options.allow_unpaved:
        weight = weight * np.where(graph.seg_unpaved, UNPAVED_FACTOR, 1.0)
        weight = np.where(graph.seg_class == classes.TRACK, weight + EXCLUDED_WEIGHT, weight)
    return np.maximum(weight, MIN_WEIGHT)

