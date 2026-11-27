# TODO

## Expand from Ohio to the whole USA
Done: maps live on disk (memory-mapped, searched in a corridor per route) and build in bounded memory (`routing.build_memory_mb`), so the US fits a 4 GB machine. Next:
- Build the whole US once on the target machine and time it (Ohio: 2½ min at 512 MB; expect hours for the US, and about 60 GB free disk while it runs).
- Long trips: a cross-country corridor reads millions of roads. A coarse first pass on main roads only (or contraction of the backroads between junctions) would bound it.
- Give place search the new area (it already uses the graph's bounding box).

## Other routing engines
Done: Valhalla, OSRM, OpenRouteService, GraphHopper, TomTom, HERE, Mapbox and Google can be asked beside our own router (app/routing/providers/), each route measured on our map and scored the same way. Next:
- Stadia Maps (hosted Valhalla with a key, more options than the public server).
- Use other engines' routes as hints for ours (e.g. a corridor), not only as competitors.
- Self-host GraphHopper with a backroads custom model, so it is not limited by a free tier.

### Notes from before, on each engine
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
- Speed: a route reads its corridor's roads from disk and searches them (0.3–4 s across Ohio). A* or a bidirectional search inside the corridor would cut that.
- Snap a start/destination to the road it is on, not the nearest one, when a point sits between two parallel roads (driveway-aware).
- Scenic or "curvy" scoring: prefer winding roads, rivers, forests (sinuosity from the geometry, landuse from OSM).
- Turn-by-turn directions from the pieces (the "via" list is a start).
- Avoid a road or area the user draws on the map.
- Live traffic and closures (needs a data source).

## App
- Scheduled job (`registry.register`) to re-download the extract and rebuild the graph monthly, loading the new graph without a restart.
- Self-hosted map tiles (a vector-tile server or a pre-rendered set), so the page needs nothing from tile.openstreetmap.org.
- Send to phone: a share link, or a hand-off to a nav app that follows a GPX track.
