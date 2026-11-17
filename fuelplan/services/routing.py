"""Driving route from the OSRM HTTP API: one external call per uncached request."""
import logging
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from .errors import RouteNotFound, UpstreamError
from .geo import METERS_PER_MILE, decode_polyline

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Route:
    points: tuple          # ((lat, lon), ...)
    distance_miles: float
    duration_hours: float


def _request(url, params):
    cfg = settings.ROUTING
    last_error = None
    for attempt in (1, 2):   # one retry on timeouts / 5xx
        try:
            resp = requests.get(
                url, params=params, timeout=cfg["TIMEOUT_SECONDS"],
                headers={"User-Agent": cfg["USER_AGENT"]},
            )
        except requests.RequestException as exc:
            last_error = exc
            log.warning("routing attempt %s failed: %s", attempt, exc)
            continue
        if resp.status_code >= 500 or resp.status_code == 429:
            last_error = RuntimeError(f"HTTP {resp.status_code}")
            log.warning("routing attempt %s returned HTTP %s", attempt, resp.status_code)
            continue
        return resp
    raise UpstreamError(f"The routing service is unavailable ({last_error}). Try again shortly.")


def fetch_route(start, finish):
    """start/finish: objects with .lat/.lon. Result is cached by rounded coordinates."""
    key = "route:%.4f,%.4f:%.4f,%.4f" % (start.lat, start.lon, finish.lat, finish.lon)
    cached = cache.get(key)
    if cached is not None:
        return cached

    url = "%s/route/v1/driving/%.6f,%.6f;%.6f,%.6f" % (
        settings.ROUTING["OSRM_BASE_URL"].rstrip("/"), start.lon, start.lat, finish.lon, finish.lat,
    )
    resp = _request(url, {"overview": "full", "geometries": "polyline", "steps": "false"})
    try:
        body = resp.json()
    except ValueError:
        raise UpstreamError("The routing service returned an unreadable response.")

    code = body.get("code")
    if code in ("NoRoute", "NoSegment"):
        raise RouteNotFound("No driving route exists between these two locations.")
    if code != "Ok" or not body.get("routes"):
        raise UpstreamError(f"The routing service rejected the request ({code}).")

    data = body["routes"][0]
    points = tuple(decode_polyline(data["geometry"]))
    if len(points) < 2:
        raise RouteNotFound("No driving route exists between these two locations.")
    route = Route(
        points=points,
        distance_miles=data["distance"] / METERS_PER_MILE,
        duration_hours=data["duration"] / 3600.0,
    )
    cache.set(key, route, settings.FUEL_PLAN["ROUTE_CACHE_SECONDS"])
    return route
