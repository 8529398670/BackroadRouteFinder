"""Saved routes: each person's own list, and sharing them. See app/models/saved_routes.py."""
from __future__ import annotations

from typing import Annotated, ClassVar

import msgspec
from msgspec import Meta
from starlette.requests import Request

from app.apidocs import PATH, PUBLIC, Reply, route
from app.apidocs import Request as RequestType
from app.core.security import actor_from
from app.models import saved_routes, users
from app.routes.helpers import DateTime, Page, Paged, Refused, cut_page, get_store
from app.routes.planner import MAX_POINTS, MAX_SHAPE, MAX_WAYPOINTS, OURS, Alternatives, Keep, Point, ShapePoint, Combine, Follow, LegacySecondary, LegacyUnpaved, LoopSpec, Penalty, RoadFactors, RoadRules, Run, Source, Step
from app.routing import costs

RouteName = Annotated[str, Meta(min_length=1, max_length=saved_routes.NAME_MAX, extra={"error": f"name must be 1-{saved_routes.NAME_MAX} characters"})]
RouteID = Annotated[int, PATH, Meta(description="The saved route's id, from GET /api/saved-routes.")]


class Waypoint(Reply):
    api_name: ClassVar[str] = "Waypoint"
    lat: Annotated[float, Meta(ge=-90, le=90)]
    lon: Annotated[float, Meta(ge=-180, le=180)]
    label: Annotated[str, Meta(max_length=200)] = ""


class RoutePick(Reply):
    api_name: ClassVar[str] = "RoutePick"
    source: Source
    rank: Annotated[int, Meta(ge=0, le=20)] = 0


class SavedLock(Reply):
    api_name: ClassVar[str] = "SavedLock"
    api_doc: ClassVar[str] = "A locked route: the kept line (as POST /api/route takes it) and its open middle's points."
    keep: Keep
    middle: Annotated[list[Point], Meta(min_length=2, max_length=MAX_POINTS)]


class RouteOptions(Reply):
    api_name: ClassVar[str] = "RouteOptions"
    backroads: bool = True
    penalty: Penalty = 8.0
    road_rules: RoadRules = msgspec.field(default_factory=dict)
    road_factors: Annotated[RoadFactors | None, Meta(description="Its avoid strengths; null for a route saved before they could be set (the server's).")] = None
    sources: list[Source] = msgspec.field(default_factory=lambda: [OURS])
    combine: Combine = "best"
    alternatives: Alternatives | None = None
    pick: Annotated[RoutePick | None, Meta(description="Which of the routes offered was chosen, so opening it again shows the same one.")] = None
    loop: Annotated[LoopSpec | None, Meta(description="A loop round a center, planned in place of the waypoints.")] = None
    shape: Annotated[list[ShapePoint], Meta(max_length=MAX_SHAPE, description="Its nudges (POST /api/route shape).")] = msgspec.field(default_factory=list)
    lock: Annotated[SavedLock | None, Meta(description="Its locked ends, if it was locked.")] = None
    follow: Annotated[Follow | None, Meta(description="Its line, when it was made by switching onto other routes at forks.")] = None
    secondary_is_main: LegacySecondary = None
    allow_unpaved: LegacyUnpaved = None


class RouteSummary(Reply):
    api_name: ClassVar[str] = "RouteSummary"
    seconds: float = 0
    meters: float = 0
    main_seconds: float = 0
    main_meters: float = 0


class ShareSettings(Reply):
    api_name: ClassVar[str] = "ShareSettings"
    api_doc: ClassVar[str] = "Who may view a route besides its owner."
    link: Annotated[bool, Meta(description="Anyone signed in who has the link (/r/<token>) may view it.")] = False
    users: Annotated[list[int], Meta(description="The people (user ids, from GET /api/team) it is shared with by name.")] = msgspec.field(default_factory=list)
    public: Annotated[bool, Meta(description="Anyone at all, signed in or not, may view it at /v/<public_token>.")] = False
    public_token: Annotated[str, Meta(description="Its public link is /v/<public_token>; empty while that is off.")] = ""


class SavedRoute(Reply):
    api_name: ClassVar[str] = "SavedRoute"
    id: int
    name: str
    waypoints: list[Waypoint]
    options: RouteOptions
    summary: RouteSummary
    created_at: DateTime
    updated_at: DateTime
    token: Annotated[str, Meta(description="Its link is /r/<token>.")] = ""
    share: Annotated[ShareSettings | None, Meta(description="Who else may view it; only its owner is told.")] = None


def _reply(route: saved_routes.SavedRoute, owner: bool = True) -> SavedRoute:
    return SavedRoute(
        id=route.id,
        name=route.name,
        waypoints=[Waypoint(**point) for point in route.waypoints],
        options=_options(route.options),
        summary=RouteSummary(**route.summary),
        created_at=route.created_at,
        updated_at=route.updated_at,
        token=route.token,
        share=ShareSettings(link=route.share_link, users=route.share_users, public=bool(route.public_token),
                            public_token=route.public_token) if owner else None,
    )


def _options(stored: dict) -> RouteOptions:
    """A route's options as saved, with a route saved before road rules given
    the rules its two toggles meant."""
    known = {name: value for name, value in stored.items() if name in RouteOptions.__struct_fields__}
    known["road_rules"] = costs.rules_from_legacy(stored)
    known.pop("secondary_is_main", None)
    known.pop("allow_unpaved", None)
    return msgspec.convert(known, RouteOptions)


def _owner(request: Request) -> int:
    return actor_from(request).user.id


class SavedRoutesReply(Paged):
    routes: list[SavedRoute]


@route(
    "GET /api/saved-routes", summary="Your saved routes",
    tag="Saved routes",
    description="The routes you saved, most recently changed first, a page at a time.",
)
def list_saved_routes(request: Request, body: Page) -> SavedRoutesReply:
    rows = [_reply(route) for route in saved_routes.list_routes(get_store(request), _owner(request))]
    page = cut_page(rows, body)
    return SavedRoutesReply(routes=page.pop("rows"), **page)


class SaveRoute(RequestType):
    name: RouteName
    waypoints: Annotated[list[Waypoint], Meta(max_length=MAX_WAYPOINTS, description="2 or more; with a loop, none or its start.",
                                              extra={"error": f"give 2-{MAX_WAYPOINTS} points"})]
    options: RouteOptions
    summary: RouteSummary


@route(
    "POST /api/saved-routes", summary="Save a route",
    tag="Saved routes", status=201, errors=(409,),
    description="Keep a route's points and options under a name, with its summary for the list.",
)
def save_route(request: Request, body: SaveRoute) -> SavedRoute:
    if body.options.loop is None and len(body.waypoints) < 2:
        raise Refused(400, f"give 2-{MAX_WAYPOINTS} points", field="waypoints")
    options = msgspec.to_builtins(body.options)
    options["road_rules"] = costs.rules_from_legacy(options)
    options.pop("secondary_is_main", None)
    options.pop("allow_unpaved", None)
    try:
        made = saved_routes.create(
            get_store(request), _owner(request), body.name,
            [{"lat": p.lat, "lon": p.lon, "label": p.label} for p in body.waypoints],
            options,
            {name: getattr(body.summary, name) for name in RouteSummary.__struct_fields__},
        )
    except saved_routes.TooMany as error:
        raise Refused(409, str(error)) from None
    return _reply(made)


class RouteIDPath(RequestType):
    route_id: RouteID


@route(
    "GET /api/saved-routes/{route_id}", summary="One saved route",
    tag="Saved routes", errors=(404,),
    description="A saved route of yours, to plan it again.",
)
def get_saved_route(request: Request, body: RouteIDPath) -> SavedRoute:
    try:
        return _reply(saved_routes.get(get_store(request), _owner(request), body.route_id))
    except saved_routes.NotFound as error:
        raise Refused(404, str(error)) from None


class RenameRoute(RequestType):
    route_id: RouteID
    name: RouteName
    expect: Annotated[str | None, Meta(description="The name you think it has now; a different one answers 409.")] = None


@route(
    "POST /api/saved-routes/{route_id}/rename", summary="Rename a saved route",
    tag="Saved routes", errors=(404, 409),
    description="Give a saved route a new name. With expect, a name changed elsewhere meanwhile is not overwritten.",
)
def rename_saved_route(request: Request, body: RenameRoute) -> SavedRoute:
    try:
        return _reply(saved_routes.rename(get_store(request), _owner(request), body.route_id, body.name, body.expect))
    except saved_routes.NotFound as error:
        raise Refused(404, str(error)) from None
    except saved_routes.RenameConflict as conflict:
        raise Refused(409, str(conflict), name=conflict.current) from None


class RouteIDAction(RequestType):
    route_id: RouteID


class OkReply(Reply):
    ok: bool


@route(
    "POST /api/saved-routes/{route_id}/delete", summary="Delete a saved route",
    tag="Saved routes", errors=(404,),
    description="Hide a saved route. It can be restored for 10 minutes, then it is gone.",
)
def delete_saved_route(request: Request, body: RouteIDAction) -> OkReply:
    try:
        saved_routes.delete(get_store(request), _owner(request), body.route_id)
    except saved_routes.NotFound as error:
        raise Refused(404, str(error)) from None
    return OkReply(ok=True)


@route(
    "POST /api/saved-routes/{route_id}/restore", summary="Restore a deleted saved route",
    tag="Saved routes", errors=(404,),
    description="Bring back a saved route deleted in the last 10 minutes.",
)
def restore_saved_route(request: Request, body: RouteIDAction) -> SavedRoute:
    try:
        return _reply(saved_routes.restore(get_store(request), _owner(request), body.route_id))
    except saved_routes.NotFound as error:
        raise Refused(404, str(error)) from None


# --- sharing ---------------------------------------------------------------------

MAX_TRIP_STEPS = 5000
MAX_TRIP_RUNS = 5000


class TripRoute(Reply):
    api_name: ClassVar[str] = "TripRoute"
    api_doc: ClassVar[str] = "A planned route's line and steps, as POST /api/route answered them."
    runs: Annotated[list[Run], Meta(min_length=1, max_length=MAX_TRIP_RUNS)]
    steps: Annotated[list[Step], Meta(max_length=MAX_TRIP_STEPS)] = msgspec.field(default_factory=list)
    seconds: float = 0
    meters: float = 0


class PublicTrip(Reply):
    api_name: ClassVar[str] = "PublicTrip"
    api_doc: ClassVar[str] = ("What a public link shows: the route as planned when it was shared (its line and turn-by-turn steps) "
                              "and the stops it passes. Never its options, and never planned again.")
    route: TripRoute
    title: Annotated[str, Meta(max_length=200)] = ""
    stops: Annotated[list[Point], Meta(max_length=MAX_POINTS)] = msgspec.field(default_factory=list)


class ShareRoute(RequestType):
    route_id: RouteID
    link: Annotated[bool, Meta(description="Let anyone signed in who has the link view it.")] = False
    public: Annotated[bool | None, Meta(description="Let anyone at all view it, with no sign-in, at /v/<public_token>: the map and "
                                        "navigation only. Needs trip when it is turned on. Left out: unchanged. "
                                        "Turning it off and on again makes a new link.")] = None
    trip: Annotated[PublicTrip | None, Meta(description="The route as planned now, for the public link to show. "
                                            "Sent while public is on, it replaces the snapshot and keeps the link.")] = None
    users: Annotated[list[int], Meta(max_length=saved_routes.MAX_SHARED_USERS, description="User ids to share it with by name (GET /api/team). "
                                     "Replaces the list; ids of people who cannot sign in, and your own, are left out.",
                                     extra={"error": f"at most {saved_routes.MAX_SHARED_USERS} people"})] = msgspec.field(default_factory=list)


@route(
    "POST /api/saved-routes/{route_id}/share", summary="Share a saved route",
    tag="Saved routes", errors=(400, 404),
    description="Set who may view one of your routes: anyone signed in with its link, and/or people by name, "
                "and/or anyone at all with its public link (send the route as planned in trip). "
                "Send link false, no users and public false to make it private again.",
)
def share_saved_route(request: Request, body: ShareRoute) -> SavedRoute:
    store = get_store(request)
    people = [user_id for user_id in dict.fromkeys(body.users) if _can_share_with(store, user_id)]
    owner = _owner(request)
    try:
        if body.public is not None or body.trip is not None:
            public = body.public if body.public is not None else bool(saved_routes.get(store, owner, body.route_id).public_token)
            if public and body.trip is None and not saved_routes.get(store, owner, body.route_id).public_trip:
                raise Refused(400, "a public link needs the route as planned (trip)")
            trip = msgspec.to_builtins(body.trip) if body.trip is not None else None
            saved_routes.set_public(store, owner, body.route_id, public, trip)
        return _reply(saved_routes.set_share(store, owner, body.route_id, body.link, people))
    except saved_routes.NotFound as error:
        raise Refused(404, str(error)) from None


def _can_share_with(store, user_id: int) -> bool:
    user = users.get_user(store, user_id)
    return user is not None and not user.disabled and not user.is_service


def _owner_name(store, user_id: int) -> str:
    user = users.get_user(store, user_id)
    return user.display_name if user is not None else ""


class SharedRoute(Reply):
    api_name: ClassVar[str] = "SharedRoute"
    route: SavedRoute
    owner_name: Annotated[str, Meta(description="Who it belongs to.")]
    mine: Annotated[bool, Meta(description="It is your own route.")]


class SharedRoutesReply(Paged):
    routes: list[SharedRoute]


@route(
    "GET /api/shared-routes", summary="Routes shared with you",
    tag="Saved routes",
    description="Other people's routes they shared with you by name, most recently changed first, a page at a time. "
                "Routes shared only by link are not listed; open their link.",
)
def list_shared_routes(request: Request, body: Page) -> SharedRoutesReply:
    store = get_store(request)
    me = _owner(request)
    rows = [SharedRoute(route=_reply(route, owner=False), owner_name=_owner_name(store, route.owner_id), mine=False)
            for route in saved_routes.shared_with(store, me)]
    page = cut_page(rows, body)
    return SharedRoutesReply(routes=page.pop("rows"), **page)


class TokenPath(RequestType):
    token: Annotated[str, PATH, Meta(max_length=64, description="The token from the route's link, /r/<token>.")]


@route(
    "GET /api/shared-routes/{token}", summary="A route by its link",
    tag="Saved routes", errors=(404,),
    description="The route behind a link, if it is yours or shared with you (by link or by name), to plan it again. "
                "A route not shared with you answers 404, the same as no route.",
)
def get_shared_route(request: Request, body: TokenPath) -> SharedRoute:
    store = get_store(request)
    me = _owner(request)
    try:
        route = saved_routes.find_by_token(store, body.token, me)
    except saved_routes.NotFound as error:
        raise Refused(404, str(error)) from None
    mine = route.owner_id == me
    return SharedRoute(route=_reply(route, owner=mine), owner_name=_owner_name(store, route.owner_id), mine=mine)


# --- public links (no sign-in) ------------------------------------------------------

class PublicRoute(Reply):
    api_name: ClassVar[str] = "PublicRoute"
    name: str
    owner_name: Annotated[str, Meta(description="Who shared it.")]
    trip: PublicTrip


@route(
    "GET /api/public-routes/{token}", summary="A route by its public link",
    tag="Saved routes", errors=(404,), access=PUBLIC,
    description="The route behind a public link (/v/<token>), for anyone, signed in or not: its name, who shared it, "
                "and the route as planned when it was shared. Nothing else about it (no options), and nothing is planned. "
                "A link that was turned off, or a deleted route, answers 404.",
)
def get_public_route(request: Request, body: TokenPath) -> PublicRoute:
    store = get_store(request)
    try:
        route = saved_routes.find_public(store, body.token)
    except saved_routes.NotFound as error:
        raise Refused(404, str(error)) from None
    return PublicRoute(name=route.name, owner_name=_owner_name(store, route.owner_id),
                       trip=msgspec.convert(route.public_trip, PublicTrip))
