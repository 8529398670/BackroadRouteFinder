"""GraphHopper (https://www.graphhopper.com): free API key, a few hundred
routes a day; or self-hosted (providers.graphhopper_url).

Backroads: a custom model that makes motorways, trunk and primary roads a
tenth as attractive -- the closest of all the engines to what our own router
does. Alternatives on a two-point trip.

The custom model needs GraphHopper's "flexible mode", which the free hosted
plan refuses (HTTP 400 "Free packages cannot use flexible mode"). Then the
request is sent again in speed mode, with no custom model and ch.disable
left out, and the key is remembered as speed-only so later plans go straight
there. Its routes are still scored against the road rules on our own map."""
from __future__ import annotations

import hashlib
import threading

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


# Keys (hashed, with their address) the server refused flexible mode for.
_SPEED_ONLY: set[str] = set()
_SPEED_ONLY_LOCK = threading.Lock()


def _account(key: str, url: str) -> str:
    return hashlib.sha256(f"{url}\n{key}".encode("utf-8")).hexdigest()


def refuses_flexible(error: ProviderError) -> bool:
    return "flexible" in str(error).lower()


def build_body(points: Points, ask: Ask, flexible: bool) -> dict:
    body: dict = {
        "points": [[lon, lat] for lat, lon in points],
        "profile": "car",
        "points_encoded": False,
        "instructions": False,
        "calc_points": True,
    }
    model = custom_model(ask) if flexible else None
    if model:
        body["ch.disable"] = True
        body["custom_model"] = model
    if len(points) == 2:
        body["algorithm"] = "alternative_route"
        body["alternative_route.max_paths"] = 3
        if flexible:
            body["ch.disable"] = True
    return body


def fetch(points: Points, ask: Ask | bool, key: str, url: str, timeout: float) -> list[Candidate]:
    ask = Ask.coerce(ask)
    address = url.rstrip("/") + "/route" + (f"?{query({'key': key})}" if key else "")
    account = _account(key, url)
    with _SPEED_ONLY_LOCK:
        flexible = account not in _SPEED_ONLY
    try:
        answer = request_json(address, body=build_body(points, ask, flexible), timeout=timeout)
    except ProviderError as error:
        if not flexible or not refuses_flexible(error):
            raise
        with _SPEED_ONLY_LOCK:
            _SPEED_ONLY.add(account)
        answer = request_json(address, body=build_body(points, ask, False), timeout=timeout)
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
    backroads="Makes freeways, US routes and major state routes 10x less attractive (paid plans; "
              "the free plan plans in speed mode and our map scores its routes).",
    fetch=fetch,
    honours=frozenset({"freeways", "major", "unpaved", "tracks"}),
)
