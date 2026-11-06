"""URL routes for the Feed app (Prompt 11).

app_name = "feed" — templates and tests reverse names like `feed:list`.
mtaani/urls.py already includes this module under /feed/ (parent-owned file).

Route set (final report ships these names to the parent):
  list          ranked utility feed, 10/page (?page=N)
"""
from django.urls import path

from . import views

app_name = "feed"

urlpatterns = [
    path("", views.feed, name="list"),
]
