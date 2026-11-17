from django.urls import include, path

urlpatterns = [
    path("", include("fuelplan.urls")),
]

handler404 = "fuelplan.exceptions.not_found"
handler500 = "fuelplan.exceptions.server_error"
