"""The road graph: a directory of arrays build_stream.py wrote, mapped from
disk rather than read into memory (disk.py), and only read, by any number of
requests at once. A route touches the pages of the roads near it, which the
operating system keeps in its cache while there is room, so a map of the
whole country needs no more memory than the routes being planned.

Opening a map is quick; it still happens in a background thread at startup,
and routes say "still loading" until it is ready.

Directed edges: a two-way segment becomes two edges, a oneway one edge. The
search runs on the edges themselves, moving from one to the next through a
turn (turns.py), so that what a turn costs can depend on both roads.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.routing import build_stream, classes, turns
from app.routing.disk import ArrayDir

METRES_PER_DEGREE = 111_320.0


class Names:
    """Road names by id, read from the names blob on demand (a country has
    millions; they stay on disk). names[i] is a str."""

    def __init__(self, blob: np.ndarray) -> None:
        self.blob = blob
        newline = np.flatnonzero(np.asarray(blob) == 10) if len(blob) else np.zeros(0, dtype=np.int64)
        self.starts = np.concatenate(([0], newline + 1)).astype(np.int64)
        self.stops = np.concatenate((newline, [len(blob)])).astype(np.int64)

    def __len__(self) -> int:
        return len(self.starts)

    def __getitem__(self, index: int) -> str:
        index = int(index)
        if index < 0 or index >= len(self.starts):
            return ""
        return bytes(self.blob[self.starts[index]:self.stops[index]]).decode("utf-8", errors="replace")


@dataclass
class Graph:
    meta: dict
    folder: Path
    names: Names
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
    seg_category: np.ndarray
    seg_restricted: np.ndarray
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
    node_edge_ptr: np.ndarray
    # each segment's edge a->b and b->a, or -1 where its oneway forbids it
    seg_fwd: np.ndarray
    seg_rev: np.ndarray
    # the moves between edges, and what each costs
    turns: turns.Turns
    # the spatial index (grid.py)
    cell_keys: np.ndarray
    cell_ptr: np.ndarray
    cell_segs: np.ndarray

    @property
    def node_count(self) -> int:
        return len(self.node_lat)

    @property
    def segment_count(self) -> int:
        return len(self.seg_a)

    @staticmethod
    def project(lat: np.ndarray | float, lon: np.ndarray | float, lat0: float) -> np.ndarray:
        """Local metres round latitude lat0: good for distances over a few
        kilometres, anywhere."""
        cos0 = float(np.cos(np.radians(lat0)))
        return np.column_stack((np.asarray(lon, dtype=np.float64) * cos0 * METRES_PER_DEGREE,
                                np.asarray(lat, dtype=np.float64) * METRES_PER_DEGREE))

    def segment_of_point(self, position: int) -> int:
        """Which segment a geometry index belongs to."""
        return int(np.searchsorted(self.seg_g0, position, side="right") - 1)


def load(path: Path) -> Graph:
    """Opens a map directory. Nothing is read into memory here: every array is
    mapped from its file and read as routes use it (disk.py)."""
    if path.suffix == ".npz" or path.is_file():
        raise ValueError(f"{path.name} is an old single-file map: convert it with `manage convert-graph` "
                         "or rebuild it on the Map data screen")
    meta = json.loads((path / "meta.json").read_text())
    if meta.get("version") != build_stream.FORMAT_VERSION:
        raise ValueError(f"map is format {meta.get('version')}, this server reads {build_stream.FORMAT_VERSION}: "
                         "rebuild it on the Map data screen")
    if meta.get("classes") != list(classes.CLASSES):
        raise ValueError("map was built with other road classes: rebuild it on the Map data screen")
    if meta.get("categories") != list(classes.CATEGORIES):
        raise ValueError("map was built with other road categories: rebuild it on the Map data screen")
    arrays = ArrayDir(path)
    a = arrays.open
    return Graph(
        meta=meta, folder=path, names=Names(a("names")),
        node_lat=a("node_lat"), node_lon=a("node_lon"),
        seg_a=a("seg_a"), seg_b=a("seg_b"), seg_len=a("seg_len"), seg_seconds=a("seg_seconds"),
        seg_class=a("seg_class"), seg_oneway=a("seg_oneway"), seg_unpaved=a("seg_unpaved"),
        seg_category=a("seg_category"), seg_restricted=a("seg_restricted"), seg_name=a("seg_name"),
        seg_g0=a("seg_g0"), seg_g1=a("seg_g1"), geom_lat=a("geom_lat"), geom_lon=a("geom_lon"),
        edge_seg=a("edge_seg"), edge_rev=a("edge_rev"), edge_src=a("edge_src"), edge_dst=a("edge_dst"),
        node_edge_ptr=a("node_edge_ptr"), seg_fwd=a("seg_fwd"), seg_rev=a("seg_rev"),
        turns=turns.Turns(ptr=a("turn_ptr"), to=a("turn_to"), seconds=a("turn_seconds")),
        cell_keys=a("cell_keys"), cell_ptr=a("cell_ptr"), cell_segs=a("cell_segs"),
    )


def old_categories(seg_class: np.ndarray, seg_name: np.ndarray, names: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Categories for a format-2 file, which has none stored: from each road's
    class and the ref kept in its label. Such a file left restricted roads out,
    so none is restricted."""
    pairs, inverse = np.unique(np.column_stack((seg_name.astype(np.int64), seg_class.astype(np.int64))), axis=0, return_inverse=True)
    table = np.empty(len(pairs), dtype=np.uint8)
    for row, (name_id, class_id) in enumerate(pairs):
        label = names[int(name_id)]
        table[row] = classes.CATEGORY_INDEX[classes.category_of(classes.CLASSES[int(class_id)], classes.ref_of_label(label))]
    return table[inverse.ravel()], np.zeros(len(seg_class), dtype=bool)
