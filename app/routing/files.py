"""Where the map files live: <app dir>/mapdata/<region>.osm.pbf (the
downloaded extract) and <app dir>/mapdata/<region>.graph/ (the map built from
it, a directory of arrays: graph.py). Outside storage/ on purpose, so backups leave them out: both can be
fetched and built again at any time.

Which map the server routes on is mapdata/active.json when the Map data
screen has picked one, and routing.region otherwise. Each built map has a
<region>.json beside it naming the areas it holds."""
from __future__ import annotations

import json
import os
import shutil
import urllib.request
from pathlib import Path

from app.core.config import Config

MAPDATA_DIR_NAME = "mapdata"
USER_AGENT = "BackroadRouteFinder/1.0 (self-hosted route planner)"


def mapdata_dir(cfg: Config) -> Path:
    return cfg.app_dir / MAPDATA_DIR_NAME


ACTIVE_FILE = "active.json"


def pbf_path(cfg: Config) -> Path:
    return mapdata_dir(cfg) / f"{cfg.file.routing.region}.osm.pbf"


def pbf_for(cfg: Config, slug: str) -> Path:
    return mapdata_dir(cfg) / f"{slug}.osm.pbf"


GRAPH_SUFFIX = ".graph"


def graph_for(cfg: Config, region: str) -> Path:
    return mapdata_dir(cfg) / f"{region}{GRAPH_SUFFIX}"


def region_of(path: Path) -> str:
    return path.name.removesuffix(GRAPH_SUFFIX)


def is_built(path: Path) -> bool:
    return (path / "meta.json").is_file()


def size_of(path: Path) -> int:
    """A map directory's size on disk."""
    try:
        return sum(p.stat().st_size for p in path.iterdir() if p.is_file())
    except OSError:
        return 0


def active_region(cfg: Config) -> str:
    try:
        region = json.loads((mapdata_dir(cfg) / ACTIVE_FILE).read_text()).get("region", "")
    except (OSError, ValueError):
        region = ""
    return region if isinstance(region, str) and region and "/" not in region and not region.startswith(".") else cfg.file.routing.region


def set_active(cfg: Config, region: str) -> None:
    folder = mapdata_dir(cfg)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = folder / (ACTIVE_FILE + ".part")
    temp.write_text(json.dumps({"region": region}))
    os.replace(temp, folder / ACTIVE_FILE)


def graph_path(cfg: Config) -> Path:
    return graph_for(cfg, active_region(cfg))


def write_areas(cfg: Config, region: str, slugs: list[str]) -> None:
    (mapdata_dir(cfg) / f"{region}.json").write_text(json.dumps({"areas": sorted(set(slugs))}))


def areas_of(cfg: Config, region: str) -> list[str]:
    try:
        areas = json.loads((mapdata_dir(cfg) / f"{region}.json").read_text()).get("areas", [])
    except (OSError, ValueError):
        return [region]
    return [a for a in areas if isinstance(a, str)]


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
            if done - last_report >= 10 << 20:
                last_report = done
                progress(f"  {done >> 20} MB" + (f" of {total >> 20} MB ({done * 100 // total}%)" if total else ""))
    shutil.move(partial, target)
    return target.stat().st_size
