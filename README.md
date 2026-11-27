# Backroad Route Finder

A route planner that, by default, keeps you on the backroads: it works to keep the time spent on main roads (freeways, US routes, major state routes) as low as it can, crossing or briefly using them only where that leads to a better backroad path.

It routes on its own road graph, built from OpenStreetMap. Each minute on a main road counts several times over (8× by default; the slider goes from "Balanced" 3× to "Max" 25× and beyond), so a main road is used only where every way around it is that many times slower. Every route is shown next to the plain fastest route, with the time on main roads for both. Times include turns, traffic signals and stop signs (mapped in OpenStreetMap, or implied where a smaller road meets a bigger one), which is what makes a backroads time realistic: those routes turn and stop far more often.

Any set of US states can be downloaded and built from the Map data screen. See [TODO.md](TODO.md) for what the whole US still needs, and for other engines worth trying.

## Running it

```bash
scripts/run.sh            # starts the server (creates .venv the first time)
scripts/login.sh          # a login link for the owner
```

The map data: the owner opens **Admin → Map data**, ticks any US states (or the whole country), and presses **Build map**. The server downloads the extracts from Geofabrik, builds one map from them (neighbouring states are joined, so trips can cross borders), and switches to it without a restart. Building needs osmium, once:

```bash
.venv/bin/pip install -r app/requirements-build.txt
```

The same from the shell:

```bash
scripts/run.sh manage fetch-osm --state ohio --state indiana
scripts/run.sh manage build-graph --state ohio --state indiana   # writes mapdata/indiana_ohio.graph/
```

Everything lives in `~/.config/backroad-route-finder/mapdata/`, outside `storage/`, so backups leave it out; `active.json` there names the map in use (otherwise `routing.region`).

### Maps stay on disk

A map is a directory of arrays (`<region>.graph/`), read through memory maps rather than loaded, so the server opens it in a fraction of a second and holds only the roads the routes in hand are using. Each route is searched in a corridor round its points, which widens if the way round needs it. On Ohio the server's own memory stays around 300 MB, where it used to be about 1.7 GB. Routes take 0.3–4 s, and about twice that with five alternatives.

Building streams too: node locations go through an on-disk osmium index, and the big sorts and joins work a bucket at a time within `routing.build_memory_mb` (512 MB by default; `--memory-mb` on the command line). Ohio builds in about 2½ minutes with a peak footprint near 580 MB, where it used to need 1.1 GB. The finished map is about 1.4× the extract on disk (Ohio: 330 MB → 430 MB), and building needs about 5× the extract free while it runs. So the whole US (about 15 GB as a map) can be built and served on a machine with 4 GB of RAM, given the disk.

A map from before this format (`<region>.npz`) is converted the first time the server starts, or by hand with `scripts/run.sh manage convert-graph`. The Docker image has osmium compiled in (a build stage of its own in `docker/Dockerfile`), so the Map data screen works there too.

Settings are in `config/config.yaml` under `routing:` (region, default penalty, whether secondary roads count as main, place search), editable from the Settings screen.

## Other routing engines

Beside its own router, the planner can ask other engines (the **Sources** card): Valhalla and OSRM (free public servers, no key), and OpenRouteService, GraphHopper, TomTom, HERE, Mapbox and Google (API keys). Each is asked for its own "avoid main roads" route and its alternatives; every route is then laid on our map and scored the same way (time, plus main-road minutes × the penalty), so **Use the best route** picks the winner whoever planned it, and **Show every route** lists them all, best first.

- **API keys:** everyone has a **Settings** screen whose first card, *Routing engines and API keys*, holds their own keys. The owner also has *Shared keys for everyone*, and chooses per engine whether users may use the shared key or must bring their own (one click sets it for every engine). Someone's own key always wins over the shared one. Keys are stored encrypted in the database (never in config.yaml), re-sealed on key rotation, and never shown again; each row has a **Test** button.
- **Addresses** (free and self-hosted engines) and the timeout: `providers:` in `config/config.yaml`.
- Answers are cached for 15 minutes, so moving the slider does not spend a free tier's quota.
- Code: `app/routing/providers/` (one module per engine), `app/routing/match.py` (lays a route on our map), `app/routing/sources.py` (asks them all and combines).

## How it works

- `app/routing/`: the router. `osm_parse.py` and `build_stream.py` turn the extract into arrays on disk (`mapdata/ohio.graph/`, see `disk.py`). `graph.py` maps them. `grid.py` is the spatial index (roads near a point, a route's corridor). `costs.py` weights each road for a request. `turns.py` prices every turn. `search.py` runs scipy's Dijkstra over the turns. `summarize.py` works out the main-road share and the roads used.
- `app/routes/planner.py` and `saved_routes.py`: the API (see `config/api.md`).
- `static/js/planner.js`, `map.js` (Leaflet, vendored), `export.js`, `saved_routes.js`, `account_data.js`: the screens.

## Alternatives, exports and your data

- **Alternative routes:** the planner offers up to `routing.alternatives` routes (5 by default; *Routes to offer* on the result card), best first. Our router finds alternatives by counting the roads it already used 1.4× over and searching again, and keeps a route only if it shares under 70% of its length with the ones kept and scores within 1.5× of the best. Hover a route in the list to see it on the map, or click a line to pick it. Plans are cached on the server (15 min) and in the browser (the last 20), so going back to a request is instant, with the route you picked. A saved route remembers which alternative was picked.
- **Exports** (the *Export* menu): GPX track, GPX route (a shaping point every km, for Garmin and TomTom units that want a route), Garmin course (TCX), TomTom itinerary (ITN), KML (Google Earth, My Maps), GeoJSON, CSV, and a Google Maps link (up to 9 points along the way; Google plans between them).
- **Your data** (Settings): one JSON file with your saved routes, planner defaults, shortcuts and themes, to download, or to load into this or another account (`GET /api/account/export`, `POST /api/account/import`). Loading skips routes you already have. API keys are left out.

Built from the python-webserver template: sign-in by login link, optional two-factor, everything stored encrypted in one SQLite file.
