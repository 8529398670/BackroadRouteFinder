"""Other routing engines' API keys (app/routing/providers), entered by the
owner on the Settings screen.

They are secrets, so they are not in config.yaml (which holds none, and may be
committed or shared). They are kept in one meta record, each sealed with
secret.key's "providers" subkey on top of the record's own encryption, and
re-sealed when the key rotates (app/models/sealed.py). No route ever sends one
back: the screens see only whether a key is set and its last four characters.
"""
from __future__ import annotations

from app.core import encryption
from app.core.db import BUCKET_META, Record, Store
from app.routing.providers import ENGINES

META_KEY = "provider_keys"
FIELD_PREFIX = "provider_key_"
MAX_KEY_LENGTH = 400

# Every engine's field, for sealed.py's rotation.
SEALED_META_FIELDS: dict[str, str] = {FIELD_PREFIX + engine_id: encryption.PURPOSE_PROVIDERS for engine_id in ENGINES}


def _subkey(secret_key: str) -> str:
    return encryption.subkey(secret_key, encryption.PURPOSE_PROVIDERS)


def get_all(store: Store, secret_key: str) -> dict[str, str]:
    """Engine id -> key, for every engine that has one."""
    record = store.get(BUCKET_META, META_KEY) or {}
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
