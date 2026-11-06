"""URL routes for the Guides app (Prompt 08).

app_name = "guides" — every name lives in the namespace, so templates and
tests reverse names like `guides:list`. mtaani/urls.py already includes this
module under /guides/.
"""
from django.urls import path

from . import views

app_name = "guides"

urlpatterns = [
    path("", views.guide_list, name="list"),
    path("new/", views.guide_create, name="create"),
    # Prompt 09: offline saved guides. Plain-string paths BEFORE the <int:pk>
    # patterns are belt-and-braces (the int converter would reject them
    # anyway) — readability over trickery.
    path("offline/", views.offline_list, name="offline"),
    path("offline-manifest.json", views.offline_manifest, name="offline_manifest"),
    path("<int:pk>/", views.guide_detail, name="detail"),
    path("<int:pk>/edit/", views.guide_edit, name="edit"),
    path("<int:pk>/history/", views.guide_history, name="history"),
    path("<int:pk>/publish/", views.guide_publish, name="publish"),
    path("<int:pk>/delete/", views.guide_delete, name="delete"),
    path("<int:pk>/confirm/", views.guide_confirm, name="confirm"),
    path("<int:pk>/offline/save/", views.offline_save, name="offline_save"),
    path("<int:pk>/offline/unsave/", views.offline_unsave, name="offline_unsave"),
]
