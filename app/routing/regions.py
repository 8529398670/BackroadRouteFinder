"""The areas the Map data screen can download: every US state (plus DC and
Puerto Rico) from Geofabrik, one .osm.pbf each, or the whole US in one file.

A map is built from any set of them (osm_parse.merge joins neighbours), and is
named after what it holds: one state is its slug ("ohio"), several are their
slugs joined by "_", and the whole country is "us".
"""
from __future__ import annotations

import hashlib

GEOFABRIK = "https://download.geofabrik.de/north-america"
US = "us"
US_NAME = "Entire United States"

# Geofabrik's slug -> name, and a rough download size in MB (late 2026), for
# the screen to show before anything is fetched.
STATES: dict[str, tuple[str, int]] = {
    "alabama": ("Alabama", 150), "alaska": ("Alaska", 140), "arizona": ("Arizona", 290),
    "arkansas": ("Arkansas", 120), "california": ("California", 1300), "colorado": ("Colorado", 300),
    "connecticut": ("Connecticut", 70), "delaware": ("Delaware", 25), "district-of-columbia": ("District of Columbia", 25),
    "florida": ("Florida", 600), "georgia": ("Georgia", 340), "hawaii": ("Hawaii", 40),
    "idaho": ("Idaho", 130), "illinois": ("Illinois", 400), "indiana": ("Indiana", 240),
    "iowa": ("Iowa", 150), "kansas": ("Kansas", 140), "kentucky": ("Kentucky", 150),
    "louisiana": ("Louisiana", 180), "maine": ("Maine", 110), "maryland": ("Maryland", 180),
    "massachusetts": ("Massachusetts", 300), "michigan": ("Michigan", 460), "minnesota": ("Minnesota", 330),
    "mississippi": ("Mississippi", 120), "missouri": ("Missouri", 220), "montana": ("Montana", 130),
    "nebraska": ("Nebraska", 100), "nevada": ("Nevada", 110), "new-hampshire": ("New Hampshire", 70),
    "new-jersey": ("New Jersey", 260), "new-mexico": ("New Mexico", 130), "new-york": ("New York", 500),
    "north-carolina": ("North Carolina", 600), "north-dakota": ("North Dakota", 90), "ohio": ("Ohio", 330),
    "oklahoma": ("Oklahoma", 170), "oregon": ("Oregon", 250), "pennsylvania": ("Pennsylvania", 430),
    "puerto-rico": ("Puerto Rico", 50), "rhode-island": ("Rhode Island", 20), "south-carolina": ("South Carolina", 180),
    "south-dakota": ("South Dakota", 70), "tennessee": ("Tennessee", 200), "texas": ("Texas", 1000),
    "utah": ("Utah", 150), "vermont": ("Vermont", 40), "virginia": ("Virginia", 380),
    "washington": ("Washington", 330), "west-virginia": ("West Virginia", 90), "wisconsin": ("Wisconsin", 280),
    "wyoming": ("Wyoming", 80),
}
US_MB = 11000


def valid(slug: str) -> bool:
    return slug == US or slug in STATES


def name_of(slug: str) -> str:
    return US_NAME if slug == US else STATES[slug][0]


def url_of(slug: str) -> str:
    return f"{GEOFABRIK}/us-latest.osm.pbf" if slug == US else f"{GEOFABRIK}/us/{slug}-latest.osm.pbf"


def region_name(slugs: list[str]) -> str:
    """The map's name for a set of areas: the whole US wins over any states."""
    picked = sorted(set(slugs))
    if US in picked:
        return US
    joined = "_".join(picked)
    if len(joined) <= 120:
        return joined
    return "states-" + hashlib.sha256(joined.encode()).hexdigest()[:10]
