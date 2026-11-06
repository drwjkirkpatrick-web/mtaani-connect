"""URL routes for the Disputes app (Prompt 23).

app_name = "disputes" — mtaani/urls.py already includes this under
/disputes/ (parent prewired; root urls.py is not touched here).

Routes: member surfaces (open / mine / case detail + evidence), and the
moderator dashboard (queue / start-review / propose / confirm /
resolve / dismiss — POST-only state changes, two-actor gated in services).
"""
from django.urls import path

from . import views

app_name = "disputes"

urlpatterns = [
    path("", views.queue_redirect, name="home"),
    path("open/", views.open_case, name="open"),
    path("mine/", views.my_cases, name="my_cases"),
    path("queue/", views.queue, name="queue"),
    path("<int:case_id>/", views.detail, name="detail"),
    path("<int:case_id>/evidence/", views.evidence_add, name="evidence_add"),
    path("<int:case_id>/start-review/", views.start_review, name="start_review"),
    path("<int:case_id>/propose/", views.propose, name="propose"),
    path("<int:case_id>/resolve/", views.resolve_route, name="resolve"),
    path("<int:case_id>/dismiss/", views.dismiss, name="dismiss"),
    path("<int:case_id>/confirm/", views.confirm, name="confirm_proposal"),
]
