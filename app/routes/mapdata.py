"""The owner's Map data screen: download OpenStreetMap extracts for any US
states (or the whole country) and any other country, and build each into a
map of its own (or several into one joined map). Every built map is used:
each route opens the one that covers its points (app/routing/maps.py), and a
trip across areas gets a joined map built for it in the background.

Downloads and builds run as `manage fetch-osm` / `manage build-graph` in a
child process (app/routing/tasks.py), one task at a time (later ones wait
their turn), with their output
shown on the screen. Building needs osmium (app/requirements-build.txt); the
Docker image compiles it in (docker/Dockerfile), and a local run needs it
installed in .venv.
"""
from __future__ import annotations

import os
import shutil
from typing import Annotated, ClassVar

from msgspec import Meta
from starlette.requests import Request

from app.apidocs import OWNER, NoInput, Reply, route
from app.apidocs import Request as RequestType
from app.core.logs import KIND_ADMIN
from app.routes.helpers import Refused, get_config
from app.routes.logs import audit
from app.routing import files, regions
from app.routing import maps as map_set
from app.routing.tasks import Runner

Slug = Annotated[str, Meta(pattern="^[a-z][a-z-]{1,60}$", description="An area's slug: a US state's (ohio, new-york), us, or a country's (jordan).")]
Slugs = Annotated[list[Slug], Meta(min_length=1, max_length=len(regions.STATES) + len(regions.COUNTRIES) + 1,
                                   extra={"error": "pick at least one area"})]


class FileInfo(Reply):
    api_name: ClassVar[str] = "MapFile"
    bytes: int
    modified: Annotated[str, Meta(description="When it was written, ISO 8601.")]


class AreaInfo(Reply):
    api_name: ClassVar[str] = "MapArea"
    slug: str
    name: str
    approx_mb: Annotated[int, Meta(description="Roughly how big its download is.")]
    downloaded: FileInfo | None
    group: Annotated[str, Meta(description="A country's continent, as Geofabrik groups them; empty for a US state.")] = ""


class GraphInfo(Reply):
    api_name: ClassVar[str] = "MapGraph"
    region: str
    areas: list[str]
    file: FileInfo
    kind: Annotated[str, Meta(description="area (one area's own map) or joined (several, for trips across them).")]
    open: Annotated[bool, Meta(description="Opened now: a route used it lately.")]
    error: Annotated[str, Meta(description="Why it would not open, if it would not.")] = ""
    nodes: int = 0
    segments: int = 0


class WaitingTask(Reply):
    api_name: ClassVar[str] = "MapTaskWaiting"
    id: int
    title: str


class TaskInfo(Reply):
    api_name: ClassVar[str] = "MapTask"
    id: int
    title: str
    state: Annotated[str, Meta(description="running, done, failed or cancelled.")]
    step: int
    steps: int
    started: str
    finished: str
    lines: list[str]


class MapDataReply(Reply):
    areas: Annotated[list[AreaInfo], Meta(description="The US states.")]
    us: AreaInfo
    countries: Annotated[list[AreaInfo], Meta(description="Every other country, by continent then name.")]
    graphs: list[GraphInfo]
    task: TaskInfo | None
    waiting: Annotated[list[WaitingTask], Meta(description="Tasks queued behind the one running, in order.")]
    can_build: Annotated[bool, Meta(description="Whether osmium is installed here, which building needs.")]
    folder: str
    memory_bytes: Annotated[int, Meta(description="This machine's memory, 0 when unknown.")]
    disk_free_bytes: int


def _iso(stamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds") if stamp else ""


def _file(path) -> FileInfo | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return FileInfo(bytes=st.st_size, modified=_iso(st.st_mtime))


def _maps(request: Request) -> map_set.MapSet:
    return request.app.state.maps


def _runner(request: Request) -> Runner:
    return request.app.state.map_tasks


def _can_build() -> bool:
    return map_set.can_build()


def _memory() -> int:
    """The memory this process may use: the machine's, or a container's limit
    (docker.memory) when that is lower -- inside Docker the machine's total
    is the host's, which a build cannot actually have."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        total = 0
    for limit_file in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(limit_file) as handle:
                value = handle.read().strip()
        except OSError:
            continue
        if value.isdigit() and (total == 0 or int(value) < total):
            total = int(value)
        break
    return total


def _reply(request: Request) -> MapDataReply:
    cfg = get_config(request)
    folder = files.mapdata_dir(cfg)
    maps = _maps(request)
    opened = set(maps.open_names())
    areas = [AreaInfo(slug=slug, name=name, approx_mb=mb, downloaded=_file(files.pbf_for(cfg, slug)))
             for slug, (name, mb) in regions.STATES.items()]
    us = AreaInfo(slug=regions.US, name=regions.US_NAME, approx_mb=regions.US_MB, downloaded=_file(files.pbf_for(cfg, regions.US)))
    order = list(regions.GROUPS)
    countries = [AreaInfo(slug=c.slug, name=c.name, approx_mb=c.mb, downloaded=_file(files.pbf_for(cfg, c.slug)),
                          group=regions.GROUPS.get(c.group, c.group))
                 for c in sorted(regions.COUNTRIES.values(), key=lambda c: (order.index(c.group) if c.group in order else 99, c.name))]
    graphs = []
    for name, info in sorted(maps.maps.items(), key=lambda item: (item[1].kind != "area", item[0])):
        try:
            modified = (info.path / "meta.json").stat().st_mtime
        except OSError:
            continue
        graphs.append(GraphInfo(region=name, areas=info.areas, file=FileInfo(bytes=info.bytes, modified=_iso(modified)),
                                kind=info.kind, open=name in opened, error=maps.errors.get(name, ""),
                                nodes=info.nodes, segments=info.segments))
    runner = _runner(request)
    task = runner.task
    task_info = None
    if task is not None:
        task_info = TaskInfo(id=task.id, title=task.title, state=task.state, step=task.step, steps=len(task.steps),
                             started=_iso(task.started), finished=_iso(task.finished), lines=list(task.lines))
    waiting = [WaitingTask(id=t.id, title=t.title) for t in list(runner.waiting)]
    try:
        free = shutil.disk_usage(folder if folder.is_dir() else cfg.app_dir).free
    except OSError:
        free = 0
    return MapDataReply(areas=areas, us=us, countries=countries, graphs=graphs, task=task_info, waiting=waiting, can_build=_can_build(),
                        folder=str(folder), memory_bytes=_memory(), disk_free_bytes=free)


@route(
    "GET /api/owner/mapdata", summary="Map data",
    tag="Owner", role=OWNER, feature="settings",
    description="The maps built so far (which are open now), the extracts downloaded, every US state and "
    "country that can be downloaded, and the download or build running now (with its output) and those waiting.",
)
def get_mapdata(request: Request, body: NoInput) -> MapDataReply:
    return _reply(request)


class RunMapTask(RequestType):
    areas: Slugs
    download: Annotated[str, Meta(pattern="^(missing|all|none)$", description="Which extracts to fetch first: missing ones, all (fresh copies), or none.",
                                  extra={"error": "download must be missing, all or none"})] = "missing"
    build: Annotated[bool, Meta(description="Build a map of each area on its own after downloading (only those without one, "
                                "or all with download=all), or with join one map of them all.")] = True
    join: Annotated[bool, Meta(description="Build one joined map of all the areas instead of one each.")] = False


@route(
    "POST /api/owner/mapdata/run", summary="Download and build maps",
    tag="Owner", role=OWNER, feature="settings", errors=(400,),
    description="Downloads the areas' extracts from Geofabrik (missing ones, all, or none), then builds each "
    "into a map of its own -- or, with join, one map of them all. Every map built is used at once: each "
    "route opens the one that covers it. Runs in the background, after any task already running; GET "
    "/api/owner/mapdata shows its progress.",
)
def run_task(request: Request, body: RunMapTask) -> MapDataReply:
    cfg = get_config(request)
    bad = [slug for slug in body.areas if not regions.valid(slug)]
    if bad:
        raise Refused(400, f"unknown area: {', '.join(bad)}", field="areas")
    slugs = regions.normalized(body.areas)
    maps = _maps(request)
    fetch = [s for s in slugs if body.download == "all" or (body.download == "missing" and not files.pbf_for(cfg, s).is_file())]
    builds: list[list[str]] = []
    if body.build:
        missing = [s for s in slugs if s not in fetch and not files.pbf_for(cfg, s).is_file()]
        if missing:
            raise Refused(400, f"not downloaded yet: {', '.join(regions.name_of(s) for s in missing)}", field="areas")
        if not _can_build():
            raise Refused(400, "building needs osmium, which is not installed here: .venv/bin/pip install -r app/requirements-build.txt")
        if body.join:
            if len(slugs) < 2:
                raise Refused(400, "tick two or more areas to join", field="areas")
            builds.append(map_set.build_step(slugs))
        else:
            builds += [map_set.build_step([s]) for s in slugs if s in fetch or maps.area_map(s) is None]
    if not fetch and not builds:
        raise Refused(400, "nothing to do: every ticked area is downloaded" + (" and has its map" if body.build else ""))
    steps: list[list[str]] = []
    if fetch:
        steps.append(["manage", "fetch-osm", *[f"--state={s}" for s in fetch]])
    steps += builds
    names = ", ".join(regions.name_of(s) for s in slugs)
    title = ("Download and " if fetch and builds else "Download: " if fetch else "") + \
        (("join: " if body.join else "build: ") if builds else "") + names
    title = title[0].upper() + title[1:]
    task = map_set.queue_build(_runner(request), maps, title, steps)
    audit(request, KIND_ADMIN, "map data task started", {"areas": slugs, "download": fetch, "builds": len(builds),
                                                         "join": body.join, "queued": task.state == "waiting"})
    return _reply(request)


class CancelTask(RequestType):
    id: Annotated[int | None, Meta(description="A waiting task to take off the queue; left out, the one running is stopped.")] = None


@route(
    "POST /api/owner/mapdata/cancel", summary="Stop the map task",
    tag="Owner", role=OWNER, feature="settings", errors=(409,),
    description="Stops the download or build running now, or takes a waiting one off the queue (id). "
    "A cut-off download leaves no file behind.",
)
def cancel_task(request: Request, body: CancelTask) -> MapDataReply:
    if not _runner(request).cancel(body.id):
        raise Refused(409, "no such map task is running or waiting")
    audit(request, KIND_ADMIN, "map data task cancelled", {"id": body.id})
    return _reply(request)


class DeleteFile(RequestType):
    kind: Annotated[str, Meta(pattern="^(extract|map)$", description="extract (a downloaded .osm.pbf) or map (a built map).",
                              extra={"error": "kind must be extract or map"})]
    name: Annotated[str, Meta(pattern=r"^[a-z0-9][a-z0-9_+-]{0,127}$", description="The area's slug, or the map's name.",
                              extra={"error": "unknown file"})]


@route(
    "POST /api/owner/mapdata/delete", summary="Delete a map file",
    tag="Owner", role=OWNER, feature="settings", errors=(404, 409),
    description="Deletes a downloaded extract or a built map to free disk space. A joined map is built again "
    "when a trip needs it; an area's own map at the server's next start while its extract is still downloaded.",
)
def delete_file(request: Request, body: DeleteFile) -> MapDataReply:
    cfg = get_config(request)
    if _runner(request).busy():
        raise Refused(409, "wait for the map task to finish")
    if body.kind == "map":
        path = files.graph_for(cfg, body.name)
    else:
        path = files.pbf_for(cfg, body.name)
    if body.kind == "map":
        if not files.is_built(path):
            raise Refused(404, "no such file")
        _maps(request).forget(body.name)
        shutil.rmtree(path)
    else:
        if not path.is_file():
            raise Refused(404, "no such file")
        path.unlink()
    if body.kind == "map":
        files.mapdata_dir(cfg).joinpath(f"{body.name}.json").unlink(missing_ok=True)
        _maps(request).rescan()
    audit(request, KIND_ADMIN, "map file deleted", {"kind": body.kind, "name": body.name})
    return _reply(request)
