"""Downloading all of one person's data and loading it into another account."""
from __future__ import annotations

from app.models import saved_routes
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
