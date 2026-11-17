from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteQuerySerializer
from .services import gazetteer
from .services.errors import PlanError
from .services.planner import build_plan


class RoutePlanView(APIView):
    """GET /api/v1/route/?start=Chicago, IL&finish=Miami, FL  (POST with a JSON body also works)."""

    throttle_scope = "route"

    def get(self, request):
        return self._respond(request, request.query_params)

    def post(self, request):
        return self._respond(request, request.data)

    def _respond(self, request, data):
        query = RouteQuerySerializer(data=data)
        if not query.is_valid():
            errors = query.errors
            missing = [name for name in ("start", "finish") if name in errors]
            if missing:
                message = "Enter both a start and a finish location."
            else:
                name = next(iter(errors))
                message = "Invalid %s: %s" % (name, " ".join(str(e) for e in errors[name]))
            body = {"error": {"code": "invalid_request", "message": message, "fields": errors}}
            # Point at one field when it is clear which: a single missing location, or
            # the first rule override that is out of range.
            if len(missing) == 1:
                body["error"]["field"] = missing[0]
            elif not missing:
                body["error"]["field"] = next(iter(errors))
            return Response(body, status=400)
        params = query.validated_data
        try:
            plan = build_plan(
                params["start"], params["finish"],
                stop_cost=params.get("stop_cost"),
                reserve_miles=params.get("reserve_miles"),
                min_purchase_gallons=params.get("min_purchase_gallons"),
            )
        except PlanError as exc:
            return Response(exc.as_dict(), status=exc.status)
        # The map link carries the same rule overrides, so it shows the same plan.
        link = {k: v for k, v in params.items() if v is not None}
        map_path = reverse("route-map") + "?" + urlencode(link)
        return Response({**plan, "map_url": request.build_absolute_uri(map_path)})


class LocationSuggestView(APIView):
    """GET /api/v1/locations/?q=chi  ->  places matching what has been typed so far."""

    throttle_scope = "locations"

    def get(self, request):
        query = request.query_params.get("q", "")[:100]
        places = gazetteer.suggest(query, limit=8)
        return Response({
            "query": query,
            "results": [
                {"label": p.label, "name": p.name, "state": p.state,
                 "latitude": p.latitude, "longitude": p.longitude}
                for p in places
            ],
        })


def _plain(value):
    """3.0 -> "3", 2.5 -> "2.5": friendlier numbers for the page text."""
    return ("%f" % value).rstrip("0").rstrip(".")


def route_map(request):
    """The map page. It calls the API from the browser, using ?start=&finish= when present."""
    cfg = settings.FUEL_PLAN
    rules = {
        "stop_cost": _plain(cfg["STOP_COST_USD"]),
        "reserve_miles": _plain(cfg["SAFETY_RESERVE_MILES"]),
        "min_purchase": _plain(cfg["MIN_PURCHASE_GALLONS"]),
    }
    return render(request, "fuelplan/map.html", {"rules": rules})
