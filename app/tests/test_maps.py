"""Many maps, each picked per route (app/routing/maps.py), and the task queue
that builds them (app/routing/tasks.py), on small grid maps built here: an
"ohio" grid, an "indiana" grid just west of it, a "jordan" grid far away,
and a joined map of ohio and indiana."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.routing import build, maps as map_set, sources, tasks
from app.routing.osm_parse import Ways
from app.tests.test_road_rules import DLAT, DLON, SIZE, grid_ways

OHIO = (40.0, -83.0)
INDIANA = (40.0, -83.0 - SIZE * DLON - 0.01)     # its east edge 1 km from Ohio's west edge
JORDAN = (31.9, 35.9)


def shifted(origin: tuple[float, float], id_base: int) -> Ways:
    ways = grid_ways()
    return replace(ways, lat=ways.lat - 40.0 + origin[0], lon=ways.lon + 83.0 + origin[1],
                   refs=ways.refs + id_base, way_ids=ways.way_ids + id_base)


def joined(a: Ways, b: Ways) -> Ways:
    """Two grids as one map, with a road across the border (a's last way's
    end to b's first way's start), as neighbouring states have."""
    bridge = {"refs": [a.refs[a.starts[-1] - 1], b.refs[0]], "lat": [a.lat[a.starts[-1] - 1], b.lat[0]],
              "lon": [a.lon[a.starts[-1] - 1], b.lon[0]]}
    out = {}
    for name in ("refs", "lat", "lon"):
        out[name] = np.concatenate((getattr(a, name), getattr(b, name), np.array(bridge[name], dtype=getattr(a, name).dtype)))
    for name in ("road_class", "kmh", "oneway", "unpaved", "category", "restricted", "name_id"):
        out[name] = np.concatenate((getattr(a, name), getattr(b, name), getattr(a, name)[-1:]))
    out["way_ids"] = np.concatenate((a.way_ids, b.way_ids, [999_999]))
    total = a.starts[-1] + b.starts[-1]
    out["starts"] = np.concatenate((a.starts, b.starts[1:] + a.starts[-1], [total + 2]))
    return Ways(**out, names=[""], signals=np.array([], dtype=np.int64), stops=np.array([], dtype=np.int64))


def at(origin: tuple[float, float], r: int, c: int) -> tuple[float, float]:
    return origin[0] + r * DLAT, origin[1] + c * DLON


class MapsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        app_dir = Path(cls.tmp.name)
        folder = app_dir / "mapdata"
        folder.mkdir()
        ohio, indiana, jordan = shifted(OHIO, 0), shifted(INDIANA, 100_000), shifted(JORDAN, 200_000)
        for name, ways, areas in (("ohio", ohio, ["ohio"]), ("indiana", indiana, ["indiana"]), ("jordan", jordan, ["jordan"]),
                                  ("indiana_ohio", joined(indiana, ohio), ["indiana", "ohio"])):
            build.build(ways, folder / f"{name}.graph", "test", progress=lambda _line: None)
            (folder / f"{name}.json").write_text(json.dumps({"areas": areas}))
        for slug in ("ohio", "indiana", "jordan"):
            (folder / f"{slug}.osm.pbf").write_bytes(b"x" * 1000)
        cls.cfg = SimpleNamespace(app_dir=app_dir, file=SimpleNamespace(routing=SimpleNamespace(region="ohio")))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def fresh(self) -> map_set.MapSet:
        maps = map_set.MapSet(Path(self.tmp.name) / "mapdata", self.cfg)
        maps.rescan()
        return maps

    def test_index(self) -> None:
        maps = self.fresh()
        self.assertEqual(set(maps.maps), {"ohio", "indiana", "jordan", "indiana_ohio"})
        self.assertEqual(maps.maps["indiana_ohio"].kind, "joined")
        self.assertEqual(maps.area_map("ohio").name, "ohio")
        self.assertIsNone(maps.area_map("indiana_ohio"))
        self.assertEqual(maps.open_names(), [])   # nothing opened until a route needs it

    def test_a_trip_uses_its_own_areas_map(self) -> None:
        maps = self.fresh()
        name, graph = maps.pick([at(JORDAN, 0, 0), at(JORDAN, 6, 6)], 200)
        self.assertEqual(name, "jordan")
        self.assertEqual(maps.open_names(), ["jordan"])   # Ohio's maps stay closed
        self.assertEqual(maps.pick([at(OHIO, 0, 0), at(OHIO, 6, 6)], 200)[0], "ohio")   # the area map, not the join
        self.assertEqual(maps.last_used, "ohio")

    def test_a_trip_across_areas_uses_their_join(self) -> None:
        maps = self.fresh()
        self.assertEqual(maps.pick([at(INDIANA, 0, 0), at(OHIO, 6, 6)], 200)[0], "indiana_ohio")

    def test_no_map_holds_both_ohio_and_jordan(self) -> None:
        maps = self.fresh()
        points = [at(OHIO, 0, 0), at(JORDAN, 0, 0)]
        self.assertIsNone(maps.pick(points, 200))
        areas, why = maps.join_for(points, 200, 3000)
        # Indiana borders Ohio and lies within the trip's box, so it comes too.
        self.assertEqual(areas, ["indiana", "jordan", "ohio"], why)

    def test_a_join_takes_in_an_area_beside_the_trip(self) -> None:
        maps = self.fresh()
        maps.maps.pop("indiana_ohio")
        areas, _ = maps.join_for([at(OHIO, 3, 0), at(INDIANA, 3, 6)], 200, 3000)
        self.assertEqual(areas, ["indiana", "ohio"])
        # Jordan is far from both: a trip from Ohio to Indiana never takes it in.
        self.assertNotIn("jordan", areas)

    def test_points_on_one_area_need_no_join(self) -> None:
        maps = self.fresh()
        maps.maps.pop("indiana_ohio")
        areas, why = maps.join_for([at(OHIO, 3, 0), at(OHIO, 3, SIZE - 1)], 200, 3000)
        self.assertEqual(areas, [])
        self.assertIn("one area", why)

    def test_a_point_on_no_map_gets_no_join(self) -> None:
        maps = self.fresh()
        areas, why = maps.join_for([at(OHIO, 0, 0), (10.0, 10.0)], 200, 3000)
        self.assertEqual(areas, [])
        self.assertIn("no map", why)

    def test_a_join_over_the_size_limit_is_not_built(self) -> None:
        maps = self.fresh()
        areas, why = maps.join_for([at(OHIO, 0, 0), at(JORDAN, 0, 0)], 200, 0)
        self.assertEqual(areas, [])
        self.assertIn("join_max_mb", why)

    def test_partial_finds_a_map_for_some_point(self) -> None:
        maps = self.fresh()
        self.assertEqual(maps.partial([(10.0, 10.0), at(JORDAN, 1, 1)], 200)[0], "jordan")
        self.assertIsNone(maps.partial([(10.0, 10.0)], 200))

    def test_open_maps_are_capped(self) -> None:
        maps = self.fresh()
        maps.configure(open_max=1, idle_minutes=15)
        maps.pick([at(OHIO, 0, 0)], 200)
        maps.pick([at(JORDAN, 0, 0)], 200)
        self.assertEqual(maps.open_names(), ["jordan"])

    def test_a_rebuilt_map_is_opened_again(self) -> None:
        maps = self.fresh()
        first = maps.get("jordan")
        maps.maps["jordan"].built_at = "later"
        self.assertIsNot(maps.get("jordan"), first)

    def test_unbuilt_areas(self) -> None:
        maps = self.fresh()
        self.assertEqual(map_set.unbuilt_areas(self.cfg, maps), [])
        maps.maps.pop("indiana")
        self.assertEqual(map_set.unbuilt_areas(self.cfg, maps), ["indiana"])


class NoMapTest(unittest.TestCase):
    def test_engines_alone_with_no_map_at_all(self) -> None:
        from unittest import mock

        from app.routing.providers import base

        line = [(31.95, 35.93), (32.2, 35.9), (32.55, 35.85)]

        def fetch(engine, points, ask, key, url, timeout):
            return [base.Candidate(coords=line, seconds=4000.0, meters=90_000.0)]

        access = sources.Access(keys={}, urls={"valhalla": "https://valhalla.test"}, timeout=1.0)
        points = [sources.Spot(*line[0]), sources.Spot(*line[-1])]
        with mock.patch.object(base, "cached_fetch", fetch):
            plan = sources.plan_off_map(None, points, map_set.costs.Options(penalty=8.0), ["valhalla"], 3, access)
        route = plan.routes[0]
        self.assertEqual((route.summary.seconds, route.summary.meters, route.off_map_meters), (4000.0, 90_000.0, 90_000.0))
        self.assertEqual(len(route.summary.runs), 1)
        self.assertFalse(route.summary.runs[0].main)


class QueueTest(unittest.TestCase):
    """The runner's queue, with each task held until the test lets it finish."""

    def runner(self):
        gates: dict[int, threading.Event] = {}

        class Held(tasks.Runner):
            def _run(self, task):
                gates.setdefault(task.id, threading.Event()).wait(5)
                task.state = "done"
                self._next_task()

        return Held(), gates

    def test_tasks_wait_their_turn_and_keys_are_kept_once(self) -> None:
        runner, gates = self.runner()
        first = runner.start("one", [], key="a")
        self.assertEqual(first.state, "running")
        second = runner.start("two", [], key="b")
        self.assertEqual(second.state, "waiting")
        self.assertIs(runner.start("two again", [], key="b"), second)
        self.assertIs(runner.start("one again", [], key="a"), first)
        self.assertTrue(runner.pending("b"))
        third = runner.start("three", [])
        self.assertTrue(runner.cancel(third.id))     # off the queue
        self.assertEqual([t.title for t in runner.waiting], ["two"])
        gates.setdefault(first.id, threading.Event()).set()
        for _ in range(100):
            if runner.task is second:
                break
            threading.Event().wait(0.02)
        self.assertIs(runner.task, second)
        self.assertEqual(second.state, "running")
        gates.setdefault(second.id, threading.Event()).set()


if __name__ == "__main__":
    unittest.main()
