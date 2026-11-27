"""Everything one person keeps here, as one JSON file they can download and
load again: on another server, into another account, or back after a mistake.

What it holds: their saved routes, their planner defaults, their keyboard
shortcuts and their themes. API keys stay out on purpose: the file is plain
text, and a key is a secret for another service. The owner's whole-server
backup (app/core/backup.py) is a different thing and is unchanged.

Loading merges rather than replaces where that is safe: a saved route that is
already here (same name, points and options) is skipped, so loading the same
file twice adds nothing; themes are added by id. Planner defaults and
shortcuts are single settings, so the file's replace the current ones when it
has them.
"""
from __future__ import annotations

import json
from typing import Annotated, Any, ClassVar

import msgspec
from msgspec import Meta
from starlette.requests import Request

from app.apidocs import NoInput, Reply, route
from app.apidocs import Request as RequestType
from app.core.security import actor_from
from app.models import saved_routes, shortcuts, themes, to_iso, utc_now
from app.models.users import get_user
from app.routes.helpers import Refused, get_store
from app.routes.planner import PlannerPrefs
from app.routes.saved_routes import RouteName, RouteOptions, RouteSummary, Waypoint, _options
from app.routing import costs

FORMAT = "backroad-route-finder/1"
MAX_FILE_ROUTES = saved_routes.MAX_PER_USER


class ExportedRoute(Reply):
    api_name: ClassVar[str] = "ExportedRoute"
    name: RouteName
    waypoints: Annotated[list[Waypoint], Meta(max_length=10)]
    options: RouteOptions
    summary: RouteSummary = msgspec.field(default_factory=RouteSummary)
    created_at: str = ""


class AccountData(Reply):
    api_name: ClassVar[str] = "AccountData"
    format: Annotated[str, Meta(description=f"Always {FORMAT}.")] = FORMAT
    exported_at: str = ""
    saved_routes: Annotated[list[ExportedRoute], Meta(max_length=MAX_FILE_ROUTES)] = msgspec.field(default_factory=list)
    planner_prefs: Annotated[PlannerPrefs | None, Meta(description="Null: you had not set your own.")] = None
    shortcuts: Annotated[dict[str, str] | None, Meta(description="The keyboard shortcuts you changed.")] = None
    themes: Annotated[list[dict[str, Any]] | None, Meta(description="Your own themes; null while you follow the server's.")] = None
    theme_id: str = ""


@route(
    "GET /api/account/export", summary="Download all your data",
    tag="Account",
    description="Your saved routes, planner defaults, keyboard shortcuts and themes, as one file to keep or load "
    "into another account. API keys are left out.",
)
def export_account(request: Request, body: NoInput) -> AccountData:
    store = get_store(request)
    user = actor_from(request).user
    record = get_user(store, user.id)
    routes = [
        ExportedRoute(name=r.name, waypoints=[Waypoint(**p) for p in r.waypoints], options=_options(r.options),
                      summary=RouteSummary(**r.summary), created_at=r.created_at)
        for r in reversed(saved_routes.list_routes(store, user.id))
    ]
    stored_prefs = saved_routes.get_prefs(store, user.id)
    prefs = None
    if stored_prefs:
        known = {k: v for k, v in stored_prefs.items() if k in PlannerPrefs.__struct_fields__}
        known["road_rules"] = costs.rules_from_legacy(stored_prefs)
        try:
            prefs = msgspec.convert(known, PlannerPrefs)
        except msgspec.ValidationError:
            prefs = None
    return AccountData(
        exported_at=to_iso(utc_now()),
        saved_routes=routes,
        planner_prefs=prefs,
        shortcuts=dict(record.shortcuts) if record else {},
        themes=record.themes if record else None,
        theme_id=record.theme_id if record else "",
    )


class ImportAccount(RequestType):
    data: Annotated[AccountData, Meta(description="A file from GET /api/account/export.")]


class ImportReply(Reply):
    routes_added: int
    routes_skipped: int
    prefs_replaced: bool
    shortcuts_replaced: bool
    themes_added: int


def _route_key(name: str, waypoints: list[dict], options: dict) -> str:
    """What makes two saved routes the same, for skipping duplicates. The
    options go through RouteOptions so a stored route and a file's compare
    alike."""
    clean = msgspec.to_builtins(_options(options))
    return json.dumps([saved_routes.clean_name(name), [[round(p["lat"], 6), round(p["lon"], 6), p.get("label", "")] for p in waypoints], clean],
                      sort_keys=True)


@route(
    "POST /api/account/import", summary="Load your data from a file",
    tag="Account", errors=(409,),
    description="Merge a file from GET /api/account/export into your account. Saved routes already here are "
    "skipped and themes are added by id; planner defaults and shortcuts in the file replace yours.",
)
def import_account(request: Request, body: ImportAccount) -> ImportReply:
    data = body.data
    if data.format != FORMAT:
        raise Refused(400, f"not a {FORMAT} file", field="data")
    store = get_store(request)
    user_id = actor_from(request).user.id

    if data.shortcuts is not None:
        try:
            wanted_shortcuts = shortcuts.check(data.shortcuts)
        except shortcuts.ShortcutsError as invalid:
            raise Refused(400, f"shortcuts: {invalid}", field="data") from None
    current = get_user(store, user_id)
    if current is None:
        raise Refused.internal("import: the signed-in account has no record")
    merged_themes = None
    themes_added = 0
    if data.themes:
        base, own, seen = themes.mine(store, current.themes, current.themes_builtins)
        base = list(base or [])
        have = {t.get("id") for t in base}
        fresh = [t for t in data.themes if t.get("id") not in have]
        if fresh:
            try:
                merged_themes = themes.check(base + fresh)
            except themes.ThemesError as invalid:
                raise Refused(400, f"themes: {invalid}", field="data") from None
            themes_added = len(fresh)

    existing = saved_routes.list_routes(store, user_id)
    seen_keys = {_route_key(r.name, r.waypoints, r.options) for r in existing}
    room = saved_routes.MAX_PER_USER - len(existing)
    added = skipped = 0
    for item in data.saved_routes:
        waypoints = [{"lat": p.lat, "lon": p.lon, "label": p.label} for p in item.waypoints]
        if item.options.loop is None and len(waypoints) < 2:
            skipped += 1
            continue
        options = msgspec.to_builtins(item.options)
        options["road_rules"] = costs.rules_from_legacy(options)
        options.pop("secondary_is_main", None)
        options.pop("allow_unpaved", None)
        key = _route_key(item.name, waypoints, options)
        if key in seen_keys:
            skipped += 1
            continue
        if room <= 0:
            raise Refused(409, f"at most {saved_routes.MAX_PER_USER} saved routes: {added} were added before the limit")
        saved_routes.create(store, user_id, item.name, waypoints, options,
                            {name: getattr(item.summary, name) for name in RouteSummary.__struct_fields__})
        seen_keys.add(key)
        room -= 1
        added += 1

    prefs_replaced = False
    if data.planner_prefs is not None:
        saved_routes.set_prefs(store, user_id, msgspec.to_builtins(data.planner_prefs))
        prefs_replaced = True
    shortcuts_replaced = False
    if data.shortcuts is not None:
        shortcuts.set_shortcuts(store, user_id, wanted_shortcuts)
        shortcuts_replaced = True
    if merged_themes is not None:
        themes.set_user_themes(store, user_id, merged_themes, data.theme_id or current.theme_id, current.themes_builtins)
    return ImportReply(routes_added=added, routes_skipped=skipped, prefs_replaced=prefs_replaced,
                       shortcuts_replaced=shortcuts_replaced, themes_added=themes_added)
