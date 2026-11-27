"""The backroad router: our own shortest-path search over an OpenStreetMap
road graph, with a cost that makes time on main roads count several times
over.

- classes.py    which OSM roads exist for us, how fast, and which are "main"
- osm_parse.py    .pbf extract -> drivable ways, streamed (needs osmium)
- build_stream.py ways -> the map, a directory of arrays (mapdata/<name>.graph/),
                  built in bounded memory
- disk.py         arrays on disk: written in chunks, read through memory maps
- graph.py        opens a map (nothing read until used)
- grid.py         the spatial index: roads near a point, a route's corridor
- costs.py        a request's options -> one weight per road
- snap.py         a lat/lon -> a point on the nearest road
- search.py       the search, in the corridor round the route, and its path
- router.py       a whole request, with alternatives, cached
- summarize.py  a path -> what the page shows (times, main-road share, roads)

Only build needs osmium; serving needs numpy and scipy.
"""
