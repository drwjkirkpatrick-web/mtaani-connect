"""URL routes for the Fleet app (Prompt 26).

app_name = "fleet" — every name lives in this namespace so templates and
tests reverse names like ``fleet:dashboard``. mtaani/urls.py already
includes this module under /fleet/.
"""
from django.urls import path

from . import views

app_name = "fleet"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("vehicles/new/", views.vehicle_register, name="vehicle_register"),
    path("vehicles/<int:pk>/", views.vehicle_detail, name="vehicle_detail"),
    path("vehicles/<int:pk>/routes/", views.vehicle_routes, name="vehicle_routes"),
    path("vehicles/<int:pk>/retire/", views.vehicle_retire, name="vehicle_retire"),
    path("crew/request/", views.crew_request, name="crew_request"),
    path("crew/mine/", views.crew_mine, name="crew_mine"),
    path("crew/pending/", views.crew_pending, name="crew_pending"),
    path("crew/<int:seat_pk>/approve/", views.crew_approve, name="crew_approve"),
    path("crew/<int:seat_pk>/revoke/", views.crew_revoke, name="crew_revoke"),
    path("vehicles/<int:vehicle_pk>/crew/", views.crew_roster, name="crew_roster"),
    path("search/", views.search, name="search"),
]
