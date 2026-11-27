"""Turn-by-turn steps for a planned route, worked out from the line that is
drawn and the roads it follows (summarize.Via), so they match the route
whoever planned it, after nudges, locks and reversing.

The roads in via carry their lengths, but short bits are folded away and
another engine's line is only measured on our map, so a road's end is only
known roughly. Each change of road is therefore put at the sharpest corner
near where the lengths say it is; a corner is where a turn happens."""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

# How far either side of a point the bearing in and out is measured.
ARM_M = 30.0
# How far from where the lengths put a change of road to look for its corner.
SEARCH_M = 200.0
# Turns this sharp or less count as straight on.
STRAIGHT_DEG = 20.0
SLIGHT_DEG = 45.0
SHARP_DEG = 135.0
UTURN_DEG = 170.0


@dataclass
class Step:
    type: str  # depart, turn, continue, waypoint, arrive
    modifier: str  # straight, slight_left, left, sharp_left, slight_right, right, sharp_right, uturn ("" for depart/arrive/waypoint)
    road: str  # the road driven after this step ("" when it has no name)
    lat: float
    lon: float
    along_m: float  # metres from the start of the line
    meters: float  # to the next step
    seconds: float  # to the next step


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    x = (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(x, b[0] - a[0]) * 111_195.0


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    dx = ((b[1] - a[1] + 540.0) % 360.0 - 180.0) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.degrees(math.atan2(dx, b[0] - a[0])) % 360.0


def modifier_of(delta: float) -> str:
    """delta: degrees turned, right positive."""
    size = abs(delta)
    if size < STRAIGHT_DEG:
        return "straight"
    if size >= UTURN_DEG:
        return "uturn"
    side = "right" if delta > 0 else "left"
    if size < SLIGHT_DEG:
        return "slight_" + side
    if size < SHARP_DEG:
        return side
    return "sharp_" + side


class Line:
    """A line of (lat, lon) points with the distance along it at each."""

    def __init__(self, coords: list[tuple[float, float]]):
        self.points = coords
        self.cum = [0.0]
        for a, b in zip(coords, coords[1:]):
            self.cum.append(self.cum[-1] + metres(a, b))

    @property
    def length(self) -> float:
        return self.cum[-1]

    def at(self, along: float) -> tuple[float, float]:
        along = min(max(along, 0.0), self.length)
        i = max(1, min(bisect.bisect_left(self.cum, along), len(self.points) - 1))
        span = self.cum[i] - self.cum[i - 1]
        f = (along - self.cum[i - 1]) / span if span > 0 else 0.0
        a, b = self.points[i - 1], self.points[i]
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)

    def turn(self, i: int) -> float:
        """Degrees turned at point i, right positive (0 at the ends)."""
        d = self.cum[i]
        if d < 1 or d > self.length - 1:
            return 0.0
        before, here, after = self.at(d - ARM_M), self.points[i], self.at(d + ARM_M)
        if metres(before, here) < 1 or metres(here, after) < 1:
            return 0.0
        return (bearing(here, after) - bearing(before, here) + 540.0) % 360.0 - 180.0

    def index_near(self, along: float, lo: int = 0) -> int:
        i = bisect.bisect_left(self.cum, along, lo)
        if i >= len(self.cum):
            return len(self.cum) - 1
        if i > lo and along - self.cum[i - 1] < self.cum[i] - along:
            return i - 1
        return i

    def nearest(self, point: tuple[float, float], lo: int) -> int:
        """The point on the line nearest point, from index lo on: the first
        pass within 25 m wins, so a line that comes back past it later does
        not jump ahead."""
        best, best_m = lo, math.inf
        for i in range(lo, len(self.points)):
            m = metres(point, self.points[i])
            if m < 25:
                return i
            if m < best_m:
                best, best_m = i, m
        return best


def flatten(runs: list[list[list[float]]]) -> list[tuple[float, float]]:
    """The runs' coords as one line, the shared point where two runs meet kept once."""
    out: list[tuple[float, float]] = []
    for coords in runs:
        for index, point in enumerate(coords):
            p = (float(point[0]), float(point[1]))
            if index == 0 and out and out[-1] == p:
                continue
            out.append(p)
    return out


def build(coords: list[tuple[float, float]], via: list, seconds: float, stops: list[tuple[float, float]] | None = None) -> list[Step]:
    """via: the roads followed, in order (anything with name, meters and
    seconds); seconds: the whole route's time; stops: the points driven
    through between the start and the end (not nudges)."""
    if len(coords) < 2:
        return []
    line = Line(coords)
    total = line.length
    if total <= 0:
        return []
    roads = [v for v in via if v.meters > 0] or []
    via_total = sum(v.meters for v in roads)
    scale = total / via_total if via_total > 0 else 1.0

    # Where each road starts along the line, and the time curve the roads give.
    marks: list[tuple[int, float]] = []  # (index of the corner, degrees turned)
    lo = 0
    run_m = 0.0
    for road in roads[:-1]:
        run_m += road.meters * scale
        first = line.index_near(run_m - SEARCH_M, lo)
        last = line.index_near(run_m + SEARCH_M, first)
        best_i, best_turn = line.index_near(run_m, first), 0.0
        for i in range(max(first, lo + 1), last + 1):
            t = line.turn(i)
            if abs(t) > abs(best_turn) + 1e-9:
                best_i, best_turn = i, t
        if abs(best_turn) < STRAIGHT_DEG:
            best_i = max(line.index_near(run_m, lo), lo)
            best_turn = line.turn(best_i)
        best_i = min(best_i, len(coords) - 1)
        marks.append((best_i, best_turn))
        lo = best_i

    time_at = _time_curve(roads, scale, total, seconds)

    steps: list[Step] = [Step("depart", "", roads[0].name if roads else "", *coords[0], 0.0, 0.0, 0.0)]
    for k, (i, turned) in enumerate(marks):
        modifier = modifier_of(turned)
        kind = "continue" if modifier == "straight" else "turn"
        steps.append(Step(kind, modifier, roads[k + 1].name, *coords[i], line.cum[i], 0.0, 0.0))

    lo = 0
    for stop in stops or []:
        i = line.nearest(stop, lo)
        lo = i
        along = line.cum[i]
        road = _road_at(steps, along)
        steps.append(Step("waypoint", "", road, *coords[i], along, 0.0, 0.0))
    steps.sort(key=lambda s: (s.along_m, s.type != "depart", s.type == "waypoint"))

    steps.append(Step("arrive", "", "", *coords[-1], total, 0.0, 0.0))
    for step, after in zip(steps, steps[1:]):
        step.meters = after.along_m - step.along_m
        step.seconds = max(0.0, time_at(after.along_m) - time_at(step.along_m))
    for step in steps:
        step.lat, step.lon = round(step.lat, 6), round(step.lon, 6)
        step.along_m, step.meters, step.seconds = round(step.along_m, 1), round(step.meters, 1), round(step.seconds, 1)
    return steps


def _road_at(steps: list[Step], along: float) -> str:
    road = ""
    for step in steps:
        if step.along_m > along:
            break
        road = step.road
    return road


def _time_curve(roads: list, scale: float, total: float, seconds: float):
    """Seconds from the start at each distance along the line: the roads'
    own speeds, stretched so the end comes at the route's whole time (which
    adds turns and stops)."""
    xs, ts = [0.0], [0.0]
    for road in roads:
        xs.append(xs[-1] + road.meters * scale)
        ts.append(ts[-1] + max(road.seconds, 0.0))
    if len(xs) < 2 or ts[-1] <= 0:
        return lambda d: seconds * d / total if total > 0 else 0.0
    xs[-1] = total
    stretch = seconds / ts[-1] if seconds > 0 else 1.0

    def at(d: float) -> float:
        d = min(max(d, 0.0), total)
        i = max(1, min(bisect.bisect_left(xs, d), len(xs) - 1))
        span = xs[i] - xs[i - 1]
        f = (d - xs[i - 1]) / span if span > 0 else 0.0
        return (ts[i - 1] + (ts[i] - ts[i - 1]) * f) * stretch

    return at
