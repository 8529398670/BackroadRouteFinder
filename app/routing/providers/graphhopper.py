"""GraphHopper (https://www.graphhopper.com): free API key, a few hundred
routes a day; or self-hosted (providers.graphhopper_url).

Backroads: a custom model that makes motorways, trunk and primary roads a
tenth as attractive -- the closest of all the engines to what our own router
does. Alternatives on a two-point trip."""
from __future__ import annotations

from app.routing.providers.base import AVOID, NEVER, Ask, Candidate, Engine, Points, ProviderError, query, request_json

FREEWAYS = "road_class == MOTORWAY"
MAJOR = "road_class == TRUNK || road_class == PRIMARY"
UNPAVED = "surface == UNPAVED || surface == GRAVEL || surface == DIRT || surface == COMPACTED || surface == GROUND || surface == SAND"
TRACKS = "road_class == TRACK"
# How much less attractive "avoid" and "never" make a road. Never is not 0, so
# a trip that cannot be made without one still gets a route.
FACTOR = {AVOID: "0.1", NEVER: "0.01"}


def custom_model(ask: Ask) -> dict | None:
    priority = [{"if": test, "multiply_by": FACTOR[getattr(ask, name)]}
                for name, test in (("freeways", FREEWAYS), ("major", MAJOR), ("unpaved", UNPAVED), ("tracks", TRACKS))
                if ask.off(name)]
    return {"priority": priority} if priority else None


def fetch(points: Points, ask: Ask | bool, key: str, url: str, timeout: float) -> list[Candidate]:
    ask = Ask.coerce(ask)
    body: dict = {
        "points": [[lon, lat] for lat, lon in points],
        "profile": "car",
        "points_encoded": False,
        "instructions": False,
        "calc_points": True,
    }
    model = custom_model(ask)
    if model:
        body["ch.disable"] = True
        body["custom_model"] = model
    if len(points) == 2:
        body["algorithm"] = "alternative_route"
        body["alternative_route.max_paths"] = 3
        body["ch.disable"] = True
    address = url.rstrip("/") + "/route" + (f"?{query({'key': key})}" if key else "")
    answer = request_json(address, body=body, timeout=timeout)
    out = []
    for path in answer.get("paths", []):
        coords = [(lat, lon) for lon, lat, *_ in path.get("points", {}).get("coordinates", [])]
        out.append(Candidate(coords=coords, seconds=float(path.get("time", 0)) / 1000, meters=float(path.get("distance", 0))))
    if not out:
        raise ProviderError("no route found")
    return out


ENGINE = Engine(
    id="graphhopper", name="GraphHopper", needs_key=True, url_key="graphhopper_url",
    signup_url="https://graphhopper.com/dashboard/#/register",
    free_tier="Free key: about 500 credits a day (a backroads route costs more than one).",
    backroads="Makes freeways, US routes and major state routes 10x less attractive.",
    fetch=fetch,
    honours=frozenset({"freeways", "major", "unpaved", "tracks"}),
)
