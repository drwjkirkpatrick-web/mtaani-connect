"""URL routes for the Trust app (Prompt 16).

app_name = "trust" — mtaani/urls.py already includes this module under
/trust/ (parent prewired; do not touch root urls.py here).

Route set: vote endpoints are POST-only, login-required, idempotent per
(user, target) — voting again is a no-op, POST undo=1 revokes. Reads of trust
data go through services (helpfulness_badges / usefulness_scorer), not pages.
"""
from django.urls import path

from . import views

app_name = "trust"

urlpatterns = [
    path("posts/<int:post_id>/vote/", views.post_vote, name="post_vote"),
    path("answers/<int:answer_id>/vote/", views.answer_vote, name="answer_vote"),
]
