"""HERE Routing v8 (https://developer.here.com): API key, free monthly
allowance.

Backroads: avoid controlled-access highways and toll roads. Alternatives up
to three."""
from __future__ import annotations

from app.routing.providers import polyline
from app.routing.providers.base import Candidate, Engine, Points, ProviderError, query, request_json

URL = "https://router.hereapi.com/v8/routes"


def fetch(points: Points, backroads: bool, key: str, url: str, timeout: float) -> list[Candidate]:
    params: list[tuple[str, str]] = [
        ("transportMode", "car"),
        ("origin", f"{points[0][0]:.6f},{points[0][1]:.6f}"),
        ("destination", f"{points[-1][0]:.6f},{points[-1][1]:.6f}"),
        ("return", "polyline,summary"),
        ("alternatives", "3"),
        ("apiKey", key),
    ]
    params += [("via", f"{lat:.6f},{lon:.6f}!passThrough=true") for lat, lon in points[1:-1]]
    if backroads:
        params.append(("avoid[features]", "controlledAccessHighway,tollRoad"))
    answer = request_json(f"{URL}?{query(params)}", timeout=timeout)
    out = []
    for route in answer.get("routes", []):
        coords: list[tuple[float, float]] = []
        seconds = meters = 0.0
        for section in route.get("sections", []):
            line = polyline.decode_flexible(section.get("polyline", ""))
            coords.extend(line[1:] if coords and line and coords[-1] == line[0] else line)
            summary = section.get("summary", {})
            seconds += float(summary.get("duration", 0))
            meters += float(summary.get("length", 0))
        out.append(Candidate(coords=coords, seconds=seconds, meters=meters))
    if not out:
        raise ProviderError(answer.get("notices", [{}])[0].get("title", "no route found") if answer.get("notices") else "no route found")
    return out


ENGINE = Engine(
    id="here", name="HERE", needs_key=True, url_key="",
    signup_url="https://platform.here.com/sign-up",
    free_tier="Free monthly allowance on the base plan (card required).",
    backroads="Avoids controlled-access highways and toll roads.",
    fetch=fetch,
)
