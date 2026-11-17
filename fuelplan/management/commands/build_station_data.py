"""One-time ingestion: clean the fuel price file and geocode stations offline.

The price file has no coordinates and its addresses are highway exits
("I-44, EXIT 283 & US-69"), which street geocoders cannot resolve. Stations are
therefore located at the centroid of their city, matched against free gazetteers:

  1. US Census places            (2024_Gaz_place_national.txt)
  2. US Census county subdivisions (2024_Gaz_cousubs_national.txt)
  3. GeoNames populated places   (US.txt)

Outputs (committed to the repo; `load_data` loads the first and third into the database):
  data/stations.csv             one row per US station, with price and coordinates
  data/stations_unresolved.csv  stations left out, with the reason
  data/us_places.csv            place gazetteer used to resolve start/finish

City centres that fall in the water are moved onto land using the state outlines, so
`build_state_outlines` must have been run first. Download links are in the README. Usage:
  python manage.py build_station_data --gazetteers <dir containing the 3 files>
"""
import csv
import re
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuelplan.services.borders import state_at
from fuelplan.services.geo import US_STATES, haversine_miles, normalize_place

LSAD_SUFFIX = re.compile(
    r"\s+(city and borough|unified government|metropolitan government|consolidated government|"
    r"urban county|metro township|charter township|city|town|village|borough|CDP|municipality|"
    r"corporation|township|plantation|CCD|UT|district|precinct|gore|grant|location|purchase|"
    r"reservation)$",
    re.I,
)
AMBIGUITY_MILES = 10.0


def clean_census_name(name):
    return LSAD_SUFFIX.sub("", re.sub(r"\s*\(balance\)$", "", name.strip()))


def squash(key):
    return (key[0].replace(" ", ""), key[1])


def load_census(path, name_col, weight_col, lat_col, lon_col):
    """Return {(normalized name, state): [(lat, lon, land area, display name)]}."""
    index = defaultdict(list)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            f = [x.strip() for x in line.rstrip("\n").split("\t")]
            if f[0] == "USPS" or f[0] not in US_STATES:
                continue
            name = clean_census_name(f[name_col])
            entry = (float(f[lat_col]), float(f[lon_col]), float(f[weight_col]), name)
            index[(normalize_place(name), f[0])].append(entry)
            # Consolidated governments: "Nashville-Davidson" should also answer to "Nashville".
            head = re.split(r"[-/]", name)[0].strip()
            if head and head != name:
                index[(normalize_place(head), f[0])].append(entry)
    return index


def load_geonames(path):
    index = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            f = line.rstrip("\n").split("\t")
            if f[6] != "P" or f[10] not in US_STATES:
                continue
            entry = (float(f[4]), float(f[5]), float(f[14] or 0), f[1])
            for name in {f[1], f[2]}:
                index[(normalize_place(name), f[10])].append(entry)
    return index


ONSHORE_SEARCH_MILES = 50.0


def move_onshore(entry, key, geonames):
    """Return (entry, moved). Fixes Census centres that lie in the water.

    The Census "internal point" of a place that includes open water can fall offshore
    (San Francisco's is 34 miles out, near the Farallon Islands). Such a centre is outside
    its own state's outline; it is replaced by the nearest same-named GeoNames point that
    is inside the state. A plain distance rule is not used: it would also move correct
    centres to unrelated towns that share the name.
    """
    lat, lon = entry[0], entry[1]
    state = key[1]
    if state_at(lat, lon) == state:
        return entry, False
    best = None
    for cand in geonames.get(key, []) + geonames.get((key[0] + " CITY", state), []):
        d = haversine_miles(lat, lon, cand[0], cand[1])
        if d <= ONSHORE_SEARCH_MILES and (best is None or d < best[0]) and state_at(cand[0], cand[1]) == state:
            best = (d, cand)
    if best is None:
        return entry, False
    return (best[1][0], best[1][1], entry[2], entry[3]), True


def pick(candidates):
    """Return (entry, None) or (None, reason). Refuse same-named places that are far apart."""
    first = candidates[0]
    spread = max(haversine_miles(first[0], first[1], c[0], c[1]) for c in candidates)
    if spread > AMBIGUITY_MILES:
        return None, f"ambiguous: {len(candidates)} same-named places up to {spread:.0f} mi apart"
    return max(candidates, key=lambda c: c[2]), None


class Command(BaseCommand):
    help = "Clean the fuel price CSV and geocode stations offline against gazetteers."

    def add_arguments(self, parser):
        parser.add_argument("--gazetteers", required=True, help="Directory with the gazetteer files")
        parser.add_argument(
            "--prices",
            default=str(settings.BASE_DIR / "fuel-prices-for-be-assessment.csv"),
            help="Fuel price CSV",
        )

    def handle(self, *args, **opts):
        gaz = Path(opts["gazetteers"])
        files = {
            "places": gaz / "2024_Gaz_place_national.txt",
            "cousubs": gaz / "2024_Gaz_cousubs_national.txt",
            "geonames": gaz / "US.txt",
        }
        for path in files.values():
            if not path.exists():
                raise CommandError(f"Missing gazetteer file: {path}")

        places = load_census(files["places"], 3, 6, 10, 11)
        cousubs = load_census(files["cousubs"], 3, 5, 9, 10)
        geonames = load_geonames(files["geonames"])
        sources = [("census_place", places), ("census_cousub", cousubs), ("geonames", geonames)]
        squashed = [(name, {squash(k): v for k, v in idx.items()}) for name, idx in sources]

        # --- clean + aggregate the price file -------------------------------
        stations, skipped_non_us, rows = {}, 0, 0
        with open(opts["prices"], newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                rows += 1
                state = row["State"].strip().upper()
                if state not in US_STATES:
                    skipped_non_us += 1
                    continue
                sid = row["OPIS Truckstop ID"].strip()
                st = stations.setdefault(sid, {
                    "opis_id": sid,
                    "name": " ".join(row["Truckstop Name"].split()),
                    "address": " ".join(row["Address"].split()),
                    "city": " ".join(row["City"].split()),
                    "state": state,
                    "rack_id": row["Rack ID"].strip(),
                    "prices": [],
                })
                st["prices"].append(float(row["Retail Price"]))

        # --- geocode ---------------------------------------------------------
        resolved, unresolved, stations_moved = [], [], 0
        for st in stations.values():
            key = (normalize_place(st["city"]), st["state"])
            found = None
            for name, idx in sources:
                if key in idx:
                    found = (name, idx[key])
                    break
            if not found:
                for name, idx in squashed:
                    if squash(key) in idx:
                        found = (name, idx[squash(key)])
                        break
            # No date column exists, so repeated prices for a station are averaged.
            st["price"] = round(sum(st["prices"]) / len(st["prices"]), 5)
            st["price_samples"] = len(st["prices"])
            if not found:
                unresolved.append({**st, "reason": "city not found in any gazetteer"})
                continue
            entry, reason = pick(found[1])
            if entry is None:
                unresolved.append({**st, "reason": reason})
                continue
            entry, moved = move_onshore(entry, key, geonames)
            stations_moved += moved
            resolved.append({**st, "lat": entry[0], "lon": entry[1], "geocode_source": found[0]})

        data_dir = Path(settings.DATA_DIR)
        data_dir.mkdir(exist_ok=True)
        base = ["opis_id", "name", "address", "city", "state", "rack_id", "price", "price_samples"]
        self._write(data_dir / "stations.csv", base + ["lat", "lon", "geocode_source"],
                    sorted(resolved, key=lambda s: int(s["opis_id"])))
        self._write(data_dir / "stations_unresolved.csv", base + ["reason"],
                    sorted(unresolved, key=lambda s: int(s["opis_id"])))

        # --- place gazetteer for start/finish --------------------------------
        best, places_moved = {}, 0
        for (norm, state), entries in places.items():
            entry, moved = move_onshore(max(entries, key=lambda c: c[2]), (norm, state), geonames)
            places_moved += moved
            best[(norm, state)] = entry
        # "Boise City" should also answer to "Boise" when that name is otherwise unused.
        for (norm, state), entry in list(best.items()):
            if norm.endswith(" CITY") and (norm[:-5], state) not in best:
                best[(norm[:-5], state)] = entry
            # Census calls Honolulu "Urban Honolulu".
            if norm.startswith("URBAN ") and (norm[6:], state) not in best:
                best[(norm[6:], state)] = entry
        # Population (from GeoNames) ranks suggestions so "Chi" offers Chicago first.
        population = {}
        for (norm, state), entry in best.items():
            for key in (norm, norm + " CITY"):
                for cand in geonames.get((key, state), ()):
                    if haversine_miles(entry[0], entry[1], cand[0], cand[1]) <= 15:
                        population[entry] = max(population.get(entry, 0), int(cand[2]))
        with open(data_dir / "us_places.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["key", "state", "name", "lat", "lon", "population"])
            for (norm, state), e in sorted(best.items()):
                w.writerow([norm, state, e[3], f"{e[0]:.6f}", f"{e[1]:.6f}", population.get(e, 0)])

        self.stdout.write(
            f"rows read: {rows}; non-US rows skipped: {skipped_non_us}; "
            f"US stations: {len(stations)}; geocoded: {len(resolved)}; "
            f"unresolved: {len(unresolved)}; places: {len(best)}; offshore centres moved "
            f"onshore: {stations_moved} stations, {places_moved} places"
        )

    @staticmethod
    def _write(path, fields, records):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)
