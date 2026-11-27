"""Other routing engines, asked beside our own router: each one's route is
laid on our road graph (app/routing/match.py) so every route is measured the
same way -- how long it takes, and how much of it is on main roads -- and the
best one can be picked whoever planned it.

One module per engine. Each has an ENGINE (base.Engine) saying what it is
called, whether it needs an API key, where to get one, and a fetch() that
turns waypoints into candidate lines. ENGINES lists them in the order the
screens show them.

Keys are entered on the Settings screen: each person's own are sealed on
their account, and the owner's shared ones are written to config.yaml's
provider_keys section (app/models/provider_keys.py). Free engines' addresses are in config.yaml's
providers section; an empty address switches that engine off.
"""
from __future__ import annotations

from app.routing.providers import google, graphhopper, here, mapbox, ors, osrm, tomtom, valhalla
from app.routing.providers.base import Candidate, Engine, ProviderError

ENGINES: dict[str, Engine] = {
    engine.id: engine
    for engine in (
        valhalla.ENGINE,
        osrm.ENGINE,
        ors.ENGINE,
        graphhopper.ENGINE,
        tomtom.ENGINE,
        here.ENGINE,
        mapbox.ENGINE,
        google.ENGINE,
    )
}

# Our own router, as the planner lists it beside the others.
OURS = "ours"

__all__ = ["ENGINES", "OURS", "Candidate", "Engine", "ProviderError"]
