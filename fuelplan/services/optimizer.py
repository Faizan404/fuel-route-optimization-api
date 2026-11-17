"""Refuelling plan along a fixed route: lowest fuel cost plus a cost per stop.

This is the fixed-path "gas station problem" (Khuller, Malekian, Mestre,
"To Fill or Not to Fill"). For any *fixed set* of stops, the cheapest way to buy
fuel has a simple shape: between consecutive stops i -> j the vehicle either
leaves i with a full tank (i is cheaper than j) or arrives at j empty (j is no
dearer than i). That limits the fuel levels worth tracking on arrival at a
station to "empty" and "full minus the miles since an earlier stop", so an exact
dynamic programme over (station, arrival fuel) stays small.

Business rules, and why each keeps (or does not keep) the programme exact:

* Stop cost. Every stop adds `stop_cost` dollars to the objective, standing in
  for the driver's time to exit, queue, pay and re-enter. The added cost depends
  only on *which* stops are used, not on how much fuel is bought at them, so the
  lemma above still holds for every fixed stop set and the same state space still
  contains an optimal plan. Exact.

* Safety reserve. The truck must arrive at every stop with at least `reserve`
  miles of fuel. Subtracting the reserve from the tank size and from the starting
  fuel turns this into the plain problem with "empty" meaning "at the reserve":
  every fuel level shifts down by the same amount and nothing else changes. Exact.

* Fuel at the finish. The truck must reach the finish with `finish_reserve`
  miles of fuel, which may be more or less than `reserve`. This only changes the
  last leg: in the shifted levels it is the same as moving the finish by
  `finish_reserve - reserve` miles. Exact.

* Minimum purchase. Transitions between stops that buy less than `min_purchase`
  are skipped. At the last stop a smaller purchase is rounded up to the minimum
  instead, since the extra fuel only reaches the finish (a station where the tank
  has no room for the minimum is not used as the last stop). This is a guard, not a
  proven-optimal rule: with a minimum, buying more than "just enough" can be the
  best option, which creates arrival levels outside the state space, so the
  result may be slightly dearer than the true constrained optimum, or infeasible
  where a plan exists. In practice the stop cost already removes nearly every
  tiny purchase, so the guard rarely binds.

All fuel quantities here are expressed in miles of range.
"""
from dataclasses import dataclass

EPS = 1e-9
# Added on top of the stop cost so that, between plans of equal cost, fewer stops win.
TIE_BREAK_PER_STOP = 1e-6


@dataclass(frozen=True, slots=True)
class Purchase:
    index: int          # position in the input station list
    miles: float        # miles of range bought


def optimize(miles, prices, distance, tank_range, start_fuel, mpg, *,
             stop_cost=0.0, reserve=0.0, finish_reserve=None, min_purchase=0.0):
    """Return the plan with the lowest (fuel cost + stop_cost x stops).

    Returns a list of Purchases (empty when no stop is needed), or None when no
    plan can finish the route.

    miles             -- station mile markers along the route, ascending
    prices            -- price per gallon at each station
    distance          -- total route miles
    tank_range        -- miles on a full tank
    start_fuel        -- miles of range in the tank at departure
    stop_cost         -- dollars charged per stop
    reserve           -- miles of fuel that must remain on arrival at every stop
    finish_reserve    -- miles of fuel that must remain at the finish (default: reserve)
    min_purchase      -- smallest purchase allowed at a stop, in miles of range
    """
    # The reserve as a shift: "empty" now means "at the reserve".
    tank = tank_range - reserve
    start = start_fuel - reserve
    # Fuel the last leg may use beyond the shifted "empty": positive when less than the
    # reserve is needed at the finish, negative when more is.
    if finish_reserve is None:
        finish_reserve = reserve
    slack = reserve - finish_reserve
    per_stop = stop_cost + TIE_BREAK_PER_STOP

    if tank <= EPS:
        return None
    if distance <= start + slack + EPS:
        return []

    n = len(miles)
    # best[i][fuel_on_arrival] = (objective, parent_state or None, miles bought at parent)
    best = [dict() for _ in range(n)]
    for i in range(n):
        if miles[i] > start + EPS:
            break
        best[i][round(start - miles[i], 6)] = (0.0, None, 0.0)

    final = None
    for i in range(n):
        if not best[i]:
            continue
        here, price = miles[i], prices[i]
        unit = price / mpg   # dollars per mile of range

        successors = []
        j = i + 1
        while j < n and miles[j] - here <= tank + EPS:
            if miles[j] - here > EPS:
                successors.append((j, miles[j] - here, prices[j]))
            j += 1
        to_finish = distance - here - slack
        finish_reachable = to_finish <= tank + EPS

        for fuel, (cost, _, _) in list(best[i].items()):
            if finish_reachable and fuel <= to_finish + EPS:
                buy = max(0.0, to_finish - fuel)
                if buy + EPS < min_purchase:
                    # Last stop: round a small purchase up to the minimum; the extra fuel only
                    # arrives at the finish. Where the tank has no room for the minimum (the
                    # truck is nearly full), this station is not a sensible last stop.
                    buy = min_purchase if tank - fuel + EPS >= min_purchase else None
                if buy is not None:
                    total = cost + per_stop + buy * unit
                    if final is None or total < final[0] - 1e-12:
                        final = (total, (i, fuel), buy)

            fill = tank - fuel
            filled_cost = cost + per_stop + fill * unit
            fill_allowed = fill + EPS >= min_purchase
            for j, gap, next_price in successors:
                if next_price <= price:
                    # Next stop is no dearer: buy just enough to arrive there at the reserve.
                    if fuel <= gap + EPS:
                        buy = max(0.0, gap - fuel)
                        if buy + EPS < min_purchase:
                            continue
                        total = cost + per_stop + buy * unit
                        current = best[j].get(0.0)
                        if current is None or total < current[0] - 1e-12:
                            best[j][0.0] = (total, (i, fuel), buy)
                elif fill_allowed:
                    # Next stop is dearer: leave here full.
                    key = round(tank - gap, 6)
                    current = best[j].get(key)
                    if current is None or filled_cost < current[0] - 1e-12:
                        best[j][key] = (filled_cost, (i, fuel), fill)

    if final is None:
        return None

    purchases = []
    _, parent, bought = final
    while parent is not None:
        i, fuel = parent
        purchases.append(Purchase(index=i, miles=bought))
        _, parent, bought = best[i][fuel]
    purchases.reverse()
    return purchases
