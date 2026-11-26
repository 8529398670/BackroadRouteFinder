# Backroad Route Finder

A route planner that, by default, keeps you on the backroads: it works to keep the time spent on main roads (freeways, US routes, major state routes) as low as it can, crossing or briefly using them only where that leads to a better backroad path.

It routes on its own road graph, built from OpenStreetMap. Each minute on a main road counts several times over (8× by default; the slider goes from "Balanced" 3× to "Max" 25× and beyond), so a main road is used only where every way around it is that many times slower. Every route is shown next to the plain fastest route, with the time on main roads for both. Times include turns, traffic signals and stop signs (mapped in OpenStreetMap, or implied where a smaller road meets a bigger one), which is what makes a backroads time realistic: those routes turn and stop far more often.

Ohio only for now. See [TODO.md](TODO.md) for expanding to the USA, and for other engines worth trying.

## Running it

```bash
scripts/run.sh            # starts the server (creates .venv the first time)
scripts/login.sh          # a login link for the owner
```

The map data, once (and whenever you want fresher roads):

```bash
.venv/bin/pip install -r app/requirements-build.txt   # osmium, for the build only
scripts/run.sh manage fetch-osm                        # ~330 MB from Geofabrik
scripts/run.sh manage build-graph                      # ~1 minute, ~1 GB of memory
scripts/run.sh                                         # restart to load it
```

Both files live in `~/.config/backroad-route-finder/mapdata/`, outside `storage/`, so backups leave them out. For Docker, build the graph on the host and copy `ohio.npz` into `data/mapdata/` (`docker.data_dir`), since osmium has no Alpine wheel.

Settings are in `config/config.yaml` under `routing:` (region, default penalty, whether secondary roads count as main, place search), editable from the Settings screen.

## How it works

- `app/routing/`: the router. `osm_parse.py` and `build.py` turn the extract into arrays (`mapdata/ohio.npz`). `graph.py` loads them. `costs.py` weights each road for a request. `turns.py` prices every turn. `search.py` runs scipy's Dijkstra over the turns. `summarize.py` works out the main-road share and the roads used.
- `app/routes/planner.py` and `saved_routes.py`: the API (see `config/api.md`).
- `static/js/planner.js`, `map.js` (Leaflet, vendored), `gpx.js`, `saved_routes.js`: the screens.

Built from the python-webserver template: sign-in by login link, optional two-factor, everything stored encrypted in one SQLite file.
