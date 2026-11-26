# TODO

## Expand from Ohio to the whole USA
- The graph build is currently in-memory Python: Ohio (330 MB extract, 8.8 M node refs) peaks around 1.1 GB and takes about a minute. The US extract (~10 GB) would need tens of GB that way. Options:
  - stream the build: two passes with osmium (count node uses first, then cut ways), writing arrays to disk with `numpy.memmap` instead of holding them;
  - per-state graphs (one `routing.region` each) joined at the borders, routing a trip across the states it touches;
  - or move to one of the engines below once the map is that big.
- Search time grows with the graph: a whole-graph Dijkstra on the turn graph is ~0.9 s for Ohio, and would be ~30x for the US. It needs a bounded search (A* with a straight-line heuristic, or bidirectional) before then.
- Raise `docker.memory`, and give place search the new area (it already uses the graph's bounding box).

## Other routing engines worth trying (instead of, or beside, our own)
- **GraphHopper**, self-hosted (Java): "custom models" can change `priority` by `road_class` per request (`motorway`, `trunk`, `primary` x0.1), with CH turned off for flexible mode. Fast, turn-by-turn, multi-state. Runs as a second container/process.
- **Valhalla**, self-hosted: `costing_options.auto.use_highways` (0-1), `use_tolls`, `use_tracks`, `use_living_streets`. `use_highways` covers only freeways, so US/state routes need more (e.g. its `exclude_polygons`, or a custom costing build).
- **OSRM** with a custom Lua profile: very fast, but the weights are fixed when the graph is built, so the "how hard to avoid" slider means one graph per setting.
- **Public demo servers** (valhalla1.openstreetmap.de, router.project-osrm.org): no setup, but rate limits, no uptime promise, and only their default options.
- **OpenRouteService** (hosted, free key): `avoid_features` (highways, tollways, ferries) and avoid polygons.
- **HERE Routing v8**: `avoid[features]`, `avoid[areas]`, `avoid[segments]` per request.
- **Google Directions**: only `avoid=highways|tolls|ferries`, which leaves US and state routes in play. Not enough on its own.

## Routing quality
- Turn costs are in (app/routing/turns.py): angle, traffic signals, mapped and implied stop signs. Next:
  - mapped turn restrictions (OSM `type=restriction` relations: no_left_turn, only_straight_on, ...), which the edge-based graph can now forbid;
  - tune the seconds against real drives (record a few GPS traces, compare);
  - only ~28k stop signs are mapped in Ohio, so most stops are inferred from which road is bigger; 4-way stops between equal roads get 2 s of "caution" only.
- Speed: a whole-graph Dijkstra on the turn graph is ~0.9 s on the Mac and ~1 s in Docker per route (two with the comparison). A bounded search (A*, or a `limit` from the straight-line distance) would cut short trips to milliseconds.
- Snap a start/destination to the road it is on, not the nearest one, when a point sits between two parallel roads (driveway-aware).
- Scenic or "curvy" scoring: prefer winding roads, rivers, forests (sinuosity from the geometry, landuse from OSM).
- Turn-by-turn directions from the pieces (the "via" list is a start).
- Avoid a road or area the user draws on the map.
- Live traffic and closures (needs a data source).

## App
- Scheduled job (`registry.register`) to re-download the extract and rebuild the graph monthly, loading the new graph without a restart.
- Self-hosted map tiles (a vector-tile server or a pre-rendered set), so the page needs nothing from tile.openstreetmap.org.
- Send to phone: a share link, or a hand-off to a nav app that follows a GPX track.
- Build the graph inside the container: osmium has no Alpine wheel yet, so `manage build-graph` runs on the host today.
