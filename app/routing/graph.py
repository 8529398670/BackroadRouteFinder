"""The road graph in memory: loaded once from the file build.py wrote, then
only read, by any number of requests at once.

Loading takes several seconds (the snapping index is the slow part), so the
server starts it in a background thread at startup and routes say "still
loading" until it is ready, rather than making the first person wait.

Directed edges: a two-way segment becomes two edges, a oneway one edge. The
search runs on the edges themselves, moving from one to the next through a
turn (turns.py), so that what a turn costs can depend on both roads.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from app.routing import build, classes, turns

METRES_PER_DEGREE = 111_320.0


@dataclass
class Graph:
    meta: dict
    names: list[str]
    node_lat: np.ndarray
    node_lon: np.ndarray
    # segments (undirected, as built)
    seg_a: np.ndarray
    seg_b: np.ndarray
    seg_len: np.ndarray
    seg_seconds: np.ndarray
    seg_class: np.ndarray
    seg_oneway: np.ndarray
    seg_unpaved: np.ndarray
    seg_name: np.ndarray
    seg_g0: np.ndarray
    seg_g1: np.ndarray
    geom_lat: np.ndarray
    geom_lon: np.ndarray
    # directed edges, sorted by src
    edge_seg: np.ndarray
    edge_rev: np.ndarray
    edge_src: np.ndarray
    edge_dst: np.ndarray
    # each segment's edge a->b and b->a, or -1 where its oneway forbids it
    seg_fwd: np.ndarray
    seg_rev: np.ndarray
    # the moves between edges, and what each costs
    turns: turns.Turns
    # snapping
    tree: cKDTree = field(repr=False)
    lat0_cos: float = 1.0

    @property
    def node_count(self) -> int:
        return len(self.node_lat)

    def project(self, lat: np.ndarray | float, lon: np.ndarray | float) -> np.ndarray:
        """Local metres, good enough across one state for nearest-road lookups."""
        return np.column_stack((np.asarray(lon, dtype=np.float64) * self.lat0_cos * METRES_PER_DEGREE,
                                np.asarray(lat, dtype=np.float64) * METRES_PER_DEGREE))

    def segment_of_point(self, position: int) -> int:
        """Which segment a geometry index belongs to."""
        return int(np.searchsorted(self.seg_g0, position, side="right") - 1)


def load(path: Path) -> Graph:
    with np.load(path, allow_pickle=False) as data:
        arrays = {name: data[name] for name in data.files}
    meta = json.loads(arrays["meta"].tobytes().decode("utf-8"))
    if meta.get("version") != build.FORMAT_VERSION:
        raise ValueError(f"graph file is format {meta.get('version')}, this server reads {build.FORMAT_VERSION}: rebuild it")
    if meta.get("classes") != list(classes.CLASSES):
        raise ValueError("graph file was built with other road classes: rebuild it")
    names = arrays["names"].tobytes().decode("utf-8").split("\n")

    seg_a, seg_b = arrays["seg_a"].astype(np.int64), arrays["seg_b"].astype(np.int64)
    oneway = arrays["seg_oneway"]
    segments = np.arange(len(seg_a), dtype=np.int64)
    forward, backward = oneway >= 0, oneway <= 0
    edge_seg = np.concatenate((segments[forward], segments[backward]))
    edge_rev = np.concatenate((np.zeros(int(forward.sum()), dtype=bool), np.ones(int(backward.sum()), dtype=bool)))
    edge_src = np.where(edge_rev, seg_b[edge_seg], seg_a[edge_seg])
    edge_dst = np.where(edge_rev, seg_a[edge_seg], seg_b[edge_seg])
    order = np.lexsort((edge_dst, edge_src))
    edge_seg, edge_rev, edge_src, edge_dst = edge_seg[order], edge_rev[order], edge_src[order], edge_dst[order]
    seg_fwd = np.full(len(seg_a), -1, dtype=np.int64)
    seg_rev = np.full(len(seg_a), -1, dtype=np.int64)
    ids = np.arange(len(edge_seg), dtype=np.int64)
    seg_fwd[edge_seg[~edge_rev]] = ids[~edge_rev]
    seg_rev[edge_seg[edge_rev]] = ids[edge_rev]

    seconds = arrays["seg_len"].astype(np.float64) / (arrays["seg_kmh"].astype(np.float64) / 3.6)
    lat_mid = (meta["bbox"][0] + meta["bbox"][2]) / 2
    lat0_cos = float(np.cos(np.radians(lat_mid)))
    geom_x = arrays["geom_lon"] * lat0_cos * METRES_PER_DEGREE
    geom_y = arrays["geom_lat"] * METRES_PER_DEGREE
    turn_table = turns.build(
        node_count=len(arrays["node_lat"]),
        seg_a=seg_a, seg_b=seg_b, seg_class=arrays["seg_class"],
        seg_g0=arrays["seg_g0"], seg_g1=arrays["seg_g1"],
        seg_stop_a=arrays["seg_stop_a"], seg_stop_b=arrays["seg_stop_b"],
        node_signal=arrays["node_signal"], node_stop=arrays["node_stop"],
        geom_x=geom_x, geom_y=geom_y,
        edge_seg=edge_seg, edge_rev=edge_rev, edge_src=edge_src, edge_dst=edge_dst,
    )
    del geom_x, geom_y
    graph = Graph(
        meta=meta,
        names=names,
        node_lat=arrays["node_lat"],
        node_lon=arrays["node_lon"],
        seg_a=seg_a,
        seg_b=seg_b,
        seg_len=arrays["seg_len"].astype(np.float64),
        seg_seconds=seconds,
        seg_class=arrays["seg_class"],
        seg_oneway=oneway,
        seg_unpaved=arrays["seg_unpaved"],
        seg_name=arrays["seg_name"],
        seg_g0=arrays["seg_g0"],
        seg_g1=arrays["seg_g1"],
        geom_lat=arrays["geom_lat"],
        geom_lon=arrays["geom_lon"],
        edge_seg=edge_seg,
        edge_rev=edge_rev,
        edge_src=edge_src,
        edge_dst=edge_dst,
        seg_fwd=seg_fwd,
        seg_rev=seg_rev,
        turns=turn_table,
        tree=None,  # type: ignore[arg-type]
        lat0_cos=lat0_cos,
    )
    graph.tree = cKDTree(graph.project(graph.geom_lat, graph.geom_lon), balanced_tree=False, compact_nodes=False)
    return graph


class Holder:
    """The one graph the server routes on, and where its loading stands:
    missing (no file yet), loading, ready, or error."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.graph: Graph | None = None
        self.state = "missing"
        self.error = ""
        self.load_seconds = 0.0
        self.switching = False
        # The file the graph in memory came from (path is the one asked for,
        # which differs while a switch is loading).
        self.loaded_path: Path | None = None
        self._lock = threading.Lock()

    @property
    def region(self) -> str:
        """The name of the map answering now."""
        return (self.loaded_path or self.path).stem

    def start(self) -> None:
        """Load in a background thread, if there is a file to load."""
        if not self.path.is_file():
            self.state = "missing"
            return
        with self._lock:
            if self.state == "loading":
                return
            self.state = "loading"
        threading.Thread(target=self._load, name="graph-load", daemon=True).start()

    def switch(self, path: Path) -> None:
        """Route on another graph file (the Map data screen). The one loaded
        now keeps answering until the new one is ready; if the new one fails
        to load, the old one stays and error says why."""
        with self._lock:
            if self.state == "loading" or self.switching:
                raise RuntimeError("a map is already loading")
            self.path = path
            if self.graph is None:
                self.state = "loading"
            else:
                self.switching = True
        threading.Thread(target=self._load, name="graph-load", daemon=True).start()

    def _load(self) -> None:
        began = time.monotonic()
        try:
            graph = load(self.path)
        except Exception as error:  # noqa: BLE001 -- reported on the status route, never fatal
            traceback.print_exc()
            if self.switching:
                self.switching, self.error = False, f"could not load {self.path.name}: {error}"
            else:
                self.state, self.error = "error", str(error)
            return
        # The index that lays other engines' routes on this map (match.py),
        # built now so the first plan that asks one does not wait for it.
        from app.routing import match
        match._long_pieces(graph)
        self.graph = graph
        self.loaded_path = self.path
        self.load_seconds = round(time.monotonic() - began, 1)
        self.state, self.error, self.switching = "ready", "", False
        print(f"[routing] graph loaded from {self.path}: {graph.node_count:,} intersections, "
              f"{len(graph.seg_a):,} segments, {len(graph.turns.to):,} turns, in {self.load_seconds}s", flush=True)
