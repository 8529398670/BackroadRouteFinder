"""Turn-by-turn steps (cards: live turn-by-turn navigation), from a line and
the roads it follows, and the page being allowed to read the device's GPS."""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from app.core.middleware import PLAIN_HEADERS, SECURE_HEADERS
from app.routing import steps

# About 1 km in latitude; longitude scaled near the equator is the same.
KM = 1 / 111.195


@dataclass
class Road:
    name: str
    meters: float
    seconds: float


def dense(*corners: tuple[float, float], every_m: float = 20.0) -> list[tuple[float, float]]:
    """A line through the corners with a point every few metres, like a real road's shape."""
    out: list[tuple[float, float]] = [corners[0]]
    for a, b in zip(corners, corners[1:]):
        n = max(1, int(steps.metres(a, b) / every_m))
        for i in range(1, n + 1):
            out.append((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n))
    return out


class StepsTest(unittest.TestCase):
    def test_right_turn_then_left_turn(self) -> None:
        # North 1 km on Main, east 1 km on Oak (a right), north 1 km on Elm (a left).
        line = dense((0, 0), (KM, 0), (KM, KM), (2 * KM, KM))
        out = steps.build(line, [Road("Main", 1000, 60), Road("Oak", 1000, 60), Road("Elm", 1000, 60)], 200)
        self.assertEqual([s.type for s in out], ["depart", "turn", "turn", "arrive"])
        self.assertEqual([s.modifier for s in out[1:3]], ["right", "left"])
        self.assertEqual([s.road for s in out[:3]], ["Main", "Oak", "Elm"])
        self.assertAlmostEqual(out[1].along_m, 1000, delta=25)
        self.assertAlmostEqual(out[2].along_m, 2000, delta=25)
        self.assertAlmostEqual(sum(s.meters for s in out), 3000, delta=5)
        # The whole time includes turns: the steps share it out.
        self.assertAlmostEqual(sum(s.seconds for s in out), 200, delta=1)

    def test_turn_found_at_the_corner_when_the_lengths_are_off(self) -> None:
        # The lengths say Oak starts 1120 m in; the corner is at 1000 m.
        line = dense((0, 0), (KM, 0), (KM, KM))
        out = steps.build(line, [Road("Main", 1120, 60), Road("Oak", 880, 60)], 120)
        self.assertEqual(out[1].modifier, "right")
        self.assertAlmostEqual(out[1].along_m, 1000, delta=25)

    def test_new_name_straight_on_is_continue(self) -> None:
        line = dense((0, 0), (2 * KM, 0))
        out = steps.build(line, [Road("County Rd 5", 1000, 60), Road("Ridge Rd", 1000, 60)], 120)
        self.assertEqual([(s.type, s.modifier) for s in out[1:2]], [("continue", "straight")])
        self.assertEqual(out[1].road, "Ridge Rd")

    def test_slight_and_sharp(self) -> None:
        self.assertEqual(steps.modifier_of(10), "straight")
        self.assertEqual(steps.modifier_of(-30), "slight_left")
        self.assertEqual(steps.modifier_of(90), "right")
        self.assertEqual(steps.modifier_of(150), "sharp_right")
        self.assertEqual(steps.modifier_of(-178), "uturn")

    def test_stops_become_waypoints_in_order(self) -> None:
        line = dense((0, 0), (KM, 0), (KM, KM))
        out = steps.build(line, [Road("Main", 1000, 60), Road("Oak", 1000, 60)], 120, stops=[(0.5 * KM, 0)])
        self.assertEqual([s.type for s in out], ["depart", "waypoint", "turn", "arrive"])
        self.assertEqual(out[1].road, "Main")
        self.assertAlmostEqual(out[1].along_m, 500, delta=25)

    def test_one_road_is_depart_and_arrive(self) -> None:
        out = steps.build(dense((0, 0), (KM, 0)), [Road("", 1000, 60)], 60)
        self.assertEqual([s.type for s in out], ["depart", "arrive"])
        self.assertEqual(out[0].seconds, 60)

    def test_flatten_keeps_the_shared_point_once(self) -> None:
        self.assertEqual(steps.flatten([[[0, 0], [1, 1]], [[1, 1], [2, 2]]]), [(0, 0), (1, 1), (2, 2)])

    def test_too_short_has_no_steps(self) -> None:
        self.assertEqual(steps.build([(0, 0)], [], 0), [])


class GeolocationAllowedTest(unittest.TestCase):
    def test_the_page_may_ask_for_its_own_location_only(self) -> None:
        for headers in (PLAIN_HEADERS, SECURE_HEADERS):
            policy = headers["permissions-policy"]
            self.assertIn("geolocation=(self)", policy)
            self.assertIn("camera=()", policy)


if __name__ == "__main__":
    unittest.main()
