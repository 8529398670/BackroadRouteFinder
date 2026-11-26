"""What every engine module provides, and the HTTP they share.

fetch(points, ask, key, url, timeout) asks the engine for routes through
points ([(lat, lon), ...]) and returns candidates, best first by the engine's
own measure. ask (an Ask) is the planner's road rules in the few words every
engine shares: backroads mode, and use / avoid / never for freeways, other
major roads (US highways and state routes), unpaved roads and tracks. Each
engine says what it can of that (avoid motorways, a lower priority for trunk
and primary roads, leave out unpaved roads, a "thrilling" route type) and
lists the rules it follows in Engine.honours; whatever it cannot say is still
counted when its route is scored on our map.

Answers are cached for a while (CACHE_SECONDS) by engine, points and ask, so
moving the backroads slider -- which re-plans -- does not spend a free tier's
daily allowance: only a new or moved stop asks again.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from app.routing.files import USER_AGENT

CACHE_SECONDS = 15 * 60
CACHE_SIZE = 300

Points = list[tuple[float, float]]


class ProviderError(Exception):
    """The engine could not plan the route: a refused key, a quota, a trip it
    does not cover, or no answer."""


USE, AVOID, NEVER = "use", "avoid", "never"
_STRENGTH = {USE: 0, AVOID: 1, NEVER: 2}


@dataclass(frozen=True)
class Ask:
    """The road rules as every engine can be told them. An "avoid" only
    counts in backroads mode (penalty above 1), as on our own map."""

    backroads: bool = False
    freeways: str = USE      # Interstates (and other motorways)
    major: str = USE         # US highways and state routes
    unpaved: str = USE
    tracks: str = USE

    @classmethod
    def from_rules(cls, rules: Mapping[str, str], backroads: bool) -> "Ask":
        def rule(*keys: str) -> str:
            value = max((rules.get(key, USE) for key in keys), key=lambda v: _STRENGTH.get(v, 0))
            return USE if value == AVOID and not backroads else value
        return cls(backroads=backroads, freeways=rule("interstate"), major=rule("us_highway", "state_route"),
                   unpaved=rule("unpaved"), tracks=rule("track"))

    @classmethod
    def coerce(cls, value: "Ask | bool") -> "Ask":
        """An Ask from a plain backroads on/off, for callers that only have
        that (the Settings screen's engine test): the default rules."""
        if isinstance(value, Ask):
            return value
        if value:
            return cls(backroads=True, freeways=AVOID, major=AVOID, unpaved=AVOID, tracks=NEVER)
        return cls(tracks=NEVER)

    def off(self, name: str) -> bool:
        """Should the engine keep off this kind of road (avoid or never)."""
        return getattr(self, name) != USE


@dataclass
class Candidate:
    coords: list[tuple[float, float]]   # (lat, lon), in the order driven
    seconds: float                      # the engine's own drive time
    meters: float                       # the engine's own distance


@dataclass(frozen=True)
class Engine:
    id: str
    name: str
    needs_key: bool
    # config.yaml providers.<id>_url holds its address (free and self-hostable
    # engines); empty when the address is fixed.
    url_key: str
    signup_url: str
    # One line each, shown on the Settings card.
    free_tier: str
    backroads: str
    fetch: Callable[[Points, Ask, str, str, float], list[Candidate]] = field(repr=False, compare=False)
    # The Ask fields it can act on (freeways, major, unpaved, tracks).
    honours: frozenset[str] = frozenset()


# --- HTTP ---------------------------------------------------------------------


def request_json(url: str, *, body: Any = None, headers: dict[str, str] | None = None, timeout: float = 20.0) -> Any:
    """GET (or POST when body is given) and decode the JSON answer. An error
    answer becomes a ProviderError carrying the engine's own message, with any
    key in the address left out."""
    data = None
    sent = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=sent, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- fixed or validated https addresses
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ProviderError(f"HTTP {error.code}: {_error_text(error)}") from None
    except (OSError, ValueError) as error:
        raise ProviderError(f"no answer: {error}") from None


def _error_text(error: urllib.error.HTTPError) -> str:
    try:
        raw = error.read()[:4000]
        decoded = json.loads(raw)
    except (OSError, ValueError):
        return error.reason if isinstance(error.reason, str) else "refused"
    return _find_message(decoded) or (error.reason if isinstance(error.reason, str) else "refused")


def _find_message(value: Any) -> str:
    """The first human message in an engine's error JSON, whatever its shape."""
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, dict):
        for name in ("message", "error_message", "errorMessage", "title", "detail", "error", "cause", "status_message"):
            if name in value:
                found = _find_message(value[name])
                if found:
                    return found
        for item in value.values():
            if isinstance(item, (dict, list)):
                found = _find_message(item)
                if found:
                    return found
    if isinstance(value, list):
        for item in value:
            found = _find_message(item)
            if found:
                return found
    return ""


def query(params: dict[str, Any] | list[tuple[str, Any]]) -> str:
    return urllib.parse.urlencode(params, safe=",:;")


# --- the cache ------------------------------------------------------------------

_lock = threading.Lock()
_cache: OrderedDict[tuple, tuple[float, list[Candidate]]] = OrderedDict()


def cached_fetch(engine: Engine, points: Points, ask: Ask, key: str, url: str, timeout: float) -> list[Candidate]:
    marker = (engine.id, tuple((round(lat, 5), round(lon, 5)) for lat, lon in points), ask, url, key[-6:])
    with _lock:
        hit = _cache.get(marker)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            _cache.move_to_end(marker)
            return hit[1]
    found = engine.fetch(points, ask, key, url, timeout)
    if not found:
        raise ProviderError("no route found")
    with _lock:
        _cache[marker] = (time.time(), found)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return found


def clear_cache() -> None:
    with _lock:
        _cache.clear()
