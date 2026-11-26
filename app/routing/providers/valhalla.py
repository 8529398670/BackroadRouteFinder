"""Valhalla (https://valhalla.github.io/valhalla/): the FOSSGIS public server
by default, no key; or your own (providers.valhalla_url).

Backroads: use_highways 0 (stay off motorways and trunk roads as far as it
can), tolls and ferries discouraged. Unpaved roads left out (exclude_unpaved)
or tracks shunned (use_tracks) as the rules say. Up to two alternates."""
from __future__ import annotations

from app.routing.providers import polyline
from app.routing.providers.base import NEVER, Ask, Candidate, Engine, Points, ProviderError, request_json


def fetch(points: Points, ask: Ask | bool, key: str, url: str, timeout: float) -> list[Candidate]:
    ask = Ask.coerce(ask)
    auto: dict = {"use_tolls": 0.2, "use_ferry": 0.2}
    if ask.backroads:
        auto["use_living_streets"] = 0.3
    if ask.off("freeways"):
        auto["use_highways"] = 0.0
    auto["use_tracks"] = 0.0 if ask.off("tracks") else 0.5
    if ask.unpaved == NEVER:
        auto["exclude_unpaved"] = True
    body = {
        "locations": [{"lat": lat, "lon": lon, "type": "break" if i in (0, len(points) - 1) else "through"}
                      for i, (lat, lon) in enumerate(points)],
        "costing": "auto",
        "costing_options": {"auto": auto},
        "directions_type": "none",
        "units": "kilometers",
        "alternates": 2 if len(points) == 2 else 0,
    }
    answer = request_json(url.rstrip("/") + "/route", body=body, timeout=timeout)
    trips = [answer.get("trip")] + [alt.get("trip") for alt in answer.get("alternates", [])]
    out = []
    for trip in trips:
        if not trip:
            continue
        coords: list[tuple[float, float]] = []
        for leg in trip.get("legs", []):
            line = polyline.decode(leg.get("shape", ""), 6)
            coords.extend(line[1:] if coords and line and coords[-1] == line[0] else line)
        summary = trip.get("summary", {})
        out.append(Candidate(coords=coords, seconds=float(summary.get("time", 0)), meters=float(summary.get("length", 0)) * 1000))
    if not out:
        raise ProviderError("no route found")
    return out


ENGINE = Engine(
    id="valhalla", name="Valhalla", needs_key=False, url_key="valhalla_url",
    signup_url="https://valhalla.openstreetmap.de",
    free_tier="Free public server (FOSSGIS), light use only; or self-host.",
    backroads="Avoids freeways and trunk roads (use_highways 0).",
    fetch=fetch,
    honours=frozenset({"freeways", "unpaved", "tracks"}),
)
