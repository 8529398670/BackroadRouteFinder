"""Every map built so far, each opened when a route first needs it.

A map is a directory mapdata/<region>.graph/ (graph.py) with <region>.json
beside it naming the areas it holds (files.areas_of). An *area* map holds one
downloaded extract (a state, a country, or the whole US); a *joined* map holds
several, so a trip can cross from one into the next.

Each route picks its map from the points it drives through (pick): of the maps
whose bounding box holds them all, the smallest where every point has a road
near it. Opening a map maps its files from disk (a fraction of a second), so a
map is opened on first use and dropped again when it has not been used for a
while, or when more than routing.open_maps are open.

A trip no single map covers, whose points each lie on some area map, can have
a joined map built for it (join_for): those areas, plus any downloaded area
whose box meets the trip's (room for a detour through a neighbour). The
planner queues the build (tasks.Runner) and answers with the other engines
meanwhile. Nothing is ever downloaded here: only extracts the owner fetched.
"""
from __future__ import annotations

import json
import math
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Config
from app.routing import costs, files, regions, router
from app.routing.graph import Graph, load

# Metres per degree of latitude, for padding a trip's box.
METRES_PER_DEGREE = 111_320.0
JOIN_PADDING_M = 25_000.0


@dataclass
class MapInfo:
    name: str
    path: Path
    areas: list[str]
    bbox: list[float]           # south, west, north, east
    bytes: int
    built_at: str
    nodes: int = 0
    segments: int = 0

    @property
    def kind(self) -> str:
        return "area" if len(self.areas) == 1 else "joined"

    def holds(self, lat: float, lon: float, pad_deg: float = 0.0) -> bool:
        if len(self.bbox) != 4:
            return False
        south, west, north, east = self.bbox
        return south - pad_deg <= lat <= north + pad_deg and west - pad_deg <= lon <= east + pad_deg


@dataclass
class _Open:
    graph: Graph
    built_at: str
    used: float = field(default_factory=time.monotonic)


class MapSet:
    def __init__(self, folder: Path, cfg: Config | None = None) -> None:
        self.folder = folder
        self.cfg = cfg
        self.maps: dict[str, MapInfo] = {}
        self.errors: dict[str, str] = {}      # a map that would not open, and why
        self.last_used = ""                    # the map the last route used
        self._open: dict[str, _Open] = {}
        self._lock = threading.RLock()
        self.open_max = 4
        self.idle_seconds = 15 * 60.0

    # --- the index ------------------------------------------------------------

    def configure(self, open_max: int, idle_minutes: float) -> None:
        self.open_max = max(1, open_max)
        self.idle_seconds = max(60.0, idle_minutes * 60.0)

    def rescan(self) -> None:
        """Read which maps are built (after a build or a delete)."""
        found: dict[str, MapInfo] = {}
        if self.folder.is_dir():
            for path in sorted(self.folder.glob("*" + files.GRAPH_SUFFIX)):
                if not files.is_built(path):
                    continue
                name = files.region_of(path)
                try:
                    meta = json.loads((path / "meta.json").read_text())
                except (OSError, ValueError):
                    continue
                found[name] = MapInfo(name=name, path=path, areas=self._areas_of(name), bbox=list(meta.get("bbox", [])),
                                      bytes=files.size_of(path), built_at=str(meta.get("built_at", "")),
                                      nodes=int(meta.get("nodes", 0)), segments=int(meta.get("segments", 0)))
        with self._lock:
            self.maps = found
            for name in list(self._open):
                if name not in found or found[name].built_at != self._open[name].built_at:
                    del self._open[name]
            self.errors = {k: v for k, v in self.errors.items() if k in found}

    def _areas_of(self, name: str) -> list[str]:
        try:
            areas = json.loads((self.folder / f"{name}.json").read_text()).get("areas")
        except (OSError, ValueError):
            areas = None
        return areas if isinstance(areas, list) and areas else [name]

    def area_map(self, slug: str) -> MapInfo | None:
        """The map built from this one area alone, if there is one."""
        info = self.maps.get(slug)
        return info if info is not None and info.areas == [slug] else None

    def info(self, name: str) -> MapInfo | None:
        return self.maps.get(name)

    def has_maps(self) -> bool:
        return bool(self.maps)

    def open_names(self) -> list[str]:
        with self._lock:
            return list(self._open)

    def shown(self) -> MapInfo | None:
        """The map the page shows first: the last one used, else routing.region's,
        else the biggest."""
        for name in (self.last_used, self.cfg.file.routing.region if self.cfg else ""):
            if name in self.maps:
                return self.maps[name]
        return max(self.maps.values(), key=lambda m: m.bytes, default=None)

    # --- opening ---------------------------------------------------------------

    def get(self, name: str) -> Graph:
        """The map, opened if it is not yet. Raises KeyError for an unknown map,
        and whatever load raises for one that will not open."""
        with self._lock:
            info = self.maps[name]
            held = self._open.get(name)
            if held is not None and held.built_at == info.built_at:
                held.used = time.monotonic()
                return held.graph
        began = time.monotonic()
        try:
            graph = load(info.path)
        except Exception as error:
            traceback.print_exc()
            with self._lock:
                self.errors[name] = str(error)
            raise
        print(f"[routing] opened map {name}: {graph.node_count:,} intersections, {len(graph.seg_a):,} segments, "
              f"in {time.monotonic() - began:.1f}s", flush=True)
        with self._lock:
            self.errors.pop(name, None)
            self._open[name] = _Open(graph=graph, built_at=info.built_at)
            self._trim(keep=name)
            return graph

    def forget(self, name: str) -> None:
        """Drop an open map (before deleting it)."""
        with self._lock:
            self._open.pop(name, None)

    def _trim(self, keep: str) -> None:
        now = time.monotonic()
        for name, held in list(self._open.items()):
            if name != keep and now - held.used > self.idle_seconds:
                del self._open[name]
        while len(self._open) > self.open_max:
            oldest = min((n for n in self._open if n != keep), key=lambda n: self._open[n].used, default=None)
            if oldest is None:
                break
            del self._open[oldest]

    def tidy(self) -> None:
        """Drop maps left unused too long (called now and then)."""
        with self._lock:
            self._trim(keep="")

    # --- picking ---------------------------------------------------------------

    def _candidates(self, points: list[tuple[float, float]], only_areas: bool = False) -> list[MapInfo]:
        out = [m for m in self.maps.values() if m.name not in self.errors and all(m.holds(lat, lon) for lat, lon in points)
               and (not only_areas or m.kind == "area")]
        # Smallest first: an area map before a join holding it.
        return sorted(out, key=lambda m: (m.bytes, m.name))

    def pick(self, points: list[tuple[float, float]], max_snap_m: float, options: costs.Options | None = None,
             extent: list[tuple[float, float]] | None = None) -> tuple[str, Graph] | None:
        """The smallest map with a road near every point, or None. extent:
        corners of an area the route may reach (a loop's circle); a map whose
        box holds them too is preferred."""
        candidates = self._candidates(points)
        if extent:
            candidates.sort(key=lambda m: not all(m.holds(lat, lon) for lat, lon in extent))
        for info in candidates:
            try:
                graph = self.get(info.name)
            except Exception:  # noqa: BLE001 -- recorded in errors; try the next
                continue
            try:
                router.snap_all(graph, points, max_snap_m, options)
            except router.PointOffNetwork:
                continue
            with self._lock:
                self.last_used = info.name
            return info.name, graph
        return None

    def partial(self, points: list[tuple[float, float]], max_snap_m: float) -> tuple[str, Graph] | None:
        """A map with a road near any of the points, to score the part of an
        other engine's route it covers (first point first)."""
        for point in points:
            found = self.pick([point], max_snap_m)
            if found is not None:
                return found
        return None

    def join_for(self, points: list[tuple[float, float]], max_snap_m: float, max_mb: int) -> tuple[list[str], str]:
        """The areas a joined map for this trip would hold, and "" -- or [] and
        why not. Each point needs an area map with a road near it, and every
        area's extract still on disk."""
        if self.cfg is None:
            return [], "no settings"
        needed: list[str] = []
        for lat, lon in points:
            home = None
            for info in self._candidates([(lat, lon)], only_areas=True):
                try:
                    router.snap_all(self.get(info.name), [(lat, lon)], max_snap_m)
                except Exception:  # noqa: BLE001 -- off this map, or it will not open
                    continue
                home = info.areas[0]
                break
            if home is None:
                return [], "a point is on no map built so far"
            if home not in needed:
                needed.append(home)
        if len(needed) < 2:
            return [], "the points are on one area's map, but not all near its roads"
        # Room for a detour: a downloaded area bordering one the trip is on
        # (its box within JOIN_PADDING_M of that area's), where it meets the
        # trip's own box -- not every area between two far-apart points.
        south, north = min(p[0] for p in points), max(p[0] for p in points)
        west, east = min(p[1] for p in points), max(p[1] for p in points)
        pad_lat = JOIN_PADDING_M / METRES_PER_DEGREE
        pad_lon = pad_lat / max(0.1, math.cos(math.radians((south + north) / 2)))
        trip_box = [south - pad_lat, west - pad_lon, north + pad_lat, east + pad_lon]
        homes = [self.maps[a].bbox for a in needed if a in self.maps and len(self.maps[a].bbox) == 4]
        for info in self.maps.values():
            if info.kind != "area" or info.areas[0] in needed or len(info.bbox) != 4:
                continue
            near_home = any(_meets(info.bbox, [s - pad_lat, w - pad_lon, n + pad_lat, e + pad_lon]) for s, w, n, e in homes)
            if near_home and _meets(info.bbox, trip_box) and files.pbf_for(self.cfg, info.areas[0]).is_file():
                needed.append(info.areas[0])
        # The whole US holds every state: a join never needs both.
        if regions.US in needed:
            needed = [a for a in needed if a == regions.US or not (a in regions.STATES)]
        areas = regions.normalized(needed)
        missing = [a for a in areas if not files.pbf_for(self.cfg, a).is_file()]
        if missing:
            return [], f"the extract of {', '.join(regions.name_of(a) if regions.valid(a) else a for a in missing)} is no longer downloaded"
        total = sum(files.pbf_for(self.cfg, a).stat().st_size for a in areas)
        if total > max_mb * 1_000_000:
            return [], f"joining {len(areas)} areas ({total / 1e6:.0f} MB of extracts) is over routing.join_max_mb ({max_mb} MB)"
        return areas, ""


def _meets(a: list[float], b: list[float]) -> bool:
    """Whether two [south, west, north, east] boxes overlap."""
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


# --- building ------------------------------------------------------------------

def build_step(areas: list[str]) -> list[str]:
    """`manage build-graph` for one map of these areas (named regions.region_name)."""
    return ["manage", "build-graph", *[f"--state={a}" for a in regions.normalized(areas)]]


def queue_build(runner, maps: MapSet, title: str, steps: list[list[str]], key: str = ""):
    """Start (or queue) a task whose last step builds a map; once it is done the
    map set reads the new map, and routes use it from then on."""
    def then() -> str:
        maps.rescan()
        return f"ready: {len(maps.maps)} map(s) built"
    return runner.start(title, steps, then, key=key)


def queue_join(runner, maps: MapSet, areas: list[str]) -> str:
    """Queue a joined map of these areas (once); returns its name."""
    name = regions.region_name(areas)
    if name in maps.maps or runner.pending("join:" + name):
        return name
    names = ", ".join(regions.name_of(a) for a in areas)
    print(f"[routing] queued a joined map of {names} for a trip across them", flush=True)
    queue_build(runner, maps, f"Join for a trip: {names}", [build_step(areas)], key="join:" + name)
    return name


def unbuilt_areas(cfg: Config, maps: MapSet) -> list[str]:
    """Downloaded extracts with no map of their own yet."""
    folder = files.mapdata_dir(cfg)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.osm.pbf")):
        slug = path.name.removesuffix(".osm.pbf")
        if regions.valid(slug) and maps.area_map(slug) is None:
            out.append(slug)
    return out


def queue_area_builds(runner, maps: MapSet, cfg: Config) -> list[str]:
    """Build each downloaded area that has no map of its own (once, from the
    server's start): a map joined from several areas does not count."""
    slugs = unbuilt_areas(cfg, maps)
    for slug in slugs:
        print(f"[routing] queued a map of {regions.name_of(slug)} on its own", flush=True)
        queue_build(runner, maps, f"Build: {regions.name_of(slug)}", [build_step([slug])], key="area:" + slug)
    return slugs


def can_build() -> bool:
    """Whether osmium, which building needs, is installed here."""
    import importlib.util

    return importlib.util.find_spec("osmium") is not None
