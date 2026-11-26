"""OpenRouteService (https://openrouteservice.org): free API key, 2,000 routes
a day.

Backroads: avoid_features highways (freeways) and tollways. Alternatives on a
two-point trip."""
from __future__ import annotations

from app.routing.providers.base import Ask, Candidate, Engine, Points, ProviderError, request_json


def fetch(points: Points, ask: Ask | bool, key: str, url: str, timeout: float) -> list[Candidate]:
    ask = Ask.coerce(ask)
    body: dict = {"coordinates": [[lon, lat] for lat, lon in points], "instructions": False}
    avoid = (["highways"] if ask.off("freeways") else []) + (["tollways"] if ask.backroads else [])
    if avoid:
        body["options"] = {"avoid_features": avoid}
    if len(points) == 2:
        body["alternative_routes"] = {"target_count": 3, "share_factor": 0.6, "weight_factor": 1.6}
    answer = request_json(url.rstrip("/") + "/v2/directions/driving-car/geojson", body=body,
                          headers={"Authorization": key}, timeout=timeout)
    out = []
    for feature in answer.get("features", []):
        summary = feature.get("properties", {}).get("summary", {})
        coords = [(lat, lon) for lon, lat, *_ in feature.get("geometry", {}).get("coordinates", [])]
        out.append(Candidate(coords=coords, seconds=float(summary.get("duration", 0)), meters=float(summary.get("distance", 0))))
    if not out:
        raise ProviderError("no route found")
    return out


ENGINE = Engine(
    id="ors", name="OpenRouteService", needs_key=True, url_key="ors_url",
    signup_url="https://openrouteservice.org/dev/#/signup",
    free_tier="Free key: 2,000 routes a day.",
    backroads="Avoids freeways and toll roads.",
    fetch=fetch,
    honours=frozenset({"freeways"}),
)
