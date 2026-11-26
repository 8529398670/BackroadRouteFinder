"""Where the map files live: <app dir>/mapdata/<region>.osm.pbf (the
downloaded extract) and <app dir>/mapdata/<region>.npz (the graph built from
it). Outside storage/ on purpose, so backups leave them out: both can be
fetched and built again at any time."""
from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

from app.core.config import Config

MAPDATA_DIR_NAME = "mapdata"
USER_AGENT = "BackroadRouteFinder/1.0 (self-hosted route planner)"


def mapdata_dir(cfg: Config) -> Path:
    return cfg.app_dir / MAPDATA_DIR_NAME


def pbf_path(cfg: Config) -> Path:
    return mapdata_dir(cfg) / f"{cfg.file.routing.region}.osm.pbf"


def graph_path(cfg: Config) -> Path:
    return mapdata_dir(cfg) / f"{cfg.file.routing.region}.npz"


def download(url: str, target: Path, progress=print) -> int:
    """Fetches url to target through a .part file, so a cut-off download never
    looks finished. Returns the size in bytes."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:  # noqa: S310 -- https only (validated)
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        last_report = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if done - last_report >= 25 << 20:
                last_report = done
                progress(f"  {done >> 20} MB" + (f" of {total >> 20} MB" if total else ""))
    shutil.move(partial, target)
    return target.stat().st_size
