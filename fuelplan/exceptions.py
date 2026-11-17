"""Every API error leaves in one shape: {"error": {"code": ..., "message": ...}}."""
import logging

from django.http import JsonResponse
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler

log = logging.getLogger(__name__)

CODES = {
    exceptions.Throttled: "rate_limited",
    exceptions.ParseError: "invalid_request",
    exceptions.UnsupportedMediaType: "invalid_request",
    exceptions.MethodNotAllowed: "method_not_allowed",
}


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        # Not a DRF exception: a genuine bug or an unavailable dependency.
        log.exception("Unhandled error in %s", context.get("view"))
        return Response(
            {"error": {"code": "internal_error",
                       "message": "Something went wrong on our side. Please try again."}},
            status=500,
        )

    code = next((c for cls, c in CODES.items() if isinstance(exc, cls)), "invalid_request")
    if isinstance(exc, exceptions.Throttled):
        wait = int(exc.wait or 0) + 1
        message = f"Too many requests. Try again in {wait} second{'s' if wait != 1 else ''}."
    elif isinstance(exc, exceptions.ParseError):
        message = "The request body is not valid JSON."
    else:
        message = str(getattr(exc, "detail", exc))
    response.data = {"error": {"code": code, "message": message}}
    return response


def not_found(request, exception=None):
    if request.path.startswith("/api/"):
        return JsonResponse(
            {"error": {"code": "not_found", "message": f"No endpoint at {request.path}."}},
            status=404,
        )
    return JsonResponse({"error": {"code": "not_found", "message": "Page not found."}}, status=404)


def server_error(request):
    return JsonResponse(
        {"error": {"code": "internal_error",
                   "message": "Something went wrong on our side. Please try again."}},
        status=500,
    )
