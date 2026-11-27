"""Nudged and locked routes (card: nudge routes before locking them in), on
the small grid map test_road_rules builds."""
from __future__ import annotations

from app.routes import planner
from app.routes.helpers import Refused
from app.routing import costs, match, router, sources
from app.routing.search import straight_m
from app.routing.summarize import join
from app.tests.test_road_rules import SIZE, GridCase, node

ACCESS = sources.Access(keys={}, urls={}, timeout=1.0, own_required=frozenset())


def line_of(summary) -> list[list[float]]:
    out: list[list[float]] = []
    for run in summary.runs:
        out.extend(run.coords if not out or out[-1] != run.coords[0] else run.coords[1:])
    return out


def near(line: list[list[float]], point: tuple[float, float]) -> float:
    return min(straight_m(p[0], p[1], point[0], point[1]) for p in line)


class ShapingTest(GridCase):
    options = costs.Options(penalty=8.0, rules=costs.full_rules({"restricted": "use"}))

    def plan(self, points, shape=()):
        snapped = router.snap_all(self.graph, points, 200, self.options)
        shape_snaps = [(leg, router.snap_all(self.graph, [at], 200, self.options)[0]) for leg, at in shape]
        stops = sources.with_shape(snapped, shape_snaps)
        return sources.plan(self.graph, [(s.lat, s.lon) for s in stops], self.options, ["ours"], 1, False, 200,
                            ACCESS, snapped=stops)

    def test_nudge_pulls_the_route_through_it(self) -> None:
        points = [node(0, 0), node(0, SIZE - 1)]
        plain = line_of(self.plan(points).routes[0].summary)
        pull = node(SIZE - 2, 3)
        self.assertGreater(near(plain, pull), 1000)
        nudged = self.plan(points, [(0, pull)])
        self.assertLess(near(line_of(nudged.routes[0].summary), pull), 30)
        self.assertEqual(len(nudged.snapped), 3)

    def test_nudges_go_after_their_leg_in_order(self) -> None:
        a, b, c = (router.snap_all(self.graph, [node(r, 0)], 200, self.options)[0] for r in (0, 2, 4))
        x, y = (router.snap_all(self.graph, [node(r, 6)], 200, self.options)[0] for r in (1, 3))
        self.assertEqual(sources.with_shape([a, b, c], [(1, x), (1, y), (0, y)]), [a, y, b, x, y, c])

    def test_bad_leg_and_too_many_points_are_refused(self) -> None:
        snap = router.snap_all(self.graph, [node(1, 1)], 200, self.options)[0]
        with self.assertRaises(Refused):
            planner._shaped([snap, snap], [(1, snap)])
        with self.assertRaises(Refused):
            planner._shaped([snap] * 20, [(0, snap)] * 6)

    def test_kept_ends_stay_exactly(self) -> None:
        points = [node(0, 0), node(SIZE - 1, SIZE - 1)]
        original = self.plan(points).routes[0].summary
        coords = line_of(original)
        start, end = len(coords) // 4, 3 * len(coords) // 4
        before, after, off_map = match.measure_parts(self.graph, [tuple(p) for p in coords], original.seconds,
                                                     costs.rating(self.graph, self.options), start, end)
        self.assertLess(off_map, 1.0)
        middle = self.plan([tuple(coords[start]), tuple(coords[end])], [(0, node(SIZE - 1, 0))])
        middle_summary = middle.routes[0].summary
        sources.splice(middle, before, after, off_map)
        whole = middle.routes[0].summary
        line = line_of(whole)
        self.assertEqual(line[:start + 1], coords[:start + 1])
        self.assertEqual(line[-(len(coords) - end):], coords[end:])
        self.assertAlmostEqual(whole.seconds, before.seconds + middle_summary.seconds + after.seconds, places=3)
        self.assertAlmostEqual(whole.meters, before.meters + middle_summary.meters + after.meters, places=3)

    def test_kept_parts_add_up_to_the_route(self) -> None:
        points = [node(0, 0), node(SIZE - 1, SIZE - 1)]
        original = self.plan(points).routes[0].summary
        coords = [tuple(p) for p in line_of(original)]
        rating = costs.rating(self.graph, self.options)
        before, after, _ = match.measure_parts(self.graph, coords, original.seconds, rating, 1, len(coords) - 2)
        whole, _ = match.measure(self.graph, coords, original.seconds, rating)
        self.assertLessEqual(before.seconds + after.seconds, whole.seconds)
        self.assertGreater(join(before, after).meters, 0)


class FollowTest(ShapingTest):
    """A line made by switching onto other routes at forks: driven as given."""

    def body(self, coords, **extra) -> planner.PlanRoute:
        trip = [planner.Point(lat=coords[0][0], lon=coords[0][1]), planner.Point(lat=coords[-1][0], lon=coords[-1][1])]
        return planner.PlanRoute(follow=planner.Follow(coords=[list(p) for p in coords], trip=trip), compare_fastest=False, **extra)

    def test_followed_line_is_kept_and_measured(self) -> None:
        points = [node(0, 0), node(SIZE - 1, SIZE - 1)]
        original = self.plan(points).routes[0].summary
        coords = line_of(original)
        result = planner._plan_followed(self.graph, self.body(coords), self.options, 200)
        self.assertEqual(len(result.routes), 1)
        followed = result.routes[0].summary
        self.assertLess(near(line_of(followed), tuple(coords[len(coords) // 2])), 5)
        self.assertAlmostEqual(followed.meters, original.meters, delta=max(25.0, original.meters * 0.01))
        self.assertGreater(followed.seconds, 0)
        self.assertEqual(len(result.snapped), 2)

    def test_followed_line_refuses_nudges_and_loops(self) -> None:
        coords = [list(node(0, 0)), list(node(0, 3))]
        with self.assertRaises(Refused):
            planner._plan_followed(self.graph, self.body(coords, shape=[planner.ShapePoint(lat=coords[0][0], lon=coords[0][1], leg=0)]),
                                   self.options, 200)
