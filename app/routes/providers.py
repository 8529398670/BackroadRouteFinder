"""The owner's routing engines card on the Settings screen: which other
engines the planner can ask, and their API keys.

Keys are sealed in the database (app/models/provider_keys.py), never written
to config.yaml, and never sent back: the card sees only whether one is set and
its last four characters. Engines' addresses are ordinary settings
(config.yaml's providers section), edited on the same screen.
"""
from __future__ import annotations

import time
from typing import Annotated, ClassVar

from msgspec import Meta
from starlette.requests import Request

from app.apidocs import OWNER, PATH, NoInput, Reply, route
from app.apidocs import Request as RequestType
from app.core.logs import KIND_ADMIN
from app.models import provider_keys
from app.routes.helpers import Refused, get_config, get_store
from app.routes.logs import audit
from app.routes.planner import access_of
from app.routing.providers import ENGINES, ProviderError

EngineID = Annotated[str, PATH, Meta(pattern="^(" + "|".join(ENGINES) + ")$", description="The engine: " + ", ".join(ENGINES) + ".",
                                     extra={"error": "unknown engine"})]

# A short trip inside Ohio for the Test button: Columbus to Delaware.
TEST_POINTS = [(39.9612, -82.9988), (40.2987, -83.0680)]


class EngineInfo(Reply):
    api_name: ClassVar[str] = "EngineInfo"
    id: str
    name: str
    needs_key: bool
    key_set: bool
    key_hint: Annotated[str, Meta(description="The key's last four characters, while one is set.")]
    url_setting: Annotated[str, Meta(description="The providers.* setting holding its address, or empty when it is fixed.")]
    url: str
    usable: bool
    why_not: str
    signup_url: str
    free_tier: str
    backroads: str


class EnginesReply(Reply):
    engines: list[EngineInfo]


def _engines(request: Request) -> EnginesReply:
    access = access_of(request)
    out = []
    for engine_id, engine in ENGINES.items():
        key = access.keys.get(engine_id, "")
        usable = access.usable(engine_id)
        out.append(EngineInfo(
            id=engine_id, name=engine.name, needs_key=engine.needs_key, key_set=bool(key),
            key_hint=provider_keys.hint(key) if key else "",
            url_setting=f"providers.{engine.url_key}" if engine.url_key else "",
            url=access.urls.get(engine_id, ""), usable=usable, why_not="" if usable else access.why_not(engine_id),
            signup_url=engine.signup_url, free_tier=engine.free_tier, backroads=engine.backroads,
        ))
    return EnginesReply(engines=out)


@route(
    "GET /api/owner/providers", summary="Routing engines and their keys",
    tag="Owner", role=OWNER, feature="settings",
    description="Every other routing engine the planner can ask: whether it has a key (never the key "
    "itself), its address, and whether it can be asked now.",
)
def list_engines(request: Request, body: NoInput) -> EnginesReply:
    return _engines(request)


class SetKey(RequestType):
    engine: EngineID
    key: Annotated[str, Meta(min_length=8, max_length=provider_keys.MAX_KEY_LENGTH, pattern=r"^[\x21-\x7e]+$",
                             description="The API key, as the engine's dashboard shows it.",
                             extra={"error": "paste the key: 8-400 characters, no spaces"})]


@route(
    "POST /api/owner/providers/{engine}/key", summary="Set a routing engine's API key",
    tag="Owner", role=OWNER, feature="settings",
    description="Saves the key, sealed in the database. It replaces any key the engine had.",
)
def set_key(request: Request, body: SetKey) -> EnginesReply:
    provider_keys.set_key(get_store(request), get_config(request).secret_key, body.engine, body.key.strip())
    audit(request, KIND_ADMIN, "routing engine key set", {"engine": body.engine})
    return _engines(request)


class EngineOnly(RequestType):
    engine: EngineID


@route(
    "POST /api/owner/providers/{engine}/key/delete", summary="Remove a routing engine's API key",
    tag="Owner", role=OWNER, feature="settings", errors=(404,),
    description="Forgets the key; the planner stops offering the engine if it needs one.",
)
def delete_key(request: Request, body: EngineOnly) -> EnginesReply:
    if not provider_keys.delete_key(get_store(request), body.engine):
        raise Refused(404, "that engine has no key")
    audit(request, KIND_ADMIN, "routing engine key removed", {"engine": body.engine})
    return _engines(request)


class TestReply(Reply):
    ok: bool
    message: str
    milliseconds: int
    routes: int
    seconds: float
    meters: float


@route(
    "POST /api/owner/providers/{engine}/test", summary="Test a routing engine",
    tag="Owner", role=OWNER, feature="settings",
    description="Asks the engine for a short backroads route in Ohio (Columbus to Delaware) with its "
    "current key and address, and says whether it answered. Not cached, so it spends one request.",
)
def test_engine(request: Request, body: EngineOnly) -> TestReply:
    access = access_of(request)
    if not access.usable(body.engine):
        return TestReply(ok=False, message=access.why_not(body.engine), milliseconds=0, routes=0, seconds=0, meters=0)
    engine = ENGINES[body.engine]
    began = time.monotonic()
    try:
        found = engine.fetch(TEST_POINTS, True, access.keys.get(body.engine, ""), access.urls.get(body.engine, ""), access.timeout)
    except ProviderError as error:
        return TestReply(ok=False, message=str(error), milliseconds=int((time.monotonic() - began) * 1000), routes=0, seconds=0, meters=0)
    except Exception as error:  # noqa: BLE001 -- the card shows it
        return TestReply(ok=False, message=f"failed: {error}", milliseconds=int((time.monotonic() - began) * 1000), routes=0, seconds=0, meters=0)
    ms = int((time.monotonic() - began) * 1000)
    if not found:
        return TestReply(ok=False, message="no route found", milliseconds=ms, routes=0, seconds=0, meters=0)
    return TestReply(ok=True, message="answered", milliseconds=ms, routes=len(found), seconds=round(found[0].seconds, 1), meters=round(found[0].meters, 1))
