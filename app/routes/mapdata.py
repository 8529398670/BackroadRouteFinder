"""The owner's Map data screen: download OpenStreetMap extracts for any US
states (or the whole country), build a map from them, and switch the server
to it without a restart.

Downloads and builds run as `manage fetch-osm` / `manage build-graph` in a
child process (app/routing/tasks.py), one task at a time, with their output
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
from app.routing.graph import Holder
from app.routing.tasks import Runner

Slug = Annotated[str, Meta(pattern="^[a-z][a-z-]{1,40}$", description="A state's Geofabrik slug (ohio, new-york), or us.")]
Slugs = Annotated[list[Slug], Meta(min_length=1, max_length=len(regions.STATES) + 1, extra={"error": "pick at least one state"})]


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


class GraphInfo(Reply):
    api_name: ClassVar[str] = "MapGraph"
    region: str
    areas: list[str]
    file: FileInfo
    active: bool


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


class Loaded(Reply):
    api_name: ClassVar[str] = "MapLoaded"
    region: str
    areas: list[str]
    state: Annotated[str, Meta(description="missing, loading, ready or error.")]
    switching: Annotated[bool, Meta(description="Another map is loading; this one answers until it is ready.")]
    error: str
    built_at: str
    nodes: int
    segments: int


class MapDataReply(Reply):
    loaded: Loaded
    areas: list[AreaInfo]
    us: AreaInfo
    graphs: list[GraphInfo]
    task: TaskInfo | None
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


def _holder(request: Request) -> Holder:
    return request.app.state.graph


def _runner(request: Request) -> Runner:
    return request.app.state.map_tasks


def _can_build() -> bool:
    import importlib.util

    return importlib.util.find_spec("osmium") is not None


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
    holder = _holder(request)
    region = holder.region
    meta = holder.graph.meta if holder.graph is not None else {}
    loaded = Loaded(region=region, areas=files.areas_of(cfg, region), state=holder.state, switching=holder.switching,
                    error=holder.error, built_at=meta.get("built_at", ""), nodes=meta.get("nodes", 0), segments=meta.get("segments", 0))
    areas = [AreaInfo(slug=slug, name=name, approx_mb=mb, downloaded=_file(files.pbf_for(cfg, slug)))
             for slug, (name, mb) in regions.STATES.items()]
    us = AreaInfo(slug=regions.US, name=regions.US_NAME, approx_mb=regions.US_MB, downloaded=_file(files.pbf_for(cfg, regions.US)))
    graphs = []
    if folder.is_dir():
        for path in sorted(folder.glob("*.npz")):
            info = _file(path)
            if info is not None:
                graphs.append(GraphInfo(region=path.stem, areas=files.areas_of(cfg, path.stem), file=info, active=path.stem == region))
    task = _runner(request).task
    task_info = None
    if task is not None:
        task_info = TaskInfo(id=task.id, title=task.title, state=task.state, step=task.step, steps=len(task.steps),
                             started=_iso(task.started), finished=_iso(task.finished), lines=list(task.lines))
    try:
        free = shutil.disk_usage(folder if folder.is_dir() else cfg.app_dir).free
    except OSError:
        free = 0
    return MapDataReply(loaded=loaded, areas=areas, us=us, graphs=graphs, task=task_info, can_build=_can_build(),
                        folder=str(folder), memory_bytes=_memory(), disk_free_bytes=free)


@route(
    "GET /api/owner/mapdata", summary="Map data",
    tag="Owner", role=OWNER, feature="settings",
    description="The map the server routes on, the extracts downloaded and maps built so far, every "
    "state that can be downloaded, and the download or build running now (with its output).",
)
def get_mapdata(request: Request, body: NoInput) -> MapDataReply:
    return _reply(request)


class RunMapTask(RequestType):
    areas: Slugs
    download: Annotated[str, Meta(pattern="^(missing|all|none)$", description="Which extracts to fetch first: missing ones, all (fresh copies), or none.",
                                  extra={"error": "download must be missing, all or none"})] = "missing"
    build: Annotated[bool, Meta(description="Build one map from the areas after downloading.")] = True
    activate: Annotated[bool, Meta(description="Switch the server to the new map once it is built.")] = True


def _activate(request: Request, region: str) -> str:
    cfg = get_config(request)
    path = files.graph_for(cfg, region)
    if not path.is_file():
        raise Refused(404, f"no map named {region} has been built")
    _holder(request).switch(path)
    files.set_active(cfg, region)
    return f"switching to {region}: loading it now; the current map answers until it is ready"


@route(
    "POST /api/owner/mapdata/run", summary="Download and build a map",
    tag="Owner", role=OWNER, feature="settings", errors=(400, 409),
    description="Downloads the areas' extracts from Geofabrik (missing ones, all, or none), builds one map "
    "from them, and switches the server to it -- each step optional. Runs in the background; GET "
    "/api/owner/mapdata shows its progress. One task at a time.",
)
def run_task(request: Request, body: RunMapTask) -> MapDataReply:
    cfg = get_config(request)
    bad = [slug for slug in body.areas if not regions.valid(slug)]
    if bad:
        raise Refused(400, f"unknown area: {', '.join(bad)}", field="areas")
    region = regions.region_name(body.areas)
    slugs = [regions.US] if region == regions.US else sorted(set(body.areas))
    fetch = [s for s in slugs if body.download == "all" or (body.download == "missing" and not files.pbf_for(cfg, s).is_file())]
    if not body.build and not fetch:
        raise Refused(400, "nothing to do: every extract is already downloaded")
    if body.build:
        missing = [s for s in slugs if s not in fetch and not files.pbf_for(cfg, s).is_file()]
        if missing:
            raise Refused(400, f"not downloaded yet: {', '.join(regions.name_of(s) for s in missing)}", field="areas")
        if not _can_build():
            raise Refused(400, "building needs osmium, which is not installed here: .venv/bin/pip install -r app/requirements-build.txt")
    steps: list[list[str]] = []
    if fetch:
        steps.append(["manage", "fetch-osm", *[f"--state={s}" for s in fetch]])
    if body.build:
        steps.append(["manage", "build-graph", *[f"--state={s}" for s in slugs]])
    names = ", ".join(regions.name_of(s) for s in slugs)
    title = ("Download and build: " if fetch and body.build else "Build: " if body.build else "Download: ") + names
    then = (lambda: _activate(request, region)) if body.build and body.activate else None
    try:
        _runner(request).start(title, steps, then)
    except RuntimeError as error:
        raise Refused(409, str(error)) from None
    audit(request, KIND_ADMIN, "map data task started", {"areas": slugs, "download": fetch, "build": body.build, "activate": body.activate})
    return _reply(request)


@route(
    "POST /api/owner/mapdata/cancel", summary="Stop the map task",
    tag="Owner", role=OWNER, feature="settings", errors=(409,),
    description="Stops the download or build running now. A cut-off download leaves no file behind.",
)
def cancel_task(request: Request, body: NoInput) -> MapDataReply:
    if not _runner(request).cancel():
        raise Refused(409, "no map task is running")
    audit(request, KIND_ADMIN, "map data task cancelled", {})
    return _reply(request)


class RegionOnly(RequestType):
    region: Annotated[str, Meta(pattern=r"^[a-z0-9][a-z0-9_+-]{0,127}$", description="A built map's name, as GET /api/owner/mapdata lists it.",
                                extra={"error": "unknown map"})]


@route(
    "POST /api/owner/mapdata/activate", summary="Route on another map",
    tag="Owner", role=OWNER, feature="settings", errors=(404, 409),
    description="Switches the server to a map built earlier, without a restart. The current one keeps "
    "answering until the new one has loaded. Remembered across restarts.",
)
def activate(request: Request, body: RegionOnly) -> MapDataReply:
    try:
        _activate(request, body.region)
    except RuntimeError as error:
        raise Refused(409, str(error)) from None
    audit(request, KIND_ADMIN, "map switched", {"region": body.region})
    return _reply(request)


class DeleteFile(RequestType):
    kind: Annotated[str, Meta(pattern="^(extract|map)$", description="extract (a downloaded .osm.pbf) or map (a built .npz).",
                              extra={"error": "kind must be extract or map"})]
    name: Annotated[str, Meta(pattern=r"^[a-z0-9][a-z0-9_+-]{0,127}$", description="The area's slug, or the map's name.",
                              extra={"error": "unknown file"})]


@route(
    "POST /api/owner/mapdata/delete", summary="Delete a map file",
    tag="Owner", role=OWNER, feature="settings", errors=(404, 409),
    description="Deletes a downloaded extract or a built map to free disk space. The map the server is "
    "routing on cannot be deleted.",
)
def delete_file(request: Request, body: DeleteFile) -> MapDataReply:
    cfg = get_config(request)
    if _runner(request).busy():
        raise Refused(409, "wait for the map task to finish")
    if body.kind == "map":
        if body.name in (_holder(request).region, _holder(request).path.stem):
            raise Refused(409, "that is the map the server is routing on: switch to another first")
        path = files.graph_for(cfg, body.name)
    else:
        path = files.pbf_for(cfg, body.name)
    if not path.is_file():
        raise Refused(404, "no such file")
    path.unlink()
    if body.kind == "map":
        files.mapdata_dir(cfg).joinpath(f"{body.name}.json").unlink(missing_ok=True)
    audit(request, KIND_ADMIN, "map file deleted", {"kind": body.kind, "name": body.name})
    return _reply(request)
