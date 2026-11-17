"""Resolve and suggest start/finish locations from the Place table. No external calls.

Accepted inputs:
  "Chicago, IL"   "Chicago, Illinois"   "Chicago" (when unambiguous)   "41.8781,-87.6298"
"""
import difflib
import math
import re
from dataclasses import dataclass

from ..models import Place
from . import borders
from .errors import LocationError
from .geo import MILES_PER_DEGREE_LAT, US_STATES, haversine_miles, normalize_place

COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
STATE_BY_NAME = {normalize_place(name): code for code, name in US_STATES.items()}
# How far to look for a town to name in a coordinate's label.
MAX_MILES_FROM_US_PLACE = 30.0
FUZZY_CUTOFF = 0.72


@dataclass(frozen=True, slots=True)
class Location:
    query: str
    label: str
    lat: float
    lon: float


def _state_code(text):
    """Exact state code or full state name -> code, else None."""
    upper = text.strip().upper()
    if upper in US_STATES:
        return upper
    return STATE_BY_NAME.get(normalize_place(text))


def _states_starting_with(text):
    """State codes whose code or name starts with the (possibly partial) text."""
    upper, norm = text.strip().upper(), normalize_place(text)
    return [
        code for code, name in US_STATES.items()
        if code.startswith(upper) or normalize_place(name).startswith(norm)
    ]


def _unique(places, seen):
    """Drop alias rows that point at a place already collected."""
    out = []
    for place in places:
        ident = (place.name, place.state)
        if ident not in seen:
            seen.add(ident)
            out.append(place)
    return out


def _fuzzy(norm, states, limit):
    """Places whose name is a near-miss of `norm` (spelling mistakes)."""
    if len(norm) < 4:
        return []
    qs = Place.objects.filter(key__startswith=norm[0])
    if states:
        qs = qs.filter(state__in=states)
    keys = list(qs.values_list("key", flat=True).distinct())
    # Two swapped letters ("Maimi") score poorly on similarity, so test those swaps exactly.
    swaps = {norm[:i] + norm[i + 1] + norm[i] + norm[i + 2:] for i in range(len(norm) - 1)}
    key_set = set(keys)
    close = [key for key in swaps if key in key_set]
    close += [
        key for key in difflib.get_close_matches(norm, keys, n=limit, cutoff=FUZZY_CUTOFF)
        if key not in close
    ]
    if not close:
        return []
    found = Place.objects.filter(key__in=close)
    if states:
        found = found.filter(state__in=states)
    rank = {key: i for i, key in enumerate(close)}
    return sorted(found, key=lambda p: (rank[p.key], -p.population))


def suggest(query, limit=8):
    """Places matching what the user has typed so far, best first."""
    text = (query or "").strip()
    if not text or COORD_RE.match(text):
        return []

    seen, results = set(), []
    city_part, states = text, None
    if "," in text:
        city_part, state_part = (part.strip() for part in text.rsplit(",", 1))
        if state_part:
            states = _states_starting_with(state_part) or None
    else:
        # The whole input names a state ("FL", "Florida"): offer its largest cities.
        code = _state_code(text)
        if code:
            share = 5 if len(text) == 2 else limit
            results += _unique(
                Place.objects.filter(state=code).order_by("-population", "name")[: share * 2], seen
            )[:share]
        elif len(text) >= 4:
            for code in _states_starting_with(text)[:2]:
                results += _unique(
                    Place.objects.filter(state=code).order_by("-population", "name")[:6], seen
                )[:3]

    norm = normalize_place(city_part)
    if norm:
        prefix = Place.objects.filter(key__startswith=norm)
        if states:
            prefix = prefix.filter(state__in=states)
        results += _unique(prefix.order_by("-population", "name")[: limit * 3], seen)
        if len(results) < limit:
            results += _unique(_fuzzy(norm, states, limit), seen)
    return results[:limit]


def _labels(places, limit=5):
    return [p.label for p in places[:limit]]


def _nearest_place(lat, lon):
    lat_margin = MAX_MILES_FROM_US_PLACE / MILES_PER_DEGREE_LAT
    cos_lat = max(math.cos(math.radians(lat)), 0.2)
    lon_margin = MAX_MILES_FROM_US_PLACE / (MILES_PER_DEGREE_LAT * cos_lat)
    nearby = Place.objects.filter(
        latitude__range=(lat - lat_margin, lat + lat_margin),
        longitude__range=(lon - lon_margin, lon + lon_margin),
    )
    best = None
    for place in nearby:
        d = haversine_miles(lat, lon, place.latitude, place.longitude)
        if best is None or d < best[0]:
            best = (d, place)
    return best


def resolve(query, field=None):
    """Turn user input into a Location, or raise LocationError with suggestions."""
    text = (query or "").strip()
    if not text:
        raise LocationError("Enter a location.", field)

    match = COORD_RE.match(text)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise LocationError(f"'{text}' is not a valid latitude,longitude pair.", field)
        # The state outline decides: a nearby US town is not enough (Windsor ON is 2 miles
        # from Detroit), and neither is a state's bounding box.
        state = borders.state_at(lat, lon)
        if state is None:
            raise LocationError(f"'{text}' is not within the USA.", field)
        near = _nearest_place(lat, lon)
        label = f"near {near[1].label}" if near else US_STATES[state]
        return Location(text, f"{lat:.5f}, {lon:.5f} ({label})", lat, lon)

    if "," in text:
        city, state_text = (part.strip() for part in text.rsplit(",", 1))
        state = _state_code(state_text)
        if not state:
            raise LocationError(
                f"'{state_text}' is not a US state. Both locations must be within the USA.",
                field, _labels(suggest(city)),
            )
        if not city:
            raise LocationError(
                f"Add a city in {US_STATES[state]}.", field, _labels(suggest(state)),
            )
        place = (Place.objects.filter(key=normalize_place(city), state=state)
                 .order_by("-population").first())
        if place:
            return Location(text, place.label, place.latitude, place.longitude)
        options = _labels(suggest(f"{city}, {state}"))
        hint = f" Did you mean {options[0]}?" if options else " Check the spelling."
        raise LocationError(f"Could not find '{city}' in {US_STATES[state]}.{hint}", field, options)

    # No state given.
    state = _state_code(text)
    if state:
        raise LocationError(
            f"'{text}' is a state. Add a city, for example {suggest(text)[0].label}.",
            field, _labels(suggest(text)),
        )
    matches = _unique(Place.objects.filter(key=normalize_place(text)).order_by("-population"), set())
    if len(matches) == 1:
        place = matches[0]
        return Location(text, place.label, place.latitude, place.longitude)
    if matches:
        raise LocationError(
            f"{len(matches)} places are called '{text}'. Add the state, for example {matches[0].label}.",
            field, _labels(matches),
        )
    options = _labels(suggest(text))
    hint = f" Did you mean {options[0]}?" if options else " Use 'City, ST' such as 'Chicago, IL'."
    raise LocationError(f"Could not find '{text}'.{hint}", field, options)
