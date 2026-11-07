"""URL routes for the monetization hooks (PROMPTS.md Prompt 38).

app_name = "monetize" — namespaced so templates/tests reverse
``monetize:…``; mtaani/urls.py already includes this module under
/monetize/ (parent-wired, read-only for this app).

NOTE: there is deliberately NO route that renders promoted listings or
paid features — this app is a command/library surface; the promoted door
is consumed by the search-results seam the parent wires (README.md).
"""
from django.urls import path

from . import views

app_name = "monetize"

urlpatterns = [
    path("", views.flag_list, name="flags"),  # staff: switches + audit trail
    path("flags/<int:pk>/<str:action>/", views.flag_flip, name="flag_flip"),  # POST
    path("sacco/<int:pk>/", views.sacco_premium, name="sacco"),  # sacco admin or staff
    path("sacco/<int:pk>/tool/", views.sacco_tool_flip, name="sacco_tool_flip"),  # POST staff
    path("merchants/", views.merchant_list, name="merchants"),  # staff
    path("merchants/<int:pk>/tier/", views.merchant_tier, name="merchant_tier"),  # POST
]
