"""URL routes for the Services app (Prompt 18).

app_name = "services" — templates and tests reverse names like
`services:list`. mtaani/urls.py includes this module under /services/ (the
parent pre-wired it).

Route set (final report ships these names to the parent + sibling apps):
  list          /services/ — chips + ?q= title search + ?provider=<pk> filter
  create        /services/new/ — publish-on-create (login)
  detail        /services/<pk>/ — portfolio grid + rates block
                (enquiry link /chats/?service=<pk> — contract in services.py)
  edit          /services/<pk>/edit/ — provider-only
  image_upload  /services/<pk>/images/upload/ — POST-only JSON (8-image cap)
  remove        /services/<pk>/remove/ — soft remove → REMOVED (provider-only)
  provider      /services/provider/<user_pk>/ — provider profile: portfolio +
                rates, NO goods (Prompt 18 acceptance surface)
"""
from django.urls import path

from . import views

app_name = "services"

urlpatterns = [
    path("", views.service_list, name="list"),
    path("new/", views.service_create, name="create"),
    path("provider/<int:provider_id>/", views.provider_page, name="provider"),
    path("<int:pk>/", views.service_detail, name="detail"),
    path("<int:pk>/edit/", views.service_edit, name="edit"),
    path("<int:pk>/images/upload/", views.service_image_upload, name="image_upload"),
    path("<int:pk>/remove/", views.service_remove, name="remove"),
]
