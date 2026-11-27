"""Other routing engines: their line encodings, how each one's answer is
read, which are usable, and their API keys sealed in the database."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import db, encryption
from app.models import provider_keys, sealed
from app.routing import sources
from app.routing.providers import ENGINES, base, google, graphhopper, here, mapbox, ors, osrm, polyline, tomtom, valhalla

POINTS = [(39.96, -83.0), (40.30, -83.07)]
LINE5 = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"      # Google's documented example
LINE6 = "_izlhA~rlgdF_{geC~ywl@_kwzCn`{nI"  # the same points at precision 6


class PolylineTest(unittest.TestCase):
    def test_google_polyline(self) -> None:
        self.assertEqual(polyline.decode(LINE5), [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])
        self.assertEqual(polyline.decode(LINE6, 6), [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])

    def test_here_flexible_polyline(self) -> None:
        # HERE's documented example.
        self.assertEqual(polyline.decode_flexible("BFoz5xJ67i1B1B7PzIhaxL7Y"),
                         [(50.10228, 8.69821), (50.10201, 8.69567), (50.10063, 8.6915), (50.09878, 8.68752)])


class AnswersTest(unittest.TestCase):
    """Each engine's answer, in the shape its documentation gives, becomes
    candidates with (lat, lon) lines, seconds and metres."""

    def fetch(self, module, answer, backroads=True, key="k" * 20, url="https://example.test"):
        with mock.patch.object(module, "request_json", return_value=answer) as asked:
            found = module.fetch(POINTS, backroads, key, url, 5)
        return found, asked

    def check(self, found, seconds, meters):
        self.assertTrue(found)
        self.assertEqual(found[0].coords[0], (38.5, -120.2))
        self.assertAlmostEqual(found[0].seconds, seconds)
        self.assertAlmostEqual(found[0].meters, meters)

    def test_valhalla(self) -> None:
        found, asked = self.fetch(valhalla, {"trip": {"legs": [{"shape": LINE6}], "summary": {"time": 600, "length": 12.5}},
                                             "alternates": [{"trip": {"legs": [{"shape": LINE6}], "summary": {"time": 700, "length": 13}}}]})
        self.check(found, 600, 12500)
        self.assertEqual(len(found), 2)
        self.assertEqual(asked.call_args.kwargs["body"]["costing_options"]["auto"]["use_highways"], 0.0)

    def test_osrm(self) -> None:
        found, asked = self.fetch(osrm, {"code": "Ok", "routes": [{"geometry": LINE6, "duration": 610, "distance": 12000}]})
        self.check(found, 610, 12000)
        self.assertIn("exclude=motorway", asked.call_args.args[0])
        self.assertIn("-83.000000,39.960000;", asked.call_args.args[0])

    def test_osrm_falls_back_without_exclude(self) -> None:
        answers = [base.ProviderError("HTTP 400: exclude not supported"), {"code": "Ok", "routes": [{"geometry": LINE6, "duration": 1, "distance": 2}]}]
        with mock.patch.object(osrm, "request_json", side_effect=answers) as asked:
            found = osrm.fetch(POINTS, True, "", "https://example.test", 5)
        self.assertEqual(len(found), 1)
        self.assertNotIn("exclude", asked.call_args.args[0])

    def test_openrouteservice(self) -> None:
        feature = {"geometry": {"coordinates": [[-120.2, 38.5], [-120.95, 40.7]]}, "properties": {"summary": {"duration": 620, "distance": 12100}}}
        found, asked = self.fetch(ors, {"features": [feature]})
        self.check(found, 620, 12100)
        self.assertEqual(asked.call_args.kwargs["headers"], {"Authorization": "k" * 20})
        self.assertEqual(asked.call_args.kwargs["body"]["options"]["avoid_features"], ["highways", "tollways"])

    def test_graphhopper(self) -> None:
        path = {"points": {"coordinates": [[-120.2, 38.5], [-120.95, 40.7]]}, "time": 630000, "distance": 12200}
        found, asked = self.fetch(graphhopper, {"paths": [path]})
        self.check(found, 630, 12200)
        self.assertIn("custom_model", asked.call_args.kwargs["body"])

    def test_tomtom(self) -> None:
        route = {"legs": [{"points": [{"latitude": 38.5, "longitude": -120.2}, {"latitude": 40.7, "longitude": -120.95}]}],
                 "summary": {"travelTimeInSeconds": 640, "lengthInMeters": 12300}}
        found, asked = self.fetch(tomtom, {"routes": [route]})
        self.check(found, 640, 12300)
        self.assertIn("routeType=thrilling", asked.call_args.args[0])

    def test_here(self) -> None:
        # The same points as LINE5, in flexible polyline at precision 5.
        route = {"sections": [{"polyline": "BFoz5xJ67i1B1B7PzIhaxL7Y", "summary": {"duration": 650, "length": 12400}}]}
        found, asked = self.fetch(here, {"routes": [route]})
        self.assertEqual(found[0].coords[0], (50.10228, 8.69821))
        self.assertEqual((found[0].seconds, found[0].meters), (650, 12400))
        self.assertIn("avoid%5Bfeatures%5D=controlledAccessHighway", asked.call_args.args[0])

    def test_mapbox(self) -> None:
        found, asked = self.fetch(mapbox, {"code": "Ok", "routes": [{"geometry": LINE6, "duration": 660, "distance": 12500}]})
        self.check(found, 660, 12500)
        self.assertIn("exclude=motorway,toll", asked.call_args.args[0])

    def test_google(self) -> None:
        found, asked = self.fetch(google, {"routes": [{"duration": "670s", "distanceMeters": 12600, "polyline": {"encodedPolyline": LINE5}}]})
        self.check(found, 670, 12600)
        self.assertTrue(asked.call_args.kwargs["body"]["routeModifiers"]["avoidHighways"])

    def test_an_error_names_the_engines_message(self) -> None:
        self.assertEqual(base._find_message({"error": {"code": 403, "message": "Access to this API has been disallowed"}}),
                         "Access to this API has been disallowed")


class AskTest(unittest.TestCase):
    """The planner's road rules, as each engine is told them."""

    def test_from_rules(self) -> None:
        rules = {"interstate": "never", "us_highway": "use", "state_route": "avoid", "unpaved": "avoid", "track": "use"}
        ask = base.Ask.from_rules(rules, backroads=True)
        self.assertEqual((ask.freeways, ask.major, ask.unpaved, ask.tracks), ("never", "avoid", "avoid", "use"))
        # Backroads off: avoid means nothing, never still holds.
        ask = base.Ask.from_rules(rules, backroads=False)
        self.assertEqual((ask.freeways, ask.major, ask.unpaved), ("never", "use", "use"))

    def body(self, module, ask, answer):
        with mock.patch.object(module, "request_json", return_value=answer) as asked:
            module.fetch(POINTS, ask, "k" * 20, "https://example.test", 5)
        return asked.call_args

    def test_valhalla_follows_rules(self) -> None:
        answer = {"trip": {"legs": [{"shape": LINE6}], "summary": {"time": 1, "length": 1}}}
        auto = self.body(valhalla, base.Ask(unpaved="never", tracks="use"), answer).kwargs["body"]["costing_options"]["auto"]
        self.assertTrue(auto["exclude_unpaved"])
        self.assertNotIn("use_highways", auto)
        self.assertEqual(auto["use_tracks"], 0.5)

    def test_graphhopper_follows_rules(self) -> None:
        answer = {"paths": [{"points": {"coordinates": [[-120.2, 38.5], [-120.95, 40.7]]}, "time": 1, "distance": 1}]}
        model = self.body(graphhopper, base.Ask(backroads=True, freeways="never", major="avoid"), answer).kwargs["body"]["custom_model"]
        self.assertEqual([(rule["if"], rule["multiply_by"]) for rule in model["priority"]],
                         [(graphhopper.FREEWAYS, "0.01"), (graphhopper.MAJOR, "0.1")])
        body = self.body(graphhopper, base.Ask(), answer).kwargs["body"]
        self.assertNotIn("custom_model", body)

    def test_mapbox_and_tomtom_leave_out_unpaved(self) -> None:
        url = self.body(mapbox, base.Ask(unpaved="avoid"), {"code": "Ok", "routes": [{"geometry": LINE6, "duration": 1, "distance": 1}]}).args[0]
        self.assertIn("exclude=unpaved", url)
        route = {"legs": [{"points": [{"latitude": 38.5, "longitude": -120.2}]}], "summary": {"travelTimeInSeconds": 1, "lengthInMeters": 1}}
        url = self.body(tomtom, base.Ask(unpaved="never"), {"routes": [route]}).args[0]
        self.assertIn("avoid=unpavedRoads", url)
        self.assertNotIn("avoid=motorways", url)

    def test_every_engine_says_what_it_honours(self) -> None:
        for engine in ENGINES.values():
            self.assertTrue(engine.honours, engine.id)
            self.assertLessEqual(engine.honours, {"freeways", "major", "unpaved", "tracks"})


class AccessTest(unittest.TestCase):
    def test_usable(self) -> None:
        access = sources.Access(keys={"tomtom": "x" * 20}, urls={"valhalla": "https://v.test", "osrm": "", "ors": "https://o.test",
                                                                   "graphhopper": "https://graphhopper.com/api/1"}, timeout=5)
        self.assertTrue(access.usable("valhalla"))
        self.assertFalse(access.usable("osrm"))           # address empty: switched off
        self.assertFalse(access.usable("ors"))            # no key
        self.assertTrue(access.usable("tomtom"))
        self.assertFalse(access.usable("graphhopper"))    # hosted needs a key
        self.assertTrue(sources.Access(keys={}, urls={"graphhopper": "http://localhost:8989"}, timeout=5).usable("graphhopper"))

    def test_every_engine_is_listed_once(self) -> None:
        self.assertEqual(len(ENGINES), 8)
        self.assertNotIn("ours", ENGINES)


class KeysTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "app.db"
        self.key = encryption.generate_key_hex()
        self.store = db.open_store(self.path, self.key, True)

    def test_set_read_delete(self) -> None:
        provider_keys.set_key(self.store, self.key, "tomtom", "abcdefgh12345678")
        provider_keys.set_key(self.store, self.key, "here", "zzzzzzzzyyyy9999")
        self.assertEqual(provider_keys.get_all(self.store, self.key), {"tomtom": "abcdefgh12345678", "here": "zzzzzzzzyyyy9999"})
        record = self.store.get(db.BUCKET_META, provider_keys.META_KEY)
        self.assertNotIn("abcdefgh12345678", str(record))   # sealed on its own, too
        self.assertEqual(provider_keys.hint("abcdefgh12345678"), "…5678")
        self.assertTrue(provider_keys.delete_key(self.store, "tomtom"))
        self.assertFalse(provider_keys.delete_key(self.store, "tomtom"))
        self.assertEqual(provider_keys.get_all(self.store, self.key), {"here": "zzzzzzzzyyyy9999"})

    def test_keys_survive_a_key_rotation(self) -> None:
        provider_keys.set_key(self.store, self.key, "mapbox", "pk.abcdefghijklmnop")
        record = self.store.get(db.BUCKET_META, provider_keys.META_KEY)
        new_key = encryption.generate_key_hex()
        sealed.rekey_record(self.key, new_key)(db.BUCKET_META, record)
        self.store.put(db.BUCKET_META, provider_keys.META_KEY, record)
        self.assertEqual(provider_keys.get_all(self.store, new_key), {"mapbox": "pk.abcdefghijklmnop"})

    def test_own_keys_and_rotation(self) -> None:
        from app.models import users

        user = users.create_user(self.store, "Grace", "user")
        self.assertTrue(provider_keys.set_user_key(self.store, self.key, user.id, "here", "mine12345678"))
        self.assertEqual(provider_keys.get_user_keys(self.store, self.key, user.id), {"here": "mine12345678"})
        record = self.store.get(db.BUCKET_USERS, user.id)
        self.assertNotIn("mine12345678", str(record))
        new_key = encryption.generate_key_hex()
        sealed.rekey_record(self.key, new_key)(db.BUCKET_USERS, record)
        self.store.put(db.BUCKET_USERS, user.id, record)
        self.assertEqual(provider_keys.get_user_keys(self.store, new_key, user.id), {"here": "mine12345678"})
        self.assertTrue(provider_keys.delete_user_key(self.store, user.id, "here"))
        self.assertFalse(provider_keys.delete_user_key(self.store, user.id, "here"))

    def test_policy(self) -> None:
        self.assertEqual(set(provider_keys.get_policy(self.store).values()), {"shared"})
        provider_keys.set_policy(self.store, {"here": "own", "bogus": "own", "tomtom": "nonsense"})
        policy = provider_keys.get_policy(self.store)
        self.assertEqual(policy["here"], "own")
        self.assertEqual(policy["tomtom"], "shared")
        self.assertNotIn("bogus", policy)

    def test_effective(self) -> None:
        shared = {"here": "S" * 10, "tomtom": "T" * 10}
        own = {"tomtom": "U" * 10}
        policy = {engine_id: "shared" for engine_id in ENGINES} | {"here": "own"}
        keys, origin = provider_keys.effective(shared, own, policy, is_owner=False)
        self.assertEqual(keys, {"tomtom": "U" * 10})            # own wins; here's shared key is not inherited
        self.assertEqual(origin, {"tomtom": "yours"})
        keys, origin = provider_keys.effective(shared, {}, policy, is_owner=True)
        self.assertEqual(origin, {"here": "shared", "tomtom": "shared"})  # the owner always has them
        policy["here"] = "shared"
        keys, _ = provider_keys.effective(shared, {}, policy, is_owner=False)
        self.assertEqual(keys["here"], "S" * 10)
        access = sources.Access(keys={}, urls={}, timeout=5, own_required=frozenset({"here"}))
        self.assertIn("your own", access.why_not("here"))


class MapDataTest(unittest.TestCase):
    def test_region_names(self) -> None:
        from app.routing import regions

        self.assertEqual(regions.region_name(["ohio"]), "ohio")
        self.assertEqual(regions.region_name(["ohio", "indiana", "ohio"]), "indiana_ohio")
        self.assertEqual(regions.region_name(["ohio", "us"]), "us")
        self.assertTrue(regions.region_name(list(regions.STATES)).startswith("states-"))
        self.assertTrue(regions.url_of("new-york").endswith("/us/new-york-latest.osm.pbf"))

    def test_merge_keeps_border_ways_once(self) -> None:
        import numpy as np

        from app.routing import osm_parse

        def ways(ids, names, refs_per_way):
            refs = [r for way in refs_per_way for r in way]
            starts = np.concatenate(([0], np.cumsum([len(w) for w in refs_per_way])))
            return osm_parse.Ways(
                refs=np.array(refs, dtype=np.int64), lat=np.array(refs, dtype=np.float64), lon=np.array(refs, dtype=np.float64),
                starts=starts.astype(np.int64), road_class=np.zeros(len(ids), dtype=np.uint8), kmh=np.ones(len(ids), dtype=np.float32),
                oneway=np.zeros(len(ids), dtype=np.int8), unpaved=np.zeros(len(ids), dtype=bool),
                category=np.zeros(len(ids), dtype=np.uint8), restricted=np.zeros(len(ids), dtype=bool),
                name_id=np.arange(1, len(ids) + 1, dtype=np.int32), names=[""] + names,
                signals=np.array([1], dtype=np.int64), stops=np.array([], dtype=np.int64), way_ids=np.array(ids, dtype=np.int64))

        import tempfile
        from pathlib import Path

        from app.routing import build_stream
        from app.routing.disk import ArrayDir

        a = ways([10, 11], ["A St", "Border Rd"], [[1, 2], [2, 3, 4]])
        b = ways([11, 12], ["Border Rd", "B Ave"], [[2, 3, 4], [4, 5]])
        with tempfile.TemporaryDirectory() as folder:
            work = ArrayDir(Path(folder))
            writer = build_stream.WayWriter(work)
            writer.add_ways(a)
            writer.add_ways(b)
            names = writer.close()
            build_stream._dedupe(work, build_stream.Budget(64 << 20), lambda _line: None)
            self.assertEqual(list(work.open("way_id")), [10, 11, 12])
            self.assertEqual(list(work.open("pos_ref")), [1, 2, 2, 3, 4, 4, 5])
            self.assertEqual(list(work.open("way_len")), [2, 3, 2])
            self.assertEqual([names[i] for i in work.open("way_name")], ["A St", "Border Rd", "B Ave"])
            self.assertEqual(list(work.open("signal_ids")), [1, 1])


if __name__ == "__main__":
    unittest.main()
