"""Downloading all of one person's data and loading it into another account."""
from __future__ import annotations

from app.models import provider_keys, saved_routes
from app.tests.route_support import RoutesTestCase


class AccountDataTest(RoutesTestCase):
    def post(self, cookie: str, path: str, body: dict) -> tuple[int, dict]:
        return self.call("POST", path, cookie=cookie, body={**body, "csrf_token": self.csrf(cookie)})

    def test_round_trip(self) -> None:
        options = {"backroads": True, "penalty": 8.0, "road_rules": {}, "sources": ["ours"], "combine": "best"}
        saved_routes.create(self.store, self.owner.id, "To the lake", [{"lat": 40.0, "lon": -83.0, "label": "A"},
                                                                       {"lat": 40.1, "lon": -83.1, "label": "B"}],
                            options, {"seconds": 600, "meters": 9000, "main_seconds": 0, "main_meters": 0})
        saved_routes.set_prefs(self.store, self.owner.id, {"backroads": False, "penalty": 12.0})
        status, data = self.call("GET", "/api/account/export", cookie=self.owner_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["format"], "backroad-route-finder/1")
        self.assertEqual(len(data["saved_routes"]), 1)
        self.assertEqual(data["planner_prefs"]["penalty"], 12.0)
        self.assertNotIn("keys", data)

        status, reply = self.post(self.user_cookie, "/api/account/import", {"data": data})
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["routes_added"], 1)
        self.assertTrue(reply["prefs_replaced"])
        mine = saved_routes.list_routes(self.store, self.user.id)
        self.assertEqual([r.name for r in mine], ["To the lake"])
        self.assertEqual(saved_routes.get_prefs(self.store, self.user.id)["penalty"], 12.0)

        # The same file again adds nothing.
        status, reply = self.post(self.user_cookie, "/api/account/import", {"data": data})
        self.assertEqual((reply["routes_added"], reply["routes_skipped"]), (0, 1))
        self.assertEqual(len(saved_routes.list_routes(self.store, self.user.id)), 1)

    def test_wrong_format(self) -> None:
        status, _ = self.post(self.user_cookie, "/api/account/import", {"data": {"format": "something-else"}})
        self.assertEqual(status, 400)


class ProviderKeysTransferTest(RoutesTestCase):
    """Routing engine keys: the owner's shared ones live in config.yaml, and an
    account file carries keys across."""

    def post(self, cookie: str, path: str, body: dict) -> tuple[int, dict]:
        return self.call("POST", path, cookie=cookie, body={**body, "csrf_token": self.csrf(cookie)})

    def test_shared_key_is_saved_in_config_yaml(self) -> None:
        status, reply = self.post(self.owner_cookie, "/api/owner/providers/graphhopper/key", {"key": "gh-shared-1234567"})
        self.assertEqual(status, 200, reply)
        self.assertIn('graphhopper: "gh-shared-1234567"', self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.holder.get().file.provider_keys.graphhopper, "gh-shared-1234567")
        cfg = self.holder.get()
        self.assertEqual(provider_keys.get_all(self.store, cfg.secret_key, cfg.file), {"graphhopper": "gh-shared-1234567"})
        # Not shown on the Settings screen.
        status, settings = self.call("GET", "/api/owner/settings", cookie=self.owner_cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("gh-shared-1234567", str(settings))

        status, reply = self.post(self.owner_cookie, "/api/owner/providers/graphhopper/key/delete", {})
        self.assertEqual(status, 200, reply)
        self.assertEqual(self.holder.get().file.provider_keys.graphhopper, "")

    def test_old_database_keys_move_to_config_yaml(self) -> None:
        cfg = self.holder.get()
        provider_keys.set_key(self.store, cfg.secret_key, "ors", "ors-old-key-123456")
        self.assertEqual(provider_keys.migrate_shared_to_config(self.store, cfg.secret_key, self.holder), ["ors"])
        self.assertEqual(self.holder.get().file.provider_keys.ors, "ors-old-key-123456")
        self.assertEqual(provider_keys._open_all(self.store.get("meta", provider_keys.META_KEY) or {}, cfg.secret_key), {})

    def test_export_and_import_carry_keys(self) -> None:
        cfg = self.holder.get()
        provider_keys.set_user_key(self.store, cfg.secret_key, self.owner.id, "tomtom", "tomtom-mine-123456")
        self.post(self.owner_cookie, "/api/owner/providers/here/key", {"key": "here-shared-123456"})
        status, data = self.call("GET", "/api/account/export", cookie=self.owner_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["api_keys"], {"tomtom": "tomtom-mine-123456"})
        self.assertEqual(data["shared_api_keys"], {"here": "here-shared-123456"})

        # Someone else loading the owner's file gets the personal keys, never the shared ones.
        status, reply = self.post(self.user_cookie, "/api/account/import", {"data": data})
        self.assertEqual(status, 200, reply)
        self.assertEqual((reply["api_keys_restored"], reply["shared_keys_restored"]), (1, 0))
        self.assertEqual(provider_keys.get_user_keys(self.store, cfg.secret_key, self.user.id), {"tomtom": "tomtom-mine-123456"})

        # The owner loading it on a fresh install restores the shared keys into config.yaml.
        self.post(self.owner_cookie, "/api/owner/providers/here/key/delete", {})
        status, reply = self.post(self.owner_cookie, "/api/account/import", {"data": data})
        self.assertEqual(reply["shared_keys_restored"], 1)
        self.assertEqual(self.holder.get().file.provider_keys.here, "here-shared-123456")

        status, data = self.call("GET", "/api/account/export", cookie=self.user_cookie)
        self.assertIsNone(data["shared_api_keys"])

    def test_a_bad_key_in_the_file_is_refused(self) -> None:
        data = {"format": "backroad-route-finder/1", "api_keys": {"tomtom": "has space"}}
        status, _ = self.post(self.user_cookie, "/api/account/import", {"data": data})
        self.assertEqual(status, 400)

    def test_owner_copies_own_keys_to_shared(self) -> None:
        cfg = self.holder.get()
        provider_keys.set_user_key(self.store, cfg.secret_key, self.owner.id, "graphhopper", "gh-personal-123456")
        provider_keys.set_user_key(self.store, cfg.secret_key, self.owner.id, "google", "google-personal-1234")
        status, reply = self.post(self.owner_cookie, "/api/owner/providers/copy-mine", {})
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["copied"], 2)
        keys = self.holder.get().file.provider_keys
        self.assertEqual((keys.graphhopper, keys.google), ("gh-personal-123456", "google-personal-1234"))
        status, _ = self.post(self.user_cookie, "/api/owner/providers/copy-mine", {})
        self.assertIn(status, (403, 404))
