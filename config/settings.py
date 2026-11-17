import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-for-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "rest_framework",
    "fuelplan",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DATABASE_PATH", str(BASE_DIR / "db.sqlite3")),
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "OPTIONS": {"MAX_ENTRIES": 500},
    }
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "route": os.environ.get("API_THROTTLE_RATE", "60/min"),
        # Suggestions fire as the user types, so they get a much higher allowance.
        "locations": os.environ.get("SUGGEST_THROTTLE_RATE", "600/min"),
    },
    "EXCEPTION_HANDLER": "fuelplan.exceptions.api_exception_handler",
    # Identify clients by the connecting address. With the default (None), DRF trusts the
    # X-Forwarded-For header, so a client can reset its rate limit by sending a new one.
    "NUM_PROXIES": 0,
}

USE_TZ = True

# OpenStreetMap's tile servers reject requests that carry no Referer. Django's default
# ("same-origin") strips it from cross-origin requests, so the map tiles came back 403.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

# --- Fuel planning -----------------------------------------------------------
FUEL_PLAN = {
    "TANK_RANGE_MILES": 500.0,         # full tank; the vehicle departs full
    "MPG": 10.0,
    # Business rules for realistic stops (each can be overridden per request).
    "STOP_COST_USD": 3.0,              # driver time per stop, priced into the plan
    "SAFETY_RESERVE_MILES": 75.0,      # fuel left on arrival at every stop (7.5 gal)
    "FINISH_MIN_MILES": 25.0,          # least fuel at the destination: 5% of the tank, for
                                       # fuel economy below 10 mpg
    "MIN_PURCHASE_GALLONS": 5.0,       # no stop buys less than this (a guard)
    "CORRIDOR_MILES": 5.0,            # how far from the route a station may be
    "CORRIDOR_WIDENING": [10.0, 25.0, 50.0],  # tried in order when no plan is feasible
    "MAX_CANDIDATES": 500,
    "ROUTE_CACHE_SECONDS": 24 * 3600,
    "PLAN_CACHE_SECONDS": 3600,
}

ROUTING = {
    "OSRM_BASE_URL": os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org"),
    "TIMEOUT_SECONDS": float(os.environ.get("ROUTING_TIMEOUT_SECONDS", "15")),
    "USER_AGENT": "fuel-route-planner/1.0",
}
