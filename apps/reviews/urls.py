"""URL routes for the Reviews app (Prompt 22).

app_name = "reviews" — mtaani/urls.py already includes this under /reviews/
(parent prewired; root urls.py is not touched here).

Route set (mobile-first, few pages): the member's two read surfaces
(received / left), the leave form, and the POST-only abuse report. There is
deliberately NO publish/edit/delete route — the state machine lives ONLY in
the sweep (services.publish_due_reviews).
"""
from django.urls import path

from . import views

app_name = "reviews"

urlpatterns = [
    path("", views.received_reviews, name="list"),  # reviews received
    path("leave/", views.leave_review, name="leave"),
    path("mine/", views.my_reviews, name="my_reviews"),
    path(
        "<int:review_id>/report/",
        views.report_review,
        name="report",
    ),
]
