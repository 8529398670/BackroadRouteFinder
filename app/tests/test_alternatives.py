"""Alternative routes, the corridor search and the on-disk map, on the small
grid map test_road_rules builds."""
from __future__ import annotations

import numpy as np

from app.routing import costs, grid, router, search
from app.tests.test_road_rules import MIDDLE, SIZE, GridCase, node


class AlternativesTest(GridCase):
    options = costs.Options(penalty=8.0, rules=costs.full_rules({"restricted": "use"}))

    def test_offers_distinct_routes_best_first(self) -> None:
        points = [node(0, 0), node(SIZE - 1, SIZE - 1)]
        result = router.plan(self.graph, points, self.options, compare_fastest=False, max_snap_m=200, alternatives=4)
        routes = result.routes
        self.assertGreater(len(routes), 1)
        self.assertEqual([r.rank for r in routes], list(range(len(routes))))
        scores = [r.summary.seconds + r.summary.extra_seconds for r in routes]
        self.assertEqual(scores[0], min(scores))
        lines = [tuple(tuple(map(tuple, run.coords)) for run in r.summary.runs) for r in routes]
        self.assertEqual(len(set(lines)), len(lines))
        for score in scores[1:]:
            self.assertLessEqual(score, scores[0] * router.ALT_MAX_STRETCH + 1)

    def test_one_alternative_is_the_plain_plan(self) -> None:
        points = [node(0, 0), node(SIZE - 1, SIZE - 1)]
        one = router.plan(self.graph, points, self.options, compare_fastest=True, max_snap_m=200)
        self.assertEqual([r.compare for r in one.routes], [False, True])

    def test_cached(self) -> None:
        points = [node(1, 0), node(1, SIZE - 1)]
        first = router.plan(self.graph, points, self.options, compare_fastest=False, max_snap_m=200, alternatives=3)
        again = router.plan(self.graph, points, self.options, compare_fastest=False, max_snap_m=200, alternatives=3)
        self.assertIs(first.routes[0].summary, again.routes[0].summary)


class CorridorTest(GridCase):
    def test_corridor_matches_whole_map(self) -> None:
        options = costs.Options(penalty=8.0)
        whole = search.Sub(self.graph, options, np.arange(self.graph.segment_count))
        pairs = [(node(0, 0), node(SIZE - 1, SIZE - 1)), (node(MIDDLE, 0), node(MIDDLE, SIZE - 1)), (node(2, 5), node(5, 1))]
        for a, b in pairs:
            snapped = router.snap_all(self.graph, [a, b], 200, options)
            near = search.Searcher(self.graph, options, snapped).leg(*snapped)
            far = search._leg(whole, snapped[0], snapped[1], np.inf)
            self.assertEqual(near[0], far[0])

    def test_grid_index_finds_every_segment(self) -> None:
        for s in range(self.graph.segment_count):
            g0 = int(self.graph.seg_g0[s])
            lat, lon = float(self.graph.geom_lat[g0]), float(self.graph.geom_lon[g0])
            self.assertIn(s, grid.segments_near(self.graph, lat, lon, 10))
