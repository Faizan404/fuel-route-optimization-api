import json
import random
from pathlib import Path
from unittest import mock

import pytest
import requests
from rest_framework.test import APIClient

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "osrm_chicago_miami.json").read_text())
URL = "/api/v1/route/"


class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def osrm():
    """Replace the outbound HTTP call with a recorded Chicago -> Miami OSRM response."""
    with mock.patch("fuelplan.services.routing.requests.get",
                    return_value=FakeResponse(FIXTURE)) as patched:
        yield patched


def test_long_route_returns_optimal_plan(client, osrm):
    resp = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["route"]["distance_miles"] == pytest.approx(1345.4, abs=0.5)
    assert body["route"]["geometry"]["type"] == "LineString"
    assert len(body["route"]["geometry"]["coordinates"]) > 100
    assert body["summary"]["fuel_consumed_gallons"] == pytest.approx(134.54, abs=0.05)
    assert "map_url" in body and "start=Chicago" in body["map_url"]

    stops = body["fuel_stops"]
    assert len(stops) >= 2                       # 1,345 miles cannot be done on one extra fill
    # No leg may exceed the 500-mile range.
    marks = [0.0] + [s["mile_marker"] for s in stops] + [body["route"]["distance_miles"]]
    assert all(b - a <= 500 for a, b in zip(marks, marks[1:]))
    # Tank never runs dry and is never over-filled (50 gal = 500 mi / 10 mpg).
    assert all(s["fuel_on_arrival_gallons"] >= 0 for s in stops)
    assert all(s["fuel_after_fill_gallons"] <= 50.0 for s in stops)
    # Total is the sum of the stops, and each stop is gallons x price.
    assert body["summary"]["total_fuel_cost"] == pytest.approx(sum(s["cost"] for s in stops), abs=0.02)
    for s in stops:
        assert s["cost"] == pytest.approx(s["gallons_purchased"] * s["price_per_gallon"], abs=0.02)
    assert body["summary"]["total_fuel_cost"] > 0


# --- business rules: stop cost, safety reserve, minimum purchase -------------------

RESERVE_GALLONS = 7.5
MIN_PURCHASE = 5.0
MAX_LEG = 500 - 2 * 5 - 75      # tank range less detour allowance and reserve


def assert_rules_hold(body):
    stops = body["fuel_stops"]
    assert all(s["fuel_on_arrival_gallons"] >= RESERVE_GALLONS - 0.01 for s in stops)
    assert all(s["gallons_purchased"] >= MIN_PURCHASE - 0.01 for s in stops)
    marks = [0.0] + [s["mile_marker"] for s in stops] + [body["route"]["distance_miles"]]
    legs = [b - a for a, b in zip(marks, marks[1:])]
    assert all(leg <= MAX_LEG + 0.5 for leg in legs)
    # At the finish the truck still has enough fuel to reach the nearest listed station.
    summary = body["summary"]
    assert summary["fuel_left_at_finish_gallons"] >= summary["finish_reserve_miles"] / 10 - 0.01


def test_default_rules_hold_on_a_real_route(client, osrm):
    body = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"}).json()
    assert_rules_hold(body)
    summary = body["summary"]
    assert summary["stop_cost_usd"] == 3.0
    assert summary["safety_reserve_miles"] == 75.0
    assert summary["min_purchase_gallons"] == 5.0
    assert summary["finish_reserve_miles"] >= 75.0          # a trip with stops keeps the reserve
    assert set(summary["nearest_station_to_finish"]) == {"name", "city", "state", "miles"}
    assert summary["total_cost_including_stop_cost"] == pytest.approx(
        summary["total_fuel_cost"] + 3.0 * summary["number_of_stops"], abs=0.01)
    assert body["vehicle"]["reserve_gallons"] == RESERVE_GALLONS


def test_stop_cost_cuts_stops_for_little_extra_fuel(client, osrm):
    priced = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"}).json()
    free = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL", "stop_cost": 0}).json()
    assert priced["summary"]["number_of_stops"] < free["summary"]["number_of_stops"]
    assert priced["summary"]["total_fuel_cost"] <= free["summary"]["total_fuel_cost"] * 1.03


def test_a_different_stop_cost_is_a_different_plan_but_the_same_route(client, osrm):
    first = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL", "stop_cost": 0}).json()
    second = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL", "stop_cost": 10}).json()
    assert first["summary"]["stop_cost_usd"] == 0 and second["summary"]["stop_cost_usd"] == 10
    assert first["fuel_stops"] != second["fuel_stops"]
    assert osrm.call_count == 1     # the route itself is cached; only the plan differs


def test_synthetic_long_route_with_dense_stations(client):
    # A 1,500-mile route with a station every 25 miles at realistic prices.
    rng = random.Random(5)
    stations = [(m, 1.0, fake_station(900000 + i, round(rng.uniform(2.8, 3.8), 3)))
                for i, m in enumerate(range(25, 1500, 25))]
    with mock.patch("fuelplan.services.routing.requests.get", return_value=FakeResponse(synthetic_route(1500))), \
            mock.patch("fuelplan.services.planner._candidates", return_value=stations):
        priced = client.get(URL, {"start": "Dallas, TX", "finish": "Houston, TX"}).json()
        free = client.get(URL, {"start": "Dallas, TX", "finish": "Houston, TX", "stop_cost": 0}).json()
    assert_rules_hold(priced)
    assert priced["summary"]["number_of_stops"] < free["summary"]["number_of_stops"]
    assert priced["summary"]["total_fuel_cost"] <= free["summary"]["total_fuel_cost"] * 1.03


def fake_station(opis_id, price):
    class FakeStation:
        pass
    st = FakeStation()
    st.opis_id, st.name, st.address = opis_id, "TEST STOP %d" % opis_id, "I-00, EXIT %d" % opis_id
    st.city, st.state, st.latitude, st.longitude, st.price = "Testville", "TX", 32.0, -97.0, price
    return st


def synthetic_route(miles):
    return {"code": "Ok", "routes": [{
        "geometry": "_p~iF~ps|U_ulLnnqC", "distance": miles * 1609.344, "duration": miles * 60,
    }]}


def recorded(name):
    """Patch the routing call with a recorded OSRM response from tests/fixtures."""
    body = json.loads((Path(__file__).parent / "fixtures" / ("osrm_%s.json" % name)).read_text())
    return mock.patch("fuelplan.services.routing.requests.get", return_value=FakeResponse(body))


def test_destination_far_from_a_station_needs_fuel_to_reach_one(client):
    # The price list has no station in Houston itself; the nearest is 40 miles away in Liberty,
    # TX, so the truck must arrive with 50 miles of fuel (1.25 x 40). A 460-mile trip on a
    # 500-mile tank would leave only 40, so it stops.
    stations = [(200.0, 1.0, fake_station(900001, 3.0))]
    with mock.patch("fuelplan.services.routing.requests.get", return_value=FakeResponse(synthetic_route(460))), \
            mock.patch("fuelplan.services.planner._candidates", return_value=stations):
        body = client.get(URL, {"start": "Dallas, TX", "finish": "Houston, TX"}).json()
    summary = body["summary"]
    assert summary["nearest_station_to_finish"]["city"] == "Liberty"
    assert summary["number_of_stops"] == 1
    assert summary["fuel_left_at_finish_gallons"] >= summary["finish_reserve_miles"] / 10 - 0.01


FINISH_MIN_GALLONS = 2.5          # 25 miles: margin for fuel economy below 10 mpg


@pytest.mark.parametrize("miles", [430, 460, 475])
def test_trips_within_range_need_no_stop(client, miles):
    # Memphis has a listed station at its centre, so only the 25-mile margin is needed at the
    # finish: a full tank covers trips up to 475 miles.
    with mock.patch("fuelplan.services.routing.requests.get", return_value=FakeResponse(synthetic_route(miles))):
        body = client.get(URL, {"start": "Dallas, TX", "finish": "Memphis, TN"}).json()
    summary = body["summary"]
    assert body["fuel_stops"] == []
    assert summary["total_fuel_cost"] == 0
    assert summary["fuel_left_at_finish_gallons"] == pytest.approx((500 - miles) / 10)
    assert summary["finish_reserve_miles"] == 25.0
    assert summary["fuel_left_at_finish_gallons"] >= FINISH_MIN_GALLONS
    assert any("no fuel stop is needed" in note for note in body["notes"])


def test_trip_that_would_arrive_nearly_empty_stops(client):
    # 490 miles would leave 1 gallon: below the 2.5-gallon margin, so the truck stops.
    stations = [(250.0, 1.0, fake_station(900001, 3.0))]
    with mock.patch("fuelplan.services.routing.requests.get", return_value=FakeResponse(synthetic_route(490))), \
            mock.patch("fuelplan.services.planner._candidates", return_value=stations):
        body = client.get(URL, {"start": "Dallas, TX", "finish": "Memphis, TN"}).json()
    assert body["summary"]["number_of_stops"] == 1
    assert body["summary"]["fuel_left_at_finish_gallons"] >= RESERVE_GALLONS - 0.01


def test_new_york_to_chicago_arrives_with_the_reserve(client):
    with recorded("new_york_chicago"):
        body = client.get(URL, {"start": "New York, NY", "finish": "Chicago, IL"}).json()
    assert body["summary"]["number_of_stops"] == 1
    assert body["summary"]["fuel_left_at_finish_gallons"] == pytest.approx(8.5, abs=0.05)
    assert_rules_hold(body)


def test_indianapolis_to_kansas_city_stops_rather_than_arrive_empty(client):
    # 485 miles: a full tank would arrive with 1.5 gallons.
    with recorded("indianapolis_kansas_city"):
        body = client.get(URL, {"start": "Indianapolis, IN", "finish": "Kansas City, MO"}).json()
    summary = body["summary"]
    assert summary["number_of_stops"] == 1
    assert all(s["gallons_purchased"] >= MIN_PURCHASE - 0.01 for s in body["fuel_stops"])
    assert summary["fuel_left_at_finish_gallons"] >= FINISH_MIN_GALLONS


@pytest.mark.parametrize("start,finish,fixture", [
    ("Dallas, TX", "Memphis, TN", "dallas_memphis"),
    ("Nashville, TN", "Chicago, IL", "nashville_chicago"),
])
def test_trips_arriving_with_the_margin_need_no_stop(client, start, finish, fixture):
    with recorded(fixture):
        body = client.get(URL, {"start": start, "finish": finish}).json()
    assert body["summary"]["number_of_stops"] == 0
    assert body["summary"]["fuel_left_at_finish_gallons"] >= FINISH_MIN_GALLONS


@pytest.mark.parametrize("start,finish,fixture", [
    ("Phoenix, AZ", "Bakersfield, CA", "phoenix_bakersfield"),
    ("Denver, CO", "Santa Barbara, CA", "denver_santa_barbara"),
    ("Denver, CO", "Bakersfield, CA", "denver_bakersfield"),
])
def test_remote_destination_is_planned_with_a_note(client, start, finish, fixture):
    # No listed station is within about 200 miles of these destinations. The trip itself is
    # possible, so it is planned with the 25-mile margin and the response says to refuel locally.
    with recorded(fixture):
        resp = client.get(URL, {"start": start, "finish": finish})
    assert resp.status_code == 200
    body = resp.json()
    summary = body["summary"]
    assert summary["finish_reserve_miles"] == 25.0
    assert summary["fuel_left_at_finish_gallons"] >= FINISH_MIN_GALLONS - 0.01
    assert summary["nearest_station_to_finish"]["miles"] > 150
    assert any("refuel at a local station" in note for note in body["notes"])
    assert all(s["fuel_on_arrival_gallons"] >= RESERVE_GALLONS - 0.01 for s in body["fuel_stops"])


def test_route_with_a_station_gap_is_still_returned_with_the_reason(client):
    # Los Angeles to Seattle exists as a route, but the price list has no station for the first
    # 877 miles. The answer stays a 422, and now also carries the route, the stations that do
    # exist and the gap, so the trip can be shown with a warning.
    with recorded("los_angeles_seattle"):
        resp = client.get(URL, {"start": "Los Angeles, CA", "finish": "Seattle, WA"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "no_feasible_fuel_plan"
    assert "station not in the price list before mile" in body["error"]["message"]
    assert body["route"]["distance_miles"] > 1000
    assert len(body["route"]["geometry"]["coordinates"]) > 100
    gap = body["gap"]
    assert gap["from_mile"] == 0 and gap["to_mile"] > 500 and gap["miles"] == gap["to_mile"]
    assert gap["refuel_before_mile"] == pytest.approx(500 - 75)
    assert len(gap["geometry"]["coordinates"]) > 10
    assert body["stations_on_route"] and all(s["mile_marker"] >= gap["to_mile"] for s in body["stations_on_route"])
    assert body["start"]["query"] == "Los Angeles, CA" and body["finish"]["resolved"] == "Seattle, WA"
    assert "fuel_stops" not in body and "summary" not in body     # no plan is presented


def test_long_route_keeps_reserve_at_stops_and_at_finish(client, osrm):
    body = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"}).json()
    assert all(s["fuel_on_arrival_gallons"] >= RESERVE_GALLONS - 0.01 for s in body["fuel_stops"])
    assert body["summary"]["fuel_left_at_finish_gallons"] >= RESERVE_GALLONS - 0.01


def test_minimum_purchase_falls_back_with_a_note(client):
    # The truck can only reach mile 410 and must go on to the cheaper station at mile 420;
    # that hop needs 1 gallon, below the minimum, and it is not the last stop. The plan is
    # still made, without the minimum, and says so.
    stations = [(410.0, 1.0, fake_station(900001, 3.0)), (420.0, 1.0, fake_station(900002, 1.0))]
    with mock.patch("fuelplan.services.routing.requests.get", return_value=FakeResponse(synthetic_route(830))), \
            mock.patch("fuelplan.services.planner._candidates", return_value=stations):
        body = client.get(URL, {"start": "Dallas, TX", "finish": "Houston, TX"}).json()
    assert body["summary"]["number_of_stops"] == 2
    assert body["fuel_stops"][0]["gallons_purchased"] < MIN_PURCHASE
    assert any("minimum purchase" in note for note in body["notes"])


def test_map_url_carries_rule_overrides(client, osrm):
    body = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL",
                            "stop_cost": 5, "min_purchase_gallons": 4}).json()
    assert "stop_cost=5" in body["map_url"] and "min_purchase_gallons=4" in body["map_url"]
    assert "reserve_miles" not in body["map_url"]


@pytest.mark.parametrize("name,value", [
    ("stop_cost", -1), ("stop_cost", 51), ("reserve_miles", 201),
    ("min_purchase_gallons", 41), ("stop_cost", "abc"),
])
def test_rule_overrides_out_of_range_are_rejected(client, name, value):
    resp = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL", name: value})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["field"] == name


def test_stations_on_route_cover_every_option_the_planner_weighed(client, osrm):
    body = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"}).json()
    stations = body["stations_on_route"]
    miles = [s["mile_marker"] for s in stations]
    assert miles == sorted(miles)
    assert len(stations) == body["summary"]["stations_considered"]
    on_route = {s["station_id"] for s in stations}
    assert all(stop["station_id"] in on_route for stop in body["fuel_stops"])
    assert {"station_id", "name", "city", "state", "latitude", "longitude",
            "mile_marker", "price_per_gallon"} <= set(stations[0])


def test_each_stop_explains_its_decision(client, osrm):
    body = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"}).json()
    stops = body["fuel_stops"]
    for stop, nxt in zip(stops, stops[1:] + [None]):
        decision = stop["decision"]
        assert decision["in_range_count"] >= 1
        if nxt is None:
            assert decision["action"] == "finish" and decision["next_stop"] is None
        elif nxt["price_per_gallon"] <= stop["price_per_gallon"]:
            # The next stop is no dearer: buy just enough to get there.
            assert decision["action"] == "just_enough"
            assert decision["next_stop"]["station_id"] == nxt["station_id"]
        else:
            # The next stop is dearer: leave with a full tank.
            assert decision["action"] == "fill_up"
            assert stop["fuel_after_fill_gallons"] == pytest.approx(50.0, abs=0.05)


def test_only_one_routing_call_and_repeat_is_cached(client, osrm):
    client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"})
    assert osrm.call_count == 1
    client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"})
    assert osrm.call_count == 1


def test_post_with_json_body(client, osrm):
    resp = client.post(URL, {"start": "Chicago, IL", "finish": "Miami, FL"}, format="json")
    assert resp.status_code == 200
    assert resp.json()["summary"]["number_of_stops"] >= 2


def test_short_route_needs_no_stop(client):
    with mock.patch("fuelplan.services.routing.requests.get", return_value=FakeResponse(synthetic_route(300))):
        body = client.get(URL, {"start": "Dallas, TX", "finish": "Houston, TX"}).json()
    assert body["fuel_stops"] == []
    assert body["stations_on_route"] == []
    assert body["summary"]["total_fuel_cost"] == 0
    assert body["summary"]["fuel_consumed_gallons"] == 30.0


@pytest.mark.parametrize("params", [{}, {"start": "Chicago, IL"}, {"finish": "Miami, FL"}])
def test_missing_parameters(client, params):
    resp = client.get(URL, params)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_location_outside_usa(client):
    resp = client.get(URL, {"start": "Toronto, ON", "finish": "Miami, FL"})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "location_not_resolved"
    assert error["field"] == "start"


def test_windsor_coordinates_are_rejected(client):
    resp = client.get(URL, {"start": "42.3149,-83.0364", "finish": "Miami, FL"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "location_not_resolved"
    assert resp.json()["error"]["field"] == "start"


def test_misspelled_finish_names_the_field_and_suggests(client):
    resp = client.get(URL, {"start": "Chicago, IL", "finish": "Maimi, FL"})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["field"] == "finish"
    assert "Miami, FL" in error["suggestions"]


def test_same_start_and_finish(client):
    resp = client.get(URL, {"start": "Chicago, IL", "finish": "Chicago, Illinois"})
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "finish"


def test_malformed_json_body(client):
    resp = client.post(URL, "{not json", content_type="application/json")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_method_not_allowed_uses_error_shape(client):
    resp = client.delete(URL)
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "method_not_allowed"


def test_unknown_api_path_returns_json_404(client):
    resp = client.get("/api/v1/nope/")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_rate_limit_returns_429_in_error_shape(client, osrm):
    from rest_framework.throttling import ScopedRateThrottle
    with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"route": "2/min", "locations": "600/min"}):
        params = {"start": "Chicago, IL", "finish": "Miami, FL"}
        assert client.get(URL, params).status_code == 200
        assert client.get(URL, params).status_code == 200
        resp = client.get(URL, params)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"


def test_costs_are_exact_receipts_and_the_total_is_their_sum(client, osrm):
    body = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"}).json()
    stops = body["fuel_stops"]
    for s in stops:
        assert s["cost"] == round(s["gallons_purchased"] * s["price_per_gallon"], 2)
    assert body["summary"]["total_fuel_cost"] == round(sum(s["cost"] for s in stops), 2)
    assert body["summary"]["fuel_purchased_gallons"] == round(sum(s["gallons_purchased"] for s in stops), 2)


def test_trip_fuel_cost_prices_the_starting_tank_at_the_first_stop(client, osrm):
    body = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"}).json()
    s = body["summary"]
    from_start = s["fuel_consumed_gallons"] - s["fuel_purchased_gallons"]
    first_price = body["fuel_stops"][0]["price_per_gallon"]
    assert s["trip_fuel_cost"] == pytest.approx(from_start * first_price + s["total_fuel_cost"], abs=0.02)
    assert s["trip_fuel_cost"] > s["total_fuel_cost"]


def test_trip_fuel_cost_without_stops_uses_the_corridor_mean(client):
    stations = [(100.0, 1.0, fake_station(900001, 3.0)), (200.0, 1.0, fake_station(900002, 4.0))]
    with mock.patch("fuelplan.services.routing.requests.get", return_value=FakeResponse(synthetic_route(300))), \
            mock.patch("fuelplan.services.planner._candidates", return_value=stations):
        body = client.get(URL, {"start": "Dallas, TX", "finish": "Houston, TX"}).json()
    assert body["summary"]["total_fuel_cost"] == 0
    assert body["summary"]["trip_fuel_cost"] == pytest.approx(30.0 * 3.5)    # 30 gal at the $3.50 mean


def test_cached_plan_echoes_each_requests_own_text(client, osrm):
    first = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"}).json()
    second = client.get(URL, {"start": "chicago, illinois", "finish": "miami, florida"}).json()
    assert first["start"]["query"] == "Chicago, IL"
    assert second["start"]["query"] == "chicago, illinois"
    assert second["finish"]["query"] == "miami, florida"
    assert second["start"]["resolved"] == "Chicago, IL"
    assert osrm.call_count == 1                      # the second request was served from the cache


def test_spoofed_forwarded_for_does_not_reset_the_rate_limit(client):
    from rest_framework.throttling import ScopedRateThrottle
    with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"route": "3/min", "locations": "600/min"}):
        codes = [client.get(URL, {"start": "Chicago, IL"}, HTTP_X_FORWARDED_FOR="203.0.113.%d" % i).status_code
                 for i in range(4)]
    # Three requests are allowed; the fourth is throttled although it claims a new address.
    assert codes[:3] == [400, 400, 400]
    assert codes[3] == 429


def test_location_suggestions_endpoint(client):
    body = client.get("/api/v1/locations/", {"q": "chi"}).json()
    assert body["results"][0]["label"] == "Chicago, IL"
    assert {"label", "name", "state", "latitude", "longitude"} <= set(body["results"][0])
    assert client.get("/api/v1/locations/", {"q": "FL"}).json()["results"][0]["state"] == "FL"
    assert client.get("/api/v1/locations/").json()["results"] == []


def test_stations_come_from_the_database(client, osrm):
    from fuelplan.models import Station
    params = {"start": "Chicago, IL", "finish": "Miami, FL"}
    stop = client.get(URL, params).json()["fuel_stops"][0]
    row = Station.objects.get(opis_id=int(stop["station_id"]))
    assert stop["name"] == row.name and stop["price_per_gallon"] == round(float(row.price), 3)


def test_no_route_between_locations(client):
    with mock.patch("fuelplan.services.routing.requests.get",
                    return_value=FakeResponse({"code": "NoRoute"}, status=400)):
        resp = client.get(URL, {"start": "Honolulu, HI", "finish": "Miami, FL"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "route_not_found"


def test_routing_service_down(client):
    with mock.patch("fuelplan.services.routing.requests.get",
                    side_effect=requests.ConnectionError("boom")) as patched:
        resp = client.get(URL, {"start": "Chicago, IL", "finish": "Miami, FL"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "routing_service_unavailable"
    assert patched.call_count == 2          # one retry


@pytest.mark.parametrize("path", ["/", "/api/v1/route/map/?start=Chicago%2C+IL&finish=Miami%2C+FL"])
def test_map_page_renders(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Plan route" in html and "leaflet" in html
    assert 'data-route-url="/api/v1/route/"' in html
    assert 'data-suggest-url="/api/v1/locations/"' in html
