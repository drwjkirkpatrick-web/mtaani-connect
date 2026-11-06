"""URL routes for the Listings app (Prompt 17).

app_name = "listings" — templates and tests reverse names like
`listings:list`. mtaani/urls.py includes this module under /market/ (the
parent pre-wired it; the market stub is retired).

Route set (final report ships these names to the parent + sibling apps):
  list          /market/ — chips + ?q= title search (?category=<value>)
  create        /market/new/ — publish-on-create (login)
  detail        /market/<pk>/ — the listing page ("Message seller" link)
  edit          /market/<pk>/edit/ — seller-only
  image_upload  /market/<pk>/images/upload/ — POST-only JSON (6-image cap)
  remove        /market/<pk>/remove/ — soft remove → REMOVED (seller-only)
"""
from django.urls import path

from . import views

app_name = "listings"

urlpatterns = [
    path("", views.listing_list, name="list"),
    path("new/", views.listing_create, name="create"),
    path("<int:pk>/", views.listing_detail, name="detail"),
    path("<int:pk>/edit/", views.listing_edit, name="edit"),
    path("<int:pk>/images/upload/", views.listing_image_upload, name="image_upload"),
    path("<int:pk>/remove/", views.listing_remove, name="remove"),
]
