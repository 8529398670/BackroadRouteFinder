"""TomTom Routing (https://developer.tomtom.com): free API key, 2,500 requests
a day.

Backroads: routeType "thrilling" (winding, hilly roads, made for this) with
motorways and toll roads avoided. Fastest: routeType fastest, without live
traffic so it compares with the others."""
from __future__ import annotations

from app.routing.providers.base import Ask, Candidate, Engine, Points, ProviderError, query, request_json

URL = "https://api.tomtom.com/routing/1/calculateRoute"


def fetch(points: Points, ask: Ask | bool, key: str, url: str, timeout: float) -> list[Candidate]:
    ask = Ask.coerce(ask)
    where = ":".join(f"{lat:.6f},{lon:.6f}" for lat, lon in points)
    params: list[tuple[str, str]] = [("key", key), ("travelMode", "car"), ("traffic", "false"), ("routeRepresentation", "polyline")]
    if ask.off("freeways"):
        params.append(("avoid", "motorways"))
    if ask.off("unpaved"):
        params.append(("avoid", "unpavedRoads"))
    if ask.backroads:
        params += [("routeType", "thrilling"), ("hilliness", "normal"), ("windingness", "high"), ("avoid", "tollRoads")]
    else:
        params += [("routeType", "fastest")]
        if len(points) == 2:
            params.append(("maxAlternatives", "2"))
    answer = request_json(f"{URL}/{where}/json?{query(params)}", timeout=timeout)
    out = []
    for route in answer.get("routes", []):
        coords: list[tuple[float, float]] = []
        for leg in route.get("legs", []):
            line = [(p["latitude"], p["longitude"]) for p in leg.get("points", [])]
            coords.extend(line[1:] if coords and line and coords[-1] == line[0] else line)
        summary = route.get("summary", {})
        out.append(Candidate(coords=coords, seconds=float(summary.get("travelTimeInSeconds", 0)), meters=float(summary.get("lengthInMeters", 0))))
    if not out:
        raise ProviderError("no route found")
    return out


ENGINE = Engine(
    id="tomtom", name="TomTom", needs_key=True, url_key="",
    signup_url="https://developer.tomtom.com/user/register",
    free_tier="Free key: 2,500 requests a day.",
    backroads="\"Thrilling\" route type (winding, hilly roads), avoiding freeways and tolls.",
    fetch=fetch,
    honours=frozenset({"freeways", "unpaved"}),
)
