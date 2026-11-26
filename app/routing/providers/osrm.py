"""OSRM (https://project-osrm.org): the public demo server by default, no
key; or your own (providers.osrm_url).

The demo's car profile can exclude motorways, nothing finer, so backroads
mode only drops freeways. Alternatives on a two-point trip."""
from __future__ import annotations

from app.routing.providers import polyline
from app.routing.providers.base import Candidate, Engine, Points, ProviderError, query, request_json


def _ask(points: Points, url: str, timeout: float, exclude: bool) -> dict:
    where = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    params = {"overview": "full", "geometries": "polyline6", "alternatives": "true" if len(points) == 2 else "false", "steps": "false"}
    if exclude:
        params["exclude"] = "motorway"
    return request_json(f"{url.rstrip('/')}/route/v1/driving/{where}?{query(params)}", timeout=timeout)


def fetch(points: Points, backroads: bool, key: str, url: str, timeout: float) -> list[Candidate]:
    try:
        answer = _ask(points, url, timeout, backroads)
    except ProviderError:
        if not backroads:
            raise
        # A server whose profile cannot exclude motorways: its plain route.
        answer = _ask(points, url, timeout, False)
    if answer.get("code") != "Ok":
        raise ProviderError(answer.get("message") or answer.get("code") or "no route found")
    return [Candidate(coords=polyline.decode(r["geometry"], 6), seconds=float(r["duration"]), meters=float(r["distance"]))
            for r in answer.get("routes", [])]


ENGINE = Engine(
    id="osrm", name="OSRM", needs_key=False, url_key="osrm_url",
    signup_url="https://project-osrm.org",
    free_tier="Free public demo server, light use only; or self-host.",
    backroads="Can only leave out freeways.",
    fetch=fetch,
)
