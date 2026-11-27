"""Road categories and the use / avoid / never rules, and loops, on a small
map built here: a 7 x 7 grid of streets about 550 m apart, whose middle row is
an Interstate (two-way, so a car can drive it either way) and whose top row
is a private road."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.routing import build, classes, costs, graph as graph_module, loop, router, search
from app.routing.osm_parse import Ways

SIZE = 7
LAT0, LON0 = 40.0, -83.0
DLAT, DLON = 0.005, 0.0065
MIDDLE = SIZE // 2


def node(r: int, c: int) -> tuple[float, float]:
    return LAT0 + r * DLAT, LON0 + c * DLON


def grid_ways() -> Ways:
    lines: list[tuple[list[tuple[int, int]], str, str, bool]] = []  # cells, highway, ref, restricted
    for r in range(SIZE):
        highway, ref = ("trunk", "I 71") if r == MIDDLE else ("residential", "")
        lines.append(([(r, c) for c in range(SIZE)], highway, ref, r == SIZE - 1))
    for c in range(SIZE):
        lines.append(([(r, c) for r in range(SIZE)], "tertiary", "CR 5", False))
    refs, lat, lon, starts = [], [], [], [0]
    road_class, kmh, category, restricted = [], [], [], []
    for cells, highway, ref, private in lines:
        for r, c in cells:
            refs.append(r * 100 + c + 1)
            la, lo = node(r, c)
            lat.append(la)
            lon.append(lo)
        starts.append(len(refs))
        road_class.append(classes.CLASS_INDEX[highway])
        kmh.append(classes.DEFAULT_KMH[highway])
        category.append(classes.CATEGORY_INDEX[classes.category_of(highway, ref)])
        restricted.append(private)
    count = len(lines)
    return Ways(
        refs=np.array(refs, dtype=np.int64), lat=np.array(lat), lon=np.array(lon), starts=np.array(starts, dtype=np.int64),
        road_class=np.array(road_class, dtype=np.uint8), kmh=np.array(kmh, dtype=np.float32),
        oneway=np.zeros(count, dtype=np.int8), unpaved=np.zeros(count, dtype=bool),
        category=np.array(category, dtype=np.uint8), restricted=np.array(restricted, dtype=bool),
        name_id=np.zeros(count, dtype=np.int32), names=[""], signals=np.array([], dtype=np.int64),
        stops=np.array([], dtype=np.int64), way_ids=np.arange(1, count + 1, dtype=np.int64),
    )


class GridCase(unittest.TestCase):
    graph: graph_module.Graph

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "grid.graph"
            build.build(grid_ways(), path, "test", progress=lambda _line: None)
            cls.graph = graph_module.load(path)


class CategoryTest(unittest.TestCase):
    def test_refs(self) -> None:
        cases = [
            ("motorway", "I 71", "interstate"), ("trunk", "I-70", "interstate"), ("motorway", "US 33", "us_highway"),
            ("motorway", "", "interstate"), ("primary", "US 23;SR 15", "us_highway"), ("secondary", "SR 37", "state_route"),
            ("secondary", "OH 7", "state_route"), ("primary", "", "state_route"), ("tertiary", "CR 12", "county_road"),
            ("residential", "TR 123", "county_road"), ("tertiary", "", "county_road"), ("residential", "", "local"),
            ("service", "SR 1", "service"), ("track", "", "track"), ("tertiary", "C-870", "county_road"),
            ("residential", "TWP. 9", "county_road"), ("residential", "CTY RT 3", "county_road"),
            ("residential", "MR 4", "local"), ("primary", "WV 2", "state_route"),
        ]
        for highway, ref, expected in cases:
            self.assertEqual(classes.category_of(highway, ref), expected, (highway, ref))

    def test_old_labels(self) -> None:
        self.assertEqual(classes.ref_of_label("Main St (SR 7)"), "SR 7")
        self.assertEqual(classes.ref_of_label("US 23, SR 15"), "US 23, SR 15")
        self.assertEqual(classes.ref_of_label("NE 5th Ave"), "")

    def test_legacy_options(self) -> None:
        self.assertEqual(costs.rules_from_legacy({}), costs.DEFAULT_RULES)
        rules = costs.rules_from_legacy({"secondary_is_main": False, "allow_unpaved": True})
        self.assertEqual((rules["state_route"], rules["unpaved"], rules["track"]), ("use", "use", "use"))
        rules = costs.rules_from_legacy({"allow_unpaved": True, "road_rules": {"track": "never"}})
        self.assertEqual(rules["track"], "never")


class ConfigTest(unittest.TestCase):
    def test_road_types_match(self) -> None:
        from app.core import config
        self.assertEqual(config.ROAD_TYPES, classes.RULE_KEYS)
        self.assertEqual(tuple(costs.DEFAULT_RULES), classes.RULE_KEYS)

    def test_bad_values_are_refused(self) -> None:
        from app.core import config
        for raw in ("routing:\n  road_rules: [highway=avoid]\n", "routing:\n  road_rules: [interstate=sometimes]\n",
                    "routing:\n  avoid_strength: [unpaved=0.5]\n", "routing:\n  loop_points: 9\n", "routing:\n  loop_unit: km\n"):
            with self.assertRaises(config.ConfigError, msg=raw):
                config.parse(raw, "test")
        file = config.parse("routing:\n  avoid_strength: [unpaved=2, track=4.5]\n", "test")
        self.assertEqual(costs.parse_pairs(file.routing.avoid_strength, "x"), {"unpaved": "2", "track": "4.5"})

    def test_old_secondary_setting_still_reads(self) -> None:
        from app.core import config
        self.assertFalse(config.parse("routing:\n  secondary_is_main: false\n", "test").routing.secondary_is_main)


class RulesTest(GridCase):
    def plan(self, rules: dict[str, str], penalty: float = 1.0):
        options = costs.Options(penalty=penalty, rules=costs.full_rules(rules))
        points = [node(MIDDLE, 0), node(MIDDLE, SIZE - 1)]
        return router.plan(self.graph, points, options, compare_fastest=False, max_snap_m=200).routes[0].summary

    def test_categories_are_stored(self) -> None:
        interstate = classes.CATEGORY_INDEX["interstate"]
        self.assertTrue((self.graph.seg_category == interstate).any())
        self.assertTrue(self.graph.seg_restricted.any())

    def test_use_takes_the_interstate(self) -> None:
        summary = self.plan({"interstate": "use"})
        self.assertAlmostEqual(summary.main_meters, 0.0)
        self.assertLess(summary.meters, (SIZE - 1) * 560 + 50)

    def test_never_keeps_off_it(self) -> None:
        summary = self.plan({"interstate": "never"})
        self.assertLess(summary.main_meters, 1.0)
        self.assertGreater(summary.meters, (SIZE - 1) * 560 + 500)

    def test_avoid_counts_it_as_main(self) -> None:
        summary = self.plan({"interstate": "avoid"}, penalty=1.0)
        self.assertGreater(summary.main_meters, 3000)

    def test_own_strength(self) -> None:
        # Avoided, but at strength 1 it costs no more than using it.
        options = costs.Options(penalty=8.0, rules=costs.full_rules({"interstate": "avoid"}), factors={"interstate": 1.0})
        points = [node(MIDDLE, 0), node(MIDDLE, SIZE - 1)]
        summary = router.plan(self.graph, points, options, compare_fastest=False, max_snap_m=200).routes[0].summary
        self.assertGreater(summary.main_meters, 3000)
        self.assertAlmostEqual(summary.extra_seconds, 0.0)
        # The slider's strength keeps off it.
        self.assertLess(self.plan({"interstate": "avoid"}, penalty=8.0).main_meters, 1.0)

    def test_strength_and_score(self) -> None:
        options = costs.Options(penalty=8.0, rules=costs.full_rules({"interstate": "avoid", "restricted": "use"}), factors={"interstate": 3.0})
        factors = costs.segment_factors(self.graph, options)
        interstate = self.graph.seg_category == classes.CATEGORY_INDEX["interstate"]
        self.assertTrue(np.all(factors[interstate] == 3.0))
        self.assertTrue(np.all(factors[~interstate] == 1.0))
        # Backroads off: avoid counts nothing, whatever the strength.
        off = costs.Options(penalty=1.0, rules=options.rules, factors=options.factors)
        self.assertTrue(np.all(costs.segment_factors(self.graph, off) == 1.0))
        summary = self.plan({"interstate": "use"})
        rating = costs.rating(self.graph, options)
        from app.routing import sources, summarize
        pieces = [(int(s), 0.0, float(self.graph.seg_len[s])) for s in np.flatnonzero(interstate)[:2]]
        again = summarize.summarize(self.graph, pieces, rating)
        self.assertAlmostEqual(again.extra_seconds, 2.0 * again.seconds, places=3)
        self.assertAlmostEqual(sources.score(again), 3.0 * again.seconds, places=3)
        self.assertGreater(summary.meters, 0)

    def test_restricted(self) -> None:
        never = costs.Options(rules=costs.full_rules({"restricted": "never"}))
        use = costs.Options(rules=costs.full_rules({"restricted": "use"}))
        shut = costs.excluded_segments(self.graph, never)
        self.assertTrue(np.array_equal(shut & self.graph.seg_restricted, self.graph.seg_restricted.astype(bool)))
        self.assertFalse((costs.excluded_segments(self.graph, use) & self.graph.seg_restricted).any())

    def test_snap_prefers_allowed_roads(self) -> None:
        options = costs.Options(rules=costs.full_rules({"interstate": "never"}))
        on_corner = router.snap_all(self.graph, [node(MIDDLE, 0)], 200, options)[0]
        self.assertNotEqual(self.graph.seg_category[on_corner.segment], classes.CATEGORY_INDEX["interstate"])

    def test_reuse_discourages_driven_roads(self) -> None:
        options = costs.Options(penalty=1.0)
        sub = search.Sub(self.graph, options, np.arange(self.graph.segment_count))
        plain = sub.weight.copy()
        sub.scale(np.array([0, 1]), 3.0)
        self.assertAlmostEqual(sub.weight[0], plain[0] * 3)
        self.assertAlmostEqual(sub.weight[2], plain[2])


class LoopTest(GridCase):
    options = costs.Options(penalty=8.0, rules=costs.full_rules({"restricted": "use"}))

    def check_route(self, circle: loop.Loop) -> None:
        result = router.plan(self.graph, [], self.options, compare_fastest=False, max_snap_m=300,
                             snapped=circle.stops, reuse_factor=loop.REUSE_FACTOR)
        self.assertGreater(result.routes[0].summary.meters, 1000)

    def test_center_start(self) -> None:
        circle = loop.plan(self.graph, node(MIDDLE, MIDDLE), 0.8, "mi", "center", "cw", self.options, 300)
        self.assertGreaterEqual(len(circle.ring), loop.MIN_RING)
        self.assertEqual(circle.stops[0], circle.stops[-1])
        self.assertEqual(circle.stops[1], circle.stops[-2])
        self.check_route(circle)

    def test_ring_start(self) -> None:
        circle = loop.plan(self.graph, node(MIDDLE, MIDDLE), 0.8, "mi", "ring", "ccw", self.options, 300)
        self.assertEqual(circle.stops[0], circle.stops[-1])
        self.assertEqual(circle.stops[0], circle.ring[0])
        self.check_route(circle)

    def test_own_start(self) -> None:
        circle = loop.plan(self.graph, node(MIDDLE, MIDDLE), 0.8, "mi", "stop", "cw", self.options, 300, start_point=node(0, 0))
        self.assertEqual(circle.stops[0], circle.stops[-1])
        self.assertAlmostEqual(circle.stops[0].lat, node(0, 0)[0], places=4)
        self.check_route(circle)

    def test_minutes_ring_surrounds_the_center(self) -> None:
        circle = loop.plan(self.graph, node(MIDDLE, MIDDLE), 1.5, "min", "ring", "cw", self.options, 300)
        self.assertGreaterEqual(len(circle.ring), loop.MIN_RING)
        center_lat, center_lon = node(MIDDLE, MIDDLE)
        bearings = loop._bearing(center_lat, center_lon, np.array([p.lat for p in circle.ring]), np.array([p.lon for p in circle.ring]))
        self.assertGreater(np.ptp(bearings), 180)  # points on more than one side
        self.check_route(circle)

    def test_ring_edits(self) -> None:
        plain = loop.plan(self.graph, node(MIDDLE, MIDDLE), 0.8, "mi", "ring", "cw", self.options, 300, count=8)
        self.assertEqual(len(plain.sectors), len(plain.ring))
        dropped = plain.sectors[1]
        fewer = loop.plan(self.graph, node(MIDDLE, MIDDLE), 0.8, "mi", "ring", "cw", self.options, 300, count=8, edits={dropped: None})
        self.assertNotIn(dropped, fewer.sectors)
        self.assertEqual(len(fewer.ring), len(plain.ring) - 1)
        self.check_route(fewer)
        # Moved onto the center: that sector's point is there now.
        moved = loop.plan(self.graph, node(MIDDLE, MIDDLE), 0.8, "mi", "ring", "cw", self.options, 300, count=8,
                          edits={dropped: node(MIDDLE, MIDDLE)})
        at = moved.ring[moved.sectors.index(dropped)]
        self.assertAlmostEqual(at.lat, node(MIDDLE, MIDDLE)[0], places=3)
        self.check_route(moved)
        # Too few left: refused with a way out.
        with self.assertRaises(search.NoRoute):
            loop.plan(self.graph, node(MIDDLE, MIDDLE), 0.8, "mi", "ring", "cw", self.options, 300, count=8,
                      edits={k: None for k in plain.sectors[:-2]})

    def test_too_small_for_a_loop(self) -> None:
        with self.assertRaises(search.NoRoute):
            loop.plan(self.graph, node(MIDDLE, MIDDLE), 0.01, "mi", "ring", "cw", self.options, 300)


if __name__ == "__main__":
    unittest.main()
