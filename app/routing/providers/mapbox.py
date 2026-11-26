"""Mapbox Directions (https://www.mapbox.com): access token, 100,000 requests
a month free.

Backroads: exclude motorways and toll roads. Alternatives on a two-point
trip."""
from __future__ import annotations

from app.routing.providers import polyline
from app.routing.providers.base import Ask, Candidate, Engine, Points, ProviderError, query, request_json

URL = "https://api.mapbox.com/directions/v5/mapbox/driving"


def fetch(points: Points, ask: Ask | bool, key: str, url: str, timeout: float) -> list[Candidate]:
    ask = Ask.coerce(ask)
    where = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    params = {"geometries": "polyline6", "overview": "full", "steps": "false",
              "alternatives": "true" if len(points) == 2 else "false", "access_token": key}
    exclude = (["motorway"] if ask.off("freeways") else []) + (["toll"] if ask.backroads else []) \
        + (["unpaved"] if ask.off("unpaved") else [])
    if exclude:
        params["exclude"] = ",".join(exclude)
    answer = request_json(f"{URL}/{where}?{query(params)}", timeout=timeout)
    if answer.get("code") != "Ok":
        raise ProviderError(answer.get("message") or answer.get("code") or "no route found")
    return [Candidate(coords=polyline.decode(r["geometry"], 6), seconds=float(r["duration"]), meters=float(r["distance"]))
            for r in answer.get("routes", [])]


ENGINE = Engine(
    id="mapbox", name="Mapbox", needs_key=True, url_key="",
    signup_url="https://account.mapbox.com/auth/signup/",
    free_tier="Free: 100,000 directions requests a month (card required).",
    backroads="Leaves out freeways and toll roads.",
    fetch=fetch,
    honours=frozenset({"freeways", "unpaved"}),
)
