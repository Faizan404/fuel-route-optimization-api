"""Orchestrates one request: resolve locations -> route -> stations -> optimise -> response."""
import math

from django.conf import settings
from django.core.cache import cache

from ..models import Station
from . import gazetteer, routing
from .errors import InfeasiblePlan, PlanError
from .geo import MILES_PER_DEGREE_LAT, haversine_miles
from .optimizer import optimize
from .route_line import RouteLine


def _candidates(line, corridor_miles):
    """Stations within the corridor as (mile_marker, off_route_miles, station), ascending."""
    # Only stations inside the route's bounding box (plus the corridor) are read from the DB.
    lo_lat, hi_lat, lo_lon, hi_lon = line.bbox
    lat_margin = corridor_miles / MILES_PER_DEGREE_LAT
    lon_margin = lat_margin / 0.3      # generous: longitude degrees shrink towards the north
    nearby = Station.objects.filter(
        latitude__range=(lo_lat - lat_margin, hi_lat + lat_margin),
        longitude__range=(lo_lon - lon_margin, hi_lon + lon_margin),
    )
    cheapest_at = {}
    for st in nearby.iterator():
        hit = line.locate(st.latitude, st.longitude, corridor_miles)
        if hit is None:
            continue
        off, mile = hit
        st.price = float(st.price)
        # Stations geocoded to the same point share a mile marker; only the cheapest matters.
        key = round(mile, 3)
        current = cheapest_at.get(key)
        if current is None or st.price < current[2].price:
            cheapest_at[key] = (mile, off, st)
    return sorted(cheapest_at.values(), key=lambda c: c[0])


NEAREST_SEARCH_MILES = (25.0, 50.0, 100.0, 300.0)
# Road miles to a station are longer than straight-line miles; 1.25 is the allowance.
ROAD_FACTOR = 1.25


def _nearest_station_miles(lat, lon):
    """(straight-line miles, station) to the nearest listed station, or (None, None).

    Searches a bounding box that widens 25 -> 50 -> 100 -> 300 miles. A hit within the
    current radius is the true nearest: anything closer lies inside that radius, and the
    radius lies inside the box.
    """
    for radius in NEAREST_SEARCH_MILES:
        lat_margin = radius / MILES_PER_DEGREE_LAT
        lon_margin = radius / (MILES_PER_DEGREE_LAT * max(math.cos(math.radians(lat)), 0.2))
        best = None
        for st in Station.objects.filter(latitude__range=(lat - lat_margin, lat + lat_margin),
                                         longitude__range=(lon - lon_margin, lon + lon_margin)):
            d = haversine_miles(lat, lon, st.latitude, st.longitude)
            if best is None or d < best[0]:
                best = (d, st)
        if best is not None and best[0] <= radius:
            return best
    return None, None


def _thin(candidates, limit):
    """Bound the optimiser's input on very dense corridors: cheapest station per stretch."""
    for bin_miles in (2.0, 5.0, 10.0, 20.0):
        if len(candidates) <= limit:
            break
        bins = {}
        for cand in candidates:
            slot = int(cand[0] // bin_miles)
            if slot not in bins or cand[2].price < bins[slot][2].price:
                bins[slot] = cand
        candidates = sorted(bins.values(), key=lambda c: c[0])
    return candidates


def _largest_gap(candidates, distance):
    marks = [0.0] + [c[0] for c in candidates] + [distance]
    return max((b - a, a, b) for a, b in zip(marks, marks[1:]))


def _station_brief(mile, st):
    return {
        "station_id": str(st.opis_id),
        "name": st.name,
        "city": st.city,
        "state": st.state,
        "latitude": round(st.latitude, 6),
        "longitude": round(st.longitude, 6),
        "mile_marker": round(mile, 1),
        "price_per_gallon": round(st.price, 3),
    }


def _decisions(purchases, candidates, usable):
    """Why each stop buys what it buys, read straight from the optimiser's plan.

    Between consecutive stops the optimiser either leaves full (the next stop is dearer)
    or buys just enough to reach the next stop with the reserve left (it is no dearer); the
    last stop buys what is needed to finish.
    """
    out = []
    for k, purchase in enumerate(purchases):
        mile, _, st = candidates[purchase.index]
        in_range = sum(1 for m, _, _ in candidates if mile < m <= mile + usable)
        if k + 1 == len(purchases):
            action, next_stop = "finish", None
        else:
            n_mile, _, n_st = candidates[purchases[k + 1].index]
            next_stop = _station_brief(n_mile, n_st)
            action = "just_enough" if n_st.price <= st.price else "fill_up"
        out.append({"action": action, "in_range_count": in_range, "next_stop": next_stop})
    return out


def _plan_stops(line, distance, rules, finish_reserve):
    """Try the corridor widths in order.

    Returns (purchases, candidates, width, minimum_dropped); purchases is None when no
    width works. At each width a plan without the minimum purchase is found first: if
    there is none, the stations are too far apart and the next width is tried. If there
    is one, the minimum purchase is applied on top and dropped only when it cannot be
    met: it is a guard, so it never makes the search move further from the route.
    """
    cfg = settings.FUEL_PLAN
    minimum = rules["min_purchase_gallons"] * cfg["MPG"]
    widths = [cfg["CORRIDOR_MILES"]] + list(cfg["CORRIDOR_WIDENING"])
    widest = None
    candidates = []
    for width in widths:
        if width == widths[0]:
            pool = _candidates(line, width)
        else:
            # Scan once at the widest corridor and filter, rather than rescanning per width.
            widest = widest if widest is not None else _candidates(line, widths[-1])
            pool = [c for c in widest if c[1] <= width]
        candidates = _thin(pool, cfg["MAX_CANDIDATES"])
        # A station may be up to `width` miles off the route, so the detour there and back
        # is held out of the tank: no leg can then exceed the real range.
        usable = cfg["TANK_RANGE_MILES"] - 2 * width
        args = ([c[0] for c in candidates], [c[2].price for c in candidates],
                distance, usable, usable, cfg["MPG"])
        rule_args = {"stop_cost": rules["stop_cost"], "reserve": rules["reserve_miles"],
                     "finish_reserve": finish_reserve}
        unguarded = optimize(*args, **rule_args)
        if unguarded is None:
            continue
        if minimum <= 0:
            return unguarded, candidates, width, False
        guarded = optimize(*args, min_purchase=minimum, **rule_args)
        if guarded is not None:
            return guarded, candidates, width, False
        return unguarded, candidates, width, True
    return None, candidates, widths[-1], False


def _rules(stop_cost, reserve_miles, min_purchase_gallons):
    """Business rules for this request: explicit values, else the settings."""
    cfg = settings.FUEL_PLAN
    return {
        "stop_cost": cfg["STOP_COST_USD"] if stop_cost is None else float(stop_cost),
        "reserve_miles": cfg["SAFETY_RESERVE_MILES"] if reserve_miles is None else float(reserve_miles),
        "min_purchase_gallons": (cfg["MIN_PURCHASE_GALLONS"] if min_purchase_gallons is None
                                 else float(min_purchase_gallons)),
    }


def _with_queries(plan, start, finish):
    """The cached plan is shared by every spelling of the same places, so the text each
    request typed is added per request rather than stored in the cache."""
    return {**plan,
            "start": {"query": start.query, **plan["start"]},
            "finish": {"query": finish.query, **plan["finish"]}}


def _trip_fuel_cost(stops, candidates, fuel_bought, fuel_consumed, total_fuel_cost):
    """Money for all the fuel the trip burns, not just what is bought on the road.

    The starting tank has no price in the sheet, so the fuel it supplies is priced at the
    first stop's price, or at the mean price of the stations along the route when there is
    no stop. None when there is nothing to price it with.
    """
    from_start = max(0.0, fuel_consumed - fuel_bought)
    if stops:
        price = stops[0]["price_per_gallon"]
    elif candidates:
        price = sum(c[2].price for c in candidates) / len(candidates)
    else:
        return None
    return round(from_start * price + total_fuel_cost, 2)


def build_plan(start_query, finish_query, stop_cost=None, reserve_miles=None,
               min_purchase_gallons=None):
    cfg = settings.FUEL_PLAN
    tank_range, mpg = cfg["TANK_RANGE_MILES"], cfg["MPG"]
    rules = _rules(stop_cost, reserve_miles, min_purchase_gallons)
    reserve = rules["reserve_miles"]

    start = gazetteer.resolve(start_query, "start")
    finish = gazetteer.resolve(finish_query, "finish")
    if haversine_miles(start.lat, start.lon, finish.lat, finish.lon) < 0.1:
        raise PlanError("Start and finish are the same place. Choose a different finish.", "finish")

    cache_key = "plan:%.4f,%.4f:%.4f,%.4f:%g:%g:%g" % (
        start.lat, start.lon, finish.lat, finish.lon, rules["stop_cost"], reserve,
        rules["min_purchase_gallons"],
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return _with_queries(cached, start, finish)

    route = routing.fetch_route(start, finish)
    line = RouteLine(route.points, route.distance_miles)
    distance = route.distance_miles
    notes = []

    # Fuel needed at the destination: enough to reach the nearest listed station (1.25 x the
    # straight-line miles), never less than FINISH_MIN_MILES as a margin for worse fuel
    # economy, and never less than the safety reserve on a trip that stops for fuel.
    finish_min = cfg["FINISH_MIN_MILES"]
    near_miles, near_station = _nearest_station_miles(finish.lat, finish.lon)
    to_station = ROAD_FACTOR * near_miles if near_miles is not None else float("inf")
    finish_need = max(finish_min, to_station)
    if near_station is not None:
        near_text = "%s in %s, %s (%.0f mi)" % (near_station.name, near_station.city,
                                                near_station.state, near_miles)
    else:
        near_text = "more than %.0f miles away" % NEAREST_SEARCH_MILES[-1]
    remote = False     # True when the only plan keeps just the minimum margin at the finish

    chosen, purchases, corridor, considered, minimum_dropped = [], [], cfg["CORRIDOR_MILES"], 0, False
    if distance <= tank_range - finish_need:
        finish_reserve = finish_need
        notes.append(
            "A full tank covers this %.0f-mile trip and still leaves %.1f gallons, enough to reach "
            "the nearest listed station, %s, so no fuel stop is needed."
            % (distance, (tank_range - distance) / mpg, near_text)
        )
    else:
        finish_reserve = max(finish_need, reserve)
        purchases, chosen, width, minimum_dropped = _plan_stops(line, distance, rules, finish_reserve)
        if purchases is None:
            # The destination is too far from any listed station to arrive with that much fuel.
            # The trip itself may still be possible: plan for the minimum margin and say so.
            if distance <= tank_range - finish_min:
                purchases, chosen, width, minimum_dropped = [], [], cfg["CORRIDOR_MILES"], False
                finish_reserve, remote = finish_min, True
            else:
                retry = _plan_stops(line, distance, rules, finish_min)
                if retry[0] is not None:
                    purchases, chosen, width, minimum_dropped = retry
                    finish_reserve, remote = finish_min, True
        if purchases is None:
            gap, a, b = _largest_gap(chosen, distance)
            # The route exists; only the price list cannot cover it. Return the route, the
            # stations that do exist and the gap, so the trip can still be shown with a warning.
            refuel_before = min(b, a + tank_range - reserve)
            raise InfeasiblePlan(
                "No fuel plan fits the %.0f-mile range with a %.0f-mile reserve: the price list "
                "has no station within %.0f miles of the route between mile %.0f and mile %.0f "
                "(%.0f miles). The truck would need to refuel at a station not in the price list "
                "before mile %.0f." % (tank_range, reserve, width, a, b, gap, refuel_before),
                details={
                    "start": {"query": start.query, "resolved": start.label,
                              "latitude": start.lat, "longitude": start.lon},
                    "finish": {"query": finish.query, "resolved": finish.label,
                               "latitude": finish.lat, "longitude": finish.lon},
                    "route": {
                        "distance_miles": round(distance, 1),
                        "duration_hours": round(route.duration_hours, 2),
                        "geometry": {"type": "LineString", "coordinates": line.geojson_coordinates()},
                    },
                    "stations_on_route": [_station_brief(c[0], c[2]) for c in chosen],
                    "gap": {
                        "from_mile": round(a, 1), "to_mile": round(b, 1), "miles": round(gap, 1),
                        "refuel_before_mile": round(refuel_before, 1),
                        "search_corridor_miles": width,
                        "geometry": {"type": "LineString", "coordinates": line.geojson_between(a, b)},
                    },
                },
            )
        if minimum_dropped:
            notes.append(
                "No plan could buy at least %g gallons at every stop on this route, so the "
                "minimum purchase was not applied." % rules["min_purchase_gallons"]
            )
        corridor, considered = width, len(chosen)
        if corridor != cfg["CORRIDOR_MILES"]:
            notes.append(
                "Stations are sparse on this route, so the search was widened to %.0f miles "
                "either side of it." % corridor
            )

    # Stations used to price the starting tank on a trip with no stop.
    trip_candidates = chosen or ([] if purchases else _candidates(line, corridor))

    # How far the truck can go between stops while keeping the reserve.
    usable = tank_range - 2 * corridor - reserve
    decisions = _decisions(purchases, chosen, usable)
    stops, total_cost, purchased_gallons = [], 0.0, 0.0
    tank_gallons = tank_range / mpg
    fuel, previous_mile = tank_gallons, 0.0
    for seq, (purchase, decision) in enumerate(zip(purchases, decisions), start=1):
        mile, off, st = chosen[purchase.index]
        # Round first and cost from the rounded figures, so every stop reads as an exact
        # receipt (gallons x price = cost) and the total is the sum of the stops.
        gallons = round(purchase.miles / mpg, 2)
        price = round(st.price, 3)
        cost = round(gallons * price, 2)
        arrive = fuel - (mile - previous_mile) / mpg
        fuel, previous_mile = arrive + gallons, mile
        total_cost += cost
        purchased_gallons += gallons
        stops.append({
            "sequence": seq,
            "station_id": str(st.opis_id),
            "name": st.name,
            "address": st.address,
            "city": st.city,
            "state": st.state,
            "latitude": round(st.latitude, 6),
            "longitude": round(st.longitude, 6),
            "mile_marker": round(mile, 1),
            "distance_from_route_miles": round(off, 1),
            "price_per_gallon": price,
            "gallons_purchased": gallons,
            "cost": cost,
            "fuel_on_arrival_gallons": round(arrive, 2),
            "fuel_after_fill_gallons": round(fuel, 2),
            "decision": decision,
        })

    fuel_at_finish = fuel - (distance - previous_mile) / mpg
    if remote:
        notes.append(
            "Arrives with %.1f gal. The nearest listed station is %.0f mi away (%s, %s); refuel "
            "at a local station." % (fuel_at_finish, near_miles, near_station.name, near_station.city)
            if near_station is not None else
            "Arrives with %.1f gal. No listed station is within %.0f mi of the destination; refuel "
            "at a local station." % (fuel_at_finish, NEAREST_SEARCH_MILES[-1])
        )

    plan = {
        "start": {"resolved": start.label, "latitude": start.lat, "longitude": start.lon},
        "finish": {"resolved": finish.label, "latitude": finish.lat, "longitude": finish.lon},
        "vehicle": {"max_range_miles": tank_range, "miles_per_gallon": mpg,
                    "tank_at_departure": "full", "reserve_gallons": round(reserve / mpg, 2)},
        "route": {
            "distance_miles": round(distance, 1),
            "duration_hours": round(route.duration_hours, 2),
            "geometry": {"type": "LineString", "coordinates": line.geojson_coordinates()},
        },
        "fuel_stops": stops,
        # Every station the optimiser weighed, for showing the alternatives on the map.
        "stations_on_route": [_station_brief(m, st) for m, _, st in chosen],
        "summary": {
            "total_distance_miles": round(distance, 1),
            "fuel_consumed_gallons": round(distance / mpg, 2),
            "fuel_purchased_gallons": round(purchased_gallons, 2),
            "fuel_left_at_finish_gallons": round(fuel_at_finish, 2),
            "total_fuel_cost": round(total_cost, 2),
            "trip_fuel_cost": _trip_fuel_cost(stops, trip_candidates, purchased_gallons,
                                              distance / mpg, round(total_cost, 2)),
            "number_of_stops": len(stops),
            "stop_cost_usd": rules["stop_cost"],
            "total_cost_including_stop_cost": round(total_cost + rules["stop_cost"] * len(stops), 2),
            "safety_reserve_miles": reserve,
            "finish_reserve_miles": round(finish_reserve, 1),
            "nearest_station_to_finish": None if near_station is None else {
                "name": near_station.name, "city": near_station.city,
                "state": near_station.state, "miles": round(near_miles, 1),
            },
            "min_purchase_gallons": rules["min_purchase_gallons"],
            "stations_considered": considered,
            "search_corridor_miles": corridor,
        },
        "notes": notes,
    }
    cache.set(cache_key, plan, cfg["PLAN_CACHE_SECONDS"])
    return _with_queries(plan, start, finish)
