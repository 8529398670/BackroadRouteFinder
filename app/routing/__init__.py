"""The backroad router: our own shortest-path search over an OpenStreetMap
road graph, with a cost that makes time on main roads count several times
over.

- classes.py    which OSM roads exist for us, how fast, and which are "main"
- osm_parse.py  .pbf extract -> flat arrays of drivable ways (needs osmium)
- build.py      ways -> the graph file (<storage>/graph/<name>.npz)
- graph.py      loads the graph file once and holds it
- costs.py      a request's options -> one weight per edge
- snap.py       a lat/lon -> a point on the nearest road
- search.py     the search itself, and the path it finds
- summarize.py  a path -> what the page shows (times, main-road share, roads)

Only build needs osmium; serving needs numpy and scipy.
"""
