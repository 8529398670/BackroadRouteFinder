"""Routes people saved from the planner, and each person's planner defaults.

A saved route keeps what is needed to plan it again -- the points and the
options -- plus the summary from when it was saved, for the list. The line
itself is not stored: the map changes, and planning again takes a second.

Deleting is soft: the record is marked and hidden, and its owner can restore
it for RESTORE_WINDOW (the toast's Undo). After that it is removed for good
the next time the owner's list is read.

Sharing: every route has a random token, its link (/r/<token>). Its owner
can let anyone signed in who has the link view it, and/or name the people who
may (they also see it under "Shared with me"). Only the owner changes it.
Viewing re-plans it from the points and options, like opening your own.

The planner defaults (backroads on or off, the penalty, the two toggles) and
the recent places (the stop boxes' quick picks) live
on the user's own record, the way keyboard shortcuts do, so they follow the
person to every browser.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.core.db import BUCKET_SAVED_ROUTES, BUCKET_USERS, Record, Store
from app.core.encryption import constant_time_equal, url_token
from app.models import from_iso, to_iso, utc_now

RESTORE_WINDOW = timedelta(minutes=10)
MAX_PER_USER = 500
NAME_MAX = 80
TOKEN_BYTES = 12
MAX_SHARED_USERS = 200


class NotFound(Exception):
    """No such route, or not this person's."""


class RenameConflict(Exception):
    def __init__(self, current: str) -> None:
        super().__init__("it was renamed somewhere else")
        self.current = current


class TooMany(Exception):
    pass


@dataclass
class SavedRoute:
    id: int
    owner_id: int
    name: str
    waypoints: list[dict[str, Any]]
    options: dict[str, Any]
    summary: dict[str, Any]
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    token: str = ""
    share_link: bool = False
    share_users: list[int] = field(default_factory=list)

    def can_view(self, user_id: int) -> bool:
        """Its owner, anyone signed in when the link is on, or someone it was
        shared with by name. Never once it is deleted."""
        if self.deleted_at is not None:
            return False
        return user_id == self.owner_id or self.share_link or user_id in self.share_users


def _from_record(key: int, record: Record) -> SavedRoute:
    share = record.get("share") or {}
    return SavedRoute(
        id=int(key),
        owner_id=int(record["owner_id"]),
        name=record.get("name", ""),
        waypoints=record.get("waypoints", []),
        options=record.get("options", {}),
        summary=record.get("summary", {}),
        created_at=record.get("created_at", ""),
        updated_at=record.get("updated_at", record.get("created_at", "")),
        deleted_at=record.get("deleted_at"),
        token=record.get("token", ""),
        share_link=bool(share.get("link", False)),
        share_users=[int(user_id) for user_id in share.get("users", [])],
    )


def _with_token(store: Store, key: int, record: Record) -> Record:
    """A route saved before links had no token: it gets one the first time
    it is read."""
    if record.get("token"):
        return record

    def mutate(stored: Record) -> None:
        if not stored.get("token"):
            stored["token"] = url_token(TOKEN_BYTES)

    return store.update_value(BUCKET_SAVED_ROUTES, key, mutate) or record


def clean_name(name: str) -> str:
    return " ".join(name.split())[:NAME_MAX]


def create(store: Store, owner_id: int, name: str, waypoints: list[dict], options: dict, summary: dict) -> SavedRoute:
    if len(list_routes(store, owner_id)) >= MAX_PER_USER:
        raise TooMany(f"at most {MAX_PER_USER} saved routes")
    key = store.next_sequence(BUCKET_SAVED_ROUTES)
    now = to_iso(utc_now())
    record = {
        "owner_id": owner_id,
        "name": clean_name(name),
        "waypoints": waypoints,
        "options": options,
        "summary": summary,
        "created_at": now,
        "updated_at": now,
        "token": url_token(TOKEN_BYTES),
        "share": {"link": False, "users": []},
    }
    store.put(BUCKET_SAVED_ROUTES, key, record)
    return _from_record(key, record)


def _purge_expired(store: Store, owner_id: int) -> None:
    cutoff = utc_now() - RESTORE_WINDOW

    def expired(_key: Any, record: Record) -> bool:
        deleted = record.get("deleted_at")
        return record.get("owner_id") == owner_id and deleted is not None and from_iso(deleted) < cutoff

    store.delete_where(BUCKET_SAVED_ROUTES, expired)


def list_routes(store: Store, owner_id: int) -> list[SavedRoute]:
    """The owner's routes that are not deleted, newest change first."""
    _purge_expired(store, owner_id)
    rows = [_from_record(key, _with_token(store, key, record)) for key, record in store.all(BUCKET_SAVED_ROUTES)
            if record.get("owner_id") == owner_id and record.get("deleted_at") is None]
    return sorted(rows, key=lambda route: route.updated_at, reverse=True)


def get(store: Store, owner_id: int, route_id: int) -> SavedRoute:
    record = store.get(BUCKET_SAVED_ROUTES, route_id)
    if record is None or record.get("owner_id") != owner_id or record.get("deleted_at") is not None:
        raise NotFound("no such saved route")
    return _from_record(route_id, _with_token(store, route_id, record))


def _mutate(store: Store, owner_id: int, route_id: int, change, want_deleted: bool = False) -> SavedRoute:
    problem: list[Exception] = []

    def mutate(record: Record) -> None:
        if record.get("owner_id") != owner_id or (record.get("deleted_at") is not None) != want_deleted:
            problem.append(NotFound("no such saved route"))
            return
        try:
            change(record)
        except Exception as error:  # noqa: BLE001 -- handed back below
            problem.append(error)

    updated = store.update_value(BUCKET_SAVED_ROUTES, route_id, mutate)
    if updated is None:
        raise NotFound("no such saved route")
    if problem:
        raise problem[0]
    return _from_record(route_id, updated)


def rename(store: Store, owner_id: int, route_id: int, name: str, expect: str | None) -> SavedRoute:
    def change(record: Record) -> None:
        if expect is not None and record.get("name") != expect:
            raise RenameConflict(record.get("name", ""))
        record["name"] = clean_name(name)
        record["updated_at"] = to_iso(utc_now())

    return _mutate(store, owner_id, route_id, change)


def delete(store: Store, owner_id: int, route_id: int) -> SavedRoute:
    def change(record: Record) -> None:
        record["deleted_at"] = to_iso(utc_now())

    return _mutate(store, owner_id, route_id, change)


def restore(store: Store, owner_id: int, route_id: int) -> SavedRoute:
    def change(record: Record) -> None:
        if from_iso(record["deleted_at"]) < utc_now() - RESTORE_WINDOW:
            raise NotFound("too late to restore it")
        record.pop("deleted_at", None)

    return _mutate(store, owner_id, route_id, change, want_deleted=True)


# --- sharing -------------------------------------------------------------------

def set_share(store: Store, owner_id: int, route_id: int, link: bool, user_ids: list[int]) -> SavedRoute:
    """Who may view it besides its owner. user_ids should already be people
    who can sign in; the owner is left out."""
    people = list(dict.fromkeys(user_id for user_id in user_ids if user_id != owner_id))[:MAX_SHARED_USERS]

    def change(record: Record) -> None:
        if not record.get("token"):
            record["token"] = url_token(TOKEN_BYTES)
        record["share"] = {"link": bool(link), "users": people}

    return _mutate(store, owner_id, route_id, change)


def find_by_token(store: Store, token: str, viewer_id: int) -> SavedRoute:
    """The route behind a link, if viewer_id may see it. A route that exists
    but is not shared with them is the same NotFound as no route at all."""
    if token:
        for key, record in store.all(BUCKET_SAVED_ROUTES):
            stored = record.get("token", "")
            if stored and constant_time_equal(stored, token):
                route = _from_record(key, record)
                if route.can_view(viewer_id):
                    return route
                break
    raise NotFound("no such route, or it is not shared with you")


def shared_with(store: Store, user_id: int) -> list[SavedRoute]:
    """Routes other people shared with user_id by name, newest change first."""
    rows = []
    for key, record in store.all(BUCKET_SAVED_ROUTES):
        if record.get("owner_id") == user_id or record.get("deleted_at") is not None:
            continue
        if user_id in (record.get("share") or {}).get("users", []):
            rows.append(_from_record(key, _with_token(store, key, record)))
    return sorted(rows, key=lambda route: route.updated_at, reverse=True)


# --- planner defaults, on the user's record ---------------------------------

def get_prefs(store: Store, user_id: int) -> dict[str, Any] | None:
    record = store.get(BUCKET_USERS, user_id)
    return None if record is None else record.get("planner")


def set_prefs(store: Store, user_id: int, prefs: dict[str, Any]) -> bool:
    def mutate(record: Record) -> None:
        record["planner"] = prefs

    return store.update_value(BUCKET_USERS, user_id, mutate) is not None


# --- recent places, on the user's record --------------------------------------
#
# Places picked from search and the stops of saved or opened routes, newest
# first, so the stop boxes can offer them before (and without) a search. A
# place near one already kept (or with its name) takes that one's place.

MAX_PLACES = 30
PLACE_NAME_MAX = 200
SAME_PLACE_M = 50.0


def _apart_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    x = (b["lon"] - a["lon"]) * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
    return math.hypot(x, b["lat"] - a["lat"]) * 111195.0


def get_places(store: Store, user_id: int) -> list[dict[str, Any]]:
    record = store.get(BUCKET_USERS, user_id)
    return [] if record is None else list(record.get("recent_places") or [])


def merge_places(kept: list[dict[str, Any]], places: list[dict[str, Any]], now: str) -> list[dict[str, Any]]:
    """kept with places put in front (the first of places first), each
    replacing any kept one at the same spot or with the same name."""
    out = list(kept)
    for place in reversed(places):
        name = str(place.get("name", "")).strip()[:PLACE_NAME_MAX]
        if not name:
            continue
        item = {"name": name, "lat": float(place["lat"]), "lon": float(place["lon"]), "used_at": place.get("used_at") or now}
        out = [old for old in out if old["name"].casefold() != name.casefold() and _apart_m(old, item) >= SAME_PLACE_M]
        out.insert(0, item)
    return out[:MAX_PLACES]


def add_places(store: Store, user_id: int, places: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """None: no such user."""
    now = to_iso(utc_now())
    result: list[dict[str, Any]] = []

    def mutate(record: Record) -> None:
        nonlocal result
        result = merge_places(list(record.get("recent_places") or []), places, now)
        record["recent_places"] = result

    return result if store.update_value(BUCKET_USERS, user_id, mutate) is not None else None


def clear_places(store: Store, user_id: int) -> bool:
    def mutate(record: Record) -> None:
        record.pop("recent_places", None)

    return store.update_value(BUCKET_USERS, user_id, mutate) is not None


def import_places(store: Store, user_id: int, places: list[dict[str, Any]]) -> int:
    """Places from an account file, behind the ones already kept (which win
    where both have the same place). How many of the file's were added."""
    now = to_iso(utc_now())
    added = 0

    def mutate(record: Record) -> None:
        nonlocal added
        current = list(record.get("recent_places") or [])
        merged = merge_places(merge_places([], places, now), current, now)
        added = len(merged) - len(merge_places([], current, now))
        record["recent_places"] = merged

    store.update_value(BUCKET_USERS, user_id, mutate)
    return max(0, added)
