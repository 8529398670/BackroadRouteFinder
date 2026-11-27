"""The map's spatial index: the world cut into cells CELL_DEG degrees square
(about 2 km), and for each cell the segments whose shape passes through it
(cell_keys, sorted; cell_ptr; cell_segs, built by build_stream step I).

It answers the two questions the router asks of space:
- which roads are near a point (snapping, laying another engine's line on
  our map): segments_near;
- which roads a route between two points could use (the corridor its search
  runs in, search.py): corridor_segments.
"""
from __future__ import annotations

import math

import numpy as np

CELL_DEG = 0.02
ROWS = int(round(180 / CELL_DEG))
COLS = int(round(360 / CELL_DEG))
SEG_SPACE = 1 << 28          # a (cell, segment) pair packs as cell * SEG_SPACE + segment
METRES_PER_DEGREE = 111_320.0


def cell_rc(lat, lon) -> tuple[np.ndarray, np.ndarray]:
    rows = np.clip(np.floor((np.asarray(lat, dtype=np.float64) + 90.0) / CELL_DEG).astype(np.int64), 0, ROWS - 1)
    cols = np.floor((np.asarray(lon, dtype=np.float64) + 180.0) / CELL_DEG).astype(np.int64) % COLS
    return rows, cols


def key_of(rows, cols) -> np.ndarray:
    return np.asarray(rows, dtype=np.int64) * COLS + np.asarray(cols, dtype=np.int64) % COLS


def segments_in_cells(graph, keys: np.ndarray) -> np.ndarray:
    """The distinct segments in these cells (any order of keys)."""
    keys = np.unique(np.asarray(keys, dtype=np.int64))
    if not len(keys) or not len(graph.cell_keys):
        return np.zeros(0, dtype=np.int64)
    at = np.searchsorted(graph.cell_keys, keys)
    at = np.minimum(at, len(graph.cell_keys) - 1)
    hit = at[graph.cell_keys[at] == keys]
    if not len(hit):
        return np.zeros(0, dtype=np.int64)
    starts = np.asarray(graph.cell_ptr[hit])
    stops = np.asarray(graph.cell_ptr[hit + 1])
    from app.routing.disk import ranges

    idx = ranges(starts, stops)
    return distinct(np.asarray(graph.cell_segs[idx]), graph.segment_count)


def distinct(values: np.ndarray, size: int) -> np.ndarray:
    """The distinct values (0..size-1), sorted: a bitmap, much faster than
    sorting when there are many."""
    if len(values) < 4096:
        return np.unique(np.asarray(values, dtype=np.int64))
    mask = np.zeros(size, dtype=bool)
    mask[values] = True
    return np.flatnonzero(mask)


def cells_near(lat: float, lon: float, metres: float) -> np.ndarray:
    """Every cell within metres of the point (the square round the circle)."""
    dlat = metres / METRES_PER_DEGREE
    dlon = metres / (METRES_PER_DEGREE * max(0.05, math.cos(math.radians(lat))))
    r0, c0 = cell_rc(lat - dlat, lon - dlon)
    r1, c1 = cell_rc(lat + dlat, lon + dlon)
    rows = np.arange(int(r0), int(r1) + 1)
    span = (int(c1) - int(c0)) % COLS
    cols = (int(c0) + np.arange(span + 1)) % COLS
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    return key_of(rr.ravel(), cc.ravel())


def segments_near(graph, lat: float, lon: float, metres: float) -> np.ndarray:
    return segments_in_cells(graph, cells_near(lat, lon, metres))


def capsule_cells(points: list[tuple[float, float]], width_m: float) -> np.ndarray:
    """The cells within width_m of the path through points (each leg a straight
    line): a capsule round each leg, joined."""
    keys = []
    for (lat1, lon1), (lat2, lon2) in zip(points[:-1], points[1:]):
        lat_mid = (lat1 + lat2) / 2
        kx = METRES_PER_DEGREE * max(0.05, math.cos(math.radians(lat_mid)))
        ky = METRES_PER_DEGREE
        # Corners of the leg's box grown by the width.
        dlat = width_m / ky
        dlon = width_m / kx
        r0, c0 = cell_rc(min(lat1, lat2) - dlat, min(lon1, lon2) - dlon)
        r1, c1 = cell_rc(max(lat1, lat2) + dlat, max(lon1, lon2) + dlon)
        rows = np.arange(int(r0), int(r1) + 1)
        span = (int(c1) - int(c0)) % COLS
        cols = (int(c0) + np.arange(span + 1)) % COLS
        rr, cc = np.meshgrid(rows, cols, indexing="ij")
        rr, cc = rr.ravel(), cc.ravel()
        # Cell centres in local metres from the leg's start.
        clat = (rr + 0.5) * CELL_DEG - 90.0
        clon = (cc + 0.5) * CELL_DEG - 180.0
        dlon_c = (clon - lon1 + 540.0) % 360.0 - 180.0
        px, py = dlon_c * kx, (clat - lat1) * ky
        vx = ((lon2 - lon1 + 540.0) % 360.0 - 180.0) * kx
        vy = (lat2 - lat1) * ky
        length_sq = vx * vx + vy * vy
        t = np.clip((px * vx + py * vy) / length_sq, 0.0, 1.0) if length_sq > 0 else np.zeros(len(px))
        dist = np.hypot(px - t * vx, py - t * vy)
        half_diag = CELL_DEG * 0.75 * ky
        keep = dist <= width_m + half_diag
        keys.append(key_of(rr[keep], cc[keep]))
    return np.unique(np.concatenate(keys)) if keys else np.zeros(0, dtype=np.int64)


def corridor_segments(graph, points: list[tuple[float, float]], width_m: float) -> np.ndarray:
    return segments_in_cells(graph, capsule_cells(points, width_m))
