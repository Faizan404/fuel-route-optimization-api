import functools
import random

import pytest

from fuelplan.services.optimizer import optimize


def plan_cost(purchases, prices, mpg):
    return sum(p.miles / mpg * prices[p.index] for p in purchases)


def objective(purchases, prices, mpg, stop_cost):
    return plan_cost(purchases, prices, mpg) + stop_cost * len(purchases)


def brute_force(miles, prices, distance, tank, start_fuel, mpg,
                stop_cost=0.0, reserve=0, finish_reserve=None):
    """Independent oracle: exhaustive search over whole-mile purchases.

    Models the rules directly, without the optimiser's shift: the tank holds
    `tank` miles, every arrival at a station needs at least `reserve` miles left,
    the finish needs `finish_reserve` left (default: reserve), and each stop that buys fuel costs
    `stop_cost`. Returns the lowest fuel cost + stop costs, or inf.
    """
    n = len(miles)
    finish_left = reserve if finish_reserve is None else finish_reserve

    @functools.lru_cache(None)
    def go(i, fuel):
        position = 0 if i < 0 else miles[i]
        best = float("inf")
        options = [0] if i < 0 else range(0, tank - fuel + 1)
        for buy in options:
            have = fuel + buy
            cost = (buy * prices[i] / mpg + stop_cost) if buy else 0.0
            if have - (distance - position) >= finish_left:
                best = min(best, cost)
            for j in range(i + 1, n):
                left = have - (miles[j] - position)
                if left < reserve:
                    break
                best = min(best, cost + go(j, left))
        return best

    return go(-1, start_fuel)


# --- original behaviour (no stop cost, no reserve) --------------------------------

def test_no_stop_needed_when_trip_fits_in_tank():
    assert optimize([100.0], [3.0], 400.0, 500.0, 500.0, 10.0) == []


def test_infeasible_when_gap_exceeds_range():
    assert optimize([100.0], [3.0], 700.0, 500.0, 500.0, 10.0) is None
    assert optimize([], [], 600.0, 500.0, 500.0, 10.0) is None


def test_buys_only_what_is_needed_at_single_station():
    purchases = optimize([300.0], [3.0], 600.0, 500.0, 500.0, 10.0)
    assert [(p.index, round(p.miles, 6)) for p in purchases] == [(0, 100.0)]


def test_prefers_cheaper_station_and_fills_up_before_dearer_one():
    # Cheap station at mile 400, dear one at mile 800, finish at mile 1200.
    purchases = optimize([400.0, 800.0], [2.0, 4.0], 1200.0, 500.0, 500.0, 10.0)
    bought = {p.index: round(p.miles, 6) for p in purchases}
    # Fill the tank at the cheap station (400 -> 500 mi of range), top up the rest at the dear one.
    assert bought == {0: 400.0, 1: 300.0}


def test_skips_dear_station_when_cheaper_one_is_reachable():
    purchases = optimize([200.0, 450.0], [5.0, 2.0], 700.0, 500.0, 500.0, 10.0)
    assert [p.index for p in purchases] == [1]


def test_every_leg_respects_tank_range():
    rng = random.Random(3)
    miles = sorted(rng.uniform(0, 3000) for _ in range(120))
    prices = [rng.uniform(2.7, 4.5) for _ in miles]
    purchases = optimize(miles, prices, 3000.0, 490.0, 490.0, 10.0)
    assert purchases
    fuel, position = 490.0, 0.0
    for p in purchases:
        fuel -= miles[p.index] - position
        assert fuel >= -1e-6
        fuel += p.miles
        assert fuel <= 490.0 + 1e-6
        position = miles[p.index]
    assert fuel - (3000.0 - position) >= -1e-6


@pytest.mark.parametrize("seed", range(4))
def test_matches_brute_force_on_random_instances(seed):
    rng = random.Random(seed)
    for _ in range(100):
        tank = rng.choice([10, 20])
        distance = rng.randint(2, 45)
        count = min(rng.randint(0, 7), distance - 1)
        miles = sorted(rng.sample(range(1, distance), count))
        prices = [rng.choice([1.0, 2.0, 3.0, 5.0]) for _ in miles]
        start_fuel = rng.choice([0, tank // 2, tank])
        expected = brute_force(tuple(miles), tuple(prices), distance, tank, start_fuel, 10.0)
        result = optimize([float(m) for m in miles], prices, float(distance), float(tank),
                          float(start_fuel), 10.0)
        if result is None:
            assert expected == float("inf")
        else:
            assert plan_cost(result, prices, 10.0) == pytest.approx(expected, abs=1e-6)


# --- stop cost and safety reserve: still exact -------------------------------------

@pytest.mark.parametrize("seed", range(5))
def test_stop_cost_and_reserve_match_brute_force(seed):
    rng = random.Random(100 + seed)
    for _ in range(100):
        tank = rng.choice([10, 20])
        reserve = rng.choice([0, 1, 2, 4])
        # Fuel needed at the finish: none, less than, equal to or more than the reserve.
        finish_reserve = rng.choice([0, reserve // 2, reserve, reserve + 1, reserve + 3])
        stop_cost = rng.choice([0.0, 0.5, 1.0, 3.0])
        distance = rng.randint(2, 45)
        count = min(rng.randint(0, 7), distance - 1)
        miles = sorted(rng.sample(range(1, distance), count))
        prices = [rng.choice([1.0, 2.0, 3.0, 5.0]) for _ in miles]
        start_fuel = rng.choice([reserve, tank // 2 + reserve // 2, tank])
        expected = brute_force(tuple(miles), tuple(prices), distance, tank, start_fuel, 10.0,
                               stop_cost, reserve, finish_reserve)
        result = optimize([float(m) for m in miles], prices, float(distance), float(tank),
                          float(start_fuel), 10.0, stop_cost=stop_cost, reserve=float(reserve),
                          finish_reserve=float(finish_reserve))
        if result is None:
            assert expected == float("inf")
        else:
            assert objective(result, prices, 10.0, stop_cost) == pytest.approx(expected, abs=1e-6)


def test_stop_cost_removes_stops_that_save_cents():
    # Prices rise by a cent every 20 miles before a dear stretch. Without a stop cost the
    # plan fills at mile 100 and then tops up 2 gallons at every station after it, each
    # top-up saving about 2 cents.
    miles = [100.0, 120.0, 140.0, 160.0, 180.0, 600.0]
    prices = [3.00, 3.01, 3.02, 3.03, 3.04, 3.50]
    free = optimize(miles, prices, 1000.0, 500.0, 500.0, 10.0)
    priced = optimize(miles, prices, 1000.0, 500.0, 500.0, 10.0, stop_cost=3.0)
    assert len(free) == 6
    assert len(priced) == 2
    # Four fewer stops ($12 of stop cost) for about 50 cents more fuel.
    extra_fuel = plan_cost(priced, prices, 10.0) - plan_cost(free, prices, 10.0)
    assert 0 <= extra_fuel < 1.0


def test_reserve_forces_an_earlier_stop():
    # Without a reserve the truck reaches the cheap station at mile 450 on its starting
    # fuel. With a 75-mile reserve it would arrive there with only 50 miles left, so it
    # must stop at mile 300 instead.
    miles, prices = [300.0, 450.0], [2.0, 1.0]
    assert [p.index for p in optimize(miles, prices, 700.0, 500.0, 500.0, 10.0)] == [1]
    with_reserve = optimize(miles, prices, 700.0, 500.0, 500.0, 10.0, reserve=75.0)
    assert with_reserve[0].index == 0
    fuel, position = 500.0, 0.0
    for p in with_reserve:
        fuel -= miles[p.index] - position
        assert fuel >= 75.0 - 1e-6       # every arrival keeps the reserve
        fuel += p.miles
        position = miles[p.index]
    assert fuel - (700.0 - position) >= 75.0 - 1e-6


def test_finish_reserve_can_differ_from_the_reserve():
    # 460 miles on a 500-mile tank with a 75-mile reserve at stops.
    args = ([200.0], [3.0], 460.0, 500.0, 500.0, 10.0)
    # Needing only 20 miles at the finish: no stop.
    assert optimize(*args, reserve=75.0, finish_reserve=20.0) == []
    # Needing 75 miles at the finish: a stop.
    assert optimize(*args, reserve=75.0, finish_reserve=75.0) != []
    # Needing more than the reserve (120 miles): the stop buys enough for it.
    purchases = optimize(*args, reserve=75.0, finish_reserve=120.0)
    assert 500.0 - 200.0 + purchases[0].miles - 260.0 >= 120.0 - 1e-6


# --- minimum purchase: a guard ----------------------------------------------------

def test_minimum_purchase_is_respected():
    rng = random.Random(7)
    miles = sorted(rng.uniform(0, 3000) for _ in range(150))
    prices = [rng.uniform(2.7, 4.5) for _ in miles]
    purchases = optimize(miles, prices, 3000.0, 490.0, 490.0, 10.0,
                         stop_cost=3.0, reserve=75.0, min_purchase=50.0)
    assert purchases
    assert all(p.miles >= 50.0 - 1e-6 for p in purchases)


def test_last_stop_rounds_a_small_purchase_up_to_the_minimum():
    # The only station needs 15 miles (1.5 gal) to finish with the reserve; with a 5-gallon
    # minimum the last stop buys 5 gallons instead of being ruled out.
    purchases = optimize([300.0], [3.0], 430.0, 490.0, 490.0, 10.0, reserve=75.0, min_purchase=50.0)
    assert [(p.index, round(p.miles, 6)) for p in purchases] == [(0, 50.0)]


def test_last_stop_needs_room_for_the_minimum():
    # Stopping at mile 20 would leave room for only 20 miles (2 gal) of fuel: below a 5-gallon
    # minimum, so it is not used. The later station at mile 200 has room and is chosen.
    purchases = optimize([20.0, 200.0], [2.0, 3.0], 450.0, 490.0, 490.0, 10.0,
                         reserve=75.0, min_purchase=50.0)
    assert [p.index for p in purchases] == [1]
    assert all(p.miles >= 50.0 - 1e-6 for p in purchases)


def test_minimum_purchase_can_make_a_plan_infeasible():
    # With a 75-mile reserve the truck can only reach the station at mile 410, which cannot
    # reach the finish at mile 830; it must go on to mile 420. That hop needs 5 miles of fuel
    # (0.5 gal), below a 5-gallon minimum, and it is not the last stop, so no plan meets the guard.
    args = ([410.0, 420.0], [3.0, 1.0], 830.0, 490.0, 490.0, 10.0)
    assert optimize(*args, reserve=75.0) is not None
    assert optimize(*args, reserve=75.0, min_purchase=50.0) is None


def test_realistic_long_route_with_all_rules():
    # 3,000 miles with a station roughly every 20 miles at realistic prices.
    rng = random.Random(42)
    miles = sorted(rng.uniform(0, 3000) for _ in range(150))
    prices = [round(rng.uniform(2.8, 3.8), 3) for _ in miles]
    args = (miles, prices, 3000.0, 490.0, 490.0, 10.0)
    free = optimize(*args, reserve=75.0)                     # same reserve, stops are free
    rules = optimize(*args, stop_cost=3.0, reserve=75.0, min_purchase=50.0)
    assert len(rules) < len(free)
    # Pump cost stays close to the free-stop optimum.
    assert plan_cost(rules, prices, 10.0) <= plan_cost(free, prices, 10.0) * 1.03
    fuel, position = 490.0, 0.0
    for p in rules:
        fuel -= miles[p.index] - position
        assert fuel >= 75.0 - 1e-3        # never below the reserve on arrival
        assert p.miles >= 50.0 - 1e-6     # never less than 5 gallons
        fuel += p.miles
        position = miles[p.index]
    assert fuel - (3000.0 - position) >= 75.0 - 1e-3
