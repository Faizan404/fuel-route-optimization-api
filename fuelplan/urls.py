from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/route/", views.RoutePlanView.as_view(), name="route-plan"),
    path("api/v1/route/map/", views.route_map, name="route-map"),
    path("api/v1/locations/", views.LocationSuggestView.as_view(), name="location-suggest"),
    path("", views.route_map, name="home"),
]
