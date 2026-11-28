"""Spurs: a route that drives down a dead end to a soft point (a nudge, a
loop's ring point) and straight back. On a small map built here: a main road
west to east (a state route at 60 km/h), a service road spur north from it
that ends nowhere, and a service road bypass beside it further east."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from app.routing import build, classes, costs, graph as graph_module, router, search, snap as snap_module, sources
from app.routing.osm_parse import Ways

LAT0, LON0 = 40.0, -83.0
DLON = 0.0065          # about 550 m between the main road's intersections
MAIN_NODES = 9
SPUR_AT = 2            # the spur leaves the main road at its node 2
SPUR_DLAT = 0.003      # and runs about 330 m north
BYPASS = (5, 7)        # the bypass leaves at node 5 and comes back at node 7
BYPASS_DLAT = 0.0004   # about 45 m north of the main road
MAIN_KMH = 60.0


def main_node(c: int) -> tuple[float, float]:
    return LAT0, LON0 + c * DLON


SPUR_END = (LAT0 + SPUR_DLAT, LON0 + SPUR_AT * DLON)


def spur_ways() -> Ways:
    # (points as (ref, lat, lon), highway, kmh)
    main = [(c + 1, *main_node(c)) for c in range(MAIN_NODES)]
    spur = [(SPUR_AT + 1, *main_node(SPUR_AT)), (100, *SPUR_END)]
    lo, hi = BYPASS
    bypass = [(lo + 1, *main_node(lo)),
              (200, LAT0 + BYPASS_DLAT, LON0 + lo * DLON + 0.0005),
              (201, LAT0 + BYPASS_DLAT, LON0 + hi * DLON - 0.0005),
              (hi + 1, *main_node(hi))]
    lines = [(main, "primary", MAIN_KMH), (spur, "service", classes.DEFAULT_KMH["service"]),
             (bypass, "service", classes.DEFAULT_KMH["service"])]
    refs, lat, lon, starts = [], [], [], [0]
    road_class, kmh, category = [], [], []
    for points, highway, speed in lines:
        for ref, la, lo_ in points:
            refs.append(ref)
            lat.append(la)
            lon.append(lo_)
        starts.append(len(refs))
        road_class.append(classes.CLASS_INDEX[highway])
        kmh.append(speed)
        category.append(classes.CATEGORY_INDEX[classes.category_of(highway, "")])
    count = len(lines)
    return Ways(
        refs=np.array(refs, dtype=np.int64), lat=np.array(lat), lon=np.array(lon), starts=np.array(starts, dtype=np.int64),
        road_class=np.array(road_class, dtype=np.uint8), kmh=np.array(kmh, dtype=np.float32),
        oneway=np.zeros(count, dtype=np.int8), unpaved=np.zeros(count, dtype=bool),
        category=np.array(category, dtype=np.uint8), restricted=np.zeros(count, dtype=bool),
        name_id=np.zeros(count, dtype=np.int32), names=[""], signals=np.array([], dtype=np.int64),
        stops=np.array([], dtype=np.int64), way_ids=np.arange(1, count + 1, dtype=np.int64),
    )


class SpurCase(unittest.TestCase):
    graph: graph_module.Graph
    options = costs.Options(penalty=8.0)

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "spurs.graph"
            build.build(spur_ways(), path, "test", progress=lambda _line: None)
            cls.graph = graph_module.load(path)

    def snap(self, point: tuple[float, float], soft: bool = False):
        return router.snap_all(self.graph, [point], 200, self.options, soft=soft)[0]

    def pieces(self, stops) -> list[search.Piece]:
        searcher = search.Searcher(self.graph, self.options, stops)
        pieces, _delay = router._drive(searcher, stops, 1.0)
        return pieces

    def both_ways(self, pieces: list[search.Piece]) -> set[int]:
        """Segments the path drives in both directions."""
        ways: dict[int, set[bool]] = {}
        for s, a, b in pieces:
            if abs(b - a) > 0.01:
                ways.setdefault(s, set()).add(b > a)
        return {s for s, d in ways.items() if len(d) == 2}

    def is_service(self, segment: int) -> bool:
        return int(self.graph.seg_class[segment]) == classes.SERVICE

    def plan_meters(self, stops) -> float:
        result = router.plan(self.graph, [(s.lat, s.lon) for s in stops], self.options, False, 200, snapped=stops)
        return result.routes[0].summary.meters


class SpurTest(SpurCase):
    def test_nudge_at_a_dead_end_is_folded_away(self) -> None:
        west, east = self.snap(main_node(0)), self.snap(main_node(SPUR_AT + 2))
        nudge = self.snap(SPUR_END)
        self.assertTrue(self.is_service(nudge.segment))  # nothing through near enough: stays on the spur
        stops = sources.with_shape([west, east], [(0, nudge)])
        self.assertTrue(stops[1].soft)
        pieces = self.pieces(stops)
        self.assertEqual(self.both_ways(pieces), set())
        self.assertFalse(any(self.is_service(s) for s, _, _ in pieces))
        plain = self.plan_meters([self.snap(main_node(0)), self.snap(main_node(SPUR_AT + 2))])
        self.assertAlmostEqual(self.plan_meters(stops), plain, delta=1.0)

    def test_stop_at_a_dead_end_keeps_the_spur(self) -> None:
        stops = [self.snap(main_node(0)), self.snap(SPUR_END), self.snap(main_node(SPUR_AT + 2))]
        self.assertFalse(stops[1].soft)
        pieces = self.pieces(stops)
        spur = stops[1].segment
        self.assertIn(spur, self.both_ways(pieces))
        plain = self.plan_meters([stops[0], stops[2]])
        self.assertGreater(self.plan_meters(stops), plain + 600)

    def test_soft_snap_prefers_the_through_road(self) -> None:
        near_junction = (LAT0 + 0.0003, LON0 + SPUR_AT * DLON)  # on the spur, ~33 m from the main road
        self.assertTrue(self.is_service(self.snap(near_junction).segment))
        soft = self.snap(near_junction, soft=True)
        self.assertTrue(soft.soft)
        self.assertFalse(self.is_service(soft.segment))
        # Too far from a through road: the nearest it is.
        self.assertTrue(self.is_service(self.snap(SPUR_END, soft=True).segment))

    def test_dead_ends(self) -> None:
        spur = self.snap(SPUR_END).segment
        ends = snap_module.dead_ends(self.graph, np.array([int(self.graph.seg_a[spur]), int(self.graph.seg_b[spur])]))
        self.assertEqual(sorted(ends.tolist()), [False, True])

    def test_service_bypass_does_not_undercut_the_main_road(self) -> None:
        lo, hi = BYPASS
        stops = [self.snap(main_node(lo - 1)), self.snap(main_node(hi + 1))]
        # With service roads at their old flat factor the bypass was cheaper
        # than the avoided main road (60 km/h / 8 against 20 km/h / 2).
        with mock.patch.object(costs, "service_factor", lambda _options: costs.SERVICE_FACTOR):
            old = self.pieces(stops)
        self.assertTrue(any(self.is_service(s) for s, _, _ in old))
        new = self.pieces(stops)
        self.assertFalse(any(self.is_service(s) for s, _, _ in new))

    def test_service_factor(self) -> None:
        self.assertEqual(costs.service_factor(costs.Options(penalty=8.0)), 8.0)
        self.assertEqual(costs.service_factor(costs.Options(penalty=1.0)), costs.SERVICE_FACTOR)
        self.assertEqual(costs.service_factor(costs.Options(penalty=8.0, factors={"interstate": 12.0})), 12.0)


if __name__ == "__main__":
    unittest.main()
