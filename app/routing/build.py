"""Turns drivable ways into the router's map. The work is done by
build_stream.py, in bounded memory; this module keeps the old entry point
for in-memory ways (tests, small hand-made maps).

A map is a directory <region>.graph/ of arrays (see graph.py and disk.py).
"""
from __future__ import annotations

from pathlib import Path

from app.routing import build_stream
from app.routing.build_stream import FORMAT_VERSION, haversine  # noqa: F401  (re-exported)
from app.routing.osm_parse import Ways


def build(ways: Ways, out: Path, source: str, progress=print, budget_mb: int = build_stream.DEFAULT_BUDGET_MB) -> dict:
    return build_stream.build_from_ways(ways, out, source, budget_mb=budget_mb, progress=progress)
