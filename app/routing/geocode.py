"""Place search: a typed address or place name -> points, by asking a
Nominatim server (routing.geocoder_url) from this server, never from the
browser, so the page's Content-Security-Policy stays `connect-src 'self'`.

The public Nominatim's usage policy asks for at most one request a second, an
identifying User-Agent and caching, so all three are here: one lock spaces
every call a second apart whoever makes it, and answers are kept in memory for
a day. Searches are limited to the map's own area.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass

from app.routing.files import USER_AGENT

MIN_INTERVAL = 1.05
CACHE_SIZE = 500
CACHE_SECONDS = 24 * 3600
TIMEOUT = 10


class GeocodeError(Exception):
    pass


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float


_lock = threading.Lock()
_last_call = 0.0
_cache: OrderedDict[tuple, tuple[float, list[Place]]] = OrderedDict()


def search(base_url: str, query: str, bbox: list[float], limit: int = 6, lang: str = "") -> list[Place]:
    """bbox is [south, west, north, east]: places there first (empty: none).
    lang (e.g. "en"): names in that language where Nominatim knows them."""
    global _last_call
    key = (base_url, query.strip().lower(), tuple(round(v, 3) for v in bbox), limit, lang)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            _cache.move_to_end(key)
            return hit[1]
        wait = _last_call + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        fields: dict[str, object] = {"q": query.strip(), "format": "jsonv2", "limit": limit, "addressdetails": 0}
        if lang:
            fields["accept-language"] = lang
        if len(bbox) == 4:
            # Places in view come first; the rest of the world still answers.
            south, west, north, east = bbox
            fields["viewbox"] = f"{west},{north},{east},{south}"
        params = urllib.parse.urlencode(fields)
        request = urllib.request.Request(f"{base_url.rstrip('/')}/search?{params}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 -- https only (validated)
                rows = json.loads(response.read())
        except (OSError, ValueError) as error:
            raise GeocodeError(f"place search failed: {error}") from None
        places = [Place(name=row.get("display_name", ""), lat=float(row["lat"]), lon=float(row["lon"]))
                  for row in rows if "lat" in row and "lon" in row]
        _cache[key] = (time.time(), places)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
        return places
