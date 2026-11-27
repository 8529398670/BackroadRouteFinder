"""Arrays on disk: how a map stays out of memory.

A map is a directory of raw binary files, one per array, plus arrays.json
naming each one's dtype and length. Reading opens them with np.memmap, so
nothing is read until it is used, and what is read sits in the operating
system's page cache, which gives it back when memory runs short. A route only
touches the pages of the roads near it.

Building writes the same way: Appender adds to a file a chunk at a time, so
an array far larger than memory can be made without holding it. The big
sorts and joins a build needs are done by partitioning: rows are scattered
into bucket files small enough to sort in memory one at a time (partition,
buckets), the way a database sorts what does not fit.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Iterator

import numpy as np

MANIFEST = "arrays.json"


def _file(folder: Path, name: str) -> Path:
    return folder / f"{name}.bin"


class Appender:
    """Adds rows to one array file. Small appends are gathered in memory and
    written FLUSH_BYTES at a time."""

    FLUSH_BYTES = 8 << 20

    def __init__(self, path: Path, dtype) -> None:
        self.path = path
        self.dtype = np.dtype(dtype)
        self.count = 0
        self._parts: list[np.ndarray] = []
        self._pending = 0
        # Opened only while writing, so a build may have hundreds of these.
        path.write_bytes(b"")

    def add(self, values) -> None:
        array = np.ascontiguousarray(values, dtype=self.dtype).ravel()
        if not len(array):
            return
        self._parts.append(array)
        self._pending += array.nbytes
        self.count += len(array)
        if self._pending >= self.FLUSH_BYTES:
            self.flush()

    def flush(self) -> None:
        if self._parts:
            with self.path.open("ab") as out:
                for part in self._parts:
                    out.write(part.tobytes())
        self._parts, self._pending = [], 0

    def close(self) -> int:
        self.flush()
        return self.count


class ArrayDir:
    """A directory of arrays with their manifest."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / MANIFEST
        self.manifest: dict[str, list] = json.loads(path.read_text()) if path.is_file() else {}

    def save(self) -> None:
        temp = self.folder / (MANIFEST + ".part")
        temp.write_text(json.dumps(self.manifest, indent=1, sort_keys=True))
        os.replace(temp, self.folder / MANIFEST)

    def __contains__(self, name: str) -> bool:
        return name in self.manifest

    def appender(self, name: str, dtype) -> "_Tracked":
        return _Tracked(self, name, dtype)

    def write(self, name: str, values) -> np.ndarray:
        array = np.ascontiguousarray(values)
        with _file(self.folder, name).open("wb") as out:
            out.write(array.tobytes())
        self.manifest[name] = [array.dtype.str, int(array.size)]
        self.save()
        return array

    def create(self, name: str, dtype, length: int, fill=None) -> np.memmap | np.ndarray:
        """A writable array of the given length, on disk."""
        dtype = np.dtype(dtype)
        path = _file(self.folder, name)
        with path.open("wb") as out:
            out.truncate(int(length) * dtype.itemsize)
        self.manifest[name] = [dtype.str, int(length)]
        self.save()
        if length == 0:
            return np.zeros(0, dtype=dtype)
        array = np.memmap(path, dtype=dtype, mode="r+", shape=(int(length),))
        if fill is not None:
            for start in range(0, length, 1 << 24):
                array[start:start + (1 << 24)] = fill
        return array

    def open(self, name: str, writable: bool = False) -> np.memmap | np.ndarray:
        dtype, length = self.manifest[name]
        if length == 0:
            return np.zeros(0, dtype=np.dtype(dtype))
        return np.memmap(_file(self.folder, name), dtype=np.dtype(dtype), mode="r+" if writable else "r", shape=(length,))

    def length(self, name: str) -> int:
        return int(self.manifest[name][1])

    def remove(self, name: str) -> None:
        self.manifest.pop(name, None)
        _file(self.folder, name).unlink(missing_ok=True)
        self.save()

    def rename(self, old: str, new: str) -> None:
        os.replace(_file(self.folder, old), _file(self.folder, new))
        self.manifest[new] = self.manifest.pop(old)
        self.save()

    def adopt(self, name: str, other: "ArrayDir", as_name: str | None = None) -> None:
        """Move an array from another directory into this one."""
        target = as_name or name
        shutil.move(str(_file(other.folder, name)), str(_file(self.folder, target)))
        self.manifest[target] = other.manifest.pop(name)
        other.save()
        self.save()


class _Tracked(Appender):
    def __init__(self, owner: ArrayDir, name: str, dtype) -> None:
        super().__init__(_file(owner.folder, name), dtype)
        self.owner, self.name = owner, name

    def close(self) -> int:
        count = super().close()
        self.owner.manifest[self.name] = [self.dtype.str, count]
        self.owner.save()
        return count


def chunks(length: int, size: int) -> Iterator[tuple[int, int]]:
    """(start, stop) covering 0..length in pieces of at most size."""
    size = max(1, int(size))
    for start in range(0, length, size):
        yield start, min(length, start + size)


def rows_for(budget_bytes: int, bytes_per_row: int) -> int:
    """How many rows a step may hold at once within its memory budget."""
    return max(1 << 16, budget_bytes // max(1, bytes_per_row))


class Partitions:
    """Rows scattered by bucket into a file per bucket, then read back one
    bucket at a time as named columns."""

    def __init__(self, folder: Path, buckets: int, columns: dict[str, object]) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.buckets = max(1, int(buckets))
        self.dtype = np.dtype([(name, np.dtype(dtype)) for name, dtype in columns.items()])
        self._out = [Appender(folder / f"{b}.bin", self.dtype) for b in range(self.buckets)]
        for appender in self._out:
            appender.FLUSH_BYTES = max(1 << 16, (128 << 20) // self.buckets)

    def add(self, bucket: np.ndarray, **values: np.ndarray) -> None:
        if not len(bucket):
            return
        rows = np.empty(len(bucket), dtype=self.dtype)
        for name in self.dtype.names:
            rows[name] = values[name]
        order = np.argsort(bucket, kind="stable")
        rows = rows[order]
        cuts = np.searchsorted(np.asarray(bucket)[order], np.arange(self.buckets + 1))
        for b in range(self.buckets):
            lo, hi = cuts[b], cuts[b + 1]
            if lo < hi:
                self._out[b].add(rows[lo:hi])

    def close(self) -> None:
        for appender in self._out:
            appender.close()

    def read(self, b: int) -> np.ndarray:
        """The bucket's rows, a structured array: rows["name"] is a column."""
        path = self.folder / f"{b}.bin"
        return np.fromfile(path, dtype=self.dtype) if path.stat().st_size else np.zeros(0, dtype=self.dtype)

    def drop(self, b: int) -> None:
        (self.folder / f"{b}.bin").unlink(missing_ok=True)

    def cleanup(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


def ranges(starts: np.ndarray, stops: np.ndarray) -> np.ndarray:
    """The indices start..stop-1 of every pair, concatenated (stop exclusive)."""
    starts = np.asarray(starts, dtype=np.int64)
    lengths = np.asarray(stops, dtype=np.int64) - starts
    lengths = np.maximum(lengths, 0)
    total = int(lengths.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64)
    offsets = np.repeat(np.cumsum(lengths) - lengths, lengths)
    return np.repeat(starts, lengths) + (np.arange(total, dtype=np.int64) - offsets)


def peak_rss_mb() -> float:
    """This process's peak memory so far (resident set), in MB."""
    import resource
    import sys

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1 << 20) if sys.platform == "darwin" else peak / 1024
