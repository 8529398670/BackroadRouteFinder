"""Google Routes API (https://developers.google.com/maps/documentation/routes):
API key on a billed Google Cloud project, with a free monthly allowance.

Backroads: avoidHighways and avoidTolls -- which still allows US and state
routes, so it is the weakest at staying off main roads. Alternatives on a
two-point trip."""
from __future__ import annotations

from app.routing.providers import polyline
from app.routing.providers.base import Ask, Candidate, Engine, Points, ProviderError, request_json

URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Google answers a bare 403 with no reason when the key's Cloud project has
# no open billing account linked: none, or a closed one (the console still
# shows it as linked). Or the Routes API is on in another project.
BILLING_HINT = ("check that billing is enabled on the key's Google Cloud project "
                "(a linked billing account that was closed counts as none), "
                "and that the Routes API is enabled in that same project")


def _place(lat: float, lon: float) -> dict:
    return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}


def fetch(points: Points, ask: Ask | bool, key: str, url: str, timeout: float) -> list[Candidate]:
    ask = Ask.coerce(ask)
    body: dict = {
        "origin": _place(*points[0]),
        "destination": _place(*points[-1]),
        "intermediates": [_place(lat, lon) | {"via": True} for lat, lon in points[1:-1]],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
        "computeAlternativeRoutes": len(points) == 2,
        "routeModifiers": {"avoidHighways": ask.off("freeways"), "avoidTolls": ask.backroads},
    }
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline"}
    try:
        answer = request_json(URL, body=body, headers=headers, timeout=timeout)
    except ProviderError as error:
        if str(error).startswith("HTTP 403") and "caller does not have permission" in str(error):
            raise ProviderError(f"{error} -- {BILLING_HINT}") from None
        raise
    out = []
    for route in answer.get("routes", []):
        seconds = float(str(route.get("duration", "0s")).rstrip("s") or 0)
        out.append(Candidate(coords=polyline.decode(route.get("polyline", {}).get("encodedPolyline", ""), 5),
                             seconds=seconds, meters=float(route.get("distanceMeters", 0))))
    if not out:
        raise ProviderError("no route found")
    return out


ENGINE = Engine(
    id="google", name="Google", needs_key=True, url_key="",
    signup_url="https://console.cloud.google.com/google/maps-apis/start",
    free_tier="Free monthly allowance on a billed Cloud project (Routes API).",
    backroads="Avoids freeways and tolls only; US and state routes stay in.",
    fetch=fetch,
    honours=frozenset({"freeways"}),
)
