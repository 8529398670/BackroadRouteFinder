"""The route planner's API: plan a route, search for a place, the router's
state, and each person's planner defaults. The router itself is app/routing/;
this module only turns HTTP into calls to it and back."""
from __future__ import annotations

from typing import Annotated, ClassVar

import msgspec
from msgspec import Meta
from starlette.requests import Request

from app.apidocs import QUERY, NoInput, Reply, route
from app.apidocs import Request as RequestType
from app.core.security import actor_from
from app.models import saved_routes
from app.models.users import get_owner_id
from app.routes.helpers import Refused, get_config, get_store
from app.models import provider_keys
from app.routing import costs, geocode, router, sources
from app.routing.graph import Holder
from app.routing.providers import ENGINES, OURS
from app.routing.search import NoRoute

MAX_WAYPOINTS = 10
SOURCE_IDS = (OURS, *ENGINES)

Source = Annotated[str, Meta(pattern="^(" + "|".join(SOURCE_IDS) + ")$", description="ours, or another engine: " + ", ".join(ENGINES) + ".",
                             extra={"error": "unknown source"})]
Combine = Annotated[str, Meta(pattern="^(best|all)$", description="best keeps only the best-scoring route; all returns every route, best first.",
                              extra={"error": "combine must be best or all"})]

Penalty = Annotated[float, Meta(ge=1, le=100, description="How many times over a minute on a main road counts. 1 is the plain fastest route.", extra={"error": "penalty must be 1-100"})]


class Point(Reply):
    api_name: ClassVar[str] = "Point"
    lat: Annotated[float, Meta(ge=-90, le=90)]
    lon: Annotated[float, Meta(ge=-180, le=180)]


class Via(Reply):
    api_name: ClassVar[str] = "Via"
    api_doc: ClassVar[str] = "A road the route follows, in order. An empty name is a road with no name in the map."
    name: str
    main: bool
    meters: float
    seconds: float


class Run(Reply):
    api_name: ClassVar[str] = "Run"
    api_doc: ClassVar[str] = "A stretch of the line that is all main road or all backroad, as [lat, lon] pairs."
    main: bool
    coords: list[list[float]]


class PlannedRoute(Reply):
    api_name: ClassVar[str] = "PlannedRoute"
    kind: Annotated[str, Meta(description="backroads, or fastest for the plain fastest route.")]
    source: Annotated[str, Meta(description="Who planned it: ours, or another engine's id.")]
    source_name: str
    also: Annotated[list[str], Meta(description="Other sources that found the same roads.")]
    compare: Annotated[bool, Meta(description="The plain fastest route on our map, shown for comparison (always last).")]
    estimated: Annotated[bool, Meta(description="Planned by another engine: the time is its own, the main-road share is measured on our map.")]
    off_map_meters: Annotated[float, Meta(description="Of meters: not on our map (outside it, or roads it leaves out), counted as backroad.")]
    score: Annotated[float, Meta(description="seconds plus (penalty - 1) times main_seconds: lower is better.")]
    seconds: float
    meters: float
    main_seconds: float
    main_meters: float
    unpaved_meters: float
    delay_seconds: Annotated[float, Meta(description="Of seconds: turning, and waiting at traffic signals and stop signs.")]
    runs: list[Run]
    via: list[Via]


def _holder(request: Request) -> Holder:
    return request.app.state.graph


def _ready_graph(request: Request):
    holder = _holder(request)
    if holder.state == "ready" and holder.graph is not None:
        return holder.graph
    if holder.state == "loading":
        raise Refused(503, "the map is still loading; try again in a few seconds")
    raise Refused(503, "the map has not been built yet: the owner can download and build one on the Map data screen")


# --- plan --------------------------------------------------------------------

class PlanRoute(RequestType):
    waypoints: Annotated[list[Point], Meta(min_length=2, max_length=MAX_WAYPOINTS, description="Start, any stops, destination, in order.", extra={"error": f"give 2-{MAX_WAYPOINTS} points"})]
    penalty: Penalty = 8.0
    secondary_is_main: bool = True
    allow_unpaved: bool = False
    compare_fastest: Annotated[bool, Meta(description="Also plan the plain fastest route, for comparison.")] = True
    sources: Annotated[list[Source], Meta(min_length=1, max_length=len(SOURCE_IDS), description="Who to ask: our own router and/or other engines.",
                                          extra={"error": "pick at least one source"})] = msgspec.field(default_factory=lambda: [OURS])
    combine: Combine = "best"


class Problem(Reply):
    api_name: ClassVar[str] = "SourceProblem"
    api_doc: ClassVar[str] = "A source that gave no route this time, and why."
    source: str
    source_name: str
    message: str


class PlanRouteReply(Reply):
    routes: list[PlannedRoute]
    problems: list[Problem]
    snapped: Annotated[list[Point], Meta(description="Where each waypoint met the road.")]
    milliseconds: int


def source_name(source: str) -> str:
    return "Our map" if source == OURS else ENGINES[source].name


def engine_urls(request: Request) -> dict[str, str]:
    providers = get_config(request).file.providers
    return {engine_id: getattr(providers, engine.url_key) for engine_id, engine in ENGINES.items() if engine.url_key}


def is_owner(request: Request) -> bool:
    actor = actor_from(request)
    return actor is not None and actor.user.id == get_owner_id(get_store(request))


def access_of(request: Request) -> sources.Access:
    """The engines as the signed-in person may use them: their own keys, and
    the owner's shared ones where the policy lets them (provider_keys.effective)."""
    cfg = get_config(request)
    store = get_store(request)
    actor = actor_from(request)
    owner = is_owner(request)
    shared = provider_keys.get_all(store, cfg.secret_key)
    own = provider_keys.get_user_keys(store, cfg.secret_key, actor.user.id) if actor is not None else {}
    policy = provider_keys.get_policy(store)
    keys, _ = provider_keys.effective(shared, own, policy, owner)
    own_required = frozenset() if owner else frozenset(e for e, mode in policy.items() if mode == provider_keys.OWN)
    return sources.Access(keys=keys, urls=engine_urls(request), timeout=float(cfg.file.providers.timeout_s), own_required=own_required)


@route(
    "POST /api/route", summary="Plan a route",
    tag="Planner", errors=(400, 503),
    description="The route through the waypoints that keeps time on main roads down: each minute on a "
    "motorway, trunk or primary road (and secondary, with secondary_is_main) counts penalty times over. "
    "Times include turns, traffic signals and stop signs (delay_seconds). "
    "sources picks who plans: our own router (ours) and/or other engines (see GET /api/routing/status), "
    "each asked for its backroads route and alternatives. Every route is measured on our map and scored "
    "the same way; combine=best keeps the best, combine=all returns them all, best first. A source that "
    "fails is listed in problems and the rest still answer. "
    "With compare_fastest, the plain fastest route on our map comes last (compare=true). A waypoint with "
    "no road within routing.max_snap_m is refused with its index.",
)
def plan_route(request: Request, body: PlanRoute) -> PlanRouteReply:
    graph = _ready_graph(request)
    cfg = get_config(request)
    options = costs.Options(penalty=body.penalty, secondary_is_main=body.secondary_is_main, allow_unpaved=body.allow_unpaved)
    picked = list(dict.fromkeys(body.sources))
    try:
        result = sources.plan(graph, [(p.lat, p.lon) for p in body.waypoints], options, picked, body.combine,
                              body.compare_fastest, cfg.file.routing.max_snap_m, access_of(request))
    except router.PointOffNetwork as off:
        raise Refused(400, f"point {off.index + 1} is not near a road on the map ({off})", field="waypoints", index=off.index) from None
    except NoRoute as error:
        raise Refused(400, str(error)) from None
    routes = [
        PlannedRoute(
            kind=choice.kind,
            source=choice.source,
            source_name=source_name(choice.source),
            also=choice.also,
            compare=choice.compare,
            estimated=choice.estimated,
            off_map_meters=round(choice.off_map_meters, 1),
            score=round(choice.score, 1),
            seconds=round(choice.summary.seconds, 1),
            meters=round(choice.summary.meters, 1),
            main_seconds=round(choice.summary.main_seconds, 1),
            main_meters=round(choice.summary.main_meters, 1),
            unpaved_meters=round(choice.summary.unpaved_meters, 1),
            delay_seconds=round(choice.summary.delay_seconds, 1),
            runs=[Run(main=run.main, coords=run.coords) for run in choice.summary.runs],
            via=[Via(name=v.name, main=v.main, meters=round(v.meters, 1), seconds=round(v.seconds, 1)) for v in choice.summary.via],
        )
        for choice in result.routes
    ]
    problems = [Problem(source=p.source, source_name=source_name(p.source), message=p.message) for p in result.problems]
    snapped = [Point(lat=round(s.lat, 6), lon=round(s.lon, 6)) for s in result.snapped]
    return PlanRouteReply(routes=routes, problems=problems, snapped=snapped, milliseconds=result.milliseconds)


# --- status ------------------------------------------------------------------

class SourceInfo(Reply):
    api_name: ClassVar[str] = "SourceInfo"
    api_doc: ClassVar[str] = "A source the planner can ask, and whether it can be asked now."
    id: str
    name: str
    usable: bool
    needs_key: bool
    why_not: Annotated[str, Meta(description="Why it cannot be asked, while usable is false.")]
    backroads: Annotated[str, Meta(description="How it stays off main roads.")]


def source_list(request: Request) -> list[SourceInfo]:
    access = access_of(request)
    out = [SourceInfo(id=OURS, name=source_name(OURS), usable=True, needs_key=False, why_not="",
                      backroads="Counts each minute on a main road penalty times over; turns and stops priced.")]
    for engine_id, engine in ENGINES.items():
        usable = access.usable(engine_id)
        out.append(SourceInfo(id=engine_id, name=engine.name, usable=usable, needs_key=engine.needs_key,
                              why_not="" if usable else access.why_not(engine_id), backroads=engine.backroads))
    return out


class RoutingStatusReply(Reply):
    state: Annotated[str, Meta(description="missing, loading, ready or error.")]
    error: str
    region: str
    built_at: str
    nodes: int
    segments: int
    bbox: Annotated[list[float], Meta(description="[south, west, north, east] of the map, once loaded.")]
    place_search: Annotated[bool, Meta(description="Whether GET /api/geocode is available.")]
    default_penalty: float
    secondary_is_main: bool
    sources: Annotated[list[SourceInfo], Meta(description="Our router and the other engines, in the order to show them.")]


@route(
    "GET /api/routing/status", summary="The router's state",
    tag="Planner",
    description="Whether the map is loaded, what it covers, and the server's planner defaults.",
)
def routing_status(request: Request, body: NoInput) -> RoutingStatusReply:
    holder = _holder(request)
    routing = get_config(request).file.routing
    meta = holder.graph.meta if holder.graph is not None else {}
    return RoutingStatusReply(
        state=holder.state,
        error=holder.error,
        region=holder.region,
        built_at=meta.get("built_at", ""),
        nodes=meta.get("nodes", 0),
        segments=meta.get("segments", 0),
        bbox=meta.get("bbox", []),
        place_search=routing.geocoder_url != "",
        default_penalty=float(routing.default_penalty),
        secondary_is_main=routing.secondary_is_main,
        sources=source_list(request),
    )


# --- place search ------------------------------------------------------------

class Geocode(RequestType):
    q: Annotated[str, QUERY, Meta(min_length=2, max_length=200, description="An address or place name.", extra={"error": "type 2-200 characters"})] = ""


class Place(Reply):
    api_name: ClassVar[str] = "Place"
    name: str
    lat: float
    lon: float


class GeocodeReply(Reply):
    places: list[Place]


@route(
    "GET /api/geocode", summary="Search for a place",
    tag="Planner", errors=(404, 503),
    description="Addresses and places matching q, within the map's area, best first. Asked of "
    "routing.geocoder_url by the server, at most once a second for everyone together; 404 while "
    "place search is off.",
)
def search_places(request: Request, body: Geocode) -> GeocodeReply:
    if len(body.q.strip()) < 2:
        raise Refused(400, "type 2-200 characters", field="q")
    base = get_config(request).file.routing.geocoder_url
    if not base:
        raise Refused(404, "place search is switched off")
    graph = _ready_graph(request)
    try:
        found = geocode.search(base, body.q, graph.meta["bbox"])
    except geocode.GeocodeError as error:
        raise Refused(503, str(error)) from None
    return GeocodeReply(places=[Place(name=p.name, lat=p.lat, lon=p.lon) for p in found])


# --- planner defaults ---------------------------------------------------------

class PlannerPrefs(Reply):
    api_name: ClassVar[str] = "PlannerPrefs"
    backroads: Annotated[bool, Meta(description="Backroads mode on; off plans the plain fastest route.")] = True
    penalty: Penalty = 8.0
    secondary_is_main: bool = True
    allow_unpaved: bool = False
    compare_fastest: bool = True
    sources: list[Source] = msgspec.field(default_factory=lambda: [OURS])
    combine: Combine = "best"


class PrefsReply(Reply):
    prefs: PlannerPrefs
    saved: Annotated[bool, Meta(description="False while these are the server's defaults, not your own.")]


def _defaults(request: Request) -> PlannerPrefs:
    routing = get_config(request).file.routing
    return PlannerPrefs(penalty=float(routing.default_penalty), secondary_is_main=routing.secondary_is_main)


@route(
    "GET /api/planner/prefs", summary="Your planner defaults",
    tag="Planner",
    description="How the planner opens for you: backroads on or off, the penalty and the toggles.",
)
def get_prefs(request: Request, body: NoInput) -> PrefsReply:
    stored = saved_routes.get_prefs(get_store(request), actor_from(request).user.id)
    if not stored:
        return PrefsReply(prefs=_defaults(request), saved=False)
    defaults = _defaults(request)
    merged = {name: stored.get(name, getattr(defaults, name)) for name in PlannerPrefs.__struct_fields__}
    return PrefsReply(prefs=PlannerPrefs(**merged), saved=True)


class SetPrefs(RequestType):
    backroads: bool
    penalty: Penalty
    secondary_is_main: bool
    allow_unpaved: bool
    compare_fastest: bool
    sources: Annotated[list[Source], Meta(min_length=1, max_length=len(SOURCE_IDS), extra={"error": "pick at least one source"})] = msgspec.field(default_factory=lambda: [OURS])
    combine: Combine = "best"


@route(
    "POST /api/planner/prefs", summary="Change your planner defaults",
    tag="Planner",
    description="Save how the planner opens for you. Send the whole set.",
)
def set_prefs(request: Request, body: SetPrefs) -> PrefsReply:
    prefs = PlannerPrefs(backroads=body.backroads, penalty=body.penalty, secondary_is_main=body.secondary_is_main,
                         allow_unpaved=body.allow_unpaved, compare_fastest=body.compare_fastest,
                         sources=list(dict.fromkeys(body.sources)), combine=body.combine)
    stored = {name: getattr(prefs, name) for name in PlannerPrefs.__struct_fields__}
    if not saved_routes.set_prefs(get_store(request), actor_from(request).user.id, stored):
        raise Refused.internal("planner prefs: the signed-in account has no record")
    return PrefsReply(prefs=prefs, saved=True)
