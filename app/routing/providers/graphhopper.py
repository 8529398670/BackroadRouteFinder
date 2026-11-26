"""GraphHopper (https://www.graphhopper.com): free API key, a few hundred
routes a day; or self-hosted (providers.graphhopper_url).

Backroads: a custom model that makes motorways, trunk and primary roads a
tenth as attractive -- the closest of all the engines to what our own router
does. Alternatives on a two-point trip."""
from __future__ import annotations

from app.routing.providers.base import Candidate, Engine, Points, ProviderError, query, request_json

MAIN_ROADS = "road_class == MOTORWAY || road_class == TRUNK || road_class == PRIMARY"


def fetch(points: Points, backroads: bool, key: str, url: str, timeout: float) -> list[Candidate]:
    body: dict = {
        "points": [[lon, lat] for lat, lon in points],
        "profile": "car",
        "points_encoded": False,
        "instructions": False,
        "calc_points": True,
    }
    if backroads:
        body["ch.disable"] = True
        body["custom_model"] = {"priority": [{"if": MAIN_ROADS, "multiply_by": "0.1"}]}
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
)
