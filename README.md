# Fuel Route Optimization API

A Django REST API that plans fuel stops for a truck driving between two places in the USA.
Give it a start and a finish; it returns the driving route, a map, the most cost-effective
places to refuel along the way, and the total money spent on fuel.

**Contents**

1. [Quick start](#1-quick-start)
2. [What the application does](#2-what-the-application-does)
3. [Features](#3-features)
4. [How it works, step by step](#4-how-it-works-step-by-step)
5. [Tech and tools](#5-tech-and-tools)
6. [API reference](#6-api-reference)
7. [Planning rules and why](#7-planning-rules-and-why)
8. [Fuel price data](#8-fuel-price-data)
9. [Testing](#9-testing)
10. [Configuration](#10-configuration)
11. [Project structure](#11-project-structure)
12. [Known limits](#12-known-limits)

---

## 1. Quick start

### Requirements

- **Python 3.12 or newer** (Django 6.1 needs it). Check with `python3 --version`
  (Windows: `python --version`).
- **Git**.
- **An internet connection** while running: each new route is fetched from the free OSRM
  routing service, and the map tiles come from OpenStreetMap.
- *Optional:* **Node.js**, only to run the Postman tests from the terminal.

No API keys, no external database and no paid services are needed.

### Set up and run

```bash
# 1. Get the code
git clone https://github.com/Faizan404/fuel-route-optimization-api.git
cd fuel-route-optimization-api

# 2. Create and activate a virtual environment
python3 -m venv .venv                # Windows: python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 3. Install the dependencies
pip install -r requirements.txt

# 4. Create the database tables
python manage.py migrate

# 5. Load the fuel stations and US places into the database
python manage.py load_data           # prints: Loaded 6572 stations and 32600 places.

# 6. Start the server
python manage.py runserver
```

Step 2 uses `python3`, because on macOS and Linux plain `python` may be missing or be Python 2.
Once the virtual environment is active, `python` and `pip` point to its own Python 3 on every
system, so the remaining steps use `python`.

Then open **<http://127.0.0.1:8000/>**. Type a start and a finish (for example `Chicago, IL` and
`Miami, FL`) and press **Plan route**.

### Try the API

```bash
curl -G http://127.0.0.1:8000/api/v1/route/ \
  --data-urlencode "start=Chicago, IL" --data-urlencode "finish=Miami, FL"
```

### Run the tests

```bash
python -m pytest                     # 125 tests; no server or internet needed
```

### If something goes wrong

| Problem | Fix |
|---|---|
| `no such table: fuelplan_station` | Run steps 4 and 5 (`migrate`, then `load_data`). |
| `That port is already in use` | Use another port: `python manage.py runserver 8001`. |
| Install fails or Django won't start | Check you have Python 3.12+ and the virtual environment is active. |
| `routing_service_unavailable` (502) | The free OSRM server is busy or down. Try again shortly. |
| The map shows no tiles | You are offline, or a browser extension blocks `tile.openstreetmap.org`. |

---

## 2. What the application does

A dispatcher wants to know, before a trip starts, **where the truck should refuel and what
the fuel will cost**. The application answers that for any two places in the USA.

The fixed assumptions:

| Assumption | Value |
|---|---|
| Range on a full tank | 500 miles (a 50-gallon tank) |
| Fuel economy | 10 miles per gallon |
| Tank at departure | Full |
| Fuel prices | The provided file `fuel-prices-for-be-assessment.csv` (truck stops with diesel prices) |

On top of these, the plan follows a few rules a fleet would expect (all explained in
[section 7](#7-planning-rules-and-why)):

- Each stop is "charged" **$3** of driver time, so the truck does not stop to save a few cents.
- The truck never arrives at a fuel stop with less than **7.5 gallons** (a 75-mile safety reserve).
- No stop buys less than **5 gallons**.
- At the destination the truck still has enough fuel to reach a station, and at least **2.5 gallons**.

---

## 3. Features

**API**

- **Route planning:** one endpoint returns the route, the fuel stops, gallons and cost per stop,
  fuel left at each stop and at the destination, and the trip totals.
- **Cost-effective stops:** an exact optimiser picks where to stop and how much to buy at each
  stop. Each stop also explains its decision ("fill up", "just enough", "finish").
- **One routing call per trip:** the route is fetched once from OSRM and cached, so repeating a
  trip makes no external call at all.
- **Flexible input:** `City, ST`, `City, State`, a city on its own when the name is unique, or
  `lat,lon` coordinates (checked to be inside the USA).
- **Location suggestions:** a second endpoint suggests cities as the user types, ranked by
  population, and copes with spelling mistakes (`Chcago` → Chicago, IL) and state names (`FL`).
- **Clear errors:** every error has the same JSON shape, names the field at fault and suggests
  what the user may have meant.
- **Rule overrides per request:** stop cost, safety reserve and minimum purchase can be changed
  in the query string.
- **Protection:** per-client rate limiting that cannot be bypassed with a spoofed header.

**Map page** (at `/`)

- Route, numbered fuel stops, start and finish drawn on an OpenStreetMap map.
- Side panel with the totals, fuel left at the destination and the list of stops; clicking a
  stop zooms the map to it.
- Location suggestions as you type, with keyboard support.
- **Journey playback:** press *Play journey* to watch the plan: it scans the route for stations,
  shows the chosen stops, then drives the trip with a live distance, fuel gauge and fuel spend,
  pausing at each stop to explain the decision. Pause, restart and 1×/2×/4× speed.
- Light and dark themes, a layout that works on phones, and readable error messages.

---

## 4. How it works, step by step

### One-time data preparation (already done; files are in `data/`)

The price file has no coordinates and its addresses are highway exits such as
`I-44, EXIT 283 & US-69`, which normal address lookups cannot place. So the stations are
prepared once, offline:

1. **Clean** the file: trim spaces, drop the 620 Canadian rows, merge rows that belong to the same station.
2. **Locate** each station at the centre of its city, using free US Census and GeoNames place lists.
3. **Fix** city centres that the Census places in the water (for example San Francisco) using the US state outlines.
4. **Load** the results into the database with `python manage.py load_data`.

### What happens on each request

```
start, finish
     │
     ▼
1. Resolve both places ────────── Place table (no external call)
     │
     ▼
2. Check the cache ───────────── same trip planned in the last hour? return it
     │
     ▼
3. Get the driving route ─────── one call to OSRM (cached 24 h)
     │
     ▼
4. Find stations along it ────── Station table, within 5 miles of the road
     │
     ▼
5. Work out fuel needed at the finish (nearest station to the destination)
     │
     ▼
6. Optimise the stops ────────── exact dynamic programme
     │
     ▼
7. Build the response ────────── stops, gallons, costs, totals, map link
```

1. **Resolve the places.** `Chicago, IL` is looked up in the `Place` table (about 32,600 US
   cities and towns) to get coordinates. Coordinates typed directly are checked against the
   US state outlines. A typo returns an error with suggestions.
2. **Check the cache.** If the same trip was planned in the last hour, the saved plan is returned
   immediately.
3. **Get the route.** One call to OSRM returns the fastest driving route: its shape, distance and
   driving time. The route is cached for 24 hours. A failed call is retried once.
4. **Find the stations along the route.** Stations near the route are read from the database, and
   each one within 5 miles of the road gets a *mile marker*: how far along the trip it is. If the
   stations are too sparse, the search widens to 10, 25 and then 50 miles.
5. **Work out the fuel needed at the finish.** The nearest listed station to the destination
   decides how much fuel the truck must still have when it arrives.
6. **Optimise.** If a full tank covers the trip with that margin, there is no stop. Otherwise the
   optimiser compares all sensible combinations of stops and purchase amounts and picks the plan
   with the lowest *fuel cost + $3 per stop* that keeps every rule. It runs in well under 100 ms.
7. **Respond.** Each stop gets its gallons, price, cost, fuel on arrival and after filling, and the
   reason for the purchase. The summary adds the totals. The map page draws it.

A new coast-to-coast request takes about 1–2.5 seconds, most of it the routing call. A repeated
request takes a few milliseconds.

---

## 5. Tech and tools

| Area | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Required by Django 6.1 |
| Web framework | Django 6.1.3 | Latest stable release, as the brief asks |
| API | Django REST Framework 3.18.3 | Request validation, throttling, consistent JSON errors |
| Database | SQLite | Zero setup for the reviewer; holds the stations and places |
| Cache | Django local-memory cache | Repeat trips skip the routing call |
| Routing | [OSRM](https://project-osrm.org/) public server | Free, no API key, fast; one call per trip |
| Map | [Leaflet](https://leafletjs.com/) 1.9.4 + OpenStreetMap tiles | Free, no key; plain JavaScript, no build step |
| HTTP client | `requests` | The single call to OSRM |
| Optimiser | Own code (`fuelplan/services/optimizer.py`) | Exact algorithm for this problem; no solver dependency |
| Geodata | US Census Gazetteer, GeoNames, Census state outlines | Free, offline; used once to place the stations |
| Tests | pytest + pytest-django | 125 tests, routing calls replaced by recorded responses |
| API testing | Postman collection (+ Newman) | 17 requests with 43 automatic checks |

---

## 6. API reference

### `GET /api/v1/route/` — plan a trip

```
GET /api/v1/route/?start=Chicago, IL&finish=Miami, FL
```

`POST /api/v1/route/` with a JSON body `{"start": "...", "finish": "..."}` works the same way.

| Parameter | Required | Default | Allowed | Meaning |
|---|---|---|---|---|
| `start` | yes | | | Where the trip begins |
| `finish` | yes | | | Where the trip ends |
| `stop_cost` | no | `3` | 0–50 | Dollars of driver time charged per stop |
| `reserve_miles` | no | `75` | 0–200 | Fuel (in miles) the truck must still have on arrival at each stop |
| `min_purchase_gallons` | no | `5` | 0–40 | Smallest purchase allowed at a stop |

A location can be written as `City, ST` (`Chicago, IL`), `City, State` (`Chicago, Illinois`), a city
on its own when only one place has that name (`Chicago`), or `lat,lon` (`41.8781,-87.6298`).

`stop_cost=0&reserve_miles=0&min_purchase_gallons=0` gives the pure cheapest-fuel plan.

**Response** (Chicago → Miami, shortened):

```json
{
  "start":  {"query": "Chicago, IL", "resolved": "Chicago, IL", "latitude": 41.837045, "longitude": -87.684939},
  "finish": {"query": "Miami, FL", "resolved": "Miami, FL", "latitude": 25.775163, "longitude": -80.208615},
  "vehicle": {"max_range_miles": 500.0, "miles_per_gallon": 10.0, "tank_at_departure": "full",
              "reserve_gallons": 7.5},
  "route": {
    "distance_miles": 1344.4,
    "duration_hours": 25.35,
    "geometry": {"type": "LineString", "coordinates": [[-87.68489, 41.83705], "..."]}
  },
  "fuel_stops": [
    {
      "sequence": 2, "station_id": "6180", "name": "Circle #4703923",
      "address": "I-24, EXIT 180B & US-27", "city": "Chattanooga", "state": "TN",
      "latitude": 35.065958, "longitude": -85.248386,
      "mile_marker": 566.2, "distance_from_route_miles": 2.3,
      "price_per_gallon": 2.786, "gallons_purchased": 41.5, "cost": 115.62,
      "fuel_on_arrival_gallons": 8.5, "fuel_after_fill_gallons": 50.0,
      "decision": {"action": "fill_up", "in_range_count": 30,
                   "next_stop": {"name": "CIRCLE K #2745064", "city": "Cordele", "state": "GA",
                                 "mile_marker": 824.4, "price_per_gallon": 2.999, "...": "..."}}
    }
  ],
  "stations_on_route": [
    {"station_id": "...", "name": "...", "city": "...", "state": "...",
     "latitude": 41.5, "longitude": -87.6, "mile_marker": 12.4, "price_per_gallon": 3.159}
  ],
  "summary": {
    "total_distance_miles": 1344.4,
    "fuel_consumed_gallons": 134.44,
    "fuel_purchased_gallons": 92.95,
    "fuel_left_at_finish_gallons": 8.51,
    "total_fuel_cost": 269.3,
    "trip_fuel_cost": 389.88,
    "number_of_stops": 4,
    "stop_cost_usd": 3.0,
    "total_cost_including_stop_cost": 281.3,
    "safety_reserve_miles": 75.0,
    "finish_reserve_miles": 75.0,
    "nearest_station_to_finish": {"name": "SHELL DBA PILOT DEALER #874", "city": "Miami",
                                  "state": "FL", "miles": 0.0},
    "min_purchase_gallons": 5.0,
    "stations_considered": 75,
    "search_corridor_miles": 5.0
  },
  "notes": [],
  "map_url": "http://127.0.0.1:8000/api/v1/route/map/?start=Chicago%2C+IL&finish=Miami%2C+FL"
}
```

**Key fields**

| Field | Meaning |
|---|---|
| `route.geometry` | The route as GeoJSON, ready for any map library |
| `fuel_stops[].mile_marker` | How far along the route the stop is |
| `fuel_stops[].cost` | `gallons_purchased × price_per_gallon`, an exact receipt |
| `fuel_stops[].decision.action` | `fill_up` (the next stop is dearer, so leave full), `just_enough` (the next stop is no dearer, so buy only enough to reach it with the reserve left) or `finish` (the last stop buys what is needed to arrive) |
| `stations_on_route` | Every station the optimiser compared, sorted by mile marker |
| `summary.total_fuel_cost` | Money spent on fuel bought on the road (the sum of the stop costs) |
| `summary.trip_fuel_cost` | Money for all the fuel the trip burns: `total_fuel_cost` plus the fuel used from the starting tank, priced at the first stop's price (or the mean price along the route when there is no stop) |
| `summary.total_cost_including_stop_cost` | `total_fuel_cost` + $3 × stops |
| `summary.fuel_left_at_finish_gallons` | Fuel in the tank on arrival |
| `summary.finish_reserve_miles` | Fuel the plan keeps for after arriving; see [section 7](#7-planning-rules-and-why) |
| `summary.nearest_station_to_finish` | The listed station that figure is based on (`null` if none within 300 miles) |
| `notes` | Plain-language remarks, e.g. "no fuel stop is needed" or "refuel at a local station" |
| `map_url` | Opens the map page for this trip |

`start.query` and `finish.query` always echo the text of the current request, even when the
plan comes from the cache.

### `GET /api/v1/locations/?q=<text>` — location suggestions

Up to 8 places matching what has been typed, ranked by population.

| Input | Result |
|---|---|
| `chi` | Chicago, IL first, then other places starting with "chi" |
| `FL` or `Florida` | The largest cities in Florida |
| `miami, f` | Places called "miami" in states starting with "f" |
| `Chcago`, `Maimi` | Chicago, IL and Miami, FL (spelling mistakes and swapped letters) |

```json
{"query": "chi", "results": [{"label": "Chicago, IL", "name": "Chicago", "state": "IL",
                              "latitude": 41.837045, "longitude": -87.684939}]}
```

### `GET /` and `GET /api/v1/route/map/` — the map page

The browser page described in [Features](#3-features). `?start=…&finish=…` in its address plans
that trip straight away.

### Errors

Every error has the same shape. `field` and `suggestions` appear when one input is at fault:

```json
{"error": {"code": "location_not_resolved",
           "message": "Could not find 'Chcago' in Illinois. Did you mean Chicago, IL?",
           "field": "start", "suggestions": ["Chicago, IL"]}}
```

| Status | Code | When |
|---|---|---|
| 400 | `invalid_request` | `start` or `finish` missing, both the same place, a rule override out of range, or malformed JSON |
| 400 | `location_not_resolved` | Place not found, ambiguous without a state, or outside the USA |
| 404 | `not_found` | Unknown API path |
| 405 | `method_not_allowed` | Unsupported HTTP method |
| 415 | `invalid_request` | Unsupported request body type |
| 422 | `route_not_found` | No driving route exists (e.g. Hawaii to the mainland) |
| 422 | `no_feasible_fuel_plan` | Somewhere on the route the listed stations are further apart than a tank can cover. The route is still returned (see below) |
| 429 | `rate_limited` | Too many requests; the message says how long to wait |
| 500 | `internal_error` | Unexpected failure |
| 502 | `routing_service_unavailable` | OSRM is down or timing out (after one retry) |

**When the route exists but no fuel plan is possible** (`no_feasible_fuel_plan`), the response still
contains the trip, so it can be shown with the reason. Next to `error` it has `start`, `finish`,
`route` (distance, driving time, GeoJSON line), `stations_on_route` (the listed stations that do
exist) and `gap`:

```json
"gap": {"from_mile": 0.0, "to_mile": 876.6, "miles": 876.6, "refuel_before_mile": 425.0,
        "search_corridor_miles": 50.0, "geometry": {"type": "LineString", "coordinates": ["..."]}}
```

`refuel_before_mile` is where the truck would need fuel from a station not in the price list. No
fuel stops or costs are returned, because a plan that strands the truck is not presented as one.
The map page draws the route, marks the gap in red and shows the warning.

---

## 7. Planning rules and why

| Rule | Value | Why |
|---|---|---|
| Stop cost | $3 per stop | A stop takes 15–30 minutes of driving time. Without it, the cheapest plan stops for any saving (with no rules at all, Seattle to Miami made 20 stops, one buying 0.76 gallons). With the other rules in place, the stop cost cuts Seattle to Miami from 16 stops to 11 for $3.52 (0.4%) more fuel. |
| Safety reserve | 75 miles (7.5 gal) on arrival at every stop | Stations are placed at city centres, a pump may be closed, and real economy can be worse than 10 mpg. In practice stops are reached with 8.5 gallons, because of the detour allowance below. |
| Minimum purchase | 5 gallons | A guard against residual tiny stops. At the last stop a smaller purchase is rounded up to 5 gallons. |
| Fuel at the destination | Enough to reach the nearest listed station (1.25 × the straight-line miles), at least 25 miles, and at least the 75-mile reserve on a trip that stops | The truck must be able to refuel after arriving, with a margin for poor economy. If a destination is too far from every listed station (Bakersfield, CA is 201 miles from the nearest), the trip is still planned with the 25-mile margin and a note to refuel locally. |
| Detour allowance | 2 × the corridor width (10 miles) | A station can be up to 5 miles off the road, so the trip there and back is held out of the range: no leg can exceed 500 real miles. |
| Departure | Full tank | The fuel already in the tank has no price in the file, so `total_fuel_cost` counts what is bought on the road; `trip_fuel_cost` prices the whole trip. |

**The optimiser** (`fuelplan/services/optimizer.py`) solves the fixed-path "gas station problem"
(Khuller, Malekian and Mestre, *To Fill or Not to Fill*) with an exact dynamic programme. The key
fact: between two consecutive stops, the cheapest plan either leaves the first one with a full
tank (the next one is dearer) or arrives at the next one empty (it is no dearer). That keeps the
number of situations to compare small.

- **Stop cost and reserve keep it exact.** A cost per stop depends only on which stops are chosen,
  so the key fact still holds. The reserve just shifts every fuel level down by the same amount.
- **The minimum purchase is a guard, not proven optimal.** It can make buying *more* than needed
  the best choice, which falls outside the situations compared, so the result may be slightly
  dearer than the true best. If the minimum makes a trip impossible, it is dropped and `notes`
  says so.

The optimiser is checked against a brute-force solver that tries every possibility (see
[Testing](#9-testing)).

---

## 8. Fuel price data

The provided file has 8,151 rows: one diesel price per row, with the station's OPIS ID, name,
address, city, state and rack ID. It has no coordinates and no date.

| Step | Result |
|---|---|
| Rows read | 8,151 |
| Canadian rows dropped (outside the USA) | 620 |
| Unique US stations | 6,626 |
| Located at the centre of their city | 6,572 |
| Left out, listed in `data/stations_unresolved.csv` | 54 |

- A station listed several times with different prices gets the mean, because the file has no date to say which is current.
- The 54 left out: 6 whose city is in no place list, and 48 whose city name matches several places more than 10 miles apart.
- Station positions are city centres, not the pump, which is why stations are matched within a corridor around the road.
- Census centres that fall in the water (San Francisco's is 34 miles offshore; also Corpus Christi, Portland ME and others) are moved to the nearest same-named GeoNames point inside the state: 35 places and 7 stations.

**Rebuilding the data** is only needed if the price file changes; the generated files are in
`data/`. Download into a `gazetteers/` folder:

- `2024_Gaz_place_national.txt` and `2024_Gaz_cousubs_national.txt` from the [US Census Gazetteer](https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/) (public domain)
- `US.txt` from [GeoNames](https://download.geonames.org/export/dump/) (CC BY 4.0)
- `cb_2023_us_state_5m.shp` and `.dbf` from the [US Census boundary files](https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_5m.zip) (public domain)

Then run, in this order:

```bash
python manage.py build_state_outlines --shapefile gazetteers/cb_2023_us_state_5m.shp
python manage.py build_station_data --gazetteers gazetteers
python manage.py load_data
```

| Command | Writes |
|---|---|
| `build_state_outlines` | `data/us_states.json`: US state outlines, used to check `lat,lon` input and to find offshore city centres |
| `build_station_data` | `data/stations.csv`, `data/us_places.csv`, `data/stations_unresolved.csv` |
| `load_data` | The `Station` and `Place` database tables (replaced in one transaction, safe to rerun) |

---

## 9. Testing

### Automated tests

```bash
python -m pytest
```

125 tests, no server or internet needed (the routing call is replaced by recorded OSRM responses
in `fuelplan/tests/fixtures/`):

- **Optimiser:** compared with a brute-force solver on 400 random cases, and with stop cost,
  reserve and fuel at the finish on 500 more; plus the minimum-purchase guard and its fallback.
- **Locations:** lookup, suggestions, spelling mistakes, coordinates inside and outside the USA.
- **Data:** the station and place tables after loading.
- **API:** every endpoint and every error, rounding, caching, rate limiting, and real routes
  (Chicago → Miami, New York → Chicago, Indianapolis → Kansas City, routes to Bakersfield and more).

### Postman

The collection `postman/fuel-route-api.postman_collection.json` has 17 requests in three folders
(route plans, location suggestions, errors), each with automatic checks (43 in total).

- **In Postman:** use **Import** to load the file, start the server, and run a request or the whole
  collection (**Run collection**). The `base_url` variable is `http://127.0.0.1:8000`.
- **In the terminal** (needs Node.js), with the server running:

  ```bash
  npx newman run postman/fuel-route-api.postman_collection.json
  ```

---

## 10. Configuration

All settings work with their defaults. Override them with environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | development value | Set a real secret in production |
| `DJANGO_DEBUG` | `0` | `1` turns on debug mode |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated host names |
| `DATABASE_PATH` | `db.sqlite3` | SQLite database file |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | Routing server |
| `ROUTING_TIMEOUT_SECONDS` | `15` | Timeout per routing attempt |
| `API_THROTTLE_RATE` | `60/min` | Route plans per client (clients are identified by their connecting address; `X-Forwarded-For` is ignored, so it cannot be used to reset the limit) |
| `SUGGEST_THROTTLE_RATE` | `600/min` | Location suggestions per client |

The vehicle and planning values (`TANK_RANGE_MILES`, `MPG`, `STOP_COST_USD`,
`SAFETY_RESERVE_MILES`, `FINISH_MIN_MILES`, `MIN_PURCHASE_GALLONS`, corridor widths, cache times)
are in `FUEL_PLAN` in `config/settings.py`.

---

## 11. Project structure

### Overview

```
fuel-route-optimization-api/
├── README.md
├── requirements.txt
├── manage.py
├── pytest.ini
├── .gitignore
├── fuel-prices-for-be-assessment.csv
│
├── config/                         Django project configuration
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
│
├── data/                           Prepared data, generated once and committed
│   ├── stations.csv
│   ├── stations_unresolved.csv
│   ├── us_places.csv
│   └── us_states.json
│
├── fuelplan/                       The application
│   ├── __init__.py
│   ├── apps.py
│   ├── models.py
│   ├── serializers.py
│   ├── views.py
│   ├── urls.py
│   ├── exceptions.py
│   ├── migrations/
│   │   ├── __init__.py
│   │   └── 0001_initial.py
│   ├── services/                   The business logic
│   │   ├── __init__.py
│   │   ├── planner.py
│   │   ├── optimizer.py
│   │   ├── routing.py
│   │   ├── route_line.py
│   │   ├── gazetteer.py
│   │   ├── borders.py
│   │   ├── geo.py
│   │   └── errors.py
│   ├── management/commands/        Data commands run with manage.py
│   │   ├── build_state_outlines.py
│   │   ├── build_station_data.py
│   │   └── load_data.py
│   ├── templates/fuelplan/
│   │   └── map.html
│   └── tests/
│       ├── conftest.py
│       ├── test_api.py
│       ├── test_locations.py
│       ├── test_optimizer.py
│       └── fixtures/               Recorded OSRM responses (osrm_*.json)
│
└── postman/
    └── fuel-route-api.postman_collection.json
```

### Root files

| File | What it does |
|---|---|
| `README.md` | This document. |
| `requirements.txt` | The Python packages to install: Django, Django REST Framework, `requests`, and pytest with pytest-django for the tests. |
| `manage.py` | Django's command-line entry point: `runserver`, `migrate`, `load_data`, and the data-build commands. |
| `pytest.ini` | Tells pytest which Django settings to use and which files contain tests. |
| `.gitignore` | Keeps local-only files out of git: the virtual environment, the SQLite database, downloaded source data and caches. |
| `fuel-prices-for-be-assessment.csv` | The price file provided with the brief. It is the input for `build_station_data`; the running app reads the cleaned copy in the database, not this file. |

### `config/`: Django project configuration

| File | What it does |
|---|---|
| `settings.py` | All settings: installed apps, database, cache, rate limits, error handling, security headers, the routing server (`ROUTING`) and the vehicle and planning rules (`FUEL_PLAN`). Values can be overridden with environment variables (see [Configuration](#10-configuration)). |
| `urls.py` | The top-level URL map. It includes the app's URLs and points 404 and 500 errors at handlers that return JSON. |
| `wsgi.py` | The entry point a production web server (for example Gunicorn) uses to run the app. |
| `__init__.py` | Marks the folder as a Python package. |

### `data/`: prepared data

Generated once by the data commands ([section 8](#8-fuel-price-data)) and committed, so nothing needs
to be downloaded to run the app.

| File | What it does |
|---|---|
| `stations.csv` | One row per located US station: OPIS ID, name, address, city, state, rack ID, price, how many price rows it had, latitude, longitude, and which place list located it. `load_data` loads it into the `Station` table. |
| `stations_unresolved.csv` | The 54 stations that could not be placed, with the reason. An audit list; it is not loaded. |
| `us_places.csv` | About 32,600 US cities and towns with a search key, coordinates and population. `load_data` loads it into the `Place` table, used to resolve start and finish and for suggestions. |
| `us_states.json` | Outlines of the 50 states and DC. Read directly by `borders.py` to check that a `lat,lon` is in the USA, and used by `build_station_data` to find city centres that fall in the water. |

### `fuelplan/`: the application

| File | What it does |
|---|---|
| `models.py` | The two database tables: `Station` (fuel stations and prices) and `Place` (US cities and towns), with indexes for the map-area and name lookups. |
| `migrations/0001_initial.py` | Creates those tables when you run `python manage.py migrate`. |
| `serializers.py` | Validates the route request: `start`, `finish`, and the optional rule overrides with their allowed ranges. |
| `views.py` | The endpoints: `RoutePlanView` (`/api/v1/route/`, GET and POST), `LocationSuggestView` (`/api/v1/locations/`) and the map page (`/` and `/api/v1/route/map/`). |
| `urls.py` | Maps the app's URLs to those views. |
| `exceptions.py` | Turns every error (validation, rate limit, unknown path, unexpected failure) into the same `{"error": {...}}` JSON shape. |
| `apps.py` | Registers the app with Django. |
| `templates/fuelplan/map.html` | The whole front end in one file: the form with suggestions, the Leaflet map, the results panel, the journey playback and the themes. It calls the API from the browser. |

### `fuelplan/services/`: the business logic

| File | What it does |
|---|---|
| `planner.py` | Runs one request from start to finish: resolves the places, checks the cache, fetches the route, finds the stations along it, works out the fuel needed at the destination, calls the optimiser (widening the search if stations are sparse) and builds the response. When no plan is possible it still returns the route and the gap. |
| `optimizer.py` | The fuel-stop optimiser: an exact dynamic programme that picks the stops and the gallons at each, with the stop cost, safety reserve, fuel at the finish and minimum purchase. |
| `routing.py` | The single call to OSRM: builds the request, retries once on failure, caches the route for 24 hours and turns OSRM problems into clear errors. |
| `route_line.py` | Works with the route's shape: gives every point a mile marker, finds how far a station is from the road and where along the trip it is, and outputs GeoJSON for the route and the gap. |
| `gazetteer.py` | Turns what the user typed (`City, ST`, `City, State`, a city name or `lat,lon`) into coordinates, and produces suggestions, including for spelling mistakes and state names. |
| `borders.py` | Answers "which US state is this point in, if any?" using the state outlines. |
| `geo.py` | Shared helpers: distance between two points, decoding OSRM's route shape, normalising place names (`St.` = `Saint`) and the list of US states. |
| `errors.py` | The error types, each with its HTTP status and code (for example `location_not_resolved`, `no_feasible_fuel_plan`). |

### `fuelplan/management/commands/`: data commands

Run with `python manage.py <command>`.

| File | What it does |
|---|---|
| `load_data.py` | Loads `data/stations.csv` and `data/us_places.csv` into the database, replacing both tables in one transaction. This is the only one needed for setup. |
| `build_station_data.py` | One-time: cleans the price file, locates each station, moves offshore city centres onto land and writes the files in `data/`. |
| `build_state_outlines.py` | One-time: converts the Census state shapefile into `data/us_states.json`, using a small pure-Python reader so no GIS library is needed. |

### `fuelplan/tests/`: automated tests

| File | What it does |
|---|---|
| `conftest.py` | Test setup: loads the real station and place data into the test database once and clears the cache between tests. |
| `test_optimizer.py` | Checks the optimiser against a brute-force solver that tries every possibility, plus each planning rule. |
| `test_locations.py` | Place lookup, suggestions, spelling mistakes, the USA border check, offshore city centres and the loaded data. |
| `test_api.py` | Every endpoint and error, rounding, caching, rate limiting, and real routes. |
| `fixtures/osrm_*.json` | Recorded OSRM responses for 9 real routes (for example Chicago → Miami, Los Angeles → Seattle), so the tests run offline and give the same result every time. |

### `postman/`

| File | What it does |
|---|---|
| `fuel-route-api.postman_collection.json` | 17 ready-made requests with automatic checks, to import into Postman or run with Newman (see [Testing](#9-testing)). |

### Created locally, not in the repository

| Path | What it is |
|---|---|
| `.venv/` | The virtual environment created in [Quick start](#1-quick-start). |
| `db.sqlite3` | The SQLite database created by `migrate` and filled by `load_data`. |
| `gazetteers/` | Source data downloaded only if you rebuild the files in `data/`. |
| `__pycache__/`, `.pytest_cache/` | Caches created by Python and pytest. |

---

## 12. Known limits

- **Sparse states.** The file has 8 stations in California, all in the south-east desert, so a trip
  such as Los Angeles to Seattle has no station for its first 877 miles and gets no fuel plan. The
  route is still shown, with the gap marked and the reason. That reflects the data.
- **Station positions are city centres**, not the exact pump; the address (exit number) tells the
  driver which station to use.
- **Fastest, not shortest.** OSRM returns the fastest driving route, which is usually but not
  always the shortest in miles.
- **Routes through Canada.** OSRM may route some trips (e.g. Detroit to Buffalo) through Ontario.
  Only US stations are used.
- **Borders.** The US outlines are a 1:5,000,000 map, so a `lat,lon` within a few hundred metres
  of the border can be misjudged.
- **Cached plans.** A plan is cached for an hour, so a price changed in the database shows up in
  repeat requests after that.
- **OSRM public server.** It is free and best-effort, with no uptime guarantee. Set
  `OSRM_BASE_URL` to use another OSRM server.
