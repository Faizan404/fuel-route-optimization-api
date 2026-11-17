class PlanError(Exception):
    """A request that cannot be fulfilled, with an HTTP status and a stable code."""

    status = 400
    code = "invalid_request"

    def __init__(self, message, field=None, suggestions=None, details=None):
        super().__init__(message)
        self.message = message
        self.field = field                    # "start" or "finish" when one input is at fault
        self.suggestions = suggestions or []  # location labels the user may have meant
        self.details = details or {}          # extra data returned next to "error"

    def as_dict(self):
        body = {"code": self.code, "message": self.message}
        if self.field:
            body["field"] = self.field
        if self.suggestions:
            body["suggestions"] = self.suggestions
        return {"error": body, **self.details}


class LocationError(PlanError):
    code = "location_not_resolved"


class RouteNotFound(PlanError):
    status = 422
    code = "route_not_found"


class InfeasiblePlan(PlanError):
    status = 422
    code = "no_feasible_fuel_plan"


class UpstreamError(PlanError):
    status = 502
    code = "routing_service_unavailable"
