"""Sharing a saved route (card: share a route within the app, as a view-only
link or with other people): its link, who may view it, "Shared with me"."""
from __future__ import annotations

from app.core.db import BUCKET_SAVED_ROUTES
from app.models import saved_routes, users
from app.tests.route_support import RoutesTestCase

POINTS = [{"lat": 40.0, "lon": -83.0, "label": "A"}, {"lat": 40.2, "lon": -83.2, "label": "B"}]


class SharedRoutesTest(RoutesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.route = saved_routes.create(self.store, self.owner.id, "To the lake", POINTS, {"backroads": True}, {"seconds": 60})
        self.third = users.create_user(self.store, "Third", users.ROLE_USER)
        self.third_cookie = self.session(self.third.id)

    def share(self, body: dict, cookie: str = "", route_id: int = 0) -> tuple[int, dict]:
        cookie = cookie or self.owner_cookie
        return self.call("POST", f"/api/saved-routes/{route_id or self.route.id}/share", cookie=cookie,
                         body={**body, "csrf_token": self.csrf(cookie)})

    def by_link(self, cookie: str) -> tuple[int, dict]:
        return self.call("GET", f"/api/shared-routes/{self.route.token}", cookie=cookie)

    def test_new_routes_have_a_token_and_start_private(self) -> None:
        self.assertGreaterEqual(len(self.route.token), 16)
        status, reply = self.call("GET", f"/api/saved-routes/{self.route.id}", cookie=self.owner_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(reply["token"], self.route.token)
        self.assertEqual(reply["share"], {"link": False, "users": []})
        self.assertEqual(self.by_link(self.user_cookie)[0], 404)
        status, reply = self.by_link(self.owner_cookie)
        self.assertEqual((status, reply["mine"], reply["owner_name"]), (200, True, "Owner"))

    def test_link_lets_anyone_signed_in_view(self) -> None:
        status, reply = self.share({"link": True})
        self.assertEqual((status, reply["share"]["link"]), (200, True), reply)
        for cookie in (self.user_cookie, self.third_cookie):
            status, reply = self.by_link(cookie)
            self.assertEqual(status, 200, reply)
            self.assertFalse(reply["mine"])
            self.assertEqual(reply["route"]["name"], "To the lake")
            self.assertIsNone(reply["route"]["share"])  # only the owner is told who else sees it
        # Not signed in: nothing.
        self.assertEqual(self.by_link("")[0], 401)
        # A link only: not listed under "Shared with me".
        self.assertEqual(self.call("GET", "/api/shared-routes", cookie=self.user_cookie)[1]["routes"], [])
        # Private again.
        self.share({"link": False})
        self.assertEqual(self.by_link(self.user_cookie)[0], 404)

    def test_shared_by_name(self) -> None:
        status, reply = self.share({"users": [self.user.id, self.owner.id, 9999]})
        self.assertEqual(reply["share"]["users"], [self.user.id])  # not yourself, not nobody
        self.assertEqual(self.by_link(self.user_cookie)[0], 200)
        self.assertEqual(self.by_link(self.third_cookie)[0], 404)
        status, reply = self.call("GET", "/api/shared-routes", cookie=self.user_cookie)
        self.assertEqual(status, 200)
        self.assertEqual([(r["route"]["id"], r["owner_name"]) for r in reply["routes"]], [(self.route.id, "Owner")])
        self.assertEqual(self.call("GET", "/api/shared-routes", cookie=self.third_cookie)[1]["routes"], [])

    def test_only_the_owner_shares(self) -> None:
        status, _ = self.share({"link": True}, cookie=self.user_cookie)
        self.assertEqual(status, 404)
        self.assertEqual(self.by_link(self.user_cookie)[0], 404)

    def test_viewer_role_can_view_not_share(self) -> None:
        viewer = users.create_user(self.store, "Viewer", users.ROLE_VIEWER)
        cookie = self.session(viewer.id)
        self.share({"users": [viewer.id]})
        self.assertEqual(self.by_link(cookie)[0], 200)
        mine = saved_routes.create(self.store, viewer.id, "Mine", POINTS, {}, {})
        status, _ = self.share({"link": True}, cookie=cookie, route_id=mine.id)
        self.assertEqual(status, 403)

    def test_deleted_route_link_is_gone(self) -> None:
        self.share({"link": True})
        saved_routes.delete(self.store, self.owner.id, self.route.id)
        self.assertEqual(self.by_link(self.user_cookie)[0], 404)
        self.assertEqual(self.by_link(self.owner_cookie)[0], 404)

    def test_old_route_gets_a_token_when_read(self) -> None:
        def strip(record: dict) -> None:
            record.pop("token")
            record.pop("share")

        self.store.update_value(BUCKET_SAVED_ROUTES, self.route.id, strip)
        status, reply = self.call("GET", f"/api/saved-routes/{self.route.id}", cookie=self.owner_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(reply["token"])
        self.assertEqual(self.store.get(BUCKET_SAVED_ROUTES, self.route.id)["token"], reply["token"])

    def test_link_page_is_the_app(self) -> None:
        response = self.client.get(f"/r/{self.route.token}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
