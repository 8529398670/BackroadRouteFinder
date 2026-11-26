"""Reads an OpenStreetMap .pbf extract into flat arrays of the ways a car may
drive on. Needs osmium (app/requirements-build.txt), so only `manage
build-graph` imports this module.

Everything goes into flat typed arrays rather than per-way Python objects: an
extract the size of Ohio has around a million road ways and ten million node
references, and a list of floats per way would need gigabytes.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.routing import classes


@dataclass
class Ways:
    """Every drivable way, flattened. Way w's nodes are positions
    starts[w]..starts[w+1]-1 of refs/lat/lon."""

    refs: np.ndarray        # int64 OSM node id per position
    lat: np.ndarray         # float64 per position
    lon: np.ndarray         # float64 per position
    starts: np.ndarray      # int64, one per way plus a final end
    road_class: np.ndarray  # uint8 index into classes.CLASSES, per way
    kmh: np.ndarray         # float32 per way
    oneway: np.ndarray      # int8 per way: 1 forward only, -1 backward only, 0 both
    unpaved: np.ndarray     # bool per way
    name_id: np.ndarray     # int32 per way, index into names
    names: list[str]        # "" is index 0
    signals: np.ndarray     # int64 ids of highway=traffic_signals nodes
    stops: np.ndarray       # int64 ids of highway=stop nodes


def _oneway(tags, highway: str) -> int:
    value = tags.get("oneway", "")
    if value in ("yes", "true", "1"):
        return 1
    if value in ("-1", "reverse"):
        return -1
    if value == "no":
        return 0
    if highway in classes.IMPLIED_ONEWAY or tags.get("junction", "") in ("roundabout", "circular"):
        return 1
    return 0


def _drivable(tags, highway: str) -> bool:
    if highway not in classes.CLASS_INDEX:
        return False
    if tags.get("area", "") == "yes":
        return False
    if highway == "service" and tags.get("service", "") in classes.SKIPPED_SERVICE:
        return False
    for key in ("motor_vehicle", "motorcar"):
        value = tags.get(key, "")
        if value in classes.YES_ACCESS:
            return True
        if value in classes.NO_ACCESS:
            return False
    return tags.get("access", "") not in classes.NO_ACCESS


def _unpaved(tags, highway: str) -> bool:
    surface = tags.get("surface", "")
    if surface:
        return surface in classes.UNPAVED
    # An untagged track is almost always dirt or gravel.
    return highway == "track"


def read(pbf: Path, progress=print) -> Ways:
    import osmium  # noqa: PLC0415  (build-time only)

    refs, lat, lon = array("q"), array("d"), array("d")
    starts = array("q", [0])
    road_class, kmh, oneway, unpaved, name_id = array("B"), array("f"), array("b"), array("B"), array("i")
    names: list[str] = [""]
    name_index: dict[str, int] = {"": 0}

    signals, stops = array("q"), array("q")

    # Nodes and ways with a highway tag: the ways are the roads, the nodes the
    # traffic signals and stop signs on them. Every node still passes through
    # the location handler first, so the ways get their coordinates.
    processor = (
        osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY)
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    seen = 0
    for way in processor:
        if way.is_node():
            control = way.tags.get("highway", "")
            if control == "traffic_signals":
                signals.append(way.id)
            elif control == "stop":
                stops.append(way.id)
            continue
        seen += 1
        if seen % 500_000 == 0:
            progress(f"  read {seen:,} highway ways, kept {len(road_class):,}")
        tags = way.tags
        highway = tags.get("highway", "")
        if not _drivable(tags, highway) or len(way.nodes) < 2:
            continue
        ok = True
        way_refs, way_lat, way_lon = [], [], []
        for node in way.nodes:
            location = node.location
            if not location.valid():
                ok = False
                break
            way_refs.append(node.ref)
            way_lat.append(location.lat)
            way_lon.append(location.lon)
        if not ok:
            continue
        refs.extend(way_refs)
        lat.extend(way_lat)
        lon.extend(way_lon)
        starts.append(len(refs))
        road_class.append(classes.CLASS_INDEX[highway])
        kmh.append(classes.speed_kmh(highway, tags.get("maxspeed")))
        oneway.append(_oneway(tags, highway))
        unpaved.append(1 if _unpaved(tags, highway) else 0)
        name = tags.get("name", "")
        ref = tags.get("ref", "").replace(";", ", ")
        label = f"{name} ({ref})" if name and ref else name or ref
        # Names are stored joined by newlines (build.py).
        label = " ".join(label.split())
        if label not in name_index:
            name_index[label] = len(names)
            names.append(label)
        name_id.append(name_index[label])

    progress(f"  read {seen:,} highway ways, kept {len(road_class):,} drivable, {len(refs):,} node references; "
             f"{len(signals):,} traffic signals, {len(stops):,} stop signs")
    return Ways(
        refs=np.frombuffer(refs, dtype=np.int64).copy(),
        lat=np.frombuffer(lat, dtype=np.float64).copy(),
        lon=np.frombuffer(lon, dtype=np.float64).copy(),
        starts=np.frombuffer(starts, dtype=np.int64).copy(),
        road_class=np.frombuffer(road_class, dtype=np.uint8).copy(),
        kmh=np.frombuffer(kmh, dtype=np.float32).copy(),
        oneway=np.frombuffer(oneway, dtype=np.int8).copy(),
        unpaved=np.frombuffer(unpaved, dtype=np.uint8).astype(bool),
        name_id=np.frombuffer(name_id, dtype=np.int32).copy(),
        names=names,
        signals=np.frombuffer(signals, dtype=np.int64).copy(),
        stops=np.frombuffer(stops, dtype=np.int64).copy(),
    )
