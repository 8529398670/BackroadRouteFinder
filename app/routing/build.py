"""Turns the drivable ways from osm_parse into the router's graph file.

A way is cut into segments at every node it shares with another way (an
intersection) and at its two ends. Each segment keeps the way's class, speed,
oneway, surface and name, and its own slice of the way's geometry for drawing.
Only the largest strongly connected part of the network is kept, so a start
point can never snap onto a parking-lot island or a oneway trap with no way
out.

The file is a plain .npz of arrays (no pickled objects, so loading it runs no
code). Its layout:

  node_lat, node_lon            float64 per graph node (intersection)
  seg_a, seg_b                  int32 node index at each end of a segment
  seg_len                       float32 metres
  seg_kmh                       float32
  seg_class                     uint8 index into classes.CLASSES
  seg_oneway                    int8: 1 a->b only, -1 b->a only, 0 both
  seg_unpaved                   bool
  seg_name                      int32 index into names
  seg_g0, seg_g1                int64 first and last index into geom_* (inclusive)
  seg_stop_a, seg_stop_b        bool: a stop sign where the segment meets its a / b end
  node_signal                   bool: traffic signals at the intersection
  node_stop                     bool: a stop sign on the intersection itself (all-way)
  geom_lat, geom_lon            float64, the segments' shapes, a to b
  names                         utf-8 names joined by newlines
  meta                          utf-8 JSON: version, source, build time, counts
"""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from app.routing import classes
from app.routing.osm_parse import Ways

FORMAT_VERSION = 2
# A signal or stop sign this close to an intersection belongs to it: mappers
# often put them on the approach, a few metres before the junction node.
CONTROL_NEAR_M = 40.0
EARTH_RADIUS_M = 6_371_008.8


def haversine(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    """Metres between point pairs, element-wise."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.minimum(a, 1.0)))


def _ranges(starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """The indices start..end (inclusive) of every pair, concatenated."""
    lengths = ends - starts + 1
    offsets = np.repeat(np.cumsum(lengths) - lengths, lengths)
    return np.repeat(starts, lengths) + (np.arange(lengths.sum()) - offsets)


def build(ways: Ways, out: Path, source: str, progress=print) -> dict:
    began = time.monotonic()
    positions = len(ways.refs)
    way_count = len(ways.road_class)
    way_of = np.repeat(np.arange(way_count, dtype=np.int64), np.diff(ways.starts))
    first = ways.starts[:-1]
    last = ways.starts[1:] - 1

    # Which positions are intersections: a node any way uses twice or more
    # (two ways, or one way crossing itself), plus every way's two ends.
    _, node_key, uses = np.unique(ways.refs, return_inverse=True, return_counts=True)
    cut = uses[node_key] >= 2
    cut[first] = True
    cut[last] = True

    # Distance along each way, so a segment's length is a subtraction.
    step = np.zeros(positions, dtype=np.float64)
    step[:-1] = haversine(ways.lat[:-1], ways.lon[:-1], ways.lat[1:], ways.lon[1:])
    step[last] = 0.0
    along = np.concatenate(([0.0], np.cumsum(step)))

    cuts = np.flatnonzero(cut)
    g0, g1 = cuts[:-1], cuts[1:]
    same_way = way_of[g0] == way_of[g1]
    g0, g1 = g0[same_way], g1[same_way]
    seg_way = way_of[g0]
    seg_a, seg_b = node_key[g0], node_key[g1]
    seg_len = along[g1] - along[g0]
    keep = seg_a != seg_b
    g0, g1, seg_way, seg_a, seg_b, seg_len = g0[keep], g1[keep], seg_way[keep], seg_a[keep], seg_b[keep], seg_len[keep]
    progress(f"  {len(g0):,} segments between intersections")

    # Traffic signals and stop signs, onto the intersections they control.
    node_count = int(node_key.max()) + 1
    node_signal = np.zeros(node_count, dtype=bool)
    node_stop = np.zeros(node_count, dtype=bool)
    stop_a = np.zeros(len(g0), dtype=bool)
    stop_b = np.zeros(len(g0), dtype=bool)
    for ids, at_node, at_end in ((ways.signals, node_signal, None), (ways.stops, node_stop, (stop_a, stop_b))):
        positions = np.flatnonzero(np.isin(ways.refs, ids))
        on_node = positions[cut[positions]]
        at_node[node_key[on_node]] = True
        between = positions[~cut[positions]]
        k = np.searchsorted(g0, between, side="right") - 1
        valid = (k >= 0) & (g0[np.maximum(k, 0)] < between) & (between < g1[np.maximum(k, 0)])
        k, between = k[valid], between[valid]
        to_a = along[between] - along[g0[k]]
        to_b = along[g1[k]] - along[between]
        near_a = (to_a <= to_b) & (to_a <= CONTROL_NEAR_M)
        near_b = (to_b < to_a) & (to_b <= CONTROL_NEAR_M)
        if at_end is None:
            at_node[seg_a[k[near_a]]] = True
            at_node[seg_b[k[near_b]]] = True
        else:
            at_end[0][k[near_a]] = True
            at_end[1][k[near_b]] = True
    progress(f"  {int(node_signal.sum()):,} signalled intersections, {int(stop_a.sum() + stop_b.sum() + node_stop.sum()):,} stop signs placed")

    # Keep the largest strongly connected component.
    used, dense = np.unique(np.concatenate((seg_a, seg_b)), return_inverse=True)
    a, b = dense[: len(seg_a)], dense[len(seg_a):]
    oneway = ways.oneway[seg_way]
    forward = oneway >= 0
    backward = oneway <= 0
    src = np.concatenate((a[forward], b[backward]))
    dst = np.concatenate((b[forward], a[backward]))
    count = len(used)
    matrix = coo_matrix((np.ones(len(src), dtype=np.int8), (src, dst)), shape=(count, count)).tocsr()
    _, labels = connected_components(matrix, directed=True, connection="strong")
    biggest = np.bincount(labels).argmax()
    in_main = labels == biggest
    keep = in_main[a] & in_main[b]
    progress(f"  largest connected network: {int(in_main.sum()):,} of {count:,} intersections")

    # Re-number what is left: nodes, then geometry.
    kept_nodes = np.flatnonzero(in_main)
    renumber = np.full(count, -1, dtype=np.int64)
    renumber[kept_nodes] = np.arange(len(kept_nodes))
    g0, g1, seg_way, seg_len = g0[keep], g1[keep], seg_way[keep], seg_len[keep]
    stop_a, stop_b = stop_a[keep], stop_b[keep]
    node_signal = node_signal[used[kept_nodes]]
    node_stop = node_stop[used[kept_nodes]]
    new_a, new_b = renumber[a[keep]], renumber[b[keep]]

    # A node's position: where its first segment starts or ends.
    node_lat = np.zeros(len(kept_nodes))
    node_lon = np.zeros(len(kept_nodes))
    node_lat[new_b], node_lon[new_b] = ways.lat[g1], ways.lon[g1]
    node_lat[new_a], node_lon[new_a] = ways.lat[g0], ways.lon[g0]

    gather = _ranges(g0, g1)
    lengths = g1 - g0 + 1
    new_g0 = np.cumsum(lengths) - lengths
    new_g1 = new_g0 + lengths - 1

    meta = {
        "version": FORMAT_VERSION,
        "source": source,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nodes": int(len(kept_nodes)),
        "segments": int(len(g0)),
        "geometry_points": int(len(gather)),
        "signals": int(node_signal.sum()),
        "stop_signs": int(stop_a.sum() + stop_b.sum() + node_stop.sum()),
        "bbox": [float(node_lat.min()), float(node_lon.min()), float(node_lat.max()), float(node_lon.max())],
        "classes": list(classes.CLASSES),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + ".part.npz")
    np.savez(
        temporary,
        node_lat=node_lat,
        node_lon=node_lon,
        seg_a=new_a.astype(np.int32),
        seg_b=new_b.astype(np.int32),
        seg_len=seg_len.astype(np.float32),
        seg_kmh=ways.kmh[seg_way],
        seg_class=ways.road_class[seg_way],
        seg_oneway=ways.oneway[seg_way],
        seg_unpaved=ways.unpaved[seg_way],
        seg_name=ways.name_id[seg_way],
        seg_g0=new_g0,
        seg_g1=new_g1,
        seg_stop_a=stop_a,
        seg_stop_b=stop_b,
        node_signal=node_signal,
        node_stop=node_stop,
        geom_lat=ways.lat[gather],
        geom_lon=ways.lon[gather],
        names=np.frombuffer("\n".join(ways.names).encode("utf-8"), dtype=np.uint8),
        meta=np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8),
    )
    os.replace(temporary, out)
    meta["seconds"] = round(time.monotonic() - began, 1)
    meta["bytes"] = out.stat().st_size
    return meta
