"""Road classes: which OSM highway= values we drive on, how fast they go when
nothing says otherwise, and which of them count as main roads.

A class is stored in the graph as its index in CLASSES, so the order here is
part of the graph file's format: add new classes at the end, and rebuild.
"""
from __future__ import annotations

import re

import numpy as np

# highway= value -> default speed in km/h when the way has no usable maxspeed.
# Ohio's rural defaults: 55 mph on two-lane state and county roads, 25-35 mph
# in towns, 65-70 mph on freeways.
DEFAULT_KMH: dict[str, float] = {
    "motorway": 105.0,
    "motorway_link": 60.0,
    "trunk": 90.0,
    "trunk_link": 55.0,
    "primary": 80.0,
    "primary_link": 50.0,
    "secondary": 75.0,
    "secondary_link": 45.0,
    "tertiary": 65.0,
    "tertiary_link": 40.0,
    "unclassified": 60.0,
    "residential": 40.0,
    "living_street": 15.0,
    "service": 20.0,
    "track": 25.0,
    "road": 40.0,
}

CLASSES: tuple[str, ...] = tuple(DEFAULT_KMH)
CLASS_INDEX: dict[str, int] = {name: index for index, name in enumerate(CLASSES)}

# Main roads: what the router works to stay off. Secondary roads (most numbered
# state routes) join them only when the request says so.
MAIN: frozenset[str] = frozenset({"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"})
SECONDARY: frozenset[str] = frozenset({"secondary", "secondary_link"})

SERVICE = CLASS_INDEX["service"]
TRACK = CLASS_INDEX["track"]

# Oneway without saying so (OSM convention).
IMPLIED_ONEWAY: frozenset[str] = frozenset({"motorway", "motorway_link"})

# surface= values that mean gravel or dirt.
UNPAVED: frozenset[str] = frozenset({
    "unpaved", "gravel", "fine_gravel", "dirt", "ground", "grass", "compacted", "sand", "mud", "earth",
    "pebblestone", "rock", "woodchips", "grass_paver",
})

# service= values that are somebody's driveway or a parking lot, not a road.
SKIPPED_SERVICE: frozenset[str] = frozenset({"driveway", "parking_aisle", "drive-through", "emergency_access", "parking"})

# access= / motor_vehicle= values that keep a car out.
NO_ACCESS: frozenset[str] = frozenset({"no", "private", "agricultural", "forestry", "delivery", "customers", "permit"})
YES_ACCESS: frozenset[str] = frozenset({"yes", "designated", "permissive", "destination"})

_SPEED = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?\s*$")


def speed_kmh(highway: str, maxspeed: str | None) -> float:
    """The speed to plan with: the posted limit when it can be read, else the
    class default. Posted limits on minor roads are trimmed a little, since
    nobody holds 55 on a county road with farm entrances and stop signs."""
    default = DEFAULT_KMH[highway]
    if not maxspeed:
        return default
    match = _SPEED.match(maxspeed.split(";")[0])
    if not match:
        return default
    value = float(match.group(1))
    kmh = value * 1.609344 if match.group(2) == "mph" else value
    if kmh < 5 or kmh > 140:
        return default
    if highway not in MAIN and highway not in SECONDARY:
        kmh *= 0.9
    return kmh


def main_mask(secondary_is_main: bool) -> np.ndarray:
    """A lookup table, indexed by class, saying whether that class is main."""
    names = MAIN | SECONDARY if secondary_is_main else MAIN
    return np.array([name in names for name in CLASSES], dtype=bool)


# Where a road stands at an intersection, for working out who stops: the
# lower-ranked road gives way to the higher one. Ramps rank low, so the end of
# an off-ramp gives way to the road it meets.
RANK: dict[str, int] = {
    "motorway": 7, "trunk": 6, "primary": 5, "secondary": 4, "tertiary": 3,
    "motorway_link": 3, "trunk_link": 3, "primary_link": 3, "secondary_link": 3, "tertiary_link": 2,
    "unclassified": 2, "residential": 2, "road": 2,
    "living_street": 1, "service": 1, "track": 1,
}
RANKS = np.array([RANK[name] for name in CLASSES], dtype=np.int8)
LINKS = np.array([name.endswith("_link") for name in CLASSES], dtype=bool)
# Roads a ramp merges onto without stopping.
MERGE_TARGETS = np.array([name in ("motorway", "trunk") for name in CLASSES], dtype=bool)
