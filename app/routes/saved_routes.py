"""Saved routes: each person's own list. See app/models/saved_routes.py."""
from __future__ import annotations

from typing import Annotated, ClassVar

from msgspec import Meta
from starlette.requests import Request

from app.apidocs import PATH, Reply, route
from app.apidocs import Request as RequestType
from app.core.security import actor_from
from app.models import saved_routes
from app.routes.helpers import DateTime, Page, Paged, Refused, cut_page, get_store
from app.routes.planner import MAX_WAYPOINTS, Penalty

RouteName = Annotated[str, Meta(min_length=1, max_length=saved_routes.NAME_MAX, extra={"error": f"name must be 1-{saved_routes.NAME_MAX} characters"})]
RouteID = Annotated[int, PATH, Meta(description="The saved route's id, from GET /api/saved-routes.")]


class Waypoint(Reply):
    api_name: ClassVar[str] = "Waypoint"
    lat: Annotated[float, Meta(ge=-90, le=90)]
    lon: Annotated[float, Meta(ge=-180, le=180)]
    label: Annotated[str, Meta(max_length=200)] = ""


class RouteOptions(Reply):
    api_name: ClassVar[str] = "RouteOptions"
    backroads: bool = True
    penalty: Penalty = 8.0
    secondary_is_main: bool = False
    allow_unpaved: bool = False


class RouteSummary(Reply):
    api_name: ClassVar[str] = "RouteSummary"
    seconds: float = 0
    meters: float = 0
    main_seconds: float = 0
    main_meters: float = 0


class SavedRoute(Reply):
    api_name: ClassVar[str] = "SavedRoute"
    id: int
    name: str
    waypoints: list[Waypoint]
    options: RouteOptions
    summary: RouteSummary
    created_at: DateTime
    updated_at: DateTime


def _reply(route: saved_routes.SavedRoute) -> SavedRoute:
    return SavedRoute(
        id=route.id,
        name=route.name,
        waypoints=[Waypoint(**point) for point in route.waypoints],
        options=RouteOptions(**route.options),
        summary=RouteSummary(**route.summary),
        created_at=route.created_at,
        updated_at=route.updated_at,
    )


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
    waypoints: Annotated[list[Waypoint], Meta(min_length=2, max_length=MAX_WAYPOINTS, extra={"error": f"give 2-{MAX_WAYPOINTS} points"})]
    options: RouteOptions
    summary: RouteSummary


@route(
    "POST /api/saved-routes", summary="Save a route",
    tag="Saved routes", status=201, errors=(409,),
    description="Keep a route's points and options under a name, with its summary for the list.",
)
def save_route(request: Request, body: SaveRoute) -> SavedRoute:
    try:
        made = saved_routes.create(
            get_store(request), _owner(request), body.name,
            [{"lat": p.lat, "lon": p.lon, "label": p.label} for p in body.waypoints],
            {name: getattr(body.options, name) for name in RouteOptions.__struct_fields__},
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
