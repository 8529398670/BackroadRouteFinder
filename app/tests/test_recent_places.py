"""Recent places (card: save recent places from routes and search): kept on
the account, newest first, one per spot, and carried in the account file."""
from __future__ import annotations

import unittest

from app.models import saved_routes
from app.tests.route_support import RoutesTestCase

NOW = "2026-11-27T00:00:00Z"


class MergeTest(unittest.TestCase):
    def test_newest_first_and_one_per_spot(self) -> None:
        kept = saved_routes.merge_places([], [{"name": "Lake", "lat": 40.0, "lon": -83.0}], NOW)
        kept = saved_routes.merge_places(kept, [{"name": "Farm", "lat": 40.5, "lon": -83.5}], NOW)
        self.assertEqual([p["name"] for p in kept], ["Farm", "Lake"])
        # 20 m from the lake, under another name: it takes the lake's place.
        kept = saved_routes.merge_places(kept, [{"name": "Lake car park", "lat": 40.00018, "lon": -83.0}], NOW)
        self.assertEqual([p["name"] for p in kept], ["Lake car park", "Farm"])
        # The same name somewhere else replaces it too.
        kept = saved_routes.merge_places(kept, [{"name": "farm", "lat": 41.0, "lon": -84.0}], NOW)
        self.assertEqual([p["name"] for p in kept], ["farm", "Lake car park"])

    def test_several_keep_their_order_and_the_cap(self) -> None:
        many = [{"name": f"P{i}", "lat": 40 + i / 10, "lon": -83.0} for i in range(40)]
        kept = saved_routes.merge_places([], many, NOW)
        self.assertEqual(len(kept), saved_routes.MAX_PLACES)
        self.assertEqual(kept[0]["name"], "P0")
        self.assertEqual(kept[0]["used_at"], NOW)

    def test_blank_names_are_left_out(self) -> None:
        self.assertEqual(saved_routes.merge_places([], [{"name": "  ", "lat": 1.0, "lon": 1.0}], NOW), [])


class PlacesApiTest(RoutesTestCase):
    def send(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        return self.call(method, path, cookie=self.user_cookie, body={**body, "csrf_token": self.csrf(self.user_cookie)})

    def test_add_list_clear(self) -> None:
        status, reply = self.call("GET", "/api/planner/places", cookie=self.user_cookie)
        self.assertEqual((status, reply["places"]), (200, []))
        status, reply = self.send("POST", "/api/planner/places", {"places": [
            {"name": "Start", "lat": 40.0, "lon": -83.0}, {"name": "End", "lat": 40.2, "lon": -83.2}]})
        self.assertEqual(status, 200, reply)
        self.assertEqual([p["name"] for p in reply["places"]], ["Start", "End"])
        self.assertTrue(reply["places"][0]["used_at"])
        status, reply = self.call("GET", "/api/planner/places", cookie=self.user_cookie)
        self.assertEqual(len(reply["places"]), 2)
        # Someone else's list is their own.
        status, reply = self.call("GET", "/api/planner/places", cookie=self.owner_cookie)
        self.assertEqual(reply["places"], [])
        status, reply = self.send("POST", "/api/planner/places", {"places": [{"name": "", "lat": 1, "lon": 1}]})
        self.assertEqual(status, 400)
        status, reply = self.send("POST", "/api/planner/places/clear", {})
        self.assertEqual((status, reply["places"]), (200, []))
        self.assertEqual(saved_routes.get_places(self.store, self.user.id), [])

    def test_account_file_carries_them(self) -> None:
        saved_routes.add_places(self.store, self.owner.id, [{"name": "Lake", "lat": 40.0, "lon": -83.0}])
        saved_routes.add_places(self.store, self.user.id, [{"name": "Home", "lat": 39.0, "lon": -82.0}])
        status, data = self.call("GET", "/api/account/export", cookie=self.owner_cookie)
        self.assertEqual(status, 200)
        self.assertEqual([p["name"] for p in data["recent_places"]], ["Lake"])
        status, reply = self.send("POST", "/api/account/import", {"data": data})
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["places_added"], 1)
        # The ones already here stay in front.
        self.assertEqual([p["name"] for p in saved_routes.get_places(self.store, self.user.id)], ["Home", "Lake"])
        status, reply = self.send("POST", "/api/account/import", {"data": data})
        self.assertEqual(reply["places_added"], 0)
