"""The route planner's API: plan a route, search for a place, the router's
state, and each person's planner defaults. The router itself is app/routing/;
this module only turns HTTP into calls to it and back."""
from __future__ import annotations

from typing import Annotated, ClassVar, Literal

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
from app.routing import classes, costs, geocode, loop, router, sources
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

RuleKey = Literal["interstate", "us_highway", "state_route", "county_road", "local", "service", "track", "unpaved", "restricted"]
RuleValue = Literal["use", "avoid", "never"]
assert RuleKey.__args__ == classes.RULE_KEYS  # type: ignore[attr-defined]
RoadRules = Annotated[dict[RuleKey, RuleValue], Meta(
    description="What to do with each road type: use it, avoid it (each minute on it counts penalty times over), or "
    "never (only when there is no other way). Categories: interstate, us_highway, state_route, county_road, local, "
    "service, track; and any road that is unpaved or restricted (private, permit or farm access). Types left out take "
    "the defaults in GET /api/routing/status.",
    extra={"error": "road_rules: each road type is use, avoid or never"})]
RoadFactors = Annotated[dict[RuleKey, Annotated[float, Meta(ge=1, le=100)]], Meta(
    description="Avoid strengths of their own, by road type: how many times over a minute on that type counts when "
    "its rule is avoid (unpaved: 2 makes gravel count double however high the penalty). Types left out count the "
    "penalty. Left out altogether: the defaults in GET /api/routing/status.",
    extra={"error": "road_factors: each strength is 1-100"})]
LegacySecondary = Annotated[bool | None, Meta(description="Deprecated, use road_rules: false is state_route use.")]
LegacyUnpaved = Annotated[bool | None, Meta(description="Deprecated, use road_rules: true is unpaved and track use.")]


class LoopSpec(Reply):
    api_name: ClassVar[str] = "Loop"
    api_doc: ClassVar[str] = "A round trip circling a center at about radius miles, or radius minutes of fastest driving from it."
    center_lat: Annotated[float, Meta(ge=-90, le=90)]
    center_lon: Annotated[float, Meta(ge=-180, le=180)]
    radius: Annotated[float, Meta(ge=0.5, le=200, extra={"error": "radius must be 0.5-200"})]
    unit: Annotated[Literal["mi", "min"], Meta(description="mi: a circle of that radius; min: as far as that many minutes of the fastest drive reach.")] = "mi"
    start: Annotated[Literal["center", "ring", "stop"], Meta(
        description="center: out from the center and back; ring: on the circle itself; stop: from the first waypoint and back to it.")] = "center"
    direction: Annotated[Literal["cw", "ccw"], Meta(description="Clockwise or anticlockwise round the ring.")] = "cw"
    points: Annotated[Annotated[int, Meta(ge=4, le=8)] | None, Meta(description="How many points the ring has (4-8); left out, routing.loop_points.")] = None
    reuse: Annotated[Annotated[float, Meta(ge=1, le=10)] | None, Meta(description="How many times over a road already driven counts on the "
                                                                    "rest of the loop (1-10; 1 lets it double back); left out, routing.loop_reuse.")] = None


def rules_of(request: Request, road_rules: dict | None, secondary_is_main: bool | None = None, allow_unpaved: bool | None = None) -> dict[str, str]:
    """Every road type's rule: the ones given, the server's defaults for the rest."""
    return costs.rules_from_legacy({"road_rules": road_rules or {}, "secondary_is_main": secondary_is_main, "allow_unpaved": allow_unpaved},
                                   base=default_rules(request))


def factors_of(request: Request, road_factors: dict | None) -> dict[str, float]:
    """The avoid strengths: the ones given (all of them, when given), else the server's."""
    return costs.clean_factors(default_factors(request) if road_factors is None else road_factors)


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
    waypoints: Annotated[list[Point], Meta(max_length=MAX_WAYPOINTS, description="Start, any stops, destination, in order (2 or more). "
                                           "With a loop: none, or the start alone for start=stop.", extra={"error": f"give 2-{MAX_WAYPOINTS} points"})] = msgspec.field(default_factory=list)
    penalty: Penalty = 8.0
    road_rules: RoadRules = msgspec.field(default_factory=dict)
    road_factors: RoadFactors | None = None
    secondary_is_main: LegacySecondary = None
    allow_unpaved: LegacyUnpaved = None
    loop: Annotated[LoopSpec | None, Meta(description="Plan a loop round a center instead of a trip through the waypoints.")] = None
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
    snapped: Annotated[list[Point], Meta(description="Where each waypoint met the road (with a loop: every point it drives through).")]
    ring: Annotated[list[Point], Meta(description="With a loop: the ring's points, in driving order.")]
    road_rules: Annotated[dict[str, str], Meta(description="The road rules the plan used, every type filled in.")]
    road_factors: Annotated[dict[str, float], Meta(description="The avoid strengths the plan used; types left out counted the penalty.")]
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
    "road type its road_rules avoid counts penalty times over (or the type's own strength in road_factors). "
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
    options = costs.Options(penalty=body.penalty, rules=rules_of(request, body.road_rules, body.secondary_is_main, body.allow_unpaved),
                            factors=factors_of(request, body.road_factors))
    routing = cfg.file.routing
    picked = list(dict.fromkeys(body.sources))
    max_snap = cfg.file.routing.max_snap_m
    ring: list = []
    try:
        if body.loop is not None:
            spec = body.loop
            if spec.start == "stop" and len(body.waypoints) != 1:
                raise Refused(400, "a loop from your own start takes that start as its one waypoint", field="waypoints")
            if spec.start != "stop" and body.waypoints:
                raise Refused(400, "a loop from its center or ring takes no waypoints", field="waypoints")
            start = (body.waypoints[0].lat, body.waypoints[0].lon) if body.waypoints else None
            circle = loop.plan(graph, (spec.center_lat, spec.center_lon), spec.radius, spec.unit, spec.start, spec.direction,
                               options, max_snap, start_point=start, count=spec.points or routing.loop_points)
            ring = circle.ring
            result = sources.plan(graph, [(s.lat, s.lon) for s in circle.stops], options, picked, body.combine,
                                  body.compare_fastest, max_snap, access_of(request), snapped=circle.stops,
                                  reuse_factor=spec.reuse if spec.reuse is not None else float(routing.loop_reuse))
        else:
            if len(body.waypoints) < 2:
                raise Refused(400, f"give 2-{MAX_WAYPOINTS} points", field="waypoints")
            result = sources.plan(graph, [(p.lat, p.lon) for p in body.waypoints], options, picked, body.combine,
                                  body.compare_fastest, max_snap, access_of(request))
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
    ring_points = [Point(lat=round(s.lat, 6), lon=round(s.lon, 6)) for s in ring]
    return PlanRouteReply(routes=routes, problems=problems, snapped=snapped, ring=ring_points, road_rules=options.rules,
                          road_factors=options.factors, milliseconds=result.milliseconds)


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
    honours: Annotated[list[str], Meta(description="The road rules it can be told: freeways (interstate), major (us_highway, "
                                       "state_route), unpaved, tracks. The rest are still counted when its routes are scored.")]


def source_list(request: Request) -> list[SourceInfo]:
    access = access_of(request)
    out = [SourceInfo(id=OURS, name=source_name(OURS), usable=True, needs_key=False, why_not="",
                      backroads="Counts each minute on a main road penalty times over; turns and stops priced.",
                      honours=["freeways", "major", "unpaved", "tracks"])]
    for engine_id, engine in ENGINES.items():
        usable = access.usable(engine_id)
        out.append(SourceInfo(id=engine_id, name=engine.name, usable=usable, needs_key=engine.needs_key,
                              why_not="" if usable else access.why_not(engine_id), backroads=engine.backroads,
                              honours=sorted(engine.honours)))
    return out


class RoadType(Reply):
    api_name: ClassVar[str] = "RoadType"
    api_doc: ClassVar[str] = "A road type the planner's road_rules can name, and what it does with it by default."
    id: str
    flag: Annotated[bool, Meta(description="A property any road can have (unpaved, restricted), not a category of its own.")]
    default: str
    default_strength: Annotated[float | None, Meta(description="Its own avoid strength by default; null counts the penalty.")] = None


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
    secondary_is_main: Annotated[bool, Meta(description="Deprecated: see road_types.")]
    loop_radius: Annotated[float, Meta(description="How the loop planner opens: radius, unit, ring points and reuse (routing.loop_*).")]
    loop_unit: str
    loop_points: int
    loop_reuse: float
    road_types: Annotated[list[RoadType], Meta(description="The road types road_rules names, in the order to show them, with the server's defaults.")]
    restricted_on_map: Annotated[bool, Meta(description="Whether the loaded map holds restricted roads (one built before them leaves them out).")]
    sources: Annotated[list[SourceInfo], Meta(description="Our router and the other engines, in the order to show them.")]


def default_rules(request: Request) -> dict[str, str]:
    """routing.road_rules over the built-in defaults (and the old
    routing.secondary_is_main, where a config.yaml still sets it false)."""
    routing = get_config(request).file.routing
    rules = costs.full_rules(costs.parse_pairs(routing.road_rules, "routing.road_rules"))
    if routing.secondary_is_main is False:
        rules["state_route"] = costs.USE
    return rules


def default_factors(request: Request) -> dict[str, float]:
    routing = get_config(request).file.routing
    return costs.clean_factors({k: float(v) for k, v in costs.parse_pairs(routing.avoid_strength, "routing.avoid_strength").items()})


def road_types(request: Request) -> list[RoadType]:
    rules, factors = default_rules(request), default_factors(request)
    return [RoadType(id=key, flag=key in classes.FLAGS, default=rules[key], default_strength=factors.get(key))
            for key in classes.RULE_KEYS]


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
        secondary_is_main=default_rules(request)["state_route"] != costs.USE,
        loop_radius=float(routing.loop_radius),
        loop_unit=routing.loop_unit,
        loop_points=routing.loop_points,
        loop_reuse=float(routing.loop_reuse),
        road_types=road_types(request),
        restricted_on_map="restricted_segments" in meta,
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

LoopRadius = Annotated[float, Meta(ge=0.5, le=200, extra={"error": "loop_radius must be 0.5-200"})]
LoopUnit = Literal["mi", "min"]
LoopPoints = Annotated[int, Meta(ge=4, le=8, extra={"error": "loop_points must be 4-8"})]
LoopReuse = Annotated[float, Meta(ge=1, le=10, extra={"error": "loop_reuse must be 1-10"})]


class PlannerPrefs(Reply):
    api_name: ClassVar[str] = "PlannerPrefs"
    backroads: Annotated[bool, Meta(description="Backroads mode on; off plans the plain fastest route.")] = True
    penalty: Penalty = 8.0
    road_rules: RoadRules = msgspec.field(default_factory=dict)
    road_factors: RoadFactors = msgspec.field(default_factory=dict)
    compare_fastest: bool = True
    sources: list[Source] = msgspec.field(default_factory=lambda: [OURS])
    combine: Combine = "best"
    loop_radius: LoopRadius = 20.0
    loop_unit: LoopUnit = "mi"
    loop_points: LoopPoints = 6
    loop_reuse: LoopReuse = 3.0


class PrefsReply(Reply):
    prefs: PlannerPrefs
    saved: Annotated[bool, Meta(description="False while these are the server's defaults, not your own.")]


def _defaults(request: Request) -> PlannerPrefs:
    routing = get_config(request).file.routing
    return PlannerPrefs(penalty=float(routing.default_penalty), road_rules=default_rules(request), road_factors=default_factors(request),
                        loop_radius=float(routing.loop_radius), loop_unit=routing.loop_unit, loop_points=routing.loop_points,
                        loop_reuse=float(routing.loop_reuse))


@route(
    "GET /api/planner/prefs", summary="Your planner defaults",
    tag="Planner",
    description="How the planner opens for you: backroads on or off, the penalty, the road rules and their avoid "
    "strengths, and the loop settings. What you have not set yourself is the server's (config.yaml routing).",
)
def get_prefs(request: Request, body: NoInput) -> PrefsReply:
    stored = saved_routes.get_prefs(get_store(request), actor_from(request).user.id)
    if not stored:
        return PrefsReply(prefs=_defaults(request), saved=False)
    defaults = _defaults(request)
    merged = {name: stored.get(name, getattr(defaults, name)) for name in PlannerPrefs.__struct_fields__}
    # Saved before road rules: their two toggles, as rules.
    merged["road_rules"] = costs.rules_from_legacy(stored, base=defaults.road_rules)
    merged["road_factors"] = costs.clean_factors(merged["road_factors"])
    return PrefsReply(prefs=msgspec.convert(merged, PlannerPrefs), saved=True)


class SetPrefs(RequestType):
    backroads: bool
    penalty: Penalty
    compare_fastest: bool
    sources: Annotated[list[Source], Meta(min_length=1, max_length=len(SOURCE_IDS), extra={"error": "pick at least one source"})] = msgspec.field(default_factory=lambda: [OURS])
    combine: Combine = "best"
    road_rules: RoadRules = msgspec.field(default_factory=dict)
    road_factors: RoadFactors | None = None
    loop_radius: LoopRadius | None = None
    loop_unit: LoopUnit | None = None
    loop_points: LoopPoints | None = None
    loop_reuse: LoopReuse | None = None
    secondary_is_main: LegacySecondary = None
    allow_unpaved: LegacyUnpaved = None


@route(
    "POST /api/planner/prefs", summary="Change your planner defaults",
    tag="Planner",
    description="Save how the planner opens for you. Send the whole set; a loop setting or road_factors left out keeps the server's.",
)
def set_prefs(request: Request, body: SetPrefs) -> PrefsReply:
    defaults = _defaults(request)
    prefs = PlannerPrefs(backroads=body.backroads, penalty=body.penalty,
                         road_rules=rules_of(request, body.road_rules, body.secondary_is_main, body.allow_unpaved),
                         road_factors=factors_of(request, body.road_factors), compare_fastest=body.compare_fastest,
                         sources=list(dict.fromkeys(body.sources)), combine=body.combine,
                         loop_radius=body.loop_radius if body.loop_radius is not None else defaults.loop_radius,
                         loop_unit=body.loop_unit or defaults.loop_unit,
                         loop_points=body.loop_points or defaults.loop_points,
                         loop_reuse=body.loop_reuse if body.loop_reuse is not None else defaults.loop_reuse)
    stored = msgspec.to_builtins(prefs)
    if not saved_routes.set_prefs(get_store(request), actor_from(request).user.id, stored):
        raise Refused.internal("planner prefs: the signed-in account has no record")
    return PrefsReply(prefs=prefs, saved=True)
