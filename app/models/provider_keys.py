"""Other routing engines' API keys (app/routing/providers).

Two kinds, both entered on the Settings screen:
- shared keys, set by the owner for everyone (one meta record);
- each person's own keys, on their user record.

Which one a plan uses is the owner's policy, per engine: "shared" lets
everyone use the owner's key when they have none of their own, "own" makes
everyone (but the owner) bring their own. Someone's own key always wins over
the shared one, so their plans spend their own quota.

Where they are kept:
- the shared keys are in config.yaml's provider_keys section, so a restart,
  a fresh install from a copied config.yaml or a restored backup has them
  with nothing to set up again (config.yaml is never committed; the Settings
  screen never shows that section). A shared key saved before that moved, or
  saved while config.yaml could not be written, sits in one meta record,
  sealed; config.yaml's value wins where both have one, and
  migrate_shared_to_config moves the record's keys over at startup;
- each person's own keys stay on their user record, sealed with secret.key's
  "providers" subkey on top of the record's own encryption and re-sealed when
  the key rotates (app/models/sealed.py).
No route ever sends a key back, except a person's own account export (and
the owner's, which carries the shared keys too): the screens see only whether
a key is set and its last four characters.
"""
from __future__ import annotations

from typing import Any

from app.core import encryption
from app.core.db import BUCKET_META, BUCKET_USERS, Record, Store
from app.routing.providers import ENGINES

META_KEY = "provider_keys"
FIELD_PREFIX = "provider_key_"
MAX_KEY_LENGTH = 400

POLICY_KEY = "provider_key_policy"
SHARED = "shared"
OWN = "own"
POLICIES = (SHARED, OWN)

# Every engine's field, for sealed.py's rotation: the shared keys' meta record
# and every user record use the same field names.
SEALED_META_FIELDS: dict[str, str] = {FIELD_PREFIX + engine_id: encryption.PURPOSE_PROVIDERS for engine_id in ENGINES}
SEALED_USER_FIELDS: dict[str, str] = dict(SEALED_META_FIELDS)


def _subkey(secret_key: str) -> str:
    return encryption.subkey(secret_key, encryption.PURPOSE_PROVIDERS)


def _open_all(record: Record, secret_key: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for engine_id in ENGINES:
        sealed = record.get(FIELD_PREFIX + engine_id, "")
        if not sealed:
            continue
        try:
            out[engine_id] = encryption.xchacha_decrypt_string(_subkey(secret_key), sealed)
        except encryption.CipherTextError:
            continue
    return out


def config_keys(file: Any) -> dict[str, str]:
    """The shared keys config.yaml holds (a config.File's provider_keys):
    engine id -> key, for every engine that has one."""
    section = getattr(file, "provider_keys", None)
    if section is None:
        return {}
    return {engine_id: getattr(section, engine_id, "") for engine_id in ENGINES if getattr(section, engine_id, "")}


def get_all(store: Store, secret_key: str, file: Any = None) -> dict[str, str]:
    """The shared keys: engine id -> key, for every engine that has one.
    config.yaml's (file.provider_keys) win over any left in the database."""
    keys = _open_all(store.get(BUCKET_META, META_KEY) or {}, secret_key)
    keys.update(config_keys(file))
    return keys


def migrate_shared_to_config(store: Store, secret_key: str, holder: Any) -> list[str]:
    """Move shared keys still in the database into config.yaml, once, at
    startup. Returns the engines moved; an engine config.yaml already has a
    key for keeps config.yaml's and just loses the old record. Nothing moves
    when config.yaml cannot be written; the record keeps working."""
    stored = _open_all(store.get(BUCKET_META, META_KEY) or {}, secret_key)
    if not stored:
        return []
    present = config_keys(holder.get().file)
    moving = {f"provider_keys.{engine_id}": key for engine_id, key in stored.items() if engine_id not in present}
    if moving:
        holder.set_secret_values(moving)
    for engine_id in stored:
        delete_key(store, engine_id)
    return sorted(engine_id for engine_id in stored if engine_id not in present)


def get_user_keys(store: Store, secret_key: str, user_id: int) -> dict[str, str]:
    """One person's own keys: engine id -> key."""
    return _open_all(store.get(BUCKET_USERS, user_id) or {}, secret_key)


def set_user_key(store: Store, secret_key: str, user_id: int, engine_id: str, key: str) -> bool:
    sealed = encryption.xchacha_encrypt_string(_subkey(secret_key), key)

    def mutate(record: Record) -> None:
        record[FIELD_PREFIX + engine_id] = sealed

    return store.update_value(BUCKET_USERS, user_id, mutate) is not None


def delete_user_key(store: Store, user_id: int, engine_id: str) -> bool:
    found = False

    def mutate(record: Record) -> None:
        nonlocal found
        found = record.pop(FIELD_PREFIX + engine_id, None) is not None

    store.update_value(BUCKET_USERS, user_id, mutate)
    return found


def get_policy(store: Store) -> dict[str, str]:
    """Engine id -> "shared" or "own", for every engine (shared by default)."""
    record = store.get(BUCKET_META, POLICY_KEY) or {}
    engines = record.get("engines") or {}
    return {engine_id: engines.get(engine_id) if engines.get(engine_id) in POLICIES else SHARED for engine_id in ENGINES}


def set_policy(store: Store, policy: dict[str, str]) -> dict[str, str]:
    merged = get_policy(store)
    merged.update({engine_id: mode for engine_id, mode in policy.items() if engine_id in ENGINES and mode in POLICIES})

    def mutate(record: Record) -> None:
        record["engines"] = merged

    if store.update_value(BUCKET_META, POLICY_KEY, mutate) is None:
        store.put(BUCKET_META, POLICY_KEY, {"engines": merged})
    return merged


def effective(shared: dict[str, str], own: dict[str, str], policy: dict[str, str], is_owner: bool) -> tuple[dict[str, str], dict[str, str]]:
    """The key each engine is asked with for one person, and where it came
    from ("yours" or "shared"). The owner always has the shared keys."""
    keys: dict[str, str] = {}
    origin: dict[str, str] = {}
    for engine_id in ENGINES:
        if own.get(engine_id):
            keys[engine_id], origin[engine_id] = own[engine_id], "yours"
        elif shared.get(engine_id) and (is_owner or policy.get(engine_id, SHARED) == SHARED):
            keys[engine_id], origin[engine_id] = shared[engine_id], "shared"
    return keys, origin


def hint(key: str) -> str:
    """What the screen may show of a key: its last four characters."""
    return "…" + key[-4:] if len(key) >= 8 else "set"


def set_key(store: Store, secret_key: str, engine_id: str, key: str) -> None:
    sealed = encryption.xchacha_encrypt_string(_subkey(secret_key), key)

    def mutate(record: Record) -> None:
        record[FIELD_PREFIX + engine_id] = sealed

    if store.update_value(BUCKET_META, META_KEY, mutate) is None:
        store.put(BUCKET_META, META_KEY, {FIELD_PREFIX + engine_id: sealed})


def delete_key(store: Store, engine_id: str) -> bool:
    found = False

    def mutate(record: Record) -> None:
        nonlocal found
        found = record.pop(FIELD_PREFIX + engine_id, None) is not None

    store.update_value(BUCKET_META, META_KEY, mutate)
    return found
