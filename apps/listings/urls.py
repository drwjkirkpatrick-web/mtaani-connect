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

Prompt 24 lifecycle routes (all POST-only, seller-only one-taps; the
detail page's lifecycle panel posts them; refusals flash plain reasons):

  pause         /market/<pk>/pause/   — ACTIVE → PAUSED
  resume        /market/<pk>/resume/  — PAUSED → ACTIVE
  sold          /market/<pk>/sold/    — ACTIVE/PAUSED → SOLD (terminal)
  renew         /market/<pk>/renew/   — EXPIRED → ACTIVE, fresh timestamp
  bump          /market/<pk>/bump/    — refresh position, 7-day cap
  recategorize  /market/<pk>/category/ — gated door → UNDER_REVIEW + flag

Prompt 19 geo search (ADDITIVE — the list/detail flows never change):
  search        /market/search/ — GET distance-ring form (area picked from
                the WardPoint dropdown + 5/10/25 km; category, price band
                and condition filters). Query code: apps/listings/search.py.
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
    # Prompt 24 lifecycle (POST-only seller one-taps — see docstring):
    path("<int:pk>/pause/", views.listing_pause, name="pause"),
    path("<int:pk>/resume/", views.listing_resume, name="resume"),
    path("<int:pk>/sold/", views.listing_mark_sold, name="sold"),
    path("<int:pk>/renew/", views.listing_renew, name="renew"),
    path("<int:pk>/bump/", views.listing_bump, name="bump"),
    path("<int:pk>/category/", views.listing_recategorize, name="recategorize"),
    # Prompt 19 (additive): GET ring search — see docstring above. Placed
    # after the existing routes; ?search/ path is never shadowed by <int:pk>/,
    # which only captures numeric segments.
    path("search/", views.listing_search, name="search"),
]
