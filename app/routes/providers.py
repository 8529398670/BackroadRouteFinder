"""The Settings screen's routing engines: which other engines the planner can
ask, and their API keys.

Everyone has their own keys (the "Routing engines and API keys" card). The
owner also sets shared keys for everyone, and per engine whether people may
use the shared key ("shared") or must bring their own ("own"): see
app/models/provider_keys.py.

Keys are sealed in the database, never written to config.yaml, and never sent
back: a card sees only whether one is set and its last four characters (the
shared keys' hint only on the owner's card). Engines' addresses are ordinary
settings (config.yaml's providers section).
"""
from __future__ import annotations

import time
from typing import Annotated, ClassVar, Literal

from msgspec import Meta
from starlette.requests import Request

from app.apidocs import OWNER, PATH, NoInput, Reply, route
from app.apidocs import Request as RequestType
from app.core.logs import KIND_ADMIN
from app.core.security import actor_from
from app.models import provider_keys
from app.routes.helpers import Refused, get_config, get_store
from app.routes.logs import audit
from app.routes.planner import access_of, engine_urls, is_owner
from app.routing import sources
from app.routing.providers import ENGINES, ProviderError

EngineID = Annotated[str, PATH, Meta(pattern="^(" + "|".join(ENGINES) + ")$", description="The engine: " + ", ".join(ENGINES) + ".",
                                     extra={"error": "unknown engine"})]
Policy = Annotated[Literal["shared", "own"], Meta(description="shared: people without their own key use the owner's. own: everyone but the owner brings their own.")]

# A short trip inside Ohio for the Test button: Columbus to Delaware.
TEST_POINTS = [(39.9612, -82.9988), (40.2987, -83.0680)]


class EngineInfo(Reply):
    api_name: ClassVar[str] = "EngineInfo"
    id: str
    name: str
    needs_key: bool
    key_set: Annotated[bool, Meta(description="Whether you have your own key for it.")]
    key_hint: Annotated[str, Meta(description="Your key's last four characters, while one is set.")]
    shared_key_set: Annotated[bool, Meta(description="Whether the owner has set a shared key for it.")]
    policy: Policy
    using: Annotated[str, Meta(description="Which key your plans use: yours, shared, or none.")]
    url_setting: Annotated[str, Meta(description="The providers.* setting holding its address, or empty when it is fixed.")]
    url: str
    usable: bool
    why_not: str
    signup_url: str
    free_tier: str
    backroads: str


class EnginesReply(Reply):
    engines: list[EngineInfo]


def _user_id(request: Request) -> int:
    actor = actor_from(request)
    if actor is None:
        raise Refused(401, "sign in first")
    return actor.user.id


def _engines(request: Request) -> EnginesReply:
    cfg = get_config(request)
    store = get_store(request)
    access = access_of(request)
    shared = provider_keys.get_all(store, cfg.secret_key)
    own = provider_keys.get_user_keys(store, cfg.secret_key, _user_id(request))
    policy = provider_keys.get_policy(store)
    _, origin = provider_keys.effective(shared, own, policy, is_owner(request))
    out = []
    for engine_id, engine in ENGINES.items():
        key = own.get(engine_id, "")
        usable = access.usable(engine_id)
        out.append(EngineInfo(
            id=engine_id, name=engine.name, needs_key=engine.needs_key, key_set=bool(key),
            key_hint=provider_keys.hint(key) if key else "",
            shared_key_set=bool(shared.get(engine_id)), policy=policy[engine_id],  # type: ignore[arg-type]
            using=origin.get(engine_id, "none") if engine.needs_key else "none",
            url_setting=f"providers.{engine.url_key}" if engine.url_key else "",
            url=access.urls.get(engine_id, ""), usable=usable, why_not="" if usable else access.why_not(engine_id),
            signup_url=engine.signup_url, free_tier=engine.free_tier, backroads=engine.backroads,
        ))
    return EnginesReply(engines=out)


@route(
    "GET /api/providers", summary="Routing engines and your keys",
    tag="Settings",
    description="Every other routing engine the planner can ask: whether you have your own key (never "
    "the key itself), whether the owner shares one, which your plans use, and whether it can be asked now.",
)
def list_engines(request: Request, body: NoInput) -> EnginesReply:
    return _engines(request)


class SetKey(RequestType):
    engine: EngineID
    key: Annotated[str, Meta(min_length=8, max_length=provider_keys.MAX_KEY_LENGTH, pattern=r"^[\x21-\x7e]+$",
                             description="The API key, as the engine's dashboard shows it.",
                             extra={"error": "paste the key: 8-400 characters, no spaces"})]


@route(
    "POST /api/providers/{engine}/key", summary="Set your key for a routing engine",
    tag="Settings",
    description="Saves your own key, sealed in the database. Your plans use it instead of any shared key.",
)
def set_key(request: Request, body: SetKey) -> EnginesReply:
    cfg = get_config(request)
    if not provider_keys.set_user_key(get_store(request), cfg.secret_key, _user_id(request), body.engine, body.key.strip()):
        raise Refused.internal("provider key: the signed-in account has no record")
    return _engines(request)


class EngineOnly(RequestType):
    engine: EngineID


@route(
    "POST /api/providers/{engine}/key/delete", summary="Remove your key for a routing engine",
    tag="Settings", errors=(404,),
    description="Forgets your key; your plans fall back to the shared key, where the owner allows it.",
)
def delete_key(request: Request, body: EngineOnly) -> EnginesReply:
    if not provider_keys.delete_user_key(get_store(request), _user_id(request), body.engine):
        raise Refused(404, "you have no key for that engine")
    return _engines(request)


class TestReply(Reply):
    ok: bool
    message: str
    milliseconds: int
    routes: int
    seconds: float
    meters: float


def _test(access: sources.Access, engine_id: str) -> TestReply:
    if not access.usable(engine_id):
        return TestReply(ok=False, message=access.why_not(engine_id), milliseconds=0, routes=0, seconds=0, meters=0)
    engine = ENGINES[engine_id]
    began = time.monotonic()
    try:
        found = engine.fetch(TEST_POINTS, True, access.keys.get(engine_id, ""), access.urls.get(engine_id, ""), access.timeout)
    except ProviderError as error:
        return TestReply(ok=False, message=str(error), milliseconds=int((time.monotonic() - began) * 1000), routes=0, seconds=0, meters=0)
    except Exception as error:  # noqa: BLE001 -- the card shows it
        return TestReply(ok=False, message=f"failed: {error}", milliseconds=int((time.monotonic() - began) * 1000), routes=0, seconds=0, meters=0)
    ms = int((time.monotonic() - began) * 1000)
    if not found:
        return TestReply(ok=False, message="no route found", milliseconds=ms, routes=0, seconds=0, meters=0)
    return TestReply(ok=True, message="answered", milliseconds=ms, routes=len(found), seconds=round(found[0].seconds, 1), meters=round(found[0].meters, 1))


@route(
    "POST /api/providers/{engine}/test", summary="Test a routing engine",
    tag="Settings",
    description="Asks the engine for a short backroads route in Ohio (Columbus to Delaware) with the key "
    "your plans would use, and says whether it answered. Not cached, so it spends one request.",
)
def test_engine(request: Request, body: EngineOnly) -> TestReply:
    return _test(access_of(request), body.engine)


# --- the owner's shared keys and policy --------------------------------------

class SharedEngine(Reply):
    api_name: ClassVar[str] = "SharedEngine"
    id: str
    name: str
    needs_key: bool
    key_set: Annotated[bool, Meta(description="Whether a shared key is set.")]
    key_hint: Annotated[str, Meta(description="The shared key's last four characters, while one is set.")]
    policy: Policy
    signup_url: str


class SharedReply(Reply):
    engines: list[SharedEngine]


def _shared(request: Request) -> SharedReply:
    store = get_store(request)
    shared = provider_keys.get_all(store, get_config(request).secret_key)
    policy = provider_keys.get_policy(store)
    return SharedReply(engines=[
        SharedEngine(id=engine_id, name=engine.name, needs_key=engine.needs_key, key_set=bool(shared.get(engine_id)),
                     key_hint=provider_keys.hint(shared[engine_id]) if shared.get(engine_id) else "",
                     policy=policy[engine_id], signup_url=engine.signup_url)  # type: ignore[arg-type]
        for engine_id, engine in ENGINES.items() if engine.needs_key
    ])


@route(
    "GET /api/owner/providers", summary="Shared routing engine keys",
    tag="Owner", role=OWNER, feature="settings",
    description="The keys the owner shares with everyone (never the keys themselves), and per engine "
    "whether people may use them or must bring their own.",
)
def list_shared(request: Request, body: NoInput) -> SharedReply:
    return _shared(request)


@route(
    "POST /api/owner/providers/{engine}/key", summary="Set a shared routing engine key",
    tag="Owner", role=OWNER, feature="settings",
    description="Saves the shared key, sealed in the database. It replaces any shared key the engine had.",
)
def set_shared_key(request: Request, body: SetKey) -> SharedReply:
    provider_keys.set_key(get_store(request), get_config(request).secret_key, body.engine, body.key.strip())
    audit(request, KIND_ADMIN, "shared routing engine key set", {"engine": body.engine})
    return _shared(request)


@route(
    "POST /api/owner/providers/{engine}/key/delete", summary="Remove a shared routing engine key",
    tag="Owner", role=OWNER, feature="settings", errors=(404,),
    description="Forgets the shared key; people without their own key stop being offered the engine.",
)
def delete_shared_key(request: Request, body: EngineOnly) -> SharedReply:
    if not provider_keys.delete_key(get_store(request), body.engine):
        raise Refused(404, "that engine has no shared key")
    audit(request, KIND_ADMIN, "shared routing engine key removed", {"engine": body.engine})
    return _shared(request)


class SetPolicy(RequestType):
    engines: Annotated[dict[str, Policy], Meta(description="Engine id -> shared or own. Engines left out keep theirs.",
                                               extra={"error": "give engine ids and shared or own"})]


@route(
    "POST /api/owner/providers/policy", summary="Who may use the shared keys",
    tag="Owner", role=OWNER, feature="settings", errors=(400,),
    description="Per engine: shared lets people without their own key use the owner's; own makes everyone "
    "but the owner bring their own key.",
)
def set_policy(request: Request, body: SetPolicy) -> SharedReply:
    unknown = [engine_id for engine_id in body.engines if engine_id not in ENGINES]
    if unknown:
        raise Refused(400, f"unknown engine: {', '.join(unknown)}", field="engines")
    changed = provider_keys.set_policy(get_store(request), dict(body.engines))
    audit(request, KIND_ADMIN, "routing engine key policy changed", {"policy": changed})
    return _shared(request)


@route(
    "POST /api/owner/providers/{engine}/test", summary="Test a shared routing engine key",
    tag="Owner", role=OWNER, feature="settings",
    description="Like POST /api/providers/{engine}/test, but always with the shared key.",
)
def test_shared(request: Request, body: EngineOnly) -> TestReply:
    cfg = get_config(request)
    access = sources.Access(keys=provider_keys.get_all(get_store(request), cfg.secret_key), urls=engine_urls(request),
                            timeout=float(cfg.file.providers.timeout_s))
    if ENGINES[body.engine].needs_key and not access.keys.get(body.engine):
        return TestReply(ok=False, message="no shared key set", milliseconds=0, routes=0, seconds=0, meters=0)
    return _test(access, body.engine)
