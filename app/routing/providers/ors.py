"""OpenRouteService (https://openrouteservice.org): free API key, 2,000 routes
a day.

Backroads: avoid_features highways (freeways) and tollways. Alternatives on a
two-point trip.

HeiGIT switched api.openrouteservice.org off on 2026-11-24 (it answers 410)
in favour of api.heigit.org/openrouteservice; the same keys work there. A
config.yaml still naming the old address is sent to the new one."""
from __future__ import annotations

import urllib.parse

from app.routing.providers.base import Ask, Candidate, Engine, Points, ProviderError, request_json

OLD_HOST = "api.openrouteservice.org"
NEW_BASE = "https://api.heigit.org/openrouteservice"


def base_url(url: str) -> str:
    """The address to ask: the configured one, or the new one for the old host."""
    if urllib.parse.urlsplit(url).hostname == OLD_HOST:
        return NEW_BASE
    return url.rstrip("/")


def fetch(points: Points, ask: Ask | bool, key: str, url: str, timeout: float) -> list[Candidate]:
    ask = Ask.coerce(ask)
    body: dict = {"coordinates": [[lon, lat] for lat, lon in points], "instructions": False}
    avoid = (["highways"] if ask.off("freeways") else []) + (["tollways"] if ask.backroads else [])
    if avoid:
        body["options"] = {"avoid_features": avoid}
    if len(points) == 2:
        body["alternative_routes"] = {"target_count": 3, "share_factor": 0.6, "weight_factor": 1.6}
    try:
        answer = request_json(base_url(url) + "/v2/directions/driving-car/geojson", body=body,
                              headers={"Authorization": key}, timeout=timeout)
    except ProviderError as error:
        if str(error).startswith("HTTP 410"):
            raise ProviderError(f"HTTP 410: this address is gone; set providers.ors_url to {NEW_BASE}") from None
        raise
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
