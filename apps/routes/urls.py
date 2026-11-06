"""URL routes for the Routes app (Prompt 25).

app_name = "routes" — every name lives in this namespace so templates and
tests reverse names like ``routes:list``. mtaani/urls.py already includes
this module under /routes/.
"""
from django.urls import path

from . import views

app_name = "routes"

urlpatterns = [
    path("", views.index, name="list"),
    path("search/", views.search, name="search"),
    path("corrections/", views.corrections_mine, name="corrections_mine"),
    path("corrections/new/", views.correction_new, name="correction_new"),
    path("corrections/<int:pk>/resolve/", views.correction_resolve, name="correction_resolve"),
    path("queue/", views.correction_queue, name="queue"),
    path("stage/<int:pk>/", views.stage_detail, name="stage_detail"),
    path("<int:pk>/", views.route_detail, name="detail"),
]
