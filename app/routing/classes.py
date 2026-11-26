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

# access= / motor_vehicle= values that keep a car out. All but "no" stay in the
# map flagged restricted (private roads, permits, farm access), for the
# "restricted" road rule; "no" is a road cars may not use at all.
CLOSED_ACCESS: frozenset[str] = frozenset({"no"})
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


# --- road categories: one vocabulary for every road, whatever the source ---
#
# OSM's highway= says how big a road is; its ref= says whose road it is (an
# Interstate, a US highway, a state route, a county or township road). People
# think in the second, and the other engines each name roads their own way, so
# the planner's road-type rules are written in these categories. A category is
# stored in the graph as its index here: add new ones at the end, and rebuild.
CATEGORIES: tuple[str, ...] = ("interstate", "us_highway", "state_route", "county_road", "local", "service", "track")
CATEGORY_INDEX: dict[str, int] = {name: index for index, name in enumerate(CATEGORIES)}
# Rules that are not a category but a property any road can have.
FLAGS: tuple[str, ...] = ("unpaved", "restricted")
RULE_KEYS: tuple[str, ...] = CATEGORIES + FLAGS

# State postal codes: "OH 7", "WV 2". Other two-letter prefixes (MR, PR, FR:
# municipal, park and forest roads) say nothing, and fall back on road size.
_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK "
           "OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
_REF_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("interstate", re.compile(r"^I(?:H)?[ -]?\d")),
    ("us_highway", re.compile(r"^US[ -]?\d")),
    # County and township roads, as Ohio's counties write them: CR 12, C-870,
    # CH 5, CTY RT 3, TR 123, T-45, TWP. 9, TH 2.
    ("county_road", re.compile(r"^(?:CR|C\.R\.|C|CH|CTY|CTY RT|COUNTY(?: ROAD)?|TR|T\.R\.|T|TWP\.?|TOWNSHIP(?: ROAD)?|TH)[ -]?\d")),
    ("state_route", re.compile(r"^(?:SR|S\.R\.|STATE(?: ROUTE)?|" + "|".join(_STATES) + r")[ -]?\d")),
)

_BY_CLASS: dict[str, str] = {
    "motorway": "interstate", "motorway_link": "interstate",
    "trunk": "state_route", "trunk_link": "state_route",
    "primary": "state_route", "primary_link": "state_route",
    "secondary": "state_route", "secondary_link": "state_route",
    "tertiary": "county_road", "tertiary_link": "county_road",
    "unclassified": "local", "residential": "local", "living_street": "local", "road": "local",
    "service": "service", "track": "track",
}


def ref_category(ref: str) -> str | None:
    """The highest-ranked category any of a ref's values names ("US 23;SR 15"
    is a US highway), or None when no value is a known route number."""
    best: int | None = None
    for value in re.split(r"[;,/]", ref or ""):
        value = " ".join(value.strip().upper().split())
        for name, pattern in _REF_PATTERNS:
            if pattern.match(value):
                index = CATEGORY_INDEX[name]
                best = index if best is None else min(best, index)
                break
    return None if best is None else CATEGORIES[best]


def category_of(highway: str, ref: str = "") -> str:
    """A way's category: service roads and tracks by what they are; otherwise
    the route its ref names, falling back on how big the road is."""
    if highway in ("service", "track"):
        return highway
    return ref_category(ref) or _BY_CLASS.get(highway, "local")


def ref_of_label(label: str) -> str:
    """The ref inside a stored road label: "Main St (SR 7)" -> "SR 7", or the
    whole label when it is only a ref. For graph files built before categories
    were stored."""
    if label.endswith(")") and " (" in label:
        return label[label.rindex(" (") + 2:-1]
    # A bare ref is capitals and digits ("SR 7"); a name ("NE 5th Ave") is not.
    return "" if any(c.islower() for c in label) else label


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
