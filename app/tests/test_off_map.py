"""Countries on the Map data screen, and a trip off our map planned by the
other engines alone (PlanRoute.off_map), on the small grid map
test_road_rules builds."""
from __future__ import annotations

import unittest
from unittest import mock

from app.routing import costs, regions, router, sources
from app.routing.providers import ENGINES, base
from app.routing.search import NoRoute
from app.tests.test_road_rules import SIZE, GridCase, node

ACCESS = sources.Access(keys={}, urls={"valhalla": "https://valhalla.test"}, timeout=1.0, own_required=frozenset())
FAR = (31.95, 35.93)   # Amman: nowhere near the test grid


class CountriesTest(unittest.TestCase):
    def test_countries_download_from_their_continent(self) -> None:
        self.assertTrue(regions.valid("jordan"))
        self.assertEqual(regions.name_of("jordan"), "Jordan")
        self.assertEqual(regions.url_of("jordan"), "https://download.geofabrik.de/asia/jordan/jordan-latest.osm.pbf")

    def test_a_country_never_shares_a_states_slug(self) -> None:
        self.assertFalse(set(regions.COUNTRIES) & set(regions.STATES))
        self.assertTrue(regions.url_of("georgia").endswith("/north-america/us/georgia-latest.osm.pbf"))
        self.assertTrue(regions.url_of("georgia-country").endswith("/europe/georgia/georgia-latest.osm.pbf"))

    def test_the_whole_us_keeps_countries_beside_it(self) -> None:
        self.assertEqual(regions.normalized(["ohio", "us", "jordan"]), ["jordan", "us"])
        self.assertEqual(regions.region_name(["ohio", "us", "jordan"]), "jordan_us")
        self.assertEqual(regions.region_name(["jordan", "ohio"]), "jordan_ohio")

    def test_every_country_has_a_group_and_a_size(self) -> None:
        for country in regions.COUNTRIES.values():
            self.assertIn(country.group, regions.GROUPS, country.slug)
            self.assertGreater(country.mb, 0, country.slug)


class OffMapTest(GridCase):
    options = costs.Options(penalty=8.0, rules=costs.full_rules({"restricted": "use"}))

    def fake_fetch(self, coords):
        def fetch(engine, points, ask, key, url, timeout):
            self.asked = points
            return [base.Candidate(coords=coords, seconds=600.0, meters=9000.0)]
        return fetch

    def test_a_far_point_is_off_our_map(self) -> None:
        with self.assertRaises(router.PointOffNetwork):
            router.snap_all(self.graph, [node(0, 0), FAR], 200, self.options)

    def test_the_other_engines_plan_it_through_the_points_as_given(self) -> None:
        line = [node(0, 0), node(0, SIZE - 1), FAR]
        points = [sources.Spot(*node(0, 0)), sources.Spot(*FAR)]
        with mock.patch.object(base, "cached_fetch", self.fake_fetch(line)):
            plan = sources.plan_off_map(self.graph, points, self.options, ["valhalla"], 3, ACCESS)
        self.assertEqual(self.asked, [node(0, 0), FAR])
        self.assertEqual(len(plan.routes), 1)
        route = plan.routes[0]
        self.assertEqual(route.source, "valhalla")
        self.assertEqual(route.summary.seconds, 600.0)
        # The part on the grid is on our map; the long leg to Amman is not.
        self.assertGreater(route.off_map_meters, 100_000)
        self.assertEqual(plan.snapped, points)

    def test_ours_alone_is_not_enough(self) -> None:
        with self.assertRaises(NoRoute):
            sources.plan_off_map(self.graph, [sources.Spot(*FAR), sources.Spot(*FAR)], self.options, ["ours"], 1, ACCESS)

    def test_default_engines_are_the_usable_ones(self) -> None:
        picked = sources.default_engines(ACCESS)
        self.assertIn("valhalla", picked)
        self.assertTrue(all(ACCESS.usable(e) for e in picked))
        self.assertNotIn("google", picked)   # needs a key nobody gave
        self.assertTrue(set(picked) <= set(ENGINES))


if __name__ == "__main__":
    unittest.main()
