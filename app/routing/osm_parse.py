"""Reads an OpenStreetMap .pbf extract: the ways a car may drive on, handed a
way at a time to the streaming build (build_stream.py), which writes them to
disk. Needs osmium (app/requirements-build.txt), so only `manage build-graph`
imports read_into.

Ways is the same data held in memory, for tests and small hand-made maps
(build_stream.build_from_ways).
"""
from __future__ import annotations

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
    category: np.ndarray    # uint8 index into classes.CATEGORIES, per way
    restricted: np.ndarray  # bool per way: private, permit, farm access...
    name_id: np.ndarray     # int32 per way, index into names
    names: list[str]        # "" is index 0
    signals: np.ndarray     # int64 ids of highway=traffic_signals nodes
    stops: np.ndarray       # int64 ids of highway=stop nodes
    way_ids: np.ndarray     # int64 OSM way id per way, so extracts can be merged


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


def _access(tags, highway: str) -> str:
    """"open", "restricted" (private, permit, farm access: kept, flagged, for
    the restricted road rule) or "closed" (not a car's road at all)."""
    if highway not in classes.CLASS_INDEX:
        return "closed"
    if tags.get("area", "") == "yes":
        return "closed"
    if highway == "service" and tags.get("service", "") in classes.SKIPPED_SERVICE:
        return "closed"
    for key in ("motor_vehicle", "motorcar"):
        value = tags.get(key, "")
        if value in classes.YES_ACCESS:
            return "open"
        if value in classes.CLOSED_ACCESS:
            return "closed"
        if value in classes.NO_ACCESS:
            return "restricted"
    value = tags.get("access", "")
    if value in classes.CLOSED_ACCESS:
        return "closed"
    return "restricted" if value in classes.NO_ACCESS else "open"


def _unpaved(tags, highway: str) -> bool:
    surface = tags.get("surface", "")
    if surface:
        return surface in classes.UNPAVED
    # An untagged track is almost always dirt or gravel.
    return highway == "track"


def read_into(pbf: Path, writer, index_path: Path, progress=print) -> None:
    """Appends the extract's drivable ways to writer (build_stream.WayWriter).
    Node locations go through an osmium index on disk (index_path), so even a
    large extract's billions of nodes stay out of memory; the index is
    removed afterwards."""
    import osmium  # noqa: PLC0415  (build-time only)

    index_path.unlink(missing_ok=True)
    processor = (
        osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY)
        .with_locations(f"sparse_file_array,{index_path}")
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    seen = kept = refs_total = signals = stops = 0
    try:
        for item in processor:
            if item.is_node():
                control = item.tags.get("highway", "")
                if control == "traffic_signals":
                    writer.control(item.id, "signal")
                    signals += 1
                elif control == "stop":
                    writer.control(item.id, "stop")
                    stops += 1
                continue
            seen += 1
            if seen % 500_000 == 0:
                progress(f"  read {seen:,} highway ways, kept {kept:,}")
            tags = item.tags
            highway = tags.get("highway", "")
            access = _access(tags, highway)
            if access == "closed" or len(item.nodes) < 2:
                continue
            way_refs, way_lat, way_lon = [], [], []
            ok = True
            for node in item.nodes:
                location = node.location
                if not location.valid():
                    ok = False
                    break
                way_refs.append(node.ref)
                way_lat.append(location.lat)
                way_lon.append(location.lon)
            if not ok:
                continue
            name = tags.get("name", "")
            ref = tags.get("ref", "").replace(";", ", ")
            label = " ".join((f"{name} ({ref})" if name and ref else name or ref).split())
            writer.add(way_refs, way_lat, way_lon,
                       road_class=classes.CLASS_INDEX[highway],
                       kmh=classes.speed_kmh(highway, tags.get("maxspeed")),
                       oneway=_oneway(tags, highway),
                       unpaved=_unpaved(tags, highway),
                       category=classes.CATEGORY_INDEX[classes.category_of(highway, tags.get("ref", ""))],
                       restricted=access == "restricted",
                       name=writer.name_id(label),
                       way_id=item.id)
            kept += 1
            refs_total += len(way_refs)
    finally:
        index_path.unlink(missing_ok=True)
    progress(f"  read {seen:,} highway ways, kept {kept:,} drivable, {refs_total:,} node references; "
             f"{signals:,} traffic signals, {stops:,} stop signs")
