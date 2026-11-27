"""Builds the router's map in bounded memory, so a map of the whole country
can be built on a small machine.

Every step reads and writes arrays on disk (disk.py) a chunk at a time, and
the steps that need a whole array sorted or joined -- which nodes are
intersections, spatial order, edges by start node -- scatter rows into
bucket files that each fit the memory budget and handle one bucket at a
time. The budget (routing.build_memory_mb) sets the chunk and bucket sizes;
the peak is a little above it, plus osmium's own buffers.

The steps:

  A  ways      the drivable ways from each extract, appended to files, with
               node locations from an on-disk osmium index (osm_parse.read_into)
  A2 dedupe    a way found in two extracts (one crossing a border) kept once
  B  junctions which nodes are intersections (used twice, or a way's end),
               each given a node id; signals and stop signs onto positions
  C  segments  ways cut at intersections, a chunk of ways at a time
  D  order     nodes in spatial order (by 1-degree tile, then 2 km cell), and
               segments by their first node, so a route reads nearby pages
  E  islands   per tile, with its neighbours round it, the largest strongly
               connected network is kept, so a point never snaps onto a
               parking-lot island or a oneway trap; separate networks far
               apart (Hawaii's islands) each keep their own
  F  write     the kept segments, nodes and geometry in their final order
  G  edges     directed edges sorted by start node
  H  turns     every move from one edge to the next, priced (turns.py)
  I  grid      which 2 km cells each segment passes through, for snapping
               and corridors (grid.py)

The result is a directory <region>.graph/ (see graph.py for its arrays).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

from app.routing import classes, grid, turns
from app.routing.disk import ArrayDir, Partitions, chunks, peak_rss_mb, ranges, rows_for

FORMAT_VERSION = 4
CONTROL_NEAR_M = 40.0
EARTH_RADIUS_M = 6_371_008.8
TILE_DEG = 1.0
DEFAULT_BUDGET_MB = 512

# What each step names its working arrays, in the work directory.
WAY_COLUMNS = {
    "way_len": np.int32, "way_class": np.uint8, "way_kmh": np.float32, "way_oneway": np.int8,
    "way_unpaved": np.bool_, "way_category": np.uint8, "way_restricted": np.bool_, "way_name": np.int32,
    "way_id": np.int64,
}


def haversine(lat1, lon1, lat2, lon2) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.minimum(a, 1.0)))


class WayWriter:
    """Step A's output: the drivable ways, appended a way at a time (gathered
    in small Python arrays, written out every FLUSH_POSITIONS positions)."""

    FLUSH_POSITIONS = 1 << 20
    _CODES = {"way_len": "i", "way_class": "B", "way_kmh": "f", "way_oneway": "b", "way_unpaved": "B", "way_category": "B",
              "way_restricted": "B", "way_name": "i", "way_id": "q"}

    def __init__(self, work: ArrayDir) -> None:
        self.work = work
        self.refs = work.appender("pos_ref", np.int64)
        self.lat = work.appender("pos_lat", np.float64)
        self.lon = work.appender("pos_lon", np.float64)
        self.cols = {name: work.appender(name, dtype) for name, dtype in WAY_COLUMNS.items()}
        self.signals = work.appender("signal_ids", np.int64)
        self.stops = work.appender("stop_ids", np.int64)
        self.names: list[str] = [""]
        self.name_index: dict[str, int] = {"": 0}
        self.ways = 0
        self._reset()

    def _reset(self) -> None:
        from array import array

        self._refs, self._lat, self._lon = array("q"), array("d"), array("d")
        self._cols = {name: array(code) for name, code in self._CODES.items()}
        self._signals, self._stops = array("q"), array("q")

    def _flush(self) -> None:
        self.refs.add(np.frombuffer(self._refs, dtype=np.int64))
        self.lat.add(np.frombuffer(self._lat, dtype=np.float64))
        self.lon.add(np.frombuffer(self._lon, dtype=np.float64))
        for name, values in self._cols.items():
            self.cols[name].add(np.frombuffer(values, dtype=np.dtype(values.typecode)).astype(WAY_COLUMNS[name]))
        self.signals.add(np.frombuffer(self._signals, dtype=np.int64))
        self.stops.add(np.frombuffer(self._stops, dtype=np.int64))
        self._reset()

    def name_id(self, label: str) -> int:
        found = self.name_index.get(label)
        if found is None:
            found = self.name_index[label] = len(self.names)
            self.names.append(label)
        return found

    def control(self, node_id: int, kind: str) -> None:
        (self._signals if kind == "signal" else self._stops).append(node_id)

    def add(self, refs, lat, lon, road_class: int, kmh: float, oneway: int, unpaved: bool, category: int,
            restricted: bool, name: int, way_id: int) -> None:
        self._refs.extend(refs)
        self._lat.extend(lat)
        self._lon.extend(lon)
        c = self._cols
        c["way_len"].append(len(refs))
        c["way_class"].append(road_class)
        c["way_kmh"].append(kmh)
        c["way_oneway"].append(oneway)
        c["way_unpaved"].append(1 if unpaved else 0)
        c["way_category"].append(category)
        c["way_restricted"].append(1 if restricted else 0)
        c["way_name"].append(name)
        c["way_id"].append(way_id)
        self.ways += 1
        if len(self._refs) >= self.FLUSH_POSITIONS:
            self._flush()

    def add_ways(self, ways) -> None:
        """A whole osm_parse.Ways at once (tests, small maps)."""
        self._flush()
        remap = np.array([self.name_id(label) for label in ways.names], dtype=np.int32)
        self.refs.add(ways.refs)
        self.lat.add(ways.lat)
        self.lon.add(ways.lon)
        c = self.cols
        c["way_len"].add(np.diff(ways.starts))
        c["way_class"].add(ways.road_class)
        c["way_kmh"].add(ways.kmh)
        c["way_oneway"].add(ways.oneway)
        c["way_unpaved"].add(ways.unpaved)
        c["way_category"].add(ways.category)
        c["way_restricted"].add(ways.restricted)
        c["way_name"].add(remap[ways.name_id])
        c["way_id"].add(ways.way_ids)
        self.signals.add(ways.signals)
        self.stops.add(ways.stops)
        self.ways += len(ways.road_class)

    def close(self) -> list[str]:
        self._flush()
        for appender in (self.refs, self.lat, self.lon, self.signals, self.stops, *self.cols.values()):
            appender.close()
        return self.names


@dataclass
class Budget:
    bytes: int

    def rows(self, bytes_per_row: int) -> int:
        return rows_for(self.bytes, bytes_per_row)

    def buckets(self, rows: int, bytes_per_row: int) -> int:
        """How many buckets so that one bucket, sorted (about 3x its size),
        fits the budget."""
        return max(1, math.ceil(rows * bytes_per_row * 3 / self.bytes))


def _starts(work: ArrayDir) -> np.ndarray:
    """starts[w] = first position of way w, with a final end; on disk."""
    lengths = work.open("way_len")
    starts = work.create("way_start", np.int64, len(lengths) + 1)
    total = 0
    starts[0] = 0
    for lo, hi in chunks(len(lengths), 1 << 22):
        part = np.cumsum(lengths[lo:hi], dtype=np.int64) + total
        starts[lo + 1:hi + 1] = part
        total = int(part[-1]) if len(part) else total
    starts.flush()
    return starts


def _dedupe(work: ArrayDir, budget: Budget, say) -> None:
    """Step A2: a way in two extracts is kept the first time only."""
    way_ids = work.open("way_id")
    count = len(way_ids)
    parts = Partitions(work.folder / "dedupe", budget.buckets(count, 16), {"id": np.int64, "way": np.int64})
    for lo, hi in chunks(count, budget.rows(48)):
        ids = np.asarray(way_ids[lo:hi])
        parts.add(ids % parts.buckets, id=ids, way=np.arange(lo, hi, dtype=np.int64))
    parts.close()
    drop = work.create("way_drop", np.bool_, count, fill=False)
    dropped = 0
    for b in range(parts.buckets):
        rows = parts.read(b)
        if len(rows):
            order = np.lexsort((rows["way"], rows["id"]))
            ids, ways = rows["id"][order], rows["way"][order]
            repeat = np.zeros(len(ids), dtype=bool)
            repeat[1:] = ids[1:] == ids[:-1]
            if repeat.any():
                drop[np.sort(ways[repeat])] = True
                dropped += int(repeat.sum())
        parts.drop(b)
    parts.cleanup()
    drop.flush()
    if not dropped:
        work.remove("way_drop")
        return
    say(f"  {dropped:,} ways on a border kept once")
    # Rewrite the way and position arrays without the dropped ways.
    starts = _starts(work)
    keep_cols = {name: work.appender("new_" + name, dtype) for name, dtype in WAY_COLUMNS.items()}
    keep_pos = {name: work.appender("new_" + name, dtype) for name, dtype in (("pos_ref", np.int64), ("pos_lat", np.float64), ("pos_lon", np.float64))}
    cols = {name: work.open(name) for name in WAY_COLUMNS}
    pos = {name: work.open(name) for name in keep_pos}
    step = budget.rows(64)
    for lo, hi in chunks(count, max(1, step // 16)):
        keep = ~np.asarray(drop[lo:hi])
        for name, out in keep_cols.items():
            out.add(np.asarray(cols[name][lo:hi])[keep])
        p0, p1 = int(starts[lo]), int(starts[hi])
        lengths = np.diff(np.asarray(starts[lo:hi + 1]))
        at = np.repeat(keep, lengths)
        for name, out in keep_pos.items():
            out.add(np.asarray(pos[name][p0:p1])[at])
    del cols, pos, starts, drop
    for out in (*keep_cols.values(), *keep_pos.values()):
        out.close()
    for name in (*WAY_COLUMNS, "pos_ref", "pos_lat", "pos_lon"):
        work.remove(name)
        work.rename("new_" + name, name)
    work.remove("way_drop")
    work.remove("way_start")


def _junctions(work: ArrayDir, budget: Budget, say) -> int:
    """Step B. Writes pos_node (node id, -1 between intersections) and
    pos_flags (1 signal, 2 stop sign), node_lat/node_lon. Returns the node count."""
    refs = work.open("pos_ref")
    lat, lon = work.open("pos_lat"), work.open("pos_lon")
    starts = _starts(work)
    positions = len(refs)
    end = work.create("pos_end", np.bool_, positions, fill=False)
    first, last = np.asarray(starts[:-1]), np.asarray(starts[1:]) - 1
    valid = last >= first
    end[first[valid]] = True
    end[last[valid]] = True
    end.flush()
    del first, last

    parts = Partitions(work.folder / "junctions", budget.buckets(positions, 24), {"ref": np.int64, "pos": np.int64, "end": np.bool_})
    for lo, hi in chunks(positions, budget.rows(64)):
        r = np.asarray(refs[lo:hi])
        parts.add(r % parts.buckets, ref=r, pos=np.arange(lo, hi, dtype=np.int64), end=np.asarray(end[lo:hi]))
    parts.close()
    controls = Partitions(work.folder / "controls", parts.buckets, {"ref": np.int64, "kind": np.uint8})
    for name, kind in (("signal_ids", 1), ("stop_ids", 2)):
        ids = work.open(name)
        for lo, hi in chunks(len(ids), budget.rows(32)):
            part = np.asarray(ids[lo:hi])
            controls.add(part % controls.buckets, ref=part, kind=np.full(len(part), kind, dtype=np.uint8))
    controls.close()

    pos_node = work.create("pos_node", np.int64, positions, fill=-1)
    pos_flags = work.create("pos_flags", np.uint8, positions, fill=0)
    node_lat = work.appender("node_lat", np.float64)
    node_lon = work.appender("node_lon", np.float64)
    nodes = 0
    for b in range(parts.buckets):
        rows = parts.read(b)
        ctl = controls.read(b)
        parts.drop(b)
        controls.drop(b)
        if not len(rows):
            continue
        unique, inverse, uses = np.unique(rows["ref"], return_inverse=True, return_counts=True)
        is_end = np.bincount(inverse, weights=rows["end"].astype(np.float64), minlength=len(unique)) > 0
        is_node = (uses >= 2) | is_end
        ids = np.full(len(unique), -1, dtype=np.int64)
        count = int(is_node.sum())
        ids[is_node] = np.arange(nodes, nodes + count)
        on = is_node[inverse]
        order = np.argsort(rows["pos"][on])
        node_pos = rows["pos"][on][order]
        pos_node[node_pos] = ids[inverse[on]][order]
        # A node's position: the first place a way uses it.
        firsts = np.full(len(unique), np.iinfo(np.int64).max, dtype=np.int64)
        np.minimum.at(firsts, inverse, rows["pos"])
        at = firsts[is_node]          # in node id order
        order = np.argsort(at)
        coords_lat = np.empty(len(at))
        coords_lon = np.empty(len(at))
        coords_lat[order] = lat[at[order]]
        coords_lon[order] = lon[at[order]]
        node_lat.add(coords_lat)
        node_lon.add(coords_lon)
        nodes += count
        if len(ctl):
            for kind in (1, 2):
                wanted = ctl["ref"][ctl["kind"] == kind]
                hit = np.isin(rows["ref"], wanted)
                if hit.any():
                    p = np.sort(rows["pos"][hit])
                    pos_flags[p] = pos_flags[p] | kind
    parts.cleanup()
    controls.cleanup()
    pos_node.flush()
    pos_flags.flush()
    node_lat.close()
    node_lon.close()
    work.remove("pos_end")
    say(f"  {nodes:,} intersections among {positions:,} node references")
    return nodes


def _segments(work: ArrayDir, nodes: int, budget: Budget, say) -> int:
    """Step C: each way cut at its intersections."""
    starts = work.open("way_start")
    lat, lon = work.open("pos_lat"), work.open("pos_lon")
    pos_node, pos_flags = work.open("pos_node"), work.open("pos_flags")
    cols = {name: work.open(name) for name in WAY_COLUMNS}
    node_signal = work.create("node_signal", np.bool_, nodes, fill=False)
    node_stop = work.create("node_stop", np.bool_, nodes, fill=False)
    out = {
        "seg_a": work.appender("seg_a", np.int64), "seg_b": work.appender("seg_b", np.int64),
        "seg_g0": work.appender("seg_g0", np.int64), "seg_g1": work.appender("seg_g1", np.int64),
        "seg_len": work.appender("seg_len", np.float32), "seg_kmh": work.appender("seg_kmh", np.float32),
        "seg_class": work.appender("seg_class", np.uint8), "seg_oneway": work.appender("seg_oneway", np.int8),
        "seg_unpaved": work.appender("seg_unpaved", np.bool_), "seg_category": work.appender("seg_category", np.uint8),
        "seg_restricted": work.appender("seg_restricted", np.bool_), "seg_name": work.appender("seg_name", np.int32),
        "seg_stop_a": work.appender("seg_stop_a", np.bool_), "seg_stop_b": work.appender("seg_stop_b", np.bool_),
    }
    way_count = len(starts) - 1
    per_chunk = budget.rows(120)
    w0 = 0
    made = 0
    while w0 < way_count:
        # As many whole ways as fit.
        p0 = int(starts[w0])
        w1 = int(np.searchsorted(starts, p0 + per_chunk, side="right")) - 1
        w1 = min(way_count, max(w0 + 1, w1))
        p1 = int(starts[w1])
        s = np.asarray(starts[w0:w1 + 1]) - p0
        la, lo = np.asarray(lat[p0:p1]), np.asarray(lon[p0:p1])
        node = np.asarray(pos_node[p0:p1])
        flags = np.asarray(pos_flags[p0:p1])
        n = p1 - p0
        way_of = np.repeat(np.arange(w1 - w0), np.diff(s))
        last = s[1:] - 1
        cut = node >= 0
        step = np.zeros(n)
        if n > 1:
            step[:-1] = haversine(la[:-1], lo[:-1], la[1:], lo[1:])
        step[last[last >= 0]] = 0.0
        along = np.concatenate(([0.0], np.cumsum(step)))
        cuts = np.flatnonzero(cut)
        g0, g1 = cuts[:-1], cuts[1:]
        same = way_of[g0] == way_of[g1]
        g0, g1 = g0[same], g1[same]
        a, b = node[g0], node[g1]
        keep = a != b
        g0, g1, a, b = g0[keep], g1[keep], a[keep], b[keep]
        seg_way = way_of[g0]
        stop_a = np.zeros(len(g0), dtype=bool)
        stop_b = np.zeros(len(g0), dtype=bool)
        for kind, target, ends in ((1, node_signal, None), (2, node_stop, (stop_a, stop_b))):
            at = np.flatnonzero(flags & kind)
            if not len(at):
                continue
            on_node = at[cut[at]]
            if len(on_node):
                target[np.sort(node[on_node])] = True
            between = at[~cut[at]]
            if not len(between) or not len(g0):
                continue
            k = np.searchsorted(g0, between, side="right") - 1
            okay = (k >= 0) & (g0[np.maximum(k, 0)] < between) & (between < g1[np.maximum(k, 0)])
            k, between = k[okay], between[okay]
            to_a = along[between] - along[g0[k]]
            to_b = along[g1[k]] - along[between]
            near_a = (to_a <= to_b) & (to_a <= CONTROL_NEAR_M)
            near_b = (to_b < to_a) & (to_b <= CONTROL_NEAR_M)
            if ends is None:
                hit = np.concatenate((a[k[near_a]], b[k[near_b]]))
                if len(hit):
                    target[np.sort(hit)] = True
            else:
                ends[0][k[near_a]] = True
                ends[1][k[near_b]] = True
        ways = {name: np.asarray(cols[name][w0:w1]) for name in WAY_COLUMNS}
        out["seg_a"].add(a)
        out["seg_b"].add(b)
        out["seg_g0"].add(g0 + p0)
        out["seg_g1"].add(g1 + p0)
        out["seg_len"].add(along[g1] - along[g0])
        out["seg_kmh"].add(ways["way_kmh"][seg_way])
        out["seg_class"].add(ways["way_class"][seg_way])
        out["seg_oneway"].add(ways["way_oneway"][seg_way])
        out["seg_unpaved"].add(ways["way_unpaved"][seg_way])
        out["seg_category"].add(ways["way_category"][seg_way])
        out["seg_restricted"].add(ways["way_restricted"][seg_way])
        out["seg_name"].add(ways["way_name"][seg_way])
        out["seg_stop_a"].add(stop_a)
        out["seg_stop_b"].add(stop_b)
        made += len(g0)
        w0 = w1
    for appender in out.values():
        appender.close()
    node_signal.flush()
    node_stop.flush()
    say(f"  {made:,} segments between intersections")
    return made


def order_by(keys: np.ndarray, work: ArrayDir, name: str, budget: Budget) -> np.ndarray:
    """The permutation that sorts keys (ascending, ties by index), written to
    work as name: rows are scattered into buckets by key range (bounds from a
    sample), each bucket sorted in memory."""
    count = len(keys)
    out = work.create(name, np.int64, count)
    if count == 0:
        return out
    buckets = budget.buckets(count, 16)
    if buckets == 1:
        out[:] = np.argsort(np.asarray(keys), kind="stable")
        out.flush()
        return out
    sample = np.asarray(keys[:: max(1, count // 200_000)])
    bounds = np.unique(np.quantile(sample, np.linspace(0, 1, buckets + 1)[1:-1], method="nearest"))
    parts = Partitions(work.folder / f"order-{name}", len(bounds) + 1, {"key": keys.dtype, "idx": np.int64})
    for lo, hi in chunks(count, budget.rows(48)):
        k = np.asarray(keys[lo:hi])
        parts.add(np.searchsorted(bounds, k, side="right"), key=k, idx=np.arange(lo, hi, dtype=np.int64))
    parts.close()
    at = 0
    for b in range(parts.buckets):
        rows = parts.read(b)
        parts.drop(b)
        if not len(rows):
            continue
        order = np.lexsort((rows["idx"], rows["key"]))
        out[at:at + len(order)] = rows["idx"][order]
        at += len(order)
    parts.cleanup()
    out.flush()
    return out


def invert(perm: np.ndarray, work: ArrayDir, name: str, budget: Budget) -> np.ndarray:
    """new_of_old from old_of_new."""
    out = work.create(name, np.int64, len(perm))
    for lo, hi in chunks(len(perm), budget.rows(32)):
        p = np.asarray(perm[lo:hi])
        order = np.argsort(p)
        out[p[order]] = np.arange(lo, hi, dtype=np.int64)[order]
    out.flush()
    return out


def _tile_of(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    rows = np.floor(np.asarray(lat) + 90.0).astype(np.int64)
    cols = np.floor(np.asarray(lon) + 180.0).astype(np.int64) % 360
    return rows * 360 + cols


def _node_key(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    per = int(round(TILE_DEG / grid.CELL_DEG))
    tile = _tile_of(lat, lon)
    r = np.clip(np.floor((np.asarray(lat) + 90.0) / grid.CELL_DEG).astype(np.int64) % per, 0, per - 1)
    c = np.clip(np.floor((np.asarray(lon) + 180.0) / grid.CELL_DEG).astype(np.int64) % per, 0, per - 1)
    return tile * per * per + r * per + c


def _gather(source: np.ndarray, index: np.ndarray, budget: Budget) -> np.ndarray:
    """source[index] for a disk array and a large index, reading in index order."""
    index = np.asarray(index)
    if len(index) <= budget.rows(64):
        order = np.argsort(index)
        out = np.empty(len(index), dtype=source.dtype)
        out[order] = source[index[order]]
        return out
    return source[index]


def _order_and_islands(work: ArrayDir, nodes: int, segments: int, budget: Budget, say) -> tuple[int, int]:
    """Steps D and E. Writes node_new (final node id or -1) and seg_order
    (old segment index of each kept segment, in final order)."""
    node_lat, node_lon = work.open("node_lat"), work.open("node_lon")
    keys = work.create("node_key", np.int64, nodes)
    for lo, hi in chunks(nodes, budget.rows(48)):
        keys[lo:hi] = _node_key(node_lat[lo:hi], node_lon[lo:hi])
    keys.flush()
    old_of_new = order_by(keys, work, "node_order", budget)
    new_of_old = invert(old_of_new, work, "node_rank", budget)

    # Segments by their first node's rank.
    seg_a, seg_b = work.open("seg_a"), work.open("seg_b")
    seg_rank_a = work.create("seg_rank_a", np.int64, segments)
    for lo, hi in chunks(segments, budget.rows(48)):
        seg_rank_a[lo:hi] = _gather(new_of_old, np.asarray(seg_a[lo:hi]), budget)
    seg_rank_a.flush()
    seg_sorted = order_by(seg_rank_a, work, "seg_sorted", budget)
    sorted_a = work.create("seg_sorted_a", np.int64, segments)
    sorted_b = work.create("seg_sorted_b", np.int64, segments)
    oneway_sorted = work.create("seg_sorted_oneway", np.int8, segments)
    oneway = work.open("seg_oneway")
    for lo, hi in chunks(segments, budget.rows(64)):
        idx = np.asarray(seg_sorted[lo:hi])
        sorted_a[lo:hi] = _gather(seg_rank_a, idx, budget)
        sorted_b[lo:hi] = _gather(new_of_old, _gather(seg_b, idx, budget), budget)
        oneway_sorted[lo:hi] = _gather(oneway, idx, budget)
    for array in (sorted_a, sorted_b, oneway_sorted):
        array.flush()

    # Tiles: contiguous ranks of nodes.
    sorted_keys_tile = work.create("node_tile", np.int64, nodes)
    per = int(round(TILE_DEG / grid.CELL_DEG)) ** 2
    for lo, hi in chunks(nodes, budget.rows(32)):
        sorted_keys_tile[lo:hi] = _gather(keys, np.asarray(old_of_new[lo:hi]), budget) // per
    sorted_keys_tile.flush()
    present = []
    for lo, hi in chunks(nodes, 1 << 23):
        present.append(np.unique(np.asarray(sorted_keys_tile[lo:hi])))
    tiles = np.unique(np.concatenate(present))
    tile_start = np.searchsorted(sorted_keys_tile, tiles, side="left")
    tile_stop = np.searchsorted(sorted_keys_tile, tiles, side="right")
    keep = work.create("node_keep", np.bool_, nodes, fill=False)
    for t, tile in enumerate(tiles):
        row, col = divmod(int(tile), 360)
        around = [(row + dr) * 360 + (col + dc) % 360 for dr in (-1, 0, 1) for dc in (-1, 0, 1)]
        found = np.searchsorted(tiles, around)
        spans = [(int(tile_start[f]), int(tile_stop[f])) for f, want in zip(found, around) if f < len(tiles) and tiles[f] == want]
        spans.sort()
        window = np.concatenate([np.arange(lo, hi, dtype=np.int64) for lo, hi in spans])
        seg_idx = np.concatenate([np.arange(np.searchsorted(sorted_a, lo, side="left"), np.searchsorted(sorted_a, hi, side="left"), dtype=np.int64)
                                  for lo, hi in spans])
        a = np.asarray(sorted_a[seg_idx]) if len(seg_idx) else np.zeros(0, dtype=np.int64)
        b = np.asarray(sorted_b[seg_idx]) if len(seg_idx) else np.zeros(0, dtype=np.int64)
        ow = np.asarray(oneway_sorted[seg_idx]) if len(seg_idx) else np.zeros(0, dtype=np.int8)
        local_a = np.searchsorted(window, a)
        local_b = np.searchsorted(window, b)
        inside = (local_b < len(window)) & (window[np.minimum(local_b, len(window) - 1)] == b)
        local_a, local_b, ow = local_a[inside], local_b[inside], ow[inside]
        forward, backward = ow >= 0, ow <= 0
        src = np.concatenate((local_a[forward], local_b[backward]))
        dst = np.concatenate((local_b[forward], local_a[backward]))
        size = len(window)
        matrix = csr_matrix((np.ones(len(src), dtype=np.int8), (src, dst)), shape=(size, size))
        _, labels = connected_components(matrix, directed=True, connection="strong")
        biggest = np.bincount(labels).argmax()
        core_lo, core_hi = int(tile_start[t]), int(tile_stop[t])
        core = np.searchsorted(window, np.arange(core_lo, core_hi))
        keep[core_lo:core_hi] = labels[core] == biggest
    keep.flush()
    kept_nodes = int(np.count_nonzero(np.asarray(keep)))
    node_new = work.create("node_new", np.int64, nodes)
    at = 0
    for lo, hi in chunks(nodes, budget.rows(16)):
        k = np.asarray(keep[lo:hi])
        ids = np.full(hi - lo, -1, dtype=np.int64)
        ids[k] = np.arange(at, at + int(k.sum()))
        at += int(k.sum())
        node_new[lo:hi] = ids
    node_new.flush()
    seg_order = work.appender("seg_order", np.int64)
    for lo, hi in chunks(segments, budget.rows(32)):
        a = _gather(keep, np.asarray(sorted_a[lo:hi]), budget)
        b = _gather(keep, np.asarray(sorted_b[lo:hi]), budget)
        seg_order.add(np.asarray(seg_sorted[lo:hi])[a & b])
    kept_segments = seg_order.close()
    say(f"  largest connected networks: {kept_nodes:,} of {nodes:,} intersections, {kept_segments:,} segments")
    for name in ("node_key", "seg_rank_a", "seg_sorted", "seg_sorted_a", "seg_sorted_b", "seg_sorted_oneway", "node_tile", "node_keep"):
        work.remove(name)
    return kept_nodes, kept_segments


def _write_final(work: ArrayDir, out: ArrayDir, nodes: int, kept_nodes: int, segments: int, budget: Budget, say) -> dict:
    """Step F."""
    order = work.open("seg_order")
    node_order, node_rank, node_new = work.open("node_order"), work.open("node_rank"), work.open("node_new")
    # Nodes in final order: ranks whose node_new >= 0, in rank order.
    final_nodes = work.appender("final_node_old", np.int64)
    for lo, hi in chunks(nodes, budget.rows(16)):
        ids = np.asarray(node_new[lo:hi])
        final_nodes.add(np.asarray(node_order[lo:hi])[ids >= 0])
    final_nodes.close()
    old_nodes = work.open("final_node_old")
    for name, dtype in (("node_lat", np.float64), ("node_lon", np.float64), ("node_signal", np.bool_), ("node_stop", np.bool_)):
        source = work.open(name)
        target = out.create(name, dtype, kept_nodes)
        for lo, hi in chunks(kept_nodes, budget.rows(32)):
            target[lo:hi] = _gather(source, np.asarray(old_nodes[lo:hi]), budget)
        target.flush()

    simple = ("seg_len", "seg_kmh", "seg_class", "seg_oneway", "seg_unpaved", "seg_category", "seg_restricted", "seg_name",
              "seg_stop_a", "seg_stop_b")
    for name in simple:
        source = work.open(name)
        target = out.create(name, source.dtype, segments)
        for lo, hi in chunks(segments, budget.rows(32)):
            target[lo:hi] = _gather(source, np.asarray(order[lo:hi]), budget)
        target.flush()
    for end in ("a", "b"):
        source = work.open(f"seg_{end}")
        target = out.create(f"seg_{end}", np.int32, segments)
        for lo, hi in chunks(segments, budget.rows(48)):
            old = _gather(source, np.asarray(order[lo:hi]), budget)
            target[lo:hi] = _gather(node_new, _gather(node_rank, old, budget), budget)
        target.flush()
    seconds = out.create("seg_seconds", np.float32, segments)
    lengths, kmh = out.open("seg_len"), out.open("seg_kmh")
    for lo, hi in chunks(segments, budget.rows(16)):
        seconds[lo:hi] = np.asarray(lengths[lo:hi], dtype=np.float64) / np.maximum(np.asarray(kmh[lo:hi], dtype=np.float64) / 3.6, 0.1)
    seconds.flush()

    # Geometry: each segment's shape points, in the segments' new order.
    g0s, g1s = work.open("seg_g0"), work.open("seg_g1")
    pos_lat, pos_lon = work.open("pos_lat"), work.open("pos_lon")
    new_g0, new_g1 = out.create("seg_g0", np.int64, segments), out.create("seg_g1", np.int64, segments)
    geom_lat, geom_lon = out.appender("geom_lat", np.float64), out.appender("geom_lon", np.float64)
    at = 0
    for lo, hi in chunks(segments, max(1, budget.rows(64) // 8)):
        idx = np.asarray(order[lo:hi])
        a0 = _gather(g0s, idx, budget)
        a1 = _gather(g1s, idx, budget)
        span = a1 - a0 + 1
        new_g0[lo:hi] = at + np.cumsum(span) - span
        new_g1[lo:hi] = at + np.cumsum(span) - 1
        at += int(span.sum())
        where = ranges(a0, a1 + 1)
        geom_lat.add(_gather(pos_lat, where, budget))
        geom_lon.add(_gather(pos_lon, where, budget))
    new_g0.flush()
    new_g1.flush()
    geom_lat.close()
    geom_lon.close()
    say(f"  {at:,} shape points")
    return {"geometry_points": at}


def _edges(out: ArrayDir, work: ArrayDir, nodes: int, segments: int, budget: Budget) -> int:
    """Step G: directed edges sorted by (src, dst); seg_fwd / seg_rev."""
    seg_a, seg_b, oneway = out.open("seg_a"), out.open("seg_b"), out.open("seg_oneway")
    keys = work.appender("edge_key", np.int64)
    raw = {"seg": work.appender("edge_raw_seg", np.int64), "rev": work.appender("edge_raw_rev", np.bool_)}
    for lo, hi in chunks(segments, budget.rows(64)):
        a, b, ow = np.asarray(seg_a[lo:hi], dtype=np.int64), np.asarray(seg_b[lo:hi], dtype=np.int64), np.asarray(oneway[lo:hi])
        s = np.arange(lo, hi, dtype=np.int64)
        f, r = ow >= 0, ow <= 0
        keys.add(np.concatenate((a[f] * nodes + b[f], b[r] * nodes + a[r])))
        raw["seg"].add(np.concatenate((s[f], s[r])))
        raw["rev"].add(np.concatenate((np.zeros(int(f.sum()), dtype=bool), np.ones(int(r.sum()), dtype=bool))))
    edges = keys.close()
    for appender in raw.values():
        appender.close()
    perm = order_by(work.open("edge_key"), work, "edge_perm", budget)
    raw_seg, raw_rev, raw_key = work.open("edge_raw_seg"), work.open("edge_raw_rev"), work.open("edge_key")
    e_seg = out.create("edge_seg", np.int32, edges)
    e_rev = out.create("edge_rev", np.bool_, edges)
    e_src = out.create("edge_src", np.int32, edges)
    e_dst = out.create("edge_dst", np.int32, edges)
    fwd = out.create("seg_fwd", np.int32, segments, fill=-1)
    rev = out.create("seg_rev", np.int32, segments, fill=-1)
    for lo, hi in chunks(edges, budget.rows(64)):
        p = np.asarray(perm[lo:hi])
        k = _gather(raw_key, p, budget)
        s = _gather(raw_seg, p, budget)
        r = _gather(raw_rev, p, budget)
        e_seg[lo:hi] = s
        e_rev[lo:hi] = r
        e_src[lo:hi] = k // nodes
        e_dst[lo:hi] = k % nodes
        ids = np.arange(lo, hi, dtype=np.int32)
        order = np.argsort(s)
        fwd_rows, rev_rows = order[~r[order]], order[r[order]]
        fwd[s[fwd_rows]] = ids[fwd_rows]
        rev[s[rev_rows]] = ids[rev_rows]
    for array in (e_seg, e_rev, e_src, e_dst, fwd, rev):
        array.flush()
    # Where each node's edges start.
    ptr = out.create("node_edge_ptr", np.int64, nodes + 1)
    ptr[0] = 0
    for lo, hi in chunks(nodes, budget.rows(32)):
        ptr[lo + 1:hi + 1] = np.searchsorted(e_src, np.arange(lo + 1, hi + 1), side="left")
    ptr.flush()
    for name in ("edge_key", "edge_perm", "edge_raw_seg", "edge_raw_rev"):
        work.remove(name)
    return edges


def _turns(out: ArrayDir, work: ArrayDir, nodes: int, segments: int, edges: int, budget: Budget, say) -> int:
    """Step H."""
    seg_a, seg_b, seg_class = out.open("seg_a"), out.open("seg_b"), out.open("seg_class")
    g0s, g1s = out.open("seg_g0"), out.open("seg_g1")
    geom_lat, geom_lon = out.open("geom_lat"), out.open("geom_lon")
    e_seg, e_rev = out.open("edge_seg"), out.open("edge_rev")
    # Per node: how many roads meet, and the biggest.
    degree = work.create("node_degree", np.int32, nodes, fill=0)
    biggest = work.create("node_biggest", np.int16, nodes, fill=0)
    for lo, hi in chunks(segments, budget.rows(32)):
        a, b = np.asarray(seg_a[lo:hi]), np.asarray(seg_b[lo:hi])
        rank = classes.RANKS[np.asarray(seg_class[lo:hi])].astype(np.int16)
        for end in (a, b):
            order = np.argsort(end)
            end_sorted, rank_sorted = end[order], rank[order]
            uniq, first = np.unique(end_sorted, return_index=True)
            counts = np.diff(np.append(first, len(end_sorted)))
            degree[uniq] = np.asarray(degree[uniq]) + counts
            top = np.maximum.reduceat(rank_sorted, first)
            biggest[uniq] = np.maximum(np.asarray(biggest[uniq]), top)
    degree.flush()
    biggest.flush()
    # Per edge: which way it leaves its start and arrives at its end.
    leaves = work.create("edge_leaves", np.float32, edges)
    arrives = work.create("edge_arrives", np.float32, edges)
    for lo, hi in chunks(edges, budget.rows(96)):
        s = np.asarray(e_seg[lo:hi])
        g0, g1 = _gather(g0s, s, budget), _gather(g1s, s, budget)
        lat0, lat1 = _gather(geom_lat, g0, budget), _gather(geom_lat, g0 + 1, budget)
        lon0, lon1 = _gather(geom_lon, g0, budget), _gather(geom_lon, g0 + 1, budget)
        latm, latl = _gather(geom_lat, g1 - 1, budget), _gather(geom_lat, g1, budget)
        lonm, lonl = _gather(geom_lon, g1 - 1, budget), _gather(geom_lon, g1, budget)
        first = turns.bearing_deg(lat0, lon0, lat1, lon1)
        last = turns.bearing_deg(latm, lonm, latl, lonl)
        r = np.asarray(e_rev[lo:hi])
        leaves[lo:hi] = np.where(r, (last + 180.0) % 360.0, first)
        arrives[lo:hi] = np.where(r, (first + 180.0) % 360.0, last)
    leaves.flush()
    arrives.flush()

    tables = turns.Tables(
        edge_seg=e_seg, edge_rev=e_rev, edge_dst=out.open("edge_dst"), node_edge_ptr=out.open("node_edge_ptr"),
        seg_class=seg_class, seg_stop_a=out.open("seg_stop_a"), seg_stop_b=out.open("seg_stop_b"),
        node_signal=out.open("node_signal"), node_stop=out.open("node_stop"),
        node_degree=degree, node_biggest=biggest, leaves=leaves, arrives=arrives,
    )
    ptr = out.create("turn_ptr", np.int64, edges + 1)
    ptr[0] = 0
    to = out.appender("turn_to", np.int32)
    secs = out.appender("turn_seconds", np.float32)
    total = 0
    for lo, hi in chunks(edges, budget.rows(200)):
        counts, move_to, seconds = turns.moves(tables, lo, hi)
        ptr[lo + 1:hi + 1] = total + np.cumsum(counts)
        total += int(counts.sum())
        to.add(move_to)
        secs.add(seconds)
    ptr.flush()
    to.close()
    secs.close()
    for name in ("node_degree", "node_biggest", "edge_leaves", "edge_arrives"):
        work.remove(name)
    say(f"  {total:,} turns priced")
    return total


def _grid(out: ArrayDir, work: ArrayDir, segments: int, budget: Budget) -> int:
    """Step I: (cell, segment) for every cell a segment's shape passes through."""
    g0s, g1s = out.open("seg_g0"), out.open("seg_g1")
    geom_lat, geom_lon = out.open("geom_lat"), out.open("geom_lon")
    pairs = work.appender("grid_pair", np.int64)
    per = budget.rows(160)
    for lo, hi in chunks(segments, max(1, per // 6)):
        g0, g1 = np.asarray(g0s[lo:hi]), np.asarray(g1s[lo:hi])
        p0, p1 = int(g0[0]), int(g1[-1]) + 1
        la, lo_ = np.asarray(geom_lat[p0:p1]), np.asarray(geom_lon[p0:p1])
        # Pieces: point i to i+1 inside one segment.
        seg_of = np.repeat(np.arange(lo, hi, dtype=np.int64), g1 - g0)
        start = ranges(g0, g1) - p0
        r0, c0 = grid.cell_rc(la[start], lo_[start])
        r1, c1 = grid.cell_rc(la[start + 1], lo_[start + 1])
        rl, rh = np.minimum(r0, r1), np.maximum(r0, r1)
        cl, ch = np.minimum(c0, c1), np.maximum(c0, c1)
        # Pieces crossing the antimeridian are tiny in reality; keep their ends.
        wrap = (ch - cl) > grid.COLS // 2
        cl, ch = np.where(wrap, c0, cl), np.where(wrap, c0, ch)
        nr, nc = rh - rl + 1, ch - cl + 1
        count = nr * nc
        idx = np.repeat(np.arange(len(count)), count)
        k = np.arange(int(count.sum())) - np.repeat(np.cumsum(count) - count, count)
        rr = rl[idx] + k // nc[idx]
        cc = cl[idx] + k % nc[idx]
        keys = grid.key_of(rr, cc) * grid.SEG_SPACE + seg_of[idx]
        pairs.add(np.unique(keys))
    total = pairs.close()
    perm = order_by(work.open("grid_pair"), work, "grid_perm", budget)
    raw = work.open("grid_pair")
    cell_segs = out.create("cell_segs", np.int32, total)
    keys_out = work.appender("grid_keys", np.int64)
    starts_out = work.appender("grid_starts", np.int64)
    last_key = -1
    for lo, hi in chunks(total, budget.rows(48)):
        v = _gather(raw, np.asarray(perm[lo:hi]), budget)
        cell = v // grid.SEG_SPACE
        cell_segs[lo:hi] = v % grid.SEG_SPACE
        new = np.ones(len(cell), dtype=bool)
        new[1:] = cell[1:] != cell[:-1]
        new[0] = cell[0] != last_key
        keys_out.add(cell[new])
        starts_out.add(np.flatnonzero(new) + lo)
        last_key = int(cell[-1])
    cell_segs.flush()
    cells = keys_out.close()
    starts_out.close()
    out.adopt("grid_keys", work, "cell_keys")
    ptr = out.create("cell_ptr", np.int64, cells + 1)
    ptr[:cells] = work.open("grid_starts")
    ptr[cells] = total
    ptr.flush()
    for name in ("grid_pair", "grid_perm", "grid_starts"):
        work.remove(name)
    return cells


def finish(work: ArrayDir, out_dir: Path, names: list[str], source: str, budget_mb: int, say, began: float) -> dict:
    """Steps B-I, from step A's arrays in work, into out_dir."""
    budget = Budget(max(64, int(budget_mb)) << 20)
    if "way_start" not in work:
        _dedupe(work, budget, say)
        nodes = _junctions(work, budget, say)
        segments = _segments(work, nodes, budget, say)
    else:
        nodes, segments = work.length("node_lat"), work.length("seg_a")
    if segments == 0:
        raise ValueError("no drivable roads in the extract")
    kept_nodes, kept_segments = _order_and_islands(work, nodes, segments, budget, say)
    temp = out_dir.with_name(out_dir.name + ".part")
    shutil.rmtree(temp, ignore_errors=True)
    out = ArrayDir(temp)
    extra = _write_final(work, out, nodes, kept_nodes, kept_segments, budget, say)
    edges = _edges(out, work, kept_nodes, kept_segments, budget)
    turn_count = _turns(out, work, kept_nodes, kept_segments, edges, budget, say)
    cells = _grid(out, work, kept_segments, budget)
    blob = "\n".join(names).encode("utf-8")
    out.write("names", np.frombuffer(blob, dtype=np.uint8) if blob else np.zeros(0, dtype=np.uint8))
    lat, lon = out.open("node_lat"), out.open("node_lon")
    lat_lo, lat_hi, lon_lo, lon_hi = 90.0, -90.0, 180.0, -180.0
    for lo, hi in chunks(kept_nodes, 1 << 22):
        la, lo_ = np.asarray(lat[lo:hi]), np.asarray(lon[lo:hi])
        lat_lo, lat_hi = min(lat_lo, float(la.min())), max(lat_hi, float(la.max()))
        lon_lo, lon_hi = min(lon_lo, float(lo_.min())), max(lon_hi, float(lo_.max()))
    signals = int(np.count_nonzero(np.asarray(out.open("node_signal"))))
    stops = sum(int(np.count_nonzero(np.asarray(out.open(n)))) for n in ("seg_stop_a", "seg_stop_b", "node_stop"))
    restricted = int(np.count_nonzero(np.asarray(out.open("seg_restricted"))))
    meta = {
        "version": FORMAT_VERSION,
        "source": source,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nodes": kept_nodes,
        "segments": kept_segments,
        "edges": edges,
        "turns": turn_count,
        "cells": cells,
        "cell_deg": grid.CELL_DEG,
        "geometry_points": extra["geometry_points"],
        "signals": signals,
        "stop_signs": stops,
        "bbox": [lat_lo, lon_lo, lat_hi, lon_hi],
        "classes": list(classes.CLASSES),
        "categories": list(classes.CATEGORIES),
        "restricted_segments": restricted,
        "names": len(names),
    }
    (temp / "meta.json").write_text(json.dumps(meta, indent=1))
    old = out_dir.with_name(out_dir.name + ".old")
    if out_dir.exists():
        shutil.rmtree(old, ignore_errors=True)
        os.replace(out_dir, old)
    os.replace(temp, out_dir)
    shutil.rmtree(old, ignore_errors=True)
    meta["seconds"] = round(time.monotonic() - began, 1)
    meta["bytes"] = sum(p.stat().st_size for p in out_dir.iterdir())
    meta["peak_mb"] = round(peak_rss_mb())
    return meta


def work_dir_for(out_dir: Path) -> Path:
    return out_dir.with_name(out_dir.name.removesuffix(".graph") + ".build")


def build_from_pbfs(pbfs: list[Path], out_dir: Path, budget_mb: int = DEFAULT_BUDGET_MB, progress=print) -> dict:
    from app.routing import osm_parse

    began = time.monotonic()
    folder = work_dir_for(out_dir)
    shutil.rmtree(folder, ignore_errors=True)
    work = ArrayDir(folder)
    try:
        writer = WayWriter(work)
        for pbf in pbfs:
            progress(f"Reading {pbf.name}")
            osm_parse.read_into(pbf, writer, folder / "locations.idx", progress=progress)
        names = writer.close()
        progress(f"Building the map ({budget_mb} MB at a time)")
        return finish(work, out_dir, names, ",".join(p.name for p in pbfs), budget_mb, progress, began)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def build_from_ways(ways, out_dir: Path, source: str, budget_mb: int = DEFAULT_BUDGET_MB, progress=print) -> dict:
    began = time.monotonic()
    folder = work_dir_for(out_dir)
    shutil.rmtree(folder, ignore_errors=True)
    work = ArrayDir(folder)
    try:
        writer = WayWriter(work)
        writer.add_ways(ways)
        names = writer.close()
        return finish(work, out_dir, names, source, budget_mb, progress, began)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def convert_npz(npz: Path, out_dir: Path, budget_mb: int = DEFAULT_BUDGET_MB, progress=print) -> dict:
    """A format-2/3 .npz map into this format, without the extract. It reads
    the old file into memory once (as the old server did)."""
    began = time.monotonic()
    folder = work_dir_for(out_dir)
    shutil.rmtree(folder, ignore_errors=True)
    work = ArrayDir(folder)
    try:
        with np.load(npz, allow_pickle=False) as data:
            meta = json.loads(data["meta"].tobytes().decode("utf-8"))
            names = data["names"].tobytes().decode("utf-8").split("\n")
            for name in ("node_lat", "node_lon", "node_signal", "node_stop", "seg_len", "seg_kmh", "seg_class", "seg_oneway",
                         "seg_unpaved", "seg_name", "seg_g0", "seg_g1", "seg_stop_a", "seg_stop_b"):
                work.write(name, data[name])
            work.write("seg_a", data["seg_a"].astype(np.int64))
            work.write("seg_b", data["seg_b"].astype(np.int64))
            work.write("pos_lat", data["geom_lat"])
            work.write("pos_lon", data["geom_lon"])
            if "seg_category" in data.files:
                work.write("seg_category", data["seg_category"])
                work.write("seg_restricted", data["seg_restricted"])
            else:
                from app.routing.graph import old_categories
                category, restricted = old_categories(data["seg_class"], data["seg_name"], names)
                work.write("seg_category", category)
                work.write("seg_restricted", restricted)
        work.write("way_start", np.zeros(1, dtype=np.int64))  # marks steps A-C as done
        return finish(work, out_dir, names, meta.get("source", npz.name), budget_mb, progress, began)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
